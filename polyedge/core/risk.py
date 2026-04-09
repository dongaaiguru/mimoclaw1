"""Risk manager — circuit breaker, position sizing, trade tracking."""

import time
import logging
from typing import Tuple

from . import Config

log = logging.getLogger("polyedge.risk")


class RiskManager:
    """Enforces risk limits and tracks P&L."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.capital = cfg.capital
        self.peak = cfg.capital
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.day_start = time.time()
        self.tripped = False
        self.trade_log = []

    @property
    def drawdown(self) -> float:
        return (self.peak - self.capital) / max(self.peak, 0.01)

    @property
    def daily_return(self) -> float:
        return self.daily_pnl / self.cfg.capital

    def approve(self, size: float) -> Tuple[bool, str]:
        """Gate all orders through risk checks."""
        if self.tripped:
            return False, "CIRCUIT_BREAKER"
        if self.drawdown >= self.cfg.max_drawdown:
            self.tripped = True
            log.warning(f"🛑 CIRCUIT BREAKER TRIPPED: {self.drawdown:.0%} drawdown")
            return False, f"DRAWDOWN_{self.drawdown:.0%}"
        if self.daily_trades >= self.cfg.max_daily_trades:
            return False, "DAILY_LIMIT"
        if size > self.capital * 0.3:
            return False, "POSITION_TOO_LARGE"
        if size > self.capital * 0.5:
            return False, "RESERVE_PROTECTION"
        if size < self.cfg.min_order:
            return False, "BELOW_MIN"
        return True, "OK"

    def kelly_size(self, win_prob: float, price: float) -> float:
        """Kelly-criterion position sizing (25% fraction)."""
        if win_prob <= price or price <= 0 or price >= 1:
            return 0
        edge = win_prob - price
        kelly = edge / (1 - price)
        return kelly * self.cfg.kelly_fraction * self.capital

    def record(self, pnl: float, details: str = ""):
        """Record a completed trade."""
        self.daily_pnl += pnl
        self.daily_trades += 1
        self.capital += pnl
        self.peak = max(self.peak, self.capital)
        self.trade_log.append({
            "time": time.time(),
            "pnl": pnl,
            "capital": self.capital,
            "details": details,
        })

    def reset_daily(self):
        """Reset daily counters at midnight."""
        if time.time() - self.day_start > 86400:
            self.daily_trades = 0
            self.daily_pnl = 0.0
            self.day_start = time.time()
            self.tripped = False
            log.info("📅 Daily counters reset")

    def status(self) -> dict:
        return {
            "capital": f"${self.capital:.2f}",
            "peak": f"${self.peak:.2f}",
            "drawdown": f"{self.drawdown:.1%}",
            "daily_pnl": f"${self.daily_pnl:.2f} ({self.daily_return:.1%})",
            "daily_trades": self.daily_trades,
            "circuit_breaker": self.tripped,
        }
