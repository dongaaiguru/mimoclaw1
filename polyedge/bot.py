"""Main bot — orchestrates strategies, risk, and order placement."""

import json
import time
import hmac
import asyncio
import logging
from typing import List

import aiohttp

from .core import Config, Signal
from .core.api import PolymarketAPI
from .core.risk import RiskManager
from .strategies import fee_free_spread, liquidity_rewards, dependency_arb, momentum

log = logging.getLogger("polyedge.bot")


class PolyEdgeBot:
    """Multi-strategy Polymarket trading bot."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.api = PolymarketAPI(cfg)
        self.risk = RiskManager(cfg)
        self.cycle = 0

    async def run(self, mode: str):
        """Run the bot in scan/paper/live mode."""
        mode_name = {"scan": "SCAN", "paper": "PAPER", "live": "LIVE"}[mode]
        log.info(f"{'='*70}")
        log.info(f"  PolyEdge Bot v2 | ${self.cfg.capital:.0f} | {mode_name}")
        log.info(f"  Target: ${self.cfg.daily_target:.2f}/day ({self.cfg.daily_target/self.cfg.capital:.0%})")
        log.info(f"  Fee-Free: {self.cfg.fee_free_spread_pct:.0%} | Rewards: {self.cfg.rewards_pct:.0%} | "
                f"Arb: {self.cfg.arb_pct:.0%} | News: {self.cfg.news_pct:.0%}")
        log.info(f"{'='*70}")

        await self.api.open()

        try:
            if mode == "scan":
                await self._run_cycle(mode)
            else:
                while True:
                    self.cycle += 1
                    await self._run_cycle(mode)
                    self.risk.reset_daily()
                    log.info(f"\n⏳ Next cycle in {self.cfg.cycle_interval}s... "
                            f"(#{self.cycle} | {self.risk.status()['daily_pnl']})")
                    await asyncio.sleep(self.cfg.cycle_interval)
        except KeyboardInterrupt:
            log.info("Shutting down...")
        finally:
            await self._shutdown()

    async def _run_cycle(self, mode: str):
        """One full bot cycle."""
        log.info(f"\n{'─'*70}")
        log.info(f"  CYCLE #{self.cycle} | {time.strftime('%H:%M:%S')} | {self.risk.status()['capital']}")
        log.info(f"{'─'*70}")

        # Fetch markets
        log.info("📡 Fetching markets...")
        events = await self.api.get_events()
        markets = self.api.parse_markets(events)
        log.info(f"   {len(markets)} active markets")

        all_signals = []

        # Strategy 1: Fee-free spread
        log.info(f"\n📗 FEE-FREE SPREAD CAPTURE")
        ff_signals = fee_free_spread.scan(markets, self.cfg)
        if ff_signals:
            for s in ff_signals:
                log.info(f"   {s.action:4s} ${s.size:.0f} @ {s.price:.3f} | {s.edge:.0f}¢ | {s.market[:50]}")
            all_signals.extend(ff_signals)
        else:
            log.info("   No opportunities")

        # Strategy 2: Liquidity rewards
        log.info(f"\n🏆 LIQUIDITY REWARDS")
        reward_markets = [m for m in markets if m.has_rewards]
        log.info(f"   {len(reward_markets)} markets with active rewards")
        rw_signals = liquidity_rewards.scan(markets, self.cfg)
        if rw_signals:
            for s in rw_signals:
                log.info(f"   {s.action:4s} ${s.size:.0f} @ {s.price:.3f} | {s.market[:50]}")
            all_signals.extend(rw_signals)

        # Top reward pools
        top_rewards = sorted(reward_markets, key=lambda m: m.reward_pool, reverse=True)[:5]
        if top_rewards:
            log.info(f"   Top pools:")
            for m in top_rewards:
                if m.reward_pool > 0:
                    log.info(f"   ${m.reward_pool:>8,.0f} | {m.question[:50]}")

        # Strategy 3: Dependency arb
        log.info(f"\n🔗 DEPENDENCY ARBITRAGE")
        arb_signals = dependency_arb.scan(markets, self.cfg)
        if arb_signals:
            for s in arb_signals:
                log.info(f"   {s.action:4s} ${s.size:.0f} @ {s.price:.3f} | {s.edge:.1f}% | {s.market[:50]}")
            all_signals.extend(arb_signals)
        else:
            log.info("   No dependency arbs")

        # Strategy 4: Momentum
        log.info(f"\n🚀 NEWS MOMENTUM")
        mom_signals = momentum.scan(markets, self.cfg)
        if mom_signals:
            for s in mom_signals:
                log.info(f"   {s.action:4s} ${s.size:.0f} @ {s.price:.3f} | {s.market[:50]}")
            all_signals.extend(mom_signals)
        else:
            log.info("   No momentum opportunities")

        # Execute
        if mode in ("paper", "live") and all_signals:
            log.info(f"\n📤 EXECUTING {len(all_signals)} ORDERS")
            await self._execute(all_signals, mode)

        # Summary
        self._print_summary(ff_signals, rw_signals, arb_signals, mom_signals)

    async def _execute(self, signals: List[Signal], mode: str):
        """Execute signals through the API."""
        for s in signals:
            ok, reason = self.risk.approve(s.size)
            if not ok:
                log.debug(f"  Skip: {reason}")
                continue

            result = await self.api.place_order("", s.action, s.price, s.size)
            if result.get("success"):
                log.info(f"  ✅ {s.action} ${s.size:.2f} @ {s.price:.3f} | {s.market[:40]}")
            else:
                log.error(f"  ❌ {s.action} ${s.size:.2f} @ {s.price:.3f} | {result.get('error', 'unknown')}")

    def _print_summary(self, ff, rw, arb, mom):
        """Print daily income estimate."""
        log.info(f"\n{'='*70}")
        log.info("  💰 ESTIMATED DAILY INCOME")
        log.info(f"{'='*70}")

        ff_income = sum(s.edge * 0.01 * s.size * 0.3 for s in ff[:6])
        rw_income = len(rw) * 0.10
        arb_income = sum(s.edge * 0.01 * s.size * 0.2 for s in arb[:4])
        mom_income = sum(s.edge * 0.01 * s.size * 0.15 for s in mom[:2])

        total = ff_income + rw_income + arb_income + mom_income
        target = self.cfg.daily_target

        log.info(f"  Spread capture:  ${ff_income:.2f}")
        log.info(f"  Rewards/rebates: ${rw_income:.2f}")
        log.info(f"  Dependency arb:  ${arb_income:.2f}")
        log.info(f"  News momentum:   ${mom_income:.2f}")
        log.info(f"  TOTAL:           ${total:.2f}/day")
        log.info(f"  Target:          ${target:.2f}/day")

        if total >= target:
            log.info(f"  ✅ TARGET ACHIEVABLE")
        else:
            log.info(f"  ⚠️  Gap: ${target - total:.2f}")

        log.info(f"\n  Status: {json.dumps(self.risk.status(), indent=2)}")
        log.info(f"{'='*70}\n")

    async def _shutdown(self):
        """Clean shutdown."""
        await self.api.close()
        log.info(f"\n{'='*70}")
        log.info(f"  FINAL STATUS")
        log.info(f"{'='*70}")
        for k, v in self.risk.status().items():
            log.info(f"  {k}: {v}")
        log.info(f"  Trades: {len(self.risk.trade_log)}")
        log.info(f"{'='*70}")
