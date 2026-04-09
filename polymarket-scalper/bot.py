#!/usr/bin/env python3
"""
Polymarket Scalper — High-velocity spread capture bot with ADAPTIVE BRAIN.

The brain persists across sessions in brain.json. It learns from every trade:
- Which markets make money → trade them more aggressively
- Which markets lose money → avoid or reduce exposure
- What entry conditions lead to wins → replicate them
- What patterns lead to losses → never repeat them

Usage:
  python3 bot.py --scan           # Discover scalping targets (brain-informed)
  python3 bot.py --paper          # Paper trade with learning
  python3 bot.py --live           # Live trading (needs .env)
  python3 bot.py --brain          # Show what the brain has learned
  python3 bot.py --brain-reset    # Wipe all learned data
  python3 bot.py --live --capital 100 --per-order 10
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
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from datetime import datetime, timezone

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
    # Brain context at time of placement
    brain_score: float = 0.0
    entry_spread: float = 0.0
    entry_volume: float = 0.0
    entry_liquidity: float = 0.0
    entry_price_range: str = ""  # "low" "mid" "high"


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
    # Brain context
    entry_spread: float = 0.0
    entry_volume: float = 0.0
    entry_liquidity: float = 0.0
    entry_price_range: str = ""


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
    # Brain context
    entry_spread: float = 0.0
    entry_volume: float = 0.0
    entry_liquidity: float = 0.0
    entry_price_range: str = ""
    exit_type: str = ""  # "profit", "timeout", "stop_loss", "shutdown"


# ══════════════════════════════════════════════════════════════
# ADAPTIVE BRAIN — learns from every trade, persists forever
# ══════════════════════════════════════════════════════════════

class Brain:
    """
    The brain remembers everything. It tracks:
    - Per-market reputation (win rate, avg PnL, fill speed, risk)
    - Entry condition patterns (what spread/volume/price combos work)
    - Losing patterns to never repeat
    - Winning patterns to double down on
    - Session-level statistics
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
                    return data
            except (json.JSONDecodeError, Exception) as e:
                LOG.warning(f"Brain file corrupted, starting fresh: {e}")
        return self._default()

    def _default(self) -> dict:
        return {
            "version": 2,
            "total_trades": 0,
            "total_pnl": 0.0,
            "total_wins": 0,
            "total_losses": 0,
            "sessions": 0,
            "first_seen": time.time(),
            "markets": {},          # slug -> market reputation
            "patterns": {           # entry condition outcomes
                "by_spread": {},    # "0.03-0.05" -> {wins, losses, pnl}
                "by_volume": {},    # "1000-5000" -> {wins, losses, pnl}
                "by_liquidity": {}, # "2000-5000" -> {wins, losses, pnl}
                "by_price": {},     # "low|mid|high" -> {wins, losses, pnl}
                "by_hold_time": {}, # "0-60|60-120|..." -> {wins, losses, pnl}
            },
            "avoid_list": [],       # slugs to never trade
            "star_list": [],        # slugs that consistently profit
            "rules": [],            # learned rules in plain text
            "last_updated": time.time(),
        }

    def save(self):
        self.data["last_updated"] = time.time()
        self.data["sessions"] = self.data.get("sessions", 0)
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
            "risk_score": 0.5,  # 0=safe, 1=dangerous
            "profit_score": 0.5,  # 0=bad, 1=great
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

        # Running avg hold time
        old_avg = rep.get("avg_hold", 0)
        n = rep["trades"]
        rep["avg_hold"] = old_avg + (trade["hold_sec"] - old_avg) / n

        # Risk score: higher = more dangerous
        # Weighted by: timeout rate, loss rate, negative PnL
        timeout_rate = rep.get("timeouts", 0) / max(1, rep["trades"])
        loss_rate = rep["losses"] / max(1, rep["trades"])
        pnl_penalty = max(0, -rep["avg_pnl"]) * 10  # scale up small losses
        rep["risk_score"] = min(1.0, (timeout_rate * 0.4 + loss_rate * 0.4 + min(pnl_penalty, 0.2)))

        # Profit score: higher = more profitable
        # Weighted by: win rate, avg PnL, fill rate
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

    # ─── Learning from Trades ────────────────────────────────

    def learn_from_trade(self, trade: Trade):
        """Core learning function. Call after every completed trade."""
        is_win = trade.pnl > 0
        t = {
            "slug": trade.slug,
            "question": trade.question,
            "pnl": trade.pnl,
            "hold_sec": trade.hold_sec,
            "entry_spread": trade.entry_spread,
            "entry_volume": trade.entry_volume,
            "entry_liquidity": trade.entry_liquidity,
            "entry_price_range": trade.entry_price_range,
            "exit_type": trade.exit_type,
            "reason": trade.reason,
            "ts": trade.ts,
            "is_win": is_win,
        }
        self._session_trades.append(t)

        # Update global stats
        self.data["total_trades"] += 1
        self.data["total_pnl"] += trade.pnl
        if is_win:
            self.data["total_wins"] += 1
        else:
            self.data["total_losses"] += 1

        # Update market reputation
        self._update_market_rep(trade.slug, trade.question, t)

        # Update patterns
        self._update_pattern("by_spread",
            self._bucket_spread(trade.entry_spread), is_win, trade.pnl)
        self._update_pattern("by_volume",
            self._bucket_volume(trade.entry_volume), is_win, trade.pnl)
        self._update_pattern("by_liquidity",
            self._bucket_liquidity(trade.entry_liquidity), is_win, trade.pnl)
        self._update_pattern("by_price",
            trade.entry_price_range, is_win, trade.pnl)
        self._update_pattern("by_hold_time",
            self._bucket_hold(trade.hold_sec), is_win, trade.pnl)

        # Update avoid/star lists
        self._update_lists()

        # Generate rules if enough data
        if self.data["total_trades"] % 10 == 0:
            self._generate_rules()

        self.save()

        # Log what we learned
        rep = self.get_market_rep(trade.slug)
        emoji = "🟢" if is_win else "🔴"
        LOG.info(
            f"🧠 LEARNED | {emoji} {trade.slug[:35]} | "
            f"Market WR: {rep['win_rate']:.0%} ({rep['trades']} trades) | "
            f"Risk: {rep['risk_score']:.2f} | Profit: {rep['profit_score']:.2f}"
        )

    def _update_lists(self):
        """Rebuild avoid and star lists from market reputations."""
        avoid = []
        stars = []
        for slug, rep in self.data["markets"].items():
            if rep["trades"] < 3:
                continue  # need minimum data
            if rep["risk_score"] > 0.7 and rep["win_rate"] < 0.3:
                avoid.append(slug)
                if slug not in self.data["avoid_list"]:
                    rule = f"AVOID: {slug[:50]} — {rep['win_rate']:.0%} WR, {rep['trades']} trades, risk={rep['risk_score']:.2f}"
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
        """Analyze patterns and generate actionable rules."""
        rules = []

        # Spread analysis
        for bucket, stats in self.data["patterns"].get("by_spread", {}).items():
            if stats["trades"] < 5:
                continue
            wr = stats["wins"] / stats["trades"]
            if wr < 0.3:
                rules.append({
                    "ts": time.time(),
                    "rule": f"SPREAD {bucket}: {wr:.0%} WR ({stats['trades']} trades) — reduce aggression",
                    "type": "pattern_avoid",
                })
            elif wr > 0.7:
                rules.append({
                    "ts": time.time(),
                    "rule": f"SPREAD {bucket}: {wr:.0%} WR (${stats['total_pnl']:+.2f}) — prioritize these",
                    "type": "pattern_star",
                })

        # Volume analysis
        for bucket, stats in self.data["patterns"].get("by_volume", {}).items():
            if stats["trades"] < 5:
                continue
            wr = stats["wins"] / stats["trades"]
            if wr < 0.3:
                rules.append({
                    "ts": time.time(),
                    "rule": f"VOLUME {bucket}: {wr:.0%} WR — avoid thin markets",
                    "type": "pattern_avoid",
                })

        # Price range analysis
        for bucket, stats in self.data["patterns"].get("by_price", {}).items():
            if stats["trades"] < 5:
                continue
            wr = stats["wins"] / stats["trades"]
            if wr < 0.3:
                rules.append({
                    "ts": time.time(),
                    "rule": f"PRICE {bucket}: {wr:.0%} WR — too extreme, avoid",
                    "type": "pattern_avoid",
                })

        # Keep only last 50 rules
        self.data["rules"] = (self.data["rules"] + rules)[-50:]

    # ─── Querying the Brain ──────────────────────────────────

    def should_trade_market(self, slug: str) -> Tuple[bool, str]:
        """Should we trade this market? Returns (decision, reason)."""
        if slug in self.data["avoid_list"]:
            rep = self.get_market_rep(slug)
            return False, f"AVOID LIST: {rep['win_rate']:.0%} WR, {rep['trades']} losses learned"

        rep = self.get_market_rep(slug)
        if rep["trades"] >= 5 and rep["risk_score"] > 0.8:
            return False, f"HIGH RISK: score={rep['risk_score']:.2f}, {rep['timeouts']} timeouts"

        return True, "OK"

    def is_star_market(self, slug: str) -> bool:
        return slug in self.data["star_list"]

    def get_market_risk(self, slug: str) -> float:
        """Returns 0.0 (safe) to 1.0 (dangerous)."""
        rep = self.get_market_rep(slug)
        return rep["risk_score"]

    def get_market_profit_score(self, slug: str) -> float:
        """Returns 0.0 (bad) to 1.0 (great)."""
        rep = self.get_market_rep(slug)
        return rep["profit_score"]

    def get_order_size_multiplier(self, slug: str) -> float:
        """
        Adjust order size based on brain knowledge.
        Stars get 1.5x, unknown get 1.0x, risky get 0.5x.
        """
        if self.is_star_market(slug):
            return 1.5
        risk = self.get_market_risk(slug)
        if risk > 0.6:
            return 0.5
        if risk > 0.4:
            return 0.75
        return 1.0

    def get_spread_preference(self) -> Optional[str]:
        """Which spread bucket has the best win rate?"""
        best_wr = 0
        best_bucket = None
        for bucket, stats in self.data["patterns"].get("by_spread", {}).items():
            if stats["trades"] < 5:
                continue
            wr = stats["wins"] / stats["trades"]
            if wr > best_wr:
                best_wr = wr
                best_bucket = bucket
        return best_bucket

    def score_market_for_entry(self, slug: str, spread: float, volume: float,
                                liquidity: float, price: float) -> float:
        """
        Combined score (0-1) for whether to enter a market.
        Factors in: base metrics + brain knowledge + pattern matching.
        """
        # Base score from market metrics
        spread_score = min(1.0, spread / 0.15)  # wider spread = better for us
        volume_score = min(1.0, math.log10(max(volume, 1)) / 6)  # log scale
        liq_score = min(1.0, math.log10(max(liquidity, 1)) / 5)
        base = (spread_score * 0.4 + volume_score * 0.3 + liq_score * 0.3)

        # Brain adjustment
        rep = self.get_market_rep(slug)
        if rep["trades"] >= 3:
            # Blend base with learned profit score
            brain_weight = min(0.6, rep["trades"] / 20)  # trust brain more with more data
            brain_adj = rep["profit_score"] * (1 - rep["risk_score"])
            score = base * (1 - brain_weight) + brain_adj * brain_weight
        else:
            # Not enough data — slight penalty for unknown markets
            score = base * 0.9

        # Penalty if this spread/volume/price bucket has bad history
        spread_bucket = self._bucket_spread(spread)
        spread_stats = self.data["patterns"]["by_spread"].get(spread_bucket, {})
        if spread_stats.get("trades", 0) >= 5:
            bucket_wr = spread_stats["wins"] / spread_stats["trades"]
            if bucket_wr < 0.3:
                score *= 0.5  # heavy penalty for known-bad spread ranges

        vol_bucket = self._bucket_volume(volume)
        vol_stats = self.data["patterns"]["by_volume"].get(vol_bucket, {})
        if vol_stats.get("trades", 0) >= 5:
            bucket_wr = vol_stats["wins"] / vol_stats["trades"]
            if bucket_wr < 0.3:
                score *= 0.5

        price_bucket = self._bucket_price(price)
        price_stats = self.data["patterns"]["by_price"].get(price_bucket, {})
        if price_stats.get("trades", 0) >= 5:
            bucket_wr = price_stats["wins"] / price_stats["trades"]
            if bucket_wr < 0.3:
                score *= 0.6

        return round(max(0.0, min(1.0, score)), 4)

    def get_exit_aggressiveness(self, slug: str) -> float:
        """
        How aggressively should we exit? 0=patient, 1=cut immediately.
        Markets that tend to timeout get more aggressive exits.
        """
        rep = self.get_market_rep(slug)
        if rep["trades"] < 3:
            return 0.5
        timeout_rate = rep.get("timeouts", 0) / rep["trades"]
        return min(1.0, timeout_rate + 0.2)

    def should_adjust_hold_time(self, slug: str) -> Optional[int]:
        """
        Suggest a custom max hold time for this market based on history.
        Returns None to use default, or seconds.
        """
        rep = self.get_market_rep(slug)
        if rep["trades"] < 5:
            return None
        if rep.get("timeouts", 0) / rep["trades"] > 0.5:
            # This market keeps timing out — hold shorter
            suggested = max(60, int(rep.get("avg_hold", 300) * 0.7))
            return suggested
        return None

    def get_known_bad_entry_conditions(self) -> List[str]:
        """Return pattern buckets that have consistently lost money."""
        bad = []
        for category in ["by_spread", "by_volume", "by_liquidity", "by_price"]:
            for bucket, stats in self.data["patterns"].get(category, {}).items():
                if stats["trades"] >= 5 and stats["wins"] / stats["trades"] < 0.25:
                    bad.append(f"{category}:{bucket} ({stats['wins']}/{stats['trades']} wins)")
        return bad

    # ─── Reporting ───────────────────────────────────────────

    def report(self) -> str:
        """Full brain status report."""
        lines = []
        lines.append(f"\n{'='*60}")
        lines.append(f"  🧠 BRAIN STATUS")
        lines.append(f"{'='*60}")
        lines.append(f"  Sessions:     {self.data.get('sessions', 0)}")
        lines.append(f"  Total Trades: {self.data['total_trades']}")
        lines.append(f"  Total PnL:    ${self.data['total_pnl']:+.2f}")
        lines.append(f"  Win Rate:     {self.data['total_wins']}/{self.data['total_losses']} "
                     f"({self.data['total_wins']/max(1, self.data['total_trades']):.0%})")
        lines.append(f"  Avoid List:   {len(self.data['avoid_list'])} markets")
        lines.append(f"  Star List:    {len(self.data['star_list'])} markets")

        if self.data["avoid_list"]:
            lines.append(f"\n  🚫 AVOIDED MARKETS:")
            for slug in self.data["avoid_list"][:10]:
                rep = self.get_market_rep(slug)
                lines.append(f"    • {slug[:45]} | WR={rep['win_rate']:.0%} | "
                           f"Risk={rep['risk_score']:.2f} | ${rep['avg_pnl']:+.3f}/trade")

        if self.data["star_list"]:
            lines.append(f"\n  ⭐ STAR MARKETS:")
            for slug in self.data["star_list"][:10]:
                rep = self.get_market_rep(slug)
                lines.append(f"    • {slug[:45]} | WR={rep['win_rate']:.0%} | "
                           f"Profit={rep['profit_score']:.2f} | ${rep['avg_pnl']:+.3f}/trade")

        # Pattern insights
        lines.append(f"\n  📊 PATTERN INSIGHTS:")
        for cat_name, cat_key in [("Spread", "by_spread"), ("Volume", "by_volume"),
                                   ("Price Range", "by_price")]:
            patterns = self.data["patterns"].get(cat_key, {})
            if not patterns:
                continue
            lines.append(f"    {cat_name}:")
            for bucket, stats in sorted(patterns.items()):
                if stats["trades"] < 3:
                    continue
                wr = stats["wins"] / stats["trades"]
                emoji = "🟢" if wr > 0.6 else ("🔴" if wr < 0.4 else "⚪")
                lines.append(f"      {emoji} {bucket}: {wr:.0%} WR | {stats['trades']} trades | ${stats['total_pnl']:+.2f}")

        if self.data["rules"]:
            lines.append(f"\n  📜 LEARNED RULES (last 10):")
            for rule in self.data["rules"][-10:]:
                rtype = rule.get("type", "")
                emoji = "🚫" if "avoid" in rtype else "⭐"
                lines.append(f"    {emoji} {rule['rule']}")

        spread_pref = self.get_spread_preference()
        if spread_pref:
            lines.append(f"\n  💡 Best spread range: {spread_pref}")

        bad_conditions = self.get_known_bad_entry_conditions()
        if bad_conditions:
            lines.append(f"\n  ⚠️  KNOWN BAD CONDITIONS:")
            for bc in bad_conditions:
                lines.append(f"    • {bc}")

        lines.append(f"{'='*60}")
        return "\n".join(lines)

    def session_report(self) -> str:
        """What did we learn this session?"""
        if not self._session_trades:
            return "🧠 No trades this session — nothing new learned."

        wins = [t for t in self._session_trades if t["is_win"]]
        losses = [t for t in self._session_trades if not t["is_win"]]
        pnl = sum(t["pnl"] for t in self._session_trades)

        lines = [
            f"\n🧠 SESSION LEARNING SUMMARY",
            f"  Trades: {len(self._session_trades)} ({len(wins)}W / {len(losses)}L)",
            f"  PnL: ${pnl:+.3f}",
            f"  New avoid: {len(self.data['avoid_list'])} markets",
            f"  New stars: {len(self.data['star_list'])} markets",
        ]

        # What patterns won/lost this session?
        session_won_buckets = defaultdict(int)
        session_lost_buckets = defaultdict(int)
        for t in self._session_trades:
            bucket = f"spread={self._bucket_spread(t['entry_spread'])}, vol={self._bucket_volume(t['entry_volume'])}"
            if t["is_win"]:
                session_won_buckets[bucket] += 1
            else:
                session_lost_buckets[bucket] += 1

        if session_won_buckets:
            lines.append("  Winning conditions:")
            for b, c in sorted(session_won_buckets.items(), key=lambda x: -x[1])[:3]:
                lines.append(f"    🟢 {b} ({c} wins)")

        if session_lost_buckets:
            lines.append("  Losing conditions:")
            for b, c in sorted(session_lost_buckets.items(), key=lambda x: -x[1])[:3]:
                lines.append(f"    🔴 {b} ({c} losses)")

        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# MARKET DISCOVERY (brain-informed)
# ══════════════════════════════════════════════════════════════

async def discover_markets(cfg: Config, brain: Optional[Brain] = None) -> List[Market]:
    """Fetch and filter markets suitable for scalping. Brain-informed ranking."""
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"{GAMMA_URL}/events",
            params={"active": "true", "closed": "false", "limit": 500},
        ) as r:
            events = await r.json()

    all_markets = []
    for ev in events:
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

            # Filters
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

            # Brain filter: skip markets on the avoid list
            if brain:
                should_trade, reason = brain.should_trade_market(slug)
                if not should_trade:
                    LOG.info(f"🧠 BRAIN SKIP | {slug[:40]} | {reason}")
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
            ))

    # Brain-informed scoring
    for m in all_markets:
        if brain:
            m._score = brain.score_market_for_entry(
                m.slug, m.spread, m.volume, m.liquidity, m.yes_price
            )
            # Boost star markets
            if brain.is_star_market(m.slug):
                m._score *= 1.3
                LOG.info(f"🧠 STAR BOOST | {m.slug[:40]} | score={m._score:.3f}")
        else:
            # Fallback: original scoring
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

    def best(self, token: str) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
        book = self.books.get(token)
        if not book:
            return None, None, None, None
        bb = book["bids"][0] if book["bids"] else (None, None)
        ba = book["asks"][0] if book["asks"] else (None, None)
        return bb[0], ba[0], bb[1], ba[1]


# ══════════════════════════════════════════════════════════════
# ORDER MANAGER (brain-aware)
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
        self._market_questions: Dict[str, str] = {}  # slug -> question

    def set_market_questions(self, markets: Dict[str, Market]):
        self._market_questions = {s: m.question for s, m in markets.items()}

    async def init_client(self):
        if not self.cfg.is_live:
            LOG.info("No API keys — paper mode")
            return
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import OrderType

            self.client = ClobClient(
                CLOB_URL,
                key=self.cfg.private_key,
                chain_id=137,
                signature_type=self.cfg.sig_type,
                funder=self.cfg.funder,
            )
            creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(creds)
            LOG.info(f"Polymarket client initialized | funder={self.cfg.funder[:10]}...")
        except Exception as e:
            LOG.error(f"Failed to init client: {e}")
            self.client = None

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
        """Get order size adjusted by brain knowledge."""
        base_size = self.cfg.per_order
        if self.brain:
            multiplier = self.brain.get_order_size_multiplier(slug)
            adjusted = base_size * multiplier
            if multiplier != 1.0:
                LOG.info(f"🧠 SIZE ADJUST | {slug[:35]} | {base_size:.0f} → {adjusted:.0f} (×{multiplier})")
            return adjusted
        return base_size

    async def place_limit(self, slug: str, token: str, side: str, price: float,
                          size_usd: float, market: Optional[Market] = None) -> Optional[Order]:
        price = round(price, 4)
        shares = round(size_usd / price, 2)

        # Capture brain context for learning
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

        order = Order(
            id=f"{slug}_{side}_{int(time.time()*1000)}",
            slug=slug,
            token=token,
            side=side,
            price=price,
            size=size_usd,
            shares=shares,
            brain_score=brain_score,
            entry_spread=entry_spread,
            entry_volume=entry_volume,
            entry_liquidity=entry_liquidity,
            entry_price_range=entry_price_range,
        )

        if self.paper:
            order.status = "live"
            order.exchange_id = f"paper_{order.id}"
            LOG.info(f"📝 PAPER ORDER | {side} {shares:.0f} shares @ ${price:.4f} on {slug[:40]}")
            self.orders[order.id] = order
            return order

        if not self.client:
            return None

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            side_const = BUY if side == "BUY" else SELL
            order_args = OrderArgs(
                token_id=token,
                price=price,
                size=shares,
                side=side_const,
            )
            signed = self.client.create_order(order_args)
            resp = self.client.post_order(signed, OrderType.GTC)

            if resp:
                order.exchange_id = resp.get("orderID", resp.get("order_id", ""))
                order.status = "live"
                LOG.info(f"✅ LIVE ORDER | {side} {shares:.0f} shares @ ${price:.4f} on {slug[:40]} | ID={order.exchange_id[:12]}")
                self.orders[order.id] = order
                return order
            else:
                LOG.error(f"❌ Order rejected: {resp}")
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
            LOG.info(f"🚫 Canceled {order.exchange_id[:12]} on {order.slug[:30]}")
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
                slug=order.slug,
                token=order.token,
                side="LONG",
                entry_price=fill_price,
                shares=order.shares,
                cost=cost,
                entry_spread=order.entry_spread,
                entry_volume=order.entry_volume,
                entry_liquidity=order.entry_liquidity,
                entry_price_range=order.entry_price_range,
            )
            LOG.info(f"🟢 FILLED BUY | {order.shares:.0f} @ ${fill_price:.4f} | {order.slug[:40]}")
        else:
            pos = self.positions.pop(order.slug, None)
            if pos:
                pnl = (fill_price - pos.entry_price) * pos.shares
                self.capital += pos.shares * fill_price
                self.peak = max(self.peak, self.capital)
                hold = time.time() - pos.opened

                trade = Trade(
                    slug=order.slug,
                    question=self._market_questions.get(order.slug, ""),
                    entry_price=pos.entry_price,
                    exit_price=fill_price,
                    shares=pos.shares,
                    pnl=pnl,
                    hold_sec=hold,
                    reason="filled",
                    entry_spread=pos.entry_spread,
                    entry_volume=pos.entry_volume,
                    entry_liquidity=pos.entry_liquidity,
                    entry_price_range=pos.entry_price_range,
                    exit_type="profit" if pnl > 0 else "loss",
                )
                self.trades.append(trade)

                # LEARN from this trade
                if self.brain:
                    self.brain.learn_from_trade(trade)

                emoji = "🟢" if pnl > 0 else "🔴"
                LOG.info(f"{emoji} FILLED SELL | {pos.shares:.0f} @ ${fill_price:.4f} | PnL=${pnl:+.3f} | {order.slug[:40]}")

    def force_exit_position(self, slug: str, exit_price: float, reason: str):
        pos = self.positions.pop(slug, None)
        if not pos:
            return
        pnl = (exit_price - pos.entry_price) * pos.shares
        self.capital += pos.shares * exit_price
        self.peak = max(self.peak, self.capital)
        hold = time.time() - pos.opened

        exit_type = reason  # "timeout", "shutdown", "circuit_breaker"

        trade = Trade(
            slug=slug,
            question=self._market_questions.get(slug, ""),
            entry_price=pos.entry_price,
            exit_price=exit_price,
            shares=pos.shares,
            pnl=pnl,
            hold_sec=hold,
            reason=reason,
            entry_spread=pos.entry_spread,
            entry_volume=pos.entry_volume,
            entry_liquidity=pos.entry_liquidity,
            entry_price_range=pos.entry_price_range,
            exit_type=exit_type,
        )
        self.trades.append(trade)

        # LEARN from forced exits (these are the most important lessons)
        if self.brain:
            self.brain.learn_from_trade(trade)

        emoji = "🟢" if pnl > 0 else "🔴"
        LOG.info(f"{emoji} FORCE EXIT | {pos.shares:.0f} @ ${exit_price:.4f} | PnL=${pnl:+.3f} | {reason} | {slug[:40]}")

    def check_timeouts(self, market_prices: Dict[str, float]):
        now = time.time()
        for slug, pos in list(self.positions.items()):
            # Brain may suggest shorter hold times for problematic markets
            max_hold = self.cfg.max_hold_sec
            if self.brain:
                suggested = self.brain.should_adjust_hold_time(slug)
                if suggested:
                    max_hold = suggested
                    LOG.info(f"🧠 HOLD ADJUST | {slug[:35]} | {self.cfg.max_hold_sec}s → {suggested}s (brain)")

            if now - pos.opened > max_hold:
                price = market_prices.get(slug, pos.entry_price)
                self.force_exit_position(slug, price, "timeout")

    def summary(self) -> str:
        wins = [t for t in self.trades if t.pnl > 0]
        losses = [t for t in self.trades if t.pnl <= 0]
        total_pnl = sum(t.pnl for t in self.trades)
        wr = len(wins) / max(1, len(self.trades))
        return (
            f"Capital=${self.capital:.2f} ({self.daily_pnl:+.2f}) | "
            f"Pos={len(self.positions)} | "
            f"Trades={len(self.trades)} ({wr:.0%} WR) | "
            f"PnL=${total_pnl:+.3f} | "
            f"DD={self.drawdown:.1%}"
        )


# ══════════════════════════════════════════════════════════════
# SCALPING ENGINE (brain-powered)
# ══════════════════════════════════════════════════════════════

class Scalper:
    def __init__(self, cfg: Config, paper: bool = True):
        self.cfg = cfg
        self.paper = paper
        self.brain = Brain()
        self.om = OrderManager(cfg, paper, brain=self.brain)
        self.feed = Feed(cfg)
        self.markets: Dict[str, Market] = {}
        self.token_to_slug: Dict[str, str] = {}
        self.running = False
        self.tick = 0
        self._last_reprice = 0

    async def start(self, mode: str = "paper"):
        self.brain.start_session()

        # Show brain status
        if self.brain.data["total_trades"] > 0:
            LOG.info(f"🧠 Brain: {self.brain.data['total_trades']} trades in memory | "
                    f"Avoiding: {len(self.brain.data['avoid_list'])} markets | "
                    f"Stars: {len(self.brain.data['star_list'])} markets")

        LOG.info("=" * 60)
        LOG.info(f"  SCALPER | ${self.cfg.capital:.0f} | {mode.upper()}")
        LOG.info(f"  Exposed: {self.cfg.max_exposure_pct:.0%} | Per order: ${self.cfg.per_order:.0f}")
        LOG.info(f"  Max concurrent: {self.cfg.max_concurrent} | Reprice: {self.cfg.reprice_sec}s")
        LOG.info(f"  Max hold: {self.cfg.max_hold_sec}s | Circuit breaker: {self.cfg.circuit_breaker_pct:.0%}")
        LOG.info("=" * 60)

        # Discover markets (brain-filtered)
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

        LOG.info(f"📊 {len(self.markets)} markets selected:")
        for m in market_list:
            brain_tag = ""
            if self.brain.is_star_market(m.slug):
                brain_tag = " ⭐STAR"
            risk = self.brain.get_market_risk(m.slug)
            if risk > 0.5:
                brain_tag += f" ⚠️RISK={risk:.2f}"
            LOG.info(f"  • {m.question[:55]} | {m.spread*100:.1f}¢ | ${m.liquidity:,.0f} liq | ${m.volume:,.0f} vol{brain_tag}")

        # Init client
        await self.om.init_client()

        # Start WebSocket
        tokens = [m.yes_token for m in market_list if m.yes_token]
        ws_task = asyncio.create_task(self.feed.start(tokens))

        # Register WS callback
        self.feed.on_update(self._on_book_update)

        # Main loop
        self.running = True
        LOG.info("🚀 Scalper running — Ctrl+C to stop\n")

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

        # Cancel stale orders (brain may adjust timing per market)
        for oid, order in list(self.om.orders.items()):
            stale_threshold = 120
            # Brain: if market has low fill rate historically, don't wait as long
            if self.brain:
                rep = self.brain.get_market_rep(order.slug)
                if rep["trades"] >= 5:
                    fill_rate = rep.get("fills", 0) / rep["trades"]
                    if fill_rate < 0.3:
                        stale_threshold = 60  # cancel faster on low-fill markets
            if order.status == "live" and (now - order.created) > stale_threshold:
                await self.om.cancel_order(order)
                LOG.info(f"⏰ STALE ORDER | {order.side} @ ${order.price:.4f} | {order.slug[:40]}")

        if self.tick % 10 == 0:
            prices = {slug: m.yes_price for slug, m in self.markets.items()}
            self.om.check_timeouts(prices)

        if now - self._last_reprice > self.cfg.reprice_sec:
            await self._reprice()
            self._last_reprice = now

        if self.paper:
            await self._paper_fill_check()

        if self.tick % 60 == 0:
            live_orders = sum(1 for o in self.om.orders.values() if o.status == "live")
            LOG.info(f"[T+{self.tick}s] {self.om.summary()} | Live orders: {live_orders}")

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

        # Reactive cancellation
        price_moved = abs(mid - old_mid)
        if price_moved > 0.01:
            canceled_count = 0
            for oid, order in list(self.om.orders.items()):
                if order.status == "live" and order.slug == slug:
                    await self.om.cancel_order(order)
                    canceled_count += 1
            if canceled_count:
                LOG.info(f"⚡ REACTIVE CANCEL | {slug[:40]} | mid moved {price_moved*100:.1f}¢")

                if slug not in self.om.positions:
                    ok, _ = self.om.can_enter()
                    if ok:
                        # Brain may adjust aggressiveness
                        aggression = 0.5
                        if self.brain:
                            aggression = self.brain.get_exit_aggressiveness(slug)
                        offset = max(0.005, spread/2 - self.cfg.spread_target) * (1 + aggression * 0.3)
                        buy_price = round(mid - offset, 4)
                        buy_price = max(buy_price, round(bb + 0.001, 4))
                        if 0 < buy_price < 1:
                            size = self.om.get_brain_adjusted_size(slug, market)
                            await self.om.place_limit(slug, market.yes_token, "BUY", buy_price, size, market)
                else:
                    pos = self.om.positions[slug]
                    exit_price = round(mid + 0.005, 4)
                    if mid < pos.entry_price - 0.01:
                        exit_price = round(bb, 4)
                    await self.om.place_limit(slug, market.yes_token, "SELL", exit_price, pos.cost, market)

        # Paper fill simulation
        if self.paper:
            for oid, order in list(self.om.orders.items()):
                if order.status != "live" or order.slug != slug:
                    continue
                if order.side == "BUY" and bb <= order.price:
                    self.om.fill_order(order, order.price)
                elif order.side == "SELL" and ba >= order.price:
                    self.om.fill_order(order, order.price)

    async def _paper_fill_check(self):
        import random
        now = time.time()
        for oid, order in list(self.om.orders.items()):
            if order.status != "live":
                continue
            market = self.markets.get(order.slug)
            if not market:
                continue

            age = now - order.created
            if age < 30:
                continue

            if order.side == "BUY":
                if order.price >= market.best_bid and market.best_bid > 0:
                    fill_rate = min(0.03 * (age / 30), 0.15)
                    fill_rate *= min(market.volume / 50000, 2.0)
                    if random.random() < fill_rate:
                        self.om.fill_order(order, order.price)

            elif order.side == "SELL":
                if order.price <= market.best_ask and market.best_ask < 1:
                    fill_rate = min(0.03 * (age / 30), 0.15)
                    fill_rate *= min(market.volume / 50000, 2.0)
                    if random.random() < fill_rate:
                        self.om.fill_order(order, order.price)

    async def _reprice(self):
        for oid, order in list(self.om.orders.items()):
            if order.status == "live":
                await self.om.cancel_order(order)

        for slug, market in self.markets.items():
            if market.best_bid <= 0 or market.best_ask >= 1:
                continue
            if market.spread < self.cfg.min_spread:
                continue

            # Brain check: skip repricing for avoided markets
            if self.brain:
                should_trade, reason = self.brain.should_trade_market(slug)
                if not should_trade:
                    continue

            mid = (market.best_bid + market.best_ask) / 2

            if slug in self.om.positions:
                pos = self.om.positions[slug]
                hold_sec = time.time() - pos.opened

                # Brain-informed exit aggressiveness
                aggression = 0.5
                if self.brain:
                    aggression = self.brain.get_exit_aggressiveness(slug)

                if hold_sec > self.cfg.max_hold_sec * (0.9 - aggression * 0.3):
                    exit_price = round(market.best_bid, 4)
                elif mid > pos.entry_price + 0.005:
                    # Brain: if market is a star, be more patient on exits
                    patience = 0.003
                    if self.brain and self.brain.is_star_market(slug):
                        patience = 0.005
                    exit_price = round(mid + patience, 4)
                elif mid > pos.entry_price - 0.005:
                    exit_price = round(mid, 4)
                else:
                    exit_price = round(market.best_bid, 4)

                await self.om.place_limit(
                    slug=slug, token=market.yes_token, side="SELL",
                    price=exit_price, size_usd=pos.cost, market=market,
                )
                continue

            ok, reason = self.om.can_enter()
            if not ok:
                continue

            # Brain-adjusted entry price
            half_spread = market.spread / 2
            buy_price = round(mid - max(0.005, half_spread - self.cfg.spread_target), 4)
            buy_price = max(buy_price, round(market.best_bid + 0.001, 4))

            if buy_price <= 0 or buy_price >= 1:
                continue

            # Brain-adjusted order size
            size = self.om.get_brain_adjusted_size(slug, market)

            await self.om.place_limit(
                slug=slug, token=market.yes_token, side="BUY",
                price=buy_price, size_usd=size, market=market,
            )

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
  FINAL REPORT
{'='*60}
  Capital:         ${self.om.capital:.2f} (started ${self.cfg.capital:.2f})
  P&L:             ${self.om.daily_pnl:+.2f} ({self.om.daily_pnl/self.cfg.capital*100:+.1f}%)
  Peak:            ${self.om.peak:.2f}
  Max Drawdown:    {self.om.drawdown:.1%}
  
  Total Trades:    {len(self.om.trades)}
  Wins:            {len(wins)}
  Losses:          {len(losses)}
  Win Rate:        {wr:.0%}
  Avg Hold:        {avg_hold:.0f}s
  
  Avg Win:         ${sum(t.pnl for t in wins)/max(1,len(wins)):+.3f}
  Avg Loss:        ${sum(t.pnl for t in losses)/max(1,len(losses)):-.3f}
  Profit Factor:   {sum(t.pnl for t in wins)/max(0.001, abs(sum(t.pnl for t in losses))):.2f}
{'='*60}""")

        if self.om.trades:
            LOG.info("  Recent trades:")
            for t in self.om.trades[-10:]:
                emoji = "🟢" if t.pnl > 0 else "🔴"
                LOG.info(f"    {emoji} {t.slug[:35]:<35} {t.hold_sec:>4.0f}s | ${t.pnl:+.3f} | {t.reason}")

        # Session learning summary
        LOG.info(self.brain.session_report())
        self.brain.save()


# ══════════════════════════════════════════════════════════════
# SCAN MODE — brain-informed
# ══════════════════════════════════════════════════════════════

async def cmd_scan(cfg: Config, brain: Brain):
    markets = await discover_markets(cfg, brain)

    print(f"\n{'='*70}")
    print(f"  SCALPING TARGETS — {len(markets)} markets (brain-filtered)")
    print(f"  Filter: fee-free | spread≥3¢ | liq≥$2K | vol≥$1K | price 5-95¢")
    print(f"{'='*70}\n")

    for i, m in enumerate(markets):
        mid = (m.best_bid + m.best_ask) / 2
        buy_at = round(mid - max(0.005, m.spread/2 - 0.02), 4)
        sell_at = round(mid + 0.005, 4)
        profit_per = (sell_at - buy_at) * (cfg.per_order / buy_at)

        brain_tags = []
        if brain.is_star_market(m.slug):
            brain_tags.append("⭐STAR")
        risk = brain.get_market_risk(m.slug)
        if risk > 0.5:
            brain_tags.append(f"⚠️RISK={risk:.2f}")
        rep = brain.get_market_rep(m.slug)
        if rep["trades"] >= 3:
            brain_tags.append(f"📊{rep['trades']}trades/{rep['win_rate']:.0%}WR")
        tag_str = f" [{' '.join(brain_tags)}]" if brain_tags else ""

        print(f"  {i+1:>2}. {m.question[:55]}{tag_str}")
        print(f"      Spread: {m.spread*100:.1f}¢ | Bid: ${m.best_bid:.3f} | Ask: ${m.best_ask:.3f} | Mid: ${mid:.3f}")
        print(f"      Liq: ${m.liquidity:,.0f} | Vol: ${m.volume:,.0f} | Brain Score: {m._score:.3f}")
        print(f"      → Buy @ ${buy_at:.3f} | Sell @ ${sell_at:.3f} | Est profit: ${profit_per:.2f}/trade")
        print()

    if brain.data["avoid_list"]:
        avoided = await _get_avoided_market_names(brain)
        print(f"  🚫 Brain is avoiding {len(brain.data['avoid_list'])} markets:")
        for slug, reason in avoided[:5]:
            print(f"    • {slug[:50]} — {reason}")
        print()

    print(f"  {'─'*60}")
    print(f"  💰 With ${cfg.capital:.0f} capital:")
    print(f"     Exposed: ${cfg.capital * cfg.max_exposure_pct:.0f} ({cfg.max_exposure_pct:.0%})")
    print(f"     Reserve: ${cfg.capital * (1-cfg.max_exposure_pct):.0f}")
    print(f"     Per order: ${cfg.per_order:.0f} × {cfg.max_concurrent} concurrent")
    print(f"     Markets watched: {len(markets)}")
    print()


async def _get_avoided_market_names(brain: Brain) -> List[Tuple[str, str]]:
    results = []
    for slug in brain.data["avoid_list"]:
        rep = brain.get_market_rep(slug)
        reason = f"{rep['win_rate']:.0%} WR, risk={rep['risk_score']:.2f}"
        results.append((slug, reason))
    return results


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Polymarket Scalper (Brain-Powered)")
    parser.add_argument("--scan", action="store_true", help="Discover scalping targets (brain-informed)")
    parser.add_argument("--paper", action="store_true", help="Paper trade with learning")
    parser.add_argument("--live", action="store_true", help="Live trading (needs .env)")
    parser.add_argument("--brain", action="store_true", help="Show brain status & learned rules")
    parser.add_argument("--brain-reset", action="store_true", help="Wipe all learned data")
    parser.add_argument("--capital", type=float, default=None)
    parser.add_argument("--per-order", type=float, default=None)
    args = parser.parse_args()

    cfg = Config()
    if args.capital:
        cfg.capital = args.capital
    if args.per_order:
        cfg.per_order = args.per_order

    brain = Brain()

    if args.brain_reset:
        if os.path.exists(BRAIN_FILE):
            os.remove(BRAIN_FILE)
            print("🧠 Brain wiped clean. Starting fresh.")
        else:
            print("🧠 No brain file found — already clean.")
        return

    if args.brain:
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
