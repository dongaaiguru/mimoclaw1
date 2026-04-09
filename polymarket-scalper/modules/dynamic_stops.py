"""
Dynamic Stop Loss v5 — Adaptive stop losses based on market conditions.

The v4 bot uses a fixed 2¢ stop loss on every position regardless of:
- Market volatility
- Spread width
- Position size
- How long you've been holding

This module implements:
1. ATR-based stops (Average True Range from recent price action)
2. Spread-based stops (stop = entry - N * spread)
3. Time-decaying stops (tighten as hold time increases)
4. Trailing stops (move up with price, never down)
5. Volatility regime detection (calm vs volatile markets)
"""

import math
import time
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

LOG = logging.getLogger("scalper.stops")


@dataclass
class PricePoint:
    """A single price observation."""
    timestamp: float
    price: float
    spread: float


@dataclass
class StopState:
    """Stop loss state for a position."""
    slug: str
    side: str  # LONG or SHORT
    entry_price: float
    initial_stop: float
    current_stop: float
    trailing_high: float = 0.0  # highest price seen since entry (for trailing)
    trailing_low: float = 1.0   # lowest price seen since entry
    opened: float = field(default_factory=time.time)
    stop_type: str = "atr"  # atr, spread, fixed, trailing
    times_hit: int = 0  # how many times stop was nearly triggered


class DynamicStopLoss:
    """
    Adaptive stop loss manager.
    
    Stop calculation hierarchy:
    1. ATR-based: stop = entry - (ATR_multiplier * ATR)
    2. Spread-based: stop = entry - (spread_multiplier * spread)
    3. Take the WIDER of the two (more conservative)
    4. Apply time decay: stop tightens as hold time increases
    5. Apply trailing: stop moves up with price (never down for LONG)
    """

    # ─── Configuration ───────────────────────────────────────

    # ATR multiplier for initial stop (wider = more room)
    ATR_MULTIPLIER = 2.0

    # Spread multiplier for initial stop
    SPREAD_MULTIPLIER = 1.5

    # Minimum stop distance in dollars
    MIN_STOP_DISTANCE = 0.005  # 0.5¢

    # Maximum stop distance in dollars
    MAX_STOP_DISTANCE = 0.05   # 5¢

    # Time decay: stop tightens by this fraction per minute after threshold
    TIME_DECAY_START = 60  # seconds before decay begins
    TIME_DECAY_RATE = 0.02  # 2% tightening per minute

    # Trailing stop: activate after this profit threshold
    TRAILING_ACTIVATE_PROFIT = 0.01  # 1¢ profit
    TRAILING_DISTANCE = 0.005  # trail 0.5¢ behind high

    def __init__(self):
        # token_id → list of price points
        self._price_history: Dict[str, List[PricePoint]] = {}
        # slug → StopState
        self._stops: Dict[str, StopState] = {}
        # ATR cache
        self._atr_cache: Dict[str, float] = {}
        self._atr_cache_time: Dict[str, float] = {}

    def record_price(self, token: str, price: float, spread: float):
        """Record a price observation for ATR calculation."""
        now = time.time()
        history = self._price_history.setdefault(token, [])
        history.append(PricePoint(now, price, spread))

        # Keep last 10 minutes
        cutoff = now - 600
        self._price_history[token] = [p for p in history if p.timestamp > cutoff]

        # Update trailing stops for any positions tracking this token
        for slug, stop in self._stops.items():
            if stop.side == "LONG":
                if price > stop.trailing_high:
                    stop.trailing_high = price
            else:
                if price < stop.trailing_low:
                    stop.trailing_low = price

    def calculate_atr(self, token: str, period: int = 20) -> float:
        """
        Calculate Average True Range from recent price history.
        
        ATR = average of (high - low) over the last N observations.
        For prediction markets, we use price range as a proxy for volatility.
        """
        now = time.time()
        # Use cache if fresh
        if token in self._atr_cache and (now - self._atr_cache_time.get(token, 0)) < 5:
            return self._atr_cache[token]

        history = self._price_history.get(token, [])
        if len(history) < 3:
            return 0.01  # default ATR

        # Calculate price ranges over rolling windows
        recent = history[-period:] if len(history) >= period else history
        ranges = []

        for i in range(1, len(recent)):
            # True range = max of:
            # 1. high - low (within same observation period)
            # 2. |high - prev_close|
            # 3. |low - prev_close|
            # Simplified: just use |price_change| between observations
            price_change = abs(recent[i].price - recent[i-1].price)
            # Also consider spread as minimum range
            tr = max(price_change, recent[i].spread * 0.5)
            ranges.append(tr)

        if not ranges:
            return 0.01

        atr = sum(ranges) / len(ranges)

        # Smooth with exponential moving average if we have cached value
        if token in self._atr_cache:
            old_atr = self._atr_cache[token]
            atr = old_atr * 0.7 + atr * 0.3  # 30% weight to new value

        self._atr_cache[token] = atr
        self._atr_cache_time[token] = now
        return atr

    def calculate_volatility_regime(self, token: str) -> str:
        """Classify current volatility: calm, normal, volatile, extreme."""
        atr = self.calculate_atr(token)
        history = self._price_history.get(token, [])

        if len(history) < 5:
            return "normal"

        # Get average spread
        avg_spread = sum(p.spread for p in history[-10:]) / min(10, len(history))

        # ATR relative to spread
        if atr < avg_spread * 0.3:
            return "calm"
        elif atr < avg_spread * 0.8:
            return "normal"
        elif atr < avg_spread * 1.5:
            return "volatile"
        else:
            return "extreme"

    def set_stop(self, slug: str, side: str, entry_price: float,
                  token: str = "", spread: float = 0.05) -> StopState:
        """
        Calculate and set an initial stop loss for a new position.
        
        Uses the wider of ATR-based and spread-based stops.
        """
        # Calculate ATR-based stop
        atr = self.calculate_atr(token) if token else 0.01
        atr_distance = atr * self.ATR_MULTIPLIER

        # Calculate spread-based stop
        spread_distance = spread * self.SPREAD_MULTIPLIER

        # Take the wider (more conservative)
        stop_distance = max(atr_distance, spread_distance)

        # Clamp to min/max
        stop_distance = max(self.MIN_STOP_DISTANCE, min(self.MAX_STOP_DISTANCE, stop_distance))

        if side == "LONG":
            initial_stop = round(entry_price - stop_distance, 4)
        else:
            initial_stop = round(entry_price + stop_distance, 4)

        regime = self.calculate_volatility_regime(token)

        stop = StopState(
            slug=slug,
            side=side,
            entry_price=entry_price,
            initial_stop=initial_stop,
            current_stop=initial_stop,
            trailing_high=entry_price,
            trailing_low=entry_price,
            stop_type="atr" if atr_distance >= spread_distance else "spread",
        )
        self._stops[slug] = stop

        LOG.info(f"🛑 STOP SET | {slug[:35]} | {side} @ ${entry_price:.4f} → "
                f"stop=${initial_stop:.4f} ({stop_distance:.4f} away, {regime} vol, "
                f"ATR={atr:.4f}, spread={spread:.4f})")
        return stop

    def update_stop(self, slug: str, current_price: float) -> Optional[float]:
        """
        Update stop loss for a position. Returns new stop price if changed.
        
        Applies:
        1. Trailing stop logic
        2. Time decay
        3. Volatility adjustment
        """
        stop = self._stops.get(slug)
        if not stop:
            return None

        old_stop = stop.current_stop
        now = time.time()
        hold_time = now - stop.opened

        # ─── Trailing stop ──────────────────────────────────

        if stop.side == "LONG":
            profit = current_price - stop.entry_price
            if profit >= self.TRAILING_ACTIVATE_PROFIT:
                trail_stop = round(current_price - self.TRAILING_DISTANCE, 4)
                if trail_stop > stop.current_stop:
                    stop.current_stop = trail_stop
                    stop.stop_type = "trailing"
        else:  # SHORT
            profit = stop.entry_price - current_price
            if profit >= self.TRAILING_ACTIVATE_PROFIT:
                trail_stop = round(current_price + self.TRAILING_DISTANCE, 4)
                if trail_stop < stop.current_stop:
                    stop.current_stop = trail_stop
                    stop.stop_type = "trailing"

        # ─── Time decay ─────────────────────────────────────

        if hold_time > self.TIME_DECAY_START:
            minutes_past = (hold_time - self.TIME_DECAY_START) / 60
            decay = minutes_past * self.TIME_DECAY_RATE

            if stop.side == "LONG":
                # Tighten stop upward
                max_tighten = stop.entry_price - self.MIN_STOP_DISTANCE
                tightened = round(stop.initial_stop + (stop.entry_price - stop.initial_stop) * decay, 4)
                tightened = min(tightened, max_tighten)
                if tightened > stop.current_stop:
                    stop.current_stop = tightened
            else:
                # Tighten stop downward
                max_tighten = stop.entry_price + self.MIN_STOP_DISTANCE
                tightened = round(stop.initial_stop - (stop.initial_stop - stop.entry_price) * decay, 4)
                tightened = max(tightened, max_tighten)
                if tightened < stop.current_stop:
                    stop.current_stop = tightened

        # ─── Return if changed ──────────────────────────────

        if stop.current_stop != old_stop:
            direction = "↑" if stop.current_stop > old_stop else "↓"
            LOG.info(f"🛑 STOP MOVE {direction} | {slug[:35]} | "
                    f"${old_stop:.4f} → ${stop.current_stop:.4f} ({stop.stop_type})")
            return stop.current_stop

        return None

    def check_stop_hit(self, slug: str, current_price: float) -> bool:
        """Check if current price has hit the stop loss."""
        stop = self._stops.get(slug)
        if not stop:
            return False

        if stop.side == "LONG":
            return current_price <= stop.current_stop
        else:
            return current_price >= stop.current_stop

    def get_stop_price(self, slug: str) -> Optional[float]:
        """Get current stop price for a position."""
        stop = self._stops.get(slug)
        return stop.current_stop if stop else None

    def remove_stop(self, slug: str):
        """Remove stop when position is closed."""
        self._stops.pop(slug, None)

    def get_stop_distance(self, slug: str, current_price: float) -> Optional[float]:
        """Get distance from current price to stop."""
        stop = self._stops.get(slug)
        if not stop:
            return None
        return abs(current_price - stop.current_stop)

    def should_tighten_stop(self, slug: str, flow_imbalance: float) -> bool:
        """
        Determine if stop should be tightened based on order flow.
        
        If flow is strongly against us, tighten the stop to limit losses.
        """
        stop = self._stops.get(slug)
        if not stop:
            return False

        now = time.time()
        hold_time = now - stop.opened

        # Don't tighten in the first 30 seconds (give it room)
        if hold_time < 30:
            return False

        # Flow strongly against us
        if stop.side == "LONG" and flow_imbalance < -0.5:
            return True
        if stop.side == "SHORT" and flow_imbalance > 0.5:
            return True

        return False

    def tighten_stop(self, slug: str, current_price: float, factor: float = 0.5):
        """
        Tighten the stop loss by a factor.
        
        factor: 0.5 = move stop halfway to current price
        """
        stop = self._stops.get(slug)
        if not stop:
            return

        old_stop = stop.current_stop

        if stop.side == "LONG":
            new_stop = round(stop.current_stop + (current_price - stop.current_stop) * factor, 4)
            new_stop = min(new_stop, current_price - self.MIN_STOP_DISTANCE)
            stop.current_stop = max(stop.current_stop, new_stop)
        else:
            new_stop = round(stop.current_stop - (stop.current_stop - current_price) * factor, 4)
            new_stop = max(new_stop, current_price + self.MIN_STOP_DISTANCE)
            stop.current_stop = min(stop.current_stop, new_stop)

        if stop.current_stop != old_stop:
            LOG.info(f"🛑 STOP TIGHTENED | {slug[:35]} | ${old_stop:.4f} → ${stop.current_stop:.4f} (flow-based)")

    def report(self) -> str:
        """Human-readable stop loss report."""
        if not self._stops:
            return "🛑 No active stops"

        lines = ["\n🛑 ACTIVE STOP LOSSES", "─" * 50]
        for slug, stop in self._stops.items():
            age = time.time() - stop.opened
            lines.append(f"  {slug[:30]:<30} | {stop.side} | "
                        f"entry=${stop.entry_price:.4f} | stop=${stop.current_stop:.4f} | "
                        f"type={stop.stop_type} | {age:.0f}s")
        lines.append("─" * 50)
        return "\n".join(lines)
