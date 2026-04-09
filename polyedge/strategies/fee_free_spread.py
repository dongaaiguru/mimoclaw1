"""Fee-Free Spread Capture Strategy.

Best strategy for $100 capital. Zero fees = entire spread is profit.

From live scan (April 2026): 42 markets with ≥4¢ spread, fee-free, ≥$3K liquidity.
Top picks: USDT $200B (15.7¢), Fed rate bound (27¢), Abraham Accords (23¢)

Research insight (@defiance_cr):
- Orders closer to midpoint get exponentially higher reward scores
- Formula: S = ((max_spread - your_spread) / max_spread)² × multiplier
- Two-sided orders get ~3x score vs one-sided
"""

import math
import logging
from typing import List

from ..core import Config, Market, Signal

log = logging.getLogger("polyedge.strategies.spread")


def scan(markets: List[Market], cfg: Config) -> List[Signal]:
    """Find fee-free spread capture opportunities."""
    candidates = [
        m for m in markets
        if (not m.fees_enabled and
            m.spread >= cfg.ff_min_spread and
            m.liquidity >= cfg.ff_min_liquidity and
            0.05 < m.yes_price < 0.95 and
            m.best_bid > 0 and m.best_ask < 1 and
            m.accepting_orders)
    ]

    # Score: spread * sqrt(volume) * log(liquidity)
    scored = []
    for m in candidates:
        vol_score = math.sqrt(max(m.volume, 1))
        liq_score = math.log10(max(m.liquidity, 1))
        score = m.spread * vol_score * liq_score
        scored.append((m, score))

    scored.sort(key=lambda x: x[1], reverse=True)

    signals = []
    alloc = cfg.capital * cfg.fee_free_spread_pct
    per_market = min(alloc / 3, cfg.max_position)

    for m, score in scored[:5]:
        mid = (m.best_bid + m.best_ask) / 2
        our_bid = round(m.best_bid + cfg.ff_inside_spread, 3)
        our_ask = round(m.best_ask - cfg.ff_inside_spread, 3)

        if our_bid >= our_ask:
            continue

        size = max(per_market, cfg.min_order)

        signals.append(Signal(
            strategy="FEE_FREE_SPREAD",
            market=m.slug,
            action="BID",
            price=our_bid,
            size=size,
            edge=m.spread * 100,
            confidence=min(m.spread / 0.10, 1.0),
            details=f"Bid ${size:.0f} @ {our_bid:.3f} | Spread {m.spread*100:.0f}¢ | Liq ${m.liquidity:,.0f}"
        ))

        shares = size / our_bid
        signals.append(Signal(
            strategy="FEE_FREE_SPREAD",
            market=m.slug,
            action="ASK",
            price=our_ask,
            size=size,
            edge=m.spread * 100,
            confidence=min(m.spread / 0.10, 1.0),
            details=f"Ask {shares:.0f} shares @ {our_ask:.3f} | Profit: ${(our_ask-our_bid)*shares:.2f}"
        ))

    return signals
