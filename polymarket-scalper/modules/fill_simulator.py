"""
Fill Simulator v7 — Order-book-grounded paper fill engine.

v5/v6 approach: Random probability each tick → fantasy returns
v7 approach: Compare paper orders against LIVE WebSocket book state

The key insight: We already have real-time order book data from the
WebSocket feed. Instead of rolling dice, we check:
1. Did the best bid/ask cross our order price?
2. Did the trade stream execute at our price?
3. Did the book depth change at our price level?

This makes paper fills deterministic and realistic — if the real
market would have filled you, paper fills you. If not, not.
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
    """Tracks order flow for adverse selection modeling."""
    trades: List[Tuple[float, float, float, str]] = field(default_factory=list)
    imbalance: float = 0.0
    volatility: float = 0.0
    informed_flow: float = 0.0
    last_update: float = 0.0


@dataclass
class BookSnapshot:
    """A snapshot of the order book at a point in time."""
    timestamp: float
    best_bid: float
    best_ask: float
    bid_size: float
    ask_size: float
    last_trade_price: Optional[float] = None
    last_trade_side: Optional[str] = None


class FillSimulator:
    """
    Order-book-grounded fill simulator.
    
    Instead of random probability, checks actual market conditions:
    - If best bid >= our sell price → we got filled
    - If best ask <= our buy price → we got filled
    - If a trade printed at our price → we got filled
    
    Then applies realistic penalties:
    - Slippage proportional to order size / depth
    - Adverse selection when flow is against us
    - Partial fills on thin books
    """

    # Slippage model
    SLIPPAGE_BASE = 0.001             # 0.1¢ base slippage
    SLIPPAGE_PER_DEPTH_PCT = 0.0005   # 0.05¢ per 1% of depth consumed
    SLIPPAGE_MAX = 0.008              # 0.8¢ max slippage

    # Paper tax — hidden cost to account for real-world friction
    PAPER_TAX = 0.001                 # 0.1¢ per fill

    # Partial fill model
    PARTIAL_FILL_PROB = 0.35          # 35% chance
    PARTIAL_FILL_MIN = 0.30
    PARTIAL_FILL_MAX = 0.80

    # Size impact — orders >X% of depth get penalized
    SIZE_IMPACT_THRESHOLD = 0.03      # 3% of depth
    FILL_PROB_PENALTY_AT_10PCT = 0.5  # at 10% depth, 50% normal fill prob

    # Min resting time before fills can happen
    MIN_REST_TIME = 3.0

    # Adverse selection
    ADVERSE_SELECTION_STRENGTH = 0.6

    def __init__(self):
        self._flow: Dict[str, FlowState] = {}
        self._book_history: Dict[str, List[BookSnapshot]] = {}
        self._fill_stats = {
            "total_attempts": 0,
            "total_fills": 0,
            "book_cross_fills": 0,     # filled because book crossed our price
            "trade_fills": 0,           # filled because a trade hit our level
            "simulated_fills": 0,       # fallback probability fill
            "adverse_fills": 0,
            "favorable_fills": 0,
            "partial_fills": 0,
            "rejected_fills": 0,
            "size_penalized": 0,
            "total_slippage": 0.0,
        }

    def record_book(self, token: str, best_bid: float, best_ask: float,
                    bid_size: float, ask_size: float,
                    last_trade: Optional[float] = None,
                    last_trade_side: Optional[str] = None):
        """Record an order book snapshot from the WebSocket."""
        now = time.time()
        history = self._book_history.setdefault(token, [])
        history.append(BookSnapshot(
            timestamp=now,
            best_bid=best_bid,
            best_ask=best_ask,
            bid_size=bid_size,
            ask_size=ask_size,
            last_trade_price=last_trade,
            last_trade_side=last_trade_side,
        ))
        # Keep last 10 minutes
        cutoff = now - 600
        self._book_history[token] = [s for s in history if s.timestamp > cutoff]

    def record_trade(self, token: str, price: float, size: float, side: str):
        """Record a trade for flow analysis."""
        now = time.time()
        state = self._flow.setdefault(token, FlowState())
        state.trades.append((now, price, size, side))
        cutoff = now - 300
        state.trades = [t for t in state.trades if t[0] > cutoff]
        state.last_update = now
        self._update_flow_state(token)

    def _update_flow_state(self, token: str):
        state = self._flow.get(token)
        if not state or len(state.trades) < 2:
            return
        now = time.time()
        recent = [t for t in state.trades if t[0] > now - 60]
        if recent:
            buy_vol = sum(t[2] for t in recent if t[3] == "BUY")
            sell_vol = sum(t[2] for t in recent if t[3] == "SELL")
            total = buy_vol + sell_vol
            if total > 0:
                state.imbalance = (buy_vol - sell_vol) / total
        if len(state.trades) >= 3:
            prices = [t[1] for t in state.trades]
            changes = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
            if changes:
                mean_c = sum(changes) / len(changes)
                var = sum((c - mean_c) ** 2 for c in changes) / len(changes)
                state.volatility = math.sqrt(var)
        large = [t for t in recent if t[2] > 50]
        if large:
            lb = sum(t[2] for t in large if t[3] == "BUY")
            ls = sum(t[2] for t in large if t[3] == "SELL")
            lt = lb + ls
            if lt > 0:
                state.informed_flow = (lb - ls) / lt

    def get_adverse_selection_score(self, token: str, order_side: str) -> float:
        state = self._flow.get(token)
        if not state:
            return 0.0
        if order_side == "BUY":
            return min(1.0, max(0, state.imbalance) * 0.5 + max(0, state.informed_flow) * 0.5)
        else:
            return min(1.0, max(0, -state.imbalance) * 0.5 + max(0, -state.informed_flow) * 0.5)

    def _get_size_impact(self, order_usd: float, book_depth: float) -> Tuple[float, float]:
        """Returns (fill_prob_multiplier, expected_slippage)."""
        if book_depth <= 0:
            return 0.4, self.SLIPPAGE_MAX
        depth_pct = order_usd / book_depth
        if depth_pct <= self.SIZE_IMPACT_THRESHOLD:
            return 1.0, self.SLIPPAGE_BASE
        # Quadratic penalty — gets bad fast
        excess = min(1.0, (depth_pct - self.SIZE_IMPACT_THRESHOLD) / 0.10)
        fill_mult = 1.0 - excess * 0.7  # down to 30% at 13%+ depth
        slippage = self.SLIPPAGE_BASE + excess * (self.SLIPPAGE_MAX - self.SLIPPAGE_BASE)
        return fill_mult, slippage

    def _check_book_cross(self, token: str, order_side: str, order_price: float,
                           order_created: float) -> bool:
        """
        Check if the live order book crossed our order price since we placed it.
        
        This is the CORE of realistic paper fills:
        - If best_ask dropped below our BUY price → we'd be filled
        - If best_bid rose above our SELL price → we'd be filled
        """
        history = self._book_history.get(token, [])
        if not history:
            return False

        # Check snapshots after order was placed
        relevant = [s for s in history if s.timestamp > order_created]
        if not relevant:
            return False

        tick = 0.01
        for snap in relevant:
            if order_side == "BUY":
                # Our BUY would fill if best_ask <= our price
                if snap.best_ask > 0 and snap.best_ask <= order_price + tick:
                    return True
            else:
                # Our SELL would fill if best_bid >= our price
                if snap.best_bid > 0 and snap.best_bid >= order_price - tick:
                    return True

        return False

    def _check_trade_at_price(self, token: str, order_side: str, order_price: float,
                                order_created: float) -> bool:
        """
        Check if trades printed at or through our price since order placement.
        
        If someone traded at our price, we likely got filled too.
        """
        state = self._flow.get(token)
        if not state:
            return False

        tick = 0.01
        for ts, price, size, trade_side in state.trades:
            if ts <= order_created:
                continue
            # A trade at or through our price
            if order_side == "BUY" and trade_side == "SELL" and price <= order_price + tick:
                return True
            if order_side == "SELL" and trade_side == "BUY" and price >= order_price - tick:
                return True

        return False

    def simulate_fill(self, order_side: str, order_price: float,
                       order_shares: float,
                       best_bid: float, best_ask: float,
                       bid_size: float, ask_size: float,
                       spread: float, volume: float,
                       age: float, post_only: bool,
                       token: str = "",
                       order_created: float = 0) -> Tuple[bool, float, float, bool]:
        """
        Simulate a fill using real market data.
        
        Priority:
        1. Check if book crossed our price (deterministic)
        2. Check if trades hit our price (deterministic)
        3. Fall back to probability model (for edge cases)
        
        Returns (filled, fill_price, fill_shares, is_adverse).
        """
        self._fill_stats["total_attempts"] += 1

        if age < self.MIN_REST_TIME:
            return False, 0, 0, False

        order_usd = order_shares * order_price

        # Post-only: reject if we'd cross
        if post_only:
            if order_side == "BUY" and order_price >= best_ask:
                self._fill_stats["rejected_fills"] += 1
                return False, 0, 0, False
            if order_side == "SELL" and order_price <= best_bid:
                self._fill_stats["rejected_fills"] += 1
                return False, 0, 0, False

        book_depth = bid_size if order_side == "BUY" else ask_size
        size_mult, base_slippage = self._get_size_impact(order_usd, book_depth)
        if size_mult < 0.5:
            self._fill_stats["size_penalized"] += 1

        # ─── Priority 1: Deterministic book cross ───────────
        tick = 0.01
        book_crossed = False
        if order_side == "BUY" and best_ask > 0 and best_ask <= order_price + tick:
            book_crossed = True
        elif order_side == "SELL" and best_bid > 0 and best_bid >= order_price - tick:
            book_crossed = True

        # Also check historical book snapshots
        if not book_crossed and order_created > 0:
            book_crossed = self._check_book_cross(token, order_side, order_price, order_created)

        # ─── Priority 2: Trade at price ─────────────────────
        trade_hit = False
        if not book_crossed and order_created > 0:
            trade_hit = self._check_trade_at_price(token, order_side, order_price, order_created)

        # ─── Priority 3: Probability fallback ───────────────
        prob_fill = False
        if not book_crossed and not trade_hit:
            # Only fill via probability if we're at the best level
            at_best = False
            if order_side == "BUY" and abs(order_price - best_bid) < tick:
                at_best = True
            elif order_side == "SELL" and abs(order_price - best_ask) < tick:
                at_best = True

            if at_best:
                # Base rate: ~1.5% per second at best (much lower than v5)
                base_rate = 0.015
                base_rate *= size_mult

                # Spread penalty
                if spread > 0.10:
                    base_rate *= 0.4
                elif spread > 0.05:
                    base_rate *= 0.65

                # Adverse selection boost
                adverse_score = self.get_adverse_selection_score(token, order_side)
                if adverse_score > 0.2:
                    base_rate *= (1.0 + adverse_score * self.ADVERSE_SELECTION_STRENGTH * 2)

                # Age ramp
                age_factor = min(1.5, 1.0 + (age - self.MIN_REST_TIME) / 200)
                base_rate *= age_factor

                # Time of day
                from datetime import datetime, timezone
                utc_hour = datetime.now(timezone.utc).hour
                if 3 <= utc_hour <= 6:
                    base_rate *= 0.25

                base_rate = min(base_rate, 0.08)
                prob_fill = random.random() < base_rate

        # ─── No fill ────────────────────────────────────────
        if not book_crossed and not trade_hit and not prob_fill:
            return False, 0, 0, False

        # ─── Fill occurred — determine type ─────────────────
        self._fill_stats["total_fills"] += 1
        if book_crossed:
            self._fill_stats["book_cross_fills"] += 1
        elif trade_hit:
            self._fill_stats["trade_fills"] += 1
        else:
            self._fill_stats["simulated_fills"] += 1

        # ─── Adverse selection check ────────────────────────
        adverse_score = self.get_adverse_selection_score(token, order_side)
        is_adverse = adverse_score > 0.25
        if is_adverse:
            self._fill_stats["adverse_fills"] += 1
        else:
            self._fill_stats["favorable_fills"] += 1

        # ─── Slippage ───────────────────────────────────────
        slip = base_slippage
        if is_adverse:
            slip *= random.uniform(1.5, 3.0)
        slip = min(slip, self.SLIPPAGE_MAX)
        total_cost = slip + self.PAPER_TAX
        self._fill_stats["total_slippage"] += total_cost

        if order_side == "BUY":
            fill_price = min(order_price + total_cost, best_ask) if best_ask > 0 else order_price + total_cost
        else:
            fill_price = max(order_price - total_cost, best_bid) if best_bid > 0 else order_price - total_cost
        fill_price = round(max(0.001, min(0.999, fill_price)), 4)

        # ─── Partial fills ──────────────────────────────────
        if random.random() < self.PARTIAL_FILL_PROB:
            fill_pct = random.uniform(self.PARTIAL_FILL_MIN, self.PARTIAL_FILL_MAX)
            fill_shares = round(order_shares * fill_pct, 2)
            self._fill_stats["partial_fills"] += 1
        else:
            fill_shares = order_shares

        if is_adverse:
            LOG.warning(f"⚠️ ADVERSE FILL | {order_side} {fill_shares:.0f} @ ${fill_price:.4f} "
                       f"(limit=${order_price:.4f}, slip=${total_cost:.4f}, "
                       f"{'book_cross' if book_crossed else 'trade_hit' if trade_hit else 'simulated'})")

        return True, fill_price, fill_shares, is_adverse

    def get_stats(self) -> dict:
        s = self._fill_stats
        total = s["total_fills"]
        return {
            **s,
            "adverse_rate": s["adverse_fills"] / max(1, total),
            "partial_rate": s["partial_fills"] / max(1, total),
            "rejection_rate": s["rejected_fills"] / max(1, s["total_attempts"]),
            "fill_rate": total / max(1, s["total_attempts"]),
            "avg_slippage": s["total_slippage"] / max(1, total),
            "book_cross_pct": s["book_cross_fills"] / max(1, total),
            "trade_hit_pct": s["trade_fills"] / max(1, total),
            "sim_pct": s["simulated_fills"] / max(1, total),
        }

    def report(self) -> str:
        s = self.get_stats()
        return (
            f"\n📊 FILL SIMULATOR v7 (book-grounded)\n"
            f"  Attempts: {s['total_attempts']} | Fills: {s['total_fills']} ({s['fill_rate']:.1%})\n"
            f"  Book-cross: {s['book_cross_fills']} ({s['book_cross_pct']:.0%}) | "
            f"Trade-hit: {s['trade_fills']} ({s['trade_hit_pct']:.0%}) | "
            f"Simulated: {s['simulated_fills']} ({s['sim_pct']:.0%})\n"
            f"  Adverse: {s['adverse_fills']} ({s['adverse_rate']:.1%}) | "
            f"Partial: {s['partial_fills']} ({s['partial_rate']:.1%})\n"
            f"  Size-penalized: {s['size_penalized']} | "
            f"Avg slippage: ${s['avg_slippage']:.4f} | "
            f"Total slip cost: ${s['total_slippage']:.2f}\n"
            f"  Post-only rejects: {s['rejected_fills']} ({s['rejection_rate']:.1%})"
        )
