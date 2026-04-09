"""
ML Predictor v5 — Statistical price prediction for short-term trading.

This isn't a deep learning model (no GPU needed, no training data required).
It's a statistical ensemble of simple signals that together produce a
directional edge on short time horizons (30-300 seconds).

Signals:
1. **Momentum** — price rate of change over multiple timeframes
2. **Mean Reversion** — deviation from recent average
3. **Volume-Price Divergence** — volume increasing but price flat = pending move
4. **Spread Compression** — tightening spread = consensus forming = imminent move
5. **Order Flow Imbalance** — buy/sell pressure from trade data
6. **Time-of-Day Patterns** — some hours have predictable flow patterns
7. **Volatility Regime** — predict whether vol will increase or decrease

Each signal produces a score from -1.0 (bearish) to +1.0 (bullish).
The ensemble combines them with learned weights.
"""

import math
import time
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from collections import defaultdict

LOG = logging.getLogger("scalper.ml")


@dataclass
class PriceObservation:
    """A single price/volume observation."""
    timestamp: float
    price: float
    volume: float
    spread: float
    bid: float
    ask: float


@dataclass
class Prediction:
    """A price prediction."""
    slug: str
    direction: str  # "bullish", "bearish", "neutral"
    confidence: float  # 0.0 to 1.0
    expected_move: float  # expected price change in dollars
    time_horizon: int  # expected time in seconds
    signals: Dict[str, float]  # individual signal scores
    timestamp: float = field(default_factory=time.time)


class MLPredictor:
    """
    Statistical prediction engine for short-term price direction.
    
    NOT a neural network. Uses weighted ensemble of simple statistical
    signals. Each signal is independently calibrated and the ensemble
    weights are adjusted based on historical accuracy.
    """

    def __init__(self):
        # slug → list of observations
        self._history: Dict[str, List[PriceObservation]] = {}
        # Track prediction outcomes for weight calibration
        self._prediction_log: List[dict] = []
        # Signal weights (calibrated over time)
        self._signal_weights: Dict[str, float] = {
            "momentum": 1.0,
            "mean_reversion": 0.8,
            "volume_divergence": 0.9,
            "spread_compression": 0.7,
            "flow_imbalance": 1.1,
            "time_pattern": 0.5,
            "volatility_regime": 0.6,
        }
        # Track signal accuracy for weight adjustment
        self._signal_accuracy: Dict[str, Dict[str, int]] = defaultdict(lambda: {"correct": 0, "total": 0})

    def record(self, slug: str, price: float, volume: float = 0,
                spread: float = 0.05, bid: float = 0, ask: float = 1):
        """Record a price observation."""
        obs = PriceObservation(
            timestamp=time.time(),
            price=price,
            volume=volume,
            spread=spread,
            bid=bid,
            ask=ask,
        )
        history = self._history.setdefault(slug, [])
        history.append(obs)

        # Keep last 10 minutes
        cutoff = time.time() - 600
        self._history[slug] = [o for o in history if o.timestamp > cutoff]

    def predict(self, slug: str) -> Optional[Prediction]:
        """
        Generate a prediction for the next 30-300 seconds.
        
        Returns None if insufficient data.
        """
        history = self._history.get(slug, [])
        if len(history) < 10:
            return None

        signals = {}

        # ─── 1. Momentum ────────────────────────────────────

        signals["momentum"] = self._signal_momentum(history)

        # ─── 2. Mean Reversion ──────────────────────────────

        signals["mean_reversion"] = self._signal_mean_reversion(history)

        # ─── 3. Volume-Price Divergence ─────────────────────

        signals["volume_divergence"] = self._signal_volume_divergence(history)

        # ─── 4. Spread Compression ──────────────────────────

        signals["spread_compression"] = self._signal_spread_compression(history)

        # ─── 5. Flow Imbalance ──────────────────────────────

        signals["flow_imbalance"] = self._signal_flow_imbalance(history)

        # ─── 6. Time Pattern ────────────────────────────────

        signals["time_pattern"] = self._signal_time_pattern(history)

        # ─── 7. Volatility Regime ───────────────────────────

        signals["volatility_regime"] = self._signal_volatility_regime(history)

        # ─── Ensemble ───────────────────────────────────────

        weighted_sum = 0
        total_weight = 0
        for name, score in signals.items():
            weight = self._signal_weights.get(name, 1.0)
            # Adjust weight by historical accuracy
            accuracy = self._signal_accuracy[name]
            if accuracy["total"] >= 10:
                acc_rate = accuracy["correct"] / accuracy["total"]
                weight *= (0.5 + acc_rate)  # scale from 0.5x to 1.5x

            weighted_sum += score * weight
            total_weight += weight

        if total_weight == 0:
            return None

        ensemble_score = weighted_sum / total_weight

        # ─── Determine direction and confidence ─────────────

        if ensemble_score > 0.15:
            direction = "bullish"
        elif ensemble_score < -0.15:
            direction = "bearish"
        else:
            direction = "neutral"

        confidence = min(1.0, abs(ensemble_score))

        # Expected move: magnitude based on recent volatility
        prices = [o.price for o in history[-20:]]
        if len(prices) >= 3:
            recent_vol = max(abs(prices[i] - prices[i-1]) for i in range(1, len(prices)))
            expected_move = recent_vol * confidence * (1 if ensemble_score > 0 else -1)
        else:
            expected_move = 0.005 * (1 if ensemble_score > 0 else -1)

        # Time horizon: inversely related to confidence
        # High confidence → shorter horizon (move is imminent)
        # Low confidence → longer horizon (might take a while)
        time_horizon = int(300 - confidence * 240)  # 60-300 seconds

        return Prediction(
            slug=slug,
            direction=direction,
            confidence=confidence,
            expected_move=round(expected_move, 4),
            time_horizon=time_horizon,
            signals=signals,
        )

    def _signal_momentum(self, history: List[PriceObservation]) -> float:
        """Momentum signal: rate of change over multiple timeframes."""
        if len(history) < 5:
            return 0.0

        prices = [o.price for o in history]

        # Short-term momentum (last 30 seconds)
        short_window = min(5, len(prices))
        short_change = prices[-1] - prices[-short_window]

        # Medium-term momentum (last 2 minutes)
        med_window = min(20, len(prices))
        med_change = prices[-1] - prices[-med_window]

        # Combine (short-term weighted more)
        momentum = short_change * 0.6 + med_change * 0.4

        # Normalize to -1 to +1
        max_change = 0.05  # 5¢ is a big move in prediction markets
        return max(-1.0, min(1.0, momentum / max_change))

    def _signal_mean_reversion(self, history: List[PriceObservation]) -> float:
        """Mean reversion signal: deviation from recent mean."""
        if len(history) < 10:
            return 0.0

        prices = [o.price for o in history[-30:]]
        mean_price = sum(prices) / len(prices)
        current_price = prices[-1]

        deviation = current_price - mean_price

        # Mean reversion: expect price to revert toward mean
        # Stronger signal when deviation is larger
        max_dev = 0.03  # 3¢
        reversion = -deviation / max_dev  # negative because reversion opposes deviation
        return max(-1.0, min(1.0, reversion))

    def _signal_volume_divergence(self, history: List[PriceObservation]) -> float:
        """Volume-price divergence: high volume + flat price = pending move."""
        if len(history) < 10:
            return 0.0

        recent = history[-10:]

        # Price change over last 10 observations
        price_change = abs(recent[-1].price - recent[0].price)

        # Average volume
        volumes = [o.volume for o in recent if o.volume > 0]
        if not volumes:
            return 0.0
        avg_volume = sum(volumes) / len(volumes)

        # Volume spike (current vs average)
        current_vol = recent[-1].volume if recent[-1].volume > 0 else avg_volume
        vol_ratio = current_vol / max(avg_volume, 0.01)

        # Divergence: high volume but small price change → move coming
        if vol_ratio > 2.0 and price_change < 0.005:
            # Volume is spiking but price isn't moving → pressure building
            # We can't tell direction from this alone, so return small positive
            # (conservative: assume continuation of recent trend)
            recent_trend = recent[-1].price - recent[0].price
            direction = 1 if recent_trend >= 0 else -1
            return direction * min(1.0, vol_ratio / 5)

        return 0.0

    def _signal_spread_compression(self, history: List[PriceObservation]) -> float:
        """Spread compression signal: tightening spread = consensus forming."""
        if len(history) < 10:
            return 0.0

        spreads = [o.spread for o in history if o.spread > 0]
        if len(spreads) < 5:
            return 0.0

        recent_spread = spreads[-1]
        avg_spread = sum(spreads) / len(spreads)

        if avg_spread == 0:
            return 0.0

        # Compression ratio
        compression = 1 - (recent_spread / avg_spread)

        # Significant compression (> 20% tighter)
        if compression > 0.2:
            # Price is about to move toward the consensus
            # Direction from recent momentum
            prices = [o.price for o in history[-5:]]
            trend = prices[-1] - prices[0]
            direction = 1 if trend >= 0 else -1
            return direction * min(1.0, compression)

        return 0.0

    def _signal_flow_imbalance(self, history: List[PriceObservation]) -> float:
        """Order flow imbalance signal."""
        if len(history) < 5:
            return 0.0

        # Use price direction as proxy for flow
        # If price rose, net flow was buy. If price fell, net flow was sell.
        recent = history[-5:]
        flow_sum = 0
        for i in range(1, len(recent)):
            change = recent[i].price - recent[i-1].price
            flow_sum += change

        # Normalize
        max_flow = 0.02
        return max(-1.0, min(1.0, flow_sum / max_flow))

    def _signal_time_pattern(self, history: List[PriceObservation]) -> float:
        """Time-of-day pattern signal."""
        from datetime import datetime, timezone
        hour = datetime.now(timezone.utc).hour

        # Historical patterns (could be learned from brain.json)
        if 14 <= hour <= 16:
            return 0.2   # US open → bullish tendency (more buyers)
        elif 20 <= hour <= 22:
            return -0.1  # US close → slight bearish (profit taking)
        elif 3 <= hour <= 6:
            return 0.0   # quiet hours → no signal
        else:
            return 0.0

    def _signal_volatility_regime(self, history: List[PriceObservation]) -> float:
        """Volatility regime signal: predict vol expansion/contraction."""
        if len(history) < 20:
            return 0.0

        prices = [o.price for o in history]

        # Recent volatility (last 5 observations)
        recent_changes = [abs(prices[i] - prices[i-1]) for i in range(-4, 0) if -i <= len(prices)]
        recent_vol = sum(recent_changes) / max(1, len(recent_changes))

        # Historical volatility (last 30 observations)
        hist_changes = [abs(prices[i] - prices[i-1]) for i in range(1, min(30, len(prices)))]
        hist_vol = sum(hist_changes) / max(1, len(hist_changes))

        if hist_vol == 0:
            return 0.0

        # Vol compression: recent vol < 50% of historical → expansion coming
        vol_ratio = recent_vol / hist_vol
        if vol_ratio < 0.5:
            # Vol is compressed, expansion imminent
            # Direction is unpredictable, so return 0 (neutral)
            return 0.0
        elif vol_ratio > 2.0:
            # Vol is elevated, might continue or revert
            return 0.0

        return 0.0

    def record_outcome(self, slug: str, prediction: Prediction,
                        actual_direction: str, actual_move: float):
        """
        Record the outcome of a prediction for weight calibration.
        
        Args:
            slug: market slug
            prediction: the prediction we made
            actual_direction: what actually happened ("bullish", "bearish", "neutral")
            actual_move: actual price change
        """
        correct = prediction.direction == actual_direction

        # Update signal accuracy
        for signal_name, signal_score in prediction.signals.items():
            signal_direction = "bullish" if signal_score > 0.15 else ("bearish" if signal_score < -0.15 else "neutral")
            signal_correct = signal_direction == actual_direction

            acc = self._signal_accuracy[signal_name]
            acc["total"] += 1
            if signal_correct:
                acc["correct"] += 1

            # Adjust weight
            if acc["total"] >= 20:
                accuracy = acc["correct"] / acc["total"]
                # Move weight toward accuracy-proportional value
                target_weight = accuracy * 2  # 0-2 range
                current = self._signal_weights.get(signal_name, 1.0)
                self._signal_weights[signal_name] = current * 0.9 + target_weight * 0.1

        self._prediction_log.append({
            "slug": slug,
            "predicted": prediction.direction,
            "actual": actual_direction,
            "correct": correct,
            "confidence": prediction.confidence,
            "signals": prediction.signals,
            "timestamp": time.time(),
        })

    def get_accuracy(self) -> float:
        """Overall prediction accuracy."""
        if not self._prediction_log:
            return 0.0
        correct = sum(1 for p in self._prediction_log if p["correct"])
        return correct / len(self._prediction_log)

    def report(self) -> str:
        """Human-readable prediction report."""
        total = len(self._prediction_log)
        correct = sum(1 for p in self._prediction_log if p["correct"])
        accuracy = correct / max(1, total)

        lines = [
            f"\n🤖 ML PREDICTOR",
            f"  Predictions: {total} | Accuracy: {accuracy:.1%}",
            f"  Signal weights:",
        ]
        for name, weight in sorted(self._signal_weights.items(), key=lambda x: -x[1]):
            acc = self._signal_accuracy[name]
            acc_rate = acc["correct"] / max(1, acc["total"])
            lines.append(f"    {name:<20} weight={weight:.2f} accuracy={acc_rate:.1%} ({acc['total']} samples)")

        return "\n".join(lines)
