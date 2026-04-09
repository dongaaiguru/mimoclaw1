"""Polymarket API client — Gamma + CLOB."""

import json
import time
import hmac
import hashlib
import logging
from typing import List, Optional
from dataclasses import dataclass

import aiohttp

from . import Config, Market

log = logging.getLogger("polyedge.api")


class PolymarketAPI:
    """Async API client for Polymarket Gamma + CLOB endpoints."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._session: Optional[aiohttp.ClientSession] = None

    async def open(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )

    async def close(self):
        if self._session:
            await self._session.close()

    def _sign(self, method: str, path: str, body: str = "") -> dict:
        ts = str(int(time.time() * 1000))
        msg = ts + method + path + body
        sig = hmac.new(
            self.cfg.api_secret.encode(),
            msg.encode(),
            hashlib.sha256
        ).hexdigest()
        return {
            "Content-Type": "application/json",
            "API-KEY": self.cfg.api_key,
            "API-SIGNATURE": sig,
            "API-TIMESTAMP": ts,
            "API-PASSPHRASE": self.cfg.api_passphrase,
        }

    # ── Market Data ─────────────────────────────────────────

    async def get_events(self, limit=500) -> list:
        """Fetch all active events from Gamma API."""
        try:
            async with self._session.get(
                f"{self.cfg.gamma_url}/events",
                params={"active": "true", "closed": "false", "limit": limit}
            ) as r:
                return await r.json() if r.status == 200 else []
        except Exception as e:
            log.error(f"API error: {e}")
            return []

    def parse_markets(self, events: list) -> List[Market]:
        """Parse Gamma API events into Market objects."""
        markets = []
        for ev in events:
            ev_slug = ev.get("slug", "")
            tags_raw = ev.get("tags", [])
            if tags_raw and isinstance(tags_raw[0], dict):
                tags = [t.get("label", "").lower() for t in tags_raw]
            else:
                tags = [str(t).lower() for t in tags_raw]

            for m in ev.get("markets", []):
                if m.get("closed") or not m.get("active"):
                    continue
                if not m.get("acceptingOrders", True):
                    continue
                try:
                    prices = json.loads(m.get("outcomePrices", "[0.5,0.5]"))
                    tokens = json.loads(m.get("clobTokenIds", "[]"))
                except:
                    continue
                if len(prices) < 2 or len(tokens) < 2:
                    continue

                liq = float(m.get("liquidityClob", 0) or 0)
                if liq < 500:
                    continue

                # Rewards
                rewards = m.get("clobRewards", [])
                has_rewards = len(rewards) > 0
                reward_pool = 0.0
                if has_rewards:
                    for r in rewards:
                        reward_pool += float(r.get("rewardsAmount", 0))

                markets.append(Market(
                    slug=m.get("slug", ""),
                    question=m.get("question", ""),
                    yes_price=float(prices[0]),
                    no_price=float(prices[1]),
                    volume=float(m.get("volume", 0)),
                    volume_24h=float(m.get("volume24hr", 0) or 0),
                    liquidity=liq,
                    fees_enabled=m.get("feesEnabled", False),
                    yes_token=tokens[0],
                    no_token=tokens[1] if len(tokens) > 1 else "",
                    event_slug=ev_slug,
                    spread=float(m.get("spread", 0)),
                    best_bid=float(m.get("bestBid", 0) or 0),
                    best_ask=float(m.get("bestAsk", 1) or 1),
                    last_trade=float(m.get("lastTradePrice", 0.5) or 0.5),
                    accepting_orders=m.get("acceptingOrders", True),
                    tags=tags,
                    has_rewards=has_rewards,
                    reward_pool=reward_pool,
                    rewards_min_size=float(m.get("rewardsMinSize", 0) or 0),
                    rewards_max_spread=float(m.get("rewardsMaxSpread", 3.5) or 3.5),
                ))
        return markets

    # ── Order Management ────────────────────────────────────

    async def place_order(self, token: str, side: str, price: float, size: float) -> dict:
        """Place a GTC limit order."""
        if not self.cfg.is_live:
            oid = f"paper_{int(time.time()*10000)}"
            log.info(f"  📝 PAPER: {side} ${size:.2f} @ {price:.4f}")
            return {"success": True, "order_id": oid}

        body = json.dumps({
            "asset_id": token,
            "side": side,
            "size": str(round(size, 2)),
            "price": str(round(price, 4)),
            "order_type": "GTC",
        })
        try:
            async with self._session.post(
                f"{self.cfg.clob_url}/orders",
                headers=self._sign("POST", "/orders", body),
                data=body
            ) as r:
                data = await r.json()
                return {**data, "success": r.status in (200, 201)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def cancel_order(self, order_id: str) -> dict:
        """Cancel an order."""
        if not self.cfg.is_live:
            return {"success": True}
        try:
            async with self._session.delete(
                f"{self.cfg.clob_url}/orders/{order_id}",
                headers=self._sign("DELETE", "/orders")
            ) as r:
                return {"success": r.status in (200, 204)}
        except Exception as e:
            return {"success": False, "error": str(e)}
