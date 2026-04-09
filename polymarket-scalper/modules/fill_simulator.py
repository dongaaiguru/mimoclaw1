"""
Fill Simulator v5 — Adverse selection-aware paper fill model.

The v4 paper fill model was too optimistic: it assumed fills are always
favorable. In reality, fills come FASTER when price is moving AGAINST you
(adverse selection) and SLOWER when price is in your favor.

This simulator models:
1. Adverse selection — fills are correlated with price momentum against you
2. Queue position — realistic queue mechanics with partial fills
3. Spread dynamics — wider spreads = fewer takers = slower fills
4. Volume regime — quiet markets fill differently than active ones
5. Time-of-day — US market hours have more flow
6. Informed flow — detect when someone with info is trading

Key insight: In prediction markets, "informed traders" are people who know
something you don't. When they BUY aggressively, your SELL gets filled
(bad for you — they know YES will win). When they SELL aggressively,
your BUY gets filled (bad for you — they know YES will lose).
"""

import math
import random
import time
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

LOG = logging.getLogger("scalper.fill_sim")


@dataclass
class FlowState:
    """Tracks order flow state for adverse selection modeling."""
    # Recent trades: (timestamp, price, size, side)
    trades: List[Tuple[float, float, float, str]] = field(default_factory=list)
    # Cumulative flow imbalance (-1 to +1, positive = buy pressure)
    imbalance: float = 0.0
    # Rolling volatility (std dev of price changes)
    volatility: float = 0.0
    # Informed flow estimate (Kyle's lambda proxy)
    informed_flow: float = 0.0
    # Last update
    last_update: float = 0.0


class FillSimulator:
    """
    Adverse selection-aware fill simulator for paper trading.
    
    Philosophy: The PAPER environment should be HARDER than live.
    If you're profitable in adversarial paper mode, you'll likely
    be profitable live (where fills are somewhat better).
    """

    # ─── Configuration ───────────────────────────────────────

    # Adverse selection strength: how much flow imbalance affects fill probability
    # Higher = more realistic but more punishing
    ADVERSE_SELECTION_STRENGTH = 0.4  # 0 = none, 1 = extreme

    # Minimum resting time before fills can occur (seconds)
    MIN_REST_TIME = 3.0

    # Base fill rate per tick at best level
    BASE_FILL_RATE_AT_BEST = 0.025   # 2.5% per second tick
    BASE_FILL_RATE_INSIDE = 0.050    # 5.0% per second tick
    BASE_FILL_RATE_BEHIND = 0.003    # 0.3% per second tick

    # Partial fill probability (when fill occurs, chance it's partial)
    PARTIAL_FILL_PROB = 0.25
    PARTIAL_FILL_MIN = 0.30  # minimum fill percentage
    PARTIAL_FILL_MAX = 0.85  # maximum fill percentage

    def __init__(self):
        # token_id → FlowState
        self._flow: Dict[str, FlowState] = {}
        # Track fill outcomes for calibration
        self._fill_stats = {
            "total_attempts": 0,
            "total_fills": 0,
            "adverse_fills": 0,  # fills when price moving against us
            "favorable_fills": 0,
            "partial_fills": 0,
            "rejected_fills": 0,  # post-only rejections
        }

    def record_trade(self, token: str, price: float, size: float, side: str):
        """Record a market trade for flow analysis."""
        now = time.time()
        state = self._flow.setdefault(token, FlowState())
        state.trades.append((now, price, size, side))

        # Keep only last 5 minutes
        cutoff = now - 300
        state.trades = [t for t in state.trades if t[0] > cutoff]
        state.last_update = now

        # Update imbalance
        self._update_flow_state(token)

    def _update_flow_state(self, token: str):
        """Update flow metrics for a token."""
        state = self._flow.get(token)
        if not state or len(state.trades) < 2:
            return

        trades = state.trades
        now = time.time()

        # Imbalance over last 60 seconds
        recent = [t for t in trades if t[0] > now - 60]
        if recent:
            buy_vol = sum(t[2] for t in recent if t[3] == "BUY")
            sell_vol = sum(t[2] for t in recent if t[3] == "SELL")
            total = buy_vol + sell_vol
            if total > 0:
                state.imbalance = (buy_vol - sell_vol) / total

        # Volatility (std dev of price changes over 5 min)
        if len(trades) >= 3:
            prices = [t[1] for t in trades]
            changes = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
            if changes:
                mean_change = sum(changes) / len(changes)
                variance = sum((c - mean_change) ** 2 for c in changes) / len(changes)
                state.volatility = math.sqrt(variance)

        # Informed flow proxy: large trades in one direction
        large_trades = [t for t in recent if t[2] > 50]  # > $50 trades
        if large_trades:
            large_buy = sum(t[2] for t in large_trades if t[3] == "BUY")
            large_sell = sum(t[2] for t in large_trades if t[3] == "SELL")
            large_total = large_buy + large_sell
            if large_total > 0:
                state.informed_flow = (large_buy - large_sell) / large_total

    def get_adverse_selection_score(self, token: str, order_side: str) -> float:
        """
        Calculate adverse selection score for an order.
        
        For BUY orders: adverse when buy_pressure is high (we're buying into strength)
        For SELL orders: adverse when sell_pressure is high (we're selling into weakness)
        
        Returns 0.0 (no adverse selection) to 1.0 (extreme adverse selection).
        """
        state = self._flow.get(token)
        if not state:
            return 0.0

        if order_side == "BUY":
            # Adverse if we're buying when others are buying aggressively
            # (we're the liquidity they're taking — they know something)
            adverse = max(0, state.imbalance) * 0.5 + max(0, state.informed_flow) * 0.5
        else:
            # Adverse if we're selling when others are selling aggressively
            adverse = max(0, -state.imbalance) * 0.5 + max(0, -state.informed_flow) * 0.5

        return min(1.0, adverse)

    def get_fill_probability(self, order_side: str, order_price: float,
                              best_bid: float, best_ask: float,
                              bid_size: float, ask_size: float,
                              spread: float, volume: float,
                              age: float, post_only: bool,
                              token: str = "") -> Tuple[float, bool, str]:
        """
        Calculate fill probability for a resting order.
        
        Returns:
        - fill_prob: probability of fill this tick (0.0 to 0.10)
        - is_adverse: True if the fill would be adverse selection
        - reason: explanation string
        """
        self._fill_stats["total_attempts"] += 1

        # Must be resting long enough
        if age < self.MIN_REST_TIME:
            return 0.0, False, "too_young"

        # ─── Determine queue position ────────────────────────

        tick_size = 0.01
        at_best = False
        inside_best = False
        behind_best = False

        if order_side == "BUY":
            if abs(order_price - best_bid) < tick_size:
                at_best = True
            elif order_price > best_bid:
                inside_best = True
            else:
                behind_best = True
            book_depth = bid_size
        else:
            if abs(order_price - best_ask) < tick_size:
                at_best = True
            elif order_price < best_ask:
                inside_best = True
            else:
                behind_best = True
            book_depth = ask_size

        # Post-only: if we'd cross the spread, reject
        if post_only:
            if order_side == "BUY" and order_price >= best_ask:
                self._fill_stats["rejected_fills"] += 1
                return 0.0, False, "post_only_reject"
            if order_side == "SELL" and order_price <= best_bid:
                self._fill_stats["rejected_fills"] += 1
                return 0.0, False, "post_only_reject"

        # ─── Base fill rate ──────────────────────────────────

        if inside_best:
            base = self.BASE_FILL_RATE_INSIDE
        elif at_best:
            base = self.BASE_FILL_RATE_AT_BEST
        else:
            base = self.BASE_FILL_RATE_BEHIND

        # ─── Depth adjustment ───────────────────────────────

        if book_depth > 0:
            # Thin books fill faster (less competition)
            depth_factor = min(2.5, 500 / max(book_depth, 10))
        else:
            depth_factor = 1.5
        base *= depth_factor

        # ─── Spread adjustment ──────────────────────────────

        if spread > 0.15:
            base *= 0.4   # 15¢+ spread → fewer takers
        elif spread > 0.10:
            base *= 0.55  # 10¢ spread
        elif spread > 0.05:
            base *= 0.80  # 5¢ spread
        elif spread < 0.04:
            base *= 1.4   # tight spread → many takers

        # ─── Volume regime ──────────────────────────────────

        vol_factor = min(1.5, math.log10(max(volume, 1)) / 5.5)
        base *= vol_factor

        # ─── Age ramp ───────────────────────────────────────

        age_factor = min(2.0, 1.0 + (age - self.MIN_REST_TIME) / 150)
        base *= age_factor

        # ─── Adverse selection adjustment ───────────────────

        adverse_score = self.get_adverse_selection_score(token, order_side)

        if adverse_score > 0.3:
            # Informed flow present — fills come FASTER but are ADVERSE
            # This is realistic: when someone with info trades, they take
            # all available liquidity including your order
            adverse_boost = 1.0 + adverse_score * self.ADVERSE_SELECTION_STRENGTH * 2
            base *= adverse_boost
            is_adverse = True
            reason = f"adverse_flow({adverse_score:.2f})"
        else:
            is_adverse = False
            reason = "normal"

        # ─── Time of day ────────────────────────────────────

        from datetime import datetime, timezone
        utc_hour = datetime.now(timezone.utc).hour
        if 14 <= utc_hour <= 21:
            base *= 1.3  # US market hours → more flow
        elif 3 <= utc_hour <= 6:
            base *= 0.4  # quiet hours

        # ─── Volatility adjustment ──────────────────────────

        state = self._flow.get(token)
        if state and state.volatility > 0.02:
            # High volatility → more takers but also more adverse selection
            base *= min(1.5, 1.0 + state.volatility * 10)

        # ─── Cap ────────────────────────────────────────────

        fill_prob = min(base, 0.25)  # max 25% per tick

        # Apply the adverse selection penalty to profitability
        # (fill prob is higher, but the fill is "bad")
        if is_adverse:
            # Still return the probability — the ADVERSE flag tells the
            # caller to handle it (e.g., by reducing exit target)
            pass

        return fill_prob, is_adverse, reason

    def simulate_fill(self, order_side: str, order_price: float,
                       order_shares: float,
                       best_bid: float, best_ask: float,
                       bid_size: float, ask_size: float,
                       spread: float, volume: float,
                       age: float, post_only: bool,
                       token: str = "") -> Tuple[bool, float, float, bool]:
        """
        Simulate a fill attempt.
        
        Returns:
        - filled: True if order filled
        - fill_price: actual fill price (may differ from order price due to slippage)
        - fill_shares: number of shares filled (may be partial)
        - is_adverse: True if this was an adverse selection fill
        
        When adverse selection is active:
        - Fill probability is HIGHER (informed traders take your liquidity)
        - But the fill is BAD (price is about to move against you)
        - The bot should immediately try to exit at a worse price
        """
        fill_prob, is_adverse, reason = self.get_fill_probability(
            order_side, order_price, best_bid, best_ask,
            bid_size, ask_size, spread, volume, age, post_only, token
        )

        if fill_prob <= 0:
            return False, 0, 0, False

        if random.random() > fill_prob:
            return False, 0, 0, False

        # ─── Fill occurred ──────────────────────────────────

        self._fill_stats["total_fills"] += 1

        # Determine fill price
        if order_side == "BUY":
            # BUY fills at or below our price
            fill_price = order_price
            if is_adverse:
                # Adverse fill: we pay slightly more (slippage)
                slippage = random.uniform(0, 0.005)  # 0-0.5¢ slippage
                fill_price = min(order_price + slippage, best_ask)
        else:
            # SELL fills at or above our price
            fill_price = order_price
            if is_adverse:
                slippage = random.uniform(0, 0.005)
                fill_price = max(order_price - slippage, best_bid)

        fill_price = round(fill_price, 4)

        # Determine fill size (partial fills)
        if random.random() < self.PARTIAL_FILL_PROB:
            fill_pct = random.uniform(self.PARTIAL_FILL_MIN, self.PARTIAL_FILL_MAX)
            fill_shares = round(order_shares * fill_pct, 2)
            self._fill_stats["partial_fills"] += 1
        else:
            fill_shares = order_shares

        # Track adverse selection
        if is_adverse:
            self._fill_stats["adverse_fills"] += 1
            LOG.warning(f"⚠️ ADVERSE FILL | {order_side} {fill_shares:.0f} @ ${fill_price:.4f} | "
                       f"{reason} | Price likely to move against you")
        else:
            self._fill_stats["favorable_fills"] += 1

        return True, fill_price, fill_shares, is_adverse

    def get_stats(self) -> dict:
        """Get fill simulation statistics."""
        s = self._fill_stats
        total = s["total_fills"]
        return {
            **s,
            "adverse_rate": s["adverse_fills"] / max(1, total),
            "partial_rate": s["partial_fills"] / max(1, total),
            "rejection_rate": s["rejected_fills"] / max(1, s["total_attempts"]),
            "fill_rate": total / max(1, s["total_attempts"]),
        }

    def report(self) -> str:
        """Human-readable fill stats."""
        s = self.get_stats()
        return (
            f"\n📊 FILL SIMULATOR STATS\n"
            f"  Attempts: {s['total_attempts']} | Fills: {s['total_fills']} ({s['fill_rate']:.1%})\n"
            f"  Adverse: {s['adverse_fills']} ({s['adverse_rate']:.1%}) | "
            f"Partial: {s['partial_fills']} ({s['partial_rate']:.1%})\n"
            f"  Post-only rejects: {s['rejected_fills']} ({s['rejection_rate']:.1%})"
        )
