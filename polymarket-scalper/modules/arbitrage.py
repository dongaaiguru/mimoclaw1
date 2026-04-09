"""
Arbitrage Engine v5 — Cross-market and neg-risk arbitrage.

Polymarket has structural pricing inefficiencies:

1. **Neg-Risk Arbitrage**: On multi-outcome events, the sum of all outcome
   prices should equal 1.0. If YES_A + YES_B + YES_C < 0.95, buy all three
   for a guaranteed profit at resolution.

2. **YES/NO Arbitrage**: For binary markets, YES + NO should = 1.0.
   If YES + NO < 0.97, buy both for guaranteed profit.
   If YES + NO > 1.03, sell both (if you own them) for guaranteed profit.

3. **Cross-Event Arbitrage**: Related markets should be correlated.
   If "Will Trump run?" goes to 90% but "Will Trump win?" is at 30%,
   there's a structural mispricing.

4. **Fee Arbitrage**: Some markets have fees, some don't. A fee-free
   market might be 2¢ cheaper than a fee market on the same event.

This module detects and executes these arbitrage opportunities.
"""

import time
import logging
import math
from typing import Dict, List, Optional, Tuple, Set
from dataclasses import dataclass, field
from collections import defaultdict

LOG = logging.getLogger("scalper.arb")


@dataclass
class ArbOpportunity:
    """An arbitrage opportunity."""
    type: str  # "neg_risk", "yes_no", "cross_event", "fee_arb"
    description: str
    markets: List[str]  # slugs involved
    sides: List[str]    # "BUY" or "SELL" for each market
    prices: List[float] # current prices
    total_cost: float   # total capital required
    guaranteed_profit: float  # profit if all resolved correctly
    profit_pct: float   # profit as percentage of capital
    confidence: float   # 0-1, how confident we are this is a real arb
    timestamp: float = field(default_factory=time.time)
    executed: bool = False


class ArbitrageEngine:
    """
    Detects and manages arbitrage opportunities across Polymarket.
    
    Detection is passive (scans existing market data) — no additional
    API calls needed beyond what the bot already does.
    """

    # ─── Configuration ───────────────────────────────────────

    # Minimum profit threshold to bother with (after gas/slippage)
    MIN_PROFIT_USD = 0.50
    MIN_PROFIT_PCT = 0.5  # 0.5% minimum

    # Maximum capital per arb (don't go all-in on one arb)
    MAX_ARB_CAPITAL_PCT = 0.10  # 10% of total capital

    # How fresh an opportunity must be (seconds)
    MAX_OPPORTUNITY_AGE = 30

    def __init__(self):
        # slug → {yes_price, no_price, event_id, neg_risk, fees_enabled}
        self._market_data: Dict[str, dict] = {}
        # event_id → [slug1, slug2, ...]
        self._event_markets: Dict[str, List[str]] = defaultdict(list)
        # Active opportunities
        self._opportunities: List[ArbOpportunity] = []
        # Executed arbs for tracking
        self._executed: List[ArbOpportunity] = []

    def update_market(self, slug: str, yes_price: float, no_price: float,
                       event_id: str = "", neg_risk: bool = False,
                       fees_enabled: bool = False, spread: float = 0.05,
                       liquidity: float = 1000):
        """Update market data for arb detection."""
        self._market_data[slug] = {
            "yes_price": yes_price,
            "no_price": no_price,
            "event_id": event_id,
            "neg_risk": neg_risk,
            "fees_enabled": fees_enabled,
            "spread": spread,
            "liquidity": liquidity,
            "updated": time.time(),
        }
        if event_id and slug not in self._event_markets[event_id]:
            self._event_markets[event_id].append(slug)

    def scan(self, available_capital: float) -> List[ArbOpportunity]:
        """
        Scan all markets for arbitrage opportunities.
        
        Returns sorted list of opportunities (best first).
        """
        opportunities = []

        # 1. YES/NO arbitrage (binary markets)
        opportunities.extend(self._scan_yes_no_arb())

        # 2. Neg-risk arbitrage (multi-outcome events)
        opportunities.extend(self._scan_neg_risk_arb())

        # 3. Cross-event correlation arbitrage
        opportunities.extend(self._scan_cross_event_arb(available_capital))

        # Sort by profit percentage (best first)
        opportunities.sort(key=lambda o: o.profit_pct, reverse=True)

        # Filter by minimum thresholds
        max_capital = available_capital * self.MAX_ARB_CAPITAL_PCT
        opportunities = [
            o for o in opportunities
            if o.guaranteed_profit >= self.MIN_PROFIT_USD
            and o.profit_pct >= self.MIN_PROFIT_PCT
            and o.total_cost <= max_capital
        ]

        self._opportunities = opportunities
        return opportunities

    def _scan_yes_no_arb(self) -> List[ArbOpportunity]:
        """
        Scan for YES/NO price sum != 1.0.
        
        If YES + NO < 0.97: Buy both → guaranteed $1.00 at resolution → profit = 1 - sum
        If YES + NO > 1.03: Sell both (need to own) → profit = sum - 1
        """
        opps = []

        for slug, data in self._market_data.items():
            if data["fees_enabled"]:
                continue  # fees eat into arb profit

            yes = data["yes_price"]
            no = data["no_price"]
            price_sum = yes + no

            # Below 1.0: buy both
            if price_sum < 0.97:
                cost = 1.0  # buying 1 share of each costs price_sum, pays 1.0
                profit = 1.0 - price_sum
                profit_pct = (profit / price_sum) * 100

                opps.append(ArbOpportunity(
                    type="yes_no",
                    description=f"YES+NO = {price_sum:.3f} < 1.0 → buy both for ${profit:.3f} profit",
                    markets=[slug, slug],
                    sides=["BUY", "BUY"],
                    prices=[yes, no],
                    total_cost=price_sum,
                    guaranteed_profit=profit,
                    profit_pct=profit_pct,
                    confidence=0.95,  # very high confidence — structural guarantee
                ))

            # Above 1.0: sell both (if we own the tokens)
            elif price_sum > 1.03:
                profit = price_sum - 1.0
                profit_pct = (profit / 1.0) * 100

                opps.append(ArbOpportunity(
                    type="yes_no",
                    description=f"YES+NO = {price_sum:.3f} > 1.0 → sell both for ${profit:.3f} profit",
                    markets=[slug, slug],
                    sides=["SELL", "SELL"],
                    prices=[yes, no],
                    total_cost=0,  # selling, not buying
                    guaranteed_profit=profit,
                    profit_pct=profit_pct,
                    confidence=0.90,  # need to own tokens
                ))

        return opps

    def _scan_neg_risk_arb(self) -> List[ArbOpportunity]:
        """
        Scan for neg-risk event pricing inefficiencies.
        
        In neg-risk events, all outcomes are YES/NO pairs on the same condition.
        The sum of all YES prices should = 1.0.
        
        If sum < 0.95: buy all YES outcomes → guaranteed profit
        """
        opps = []

        for event_id, slugs in self._event_markets.items():
            if len(slugs) < 3:
                continue  # neg-risk arb only works with 3+ outcomes

            # Get prices for all outcomes
            outcomes = []
            for slug in slugs:
                data = self._market_data.get(slug)
                if not data:
                    continue
                if data["fees_enabled"]:
                    continue
                outcomes.append((slug, data["yes_price"], data["liquidity"]))

            if len(outcomes) < 3:
                continue

            # Sum all YES prices
            total_price = sum(o[1] for o in outcomes)
            min_liquidity = min(o[2] for o in outcomes)

            # Should be ~1.0 for neg-risk
            if total_price < 0.95 and min_liquidity > 500:
                profit = 1.0 - total_price
                profit_pct = (profit / total_price) * 100

                # Size limited by least liquid market
                max_size = min_liquidity * 0.1  # don't exceed 10% of any market

                opps.append(ArbOpportunity(
                    type="neg_risk",
                    description=f"Event {event_id[:20]}: {len(outcomes)} outcomes sum={total_price:.3f} → ${profit:.3f} profit",
                    markets=[o[0] for o in outcomes],
                    sides=["BUY"] * len(outcomes),
                    prices=[o[1] for o in outcomes],
                    total_cost=total_price * max_size / 1.0,  # normalized
                    guaranteed_profit=profit * max_size,
                    profit_pct=profit_pct,
                    confidence=0.90,
                ))

        return opps

    def _scan_cross_event_arb(self, available_capital: float) -> List[ArbOpportunity]:
        """
        Scan for cross-event correlation mispricings.
        
        Example: If "Trump wins primary" = 80% but "Trump wins election" = 40%,
        the primary should be ≤ the election price (you can't win without winning
        the primary first). If primary > election, that's a mispricing.
        """
        opps = []

        # Find markets with overlapping keywords
        market_keywords: Dict[str, Set[str]] = {}
        for slug, data in self._market_data.items():
            # Use slug words as keywords
            words = set(slug.replace("-", " ").split())
            # Filter short/common words
            words = {w for w in words if len(w) > 3}
            market_keywords[slug] = words

        # Compare pairs
        slugs = list(self._market_data.keys())
        for i in range(len(slugs)):
            for j in range(i + 1, min(i + 20, len(slugs))):  # limit comparisons
                slug_a, slug_b = slugs[i], slugs[j]
                data_a = self._market_data.get(slug_a)
                data_b = self._market_data.get(slug_b)
                if not data_a or not data_b:
                    continue

                # Check keyword overlap
                overlap = market_keywords.get(slug_a, set()) & market_keywords.get(slug_b, set())
                if len(overlap) < 2:
                    continue

                # Check for logical dependency
                # If A implies B, then price_A <= price_B
                price_a = data_a["yes_price"]
                price_b = data_b["yes_price"]

                # Look for conditional pricing anomalies
                # This is heuristic — real logic would need event semantics
                if abs(price_a - price_b) > 0.15:
                    # One is much higher than the other on related events
                    # Could be a mispricing or could be legitimate
                    # Only flag if both have decent liquidity
                    if data_a["liquidity"] > 1000 and data_b["liquidity"] > 1000:
                        spread = abs(price_a - price_b)
                        opps.append(ArbOpportunity(
                            type="cross_event",
                            description=f"Correlated markets {spread:.0%} apart: {slug_a[:25]} ({price_a:.0%}) vs {slug_b[:25]} ({price_b:.0%})",
                            markets=[slug_a, slug_b],
                            sides=["BUY", "SELL"] if price_a < price_b else ["SELL", "BUY"],
                            prices=[price_a, price_b],
                            total_cost=available_capital * 0.05,
                            guaranteed_profit=0,  # not guaranteed — needs judgment
                            profit_pct=spread * 100,
                            confidence=0.4,  # low confidence — heuristic
                        ))

        return opps

    def get_best_opportunity(self) -> Optional[ArbOpportunity]:
        """Get the best current arbitrage opportunity."""
        now = time.time()
        for opp in self._opportunities:
            if not opp.executed and (now - opp.timestamp) < self.MAX_OPPORTUNITY_AGE:
                return opp
        return None

    def mark_executed(self, opp: ArbOpportunity):
        """Mark an opportunity as executed."""
        opp.executed = True
        self._executed.append(opp)
        LOG.info(f"🔄 ARB EXECUTED | {opp.type} | {opp.description[:60]} | profit=${opp.guaranteed_profit:.3f}")

    def get_stats(self) -> dict:
        """Get arbitrage statistics."""
        return {
            "opportunities_found": len(self._opportunities),
            "opportunities_executed": len(self._executed),
            "total_profit": sum(o.guaranteed_profit for o in self._executed),
            "active": len([o for o in self._opportunities if not o.executed]),
        }

    def report(self) -> str:
        """Human-readable arb report."""
        lines = [f"\n🔄 ARBITRAGE", "─" * 50]

        active = [o for o in self._opportunities if not o.executed]
        if active:
            lines.append(f"  Active opportunities: {len(active)}")
            for opp in active[:5]:
                lines.append(f"  [{opp.type}] {opp.description[:50]}")
                lines.append(f"    profit=${opp.guaranteed_profit:.3f} ({opp.profit_pct:.1f}%) confidence={opp.confidence:.0%}")
        else:
            lines.append("  No active opportunities")

        if self._executed:
            total = sum(o.guaranteed_profit for o in self._executed)
            lines.append(f"\n  Executed: {len(self._executed)} arbs | Total profit: ${total:.3f}")

        lines.append("─" * 50)
        return "\n".join(lines)
