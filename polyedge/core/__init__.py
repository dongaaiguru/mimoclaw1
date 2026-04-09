"""Core configuration and data types."""

import os
import time
from dataclasses import dataclass, field
from typing import List
from enum import Enum

from dotenv import load_dotenv
load_dotenv()


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"


class Strategy(Enum):
    FEE_FREE_SPREAD = "FEE_FREE_SPREAD"
    LIQUIDITY_REWARDS = "LIQUIDITY_REWARDS"
    DEPENDENCY_ARB = "DEPENDENCY_ARB"
    MAKER_REBATES = "MAKER_REBATES"
    NEWS_MOMENTUM = "NEWS_MOMENTUM"


@dataclass
class Config:
    """Bot configuration — optimized for $100 with research-backed parameters."""
    # Capital
    capital: float = 100.0
    daily_target: float = 2.0  # $2/day = 2%

    # Strategy allocations (research-optimized)
    fee_free_spread_pct: float = 0.40   # $40 — safest, most consistent
    rewards_pct: float = 0.25           # $25 — sports liquidity rewards
    arb_pct: float = 0.20               # $20 — dependency arb
    news_pct: float = 0.15              # $15 — momentum/news scalping

    # Position sizing
    max_position: float = 20.0          # $20 max per market
    min_order: float = 5.00             # Polymarket minimum
    kelly_fraction: float = 0.25        # Conservative Kelly

    # Risk
    max_drawdown: float = 0.10          # 10% circuit breaker
    max_daily_trades: int = 50
    max_concurrent: int = 8

    # Fee-free spread capture
    ff_min_spread: float = 0.04         # 4¢ minimum
    ff_min_liquidity: float = 3000      # $3K minimum
    ff_inside_spread: float = 0.01      # Place 1¢ inside spread
    ff_max_order_age: int = 300         # 5 min max

    # Sports rewards (April 2026: $5M pool)
    sports_min_reward: float = 100
    sports_spread_target: float = 0.015
    sports_max_position: float = 15.0

    # Dependency arb
    arb_min_edge: float = 0.03          # 3% minimum
    arb_min_liquidity: float = 2000

    # Timing
    cycle_interval: int = 300           # 5 min between cycles
    order_check_interval: int = 15      # 15s order checks

    # API
    api_key: str = field(default_factory=lambda: os.getenv("POLYMARKET_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("POLYMARKET_API_SECRET", ""))
    api_passphrase: str = field(default_factory=lambda: os.getenv("POLYMARKET_API_PASSPHRASE", ""))
    private_key: str = field(default_factory=lambda: os.getenv("POLYMARKET_PRIVATE_KEY", ""))

    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    @property
    def is_live(self) -> bool:
        return bool(self.api_key and self.api_secret and self.api_passphrase)


@dataclass
class Market:
    """Parsed market data from Gamma API."""
    slug: str
    question: str
    yes_price: float
    no_price: float
    volume: float
    volume_24h: float
    liquidity: float
    fees_enabled: bool
    yes_token: str
    no_token: str
    event_slug: str
    spread: float
    best_bid: float
    best_ask: float
    last_trade: float
    accepting_orders: bool
    tags: List[str] = field(default_factory=list)
    has_rewards: bool = False
    reward_pool: float = 0.0
    rewards_min_size: float = 0.0
    rewards_max_spread: float = 3.5


@dataclass
class Signal:
    """Trading signal from a strategy."""
    strategy: str
    market: str
    action: str          # "BID", "ASK", "BUY", "SELL"
    price: float
    size: float
    edge: float          # Expected edge in % or cents
    confidence: float    # 0-1
    details: str = ""
    pair_market: str = ""  # For arb legs
    pair_action: str = ""


@dataclass
class Position:
    """Open position."""
    slug: str
    side: str
    entry_price: float
    size: float
    strategy: str
    opened: float = field(default_factory=time.time)
    target_price: float = 0.0
    stop_price: float = 0.0
