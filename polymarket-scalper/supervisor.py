#!/usr/bin/env python3
"""
AI Supervisor v2 for Polymarket Scalper — STRATEGY layer, not order-by-order gate.

Architecture (designed for speed):
  BEFORE trading:
    supervisor.py --precheck    Research all markets, write rules.jsonl
    bot_v4.py --supervised      Read rules.jsonl, trade at full speed

  DURING trading:
    bot_v4.py                   Executes orders immediately (no latency)
    supervisor.py --watch       Monitors in background, updates rules every 60s
                                Emergency intervention if market resolves/closes

  The supervisor NEVER gates individual orders. It sets:
    - Which markets are approved / blocked
    - Per-market max position size
    - Per-market exit triggers (resolution imminent, price extreme)
    - Global risk overrides (pause trading, reduce exposure)

Usage:
  python3 supervisor.py --precheck       # Run BEFORE starting bot
  python3 supervisor.py --watch          # Run DURING trading (background)
  python3 supervisor.py --status         # Show current rules
  python3 supervisor.py --emergency-stop # Immediately halt all trading
"""

import asyncio
import json
import os
import sys
import time
import logging
import argparse
import math
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set
from pathlib import Path

import aiohttp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
LOG = logging.getLogger("supervisor")

WORKSPACE = Path(__file__).parent
RULES_FILE = WORKSPACE / "rules.jsonl"
SUPERVISOR_LOG = WORKSPACE / "supervisor.log"
GAMMA_URL = "https://gamma-api.polymarket.com"


# ══════════════════════════════════════════════════════════════
# RULES — what the bot reads
# ══════════════════════════════════════════════════════════════

class Rules:
    """
    Supervisor rules that the bot reads at startup and periodically.
    Written as a single JSON file for simplicity.
    """

    def __init__(self):
        self.blocked_markets: Set[str] = set()
        self.approved_markets: Set[str] = set()
        self.market_limits: Dict[str, dict] = {}  # slug → {max_size, max_position, exit_price_floor, exit_price_cap}
        self.global_paused: bool = False
        self.global_max_exposure_pct: float = 0.0  # 0 = use bot default
        self.emergency_exits: List[str] = []  # slugs to force-exit immediately
        self.notes: List[str] = []
        self.updated: float = 0

    def to_dict(self) -> dict:
        return {
            "blocked_markets": list(self.blocked_markets),
            "approved_markets": list(self.approved_markets),
            "market_limits": self.market_limits,
            "global_paused": self.global_paused,
            "global_max_exposure_pct": self.global_max_exposure_pct,
            "emergency_exits": self.emergency_exits,
            "notes": self.notes,
            "updated": self.updated,
        }

    def save(self):
        self.updated = time.time()
        RULES_FILE.write_text(json.dumps(self.to_dict(), indent=2))
        LOG.info(f"📝 Rules saved: {len(self.approved_markets)} approved, "
                f"{len(self.blocked_markets)} blocked, "
                f"{len(self.market_limits)} limited")

    @classmethod
    def load(cls) -> "Rules":
        r = cls()
        if RULES_FILE.exists():
            try:
                data = json.loads(RULES_FILE.read_text())
                r.blocked_markets = set(data.get("blocked_markets", []))
                r.approved_markets = set(data.get("approved_markets", []))
                r.market_limits = data.get("market_limits", {})
                r.global_paused = data.get("global_paused", False)
                r.global_max_exposure_pct = data.get("global_max_exposure_pct", 0.0)
                r.emergency_exits = data.get("emergency_exits", [])
                r.notes = data.get("notes", [])
                r.updated = data.get("updated", 0)
            except Exception as e:
                LOG.warning(f"Rules load error: {e}")
        return r


# ══════════════════════════════════════════════════════════════
# MARKET ANALYZER — deep research on each market
# ══════════════════════════════════════════════════════════════

class MarketAnalyzer:
    """Fetches and analyzes market data from the Gamma API."""

    async def fetch_events(self, session: aiohttp.ClientSession) -> List[dict]:
        try:
            async with session.get(
                f"{GAMMA_URL}/events",
                params={"active": "true", "closed": "false", "limit": 500},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            LOG.error(f"Events fetch error: {e}")
        return []

    async def fetch_market_detail(self, session: aiohttp.ClientSession,
                                    slug: str) -> Optional[dict]:
        try:
            async with session.get(
                f"{GAMMA_URL}/markets",
                params={"slug": slug},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    markets = await resp.json()
                    return markets[0] if markets else None
        except Exception as e:
            LOG.debug(f"Market detail error for {slug}: {e}")
        return None

    def analyze_market(self, m: dict) -> dict:
        """Analyze a single market and return risk assessment."""
        slug = m.get("slug", "")
        question = m.get("question", "")

        try:
            prices = json.loads(m.get("outcomePrices", "[0.5,0.5]"))
            yes_price = float(prices[0])
            no_price = float(prices[1])
        except (json.JSONDecodeError, IndexError):
            yes_price = 0.5
            no_price = 0.5

        spread = float(m.get("spread", 0))
        volume = float(m.get("volume", 0))
        volume24 = float(m.get("volume24hr", 0))
        liquidity = float(m.get("liquidityClob", 0))
        closed = m.get("closed", False)
        accepting = m.get("acceptingOrders", True)
        fees = m.get("feesEnabled", False)
        end_date = m.get("endDateIso", "")

        analysis = {
            "slug": slug,
            "question": question,
            "yes_price": yes_price,
            "no_price": no_price,
            "spread": spread,
            "volume": volume,
            "volume24": volume24,
            "liquidity": liquidity,
            "closed": closed,
            "accepting": accepting,
            "fees": fees,
            "end_date": end_date,
            "hours_until_resolution": 9999,
            "decision": "approve",
            "reasons": [],
            "max_order_size_multiplier": 1.0,
            "exit_price_floor": 0.0,
            "exit_price_cap": 1.0,
        }

        # ─── Block conditions ────────────────────────────────

        if closed:
            analysis["decision"] = "block"
            analysis["reasons"].append("Market closed/resolved")
            return analysis

        if not accepting:
            analysis["decision"] = "block"
            analysis["reasons"].append("Not accepting orders")
            return analysis

        if fees:
            analysis["decision"] = "block"
            analysis["reasons"].append("Fees enabled — erodes spread capture")
            return analysis

        # ─── Resolution timing ───────────────────────────────

        if end_date:
            try:
                end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                hours = (end - datetime.now(timezone.utc)).total_seconds() / 3600
                analysis["hours_until_resolution"] = hours

                if hours < 1:
                    analysis["decision"] = "block"
                    analysis["reasons"].append(f"Resolves in {hours:.1f}h — too risky")
                    return analysis
                elif hours < 6:
                    analysis["decision"] = "limit"
                    analysis["reasons"].append(f"Resolves in {hours:.0f}h — halve position size")
                    analysis["max_order_size_multiplier"] = 0.5
                elif hours < 24:
                    analysis["reasons"].append(f"Resolves in {hours:.0f}h — caution")
                    analysis["max_order_size_multiplier"] = 0.75
            except Exception:
                pass

        # ─── Price extremes ──────────────────────────────────

        if yes_price > 0.92:
            analysis["decision"] = "limit"
            analysis["reasons"].append(f"YES at {yes_price:.0%} — near ceiling")
            analysis["max_order_size_multiplier"] = 0.25
            analysis["exit_price_cap"] = min(yes_price + 0.03, 0.99)
        elif yes_price < 0.08:
            analysis["decision"] = "limit"
            analysis["reasons"].append(f"YES at {yes_price:.0%} — near floor")
            analysis["max_order_size_multiplier"] = 0.25
            analysis["exit_price_floor"] = max(yes_price - 0.03, 0.01)

        # ─── Liquidity / spread ──────────────────────────────

        if liquidity < 2000:
            analysis["decision"] = "block"
            analysis["reasons"].append(f"Low liquidity: ${liquidity:,.0f}")
            return analysis

        if spread < 0.03:
            analysis["decision"] = "block"
            analysis["reasons"].append(f"Tight spread: {spread*100:.1f}¢ — no room to capture")
            return analysis

        if spread > 0.30:
            analysis["decision"] = "limit"
            analysis["reasons"].append(f"Very wide spread: {spread*100:.1f}¢ — slow fills")
            analysis["max_order_size_multiplier"] = 0.5

        # ─── Volume anomalies ────────────────────────────────

        if volume24 > 0 and volume > 0:
            ratio = volume24 / volume
            if ratio > 0.3:
                analysis["reasons"].append(f"High 24h activity ({ratio:.0%} of total vol)")
                # Could be news — don't block but note it

        # ─── Sports markets ──────────────────────────────────

        q_lower = question.lower()
        sports_words = ["game", "match", "win", "score", "quarter", "half",
                        "nfl", "nba", "mlb", "nhl", "soccer", "football",
                        "basketball", "baseball", "hockey", "ufc", "boxing"]
        if any(w in q_lower for w in sports_words):
            analysis["reasons"].append("Sports market — orders cancel at game start")
            analysis["max_order_size_multiplier"] = 0.75

        return analysis

    def compute_limits(self, analysis: dict) -> dict:
        """Convert analysis into per-market limits for the bot."""
        slug = analysis["slug"]
        multi = analysis.get("max_order_size_multiplier", 1.0)

        limits = {
            "max_order_size_multiplier": multi,
            "exit_price_floor": analysis.get("exit_price_floor", 0.0),
            "exit_price_cap": analysis.get("exit_price_cap", 1.0),
        }

        return limits


# ══════════════════════════════════════════════════════════════
# SUPERVISOR
# ══════════════════════════════════════════════════════════════

class Supervisor:
    def __init__(self):
        self.rules = Rules()
        self.analyzer = MarketAnalyzer()

    async def precheck(self):
        """
        Run BEFORE starting the bot. Researches all markets and writes rules.
        This is the slow, thorough analysis pass. Takes 10-30 seconds.
        """
        LOG.info("🔍 Precheck: analyzing all available markets...")
        LOG.info("=" * 60)

        async with aiohttp.ClientSession() as session:
            events = await self.analyzer.fetch_events(session)

        all_analyses = []
        for ev in events:
            for m in ev.get("markets", []):
                analysis = self.analyzer.analyze_market(m)
                all_analyses.append(analysis)

        # Sort by quality score
        scored = []
        for a in all_analyses:
            if a["decision"] == "block":
                self.rules.blocked_markets.add(a["slug"])
                continue

            # Quality score: spread * sqrt(liquidity)
            score = a["spread"] * math.sqrt(max(a["liquidity"], 1))
            a["_score"] = score
            scored.append(a)

        scored.sort(key=lambda x: x["_score"], reverse=True)

        for a in scored[:20]:  # Top 20
            slug = a["slug"]
            if a["decision"] == "approve":
                self.rules.approved_markets.add(slug)
            elif a["decision"] == "limit":
                self.rules.approved_markets.add(slug)
                self.rules.market_limits[slug] = self.analyzer.compute_limits(a)

            # Log
            emoji = {"approve": "✅", "limit": "⚠️", "block": "❌"}.get(a["decision"], "❓")
            LOG.info(f"{emoji} {a['question'][:55]}")
            LOG.info(f"   {a['spread']*100:.1f}¢ spread | ${a['liquidity']:,.0f} liq | "
                    f"${a['volume']:,.0f} vol | YES={a['yes_price']:.0%}")
            for r in a["reasons"]:
                LOG.info(f"   → {r}")
            if a["decision"] == "limit":
                LOG.info(f"   → Size multiplier: {a['max_order_size_multiplier']:.0%}")

        # Log blocked count
        LOG.info(f"\n{'='*60}")
        LOG.info(f"  PRECHECK COMPLETE")
        LOG.info(f"  Approved: {len(self.rules.approved_markets)} markets")
        LOG.info(f"  Blocked:  {len(self.rules.blocked_markets)} markets")
        LOG.info(f"  Limited:  {len(self.rules.market_limits)} markets with size limits")
        LOG.info(f"{'='*60}\n")

        self.rules.save()

    async def watch_loop(self, interval: int = 60):
        """
        Background watch during trading. Checks for changes every 60s.
        Only intervenes for emergencies (market closing, catastrophic price move).
        """
        LOG.info(f"👁 Supervisor watching (interval: {interval}s)")
        LOG.info(f"   Rules: {RULES_FILE}")

        while True:
            try:
                await self._watch_tick()
            except Exception as e:
                LOG.error(f"Watch error: {e}")
            await asyncio.sleep(interval)

    async def _watch_tick(self):
        """One watch cycle — check for changes that need intervention."""
        async with aiohttp.ClientSession() as session:
            # Only re-check markets that are currently approved
            for slug in list(self.rules.approved_markets):
                detail = await self.analyzer.fetch_market_detail(session, slug)
                if not detail:
                    continue

                analysis = self.analyzer.analyze_market(detail)

                # Emergency: market closing within 30 minutes
                if analysis["hours_until_resolution"] < 0.5:
                    if slug not in self.rules.emergency_exits:
                        LOG.warning(f"🚨 EMERGENCY EXIT | {slug[:40]} | "
                                   f"Resolves in {analysis['hours_until_resolution']:.1f}h")
                        self.rules.emergency_exits.append(slug)

                # Market closed since precheck
                if analysis["closed"] or not analysis["accepting"]:
                    LOG.warning(f"🚨 MARKET CLOSED | {slug[:40]}")
                    self.rules.emergency_exits.append(slug)
                    self.rules.approved_markets.discard(slug)
                    self.rules.blocked_markets.add(slug)

                # Price moved to extreme — update limits
                if analysis["decision"] == "limit":
                    self.rules.market_limits[slug] = self.analyzer.compute_limits(analysis)

                # Market now blocked for other reasons
                if analysis["decision"] == "block":
                    LOG.warning(f"⚠️ MARKET BLOCKED | {slug[:40]} | {analysis['reasons']}")
                    self.rules.approved_markets.discard(slug)
                    self.rules.blocked_markets.add(slug)

            self.rules.save()

    def emergency_stop(self):
        """Immediately halt all trading."""
        self.rules.global_paused = True
        self.rules.emergency_exits = list(self.rules.approved_markets)
        self.rules.notes.append(f"EMERGENCY STOP at {datetime.now(timezone.utc).isoformat()}")
        self.rules.save()
        LOG.warning("🛑 EMERGENCY STOP — all trading halted, all positions queued for exit")

    def show_status(self):
        r = self.rules
        print(f"\n{'='*60}")
        print(f"  👁 SUPERVISOR RULES")
        print(f"{'='*60}")
        print(f"  Global paused: {r.global_paused}")
        print(f"  Approved: {len(r.approved_markets)}")
        print(f"  Blocked:  {len(r.blocked_markets)}")
        print(f"  Limited:  {len(r.market_limits)}")
        print(f"  Emergency exits: {len(r.emergency_exits)}")

        if r.approved_markets:
            print(f"\n  ✅ APPROVED:")
            for slug in sorted(r.approved_markets)[:10]:
                limits = r.market_limits.get(slug, {})
                multi = limits.get("max_order_size_multiplier", 1.0)
                tag = f" (×{multi:.0%})" if multi < 1 else ""
                print(f"    • {slug[:50]}{tag}")

        if r.blocked_markets:
            print(f"\n  ❌ BLOCKED:")
            for slug in sorted(r.blocked_markets)[:10]:
                print(f"    • {slug[:50]}")

        if r.emergency_exits:
            print(f"\n  🚨 EMERGENCY EXITS:")
            for slug in r.emergency_exits:
                print(f"    • {slug[:50]}")

        if r.notes:
            print(f"\n  📝 NOTES:")
            for note in r.notes[-5:]:
                print(f"    • {note}")

        print(f"  Updated: {datetime.fromtimestamp(r.updated).strftime('%Y-%m-%d %H:%M:%S') if r.updated else 'never'}")
        print(f"{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(description="AI Supervisor for Polymarket Scalper v4")
    parser.add_argument("--precheck", action="store_true",
                       help="Research markets and write rules (run BEFORE bot)")
    parser.add_argument("--watch", action="store_true",
                       help="Monitor markets in background (run DURING trading)")
    parser.add_argument("--status", action="store_true", help="Show current rules")
    parser.add_argument("--emergency-stop", action="store_true",
                       help="Immediately halt all trading")
    parser.add_argument("--reset", action="store_true", help="Clear all rules")
    parser.add_argument("--interval", type=int, default=60,
                       help="Watch interval in seconds (default: 60)")
    args = parser.parse_args()

    sup = Supervisor()

    if args.reset:
        if RULES_FILE.exists():
            RULES_FILE.unlink()
        print("🗑 Rules cleared.")
        return

    if args.emergency_stop:
        sup.emergency_stop()
        return

    if args.precheck:
        asyncio.run(sup.precheck())
    elif args.watch:
        asyncio.run(sup.watch_loop(args.interval))
    elif args.status:
        sup.show_status()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
