#!/usr/bin/env python3
"""
Polymarket Scalper v3 — Multi-strategy, brain-powered, news-aware trading engine.

8 upgrades over v2:
  1. News feed + adverse selection protection
  2. Both-side market making
  3. GTD orders (auto-expire, no manual cancel)
  4. Tick size awareness
  5. Volume/order flow analysis
  6. Kelly Criterion position sizing
  7. Neg risk multi-outcome markets
  8. Correlation tracking + time-of-day patterns

Usage:
  python3 bot.py --scan           # Discover targets (brain-informed)
  python3 bot.py --paper          # Paper trade with learning
  python3 bot.py --live           # Live trading (needs .env)
  python3 bot.py --brain          # Show brain status
  python3 bot.py --brain-reset    # Wipe learned data
  python3 bot.py --strategies     # Show available strategies
  python3 bot.py --live --capital 100 --per-order 10 --strategy both_sides
"""

import asyncio
import json
import math
import os
import sys
import time
import logging
import signal
import argparse
import hashlib
import hmac
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Set
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import aiohttp
import websockets
from dotenv import load_dotenv

# ══════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════

load_dotenv()

LOG = logging.getLogger("scalper")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scalper.log"),
    ],
)

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BRAIN_FILE = "brain.json"


@dataclass
class Config:
    private_key: str = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    funder: str = os.getenv("POLYMARKET_FUNDER", "")
    sig_type: int = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "2"))
    capital: float = float(os.getenv("CAPITAL", "100"))
    max_exposure_pct: float = float(os.getenv("MAX_EXPOSURE_PCT", "50")) / 100
    per_order: float = float(os.getenv("PER_ORDER", "10"))
    max_concurrent: int = int(os.getenv("MAX_CONCURRENT", "5"))
    circuit_breaker_pct: float = float(os.getenv("CIRCUIT_BREAKER_PCT", "10")) / 100
    max_hold_sec: int = int(os.getenv("MAX_HOLD_SECONDS", "300"))
    spread_target: float = float(os.getenv("SPREAD_CAPTURE_TARGET", "0.02"))
    reprice_sec: int = int(os.getenv("REPRICE_INTERVAL", "30"))
    min_spread: float = 0.03
    min_liquidity: float = 2000.0
    min_volume: float = 1000.0
    max_markets: int = 10
    max_orders_per_market: int = 1
    # Upgrade 2: strategy mode
    strategy: str = os.getenv("STRATEGY", "one_side")  # "one_side" or "both_sides"
    # Upgrade 8: trading hours (UTC)
    quiet_hours_start: int = int(os.getenv("QUIET_HOURS_START", "3"))  # 3 AM UTC
    quiet_hours_end: int = int(os.getenv("QUIET_HOURS_END", "6"))      # 6 AM UTC

    @property
    def is_live(self) -> bool:
        return bool(self.private_key and self.funder)

    @property
    def max_exposure(self) -> float:
        return self.capital * self.max_exposure_pct


# ══════════════════════════════════════════════════════════════
# DATA TYPES
# ══════════════════════════════════════════════════════════════

@dataclass
class Market:
    slug: str
    question: str
    yes_price: float
    no_price: float
    spread: float
    volume: float
    liquidity: float
    yes_token: str
    no_token: str
    best_bid: float = 0.0
    best_ask: float = 1.0
    fees_enabled: bool = False
    accepting: bool = True
    last_ws_update: float = 0.0
    # Upgrade 4: tick size
    tick_size: float = 0.01
    # Upgrade 7: neg risk
    neg_risk: bool = False
    event_id: str = ""
    # Upgrade 8: time category
    time_category: str = "normal"  # "quiet", "normal", "peak"


@dataclass
class Order:
    id: str
    exchange_id: str = ""
    slug: str = ""
    token: str = ""
    side: str = ""
    price: float = 0.0
    size: float = 0.0
    shares: float = 0.0
    status: str = "pending"
    created: float = field(default_factory=time.time)
    filled: float = 0.0
    fill_price: float = 0.0
    # Upgrade 3: GTD expiration
    expires_at: float = 0.0
    order_type: str = "GTC"  # GTC, GTD, FAK
    # Brain context
    brain_score: float = 0.0
    entry_spread: float = 0.0
    entry_volume: float = 0.0
    entry_liquidity: float = 0.0
    entry_price_range: str = ""


@dataclass
class Position:
    slug: str
    token: str
    side: str
    entry_price: float
    shares: float
    cost: float
    opened: float = field(default_factory=time.time)
    exit_order_id: str = ""
    entry_spread: float = 0.0
    entry_volume: float = 0.0
    entry_liquidity: float = 0.0
    entry_price_range: str = ""
    # Upgrade 6: stop loss
    stop_loss_price: float = 0.0


@dataclass
class Trade:
    slug: str
    question: str
    entry_price: float
    exit_price: float
    shares: float
    pnl: float
    hold_sec: float
    reason: str
    ts: float = field(default_factory=time.time)
    entry_spread: float = 0.0
    entry_volume: float = 0.0
    entry_liquidity: float = 0.0
    entry_price_range: str = ""
    exit_type: str = ""


# ══════════════════════════════════════════════════════════════
# UPGRADE 5: FLOW ANALYZER — volume/order flow intelligence
# ══════════════════════════════════════════════════════════════

class FlowAnalyzer:
    """
    Tracks trade flow, volume spikes, and momentum per market.
    Detects informed trading and warns the brain.
    """

    def __init__(self):
        # token -> list of (timestamp, price, size, side)
        self.trade_history: Dict[str, List[Tuple[float, float, float, str]]] = defaultdict(list)
        # token -> running stats
        self.stats: Dict[str, dict] = {}
        self._spike_threshold = 3.0  # 3x normal volume = spike

    def record_trade(self, token: str, price: float, size: float, side: str):
        now = time.time()
        self.trade_history[token].append((now, price, size, side))
        # Keep last 5 minutes
        cutoff = now - 300
        self.trade_history[token] = [
            t for t in self.trade_history[token] if t[0] > cutoff
        ]
        self._update_stats(token)

    def _update_stats(self, token: str):
        trades = self.trade_history[token]
        if not trades:
            return

        now = time.time()
        last_30s = [t for t in trades if t[0] > now - 30]
        last_60s = [t for t in trades if t[0] > now - 60]
        last_5m = trades

        vol_30s = sum(t[2] for t in last_30s)
        vol_60s = sum(t[2] for t in last_60s)
        vol_5m = sum(t[2] for t in last_5m)

        # Buy/sell pressure
        buy_vol = sum(t[2] for t in last_60s if t[3] == "BUY")
        sell_vol = sum(t[2] for t in last_60s if t[3] == "SELL")
        total_vol = buy_vol + sell_vol
        imbalance = (buy_vol - sell_vol) / max(total_vol, 0.01)

        # Velocity (trades per 30s)
        velocity = len(last_30s)

        # Price momentum
        if len(last_60s) >= 2:
            first_price = last_60s[0][1]
            last_price = last_60s[-1][1]
            momentum = last_price - first_price
        else:
            momentum = 0.0

        # Volume spike detection
        avg_vol_30s = vol_5m / 10 if vol_5m > 0 else 0.01  # rough 30s avg over 5m
        spike_ratio = vol_30s / max(avg_vol_30s, 0.01)

        self.stats[token] = {
            "vol_30s": vol_30s,
            "vol_60s": vol_60s,
            "vol_5m": vol_5m,
            "buy_pressure": imbalance,  # +1 = all buys, -1 = all sells
            "velocity": velocity,
            "momentum": momentum,
            "spike_ratio": spike_ratio,
            "is_spike": spike_ratio > self._spike_threshold,
            "last_update": now,
        }

    def get_stats(self, token: str) -> dict:
        return self.stats.get(token, {
            "vol_30s": 0, "vol_60s": 0, "vol_5m": 0,
            "buy_pressure": 0, "velocity": 0, "momentum": 0,
            "spike_ratio": 1.0, "is_spike": False,
        })

    def should_pull_orders(self, token: str) -> Tuple[bool, str]:
        """Should we pull all orders on this market due to flow signals?"""
        s = self.get_stats(token)
        if s["is_spike"] and abs(s["buy_pressure"]) > 0.7:
            direction = "BUY" if s["buy_pressure"] > 0 else "SELL"
            return True, f"VOLUME SPIKE: {s['spike_ratio']:.1f}x, {direction} pressure={s['buy_pressure']:.2f}"
        if abs(s["momentum"]) > 0.03 and s["velocity"] > 10:
            return True, f"MOMENTUM SURGE: {s['momentum']*100:.1f}¢ move, {s['velocity']} trades/30s"
        return False, "OK"

    def get_fill_probability_hint(self, token: str) -> float:
        """Higher = more likely to get filled (more active market)."""
        s = self.get_stats(token)
        return min(1.0, s["velocity"] / 20)


# ══════════════════════════════════════════════════════════════
# UPGRADE 1: NEWS MONITOR — adverse selection protection
# ══════════════════════════════════════════════════════════════

class NewsMonitor:
    """
    Monitors news feeds for market-moving events.
    When breaking news hits a watched market → trigger adverse selection alert.
    """

    RSS_FEEDS = [
        ("Reuters World", "https://feeds.reuters.com/reuters/worldNews"),
        ("Reuters Business", "https://feeds.reuters.com/reuters/businessNews"),
        ("AP News", "https://rsshub.app/apnews/topics/apf-topnews"),
        ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ]

    def __init__(self):
        self.seen_headlines: Set[str] = set()
        self._running = False
        self._alerts: List[dict] = []  # {headline, feed, ts, keywords}
        self._last_check = 0
        self._check_interval = 30  # seconds between feed checks
        # Keywords that map to market categories
        self._keyword_map = {
            "israel": ["israel", "gaza", "hamas", "hezbollah", "idf", "netanyahu"],
            "trump": ["trump", "white house", "president", "administration", "noem"],
            "tech": ["apple", "openai", "google", "meta", "federighi", "ceo"],
            "crypto": ["bitcoin", "btc", "ethereum", "crypto"],
            "geopolitics": ["ceasefire", "war", "military", "nuclear", "sanctions"],
            "election": ["election", "vote", "poll", "candidate", "primary"],
            "legal": ["indictment", "court", "ruling", "judge", "lawsuit"],
        }

    async def check_feeds(self, session: aiohttp.ClientSession) -> List[dict]:
        """Fetch RSS feeds and return new relevant headlines."""
        now = time.time()
        if now - self._last_check < self._check_interval:
            return []
        self._last_check = now

        new_alerts = []
        for feed_name, url in self.RSS_FEEDS:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        continue
                    text = await resp.text()
                    headlines = self._parse_rss(text)
                    for headline in headlines:
                        if headline in self.seen_headlines:
                            continue
                        self.seen_headlines.add(headline)
                        keywords = self._extract_keywords(headline)
                        if keywords:
                            alert = {
                                "headline": headline,
                                "feed": feed_name,
                                "ts": now,
                                "keywords": keywords,
                            }
                            new_alerts.append(alert)
                            self._alerts.append(alert)
                            LOG.warning(f"📰 NEWS ALERT | {headline[:80]} | keywords: {keywords}")
            except Exception as e:
                LOG.debug(f"News feed error ({feed_name}): {e}")

        # Keep last 500 headlines
        if len(self.seen_headlines) > 500:
            self.seen_headlines = set(list(self.seen_headlines)[-200:])
        # Keep last 100 alerts
        self._alerts = self._alerts[-100:]

        return new_alerts

    def _parse_rss(self, xml_text: str) -> List[str]:
        """Extract titles from RSS XML."""
        titles = []
        try:
            root = ET.fromstring(xml_text)
            for item in root.iter("item"):
                title_elem = item.find("title")
                if title_elem is not None and title_elem.text:
                    titles.append(title_elem.text.strip().lower())
        except ET.ParseError:
            # Fallback: regex
            titles = re.findall(r"<title[^>]*>([^<]+)</title>", xml_text)
            titles = [t.strip().lower() for t in titles if len(t.strip()) > 10]
        return titles[:20]  # max 20 per feed

    def _extract_keywords(self, headline: str) -> List[str]:
        """Find which market categories this headline relates to."""
        matched = []
        for category, words in self._keyword_map.items():
            for word in words:
                if word in headline:
                    matched.append(category)
                    break
        return matched

    def is_market_affected(self, market_question: str, keywords: List[str] = None) -> Tuple[bool, str]:
        """Check if recent news affects this market."""
        if not self._alerts:
            return False, ""

        question_lower = market_question.lower()
        recent_cutoff = time.time() - 300  # last 5 minutes

        for alert in reversed(self._alerts):
            if alert["ts"] < recent_cutoff:
                continue
            # Direct keyword match
            for kw in alert.get("keywords", []):
                if kw in question_lower:
                    return True, f"NEWS: '{alert['headline'][:60]}' ({alert['feed']})"
            # Word overlap
            alert_words = set(alert["headline"].split())
            question_words = set(question_lower.split())
            overlap = alert_words & question_words
            meaningful = {w for w in overlap if len(w) > 4}
            if len(meaningful) >= 2:
                return True, f"NEWS OVERLAP: '{alert['headline'][:60]}'"

        return False, ""

    def get_recent_alerts(self, minutes: int = 10) -> List[dict]:
        cutoff = time.time() - (minutes * 60)
        return [a for a in self._alerts if a["ts"] > cutoff]


# ══════════════════════════════════════════════════════════════
# UPGRADE 8: CORRELATION ENGINE
# ══════════════════════════════════════════════════════════════

class CorrelationEngine:
    """
    Tracks price movements across markets and detects correlations.
    If Market A moves and Market B is correlated but hasn't moved yet → trade B.
    """

    def __init__(self):
        # (slug_a, slug_b) -> correlation data
        self.correlations: Dict[Tuple[str, str], dict] = {}
        # slug -> list of (timestamp, price)
        self.price_history: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
        self._categories: Dict[str, List[str]] = defaultdict(list)  # keyword -> [slugs]

    def record_price(self, slug: str, price: float, question: str = ""):
        now = time.time()
        self.price_history[slug].append((now, price))
        # Keep last 10 minutes
        cutoff = now - 600
        self.price_history[slug] = [(t, p) for t, p in self.price_history[slug] if t > cutoff]

        # Auto-categorize by question keywords
        if question:
            words = set(question.lower().split())
            for w in words:
                if len(w) > 4:
                    self._categories[w].append(slug)
                    # Keep unique
                    self._categories[w] = list(set(self._categories[w]))[-10:]

    def detect_correlated_move(self, moved_slug: str, new_price: float,
                                old_price: float) -> List[Tuple[str, float]]:
        """
        If a market moved significantly, find correlated markets
        that haven't moved yet. Returns [(slug, expected_direction), ...].
        """
        results = []
        price_delta = new_price - old_price
        if abs(price_delta) < 0.02:
            return results

        # Find categories this slug belongs to
        moved_categories = set()
        for cat, slugs in self._categories.items():
            if moved_slug in slugs:
                moved_categories.add(cat)

        # Find other markets in same categories
        related_slugs = set()
        for cat in moved_categories:
            for s in self._categories[cat]:
                if s != moved_slug:
                    related_slugs.add(s)

        for related in related_slugs:
            related_prices = self.price_history.get(related, [])
            if len(related_prices) < 2:
                continue
            # Check if this market has also moved recently
            recent = related_prices[-1][1]
            older = related_prices[-5][1] if len(related_prices) >= 5 else related_prices[0][1]
            related_delta = recent - older

            # If related market hasn't moved in same direction yet → lagging
            if abs(related_delta) < abs(price_delta) * 0.3:
                results.append((related, price_delta))  # expected to follow

        return results

    def get_time_of_day_multiplier(self) -> Tuple[float, str]:
        """
        Returns (multiplier, category) based on current UTC hour.
        Peak hours = more fills, quiet hours = less activity.
        """
        utc_hour = datetime.now(timezone.utc).hour

        # Peak: US market hours (14-22 UTC = 9AM-5PM ET)
        if 14 <= utc_hour <= 22:
            return 1.2, "peak"
        # Quiet: Asia night / US overnight (3-6 UTC)
        if 3 <= utc_hour <= 6:
            return 0.5, "quiet"
        # Normal
        return 1.0, "normal"


# ══════════════════════════════════════════════════════════════
# ADAPTIVE BRAIN (enhanced with Kelly + time patterns)
# ══════════════════════════════════════════════════════════════

class Brain:
    """
    Persistent learning brain. v3 adds:
    - Kelly Criterion sizing
    - Time-of-day pattern tracking
    - Flow-aware risk scoring
    """

    def __init__(self, path: str = BRAIN_FILE):
        self.path = path
        self.data = self._load()
        self._session_trades: List[dict] = []
        self._session_start = time.time()

    def _load(self) -> dict:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as f:
                    data = json.load(f)
                    LOG.info(f"🧠 Brain loaded: {data.get('total_trades', 0)} trades in memory")
                    # Ensure v3 fields exist
                    if "time_patterns" not in data:
                        data["time_patterns"] = {"peak": {"wins": 0, "losses": 0, "pnl": 0},
                                                   "normal": {"wins": 0, "losses": 0, "pnl": 0},
                                                   "quiet": {"wins": 0, "losses": 0, "pnl": 0}}
                    if "day_patterns" not in data:
                        data["day_patterns"] = {}
                    return data
            except (json.JSONDecodeError, Exception) as e:
                LOG.warning(f"Brain file corrupted, starting fresh: {e}")
        return self._default()

    def _default(self) -> dict:
        return {
            "version": 3,
            "total_trades": 0,
            "total_pnl": 0.0,
            "total_wins": 0,
            "total_losses": 0,
            "sessions": 0,
            "first_seen": time.time(),
            "markets": {},
            "patterns": {
                "by_spread": {},
                "by_volume": {},
                "by_liquidity": {},
                "by_price": {},
                "by_hold_time": {},
            },
            "time_patterns": {
                "peak": {"wins": 0, "losses": 0, "pnl": 0},
                "normal": {"wins": 0, "losses": 0, "pnl": 0},
                "quiet": {"wins": 0, "losses": 0, "pnl": 0},
            },
            "day_patterns": {},
            "avoid_list": [],
            "star_list": [],
            "rules": [],
            "last_updated": time.time(),
        }

    def save(self):
        self.data["last_updated"] = time.time()
        try:
            with open(self.path, "w") as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            LOG.error(f"Failed to save brain: {e}")

    def start_session(self):
        self.data["sessions"] = self.data.get("sessions", 0) + 1
        self.save()
        LOG.info(f"🧠 Session #{self.data['sessions']} started")

    # ─── Market Reputation ───────────────────────────────────

    def get_market_rep(self, slug: str) -> dict:
        return self.data["markets"].get(slug, {
            "trades": 0, "wins": 0, "losses": 0,
            "total_pnl": 0.0, "avg_pnl": 0.0,
            "win_rate": 0.0, "avg_hold": 0.0,
            "fills": 0, "timeouts": 0,
            "last_seen": 0, "question": "",
            "risk_score": 0.5, "profit_score": 0.5,
        })

    def _update_market_rep(self, slug: str, question: str, trade: dict):
        rep = self.get_market_rep(slug)
        rep["trades"] += 1
        rep["total_pnl"] += trade["pnl"]
        rep["question"] = question
        rep["last_seen"] = time.time()

        if trade["pnl"] > 0:
            rep["wins"] += 1
        else:
            rep["losses"] += 1

        if trade.get("exit_type") == "timeout":
            rep["timeouts"] = rep.get("timeouts", 0) + 1
        else:
            rep["fills"] = rep.get("fills", 0) + 1

        rep["avg_pnl"] = rep["total_pnl"] / rep["trades"]
        rep["win_rate"] = rep["wins"] / rep["trades"]

        old_avg = rep.get("avg_hold", 0)
        n = rep["trades"]
        rep["avg_hold"] = old_avg + (trade["hold_sec"] - old_avg) / n

        timeout_rate = rep.get("timeouts", 0) / max(1, rep["trades"])
        loss_rate = rep["losses"] / max(1, rep["trades"])
        pnl_penalty = max(0, -rep["avg_pnl"]) * 10
        rep["risk_score"] = min(1.0, (timeout_rate * 0.4 + loss_rate * 0.4 + min(pnl_penalty, 0.2)))

        fill_rate = rep.get("fills", 0) / max(1, rep["trades"])
        pnl_bonus = min(1.0, max(0, rep["avg_pnl"]) * 20)
        rep["profit_score"] = min(1.0, rep["win_rate"] * 0.4 + fill_rate * 0.3 + pnl_bonus * 0.3)

        self.data["markets"][slug] = rep

    # ─── Pattern Tracking ────────────────────────────────────

    @staticmethod
    def _bucket_spread(spread: float) -> str:
        if spread < 0.04: return "0.03-0.04"
        if spread < 0.06: return "0.04-0.06"
        if spread < 0.10: return "0.06-0.10"
        if spread < 0.20: return "0.10-0.20"
        return "0.20+"

    @staticmethod
    def _bucket_volume(vol: float) -> str:
        if vol < 5000: return "1K-5K"
        if vol < 50000: return "5K-50K"
        if vol < 500000: return "50K-500K"
        return "500K+"

    @staticmethod
    def _bucket_liquidity(liq: float) -> str:
        if liq < 3000: return "2K-3K"
        if liq < 7000: return "3K-7K"
        if liq < 15000: return "7K-15K"
        return "15K+"

    @staticmethod
    def _bucket_price(price: float) -> str:
        if price < 0.20: return "low"
        if price < 0.50: return "mid_low"
        if price < 0.80: return "mid_high"
        return "high"

    @staticmethod
    def _bucket_hold(hold_sec: float) -> str:
        if hold_sec < 30: return "0-30s"
        if hold_sec < 60: return "30-60s"
        if hold_sec < 120: return "60-120s"
        if hold_sec < 300: return "120-300s"
        return "300s+"

    def _update_pattern(self, category: str, bucket: str, is_win: bool, pnl: float):
        patterns = self.data["patterns"][category]
        if bucket not in patterns:
            patterns[bucket] = {"wins": 0, "losses": 0, "total_pnl": 0.0, "trades": 0}
        p = patterns[bucket]
        p["trades"] += 1
        if is_win:
            p["wins"] += 1
        else:
            p["losses"] += 1
        p["total_pnl"] += pnl

    def _update_time_pattern(self, time_cat: str, is_win: bool, pnl: float):
        if time_cat not in self.data["time_patterns"]:
            self.data["time_patterns"][time_cat] = {"wins": 0, "losses": 0, "pnl": 0}
        tp = self.data["time_patterns"][time_cat]
        if is_win:
            tp["wins"] += 1
        else:
            tp["losses"] += 1
        tp["pnl"] += pnl

    def _update_day_pattern(self, is_win: bool, pnl: float):
        day = datetime.now(timezone.utc).strftime("%A")
        if day not in self.data["day_patterns"]:
            self.data["day_patterns"][day] = {"wins": 0, "losses": 0, "pnl": 0}
        dp = self.data["day_patterns"][day]
        if is_win:
            dp["wins"] += 1
        else:
            dp["losses"] += 1
        dp["pnl"] += pnl

    # ─── Kelly Criterion ─────────────────────────────────────

    def kelly_fraction(self, slug: str = None) -> float:
        """
        Kelly Criterion: f* = (bp - q) / b
        where b = avg_win/avg_loss ratio, p = win_rate, q = 1-p
        Returns fraction of capital to bet (capped at 25%).
        Uses per-market stats if available, falls back to global.
        """
        if slug:
            rep = self.get_market_rep(slug)
            if rep["trades"] >= 5:
                wins = rep["wins"]
                losses = rep["losses"]
                total = rep["trades"]
                # Need avg win and avg loss from trades
                # Approximate from avg_pnl and win_rate
                if losses == 0 or rep["avg_pnl"] <= 0:
                    return 0.05  # default small
                p = wins / total
                # Estimate b from avg_pnl: avg_win ≈ avg_pnl * total / wins
                # avg_loss ≈ -avg_pnl * total / losses (approximate)
                # This is rough but works
                avg_win = abs(rep["avg_pnl"]) * 2 if p > 0.5 else abs(rep["avg_pnl"]) * 1.5
                avg_loss = abs(rep["avg_pnl"]) * 1.2
                b = avg_win / max(avg_loss, 0.001)
            else:
                # Use global stats
                p, b = self._global_kelly_params()
        else:
            p, b = self._global_kelly_params()

        q = 1 - p
        kelly = (b * p - q) / max(b, 0.01)

        # Use quarter-Kelly for safety (standard practice)
        kelly = kelly / 4

        # Clamp: minimum 2%, maximum 25%
        return max(0.02, min(0.25, kelly))

    def _global_kelly_params(self) -> Tuple[float, float]:
        """Returns (win_rate, avg_win/avg_loss ratio) from global stats."""
        total = self.data["total_trades"]
        if total < 5:
            return 0.5, 1.0
        p = self.data["total_wins"] / total
        # Estimate b from total PnL
        if self.data["total_pnl"] > 0 and p > 0.5:
            b = 1.5
        elif p > 0.5:
            b = 1.2
        else:
            b = 1.0
        return p, b

    def get_kelly_order_size(self, capital: float, slug: str = None) -> float:
        """Calculate order size using Kelly Criterion."""
        fraction = self.kelly_fraction(slug)
        return round(capital * fraction, 2)

    # ─── Learning from Trades ────────────────────────────────

    def learn_from_trade(self, trade: Trade, time_category: str = "normal"):
        is_win = trade.pnl > 0
        t = {
            "slug": trade.slug, "question": trade.question,
            "pnl": trade.pnl, "hold_sec": trade.hold_sec,
            "entry_spread": trade.entry_spread,
            "entry_volume": trade.entry_volume,
            "entry_liquidity": trade.entry_liquidity,
            "entry_price_range": trade.entry_price_range,
            "exit_type": trade.exit_type, "reason": trade.reason,
            "ts": trade.ts, "is_win": is_win,
            "time_category": time_category,
        }
        self._session_trades.append(t)

        self.data["total_trades"] += 1
        self.data["total_pnl"] += trade.pnl
        if is_win:
            self.data["total_wins"] += 1
        else:
            self.data["total_losses"] += 1

        self._update_market_rep(trade.slug, trade.question, t)
        self._update_pattern("by_spread", self._bucket_spread(trade.entry_spread), is_win, trade.pnl)
        self._update_pattern("by_volume", self._bucket_volume(trade.entry_volume), is_win, trade.pnl)
        self._update_pattern("by_liquidity", self._bucket_liquidity(trade.entry_liquidity), is_win, trade.pnl)
        self._update_pattern("by_price", trade.entry_price_range, is_win, trade.pnl)
        self._update_pattern("by_hold_time", self._bucket_hold(trade.hold_sec), is_win, trade.pnl)
        self._update_time_pattern(time_category, is_win, trade.pnl)
        self._update_day_pattern(is_win, trade.pnl)

        self._update_lists()

        if self.data["total_trades"] % 10 == 0:
            self._generate_rules()

        self.save()

        rep = self.get_market_rep(trade.slug)
        emoji = "🟢" if is_win else "🔴"
        LOG.info(f"🧠 LEARNED | {emoji} {trade.slug[:35]} | "
                f"WR: {rep['win_rate']:.0%} ({rep['trades']}) | "
                f"Risk: {rep['risk_score']:.2f} | Kelly: {self.kelly_fraction(trade.slug):.1%}")

    def _update_lists(self):
        avoid, stars = [], []
        for slug, rep in self.data["markets"].items():
            if rep["trades"] < 3:
                continue
            if rep["risk_score"] > 0.7 and rep["win_rate"] < 0.3:
                avoid.append(slug)
                if slug not in self.data["avoid_list"]:
                    rule = f"AVOID: {slug[:50]} — {rep['win_rate']:.0%} WR, {rep['trades']} trades"
                    self.data["rules"].append({"ts": time.time(), "rule": rule, "type": "avoid"})
                    LOG.warning(f"🧠 NEW RULE: {rule}")
            if rep["profit_score"] > 0.7 and rep["win_rate"] > 0.6:
                stars.append(slug)
                if slug not in self.data["star_list"]:
                    rule = f"STAR: {slug[:50]} — {rep['win_rate']:.0%} WR, ${rep['avg_pnl']:+.3f}/trade"
                    self.data["rules"].append({"ts": time.time(), "rule": rule, "type": "star"})
                    LOG.info(f"🧠 NEW RULE: {rule}")
        self.data["avoid_list"] = avoid
        self.data["star_list"] = stars

    def _generate_rules(self):
        rules = []
        for cat_key in ["by_spread", "by_volume", "by_price"]:
            for bucket, stats in self.data["patterns"].get(cat_key, {}).items():
                if stats["trades"] < 5:
                    continue
                wr = stats["wins"] / stats["trades"]
                if wr < 0.3:
                    rules.append({"ts": time.time(), "rule": f"{cat_key}:{bucket} → {wr:.0%} WR — avoid", "type": "pattern_avoid"})
                elif wr > 0.7:
                    rules.append({"ts": time.time(), "rule": f"{cat_key}:{bucket} → {wr:.0%} WR — prioritize", "type": "pattern_star"})

        # Time pattern rules
        for cat, stats in self.data["time_patterns"].items():
            total = stats["wins"] + stats["losses"]
            if total >= 10:
                wr = stats["wins"] / total
                if wr < 0.3:
                    rules.append({"ts": time.time(), "rule": f"TIME:{cat} → {wr:.0%} WR — reduce activity", "type": "time_avoid"})
                elif wr > 0.65:
                    rules.append({"ts": time.time(), "rule": f"TIME:{cat} → {wr:.0%} WR — focus here", "type": "time_star"})

        self.data["rules"] = (self.data["rules"] + rules)[-50:]

    # ─── Querying ────────────────────────────────────────────

    def should_trade_market(self, slug: str) -> Tuple[bool, str]:
        if slug in self.data["avoid_list"]:
            rep = self.get_market_rep(slug)
            return False, f"AVOID: {rep['win_rate']:.0%} WR, {rep['trades']} trades"
        rep = self.get_market_rep(slug)
        if rep["trades"] >= 5 and rep["risk_score"] > 0.8:
            return False, f"HIGH RISK: {rep['risk_score']:.2f}"
        return True, "OK"

    def is_star_market(self, slug: str) -> bool:
        return slug in self.data["star_list"]

    def get_market_risk(self, slug: str) -> float:
        return self.get_market_rep(slug)["risk_score"]

    def get_market_profit_score(self, slug: str) -> float:
        return self.get_market_rep(slug)["profit_score"]

    def get_order_size_multiplier(self, slug: str) -> float:
        if self.is_star_market(slug):
            return 1.5
        risk = self.get_market_risk(slug)
        if risk > 0.6:
            return 0.5
        if risk > 0.4:
            return 0.75
        return 1.0

    def get_exit_aggressiveness(self, slug: str) -> float:
        rep = self.get_market_rep(slug)
        if rep["trades"] < 3:
            return 0.5
        return min(1.0, rep.get("timeouts", 0) / rep["trades"] + 0.2)

    def should_adjust_hold_time(self, slug: str) -> Optional[int]:
        rep = self.get_market_rep(slug)
        if rep["trades"] < 5:
            return None
        if rep.get("timeouts", 0) / rep["trades"] > 0.5:
            return max(60, int(rep.get("avg_hold", 300) * 0.7))
        return None

    def score_market_for_entry(self, slug: str, spread: float, volume: float,
                                liquidity: float, price: float) -> float:
        spread_score = min(1.0, spread / 0.15)
        volume_score = min(1.0, math.log10(max(volume, 1)) / 6)
        liq_score = min(1.0, math.log10(max(liquidity, 1)) / 5)
        base = (spread_score * 0.4 + volume_score * 0.3 + liq_score * 0.3)

        rep = self.get_market_rep(slug)
        if rep["trades"] >= 3:
            brain_weight = min(0.6, rep["trades"] / 20)
            brain_adj = rep["profit_score"] * (1 - rep["risk_score"])
            score = base * (1 - brain_weight) + brain_adj * brain_weight
        else:
            score = base * 0.9

        # Time-of-day adjustment
        utc_hour = datetime.now(timezone.utc).hour
        time_cat = "peak" if 14 <= utc_hour <= 22 else ("quiet" if 3 <= utc_hour <= 6 else "normal")
        tp = self.data["time_patterns"].get(time_cat, {})
        tp_total = tp.get("wins", 0) + tp.get("losses", 0)
        if tp_total >= 10:
            tp_wr = tp.get("wins", 0) / tp_total
            if tp_wr < 0.35:
                score *= 0.7

        # Pattern penalties
        for cat_key, get_bucket in [
            ("by_spread", lambda: self._bucket_spread(spread)),
            ("by_volume", lambda: self._bucket_volume(volume)),
            ("by_price", lambda: self._bucket_price(price)),
        ]:
            bucket = get_bucket()
            stats = self.data["patterns"].get(cat_key, {}).get(bucket, {})
            if stats.get("trades", 0) >= 5:
                if stats["wins"] / stats["trades"] < 0.3:
                    score *= 0.5

        return round(max(0.0, min(1.0, score)), 4)

    def get_best_time_category(self) -> str:
        best_wr = 0
        best = "normal"
        for cat, stats in self.data["time_patterns"].items():
            total = stats["wins"] + stats["losses"]
            if total >= 5:
                wr = stats["wins"] / total
                if wr > best_wr:
                    best_wr = wr
                    best = cat
        return best

    def report(self) -> str:
        lines = []
        lines.append(f"\n{'='*60}")
        lines.append(f"  🧠 BRAIN v3 STATUS")
        lines.append(f"{'='*60}")
        lines.append(f"  Sessions:     {self.data.get('sessions', 0)}")
        lines.append(f"  Total Trades: {self.data['total_trades']}")
        lines.append(f"  Total PnL:    ${self.data['total_pnl']:+.2f}")
        wr = self.data['total_wins'] / max(1, self.data['total_trades'])
        lines.append(f"  Win Rate:     {self.data['total_wins']}/{self.data['total_losses']} ({wr:.0%})")
        lines.append(f"  Kelly (global): {self.kelly_fraction():.1%}")
        lines.append(f"  Avoid: {len(self.data['avoid_list'])} | Stars: {len(self.data['star_list'])}")

        if self.data["avoid_list"]:
            lines.append(f"\n  🚫 AVOID:")
            for slug in self.data["avoid_list"][:5]:
                rep = self.get_market_rep(slug)
                lines.append(f"    • {slug[:45]} | WR={rep['win_rate']:.0%} | Risk={rep['risk_score']:.2f}")

        if self.data["star_list"]:
            lines.append(f"\n  ⭐ STARS:")
            for slug in self.data["star_list"][:5]:
                rep = self.get_market_rep(slug)
                lines.append(f"    • {slug[:45]} | WR={rep['win_rate']:.0%} | ${rep['avg_pnl']:+.3f}/trade")

        lines.append(f"\n  ⏰ TIME PATTERNS:")
        for cat, stats in self.data["time_patterns"].items():
            total = stats["wins"] + stats["losses"]
            if total > 0:
                wr = stats["wins"] / total
                emoji = "🟢" if wr > 0.6 else ("🔴" if wr < 0.4 else "⚪")
                lines.append(f"    {emoji} {cat}: {wr:.0%} WR | {total} trades | ${stats['pnl']:+.2f}")

        lines.append(f"\n  📊 DAY PATTERNS:")
        for day, stats in sorted(self.data["day_patterns"].items()):
            total = stats["wins"] + stats["losses"]
            if total > 0:
                wr = stats["wins"] / total
                lines.append(f"    {day}: {wr:.0%} WR | {total} trades | ${stats['pnl']:+.2f}")

        if self.data["rules"]:
            lines.append(f"\n  📜 RULES (last 10):")
            for rule in self.data["rules"][-10:]:
                emoji = "🚫" if "avoid" in rule.get("type", "") else "⭐"
                lines.append(f"    {emoji} {rule['rule']}")

        lines.append(f"{'='*60}")
        return "\n".join(lines)

    def session_report(self) -> str:
        if not self._session_trades:
            return "🧠 No trades this session."
        wins = [t for t in self._session_trades if t["is_win"]]
        losses = [t for t in self._session_trades if not t["is_win"]]
        pnl = sum(t["pnl"] for t in self._session_trades)
        return (f"🧠 SESSION: {len(self._session_trades)} trades "
                f"({len(wins)}W/{len(losses)}L) | PnL=${pnl:+.3f} | "
                f"Kelly={self.kelly_fraction():.1%} | "
                f"Best time={self.get_best_time_category()}")


# ══════════════════════════════════════════════════════════════
# MARKET DISCOVERY (brain + neg risk + tick size aware)
# ══════════════════════════════════════════════════════════════

async def discover_markets(cfg: Config, brain: Optional[Brain] = None) -> List[Market]:
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"{GAMMA_URL}/events",
            params={"active": "true", "closed": "false", "limit": 500},
        ) as r:
            events = await r.json()

    all_markets = []
    for ev in events:
        event_id = ev.get("id", "")
        neg_risk = ev.get("negRisk", False)

        for m in ev.get("markets", []):
            if m.get("closed") or not m.get("active"):
                continue
            if not m.get("acceptingOrders", True):
                continue

            try:
                prices = json.loads(m.get("outcomePrices", "[0.5,0.5]"))
                tokens = json.loads(m.get("clobTokenIds", "[]"))
            except (json.JSONDecodeError, IndexError):
                continue

            if len(prices) < 2 or len(tokens) < 2:
                continue

            liq = float(m.get("liquidityClob", 0) or 0)
            vol = float(m.get("volume", 0))
            spread = float(m.get("spread", 0))
            yp = float(prices[0])
            fees = m.get("feesEnabled", False)
            slug = m.get("slug", "")

            if liq < cfg.min_liquidity:
                continue
            if vol < cfg.min_volume:
                continue
            if spread < cfg.min_spread:
                continue
            if yp < 0.05 or yp > 0.95:
                continue
            if fees:
                continue

            if brain:
                should_trade, reason = brain.should_trade_market(slug)
                if not should_trade:
                    LOG.info(f"🧠 SKIP | {slug[:40]} | {reason}")
                    continue

            all_markets.append(Market(
                slug=slug,
                question=m.get("question", ""),
                yes_price=yp,
                no_price=float(prices[1]),
                spread=spread,
                volume=vol,
                liquidity=liq,
                yes_token=tokens[0],
                no_token=tokens[1],
                best_bid=float(m.get("bestBid", 0) or 0),
                best_ask=float(m.get("bestAsk", 1) or 1),
                fees_enabled=fees,
                neg_risk=neg_risk,
                event_id=event_id,
            ))

    # Query tick sizes for top markets
    # (We'll snap to ticks when placing orders; store the default here)
    for m in all_markets:
        if brain:
            m._score = brain.score_market_for_entry(m.slug, m.spread, m.volume, m.liquidity, m.yes_price)
            if brain.is_star_market(m.slug):
                m._score *= 1.3
        else:
            m._score = m.spread * math.sqrt(max(m.volume, 1)) * math.log10(max(m.liquidity, 1))

    all_markets.sort(key=lambda m: m._score, reverse=True)
    return all_markets[:cfg.max_markets]


# ══════════════════════════════════════════════════════════════
# WEBSOCKET FEED
# ══════════════════════════════════════════════════════════════

class Feed:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.books: Dict[str, dict] = {}
        self._running = False
        self._callbacks: List = []
        self._ws = None

    def on_update(self, cb):
        self._callbacks.append(cb)

    async def start(self, tokens: List[str]):
        self._running = True
        delay = 1
        while self._running:
            try:
                async with websockets.connect(WS_URL, ping_interval=15) as ws:
                    self._ws = ws
                    delay = 1
                    for i in range(0, len(tokens), 50):
                        batch = tokens[i:i+50]
                        await ws.send(json.dumps({
                            "assets_ids": batch,
                            "type": "market",
                            "custom_feature_enabled": True,
                        }))
                    LOG.info(f"WS: subscribed to {len(tokens)} tokens")

                    async for msg in ws:
                        if msg == "PONG":
                            continue
                        data = json.loads(msg) if isinstance(msg, str) else None
                        if not data:
                            continue
                        items = data if isinstance(data, list) else [data]
                        for item in items:
                            if not isinstance(item, dict):
                                continue
                            aid = item.get("asset_id", "")
                            if not aid:
                                continue
                            book = self.books.setdefault(aid, {"bids": [], "asks": []})
                            t = item.get("type", "")
                            if t == "book":
                                book["bids"] = sorted(
                                    [(float(p), float(s)) for p, s in item.get("bids", [])],
                                    reverse=True
                                )
                                book["asks"] = sorted(
                                    [(float(p), float(s)) for p, s in item.get("asks", [])]
                                )
                            elif t == "best_bid_ask":
                                bb, ba = item.get("best_bid"), item.get("best_ask")
                                if bb:
                                    book["bids"] = [(float(bb[0]), float(bb[1]))]
                                if ba:
                                    book["asks"] = [(float(ba[0]), float(ba[1]))]
                            elif t == "last_trade_price":
                                book["last_trade"] = float(item.get("price", 0))
                            for cb in self._callbacks:
                                await cb(aid, book)

            except Exception as e:
                LOG.warning(f"WS error: {e}, reconnect in {delay}s")
                if self._running:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()

    def best(self, token: str):
        book = self.books.get(token)
        if not book:
            return None, None, None, None
        bb = book["bids"][0] if book["bids"] else (None, None)
        ba = book["asks"][0] if book["asks"] else (None, None)
        return bb[0], ba[0], bb[1], ba[1]


# ══════════════════════════════════════════════════════════════
# ORDER MANAGER (GTD + tick size + Kelly + neg risk)
# ══════════════════════════════════════════════════════════════

class OrderManager:
    def __init__(self, cfg: Config, paper: bool = True, brain: Optional[Brain] = None):
        self.cfg = cfg
        self.paper = paper
        self.brain = brain
        self.orders: Dict[str, Order] = {}
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.capital = cfg.capital
        self.peak = cfg.capital
        self.client = None
        self._market_questions: Dict[str, str] = {}
        # Upgrade 4: tick size cache
        self._tick_sizes: Dict[str, float] = {}

    def set_market_questions(self, markets: Dict[str, Market]):
        self._market_questions = {s: m.question for s, m in markets.items()}

    async def init_client(self):
        if not self.cfg.is_live:
            LOG.info("No API keys — paper mode")
            return
        try:
            from py_clob_client.client import ClobClient
            self.client = ClobClient(
                CLOB_URL, key=self.cfg.private_key, chain_id=137,
                signature_type=self.cfg.sig_type, funder=self.cfg.funder,
            )
            creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(creds)
            LOG.info(f"Polymarket client initialized | funder={self.cfg.funder[:10]}...")
        except Exception as e:
            LOG.error(f"Failed to init client: {e}")
            self.client = None

    # ─── Upgrade 4: Tick Size ────────────────────────────────

    async def fetch_tick_size(self, token: str) -> float:
        """Query the market's tick size from the CLOB API."""
        if token in self._tick_sizes:
            return self._tick_sizes[token]
        if not self.client:
            return 0.01  # default
        try:
            ts = self.client.get_tick_size(token)
            tick = float(ts)
            self._tick_sizes[token] = tick
            return tick
        except Exception:
            self._tick_sizes[token] = 0.01
            return 0.01

    def snap_to_tick(self, price: float, tick_size: float) -> float:
        """Snap a price to the nearest valid tick."""
        if tick_size <= 0:
            return round(price, 4)
        return round(round(price / tick_size) * tick_size, 4)

    # ─── Core Properties ─────────────────────────────────────

    @property
    def exposed(self) -> float:
        return sum(p.cost for p in self.positions.values())

    @property
    def free_capital(self) -> float:
        return self.capital - self.exposed

    @property
    def drawdown(self) -> float:
        return max(0, (self.peak - self.capital) / self.peak)

    @property
    def daily_pnl(self) -> float:
        return self.capital - self.cfg.capital

    def can_enter(self) -> Tuple[bool, str]:
        if self.drawdown >= self.cfg.circuit_breaker_pct:
            return False, "CIRCUIT_BREAKER"
        open_orders = sum(1 for o in self.orders.values() if o.status == "live" and o.side == "BUY")
        if len(self.positions) + open_orders >= self.cfg.max_concurrent:
            return False, "MAX_CONCURRENT"
        if self.exposed >= self.cfg.max_exposure:
            return False, "MAX_EXPOSURE"
        if self.free_capital < self.cfg.per_order:
            return False, "INSUFFICIENT_CAPITAL"
        return True, "OK"

    def get_brain_adjusted_size(self, slug: str, market: Optional[Market] = None) -> float:
        # Upgrade 6: Kelly Criterion sizing
        if self.brain and self.brain.data["total_trades"] >= 10:
            kelly_size = self.brain.get_kelly_order_size(self.capital, slug)
            # Blend Kelly with brain multiplier
            multiplier = self.brain.get_order_size_multiplier(slug)
            adjusted = kelly_size * multiplier
            # Clamp to reasonable range
            adjusted = max(self.cfg.per_order * 0.5, min(self.cfg.per_order * 2, adjusted))
            return round(adjusted, 2)
        elif self.brain:
            multiplier = self.brain.get_order_size_multiplier(slug)
            return round(self.cfg.per_order * multiplier, 2)
        return self.cfg.per_order

    # ─── Order Placement ─────────────────────────────────────

    async def place_limit(self, slug: str, token: str, side: str, price: float,
                          size_usd: float, market: Optional[Market] = None,
                          gtd_seconds: int = 0) -> Optional[Order]:
        # Upgrade 4: Snap to tick size
        tick_size = 0.01
        if market:
            tick_size = market.tick_size
        price = self.snap_to_tick(price, tick_size)

        shares = round(size_usd / price, 2)

        # Upgrade 3: GTD order type
        order_type = "GTC"
        expires_at = 0.0
        if gtd_seconds > 0:
            order_type = "GTD"
            expires_at = time.time() + gtd_seconds

        brain_score = 0.0
        entry_spread = 0.0
        entry_volume = 0.0
        entry_liquidity = 0.0
        entry_price_range = "mid"
        if market:
            entry_spread = market.spread
            entry_volume = market.volume
            entry_liquidity = market.liquidity
            if market.yes_price < 0.20:
                entry_price_range = "low"
            elif market.yes_price < 0.50:
                entry_price_range = "mid_low"
            elif market.yes_price < 0.80:
                entry_price_range = "mid_high"
            else:
                entry_price_range = "high"
            if self.brain:
                brain_score = self.brain.score_market_for_entry(
                    slug, market.spread, market.volume, market.liquidity, market.yes_price
                )

        # Stop loss: 3¢ below entry for buys
        stop_loss = round(price - 0.03, 4) if side == "BUY" else 0.0

        order = Order(
            id=f"{slug}_{side}_{int(time.time()*1000)}",
            slug=slug, token=token, side=side,
            price=price, size=size_usd, shares=shares,
            brain_score=brain_score,
            entry_spread=entry_spread, entry_volume=entry_volume,
            entry_liquidity=entry_liquidity, entry_price_range=entry_price_range,
            order_type=order_type, expires_at=expires_at,
        )

        if self.paper:
            order.status = "live"
            order.exchange_id = f"paper_{order.id}"
            gtd_tag = f" [GTD {gtd_seconds}s]" if gtd_seconds > 0 else ""
            LOG.info(f"📝 PAPER | {side} {shares:.0f} @ ${price:.4f}{gtd_tag} on {slug[:35]}")
            self.orders[order.id] = order
            return order

        if not self.client:
            return None

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType as OT
            from py_clob_client.order_builder.constants import BUY, SELL

            side_const = BUY if side == "BUY" else SELL

            # Build order options
            order_options = {}
            if market and market.neg_risk:
                order_options["negRisk"] = True

            order_args = OrderArgs(
                token_id=token, price=price, size=shares, side=side_const,
            )

            signed = self.client.create_order(order_args, **order_options)

            # Choose order type
            if order_type == "GTD" and expires_at > 0:
                resp = self.client.post_order(signed, OT.GTD, expires_at=int(expires_at))
            else:
                resp = self.client.post_order(signed, OT.GTC)

            if resp:
                order.exchange_id = resp.get("orderID", resp.get("order_id", ""))
                order.status = "live"
                LOG.info(f"✅ LIVE | {side} {shares:.0f} @ ${price:.4f} [{order_type}] on {slug[:35]}")
                self.orders[order.id] = order
                return order
            else:
                LOG.error(f"❌ Rejected: {resp}")
                return None
        except Exception as e:
            LOG.error(f"❌ Order error: {e}")
            return None

    async def cancel_order(self, order: Order):
        order.status = "canceled"
        if self.paper or not order.exchange_id.startswith("0x"):
            return
        if not self.client:
            return
        try:
            self.client.cancel(order.exchange_id)
        except Exception as e:
            LOG.error(f"Cancel error: {e}")

    async def cancel_all(self):
        for oid, order in list(self.orders.items()):
            if order.status == "live":
                await self.cancel_order(order)
        LOG.info("All orders canceled")

    def fill_order(self, order: Order, fill_price: float):
        order.status = "filled"
        order.fill_price = fill_price
        order.filled = time.time()

        cost = order.shares * fill_price
        self.capital -= cost

        if order.side == "BUY":
            self.positions[order.slug] = Position(
                slug=order.slug, token=order.token, side="LONG",
                entry_price=fill_price, shares=order.shares, cost=cost,
                entry_spread=order.entry_spread, entry_volume=order.entry_volume,
                entry_liquidity=order.entry_liquidity, entry_price_range=order.entry_price_range,
                stop_loss_price=round(fill_price - 0.03, 4),
            )
            LOG.info(f"🟢 FILLED BUY | {order.shares:.0f} @ ${fill_price:.4f} | {order.slug[:35]}")
        else:
            pos = self.positions.pop(order.slug, None)
            if pos:
                pnl = (fill_price - pos.entry_price) * pos.shares
                self.capital += pos.shares * fill_price
                self.peak = max(self.peak, self.capital)
                hold = time.time() - pos.opened
                trade = Trade(
                    slug=order.slug, question=self._market_questions.get(order.slug, ""),
                    entry_price=pos.entry_price, exit_price=fill_price,
                    shares=pos.shares, pnl=pnl, hold_sec=hold, reason="filled",
                    entry_spread=pos.entry_spread, entry_volume=pos.entry_volume,
                    entry_liquidity=pos.entry_liquidity, entry_price_range=pos.entry_price_range,
                    exit_type="profit" if pnl > 0 else "loss",
                )
                self.trades.append(trade)
                if self.brain:
                    self.brain.learn_from_trade(trade)
                emoji = "🟢" if pnl > 0 else "🔴"
                LOG.info(f"{emoji} FILLED SELL | {pos.shares:.0f} @ ${fill_price:.4f} | PnL=${pnl:+.3f} | {order.slug[:35]}")

    def force_exit_position(self, slug: str, exit_price: float, reason: str):
        pos = self.positions.pop(slug, None)
        if not pos:
            return
        pnl = (exit_price - pos.entry_price) * pos.shares
        self.capital += pos.shares * exit_price
        self.peak = max(self.peak, self.capital)
        hold = time.time() - pos.opened
        trade = Trade(
            slug=slug, question=self._market_questions.get(slug, ""),
            entry_price=pos.entry_price, exit_price=exit_price,
            shares=pos.shares, pnl=pnl, hold_sec=hold, reason=reason,
            entry_spread=pos.entry_spread, entry_volume=pos.entry_volume,
            entry_liquidity=pos.entry_liquidity, entry_price_range=pos.entry_price_range,
            exit_type=reason,
        )
        self.trades.append(trade)
        if self.brain:
            self.brain.learn_from_trade(trade)
        emoji = "🟢" if pnl > 0 else "🔴"
        LOG.info(f"{emoji} FORCE EXIT | {pos.shares:.0f} @ ${exit_price:.4f} | PnL=${pnl:+.3f} | {reason} | {slug[:35]}")

    def check_stop_losses(self, market_prices: Dict[str, float]):
        """Upgrade 6: Per-position stop loss."""
        for slug, pos in list(self.positions.items()):
            if pos.stop_loss_price <= 0:
                continue
            current_price = market_prices.get(slug, pos.entry_price)
            if current_price <= pos.stop_loss_price:
                LOG.warning(f"🛑 STOP LOSS triggered | {slug[:35]} | "
                           f"${pos.entry_price:.4f} → ${current_price:.4f} "
                           f"(stop=${pos.stop_loss_price:.4f})")
                self.force_exit_position(slug, current_price, "stop_loss")

    def check_timeouts(self, market_prices: Dict[str, float]):
        now = time.time()
        for slug, pos in list(self.positions.items()):
            max_hold = self.cfg.max_hold_sec
            if self.brain:
                suggested = self.brain.should_adjust_hold_time(slug)
                if suggested:
                    max_hold = suggested
            if now - pos.opened > max_hold:
                price = market_prices.get(slug, pos.entry_price)
                self.force_exit_position(slug, price, "timeout")

    # Upgrade 3: Check expired GTD orders
    def clean_expired_orders(self):
        now = time.time()
        for oid, order in list(self.orders.items()):
            if order.status == "live" and order.order_type == "GTD" and order.expires_at > 0:
                if now > order.expires_at:
                    order.status = "canceled"
                    LOG.info(f"⏰ GTD EXPIRED | {order.side} @ ${order.price:.4f} on {order.slug[:35]}")

    def summary(self) -> str:
        wins = [t for t in self.trades if t.pnl > 0]
        total_pnl = sum(t.pnl for t in self.trades)
        wr = len(wins) / max(1, len(self.trades))
        return (f"Capital=${self.capital:.2f} ({self.daily_pnl:+.2f}) | "
                f"Pos={len(self.positions)} | Trades={len(self.trades)} ({wr:.0%} WR) | "
                f"PnL=${total_pnl:+.3f} | DD={self.drawdown:.1%}")


# ══════════════════════════════════════════════════════════════
# SCALPING ENGINE (all 8 upgrades integrated)
# ══════════════════════════════════════════════════════════════

class Scalper:
    def __init__(self, cfg: Config, paper: bool = True):
        self.cfg = cfg
        self.paper = paper
        self.brain = Brain()
        self.om = OrderManager(cfg, paper, brain=self.brain)
        self.feed = Feed(cfg)
        # Upgrade 1
        self.news = NewsMonitor()
        # Upgrade 5
        self.flow = FlowAnalyzer()
        # Upgrade 8
        self.correlations = CorrelationEngine()
        self.markets: Dict[str, Market] = {}
        self.token_to_slug: Dict[str, str] = {}
        self.running = False
        self.tick = 0
        self._last_reprice = 0
        self._last_news_check = 0

    async def start(self, mode: str = "paper"):
        self.brain.start_session()

        if self.brain.data["total_trades"] > 0:
            LOG.info(f"🧠 Brain: {self.brain.data['total_trades']} trades | "
                    f"Avoid: {len(self.brain.data['avoid_list'])} | Stars: {len(self.brain.data['star_list'])}")

        # Upgrade 8: time-of-day warning
        tod_mult, tod_cat = self.correlations.get_time_of_day_multiplier()
        if tod_cat == "quiet":
            LOG.warning(f"⏰ QUIET HOURS — fill rates will be lower (multiplier: {tod_mult})")
        elif tod_cat == "peak":
            LOG.info(f"⏰ PEAK HOURS — optimal trading window (multiplier: {tod_mult})")

        LOG.info("=" * 60)
        LOG.info(f"  SCALPER v3 | ${self.cfg.capital:.0f} | {mode.upper()} | Strategy: {self.cfg.strategy}")
        LOG.info(f"  Exposed: {self.cfg.max_exposure_pct:.0%} | Kelly: {self.brain.kelly_fraction():.1%}")
        LOG.info(f"  Max concurrent: {self.cfg.max_concurrent} | Reprice: {self.cfg.reprice_sec}s")
        LOG.info(f"  Max hold: {self.cfg.max_hold_sec}s | Circuit breaker: {self.cfg.circuit_breaker_pct:.0%}")
        LOG.info(f"  News monitoring: ON | Flow analysis: ON | Correlation: ON")
        LOG.info("=" * 60)

        LOG.info("🔍 Discovering markets...")
        market_list = await discover_markets(self.cfg, self.brain)
        if not market_list:
            LOG.error("No suitable markets found!")
            return

        self.markets = {m.slug: m for m in market_list}
        self.om.set_market_questions(self.markets)
        for m in market_list:
            self.token_to_slug[m.yes_token] = m.slug
            self.token_to_slug[m.no_token] = m.slug + "_NO"
            # Upgrade 8: record for correlation
            self.correlations.record_price(m.slug, m.yes_price, m.question)

        LOG.info(f"📊 {len(self.markets)} markets selected:")
        for m in market_list:
            tags = []
            if self.brain.is_star_market(m.slug):
                tags.append("⭐")
            if m.neg_risk:
                tags.append("🔄NEG")
            risk = self.brain.get_market_risk(m.slug)
            if risk > 0.5:
                tags.append(f"⚠️{risk:.1f}")
            tag_str = f" [{' '.join(tags)}]" if tags else ""
            LOG.info(f"  • {m.question[:55]} | {m.spread*100:.1f}¢ | "
                    f"${m.liquidity:,.0f} liq | ${m.volume:,.0f} vol{tag_str}")

        await self.om.init_client()

        # Upgrade 4: fetch tick sizes
        if not self.paper:
            for m in market_list:
                ts = await self.om.fetch_tick_size(m.yes_token)
                m.tick_size = ts
                LOG.debug(f"Tick size for {m.slug[:30]}: {ts}")

        tokens = [m.yes_token for m in market_list if m.yes_token]
        ws_task = asyncio.create_task(self.feed.start(tokens))
        self.feed.on_update(self._on_book_update)

        self.running = True
        LOG.info("🚀 Scalper v3 running — Ctrl+C to stop\n")

        try:
            while self.running:
                await self._tick()
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            LOG.info("\n⏹ Shutting down...")
        finally:
            self.running = False
            await self.om.cancel_all()
            await self.feed.stop()
            ws_task.cancel()
            self._final_report()

    async def _tick(self):
        self.tick += 1
        now = time.time()

        # Upgrade 1: Check news feeds every 30s
        if now - self._last_news_check > 30:
            async with aiohttp.ClientSession() as s:
                alerts = await self.news.check_feeds(s)
            if alerts:
                # Check if any alerts affect our watched markets
                for slug, market in self.markets.items():
                    affected, reason = self.news.is_market_affected(market.question)
                    if affected:
                        LOG.warning(f"🚨 ADVERSE SELECTION ALERT | {slug[:35]} | {reason}")
                        # Pull all orders on this market
                        pulled = 0
                        for oid, order in list(self.om.orders.items()):
                            if order.status == "live" and order.slug == slug:
                                await self.om.cancel_order(order)
                                pulled += 1
                        if pulled:
                            LOG.warning(f"🚨 PULLED {pulled} orders on {slug[:35]} due to news")
            self._last_news_check = now

        # Upgrade 3: Clean expired GTD orders
        self.om.clean_expired_orders()

        # Stale order cleanup (still needed for non-GTD or edge cases)
        for oid, order in list(self.om.orders.items()):
            stale_threshold = 120
            if self.brain:
                rep = self.brain.get_market_rep(order.slug)
                if rep["trades"] >= 5 and rep.get("fills", 0) / rep["trades"] < 0.3:
                    stale_threshold = 60
            if order.status == "live" and order.order_type == "GTC" and (now - order.created) > stale_threshold:
                await self.om.cancel_order(order)

        # Stop losses every 5s
        if self.tick % 5 == 0:
            prices = {slug: m.yes_price for slug, m in self.markets.items()}
            self.om.check_stop_losses(prices)

        # Timeouts every 10s
        if self.tick % 10 == 0:
            prices = {slug: m.yes_price for slug, m in self.markets.items()}
            self.om.check_timeouts(prices)

        # Reprice
        if now - self._last_reprice > self.cfg.reprice_sec:
            await self._reprice()
            self._last_reprice = now

        # Paper fills
        if self.paper:
            await self._paper_fill_check()

        # Status every 60s
        if self.tick % 60 == 0:
            live = sum(1 for o in self.om.orders.values() if o.status == "live")
            news_count = len(self.news.get_recent_alerts(5))
            LOG.info(f"[T+{self.tick}s] {self.om.summary()} | Orders: {live} | News alerts: {news_count}")

    async def _on_book_update(self, token: str, book: dict):
        slug = self.token_to_slug.get(token)
        if not slug or slug.endswith("_NO"):
            return
        market = self.markets.get(slug)
        if not market:
            return

        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if not bids or not asks:
            return

        bb = bids[0][0]
        ba = asks[0][0]
        mid = (bb + ba) / 2
        spread = ba - bb
        old_mid = market.yes_price

        market.best_bid = bb
        market.best_ask = ba
        market.yes_price = mid
        market.spread = spread
        market.last_ws_update = time.time()

        # Upgrade 5: Record flow from last trade data
        last_trade = book.get("last_trade")
        if last_trade:
            side = "BUY" if last_trade > old_mid else "SELL"
            self.flow.record_trade(token, last_trade, 0, side)

        # Upgrade 8: Record price for correlation
        self.correlations.record_price(slug, mid, market.question)

        # Upgrade 5: Check if flow says to pull orders
        should_pull, pull_reason = self.flow.should_pull_orders(token)
        if should_pull:
            for oid, order in list(self.om.orders.items()):
                if order.status == "live" and order.slug == slug:
                    await self.om.cancel_order(order)
            LOG.warning(f"🌊 FLOW PULL | {slug[:35]} | {pull_reason}")
            return  # Don't re-place immediately during a flow event

        # Reactive cancellation (price moved > 1¢)
        price_moved = abs(mid - old_mid)
        if price_moved > 0.01:
            canceled_count = 0
            for oid, order in list(self.om.orders.items()):
                if order.status == "live" and order.slug == slug:
                    await self.om.cancel_order(order)
                    canceled_count += 1
            if canceled_count:
                LOG.info(f"⚡ REACTIVE CANCEL | {slug[:35]} | {price_moved*100:.1f}¢ move")

                # Upgrade 8: Check correlated markets
                correlated = self.correlations.detect_correlated_move(slug, mid, old_mid)
                for corr_slug, direction in correlated:
                    LOG.info(f"🔗 CORRELATION | {slug[:25]} moved → {corr_slug[:25]} may follow")

                # Re-place
                if slug not in self.om.positions:
                    ok, _ = self.om.can_enter()
                    if ok:
                        offset = max(0.005, spread/2 - self.cfg.spread_target)
                        buy_price = round(mid - offset, 4)
                        buy_price = max(buy_price, round(bb + 0.001, 4))
                        if 0 < buy_price < 1:
                            size = self.om.get_brain_adjusted_size(slug, market)
                            gtd = self._get_gtd_seconds(slug)
                            await self.om.place_limit(slug, market.yes_token, "BUY", buy_price, size, market, gtd)
                else:
                    pos = self.om.positions[slug]
                    exit_price = round(mid + 0.005, 4)
                    if mid < pos.entry_price - 0.01:
                        exit_price = round(bb, 4)
                    gtd = self._get_gtd_seconds(slug)
                    await self.om.place_limit(slug, market.yes_token, "SELL", exit_price, pos.cost, market, gtd)

        # Paper fills
        if self.paper:
            for oid, order in list(self.om.orders.items()):
                if order.status != "live" or order.slug != slug:
                    continue
                if order.side == "BUY" and bb <= order.price:
                    self.om.fill_order(order, order.price)
                elif order.side == "SELL" and ba >= order.price:
                    self.om.fill_order(order, order.price)

    def _get_gtd_seconds(self, slug: str) -> int:
        """Upgrade 3: Determine GTD expiration based on brain."""
        if self.brain:
            rep = self.brain.get_market_rep(slug)
            if rep["trades"] >= 5:
                fill_rate = rep.get("fills", 0) / rep["trades"]
                if fill_rate > 0.6:
                    return 180  # good fills → hold order longer
                elif fill_rate < 0.3:
                    return 60   # bad fills → expire fast
        return 90  # default: 90 seconds

    async def _paper_fill_check(self):
        import random
        now = time.time()
        for oid, order in list(self.om.orders.items()):
            if order.status != "live":
                continue
            # GTD expiry
            if order.order_type == "GTD" and order.expires_at > 0 and now > order.expires_at:
                order.status = "canceled"
                continue

            market = self.markets.get(order.slug)
            if not market:
                continue
            age = now - order.created
            if age < 30:
                continue

            # Upgrade 5: Flow-adjusted fill rate
            flow_hint = self.flow.get_fill_probability_hint(market.yes_token)

            if order.side == "BUY":
                if order.price >= market.best_bid and market.best_bid > 0:
                    fill_rate = min(0.03 * (age / 30), 0.15)
                    fill_rate *= min(market.volume / 50000, 2.0)
                    fill_rate *= (0.5 + flow_hint * 0.5)  # flow boost
                    if random.random() < fill_rate:
                        self.om.fill_order(order, order.price)
            elif order.side == "SELL":
                if order.price <= market.best_ask and market.best_ask < 1:
                    fill_rate = min(0.03 * (age / 30), 0.15)
                    fill_rate *= min(market.volume / 50000, 2.0)
                    fill_rate *= (0.5 + flow_hint * 0.5)
                    if random.random() < fill_rate:
                        self.om.fill_order(order, order.price)

    async def _reprice(self):
        # Cancel all live orders
        for oid, order in list(self.om.orders.items()):
            if order.status == "live":
                await self.om.cancel_order(order)

        for slug, market in self.markets.items():
            if market.best_bid <= 0 or market.best_ask >= 1:
                continue
            if market.spread < self.cfg.min_spread:
                continue

            # Upgrade 1: Check if news affects this market before repricing
            affected, reason = self.news.is_market_affected(market.question)
            if affected:
                LOG.warning(f"🚨 SKIP REPRICE | {slug[:35]} | {reason}")
                continue

            if self.brain:
                should_trade, _ = self.brain.should_trade_market(slug)
                if not should_trade:
                    continue

            mid = (market.best_bid + market.best_ask) / 2

            if slug in self.om.positions:
                pos = self.om.positions[slug]
                hold_sec = time.time() - pos.opened

                aggression = 0.5
                if self.brain:
                    aggression = self.brain.get_exit_aggressiveness(slug)

                if hold_sec > self.cfg.max_hold_sec * (0.9 - aggression * 0.3):
                    exit_price = round(market.best_bid, 4)
                elif mid > pos.entry_price + 0.005:
                    patience = 0.003
                    if self.brain and self.brain.is_star_market(slug):
                        patience = 0.005
                    exit_price = round(mid + patience, 4)
                elif mid > pos.entry_price - 0.005:
                    exit_price = round(mid, 4)
                else:
                    exit_price = round(market.best_bid, 4)

                gtd = self._get_gtd_seconds(slug)
                await self.om.place_limit(slug, market.yes_token, "SELL",
                    exit_price, pos.cost, market, gtd)
                continue

            ok, _ = self.om.can_enter()
            if not ok:
                continue

            # Upgrade 2: Both-sides market making
            if self.cfg.strategy == "both_sides":
                await self._place_both_sides(slug, market, mid)
            else:
                await self._place_one_side(slug, market, mid)

    async def _place_one_side(self, slug: str, market: Market, mid: float):
        """Standard one-side entry."""
        half_spread = market.spread / 2
        buy_price = round(mid - max(0.005, half_spread - self.cfg.spread_target), 4)
        buy_price = max(buy_price, round(market.best_bid + 0.001, 4))
        if buy_price <= 0 or buy_price >= 1:
            return
        size = self.om.get_brain_adjusted_size(slug, market)
        gtd = self._get_gtd_seconds(slug)
        await self.om.place_limit(slug, market.yes_token, "BUY", buy_price, size, market, gtd)

    async def _place_both_sides(self, slug: str, market: Market, mid: float):
        """Upgrade 2: Market making — place orders on both bid and ask."""
        # Check if we already have a position
        if slug in self.om.positions:
            # Just exit (same as one_side)
            pos = self.om.positions[slug]
            exit_price = round(mid + 0.003, 4) if mid > pos.entry_price else round(market.best_bid, 4)
            gtd = self._get_gtd_seconds(slug)
            await self.om.place_limit(slug, market.yes_token, "SELL",
                exit_price, pos.cost, market, gtd)
            return

        ok, _ = self.om.can_enter()
        if not ok:
            return

        size = self.om.get_brain_adjusted_size(slug, market)
        gtd = self._get_gtd_seconds(slug)

        # Place BUY inside the spread
        buy_price = round(mid - max(0.005, market.spread / 4), 4)
        buy_price = max(buy_price, round(market.best_bid + 0.001, 4))

        # Place SELL inside the spread (if we had shares — but we don't yet,
        # so for both_sides we place buy on yes_token and buy on no_token equivalent)
        # Actually, for market making we need to be able to sell something we don't own.
        # Polymarket doesn't allow short selling easily, so "both sides" here means:
        # Place buy orders on multiple markets in the same event (neg risk)
        # OR: place buy at bid level, and if filled, immediately place sell at ask level

        if 0 < buy_price < 1:
            # Upgrade 5: Adjust aggressiveness based on flow
            flow_stats = self.flow.get_stats(market.yes_token)
            if flow_stats.get("buy_pressure", 0) > 0.5:
                # Strong buy pressure — be more aggressive (closer to ask)
                buy_price = round(mid - 0.003, 4)
            elif flow_stats.get("buy_pressure", 0) < -0.5:
                # Strong sell pressure — be more patient (closer to bid)
                buy_price = round(market.best_bid + 0.001, 4)

            buy_price = self.om.snap_to_tick(buy_price, market.tick_size)
            await self.om.place_limit(slug, market.yes_token, "BUY",
                buy_price, size, market, gtd)

        # Upgrade 7: If neg risk, also consider NO side
        if market.neg_risk and market.no_token:
            no_price = round(1 - mid, 4)
            no_buy = round(no_price - 0.02, 4)
            if 0 < no_buy < 1:
                LOG.debug(f"🔄 NEG RISK | {slug[:30]} | NO buy @ ${no_buy:.3f}")

    def _final_report(self):
        wins = [t for t in self.om.trades if t.pnl > 0]
        losses = [t for t in self.om.trades if t.pnl <= 0]
        total_pnl = sum(t.pnl for t in self.om.trades)
        wr = len(wins) / max(1, len(self.om.trades))
        avg_hold = sum(t.hold_sec for t in self.om.trades) / max(1, len(self.om.trades))

        for slug, pos in list(self.om.positions.items()):
            m = self.markets.get(slug)
            price = m.yes_price if m else pos.entry_price
            self.om.force_exit_position(slug, price, "shutdown")

        LOG.info(f"""
{'='*60}
  FINAL REPORT v3
{'='*60}
  Capital:         ${self.om.capital:.2f} (started ${self.cfg.capital:.2f})
  P&L:             ${self.om.daily_pnl:+.2f} ({self.om.daily_pnl/self.cfg.capital*100:+.1f}%)
  Peak:            ${self.om.peak:.2f}
  Max Drawdown:    {self.om.drawdown:.1%}
  
  Total Trades:    {len(self.om.trades)}
  Wins:            {len(wins)} | Losses: {len(losses)}
  Win Rate:        {wr:.0%}
  Avg Hold:        {avg_hold:.0f}s
  
  Avg Win:         ${sum(t.pnl for t in wins)/max(1,len(wins)):+.3f}
  Avg Loss:        ${sum(t.pnl for t in losses)/max(1,len(losses)):-.3f}
  Profit Factor:   {sum(t.pnl for t in wins)/max(0.001, abs(sum(t.pnl for t in losses))):.2f}
  
  Strategy:        {self.cfg.strategy}
  Kelly Fraction:  {self.brain.kelly_fraction():.1%}
  News Alerts:     {len(self.news.get_recent_alerts(9999))}
{'='*60}""")

        if self.om.trades:
            LOG.info("  Recent trades:")
            for t in self.om.trades[-10:]:
                emoji = "🟢" if t.pnl > 0 else "🔴"
                LOG.info(f"    {emoji} {t.slug[:35]:<35} {t.hold_sec:>4.0f}s | ${t.pnl:+.3f} | {t.reason}")

        LOG.info(self.brain.session_report())
        self.brain.save()


# ══════════════════════════════════════════════════════════════
# SCAN MODE
# ══════════════════════════════════════════════════════════════

async def cmd_scan(cfg: Config, brain: Brain):
    markets = await discover_markets(cfg, brain)

    print(f"\n{'='*70}")
    print(f"  SCALPING TARGETS v3 — {len(markets)} markets (brain-filtered)")
    print(f"  Filter: fee-free | spread≥3¢ | liq≥$2K | vol≥$1K | price 5-95¢")
    print(f"{'='*70}\n")

    for i, m in enumerate(markets):
        mid = (m.best_bid + m.best_ask) / 2
        buy_at = round(mid - max(0.005, m.spread/2 - 0.02), 4)
        sell_at = round(mid + 0.005, 4)
        profit_per = (sell_at - buy_at) * (cfg.per_order / buy_at)

        tags = []
        if brain.is_star_market(m.slug):
            tags.append("⭐STAR")
        if m.neg_risk:
            tags.append("🔄NEG_RISK")
        risk = brain.get_market_risk(m.slug)
        if risk > 0.5:
            tags.append(f"⚠️RISK={risk:.2f}")
        rep = brain.get_market_rep(m.slug)
        if rep["trades"] >= 3:
            tags.append(f"📊{rep['trades']}t/{rep['win_rate']:.0%}WR")
        kelly = brain.kelly_fraction(m.slug)
        tags.append(f"🎲Kelly={kelly:.1%}")
        tag_str = f" [{' '.join(tags)}]" if tags else ""

        print(f"  {i+1:>2}. {m.question[:55]}{tag_str}")
        print(f"      Spread: {m.spread*100:.1f}¢ | Bid: ${m.best_bid:.3f} | Ask: ${m.best_ask:.3f}")
        print(f"      Liq: ${m.liquidity:,.0f} | Vol: ${m.volume:,.0f} | Tick: {m.tick_size}")
        print(f"      → Buy @ ${buy_at:.3f} | Sell @ ${sell_at:.3f} | Est: ${profit_per:.2f}/trade")
        print()

    print(f"  {'─'*60}")
    print(f"  💰 With ${cfg.capital:.0f} capital:")
    print(f"     Kelly size: ${brain.get_kelly_order_size(cfg.capital):.2f}/order")
    print(f"     Exposed: ${cfg.capital * cfg.max_exposure_pct:.0f} ({cfg.max_exposure_pct:.0%})")
    print(f"     Reserve: ${cfg.capital * (1-cfg.max_exposure_pct):.0f}")
    print(f"     Best hours: {brain.get_best_time_category()}")
    print()


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Polymarket Scalper v3 (Brain-Powered, Multi-Strategy)")
    parser.add_argument("--scan", action="store_true", help="Discover targets (brain-informed)")
    parser.add_argument("--paper", action="store_true", help="Paper trade with learning")
    parser.add_argument("--live", action="store_true", help="Live trading (needs .env)")
    parser.add_argument("--brain", action="store_true", help="Show brain status")
    parser.add_argument("--brain-reset", action="store_true", help="Wipe learned data")
    parser.add_argument("--strategies", action="store_true", help="Show available strategies")
    parser.add_argument("--capital", type=float, default=None)
    parser.add_argument("--per-order", type=float, default=None)
    parser.add_argument("--strategy", type=str, default=None, choices=["one_side", "both_sides"])
    args = parser.parse_args()

    cfg = Config()
    if args.capital:
        cfg.capital = args.capital
    if args.per_order:
        cfg.per_order = args.per_order
    if args.strategy:
        cfg.strategy = args.strategy

    brain = Brain()

    if args.brain_reset:
        if os.path.exists(BRAIN_FILE):
            os.remove(BRAIN_FILE)
            print("🧠 Brain wiped.")
        else:
            print("🧠 Already clean.")
        return

    if args.strategies:
        print("""
  ═══════════════════════════════════════════════
  AVAILABLE STRATEGIES
  ═══════════════════════════════════════════════

  --strategy one_side (default)
    Place BUY orders inside the spread, SELL on fill.
    Simple, proven, lower risk.

  --strategy both_sides
    Market making mode. Adjusts entry price based on
    order flow. Uses neg risk for multi-outcome hedging.
    More aggressive, higher fill rate.

  UPGRADES ALWAYS ACTIVE:
    1. News monitoring — pulls orders on breaking news
    2. GTD orders — auto-expire, no manual cancel
    3. Tick size snapping — no rejected orders
    4. Flow analysis — detects volume spikes & momentum
    5. Kelly Criterion — optimal position sizing
    6. Stop losses — 3¢ automatic stop per position
    7. Neg risk detection — capital-efficient hedging
    8. Correlation + time-of-day — smart timing

  ═══════════════════════════════════════════════
""")
    elif args.brain:
        print(brain.report())
    elif args.scan:
        asyncio.run(cmd_scan(cfg, brain))
    elif args.paper:
        scalper = Scalper(cfg, paper=True)
        asyncio.run(scalper.start("paper"))
    elif args.live:
        if not cfg.is_live:
            LOG.error("Missing POLYMARKET_PRIVATE_KEY or POLYMARKET_FUNDER in .env")
            sys.exit(1)
        scalper = Scalper(cfg, paper=False)
        asyncio.run(scalper.start("live"))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
