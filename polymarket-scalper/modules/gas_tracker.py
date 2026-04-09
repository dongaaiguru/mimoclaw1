"""
Gas Tracker v5 — Polygon gas cost modeling for live trades.

Polymarket runs on Polygon PoS. Every on-chain operation costs gas:
- Place order: ~0 (off-chain via CLOB API, but settlement is on-chain)
- Cancel order: ~0 (off-chain)
- Split position: ~65,000 gas units
- Merge position: ~65,000 gas units
- Batch operations: ~30,000 gas per item

This module tracks current gas prices and models the actual cost of
each operation so the bot knows the TRUE profit margin after gas.
"""

import time
import logging
import aiohttp
from typing import Optional, Dict
from dataclasses import dataclass

LOG = logging.getLogger("scalper.gas")


@dataclass
class GasEstimate:
    """Estimated gas cost for an operation."""
    operation: str
    gas_units: int
    gas_price_gwei: float
    cost_matic: float
    cost_usd: float  # estimated via MATIC/USD price


class GasTracker:
    """
    Tracks Polygon gas prices and estimates trade costs.
    
    Polygon gas is cheap (typically < 1¢ per tx), but at high frequency
    it adds up. A bot doing 100 splits/day at 30 gwei with MATIC at $0.50
    costs ~$0.20/day — negligible for large capital, but worth tracking.
    """

    # Typical gas units for each operation
    GAS_UNITS = {
        "split": 65_000,
        "merge": 65_000,
        "order_place": 0,  # off-chain via CLOB
        "order_cancel": 0,  # off-chain
        "erc20_approve": 45_000,
        "batch_split": 80_000,  # batched is more efficient
    }

    # Polygon gas price tiers (gwei)
    GAS_TIERS = {
        "slow": 25,
        "standard": 30,
        "fast": 40,
        "instant": 60,
    }

    def __init__(self, matic_price_usd: float = 0.50):
        self.matic_price = matic_price_usd
        self._last_price_update = 0
        self._cached_gas_price: Optional[float] = None
        self._gas_price_cache_time = 0
        self._cache_ttl = 30  # seconds

        # Cost tracking
        self._total_gas_matic = 0.0
        self._total_gas_usd = 0.0
        self._operation_count: Dict[str, int] = {}

    async def update_matic_price(self, session: aiohttp.ClientSession):
        """Update MATIC/USD price from CoinGecko (free, no API key)."""
        now = time.time()
        if now - self._last_price_update < 300:  # update every 5 min
            return
        try:
            async with session.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "matic-network", "vs_currencies": "usd"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.matic_price = data["matic-network"]["usd"]
                    self._last_price_update = now
                    LOG.debug(f"MATIC price: ${self.matic_price:.3f}")
        except Exception:
            pass  # use cached price

    async def get_gas_price(self, session: aiohttp.ClientSession) -> float:
        """Get current gas price in gwei from Polygon RPC."""
        now = time.time()
        if self._cached_gas_price and (now - self._gas_price_cache_time) < self._cache_ttl:
            return self._cached_gas_price

        try:
            async with session.post(
                "https://polygon-rpc.com",
                json={"jsonrpc": "2.0", "method": "eth_gasPrice", "params": [], "id": 1},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    gas_wei = int(data.get("result", "0x77359400"), 16)  # default 20 gwei
                    self._cached_gas_price = gas_wei / 1e9  # convert to gwei
                    self._gas_price_cache_time = now
                    return self._cached_gas_price
        except Exception:
            pass

        # Fallback to standard tier
        return self.GAS_TIERS["standard"]

    async def estimate_cost(self, operation: str,
                              session: Optional[aiohttp.ClientSession] = None) -> GasEstimate:
        """
        Estimate the cost of an operation.
        
        Args:
            operation: one of 'split', 'merge', 'batch_split', etc.
            session: optional aiohttp session for price updates
        """
        gas_units = self.GAS_UNITS.get(operation, 50_000)

        if session:
            gas_price = await self.get_gas_price(session)
            await self.update_matic_price(session)
        else:
            gas_price = self.GAS_TIERS["standard"]

        cost_matic = (gas_units * gas_price) / 1e9
        cost_usd = cost_matic * self.matic_price

        return GasEstimate(
            operation=operation,
            gas_units=gas_units,
            gas_price_gwei=gas_price,
            cost_matic=cost_matic,
            cost_usd=cost_usd,
        )

    def record_operation(self, operation: str, cost_matic: float):
        """Record a completed operation's gas cost."""
        self._total_gas_matic += cost_matic
        self._total_gas_usd += cost_matic * self.matic_price
        self._operation_count[operation] = self._operation_count.get(operation, 0) + 1

    def is_split_profitable(self, split_amount: float, expected_profit: float,
                              gas_cost_usd: float) -> bool:
        """
        Check if a split operation is profitable after gas.
        
        split_amount: USDC being split
        expected_profit: expected profit from the trade using split tokens
        gas_cost_usd: estimated gas cost in USD
        """
        # Profit must exceed gas cost by at least 2x (safety margin)
        net_profit = expected_profit - gas_cost_usd
        return net_profit > 0 and net_profit > gas_cost_usd * 2

    def get_cost_per_trade(self, total_trades: int) -> float:
        """Average gas cost per trade in USD."""
        if total_trades <= 0:
            return 0
        return self._total_gas_usd / total_trades

    def report(self) -> str:
        """Human-readable gas report."""
        lines = [
            f"\n⛽ GAS COSTS",
            f"  MATIC price: ${self.matic_price:.3f}",
            f"  Total gas: {self._total_gas_matic:.6f} MATIC (${self._total_gas_usd:.4f})",
        ]
        for op, count in self._operation_count.items():
            lines.append(f"  {op}: {count} operations")
        return "\n".join(lines)
