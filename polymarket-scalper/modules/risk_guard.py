"""
Risk Guard v5 — Comprehensive risk management layer.

Fills the gaps the original bot missed:
1. Resolution time filtering (block markets <4h from resolution)
2. Losing streak detection (pause after N consecutive losses)
3. Daily PnL target (wind down after hitting daily goal)
4. Drawdown-based size reduction (not binary circuit breaker)
5. Daily loss limit (stop after losing X% in one day)
6. Cooldown after forced exits (don't immediately re-enter)

NOTE: Quiet hours removed — bot runs 24/7.
"""

import time
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone
from dataclasses import dataclass, field

LOG = logging.getLogger("scalper.risk")


@dataclass
class TradeRecord:
    """Minimal trade record for risk tracking."""
    timestamp: float
    slug: str
    pnl: float
    reason: str  # "filled", "stop_loss", "timeout", etc.


class RiskGuard:
    """
    Centralized risk management that wraps all protective logic.
    
    The bot asks RiskGuard before every action:
    - can_trade() — before placing new orders
    - can_trade_market(slug) — before trading a specific market
    - should_wind_down() — when approaching daily targets
    - record_trade() — after every trade to update risk state
    """

    def __init__(self, config: dict):
        """
        config keys:
        - quiet_hours_start: int (UTC hour, default 3)
        - quiet_hours_end: int (UTC hour, default 6)
        - max_daily_loss_pct: float (default 0.08 = 8%)
        - daily_profit_target_pct: float (default 0.10 = 10%)
        - losing_streak_limit: int (default 3)
        - cooldown_after_forced_exit: int (seconds, default 120)
        - min_resolution_hours: float (default 4.0)
        """
        self.quiet_start = config.get("quiet_hours_start", 3)
        self.quiet_end = config.get("quiet_hours_end", 6)
        self.max_daily_loss_pct = config.get("max_daily_loss_pct", 0.08)
        self.daily_profit_target_pct = config.get("daily_profit_target_pct", 0.10)
        self.losing_streak_limit = config.get("losing_streak_limit", 3)
        self.cooldown_seconds = config.get("cooldown_after_forced_exit", 120)
        self.min_resolution_hours = config.get("min_resolution_hours", 4.0)

        # State
        self._trades_today: List[TradeRecord] = []
        self._daily_pnl = 0.0
        self._consecutive_losses = 0
        self._last_forced_exit = 0.0
        self._paused = False
        self._pause_reason = ""
        self._wind_down = False  # approaching daily target
        self._last_reset_date = ""

    def reset_daily(self):
        """Reset daily counters. Call at start of each trading day."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._last_reset_date:
            self._trades_today = []
            self._daily_pnl = 0.0
            self._consecutive_losses = 0
            self._paused = False
            self._pause_reason = ""
            self._wind_down = False
            self._last_reset_date = today
            LOG.info(f"🛡️ Risk guard reset for {today}")

    # ─── Pre-Trade Checks ───────────────────────────────────

    def can_trade(self, capital: float, effective_capital: float) -> Tuple[bool, str]:
        """
        Master check: can we place any new trade right now?
        
        Returns (allowed, reason).
        """
        self.reset_daily()

        # 1. Manual pause
        if self._paused:
            return False, f"PAUSED: {self._pause_reason}"

        # 2. Daily loss limit (quiet hours removed — bot runs 24/7)
        daily_loss_pct = abs(min(0, self._daily_pnl)) / max(capital, 1)
        if daily_loss_pct >= self.max_daily_loss_pct:
            self._paused = True
            self._pause_reason = f"Daily loss limit hit ({daily_loss_pct:.1%})"
            LOG.warning(f"🛑 DAILY LOSS LIMIT | {self._pause_reason}")
            return False, self._pause_reason

        # 3. Losing streak
        if self._consecutive_losses >= self.losing_streak_limit:
            # Auto-recover after 10 minutes
            time_since_last = time.time() - self._trades_today[-1].timestamp if self._trades_today else 999
            if time_since_last < 600:
                return False, f"LOSING_STREAK ({self._consecutive_losses}L, cooling 10min)"
            else:
                LOG.info("🛡️ Losing streak cooldown expired, resuming")
                self._consecutive_losses = 0

        # 4. Cooldown after forced exit
        if self._last_forced_exit > 0:
            elapsed = time.time() - self._last_forced_exit
            if elapsed < self.cooldown_seconds:
                return False, f"COOLDOWN ({self.cooldown_seconds - elapsed:.0f}s remaining)"

        return True, "OK"

    def can_trade_market(self, slug: str, hours_until_resolution: float,
                          news_affected: bool) -> Tuple[bool, str]:
        """
        Check if a specific market is safe to trade.
        """
        # Resolution time filter
        if hours_until_resolution < self.min_resolution_hours:
            return False, f"RESOLVING_SOON ({hours_until_resolution:.1f}h < {self.min_resolution_hours}h)"

        # News filter
        if news_affected:
            return False, "NEWS_AFFECTED"

        return True, "OK"

    def should_wind_down(self, daily_pnl: float, capital: float) -> bool:
        """
        Check if we should start winding down (reducing activity).
        
        Triggered when daily profit approaches target.
        """
        profit_pct = daily_pnl / max(capital, 1)

        if profit_pct >= self.daily_profit_target_pct:
            if not self._wind_down:
                self._wind_down = True
                LOG.info(f"🎯 DAILY TARGET HIT | {profit_pct:.1%} profit | winding down")
            return True

        return False

    def get_size_multiplier(self, daily_pnl: float, capital: float) -> float:
        """
        Dynamic size multiplier based on daily performance.
        
        - Losing day: reduce size progressively
        - Approaching target: reduce size to protect gains
        - Normal: 1.0x
        """
        profit_pct = daily_pnl / max(capital, 1)

        if profit_pct <= -0.05:
            return 0.5  # down 5%+ → half size
        elif profit_pct <= -0.03:
            return 0.7  # down 3-5% → 70% size
        elif self._wind_down:
            return 0.3  # hit target → 30% size (protect gains)
        elif profit_pct >= 0.05:
            return 0.8  # up 5%+ → slightly reduced (don't get greedy)
        else:
            return 1.0  # normal

    # ─── Post-Trade Updates ─────────────────────────────────

    def record_trade(self, pnl: float, slug: str, reason: str = "filled"):
        """Record a completed trade for risk tracking."""
        trade = TradeRecord(
            timestamp=time.time(),
            slug=slug,
            pnl=pnl,
            reason=reason,
        )
        self._trades_today.append(trade)
        self._daily_pnl += pnl

        if pnl > 0:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1

        # Log streak
        if self._consecutive_losses >= 2:
            LOG.warning(f"⚠️ Losing streak: {self._consecutive_losses} consecutive losses")

    def record_forced_exit(self, slug: str, reason: str):
        """Record a forced exit (stop loss, news, timeout)."""
        self._last_forced_exit = time.time()
        LOG.info(f"🛡️ Forced exit cooldown started ({self.cooldown_seconds}s) | {slug[:35]} | {reason}")

    # ─── Internal Checks ────────────────────────────────────

    def _is_quiet_hours(self) -> bool:
        """Quiet hours disabled — bot runs 24/7."""
        return False

    # ─── Manual Controls ────────────────────────────────────

    def pause(self, reason: str = "manual"):
        """Manually pause trading."""
        self._paused = True
        self._pause_reason = reason
        LOG.warning(f"🛑 TRADING PAUSED | {reason}")

    def resume(self):
        """Manually resume trading."""
        self._paused = False
        self._pause_reason = ""
        self._consecutive_losses = 0
        LOG.info("🛡️ Trading resumed")

    # ─── Report ─────────────────────────────────────────────

    def report(self, capital: float) -> str:
        """Human-readable risk report."""
        self.reset_daily()
        wins = sum(1 for t in self._trades_today if t.pnl > 0)
        losses = sum(1 for t in self._trades_today if t.pnl <= 0)
        can, reason = self.can_trade(capital, capital)
        daily_loss_pct = abs(min(0, self._daily_pnl)) / max(capital, 1)

        lines = [
            f"\n🛡️ RISK GUARD",
            f"  Can trade:     {'✅' if can else '❌'} ({reason})",
            f"  Quiet hours:   {'🔇 YES' if self._is_quiet_hours() else '🔊 NO'} ({self.quiet_start}:00-{self.quiet_end}:00 UTC)",
            f"  Daily PnL:     ${self._daily_pnl:+.2f} ({self._daily_pnl/capital*100:+.1f}%)",
            f"  Daily trades:  {len(self._trades_today)} ({wins}W/{losses}L)",
            f"  Daily loss:    {daily_loss_pct:.1%} / {self.max_daily_loss_pct:.0%} limit",
            f"  Loss streak:   {self._consecutive_losses} / {self.losing_streak_limit} limit",
            f"  Wind down:     {'🎯 YES' if self._wind_down else 'NO'} (target: {self.daily_profit_target_pct:.0%})",
            f"  Size mult:     {self.get_size_multiplier(self._daily_pnl, capital):.2f}x",
            f"  Paused:        {'⏸️ ' + self._pause_reason if self._paused else 'NO'}",
        ]
        return "\n".join(lines)
