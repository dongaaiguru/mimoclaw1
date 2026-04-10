#!/usr/bin/env python3
"""
Polymarket Scalper v4 — Multi-strategy, brain-powered, news-aware trading engine.

8 v3→v4 upgrades:
  1. Book-depth-aware paper fills — uses actual bid/ask levels + sizes from WS
  2. Separate realized vs. committed capital — drawdown only on realized losses
  3. Fix short-selling capital accounting — no double-counting
  4. Spread-proportional GTD — wider spread = longer order life
  5. Both-sides capital guard — halved size, all orders counted
  6. Aggressive neg-risk quoting — decoupled from can_enter()
  7. News decay + un-skip — exponential decay, auto-un-skip after 10 min
  8. Post-only orders — guarantee maker status

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
import random
import sys
import time
import logging
import argparse
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
RULES_FILE = "rules.jsonl"  # v4: supervisor rules (read-only by bot)


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
    reprice_sec: int = int(os.getenv("REPRICE_INTERVAL", "15"))
    min_spread: float = 0.03
    min_liquidity: float = 3000.0
    min_volume: float = 2000.0
    max_markets: int = 10
    max_orders_per_market: int = 1
    strategy: str = os.getenv("STRATEGY", "one_side")
    quiet_hours_start: int = int(os.getenv("QUIET_HOURS_START", "3"))
    quiet_hours_end: int = int(os.getenv("QUIET_HOURS_END", "6"))
    # v4 Upgrade 8: post-only orders (guarantee maker status)
    post_only: bool = os.getenv("POST_ONLY", "true").lower() == "true"
    # v4: AI supervisor mode — all orders go through approval
    supervised: bool = os.getenv("SUPERVISED", "false").lower() == "true"

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
    best_bid_size: float = 0.0  # v4: depth at best bid
    best_ask_size: float = 0.0  # v4: depth at best ask
    fees_enabled: bool = False
    accepting: bool = True
    last_ws_update: float = 0.0
    tick_size: float = 0.01
    neg_risk: bool = False
    event_id: str = ""
    time_category: str = "normal"
    _score: float = 0.0


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
    expires_at: float = 0.0
    order_type: str = "GTC"
    post_only: bool = True  # v4: guarantee maker
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
    stop_loss_price: float = 0.0
    # v4: track committed capital separately for drawdown calc
    committed_capital: float = 0.0


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
# FLOW ANALYZER — volume/order flow intelligence
# ══════════════════════════════════════════════════════════════

class FlowAnalyzer:
    """
    Tracks trade flow, volume spikes, and momentum per market.
    Detects informed trading and warns the brain.
    """

    def __init__(self):
        self.trade_history: Dict[str, List[Tuple[float, float, float, str]]] = defaultdict(list)
        self.stats: Dict[str, dict] = {}
        self._spike_threshold = 2.0

    def record_trade(self, token: str, price: float, size: float, side: str):
        now = time.time()
        self.trade_history[token].append((now, price, size, side))
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

        buy_vol = sum(t[2] for t in last_60s if t[3] == "BUY")
        sell_vol = sum(t[2] for t in last_60s if t[3] == "SELL")
        total_vol = buy_vol + sell_vol
        imbalance = (buy_vol - sell_vol) / max(total_vol, 0.01)

        velocity = len(last_30s)

        if len(last_60s) >= 2:
            first_price = last_60s[0][1]
            last_price = last_60s[-1][1]
            momentum = last_price - first_price
        else:
            momentum = 0.0

        avg_vol_30s = vol_5m / 10 if vol_5m > 0 else 0.01
        spike_ratio = vol_30s / max(avg_vol_30s, 0.01)

        self.stats[token] = {
            "vol_30s": vol_30s,
            "vol_60s": vol_60s,
            "vol_5m": vol_5m,
            "buy_pressure": imbalance,
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
        s = self.get_stats(token)
        if s["is_spike"] and abs(s["buy_pressure"]) > 0.7:
            direction = "BUY" if s["buy_pressure"] > 0 else "SELL"
            return True, f"VOLUME SPIKE: {s['spike_ratio']:.1f}x, {direction} pressure={s['buy_pressure']:.2f}"
        if abs(s["momentum"]) > 0.03 and s["velocity"] > 8:
            return True, f"MOMENTUM SURGE: {s['momentum']*100:.1f}¢ move, {s['velocity']} trades/30s"
        return False, "OK"

    def get_fill_probability_hint(self, token: str) -> float:
        s = self.get_stats(token)
        return min(1.0, s["velocity"] / 20)


# ══════════════════════════════════════════════════════════════
# NEWS MONITOR — adverse selection protection (v4: with decay)
# ══════════════════════════════════════════════════════════════

class NewsMonitor:
    """
    Monitors news feeds for market-moving events.
    v4: Alerts decay exponentially. Markets auto-un-skip after 10 min
    if no fresh matching alerts arrive.
    """

    RSS_FEEDS = [
        ("Reuters World", "https://feeds.reuters.com/reuters/worldNews"),
        ("Reuters Business", "https://feeds.reuters.com/reuters/businessNews"),
        ("AP News", "https://rsshub.app/apnews/topics/apf-topnews"),
        ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ]

    # v4: Alert half-life in seconds (alert weight halves every 120s)
    ALERT_HALF_LIFE = 120.0
    # v4: Markets auto-un-skip after this many seconds with no fresh alerts
    UNSKIP_AFTER_SEC = 600  # 10 minutes

    def __init__(self):
        self.seen_headlines: Set[str] = set()
        self._running = False
        self._alerts: List[dict] = []
        self._last_check = 0
        self._check_interval = 15
        # v4: track when each market was last skipped and why
        self._market_skip_times: Dict[str, float] = {}
        self._keyword_map = {
            "israel": ["israel", "gaza", "hamas", "hezbollah", "idf", "netanyahu"],
            "trump": ["trump", "white house", "president", "administration", "noem"],
            "tech": ["apple", "openai", "google", "meta", "federighi", "ceo"],
            "crypto": ["bitcoin", "btc", "ethereum", "crypto"],
            "geopolitics": ["ceasefire", "war", "military", "nuclear", "sanctions"],
            "election": ["election", "vote", "poll", "candidate", "primary"],
            "legal": ["indictment", "court", "ruling", "judge", "lawsuit"],
        }

    def _alert_weight(self, alert: dict) -> float:
        """v4: Exponential decay weight based on alert age."""
        age = time.time() - alert["ts"]
        return 0.5 ** (age / self.ALERT_HALF_LIFE)

    async def check_feeds(self, session: aiohttp.ClientSession) -> List[dict]:
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

        if len(self.seen_headlines) > 500:
            self.seen_headlines = set(list(self.seen_headlines)[-200:])
        # v4: prune very old alerts (weight < 0.01 = effectively dead)
        self._alerts = [a for a in self._alerts if self._alert_weight(a) > 0.01]
        self._alerts = self._alerts[-200:]

        return new_alerts

    def _parse_rss(self, xml_text: str) -> List[str]:
        titles = []
        try:
            root = ET.fromstring(xml_text)
            for item in root.iter("item"):
                title_elem = item.find("title")
                if title_elem is not None and title_elem.text:
                    titles.append(title_elem.text.strip().lower())
        except ET.ParseError:
            titles = re.findall(r"<title[^>]*>([^<]+)</title>", xml_text)
            titles = [t.strip().lower() for t in titles if len(t.strip()) > 10]
        return titles[:20]

    def _extract_keywords(self, headline: str) -> List[str]:
        matched = []
        for category, words in self._keyword_map.items():
            for word in words:
                if word in headline:
                    matched.append(category)
                    break
        return matched

    def is_market_affected(self, market_question: str, keywords: List[str] = None) -> Tuple[bool, str]:
        """
        v4: Weighted check. Alerts decay over time.
        Returns (affected, reason) where reason includes decayed weight.
        """
        if not self._alerts:
            return False, ""

        question_lower = market_question.lower()
        total_weight = 0.0
        strongest_alert = None

        for alert in reversed(self._alerts):
            weight = self._alert_weight(alert)
            if weight < 0.05:
                continue  # too decayed to matter

            # Direct keyword match
            matched = False
            for kw in alert.get("keywords", []):
                if kw in question_lower:
                    matched = True
                    break

            # Word overlap
            if not matched:
                alert_words = set(alert["headline"].split())
                question_words = set(question_lower.split())
                overlap = alert_words & question_words
                meaningful = {w for w in overlap if len(w) > 4}
                if len(meaningful) >= 2:
                    matched = True

            if matched:
                total_weight += weight
                if strongest_alert is None or weight > self._alert_weight(strongest_alert):
                    strongest_alert = alert

        # v4: threshold — need combined weight > 0.5 to trigger skip
        # A single fresh alert (weight~1.0) triggers it, but old alerts decay away
        if total_weight >= 0.5 and strongest_alert:
            return True, f"NEWS (w={total_weight:.2f}): '{strongest_alert['headline'][:60]}' ({strongest_alert['feed']})"

        return False, ""

    def should_unskip_market(self, slug: str) -> bool:
        """v4: Check if a previously-skipped market should be un-skipped."""
        skip_time = self._market_skip_times.get(slug, 0)
        if skip_time == 0:
            return False
        return (time.time() - skip_time) > self.UNSKIP_AFTER_SEC

    def mark_market_skipped(self, slug: str):
        """v4: Record when a market was skipped due to news."""
        self._market_skip_times[slug] = time.time()

    def clear_market_skip(self, slug: str):
        """v4: Clear skip status for a market."""
        self._market_skip_times.pop(slug, None)

    def get_recent_alerts(self, minutes: int = 10) -> List[dict]:
        cutoff = time.time() - (minutes * 60)
        return [a for a in self._alerts if a["ts"] > cutoff]


# ══════════════════════════════════════════════════════════════
# CORRELATION ENGINE
# ══════════════════════════════════════════════════════════════

class CorrelationEngine:
    def __init__(self):
        self.correlations: Dict[Tuple[str, str], dict] = {}
        self.price_history: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
        self._categories: Dict[str, List[str]] = defaultdict(list)

    def record_price(self, slug: str, price: float, question: str = ""):
        now = time.time()
        self.price_history[slug].append((now, price))
        cutoff = now - 600
        self.price_history[slug] = [(t, p) for t, p in self.price_history[slug] if t > cutoff]

        if question:
            words = set(question.lower().split())
            for w in words:
                if len(w) > 4:
                    self._categories[w].append(slug)
                    self._categories[w] = list(set(self._categories[w]))[-10:]

    def detect_correlated_move(self, moved_slug: str, new_price: float,
                                old_price: float) -> List[Tuple[str, float]]:
        results = []
        price_delta = new_price - old_price
        if abs(price_delta) < 0.02:
            return results

        moved_categories = set()
        for cat, slugs in self._categories.items():
            if moved_slug in slugs:
                moved_categories.add(cat)

        related_slugs = set()
        for cat in moved_categories:
            for s in self._categories[cat]:
                if s != moved_slug:
                    related_slugs.add(s)

        for related in related_slugs:
            related_prices = self.price_history.get(related, [])
            if len(related_prices) < 2:
                continue
            recent = related_prices[-1][1]
            older = related_prices[-5][1] if len(related_prices) >= 5 else related_prices[0][1]
            related_delta = recent - older
            if abs(related_delta) < abs(price_delta) * 0.3:
                results.append((related, price_delta))

        return results

    def get_time_of_day_multiplier(self) -> Tuple[float, str]:
        utc_hour = datetime.now(timezone.utc).hour
        if 14 <= utc_hour <= 22:
            return 1.2, "peak"
        if 3 <= utc_hour <= 6:
            return 0.3, "quiet"
        return 1.0, "normal"


# ══════════════════════════════════════════════════════════════
# ADAPTIVE BRAIN
# ══════════════════════════════════════════════════════════════

class Brain:
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
                    if "time_patterns" not in data:
                        data["time_patterns"] = {
                            "peak": {"wins": 0, "losses": 0, "pnl": 0},
                            "normal": {"wins": 0, "losses": 0, "pnl": 0},
                            "quiet": {"wins": 0, "losses": 0, "pnl": 0},
                        }
                    if "day_patterns" not in data:
                        data["day_patterns"] = {}
                    return data
            except (json.JSONDecodeError, Exception) as e:
                LOG.warning(f"Brain file corrupted, starting fresh: {e}")
        return self._default()

    def _default(self) -> dict:
        return {
            "version": 4,
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
        if slug:
            rep = self.get_market_rep(slug)
            if rep["trades"] >= 5:
                wins = rep["wins"]
                losses = rep["losses"]
                total = rep["trades"]
                if losses == 0 or rep["avg_pnl"] <= 0:
                    return 0.05
                p = wins / total
                avg_win = abs(rep["avg_pnl"]) * 2 if p > 0.5 else abs(rep["avg_pnl"]) * 1.5
                avg_loss = abs(rep["avg_pnl"]) * 1.2
                b = avg_win / max(avg_loss, 0.001)
            else:
                p, b = self._global_kelly_params()
        else:
            p, b = self._global_kelly_params()

        q = 1 - p
        kelly = (b * p - q) / max(b, 0.01)
        kelly = kelly / 3
        return max(0.02, min(0.25, kelly))

    def _global_kelly_params(self) -> Tuple[float, float]:
        total = self.data["total_trades"]
        if total < 5:
            return 0.5, 1.0
        p = self.data["total_wins"] / total
        # v6: Better b estimation based on actual avg win/loss
        if p > 0.7 and self.data["total_pnl"] > 0:
            b = 2.0  # strong edge
        elif p > 0.6:
            b = 1.5
        elif p > 0.5:
            b = 1.2
        else:
            b = 1.0
        return p, b

    def get_kelly_order_size(self, capital: float, slug: str = None) -> float:
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
        base = (spread_score * 0.5 + volume_score * 0.3 + liq_score * 0.2)

        rep = self.get_market_rep(slug)
        if rep["trades"] >= 3:
            brain_weight = min(0.6, rep["trades"] / 20)
            brain_adj = rep["profit_score"] * (1 - rep["risk_score"])
            score = base * (1 - brain_weight) + brain_adj * brain_weight
        else:
            score = base * 0.9

        utc_hour = datetime.now(timezone.utc).hour
        time_cat = "peak" if 14 <= utc_hour <= 22 else ("quiet" if 3 <= utc_hour <= 6 else "normal")
        tp = self.data["time_patterns"].get(time_cat, {})
        tp_total = tp.get("wins", 0) + tp.get("losses", 0)
        if tp_total >= 10:
            tp_wr = tp.get("wins", 0) / tp_total
            if tp_wr < 0.35:
                score *= 0.7

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
        lines.append(f"  🧠 BRAIN v4 STATUS")
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
# MARKET DISCOVERY
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

    for m in all_markets:
        if brain:
            m._score = brain.score_market_for_entry(m.slug, m.spread, m.volume, m.liquidity, m.yes_price)
            if brain.is_star_market(m.slug):
                m._score *= 1.3
        else:
            # Use liquidity (order book depth from Gamma API) as primary signal
            # Volume from Gamma API includes minting/burning and may be inflated
            m._score = m.spread * math.sqrt(max(m.liquidity, 1)) * math.log10(max(m.volume, 1))

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
# ORDER MANAGER (v4: fixed capital accounting)
# ══════════════════════════════════════════════════════════════

class OrderManager:
    def __init__(self, cfg: Config, paper: bool = True, brain: Optional[Brain] = None):
        self.cfg = cfg
        self.paper = paper
        self.brain = brain
        self.orders: Dict[str, Order] = {}
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.client = None
        self._market_questions: Dict[str, str] = {}
        self._tick_sizes: Dict[str, float] = {}

        # v4: Separate realized vs committed capital
        self._starting_capital = cfg.capital
        self._realized_pnl = 0.0  # cumulative realized P&L from closed trades
        self._committed = 0.0     # capital tied up in open positions
        self._peak_equity = cfg.capital  # peak of (starting + realized + unrealized)

        # v4: Token inventory — Polymarket CLOB requires owning tokens to SELL
        # token_id → number of outcome tokens held (YES tokens for SELL orders)
        self._token_inventory: Dict[str, float] = {}

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

    # ─── v4: Tick Size ───────────────────────────────────────

    async def fetch_tick_size(self, token: str) -> float:
        if token in self._tick_sizes:
            return self._tick_sizes[token]
        if not self.client:
            return 0.01
        try:
            ts = self.client.get_tick_size(token)
            tick = float(ts)
            self._tick_sizes[token] = tick
            return tick
        except Exception:
            self._tick_sizes[token] = 0.01
            return 0.01

    def snap_to_tick(self, price: float, tick_size: float) -> float:
        if tick_size <= 0:
            return round(price, 4)
        return round(round(price / tick_size) * tick_size, 4)

    # ─── Inventory ───────────────────────────────────────────

    def get_net_inventory(self, slug: str) -> float:
        pos = self.positions.get(slug)
        if pos:
            return pos.shares
        return 0.0

    def get_total_inventory(self) -> float:
        return sum(abs(p.cost) for p in self.positions.values())

    # ─── v4: Token Inventory (Polymarket CLOB requires tokens to SELL) ──

    def get_token_balance(self, token: str) -> float:
        """Get the number of outcome tokens we hold for a given token ID."""
        return self._token_inventory.get(token, 0.0)

    def add_tokens(self, token: str, amount: float):
        """Add tokens to inventory (from BUY fill or split)."""
        self._token_inventory[token] = self._token_inventory.get(token, 0.0) + amount

    def remove_tokens(self, token: str, amount: float):
        """Remove tokens from inventory (from SELL fill)."""
        current = self._token_inventory.get(token, 0.0)
        self._token_inventory[token] = max(0.0, current - amount)

    async def ensure_sell_tokens(self, slug: str, token: str, shares_needed: float,
                                  market: Optional[Market] = None) -> bool:
        """
        Ensure we have enough outcome tokens to place a SELL order.
        On Polymarket, SELL orders require owning the tokens.
        
        Strategy:
        1. Check existing token inventory
        2. If we have a long position for this slug, we already own the tokens
        3. If not, split USDC → YES + NO tokens (creates complete sets)
        4. In paper mode, just add tokens to inventory + deduct USDC
        
        Returns True if we have enough tokens after this call.
        """
        # Check if we already have enough
        current = self.get_token_balance(token)
        if current >= shares_needed:
            return True

        # Check if we have a long position (we own those tokens)
        pos = self.positions.get(slug)
        if pos and pos.side == "LONG" and pos.shares >= shares_needed:
            # Tokens are already accounted for in the position
            self._token_inventory[token] = max(current, pos.shares)
            return True

        # Need to acquire tokens via split
        deficit = shares_needed - current
        split_cost = deficit  # Splitting $X creates X YES + X NO tokens

        if self.free_capital < split_cost:
            LOG.warning(f"⚠️ Can't split: need ${split_cost:.2f} but only ${self.free_capital:.2f} free")
            return False

        if self.paper:
            # Paper mode: just record the split
            self._committed += split_cost
            self.add_tokens(token, deficit)
            # We also get NO tokens — track them too
            no_token = ""
            if market and market.no_token:
                no_token = market.no_token
                self.add_tokens(no_token, deficit)
            LOG.info(f"✂️ PAPER SPLIT | ${split_cost:.2f} → {deficit:.0f} YES + {deficit:.0f} NO tokens | {slug[:35]}")
            return True

        # Live mode: call the CTF split function
        if not self.client:
            return False

        try:
            # py-clob-client doesn't have a direct split method
            # We need to use the CTF contract directly or a helper
            # For now, log the need and return False — user must pre-split
            LOG.warning(f"⚠️ LIVE: Need to split ${split_cost:.2f} USDC for {slug[:35]} tokens")
            LOG.warning(f"   Run: client.split_position(condition_id, amount={split_cost})")
            LOG.warning(f"   Or pre-split funds before starting the bot")
            return False
        except Exception as e:
            LOG.error(f"Split error: {e}")
            return False

    # ─── v4: Self-Impact Estimation ───────────────────────────

    def estimate_price_impact(self, order_usd: float, book_depth: float,
                               spread: float) -> float:
        """
        Estimate how much our order will move the price.
        
        Based on academic research (Kyle's λ):
        - Thin markets (low volume/depth): λ ≈ 0.05-0.20
        - Thick markets (high volume/depth): λ ≈ 0.005-0.02
        
        Returns estimated price change in dollars (e.g., 0.003 = 0.3¢).
        """
        if book_depth <= 0 or order_usd <= 0:
            return 0.0

        # Order size relative to available depth
        fill_ratio = order_usd / max(book_depth, 1)

        # Impact = fill_ratio * spread * dampening factor
        # For a $10 order against $200 depth (5% fill ratio) on a 10¢ spread:
        # impact = 0.05 * 0.10 * 0.5 = 0.0025 (0.25¢)
        impact = fill_ratio * spread * 0.5

        # Floor: at least 1 tick of impact for non-trivial orders
        if order_usd > 5:
            impact = max(impact, 0.005)

        return round(impact, 4)

    def adjust_exit_for_impact(self, entry_price: float, entry_side: str,
                                estimated_impact: float) -> float:
        """
        After a BUY fill, the price likely moved UP (our buy pushed it).
        Adjust the target SELL price to account for this.
        
        After a SELL fill (opening short), price likely moved DOWN.
        Adjust target BUY cover price accordingly.
        """
        if entry_side == "BUY":
            # Our BUY pushed price up — set SELL target higher
            return round(entry_price + estimated_impact + 0.005, 4)
        else:
            # Our SELL pushed price down — set BUY target lower
            return round(entry_price - estimated_impact - 0.005, 4)

    # ─── v4: Capital Accounting (FIXED) ──────────────────────

    @property
    def realized_pnl(self) -> float:
        """Cumulative realized P&L from closed trades."""
        return self._realized_pnl

    @property
    def committed_capital(self) -> float:
        """Capital currently tied up in open positions."""
        return self._committed

    @property
    def free_capital(self) -> float:
        """Available capital for new orders."""
        return self._starting_capital + self._realized_pnl - self._committed

    @property
    def equity(self) -> float:
        """Total equity = starting + realized P&L."""
        return self._starting_capital + self._realized_pnl

    @property
    def drawdown(self) -> float:
        """
        v4: Drawdown based on REALIZED P&L only.
        Committed capital in open positions doesn't count as loss.
        """
        equity = self.equity
        self._peak_equity = max(self._peak_equity, equity)
        if self._peak_equity <= 0:
            return 1.0
        return max(0, (self._peak_equity - equity) / self._peak_equity)

    @property
    def daily_pnl(self) -> float:
        return self._realized_pnl

    @property
    def exposed(self) -> float:
        return self._committed

    def can_enter(self, count_sell_orders: bool = False) -> Tuple[bool, str]:
        """
        v4: Check if we can enter a new position.
        count_sell_orders: if True, count SELL orders too (for both-sides mode).
        """
        if self.drawdown >= self.cfg.circuit_breaker_pct:
            return False, "CIRCUIT_BREAKER"

        # v5: Count all live orders when requested
        if count_sell_orders:
            open_orders = sum(1 for o in self.orders.values() if o.status == "live")
        else:
            open_orders = sum(1 for o in self.orders.values()
                            if o.status == "live" and o.side == "BUY")

        if len(self.positions) + open_orders >= self.cfg.max_concurrent:
            return False, "MAX_CONCURRENT"
        if self.committed >= self.cfg.max_exposure:
            return False, "MAX_EXPOSURE"
        if self.free_capital < self.cfg.per_order:
            return False, "INSUFFICIENT_CAPITAL"
        return True, "OK"

    def get_brain_adjusted_size(self, slug: str, market: Optional[Market] = None,
                                 is_market_making: bool = False) -> float:
        """
        v4/v5: Get order size, optionally halved for market making.
        """
        base_size = self.cfg.per_order

        if self.brain and self.brain.data["total_trades"] >= 10:
            kelly_size = self.brain.get_kelly_order_size(self._starting_capital, slug)
            multiplier = self.brain.get_order_size_multiplier(slug)
            base_size = kelly_size * multiplier
            base_size = max(self.cfg.per_order * 0.5, min(self.cfg.per_order * 2, base_size))
        elif self.brain:
            multiplier = self.brain.get_order_size_multiplier(slug)
            base_size = self.cfg.per_order * multiplier

        # v5: Halve size for both-sides mode (2 orders per market)
        if is_market_making:
            base_size *= 0.5

        return round(base_size, 2)

    # ─── Order Placement ─────────────────────────────────────

    async def place_limit(self, slug: str, token: str, side: str, price: float,
                          size_usd: float, market: Optional[Market] = None,
                          gtd_seconds: int = 0, post_only: bool = False) -> Optional[Order]:
        # Tick size snap
        tick_size = 0.01
        if market:
            tick_size = market.tick_size
        price = self.snap_to_tick(price, tick_size)

        shares = round(size_usd / price, 2) if price > 0 else 0

        # GTD — Polymarket has a 60s security threshold on GTD expiration
        # To set an effective lifetime of N seconds, use now + 60 + N
        # Paper mode uses clean timing; live mode adds the +60 buffer
        order_type = "GTC"
        expires_at = 0.0
        if gtd_seconds > 0:
            order_type = "GTD"
            if self.paper:
                expires_at = time.time() + gtd_seconds  # paper: clean timing
            else:
                expires_at = time.time() + 60 + gtd_seconds  # live: +60 security threshold

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

        stop_loss = round(price - 0.02, 4) if side == "BUY" else 0.0

        order = Order(
            id=f"{slug}_{side}_{int(time.time()*1000)}",
            slug=slug, token=token, side=side,
            price=price, size=size_usd, shares=shares,
            post_only=post_only,
            brain_score=brain_score,
            entry_spread=entry_spread, entry_volume=entry_volume,
            entry_liquidity=entry_liquidity, entry_price_range=entry_price_range,
            order_type=order_type, expires_at=expires_at,
        )

        if self.paper:
            order.status = "live"
            order.exchange_id = f"paper_{order.id}"
            gtd_tag = f" [GTD {gtd_seconds}s]" if gtd_seconds > 0 else ""
            po_tag = " [POST-ONLY]" if post_only else ""
            LOG.info(f"📝 PAPER | {side} {shares:.0f} @ ${price:.4f}{gtd_tag}{po_tag} on {slug[:35]}")
            self.orders[order.id] = order
            return order

        if not self.client:
            return None

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType as OT
            from py_clob_client.order_builder.constants import BUY, SELL

            side_const = BUY if side == "BUY" else SELL

            order_options = {}
            if market and market.neg_risk:
                order_options["negRisk"] = True

            order_args = OrderArgs(
                token_id=token, price=price, size=shares, side=side_const,
            )

            signed = self.client.create_order(order_args, **order_options)

            # v4: Post-only orders
            if post_only:
                resp = self.client.post_order(signed, OT.GTC, post_only=True)
            elif order_type == "GTD" and expires_at > 0:
                resp = self.client.post_order(signed, OT.GTD, expires_at=int(expires_at))
            else:
                resp = self.client.post_order(signed, OT.GTC)

            if resp:
                order.exchange_id = resp.get("orderID", resp.get("order_id", ""))
                order.status = "live"
                po_tag = " [POST-ONLY]" if post_only else ""
                LOG.info(f"✅ LIVE | {side} {shares:.0f} @ ${price:.4f} [{order_type}]{po_tag} on {slug[:35]}")
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
                await self.om_cancel_order(order)
        LOG.info("All orders canceled")

    async def om_cancel_order(self, order: Order):
        """Internal cancel wrapper."""
        await self.cancel_order(order)

    def fill_order(self, order: Order, fill_price: float):
        """
        v4: Fixed fill handling.
        - BUY: capital goes to committed (not lost)
        - SELL (closing long): realized PnL = (exit - entry) * shares
        - SELL (opening short): track committed, no capital gain yet
        """
        order.status = "filled"
        order.fill_price = fill_price
        order.filled = time.time()
        cost = order.shares * fill_price

        if order.side == "BUY":
            # BUY fill: money leaves free pool, enters committed
            # Also: we now own YES tokens that can be sold later
            self._committed += cost
            self.add_tokens(order.token, order.shares)

            if order.slug in self.positions:
                # Adding to existing position
                pos = self.positions[order.slug]
                total_cost = pos.cost + cost
                total_shares = pos.shares + order.shares
                pos.entry_price = total_cost / total_shares
                pos.shares = total_shares
                pos.cost = total_cost
                pos.committed_capital = total_cost
                LOG.info(f"🟢 ADDED BUY | {order.shares:.0f} @ ${fill_price:.4f} | "
                        f"Total: {total_shares:.0f} @ ${pos.entry_price:.4f} | {order.slug[:35]}")
            else:
                self.positions[order.slug] = Position(
                    slug=order.slug, token=order.token, side="LONG",
                    entry_price=fill_price, shares=order.shares, cost=cost,
                    entry_spread=order.entry_spread, entry_volume=order.entry_volume,
                    entry_liquidity=order.entry_liquidity, entry_price_range=order.entry_price_range,
                    stop_loss_price=round(fill_price - 0.02, 4),
                    committed_capital=cost,
                )
                LOG.info(f"🟢 FILLED BUY | {order.shares:.0f} @ ${fill_price:.4f} | {order.slug[:35]}")

        else:
            # SELL fill — we're giving up YES tokens
            self.remove_tokens(order.token, order.shares)

            pos = self.positions.get(order.slug)
            if pos and pos.side == "LONG":
                # Closing a long: realized PnL = (exit - entry) * shares_sold
                pnl = (fill_price - pos.entry_price) * order.shares
                self._realized_pnl += pnl
                self._committed -= (pos.entry_price * order.shares)

                # Remove position
                remaining = pos.shares - order.shares
                if remaining > 0.01:
                    pos.shares = remaining
                    pos.cost = pos.entry_price * remaining
                    pos.committed_capital = pos.cost
                else:
                    self.positions.pop(order.slug, None)

                self._peak_equity = max(self._peak_equity, self.equity)
                hold = time.time() - pos.opened

                trade = Trade(
                    slug=order.slug, question=self._market_questions.get(order.slug, ""),
                    entry_price=pos.entry_price, exit_price=fill_price,
                    shares=order.shares, pnl=pnl, hold_sec=hold, reason="filled",
                    entry_spread=pos.entry_spread, entry_volume=pos.entry_volume,
                    entry_liquidity=pos.entry_liquidity, entry_price_range=pos.entry_price_range,
                    exit_type="profit" if pnl > 0 else "loss",
                )
                self.trades.append(trade)
                if self.brain:
                    self.brain.learn_from_trade(trade)
                emoji = "🟢" if pnl > 0 else "🔴"
                LOG.info(f"{emoji} FILLED SELL | {order.shares:.0f} @ ${fill_price:.4f} | "
                        f"PnL=${pnl:+.3f} | {order.slug[:35]}")
            else:
                # SELL without existing long = opening a SHORT
                # v4: Don't add to capital — track as committed
                self._committed += cost
                self.positions[order.slug] = Position(
                    slug=order.slug, token=order.token, side="SHORT",
                    entry_price=fill_price, shares=-order.shares, cost=cost,
                    entry_spread=order.entry_spread, entry_volume=order.entry_volume,
                    entry_liquidity=order.entry_liquidity, entry_price_range=order.entry_price_range,
                    stop_loss_price=round(fill_price + 0.02, 4),
                    committed_capital=cost,
                )
                LOG.info(f"🔴 SHORT SELL | {order.shares:.0f} @ ${fill_price:.4f} | "
                        f"Will cover with BUY | {order.slug[:35]}")

    def force_exit_position(self, slug: str, exit_price: float, reason: str):
        """
        v4: Fixed exit handling. Only credits realized PnL.
        """
        pos = self.positions.pop(slug, None)
        if not pos:
            return

        if pos.side == "LONG":
            pnl = (exit_price - pos.entry_price) * pos.shares
            self._realized_pnl += pnl
            self._committed -= pos.cost
        else:  # SHORT
            pnl = (pos.entry_price - exit_price) * abs(pos.shares)
            self._realized_pnl += pnl
            self._committed -= pos.cost

        self._peak_equity = max(self._peak_equity, self.equity)
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
        for slug, pos in list(self.positions.items()):
            if pos.stop_loss_price <= 0:
                continue
            current_price = market_prices.get(slug, pos.entry_price)

            if pos.side == "LONG":
                if current_price <= pos.stop_loss_price:
                    LOG.warning(f"🛑 STOP LOSS (LONG) | {slug[:35]} | "
                               f"${pos.entry_price:.4f} → ${current_price:.4f} "
                               f"(stop=${pos.stop_loss_price:.4f})")
                    self.force_exit_position(slug, current_price, "stop_loss")
            elif pos.side == "SHORT":
                if current_price >= pos.stop_loss_price:
                    LOG.warning(f"🛑 STOP LOSS (SHORT) | {slug[:35]} | "
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

    def clean_expired_orders(self):
        now = time.time()
        for oid, order in list(self.orders.items()):
            if order.status == "live" and order.order_type == "GTD" and order.expires_at > 0:
                if now > order.expires_at:
                    order.status = "canceled"
                    LOG.info(f"⏰ GTD EXPIRED | {order.side} @ ${order.price:.4f} on {order.slug[:35]}")

    def summary(self) -> str:
        wins = [t for t in self.trades if t.pnl > 0]
        wr = len(wins) / max(1, len(self.trades))
        return (f"Equity=${self.equity:.2f} (realized ${self._realized_pnl:+.2f}) | "
                f"Committed=${self._committed:.2f} | Free=${self.free_capital:.2f} | "
                f"Pos={len(self.positions)} | Trades={len(self.trades)} ({wr:.0%} WR) | "
                f"DD={self.drawdown:.1%}")


# ══════════════════════════════════════════════════════════════
# SCALPING ENGINE (v4: all upgrades integrated)
# ══════════════════════════════════════════════════════════════

class Scalper:
    def __init__(self, cfg: Config, paper: bool = True):
        self.cfg = cfg
        self.paper = paper
        self.brain = Brain()
        self.om = OrderManager(cfg, paper, brain=self.brain)
        self.feed = Feed(cfg)
        self.news = NewsMonitor()
        self.flow = FlowAnalyzer()
        self.correlations = CorrelationEngine()
        self.markets: Dict[str, Market] = {}
        self.token_to_slug: Dict[str, str] = {}
        self.running = False
        self.tick = 0
        self._last_reprice = 0
        self._last_news_check = 0
        self._last_reconcile = 0
        # v4: AI Supervisor (rule-based, not per-order)
        self._supervised = cfg.supervised
        self._supervisor_rules: dict = {}  # loaded from rules.jsonl
        self._last_rules_load = 0

    async def start(self, mode: str = "paper"):
        self.brain.start_session()

        if self.brain.data["total_trades"] > 0:
            LOG.info(f"🧠 Brain: {self.brain.data['total_trades']} trades | "
                    f"Avoid: {len(self.brain.data['avoid_list'])} | Stars: {len(self.brain.data['star_list'])}")

        tod_mult, tod_cat = self.correlations.get_time_of_day_multiplier()
        if tod_cat == "quiet":
            LOG.warning(f"⏰ QUIET HOURS — fill rates will be lower (multiplier: {tod_mult})")
        elif tod_cat == "peak":
            LOG.info(f"⏰ PEAK HOURS — optimal trading window (multiplier: {tod_mult})")

        LOG.info("=" * 60)
        LOG.info(f"  SCALPER v4 | ${self.cfg.capital:.0f} | {mode.upper()} | Strategy: {self.cfg.strategy}")
        LOG.info(f"  Exposure: {self.cfg.max_exposure_pct:.0%} | Kelly: {self.brain.kelly_fraction():.1%}")
        LOG.info(f"  Max concurrent: {self.cfg.max_concurrent} | Reprice: {self.cfg.reprice_sec}s")
        LOG.info(f"  Max hold: {self.cfg.max_hold_sec}s | Circuit breaker: {self.cfg.circuit_breaker_pct:.0%}")
        LOG.info(f"  Post-only: {self.cfg.post_only} | Supervised: {self.cfg.supervised}")
        LOG.info(f"  News decay: {self.news.ALERT_HALF_LIFE}s half-life | News auto-unskip: {self.news.UNSKIP_AFTER_SEC}s")
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

        if not self.paper:
            for m in market_list:
                ts = await self.om.fetch_tick_size(m.yes_token)
                m.tick_size = ts
                LOG.debug(f"Tick size for {m.slug[:30]}: {ts}")

        tokens = [m.yes_token for m in market_list if m.yes_token]
        ws_task = asyncio.create_task(self.feed.start(tokens))
        self.feed.on_update(self._on_book_update)

        self.running = True
        LOG.info("🚀 Scalper v4 running — Ctrl+C to stop\n")

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

        # v4: Load supervisor rules every 60s (fast file read, no network)
        if self._supervised and now - self._last_rules_load > 60:
            self._load_supervisor_rules()

        # v4: Check for supervisor emergency exits (instant)
        if self._supervised:
            self._check_emergency_exits()

        # News check every 15s
        if now - self._last_news_check > 15:
            async with aiohttp.ClientSession() as s:
                alerts = await self.news.check_feeds(s)
            if alerts:
                for slug, market in self.markets.items():
                    affected, reason = self.news.is_market_affected(market.question)
                    if affected:
                        LOG.warning(f"🚨 ADVERSE SELECTION ALERT | {slug[:35]} | {reason}")
                        self.news.mark_market_skipped(slug)

                        # Pull all resting orders
                        pulled = 0
                        for oid, order in list(self.om.orders.items()):
                            if order.status == "live" and order.slug == slug:
                                await self.om.cancel_order(order)
                                pulled += 1
                        if pulled:
                            LOG.warning(f"🚨 PULLED {pulled} orders on {slug[:35]} due to news")

                        # v4: Adverse selection exit — if we hold a position, dump immediately
                        # at best bid (market order equivalent) before informed traders move price
                        if slug in self.om.positions:
                            pos = self.om.positions[slug]
                            dump_price = market.best_bid if market.best_bid > 0 else pos.entry_price
                            LOG.warning(f"🚨 ADVERSE EXIT | {slug[:35]} | "
                                       f"Dumping {pos.shares:.0f} @ ${dump_price:.4f}")
                            self.om.force_exit_position(slug, dump_price, "adverse_selection")

            # v4: Check if any skipped markets should be un-skipped
            for slug, market in self.markets.items():
                if self.news.should_unskip_market(slug):
                    affected, _ = self.news.is_market_affected(market.question)
                    if not affected:
                        self.news.clear_market_skip(slug)
                        LOG.info(f"📰 UNSKIP | {slug[:35]} — news decayed, resuming trading")

            self._last_news_check = now

        # Clean expired GTD orders
        self.om.clean_expired_orders()

        # Stale order cleanup
        for oid, order in list(self.om.orders.items()):
            if order.status == "live" and order.order_type == "GTC" and (now - order.created) > 60:
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
        else:
            # v4: Live mode — reconcile fills from exchange every 30s
            if now - self._last_reconcile > 30:
                await self._reconcile_fills()
                self._last_reconcile = now

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

        bb_price, bb_size = bids[0][0], bids[0][1]
        ba_price, ba_size = asks[0][0], asks[0][1]
        mid = (bb_price + ba_price) / 2
        spread = ba_price - bb_price
        old_mid = market.yes_price

        market.best_bid = bb_price
        market.best_ask = ba_price
        market.best_bid_size = bb_size  # v4: track depth
        market.best_ask_size = ba_size  # v4: track depth
        market.yes_price = mid
        market.spread = spread
        market.last_ws_update = time.time()

        # Flow recording
        last_trade = book.get("last_trade")
        if last_trade:
            side = "BUY" if last_trade > old_mid else "SELL"
            self.flow.record_trade(token, last_trade, 0, side)

        # Correlation
        self.correlations.record_price(slug, mid, market.question)

        # Flow pull check
        should_pull, pull_reason = self.flow.should_pull_orders(token)
        if should_pull:
            for oid, order in list(self.om.orders.items()):
                if order.status == "live" and order.slug == slug:
                    await self.om.cancel_order(order)
            LOG.warning(f"🌊 FLOW PULL | {slug[:35]} | {pull_reason}")
            return

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

                correlated = self.correlations.detect_correlated_move(slug, mid, old_mid)
                for corr_slug, direction in correlated:
                    LOG.info(f"🔗 CORRELATION | {slug[:25]} moved → {corr_slug[:25]} may follow")

                if slug not in self.om.positions:
                    ok, _ = self.om.can_enter()
                    if ok:
                        offset = max(0.005, spread/2 - self.cfg.spread_target)
                        buy_price = round(mid - offset, 4)
                        buy_price = max(buy_price, round(bb_price + 0.001, 4))
                        if 0 < buy_price < 1:
                            size = self.om.get_brain_adjusted_size(slug, market)
                            if self._supervised:
                                blocked, reason = self._is_market_blocked(slug)
                                if blocked:
                                    return
                                size = self._apply_supervisor_limits(slug, size)
                            gtd = self._get_gtd_seconds(slug, spread)
                            await self.om.place_limit(slug, market.yes_token, "BUY",
                                buy_price, size, market, gtd, post_only=self.cfg.post_only)
                else:
                    pos = self.om.positions[slug]
                    exit_price = round(mid + 0.005, 4)
                    if mid < pos.entry_price - 0.01:
                        exit_price = round(bb_price, 4)
                    gtd = self._get_gtd_seconds(slug, spread)
                    # Exits are always fast (unsupervised) — don't delay risk management
                    await self.om.place_limit(slug, market.yes_token, "SELL",
                        exit_price, pos.cost, market, gtd, post_only=self.cfg.post_only)

        # Paper fills (v4: book-depth-aware)
        if self.paper:
            for oid, order in list(self.om.orders.items()):
                if order.status != "live" or order.slug != slug:
                    continue
                # v4: post-only orders can't cross the spread
                if order.post_only:
                    if order.side == "BUY" and order.price >= ba_price:
                        continue  # would cross — reject
                    if order.side == "SELL" and order.price <= bb_price:
                        continue  # would cross — reject

                if order.side == "BUY" and bb_price <= order.price:
                    self._attempt_paper_fill(order, market, "bid")
                elif order.side == "SELL" and ba_price >= order.price:
                    self._attempt_paper_fill(order, market, "ask")

    # ─── v4: Book-Depth-Aware Paper Fill ─────────────────────

    def _attempt_paper_fill(self, order: Order, market: Market, book_side: str):
        """
        v4: Realistic paper fill simulation using book depth.
        
        Instead of random probability, models:
        1. Queue position — are we at/inside best bid/ask?
        2. Book depth — how much size is ahead of us?
        3. Flow — is there active trading?
        4. Age — older orders have slightly better fill chance (queue priority)
        5. Spread width — wider spreads = lower fill probability
        """
        now = time.time()
        age = now - order.created

        # Must be resting for at least 10 seconds
        if age < 10:
            return

        # v4: Determine queue position
        if book_side == "bid":
            book_depth = market.best_bid_size
            our_at_best = abs(order.price - market.best_bid) < market.tick_size
        else:
            book_depth = market.best_ask_size
            our_at_best = abs(order.price - market.best_ask) < market.tick_size

        # Base fill rate: only orders at or better than best get fills
        if our_at_best:
            # At the best level — moderate fill rate
            base_rate = 0.008  # 0.8% per tick at best
        elif (book_side == "bid" and order.price >= market.best_bid) or \
             (book_side == "ask" and order.price <= market.best_ask):
            # Inside the best — good fill rate (someone would hit us first)
            base_rate = 0.015  # 1.5% per tick
        else:
            # Behind the best — very unlikely to fill
            base_rate = 0.001  # 0.1% per tick

        # v4: Depth adjustment — smaller depth = easier to get filled
        if book_depth > 0:
            # If book depth is thin (< $500), fills are faster
            depth_factor = min(2.0, 500 / max(book_depth, 10))
        else:
            depth_factor = 1.5  # no depth info = assume thin
        base_rate *= depth_factor

        # v4: Spread adjustment — wider spread = lower fill rate
        # (fewer takers willing to cross a wide spread)
        if market.spread > 0.10:
            base_rate *= 0.3  # 20¢ spread → very few takers
        elif market.spread > 0.05:
            base_rate *= 0.6  # 10¢ spread → some takers
        elif market.spread < 0.04:
            base_rate *= 1.5  # tight spread → many takers

        # Age ramp (gentler than v3)
        age_factor = min(2.0, 1.0 + (age - 10) / 120)  # ramps over 2 minutes
        base_rate *= age_factor

        # Volume factor (logarithmic, capped)
        vol_factor = min(1.5, math.log10(max(market.volume, 1)) / 5)
        base_rate *= vol_factor

        # Flow factor
        flow_hint = self.flow.get_fill_probability_hint(market.yes_token)
        base_rate *= (0.7 + flow_hint * 0.3)

        # Cap per-tick probability
        base_rate = min(base_rate, 0.08)  # max 8% per tick (was 15% in v3)

        # v4: Partial fills — 30% chance of partial fill (only fill 40-80% of order)
        if base_rate > 0 and random.random() < base_rate:
            if random.random() < 0.3:
                # Partial fill
                fill_pct = random.uniform(0.4, 0.8)
                partial_shares = round(order.shares * fill_pct, 2)
                partial_cost = partial_shares * order.fill_price if order.fill_price else partial_shares * order.price
                LOG.info(f"📝 PARTIAL FILL | {fill_pct:.0%} of {order.side} {order.shares:.0f} "
                        f"@ ${order.price:.4f} on {order.slug[:35]}")
                # For simplicity, treat partial as full fill with adjusted size
                order.shares = partial_shares
                order.size = partial_cost

            self.om.fill_order(order, order.price)

    # ─── v4: Spread-Proportional GTD ─────────────────────────

    def _get_gtd_seconds(self, slug: str, spread: float = 0.0) -> int:
        """
        v4: GTD scales with spread width.
        Wide-spread markets need longer GTD because price discovery is slower.
        Formula: gtd = clamp(60, spread * 1500, 300)
        
        Examples:
          3¢ spread → 45s (tight, fast fills expected)
          5¢ spread → 75s
          10¢ spread → 150s
          20¢ spread → 300s (wide, needs patience)
        """
        if spread <= 0:
            # Fallback: try to get spread from market
            market = self.markets.get(slug)
            if market:
                spread = market.spread
            else:
                spread = 0.05  # default

        # Base: spread-proportional
        gtd = int(spread * 1500)
        gtd = max(60, min(300, gtd))

        # Brain override: if we have enough data, adjust
        if self.brain:
            rep = self.brain.get_market_rep(slug)
            if rep["trades"] >= 5:
                fill_rate = rep.get("fills", 0) / rep["trades"]
                timeout_rate = rep.get("timeouts", 0) / rep["trades"]

                # If mostly timeouts, increase GTD
                if timeout_rate > 0.6:
                    gtd = min(300, int(gtd * 1.5))
                # If good fill rate, can shorten slightly
                elif fill_rate > 0.6:
                    gtd = max(60, int(gtd * 0.8))

        return gtd

    async def _paper_fill_check(self):
        """
        v4: Delegates to _attempt_paper_fill which uses book depth.
        Also handles GTD expiry.
        """
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

            # Use the depth-aware fill check
            book_side = "bid" if order.side == "BUY" else "ask"
            self._attempt_paper_fill(order, market, book_side)

    async def _reconcile_fills(self):
        """
        v4: Live mode fill reconciliation.
        Polls the exchange for filled orders and updates local state.
        Catches fills that the WebSocket might have missed.
        """
        if not self.om.client:
            return

        try:
            from py_clob_client.clob_types import OpenOrderParams
            open_orders = self.om.client.get_orders(OpenOrderParams())

            # Build set of exchange order IDs that are still live
            live_exchange_ids = set()
            for o in open_orders:
                oid = o.get("id", "")
                if oid:
                    live_exchange_ids.add(oid)

            # Check our local orders — if exchange doesn't know about them, they filled
            for local_id, order in list(self.om.orders.items()):
                if order.status != "live":
                    continue
                if order.exchange_id and order.exchange_id not in live_exchange_ids:
                    # Order no longer on exchange — it filled (or was cancelled externally)
                    # Assume filled at our price for now (could query trade history for exact price)
                    LOG.info(f"🔄 RECONCILED FILL | {order.side} {order.shares:.0f} "
                            f"@ ${order.price:.4f} on {order.slug[:35]}")
                    self.om.fill_order(order, order.price)

            LOG.debug(f"Reconciled: {len(self.om.orders)} local, {len(open_orders)} on exchange")
        except Exception as e:
            LOG.debug(f"Reconcile error (non-critical): {e}")

    async def _reprice(self):
        # v5: In both-sides mode, count all orders
        if self.cfg.strategy == "both_sides":
            markets_with_pairs = defaultdict(list)
            for oid, order in self.om.orders.items():
                if order.status == "live":
                    markets_with_pairs[order.slug].append(order)

            for slug, orders in markets_with_pairs.items():
                if len(orders) >= 2:
                    oldest = min(o.created for o in orders)
                    spread = self.markets.get(slug, Market("", "", 0, 0, 0, 0, 0, "", "")).spread
                    if time.time() - oldest > self._get_gtd_seconds(slug, spread):
                        for o in orders:
                            await self.om.cancel_order(o)
                    continue
                for o in orders:
                    await self.om.cancel_order(o)
        else:
            for oid, order in list(self.om.orders.items()):
                if order.status == "live":
                    await self.om.cancel_order(order)

        for slug, market in self.markets.items():
            if market.best_bid <= 0 or market.best_ask >= 1:
                continue
            if market.spread < self.cfg.min_spread:
                continue

            live_on_market = [o for o in self.om.orders.values()
                            if o.status == "live" and o.slug == slug]
            if self.cfg.strategy == "both_sides" and len(live_on_market) >= 2:
                continue

            # v4: News check with decay — skip only if alert weight is significant
            affected, reason = self.news.is_market_affected(market.question)
            if affected:
                LOG.warning(f"🚨 SKIP REPRICE | {slug[:35]} | {reason}")
                self.news.mark_market_skipped(slug)
                continue

            if self.brain:
                should_trade, _ = self.brain.should_trade_market(slug)
                if not should_trade:
                    continue

            # v4: Supervisor block check (instant, file-based)
            if self._supervised:
                blocked, reason = self._is_market_blocked(slug)
                if blocked:
                    LOG.debug(f"👁 SKIP | {slug[:35]} | {reason}")
                    continue

            mid = (market.best_bid + market.best_ask) / 2

            if slug in self.om.positions:
                pos = self.om.positions[slug]
                hold_sec = time.time() - pos.opened

                aggression = 0.5
                if self.brain:
                    aggression = self.brain.get_exit_aggressiveness(slug)

                if hold_sec > self.cfg.max_hold_sec * (0.85 - aggression * 0.3):
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

                # v4: Impact-aware exit floor
                # After our BUY filled, price likely moved up.
                # Ensure exit covers entry + impact + minimum profit
                impact = self.om.estimate_price_impact(
                    pos.cost, market.best_bid_size, market.spread)
                min_exit = round(pos.entry_price + impact + 0.003, 4)
                exit_price = max(exit_price, min_exit)
                exit_price = min(exit_price, 0.99)  # cap

                gtd = self._get_gtd_seconds(slug, market.spread)
                await self.om.place_limit(slug, market.yes_token, "SELL",
                    exit_price, pos.cost, market, gtd, post_only=self.cfg.post_only)
                continue

            # v5: Use count_sell_orders=True for both-sides mode
            count_sells = self.cfg.strategy == "both_sides"
            ok, _ = self.om.can_enter(count_sell_orders=count_sells)
            if not ok:
                continue

            if self.cfg.strategy == "both_sides":
                await self._place_both_sides(slug, market, mid)
            else:
                await self._place_one_side(slug, market, mid)

    async def _place_one_side(self, slug: str, market: Market, mid: float):
        # v4: Check supervisor rules (instant, file-based)
        if self._supervised:
            blocked, reason = self._is_market_blocked(slug)
            if blocked:
                LOG.debug(f"👁 SKIP | {slug[:35]} | {reason}")
                return

        half_spread = market.spread / 2
        buy_price = round(mid - max(0.005, half_spread - self.cfg.spread_target), 4)
        buy_price = max(buy_price, round(market.best_bid + 0.001, 4))
        if buy_price <= 0 or buy_price >= 1:
            return
        size = self.om.get_brain_adjusted_size(slug, market)

        # v4: Apply supervisor size limits
        if self._supervised:
            size = self._apply_supervisor_limits(slug, size)

        gtd = self._get_gtd_seconds(slug, market.spread)
        await self.om.place_limit(slug, market.yes_token, "BUY", buy_price, size,
            market, gtd, post_only=self.cfg.post_only)

    async def _place_both_sides(self, slug: str, market: Market, mid: float):
        """
        v4/v5: Market making with capital guard and neg-risk quoting.
        """
        spread = market.spread
        if spread < self.cfg.min_spread:
            return

        inventory = self.om.get_net_inventory(slug)

        # If we have a position, manage exit
        if slug in self.om.positions:
            pos = self.om.positions[slug]
            hold_sec = time.time() - pos.opened

            aggression = 0.5
            if self.brain:
                aggression = self.brain.get_exit_aggressiveness(slug)

            if hold_sec > self.cfg.max_hold_sec * (0.85 - aggression * 0.3):
                exit_price = round(market.best_bid, 4)
            elif mid > pos.entry_price + 0.005:
                exit_price = round(mid + 0.003, 4)
            elif mid < pos.entry_price - 0.01:
                exit_price = round(market.best_bid, 4)
            else:
                exit_price = round(mid, 4)

            gtd = self._get_gtd_seconds(slug, market.spread)
            # v5: Halved size for market making
            size = self.om.get_brain_adjusted_size(slug, market, is_market_making=True)

            flow_stats = self.flow.get_stats(market.yes_token)
            if flow_stats.get("buy_pressure", 0) > 0.5:
                exit_price = round(min(exit_price + 0.003, market.best_ask - 0.001), 4)

            # v4: Impact-aware exit floor
            impact = self.om.estimate_price_impact(
                pos.cost, market.best_bid_size, market.spread)
            min_exit = round(pos.entry_price + impact + 0.003, 4)
            exit_price = max(exit_price, min_exit)
            exit_price = min(exit_price, 0.99)

            await self.om.place_limit(slug, market.yes_token, "SELL",
                exit_price, pos.cost, market, gtd, post_only=self.cfg.post_only)
            return

        # No position: place simultaneous BID + ASK
        # v5: Re-check can_enter with sell orders counted
        ok, _ = self.om.can_enter(count_sell_orders=True)
        if not ok:
            return

        # v5: Halved size for market making
        size = self.om.get_brain_adjusted_size(slug, market, is_market_making=True)
        gtd = self._get_gtd_seconds(slug, market.spread)
        tick = market.tick_size

        half_capture = max(0.005, spread * 0.3)
        bid_price = round(mid - half_capture, 4)
        ask_price = round(mid + half_capture, 4)

        bid_price = max(bid_price, round(market.best_bid + tick, 4))
        ask_price = min(ask_price, round(market.best_ask - tick, 4))

        # Inventory skew
        skew = inventory * 0.005
        bid_price = round(bid_price - skew, 4)
        ask_price = round(ask_price - skew, 4)

        # Flow adjustment
        flow_stats = self.flow.get_stats(market.yes_token)
        buy_pressure = flow_stats.get("buy_pressure", 0)
        if buy_pressure > 0.3:
            ask_price = round(ask_price - 0.002, 4)
        elif buy_pressure < -0.3:
            bid_price = round(bid_price + 0.002, 4)

        # Snap to ticks
        bid_price = self.om.snap_to_tick(bid_price, tick)
        ask_price = self.om.snap_to_tick(ask_price, tick)

        # Validate
        if bid_price <= 0 or bid_price >= 1 or ask_price <= 0 or ask_price >= 1:
            return
        if bid_price >= ask_price:
            return
        if (ask_price - bid_price) < tick * 2:
            return

        # Place both orders
        # v4: Ensure we own YES tokens before placing SELL
        # (Polymarket CLOB requires token ownership for SELL orders)
        shares_needed = round(size / ask_price, 2) if ask_price > 0 else 0
        has_tokens = await self.om.ensure_sell_tokens(
            slug, market.yes_token, shares_needed, market)
        if not has_tokens:
            LOG.warning(f"⚠️ No tokens for SELL on {slug[:35]}, placing BID only")
            await self.om.place_limit(slug, market.yes_token, "BUY",
                bid_price, size, market, gtd, post_only=self.cfg.post_only)
            return

        # v4: Apply supervisor size limits
        if self._supervised:
            size = self._apply_supervisor_limits(slug, size)

        await self.om.place_limit(slug, market.yes_token, "BUY",
            bid_price, size, market, gtd, post_only=self.cfg.post_only)
        await self.om.place_limit(slug, market.yes_token, "SELL",
            ask_price, size, market, gtd, post_only=self.cfg.post_only)

        LOG.info(f"🔄 MARKET MAKE | {slug[:35]} | "
                f"BID ${bid_price:.4f} × ${size:.0f} | "
                f"ASK ${ask_price:.4f} × ${size:.0f} | "
                f"Spread captured: ${(ask_price-bid_price):.4f}")

        # v6: Aggressive neg-risk NO-side quoting
        if market.neg_risk and market.no_token:
            no_mid = round(1 - mid, 4)
            # v6: Wider conditions — use spread-based capture instead of fixed
            no_half_capture = max(0.005, spread * 0.25)
            no_bid = round(no_mid - no_half_capture, 4)
            no_ask = round(no_mid + no_half_capture, 4)
            no_bid = self.om.snap_to_tick(max(no_bid, tick), tick)
            no_ask = self.om.snap_to_tick(min(no_ask, round(1 - tick, 4)), tick)

            # v6: Relaxed validation — just need bid < ask, not strict bounds
            if 0.01 < no_bid < no_ask < 0.99:
                # v6: NO-side doesn't consume can_enter (it's a hedge)
                no_size = round(size * 0.5, 2)
                await self.om.place_limit(slug + "_NO", market.no_token, "BUY",
                    no_bid, no_size, market, gtd, post_only=self.cfg.post_only)
                LOG.debug(f"🔄 NEG RISK NO | {slug[:30]} | "
                         f"BID ${no_bid:.3f} | ASK ${no_ask:.3f} | size=${no_size:.0f}")

    # ══════════════════════════════════════════════════════════
    # v4: AI SUPERVISOR (rule-based, zero-latency)
    # ══════════════════════════════════════════════════════════

    def _load_supervisor_rules(self):
        """Load rules from rules.jsonl (written by supervisor.py)."""
        if not os.path.exists(RULES_FILE):
            return
        try:
            self._supervisor_rules = json.loads(open(RULES_FILE).read())
            self._last_rules_load = time.time()
            blocked = len(self._supervisor_rules.get("blocked_markets", []))
            approved = len(self._supervisor_rules.get("approved_markets", []))
            emergency = len(self._supervisor_rules.get("emergency_exits", []))
            if emergency > 0:
                LOG.warning(f"🚨 SUPERVISOR: {emergency} emergency exits queued!")
            LOG.debug(f"👁 Rules loaded: {approved} approved, {blocked} blocked")
        except Exception as e:
            LOG.debug(f"Rules load error: {e}")

    def _is_market_blocked(self, slug: str) -> Tuple[bool, str]:
        """Check if supervisor has blocked this market. O(1) lookup."""
        rules = self._supervisor_rules
        if rules.get("global_paused"):
            return True, "GLOBAL PAUSE"
        if slug in rules.get("blocked_markets", []):
            return True, "Blocked by supervisor"
        return False, ""

    def _get_market_size_multiplier(self, slug: str) -> float:
        """Get supervisor-imposed size multiplier. 1.0 = no change."""
        limits = self._supervisor_rules.get("market_limits", {})
        market_limit = limits.get(slug, {})
        return market_limit.get("max_order_size_multiplier", 1.0)

    def _get_market_exit_bounds(self, slug: str) -> Tuple[float, float]:
        """Get (floor, cap) for exit prices from supervisor."""
        limits = self._supervisor_rules.get("market_limits", {})
        market_limit = limits.get(slug, {})
        floor = market_limit.get("exit_price_floor", 0.0)
        cap = market_limit.get("exit_price_cap", 1.0)
        return floor, cap

    def _check_emergency_exits(self):
        """Check if supervisor has queued any emergency exits."""
        emergencies = self._supervisor_rules.get("emergency_exits", [])
        if not emergencies:
            return
        for slug in emergencies:
            if slug in self.om.positions:
                market = self.markets.get(slug)
                exit_price = market.yes_price if market else self.om.positions[slug].entry_price
                LOG.warning(f"🚨 SUPERVISOR EMERGENCY EXIT | {slug[:35]} | "
                           f"exit @ ${exit_price:.4f}")
                self.om.force_exit_position(slug, exit_price, "supervisor_emergency")
            # Cancel any resting orders
            for oid, order in list(self.om.orders.items()):
                if order.status == "live" and order.slug == slug:
                    asyncio.create_task(self.om.cancel_order(order))

    def _apply_supervisor_limits(self, slug: str, base_size: float) -> float:
        """Apply supervisor size limits to an order size."""
        multi = self._get_market_size_multiplier(slug)
        return round(base_size * multi, 2)

    def _final_report(self):
        # Close any remaining positions at current prices
        for slug, pos in list(self.om.positions.items()):
            m = self.markets.get(slug)
            price = m.yes_price if m else pos.entry_price
            self.om.force_exit_position(slug, price, "shutdown")

        wins = [t for t in self.om.trades if t.pnl > 0]
        losses = [t for t in self.om.trades if t.pnl <= 0]
        total_pnl = self.om._realized_pnl
        wr = len(wins) / max(1, len(self.om.trades))
        avg_hold = sum(t.hold_sec for t in self.om.trades) / max(1, len(self.om.trades))

        LOG.info(f"""
{'='*60}
  FINAL REPORT v4
{'='*60}
  Starting Capital: ${self.cfg.capital:.2f}
  Final Equity:     ${self.om.equity:.2f}
  Realized P&L:     ${self.om._realized_pnl:+.2f} ({self.om._realized_pnl/self.cfg.capital*100:+.1f}%)
  Peak Equity:      ${self.om._peak_equity:.2f}
  Max Drawdown:     {self.om.drawdown:.1%}
  
  Total Trades:     {len(self.om.trades)}
  Wins:             {len(wins)} | Losses: {len(losses)}
  Win Rate:         {wr:.0%}
  Avg Hold:         {avg_hold:.0f}s
  
  Avg Win:          ${sum(t.pnl for t in wins)/max(1,len(wins)):+.3f}
  Avg Loss:         ${sum(t.pnl for t in losses)/max(1,len(losses)):-.3f}
  Profit Factor:    {sum(t.pnl for t in wins)/max(0.001, abs(sum(t.pnl for t in losses))):.2f}
  
  Strategy:         {self.cfg.strategy}
  Post-Only:        {self.cfg.post_only}
  Kelly Fraction:   {self.brain.kelly_fraction():.1%}
  News Alerts:      {len(self.news.get_recent_alerts(9999))}
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
    print(f"  SCALPING TARGETS v4 — {len(markets)} markets (brain-filtered)")
    print(f"  Filter: fee-free | spread≥3¢ | liq≥$3K | vol≥$2K | price 5-95¢")
    print(f"{'='*70}\n")

    for i, m in enumerate(markets):
        mid = (m.best_bid + m.best_ask) / 2
        buy_at = round(mid - max(0.005, m.spread/2 - 0.02), 4)
        sell_at = round(mid + 0.005, 4)
        profit_per = (sell_at - buy_at) * (cfg.per_order / buy_at) if buy_at > 0 else 0

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

        # v4: Show GTD recommendation
        gtd = int(m.spread * 1500)
        gtd = max(60, min(300, gtd))
        tags.append(f"⏰GTD={gtd}s")

        tag_str = f" [{' '.join(tags)}]" if tags else ""

        print(f"  {i+1:>2}. {m.question[:55]}{tag_str}")
        print(f"      Spread: {m.spread*100:.1f}¢ | Bid: ${m.best_bid:.3f} | Ask: ${m.best_ask:.3f}")
        print(f"      Liq: ${m.liquidity:,.0f} | Vol: ${m.volume:,.0f} | Tick: {m.tick_size}")
        print(f"      → Buy @ ${buy_at:.3f} | Sell @ ${sell_at:.3f} | Est: ${profit_per:.2f}/trade")
        print()

    print(f"  {'─'*60}")
    print(f"  💰 With ${cfg.capital:.0f} capital:")
    print(f"     Kelly size: ${brain.get_kelly_order_size(cfg.capital):.2f}/order")
    print(f"     Exposure: ${cfg.capital * cfg.max_exposure_pct:.0f} ({cfg.max_exposure_pct:.0%})")
    print(f"     Reserve: ${cfg.capital * (1-cfg.max_exposure_pct):.0f}")
    print(f"     Best hours: {brain.get_best_time_category()}")
    print(f"     Post-only: {cfg.post_only}")
    print()


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Polymarket Scalper v4 (Brain-Powered, Multi-Strategy)")
    parser.add_argument("--scan", action="store_true", help="Discover targets (brain-informed)")
    parser.add_argument("--paper", action="store_true", help="Paper trade with learning")
    parser.add_argument("--live", action="store_true", help="Live trading (needs .env)")
    parser.add_argument("--brain", action="store_true", help="Show brain status")
    parser.add_argument("--brain-reset", action="store_true", help="Wipe learned data")
    parser.add_argument("--strategies", action="store_true", help="Show available strategies")
    parser.add_argument("--capital", type=float, default=None)
    parser.add_argument("--per-order", type=float, default=None)
    parser.add_argument("--strategy", type=str, default=None, choices=["one_side", "both_sides"])
    parser.add_argument("--post-only", action="store_true", default=None, help="Use post-only orders")
    parser.add_argument("--no-post-only", action="store_true", help="Disable post-only orders")
    parser.add_argument("--supervised", action="store_true", help="AI supervisor mode — orders require approval")
    args = parser.parse_args()

    cfg = Config()
    if args.capital:
        cfg.capital = args.capital
    if args.per_order:
        cfg.per_order = args.per_order
    if args.strategy:
        cfg.strategy = args.strategy
    if args.post_only is not None:
        cfg.post_only = True
    if args.no_post_only:
        cfg.post_only = False
    if args.supervised:
        cfg.supervised = True

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
  AVAILABLE STRATEGIES (v4)
  ═══════════════════════════════════════════════

  --strategy one_side (default)
    Place BUY orders inside the spread, SELL on fill.
    Simple, proven, lower risk.

  --strategy both_sides
    True market making. Simultaneous BID + ASK orders
    inside the spread. Halved per-order size for safety.
    Uses inventory skew and flow-aware quoting.

  --post-only (default: on)
    Guarantees maker status. Orders that would cross
    the spread are rejected instead of executing as taker.

  v4 UPGRADES:
    1. Book-depth-aware paper fills
    2. Realized vs committed capital tracking
    3. Fixed short-selling accounting
    4. Spread-proportional GTD timing
    5. Both-sides capital guard (halved sizing)
    6. Aggressive neg-risk NO-side quoting
    7. News decay + auto-un-skip (10 min)
    8. Post-only order guarantee

  --supervised
    AI supervisor mode. Every order is proposed to the
    supervisor (supervisor.py) instead of placed directly.
    The supervisor researches context, checks for risks,
    and approves/rejects/modifies before execution.
    Exits are always fast (unsupervised).
    Requires: python3 supervisor.py (in another terminal)

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
