"""Liquidity Rewards Strategy.

Polymarket pays $5M/month in rewards (April 2026).
EPL soccer: $10,000/game. La Liga: $3,300/game.

The reward formula QUADRATICALLY favors:
1. Two-sided orders (3x multiplier vs one-sided)
2. Tight spreads (closer to midpoint = exponentially more rewards)
3. Consistent quoting (sampled every minute, 10,080 samples/epoch)
"""

import logging
from typing import List

from ..core import Config, Market, Signal

log = logging.getLogger("polyedge.strategies.rewards")


def scan(markets: List[Market], cfg: Config) -> List[Signal]:
    """Find liquidity reward opportunities."""
    rewarded = [m for m in markets if m.has_rewards and m.liquidity > 1000]

    # Score: reward_pool / competition_proxy
    scored = []
    for m in rewarded:
        if m.rewards_max_spread <= 0:
            continue
        competition = m.volume / max(m.liquidity, 1)
        score = m.reward_pool / max(competition, 0.1)
        scored.append((m, score))

    scored.sort(key=lambda x: x[1], reverse=True)

    signals = []
    for m, score in scored[:3]:
        mid = (m.best_bid + m.best_ask) / 2
        max_spread_cents = m.rewards_max_spread

        our_bid = round(max(mid - 0.01, 0.01), 3)
        our_ask = round(min(mid + 0.01, 0.99), 3)

        size = min(cfg.capital * cfg.rewards_pct / 3, cfg.sports_max_position)
        size = max(size, max(cfg.min_order, m.rewards_min_size))

        signals.append(Signal(
            strategy="LIQUIDITY_REWARDS",
            market=m.slug,
            action="BID",
            price=our_bid,
            size=size,
            edge=max_spread_cents,
            confidence=0.7,
            details=f"Reward: ${m.reward_pool:.0f} | Max spread: {max_spread_cents}¢ | Bid @ {our_bid:.3f}"
        ))
        signals.append(Signal(
            strategy="LIQUIDITY_REWARDS",
            market=m.slug,
            action="ASK",
            price=our_ask,
            size=size,
            edge=max_spread_cents,
            confidence=0.7,
            details=f"Ask @ {our_ask:.3f} | Two-sided = 3x reward multiplier"
        ))

    return signals
