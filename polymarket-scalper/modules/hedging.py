"""
Hedging Engine v5 — Event-level risk management across multi-outcome markets.

On Polymarket, many events have multiple outcomes (e.g., "Who will win the election?"
with 5+ candidates). Holding positions on a single outcome is concentrated risk.

This module:
1. Detects when you have correlated positions across the same event
2. Suggests hedges (buying other outcomes to reduce variance)
3. Calculates optimal hedge ratios
4. Manages event-level exposure limits

Example:
- You're long YES on "Trump wins" at 60¢
- You should consider hedging with "Biden wins" at 35¢
- If combined < 95¢, you have a guaranteed floor
- If combined > 95¢, you're taking directional risk but the hedge reduces variance
"""

import time
import logging
from typing import Dict, List, Optional, Tuple, Set
from dataclasses import dataclass, field
from collections import defaultdict

LOG = logging.getLogger("scalper.hedge")


@dataclass
class EventExposure:
    """Exposure to a single event across multiple markets."""
    event_id: str
    markets: Dict[str, dict]  # slug → {side, entry_price, shares, cost, current_price}
    total_cost: float = 0.0
    net_direction: float = 0.0  # -1 to +1 (net bullishness)


@dataclass
class HedgeSuggestion:
    """A suggested hedge trade."""
    event_id: str
    slug_to_hedge: str
    hedge_side: str  # "BUY" or "SELL"
    hedge_size: float  # USDC
    reason: str
    risk_reduction: float  # estimated risk reduction (0-1)
    cost: float  # cost of the hedge
    expected_floor: float  # minimum payout if hedged properly


class HedgingEngine:
    """
    Event-level hedging and risk management.
    
    Key concepts:
    - "Complete set": owning YES on all outcomes = guaranteed $1.00 at resolution
    - "Partial hedge": owning YES on some outcomes reduces variance but not to zero
    - "Correlation risk": multiple positions on the same person/event are correlated
    """

    # ─── Configuration ───────────────────────────────────────

    # Minimum exposure before suggesting a hedge
    MIN_EXPOSURE_FOR_HEDGE = 20.0  # $20 minimum

    # Maximum event-level exposure as fraction of capital
    MAX_EVENT_EXPOSURE_PCT = 0.25  # 25%

    # Hedge triggers
    UNHEDGED_EXPOSURE_TRIGGER = 0.15  # suggest hedge if 15%+ in one event

    def __init__(self):
        # event_id → EventExposure
        self._exposures: Dict[str, EventExposure] = {}
        # event_id → [slug1, slug2, ...]
        self._event_markets: Dict[str, List[str]] = defaultdict(list)
        # slug → {yes_price, no_price, event_id, liquidity}
        self._market_data: Dict[str, dict] = {}

    def register_market(self, slug: str, event_id: str, yes_price: float,
                          no_price: float, liquidity: float):
        """Register a market for hedge tracking."""
        self._market_data[slug] = {
            "yes_price": yes_price,
            "no_price": no_price,
            "event_id": event_id,
            "liquidity": liquidity,
        }
        if event_id and slug not in self._event_markets[event_id]:
            self._event_markets[event_id].append(slug)

    def update_position(self, slug: str, side: str, entry_price: float,
                          shares: float, cost: float, current_price: float,
                          event_id: str = ""):
        """Update a position for hedge tracking."""
        if not event_id:
            data = self._market_data.get(slug, {})
            event_id = data.get("event_id", "")
        if not event_id:
            return  # can't hedge without event context

        exposure = self._exposures.setdefault(event_id, EventExposure(event_id=event_id, markets={}))
        exposure.markets[slug] = {
            "side": side,
            "entry_price": entry_price,
            "shares": shares,
            "cost": cost,
            "current_price": current_price,
        }
        self._recalculate_exposure(event_id)

    def remove_position(self, slug: str, event_id: str = ""):
        """Remove a closed position from hedge tracking."""
        if not event_id:
            data = self._market_data.get(slug, {})
            event_id = data.get("event_id", "")
        if not event_id:
            return

        exposure = self._exposures.get(event_id)
        if exposure:
            exposure.markets.pop(slug, None)
            if not exposure.markets:
                del self._exposures[event_id]
            else:
                self._recalculate_exposure(event_id)

    def _recalculate_exposure(self, event_id: str):
        """Recalculate total exposure and net direction for an event."""
        exposure = self._exposures.get(event_id)
        if not exposure:
            return

        total_cost = 0
        net_direction = 0

        for slug, pos in exposure.markets.items():
            cost = pos["cost"]
            total_cost += cost

            if pos["side"] == "LONG":
                net_direction += cost
            else:
                net_direction -= cost

        exposure.total_cost = total_cost
        exposure.net_direction = net_direction / max(total_cost, 0.01)

    def scan_for_hedges(self, total_capital: float) -> List[HedgeSuggestion]:
        """
        Scan all exposures and suggest hedges.
        
        Returns list of hedge suggestions sorted by risk reduction.
        """
        suggestions = []

        for event_id, exposure in self._exposures.items():
            if exposure.total_cost < self.MIN_EXPOSURE_FOR_HEDGE:
                continue

            # Check if we're over-concentrated in this event
            event_pct = exposure.total_cost / max(total_capital, 1)
            if event_pct < self.UNHEDGED_EXPOSURE_TRIGGER:
                continue

            # Get all markets in this event
            event_slugs = self._event_markets.get(event_id, [])
            if len(event_slugs) < 2:
                continue  # can't hedge single-outcome events

            # Calculate hedge
            hedge = self._calculate_hedge(event_id, exposure, event_slugs, total_capital)
            if hedge:
                suggestions.append(hedge)

        suggestions.sort(key=lambda s: s.risk_reduction, reverse=True)
        return suggestions

    def _calculate_hedge(self, event_id: str, exposure: EventExposure,
                           event_slugs: List[str],
                           total_capital: float) -> Optional[HedgeSuggestion]:
        """
        Calculate the optimal hedge for an event exposure.
        
        Strategy: if we're long on outcome A, buying outcome B reduces
        variance. If A + B ≈ 1.0, we have a "complete set" with
        guaranteed payout.
        """
        # Find markets we DON'T have positions in
        our_slugs = set(exposure.markets.keys())
        unhedged_slugs = [s for s in event_slugs if s not in our_slugs]

        if not unhedged_slugs:
            return None  # we have positions in all markets (already hedged)

        # Calculate what we need to hedge
        # If we're long YES on A at 60¢ with $60, we'd get $100 if YES wins, $0 if NO wins
        # Buying YES on B at 35¢ with $35 gives us $100 if B wins, $0 otherwise
        # Combined: we get money if either A or B wins

        total_our_cost = exposure.total_cost
        best_hedge_slug = None
        best_hedge_score = 0

        for slug in unhedged_slugs:
            data = self._market_data.get(slug)
            if not data:
                continue

            yes_price = data["yes_price"]
            liquidity = data["liquidity"]

            if yes_price < 0.05 or yes_price > 0.95:
                continue  # skip extremes
            if liquidity < 500:
                continue  # not enough liquidity

            # Hedge value: how much does buying this reduce our variance?
            # Higher price = more expensive hedge but more coverage
            # We want to maximize coverage per dollar

            # Simple hedge: buy enough to match our exposure
            hedge_cost = total_our_cost * 0.5  # hedge 50% of exposure
            hedge_shares = hedge_cost / yes_price if yes_price > 0 else 0

            # Risk reduction: what % of our max loss does this cover?
            # If we lose everything on our main position, the hedge pays off
            hedge_payout = hedge_shares  # if YES wins, each share pays $1
            our_max_loss = total_our_cost  # worst case: our outcome loses

            risk_reduction = min(1.0, hedge_payout / max(our_max_loss, 0.01))

            # Expected floor: minimum we'd get back if hedged
            # Either our position wins OR the hedge wins
            expected_floor = min(total_our_cost, hedge_payout)

            score = risk_reduction / max(yes_price, 0.01)  # coverage per dollar

            if score > best_hedge_score:
                best_hedge_score = score
                best_hedge_slug = slug

        if not best_hedge_slug:
            return None

        data = self._market_data[best_hedge_slug]
        hedge_cost = total_our_cost * 0.5
        hedge_shares = hedge_cost / data["yes_price"] if data["yes_price"] > 0 else 0

        return HedgeSuggestion(
            event_id=event_id,
            slug_to_hedge=best_hedge_slug,
            hedge_side="BUY",
            hedge_size=hedge_cost,
            reason=f"Hedge {exposure.total_cost:.0f} exposure across {len(exposure.markets)} markets in event {event_id[:20]}",
            risk_reduction=0.5,
            cost=hedge_cost,
            expected_floor=hedge_shares,
        )

    def get_event_risk(self, event_id: str) -> dict:
        """Get risk metrics for an event."""
        exposure = self._exposures.get(event_id)
        if not exposure:
            return {"exposure": 0, "markets": 0, "concentrated": False}

        num_markets = len(exposure.markets)
        event_slugs = self._event_markets.get(event_id, [])
        coverage = num_markets / max(len(event_slugs), 1)

        return {
            "exposure": exposure.total_cost,
            "markets": num_markets,
            "total_event_markets": len(event_slugs),
            "coverage": coverage,
            "concentrated": coverage < 0.5,  # less than half of outcomes covered
            "net_direction": exposure.net_direction,
        }

    def report(self) -> str:
        """Human-readable hedging report."""
        if not self._exposures:
            return "🛡️ No event exposures"

        lines = [f"\n🛡️ EVENT EXPOSURES", "─" * 50]
        for event_id, exposure in self._exposures.items():
            risk = self.get_event_risk(event_id)
            flag = "⚠️" if risk["concentrated"] else "✅"
            lines.append(f"  {flag} Event {event_id[:25]}")
            lines.append(f"    Exposure: ${exposure.total_cost:.0f} | Markets: {risk['markets']}/{risk['total_event_markets']} | "
                        f"Coverage: {risk['coverage']:.0%}")
            for slug, pos in exposure.markets.items():
                lines.append(f"      {slug[:30]:<30} | {pos['side']} | ${pos['cost']:.0f}")

        lines.append("─" * 50)
        return "\n".join(lines)
