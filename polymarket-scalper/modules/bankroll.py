"""
Bankroll Manager v5 — Dynamic capital tracking with compounding and deposit awareness.

The original bot uses a fixed CAPITAL from .env. It doesn't know:
- Your actual balance grew from winning trades
- You deposited more money
- You withdrew profits
- Your effective capital should be different from starting capital

This module tracks REAL bankroll and makes the bot compound correctly.

Key behaviors:
1. Starting balance = $100 (from config)
2. After winning $20, effective capital = $120 → bet sizes grow 20%
3. If you deposit another $100, effective capital = $220 → bet sizes double
4. If you withdraw $50, effective capital shrinks → bet sizes shrink
5. Kelly sizing uses CURRENT bankroll, not starting capital
6. Rolling drawdown protects accumulated gains, not just original capital
"""

import json
import time
import logging
from pathlib import Path
from typing import Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timezone

LOG = logging.getLogger("scalper.bankroll")

BANKROLL_FILE = "bankroll.json"


@dataclass
class BankrollSnapshot:
    """A point-in-time bankroll snapshot."""
    timestamp: float
    starting_capital: float
    realized_pnl: float
    unrealized_pnl: float
    deposits: float
    withdrawals: float
    effective_capital: float  # starting + pnl + deposits - withdrawals
    peak_capital: float
    drawdown_from_peak: float  # 0-1


class BankrollManager:
    """
    Dynamic bankroll tracker with compounding.
    
    File format (bankroll.json):
    {
        "starting_capital": 100,
        "deposits": [ {"ts": 123, "amount": 100, "note": "user deposit"} ],
        "withdrawals": [ {"ts": 456, "amount": 50, "note": "profit withdrawal"} ],
        "peak_capital": 150,
        "daily_pnl": { "2026-04-10": 12.50, ... },
        "sessions": 5
    }
    """

    def __init__(self, starting_capital: float = 100.0):
        self.starting_capital = starting_capital
        self._deposits: list = []
        self._withdrawals: list = []
        self._peak_capital = starting_capital
        self._daily_pnl: dict = {}
        self._sessions = 0
        self._load()

    def _load(self):
        """Load bankroll state from file."""
        if Path(BANKROLL_FILE).exists():
            try:
                data = json.loads(Path(BANKROLL_FILE).read_text())
                self.starting_capital = data.get("starting_capital", self.starting_capital)
                self._deposits = data.get("deposits", [])
                self._withdrawals = data.get("withdrawals", [])
                self._peak_capital = data.get("peak_capital", self.starting_capital)
                self._daily_pnl = data.get("daily_pnl", {})
                self._sessions = data.get("sessions", 0)
                LOG.info(f"💰 Bankroll loaded: ${self.effective_capital:.2f} effective capital")
            except Exception as e:
                LOG.warning(f"Bankroll load error: {e}")

    def save(self):
        """Save bankroll state to file."""
        data = {
            "starting_capital": self.starting_capital,
            "deposits": self._deposits,
            "withdrawals": self._withdrawals,
            "peak_capital": self._peak_capital,
            "daily_pnl": self._daily_pnl,
            "sessions": self._sessions,
            "last_updated": time.time(),
        }
        Path(BANKROLL_FILE).write_text(json.dumps(data, indent=2))

    def start_session(self):
        """Mark start of a new trading session."""
        self._sessions += 1
        self._peak_capital = max(self._peak_capital, self.effective_capital)
        self.save()
        LOG.info(f"💰 Session #{self._sessions} | Effective capital: ${self.effective_capital:.2f}")

    # ─── Core Properties ────────────────────────────────────

    @property
    def total_deposits(self) -> float:
        return sum(d["amount"] for d in self._deposits)

    @property
    def total_withdrawals(self) -> float:
        return sum(w["amount"] for w in self._withdrawals)

    @property
    def effective_capital(self) -> float:
        """
        Current effective capital = starting + deposits - withdrawals.
        Realized PnL is tracked separately and added by the bot.
        """
        return self.starting_capital + self.total_deposits - self.total_withdrawals

    def get_trading_capital(self, realized_pnl: float = 0) -> float:
        """
        The capital available for position sizing.
        = effective_capital + realized_pnl from current session
        """
        return self.effective_capital + realized_pnl

    # ─── Deposit / Withdrawal ───────────────────────────────

    def record_deposit(self, amount: float, note: str = ""):
        """Record a deposit (user added funds)."""
        self._deposits.append({
            "ts": time.time(),
            "amount": amount,
            "note": note,
        })
        new_capital = self.effective_capital
        self._peak_capital = max(self._peak_capital, new_capital)
        LOG.info(f"💰 DEPOSIT +${amount:.2f} | Effective capital: ${new_capital:.2f}")
        self.save()

    def record_withdrawal(self, amount: float, note: str = ""):
        """Record a withdrawal (user took funds out)."""
        self._withdrawals.append({
            "ts": time.time(),
            "amount": amount,
            "note": note,
        })
        new_capital = self.effective_capital
        LOG.info(f"💰 WITHDRAWAL -${amount:.2f} | Effective capital: ${new_capital:.2f}")
        self.save()

    # ─── Daily PnL Tracking ─────────────────────────────────

    def record_daily_pnl(self, pnl: float):
        """Add to today's PnL."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        current = self._daily_pnl.get(today, 0)
        self._daily_pnl[today] = current + pnl

    def get_today_pnl(self) -> float:
        """Get today's cumulative PnL."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self._daily_pnl.get(today, 0)

    def get_daily_pnl_history(self, days: int = 7) -> dict:
        """Get PnL for the last N days."""
        result = {}
        for i in range(days):
            date = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=i)).strftime("%Y-%m-%d")
            result[date] = self._daily_pnl.get(date, 0)
        return result

    # ─── Dynamic Sizing ─────────────────────────────────────

    def get_per_order_size(self, realized_pnl: float = 0,
                            base_per_order: float = 10) -> float:
        """
        Dynamic per-order size based on current bankroll.
        
        Scales proportionally with capital:
        - $100 capital → $8 per order
        - $150 capital → $12 per order (50% more)
        - $200 capital → $16 per order
        - $80 capital → $6.40 per order (shrunk)
        
        Clamped to 5-20% of capital for safety.
        """
        capital = self.get_trading_capital(realized_pnl)
        
        # Scale from the original base
        scale_factor = capital / self.starting_capital
        size = base_per_order * scale_factor

        # Clamp: 5% min, 15% max of current capital
        min_size = capital * 0.05
        max_size = capital * 0.15
        size = max(min_size, min(max_size, size))

        return round(size, 2)

    def get_max_concurrent(self, realized_pnl: float = 0) -> int:
        """
        Dynamic max concurrent positions based on capital.
        
        More capital = more concurrent positions.
        $100 → 5 positions
        $200 → 7 positions
        $500 → 10 positions
        """
        capital = self.get_trading_capital(realized_pnl)
        if capital < 50:
            return 3
        elif capital < 100:
            return 4
        elif capital < 200:
            return 5
        elif capital < 500:
            return 7
        else:
            return 10

    # ─── Drawdown (Rolling) ─────────────────────────────────

    def update_peak(self, realized_pnl: float):
        """Update peak capital for drawdown calculation."""
        current = self.get_trading_capital(realized_pnl)
        self._peak_capital = max(self._peak_capital, current)

    def get_drawdown(self, realized_pnl: float) -> float:
        """
        Rolling drawdown from peak capital.
        
        This protects ACCUMULATED gains, not just starting capital.
        If you grew $100 to $200 and then lost $30, drawdown = 15%
        (from peak $200), not 30% (from starting $100).
        """
        current = self.get_trading_capital(realized_pnl)
        self._peak_capital = max(self._peak_capital, current)
        if self._peak_capital <= 0:
            return 1.0
        return max(0, (self._peak_capital - current) / self._peak_capital)

    def get_circuit_breaker_pct(self, realized_pnl: float) -> float:
        """
        Dynamic circuit breaker based on gains.
        
        - Original capital at risk: 10% drawdown stops trading
        - After $20+ profit: protect gains, tighter 8% from peak
        - After $50+ profit: very tight 5% from peak
        
        The more you've made, the less you're willing to lose.
        """
        capital = self.get_trading_capital(realized_pnl)
        gains = capital - self.starting_capital

        if gains <= 0:
            # Still at or below starting — standard 10%
            return 0.10
        elif gains < 20:
            # Small gains — 10% from peak
            return 0.10
        elif gains < 50:
            # Moderate gains — protect them, 8% from peak
            return 0.08
        else:
            # Large gains — very protective, 5% from peak
            return 0.05

    # ─── Position Sizing Multipliers ────────────────────────

    def get_growth_multiplier(self, realized_pnl: float) -> float:
        """
        How much to scale position sizes based on account growth.
        
        $100 → $120: multiplier = 1.2 (20% bigger bets)
        $100 → $100: multiplier = 1.0 (same)
        $100 → $80:  multiplier = 0.8 (20% smaller bets)
        
        This IS the compounding mechanism.
        """
        capital = self.get_trading_capital(realized_pnl)
        return capital / self.starting_capital

    def get_shrink_multiplier(self, realized_pnl: float) -> float:
        """
        Size reduction when losing.
        
        Down 5% → multiply sizes by 0.75
        Down 10% → multiply sizes by 0.5
        Down 15% → multiply sizes by 0.25 (survival mode)
        """
        capital = self.get_trading_capital(realized_pnl)
        loss_pct = max(0, (self.starting_capital - capital) / self.starting_capital)

        if loss_pct <= 0.05:
            return 1.0  # small loss, no reduction
        elif loss_pct <= 0.10:
            return 0.75  # moderate loss
        elif loss_pct <= 0.15:
            return 0.50  # serious loss
        else:
            return 0.25  # survival mode

    def get_combined_multiplier(self, realized_pnl: float) -> float:
        """
        Final multiplier combining growth and shrink logic.
        """
        growth = self.get_growth_multiplier(realized_pnl)
        shrink = self.get_shrink_multiplier(realized_pnl)

        if growth >= 1.0:
            return growth  # compounding up
        else:
            return shrink  # shrinking down

    # ─── Session Reports ────────────────────────────────────

    def report(self, realized_pnl: float = 0) -> str:
        """Human-readable bankroll report."""
        capital = self.get_trading_capital(realized_pnl)
        gains = capital - self.starting_capital
        gain_pct = gains / self.starting_capital * 100 if self.starting_capital > 0 else 0
        dd = self.get_drawdown(realized_pnl)
        today = self.get_today_pnl()

        lines = [
            f"\n💰 BANKROLL",
            f"  Starting:     ${self.starting_capital:.2f}",
            f"  Deposits:     +${self.total_deposits:.2f} ({len(self._deposits)} deposits)",
            f"  Withdrawals:  -${self.total_withdrawals:.2f} ({len(self._withdrawals)} withdrawals)",
            f"  Realized PnL: ${realized_pnl:+.2f}",
            f"  Effective:    ${capital:.2f} ({gain_pct:+.1f}%)",
            f"  Peak:         ${self._peak_capital:.2f}",
            f"  Drawdown:     {dd:.1%}",
            f"  Today PnL:    ${today:+.2f}",
            f"  Growth mult:  {self.get_combined_multiplier(realized_pnl):.2f}x",
            f"  Per-order:    ${self.get_per_order_size(realized_pnl):.2f}",
            f"  Max positions:{self.get_max_concurrent(realized_pnl)}",
            f"  Circuit brk:  {self.get_circuit_breaker_pct(realized_pnl):.0%}",
            f"  Sessions:     {self._sessions}",
        ]
        return "\n".join(lines)
