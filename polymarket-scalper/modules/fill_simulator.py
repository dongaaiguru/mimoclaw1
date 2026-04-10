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
    PAPER_TAX = 0.002                 # 0.2¢ per fill (was 0.1¢)

    # Partial fill model
    PARTIAL_FILL_PROB = 0.35          # 35% chance
    PARTIAL_FILL_MIN = 0.30
    PARTIAL_FILL_MAX = 0.80

    # Size impact — orders >X% of depth get penalized
    SIZE_IMPACT_THRESHOLD = 0.03      # 3% of depth
    FILL_PROB_PENALTY_AT_10PCT = 0.5  # at 10% depth, 50% normal fill prob

    # Min resting time before fills can happen
    MIN_REST_TIME = 8.0

    # Adverse selection
    ADVERSE_SELECTION_STRENGTH = 0.8

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
        
        v8: FIXED logic for SELL orders.
        - BUY: fills when best_ask dropped to our bid (sellers crossed down)
        - SELL: fills when best_ask dropped to our ask (asks consumed to our level)
               OR best_bid rose to our ask (buyers crossed up)
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
                # Our SELL would fill if:
                # 1) best_bid >= our price (buyer lifted our ask)
                # 2) best_ask <= our price (asks consumed down to our level)
                if snap.best_bid > 0 and snap.best_bid >= order_price - tick:
                    return True
                if snap.best_ask > 0 and snap.best_ask <= order_price + tick:
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
        # v8: FIXED book cross logic
        # BUY fills: best_ask <= our price (ask book came down to us = sellers hitting our bid)
        # SELL fills: best_ask <= our price (ask book consumed down to our level = buyers hitting our ask)
        # OR: best_bid >= our price (bid book rose to our level)
        tick = 0.01
        book_crossed = False
        if order_side == "BUY":
            # Our BUY fills when the ask side comes down to our price
            # i.e., someone is willing to sell at/below our bid price
            if best_ask > 0 and best_ask <= order_price + tick:
                book_crossed = True
        elif order_side == "SELL":
            # Our SELL fills when:
            # 1) The best bid rises to/above our ask price (aggressive buyer)
            # 2) The best ask drops to/below our price (asks consumed down to us)
            if best_bid > 0 and best_bid >= order_price - tick:
                book_crossed = True
            elif best_ask > 0 and best_ask <= order_price + tick:
                # Asks were consumed down to our level — we're now at the best ask
                book_crossed = True

        # Also check historical book snapshots
        if not book_crossed and order_created > 0:
            book_crossed = self._check_book_cross(token, order_side, order_price, order_created)

        # ─── Priority 2: Trade at price ─────────────────────
        trade_hit = False
        if not book_crossed and order_created > 0:
            trade_hit = self._check_trade_at_price(token, order_side, order_price, order_created)

        # ─── Priority 3: Queue-based fill model ─────────────
        # Realistic model for post-only maker orders:
        # - We're resting at best_bid+1tick (or best_bid)
        # - Market SELL orders sweep down through the book
        # - If enough sell volume hits, we get filled at our price
        # This is the CORE of realistic scalper fill simulation
        prob_fill = False
        if not book_crossed and not trade_hit:
            # Check how close we are to the best level
            at_best = False
            near_best = False
            if order_side == "BUY":
                if abs(order_price - best_bid) <= tick:
                    at_best = True
                elif order_price <= best_bid + tick * 2 and order_price >= best_bid:
                    near_best = True  # 1-2 ticks from best bid
            elif order_side == "SELL":
                if abs(order_price - best_ask) <= tick:
                    at_best = True
                elif order_price >= best_ask - tick * 2 and order_price <= best_ask:
                    near_best = True

            if at_best or near_best:
                # ── Queue fill rate ──────────────────────────
                # v8: More realistic fill rates
                # At best bid on a 10¢ spread market: ~5% per check
                # Near best (1 tick): ~3% per check
                # These produce fills every 2-6 min on active markets
                if at_best:
                    base_rate = 0.05  # 5% per check at best level
                else:
                    base_rate = 0.03  # 3% per check near best (1 tick)

                base_rate *= size_mult

                # Spread adjustment: wider spread = fewer aggressive takers
                # More aggressive penalty — wide-spread markets have thick books
                if spread > 0.20:
                    base_rate *= 0.15   # 20¢+ spread — very few takers, thick book
                elif spread > 0.15:
                    base_rate *= 0.25   # 15¢ spread
                elif spread > 0.10:
                    base_rate *= 0.40   # 10¢ spread
                elif spread > 0.05:
                    base_rate *= 0.65   # 5¢ spread
                elif spread < 0.03:
                    base_rate *= 1.5    # tight spread — many takers

                # Activity boost: more trades = more fills
                state = self._flow.get(token)
                if state and state.trades:
                    now = time.time()
                    recent_trades = sum(1 for t in state.trades if t[0] > now - 30)
                    if recent_trades > 10:
                        base_rate *= 1.5
                    elif recent_trades > 5:
                        base_rate *= 1.2
                    elif recent_trades < 2:
                        base_rate *= 0.5

                # Adverse selection: aggressive flow toward us = faster fill
                adverse_score = self.get_adverse_selection_score(token, order_side)
                if adverse_score > 0.2:
                    base_rate *= (1.0 + adverse_score * self.ADVERSE_SELECTION_STRENGTH * 2)

                # Age ramp: older orders get priority in queue
                age_factor = min(1.8, 1.0 + (age - self.MIN_REST_TIME) / 100)
                base_rate *= age_factor

                # Time of day: quiet hours = fewer fills
                from datetime import datetime, timezone
                utc_hour = datetime.now(timezone.utc).hour
                if 3 <= utc_hour <= 6:
                    base_rate *= 0.25

                # Cap per-check probability
                base_rate = min(base_rate, 0.15)
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

        # ─── Slippage — realistic for maker fills ───────────
        # When you BUY at the bid as a maker, a seller hits you.
        # The fill price reflects that selling pressure pushed price down slightly.
        # When you SELL at the ask as a maker, a buyer hits you.
        # The fill price reflects that buying pressure pushed price up slightly.
        slip = base_slippage
        if is_adverse:
            slip *= random.uniform(2.0, 4.0)  # worse adverse slippage
        slip = min(slip, self.SLIPPAGE_MAX)
        adverse_slip = slip * 0.5  # mild adverse — fills between order_price and best_bid/ask
        total_cost = slip + self.PAPER_TAX + adverse_slip
        self._fill_stats["total_slippage"] += total_cost

        # Maker BUY fill: you're filled between order_price and best_bid
        # (you're the highest bidder, so you fill at or below your price)
        # Maker SELL fill: you're filled between order_price and best_ask
        if order_side == "BUY":
            # Fill between best_bid and order_price — adverse slip pushes toward best_bid
            fill_price = max(order_price - total_cost, best_bid) if best_bid > 0 else order_price - total_cost
        else:
            # Fill between best_ask and order_price — adverse slip pushes toward best_ask
            fill_price = min(order_price + total_cost, best_ask) if best_ask < 1 else order_price + total_cost
        fill_price = round(max(0.001, min(0.999, fill_price)), 4)

        # ─── Partial fills — more aggressive for wide spreads ──
        # Wide-spread markets have thick books with many competing orders.
        # You rarely get your full order filled — more like 20-60%.
        spread_pct = spread / 0.50  # normalize: 0.50¢ spread = 1.0
        if spread_pct > 0.4:
            # Very wide spread: 60% chance of partial, fill 15-50%
            partial_prob = 0.60
            partial_min, partial_max = 0.15, 0.50
        elif spread_pct > 0.2:
            # Medium spread: 40% chance of partial, fill 30-70%
            partial_prob = 0.40
            partial_min, partial_max = 0.30, 0.70
        else:
            # Tight spread: 25% chance of partial, fill 50-90%
            partial_prob = 0.25
            partial_min, partial_max = 0.50, 0.90

        if random.random() < partial_prob:
            fill_pct = random.uniform(partial_min, partial_max)
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
