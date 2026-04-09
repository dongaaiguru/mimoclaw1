"""News Momentum Strategy.

Detects high-volume, wide-spread markets where informed flow may be occurring.
Rides the momentum with tight stops.

From scan: 85 markets with volume > 10x liquidity and spread > 4¢.
Best: UK IRGC terrorist designation (16¢ spread, 15x vol/liq).
"""

import math
import logging
from typing import List

from ..core import Config, Market, Signal

log = logging.getLogger("polyedge.strategies.momentum")


def scan(markets: List[Market], cfg: Config) -> List[Signal]:
    """Find momentum/news opportunities."""
    candidates = [
        m for m in markets
        if (not m.fees_enabled and
            m.liquidity >= 5000 and
            m.spread >= 0.04 and
            m.volume > 20000 and
            0.10 < m.yes_price < 0.90 and
            m.accepting_orders)
    ]

    # Score: volume/liquidity * spread
    scored = []
    for m in candidates:
        vol_liq = m.volume / max(m.liquidity, 1)
        score = vol_liq * m.spread
        scored.append((m, score, vol_liq))

    scored.sort(key=lambda x: x[1], reverse=True)

    signals = []
    for m, score, vol_liq in scored[:2]:
        size = min(cfg.capital * cfg.news_pct / 2, cfg.max_position)
        size = max(size, cfg.min_order)

        mid = (m.best_bid + m.best_ask) / 2
        if m.last_trade < mid - m.spread * 0.2:
            side = "BUY"
            price = round(m.best_bid + 0.005, 3)
        elif m.last_trade > mid + m.spread * 0.2:
            side = "SELL"
            price = round(m.best_ask - 0.005, 3)
        else:
            continue

        price = round(max(0.01, min(0.99, price)), 3)

        signals.append(Signal(
            strategy="NEWS_MOMENTUM",
            market=m.slug,
            action=side,
            price=price,
            size=size,
            edge=m.spread * 100,
            confidence=min(vol_liq / 20, 1.0),
            details=f"Vol/Liq: {vol_liq:.0f}x | Spread: {m.spread*100:.0f}¢ | ${m.volume:,.0f} vol"
        ))

    return signals
