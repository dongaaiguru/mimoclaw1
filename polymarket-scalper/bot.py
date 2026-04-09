#!/usr/bin/env python3
"""
Polymarket Scalper — High-velocity spread capture bot.

Places aggressive limit orders inside the spread on fee-free markets.
Cancels & reprices every N seconds. Auto-exits positions held > MAX_HOLD.
Recycles capital as fast as possible.

Usage:
  python3 bot.py --scan          # Find scalping targets
  python3 bot.py --paper         # Paper trade (simulated fills)
  python3 bot.py --live          # Live trading (needs .env)
  python3 bot.py --live --capital 100 --per-order 10
"""

import asyncio
import json
import math
import os
import sys
import time
import logging
import signal
import argparse
import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from datetime import datetime, timezone

import aiohttp
import websockets
from dotenv import load_dotenv

# ══════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════

load_dotenv()

LOG = logging.getLogger("scalper")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scalper.log"),
    ],
)

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass
class Config:
    private_key: str = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    funder: str = os.getenv("POLYMARKET_FUNDER", "")
    sig_type: int = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "2"))
    capital: float = float(os.getenv("CAPITAL", "100"))
    max_exposure_pct: float = float(os.getenv("MAX_EXPOSURE_PCT", "50")) / 100
    per_order: float = float(os.getenv("PER_ORDER", "10"))
    max_concurrent: int = int(os.getenv("MAX_CONCURRENT", "5"))
    circuit_breaker_pct: float = float(os.getenv("CIRCUIT_BREAKER_PCT", "10")) / 100
    max_hold_sec: int = int(os.getenv("MAX_HOLD_SECONDS", "300"))
    spread_target: float = float(os.getenv("SPREAD_CAPTURE_TARGET", "0.02"))
    reprice_sec: int = int(os.getenv("REPRICE_INTERVAL", "30"))
    min_spread: float = 0.03        # 3¢ minimum spread to trade
    min_liquidity: float = 2000.0
    min_volume: float = 1000.0
    max_markets: int = 10           # watchlist size
    max_orders_per_market: int = 1  # one order per side per market

    @property
    def is_live(self) -> bool:
        return bool(self.private_key and self.funder)

    @property
    def max_exposure(self) -> float:
        return self.capital * self.max_exposure_pct


# ══════════════════════════════════════════════════════════════
# DATA TYPES
# ══════════════════════════════════════════════════════════════

@dataclass
class Market:
    slug: str
    question: str
    yes_price: float
    no_price: float
    spread: float
    volume: float
    liquidity: float
    yes_token: str
    no_token: str
    best_bid: float = 0.0
    best_ask: float = 1.0
    fees_enabled: bool = False
    accepting: bool = True
    last_ws_update: float = 0.0


@dataclass
class Order:
    id: str                    # local id
    exchange_id: str = ""      # polymarket order id
    slug: str = ""
    token: str = ""
    side: str = ""             # BUY or SELL
    price: float = 0.0
    size: float = 0.0          # USD amount
    shares: float = 0.0        # number of shares
    status: str = "pending"    # pending, live, filled, canceled
    created: float = field(default_factory=time.time)
    filled: float = 0.0
    fill_price: float = 0.0


@dataclass
class Position:
    slug: str
    token: str
    side: str                  # LONG (bought YES)
    entry_price: float
    shares: float
    cost: float                # USD spent
    opened: float = field(default_factory=time.time)
    exit_order_id: str = ""


@dataclass
class Trade:
    slug: str
    entry_price: float
    exit_price: float
    shares: float
    pnl: float
    hold_sec: float
    reason: str
    ts: float = field(default_factory=time.time)


# ══════════════════════════════════════════════════════════════
# MARKET DISCOVERY
# ══════════════════════════════════════════════════════════════

async def discover_markets(cfg: Config) -> List[Market]:
    """Fetch and filter markets suitable for scalping."""
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"{GAMMA_URL}/events",
            params={"active": "true", "closed": "false", "limit": 500},
        ) as r:
            events = await r.json()

    all_markets = []
    for ev in events:
        for m in ev.get("markets", []):
            if m.get("closed") or not m.get("active"):
                continue
            if not m.get("acceptingOrders", True):
                continue

            try:
                prices = json.loads(m.get("outcomePrices", "[0.5,0.5]"))
                tokens = json.loads(m.get("clobTokenIds", "[]"))
            except (json.JSONDecodeError, IndexError):
                continue

            if len(prices) < 2 or len(tokens) < 2:
                continue

            liq = float(m.get("liquidityClob", 0) or 0)
            vol = float(m.get("volume", 0))
            spread = float(m.get("spread", 0))
            yp = float(prices[0])
            fees = m.get("feesEnabled", False)

            # Filters
            if liq < cfg.min_liquidity:
                continue
            if vol < cfg.min_volume:
                continue
            if spread < cfg.min_spread:
                continue
            if yp < 0.05 or yp > 0.95:
                continue
            if fees:
                continue  # fee-free only

            all_markets.append(Market(
                slug=m.get("slug", ""),
                question=m.get("question", ""),
                yes_price=yp,
                no_price=float(prices[1]),
                spread=spread,
                volume=vol,
                liquidity=liq,
                yes_token=tokens[0],
                no_token=tokens[1],
                best_bid=float(m.get("bestBid", 0) or 0),
                best_ask=float(m.get("bestAsk", 1) or 1),
                fees_enabled=fees,
            ))

    # Score: spread * sqrt(volume) * log(liquidity)
    import math
    for m in all_markets:
        m._score = m.spread * math.sqrt(max(m.volume, 1)) * math.log10(max(m.liquidity, 1))

    all_markets.sort(key=lambda m: m._score, reverse=True)
    return all_markets[:cfg.max_markets]


# ══════════════════════════════════════════════════════════════
# WEBSOCKET FEED
# ══════════════════════════════════════════════════════════════

class Feed:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.books: Dict[str, dict] = {}
        self._running = False
        self._callbacks: List = []
        self._ws = None

    def on_update(self, cb):
        self._callbacks.append(cb)

    async def start(self, tokens: List[str]):
        self._running = True
        delay = 1
        while self._running:
            try:
                async with websockets.connect(WS_URL, ping_interval=15) as ws:
                    self._ws = ws
                    delay = 1
                    # Subscribe in batches of 50
                    for i in range(0, len(tokens), 50):
                        batch = tokens[i:i+50]
                        await ws.send(json.dumps({
                            "assets_ids": batch,
                            "type": "market",
                            "custom_feature_enabled": True,
                        }))
                    LOG.info(f"WS: subscribed to {len(tokens)} tokens")

                    async for msg in ws:
                        if msg == "PONG":
                            continue
                        data = json.loads(msg) if isinstance(msg, str) else None
                        if not data:
                            continue
                        items = data if isinstance(data, list) else [data]
                        for item in items:
                            if not isinstance(item, dict):
                                continue
                            aid = item.get("asset_id", "")
                            if not aid:
                                continue
                            book = self.books.setdefault(aid, {"bids": [], "asks": []})
                            t = item.get("type", "")
                            if t == "book":
                                book["bids"] = sorted(
                                    [(float(p), float(s)) for p, s in item.get("bids", [])],
                                    reverse=True
                                )
                                book["asks"] = sorted(
                                    [(float(p), float(s)) for p, s in item.get("asks", [])]
                                )
                            elif t == "best_bid_ask":
                                bb, ba = item.get("best_bid"), item.get("best_ask")
                                if bb:
                                    book["bids"] = [(float(bb[0]), float(bb[1]))]
                                if ba:
                                    book["asks"] = [(float(ba[0]), float(ba[1]))]
                            elif t == "last_trade_price":
                                book["last_trade"] = float(item.get("price", 0))
                            for cb in self._callbacks:
                                await cb(aid, book)

            except Exception as e:
                LOG.warning(f"WS error: {e}, reconnect in {delay}s")
                if self._running:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()

    def best(self, token: str) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
        """Returns (best_bid, best_ask, bid_size, ask_size)."""
        book = self.books.get(token)
        if not book:
            return None, None, None, None
        bb = book["bids"][0] if book["bids"] else (None, None)
        ba = book["asks"][0] if book["asks"] else (None, None)
        return bb[0], ba[0], bb[1], ba[1]


# ══════════════════════════════════════════════════════════════
# ORDER MANAGER
# ══════════════════════════════════════════════════════════════

class OrderManager:
    def __init__(self, cfg: Config, paper: bool = True):
        self.cfg = cfg
        self.paper = paper
        self.orders: Dict[str, Order] = {}
        self.positions: Dict[str, Position] = {}  # slug -> position
        self.trades: List[Trade] = []
        self.capital = cfg.capital
        self.peak = cfg.capital
        self.client = None  # py-clob-client

    async def init_client(self):
        """Initialize the Polymarket client."""
        if not self.cfg.is_live:
            LOG.info("No API keys — paper mode")
            return
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import OrderType

            self.client = ClobClient(
                CLOB_URL,
                key=self.cfg.private_key,
                chain_id=137,
                signature_type=self.cfg.sig_type,
                funder=self.cfg.funder,
            )
            creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(creds)
            LOG.info(f"Polymarket client initialized | funder={self.cfg.funder[:10]}...")
        except Exception as e:
            LOG.error(f"Failed to init client: {e}")
            self.client = None

    @property
    def exposed(self) -> float:
        return sum(p.cost for p in self.positions.values())

    @property
    def free_capital(self) -> float:
        return self.capital - self.exposed

    @property
    def drawdown(self) -> float:
        return max(0, (self.peak - self.capital) / self.peak)

    @property
    def daily_pnl(self) -> float:
        return self.capital - self.cfg.capital

    def can_enter(self) -> Tuple[bool, str]:
        if self.drawdown >= self.cfg.circuit_breaker_pct:
            return False, "CIRCUIT_BREAKER"
        # Count BOTH positions AND open orders against concurrent limit
        open_orders = sum(1 for o in self.orders.values() if o.status == "live" and o.side == "BUY")
        if len(self.positions) + open_orders >= self.cfg.max_concurrent:
            return False, "MAX_CONCURRENT"
        if self.exposed >= self.cfg.max_exposure:
            return False, "MAX_EXPOSURE"
        if self.free_capital < self.cfg.per_order:
            return False, "INSUFFICIENT_CAPITAL"
        return True, "OK"

    async def place_limit(self, slug: str, token: str, side: str, price: float, size_usd: float) -> Optional[Order]:
        """Place a GTC limit order."""
        price = round(price, 4)
        shares = round(size_usd / price, 2)

        order = Order(
            id=f"{slug}_{side}_{int(time.time()*1000)}",
            slug=slug,
            token=token,
            side=side,
            price=price,
            size=size_usd,
            shares=shares,
        )

        if self.paper:
            order.status = "live"
            order.exchange_id = f"paper_{order.id}"
            LOG.info(f"📝 PAPER ORDER | {side} {shares:.0f} shares @ ${price:.4f} on {slug[:40]}")
            self.orders[order.id] = order
            return order

        if not self.client:
            return None

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            side_const = BUY if side == "BUY" else SELL
            order_args = OrderArgs(
                token_id=token,
                price=price,
                size=shares,
                side=side_const,
            )
            signed = self.client.create_order(order_args)
            resp = self.client.post_order(signed, OrderType.GTC)

            if resp:
                order.exchange_id = resp.get("orderID", resp.get("order_id", ""))
                order.status = "live"
                LOG.info(f"✅ LIVE ORDER | {side} {shares:.0f} shares @ ${price:.4f} on {slug[:40]} | ID={order.exchange_id[:12]}")
                self.orders[order.id] = order
                return order
            else:
                LOG.error(f"❌ Order rejected: {resp}")
                return None

        except Exception as e:
            LOG.error(f"❌ Order error: {e}")
            return None

    async def cancel_order(self, order: Order):
        """Cancel an order."""
        order.status = "canceled"
        if self.paper or not order.exchange_id.startswith("0x"):
            return
        if not self.client:
            return
        try:
            self.client.cancel(order.exchange_id)
            LOG.info(f"🚫 Canceled {order.exchange_id[:12]} on {order.slug[:30]}")
        except Exception as e:
            LOG.error(f"Cancel error: {e}")

    async def cancel_all(self):
        """Cancel all live orders."""
        for oid, order in list(self.orders.items()):
            if order.status == "live":
                await self.cancel_order(order)
        LOG.info("All orders canceled")

    def fill_order(self, order: Order, fill_price: float):
        """Mark order as filled and open position."""
        order.status = "filled"
        order.fill_price = fill_price
        order.filled = time.time()

        cost = order.shares * fill_price
        self.capital -= cost

        if order.side == "BUY":
            self.positions[order.slug] = Position(
                slug=order.slug,
                token=order.token,
                side="LONG",
                entry_price=fill_price,
                shares=order.shares,
                cost=cost,
            )
            LOG.info(f"🟢 FILLED BUY | {order.shares:.0f} @ ${fill_price:.4f} | {order.slug[:40]}")
        else:
            # SELL = closing a position
            pos = self.positions.pop(order.slug, None)
            if pos:
                pnl = (fill_price - pos.entry_price) * pos.shares
                self.capital += pos.shares * fill_price
                self.peak = max(self.peak, self.capital)
                hold = time.time() - pos.opened
                self.trades.append(Trade(
                    slug=order.slug,
                    entry_price=pos.entry_price,
                    exit_price=fill_price,
                    shares=pos.shares,
                    pnl=pnl,
                    hold_sec=hold,
                    reason="filled",
                ))
                emoji = "🟢" if pnl > 0 else "🔴"
                LOG.info(f"{emoji} FILLED SELL | {pos.shares:.0f} @ ${fill_price:.4f} | PnL=${pnl:+.3f} | {order.slug[:40]}")

    def force_exit_position(self, slug: str, exit_price: float, reason: str):
        """Force exit a position (timeout, circuit breaker, etc)."""
        pos = self.positions.pop(slug, None)
        if not pos:
            return
        pnl = (exit_price - pos.entry_price) * pos.shares
        self.capital += pos.shares * exit_price
        self.peak = max(self.peak, self.capital)
        hold = time.time() - pos.opened
        self.trades.append(Trade(
            slug=slug,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            shares=pos.shares,
            pnl=pnl,
            hold_sec=hold,
            reason=reason,
        ))
        emoji = "🟢" if pnl > 0 else "🔴"
        LOG.info(f"{emoji} FORCE EXIT | {pos.shares:.0f} @ ${exit_price:.4f} | PnL=${pnl:+.3f} | {reason} | {slug[:40]}")

    def check_timeouts(self, market_prices: Dict[str, float]):
        """Force exit positions held too long."""
        now = time.time()
        for slug, pos in list(self.positions.items()):
            if now - pos.opened > self.cfg.max_hold_sec:
                price = market_prices.get(slug, pos.entry_price)
                self.force_exit_position(slug, price, "timeout")

    def summary(self) -> str:
        wins = [t for t in self.trades if t.pnl > 0]
        losses = [t for t in self.trades if t.pnl <= 0]
        total_pnl = sum(t.pnl for t in self.trades)
        wr = len(wins) / max(1, len(self.trades))
        return (
            f"Capital=${self.capital:.2f} ({self.daily_pnl:+.2f}) | "
            f"Pos={len(self.positions)} | "
            f"Trades={len(self.trades)} ({wr:.0%} WR) | "
            f"PnL=${total_pnl:+.3f} | "
            f"DD={self.drawdown:.1%}"
        )


# ══════════════════════════════════════════════════════════════
# SCALPING ENGINE
# ══════════════════════════════════════════════════════════════

class Scalper:
    def __init__(self, cfg: Config, paper: bool = True):
        self.cfg = cfg
        self.paper = paper
        self.om = OrderManager(cfg, paper)
        self.feed = Feed(cfg)
        self.markets: Dict[str, Market] = {}  # slug -> Market
        self.token_to_slug: Dict[str, str] = {}
        self.running = False
        self.tick = 0
        self._last_reprice = 0

    async def start(self, mode: str = "paper"):
        LOG.info("=" * 60)
        LOG.info(f"  SCALPER | ${self.cfg.capital:.0f} | {mode.upper()}")
        LOG.info(f"  Exposed: {self.cfg.max_exposure_pct:.0%} | Per order: ${self.cfg.per_order:.0f}")
        LOG.info(f"  Max concurrent: {self.cfg.max_concurrent} | Reprice: {self.cfg.reprice_sec}s")
        LOG.info(f"  Max hold: {self.cfg.max_hold_sec}s | Circuit breaker: {self.cfg.circuit_breaker_pct:.0%}")
        LOG.info("=" * 60)

        # Discover markets
        LOG.info("🔍 Discovering markets...")
        market_list = await discover_markets(self.cfg)
        if not market_list:
            LOG.error("No suitable markets found!")
            return

        self.markets = {m.slug: m for m in market_list}
        for m in market_list:
            self.token_to_slug[m.yes_token] = m.slug
            self.token_to_slug[m.no_token] = m.slug + "_NO"

        LOG.info(f"📊 {len(self.markets)} markets selected:")
        for m in market_list:
            LOG.info(f"  • {m.question[:55]} | {m.spread*100:.1f}¢ | ${m.liquidity:,.0f} liq | ${m.volume:,.0f} vol")

        # Init client
        await self.om.init_client()

        # Start WebSocket
        tokens = [m.yes_token for m in market_list if m.yes_token]
        ws_task = asyncio.create_task(self.feed.start(tokens))

        # Register WS callback
        self.feed.on_update(self._on_book_update)

        # Main loop
        self.running = True
        LOG.info("🚀 Scalper running — Ctrl+C to stop\n")

        try:
            while self.running:
                await self._tick()
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            LOG.info("\n⏹ Shutting down...")
        finally:
            self.running = False
            await self.om.cancel_all()
            await self.feed.stop()
            ws_task.cancel()
            self._final_report()

    async def _tick(self):
        """One second of the scalping engine."""
        self.tick += 1
        now = time.time()

        # ═══ CANCEL STALE ORDERS (sitting > 2 min unfilled) ═══
        for oid, order in list(self.om.orders.items()):
            if order.status == "live" and (now - order.created) > 120:
                await self.om.cancel_order(order)
                LOG.info(f"⏰ STALE ORDER | {order.side} @ ${order.price:.4f} | {order.slug[:40]} | age={now-order.created:.0f}s")

        # Check position timeouts every 10s
        if self.tick % 10 == 0:
            prices = {slug: m.yes_price for slug, m in self.markets.items()}
            self.om.check_timeouts(prices)

        # Reprice orders every N seconds
        if now - self._last_reprice > self.cfg.reprice_sec:
            await self._reprice()
            self._last_reprice = now

        # Paper fill check every tick
        if self.paper:
            await self._paper_fill_check()

        # Status every 60s
        if self.tick % 60 == 0:
            live_orders = sum(1 for o in self.om.orders.values() if o.status == "live")
            LOG.info(f"[T+{self.tick}s] {self.om.summary()} | Live orders: {live_orders}")

    async def _on_book_update(self, token: str, book: dict):
        """React to WebSocket book updates — cancel stale orders immediately."""
        slug = self.token_to_slug.get(token)
        if not slug or slug.endswith("_NO"):
            return

        market = self.markets.get(slug)
        if not market:
            return

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        if not bids or not asks:
            return

        bb = bids[0][0]
        ba = asks[0][0]
        mid = (bb + ba) / 2
        spread = ba - bb
        old_mid = market.yes_price

        # Update market
        market.best_bid = bb
        market.best_ask = ba
        market.yes_price = mid
        market.spread = spread
        market.last_ws_update = time.time()

        # ═══ REACTIVE CANCELLATION ═══
        # If mid moved > 1¢ since we placed an order, cancel immediately
        # and reprice. Don't wait for the 30s reprice cycle.
        price_moved = abs(mid - old_mid)
        if price_moved > 0.01:
            canceled_count = 0
            for oid, order in list(self.om.orders.items()):
                if order.status == "live" and order.slug == slug:
                    await self.om.cancel_order(order)
                    canceled_count += 1
            if canceled_count:
                LOG.info(f"⚡ REACTIVE CANCEL | {slug[:40]} | mid moved {price_moved*100:.1f}¢ → canceled {canceled_count} orders")

                # Immediately re-place at new price
                if slug not in self.om.positions:
                    ok, _ = self.om.can_enter()
                    if ok:
                        buy_price = round(mid - max(0.005, spread/2 - self.cfg.spread_target), 4)
                        buy_price = max(buy_price, round(bb + 0.001, 4))
                        if 0 < buy_price < 1:
                            await self.om.place_limit(slug, market.yes_token, "BUY", buy_price, self.cfg.per_order)
                else:
                    pos = self.om.positions[slug]
                    exit_price = round(mid + 0.005, 4)
                    if mid < pos.entry_price - 0.01:
                        exit_price = round(bb, 4)
                    await self.om.place_limit(slug, market.yes_token, "SELL", exit_price, pos.cost)

        # ═══ PAPER FILL SIMULATION ═══
        if self.paper:
            for oid, order in list(self.om.orders.items()):
                if order.status != "live" or order.slug != slug:
                    continue
                # Only fill if price actually crosses our level
                if order.side == "BUY" and bb <= order.price:
                    self.om.fill_order(order, order.price)
                elif order.side == "SELL" and ba >= order.price:
                    self.om.fill_order(order, order.price)

    async def _paper_fill_check(self):
        """Simulate fills for paper trading — realistic hold times and losses."""
        import random
        now = time.time()
        for oid, order in list(self.om.orders.items()):
            if order.status != "live":
                continue
            market = self.markets.get(order.slug)
            if not market:
                continue

            # Minimum 30 seconds before first fill (order needs to rest on book)
            age = now - order.created
            if age < 30:
                continue

            if order.side == "BUY":
                # Fill only if our buy price is at or above best_bid
                if order.price >= market.best_bid and market.best_bid > 0:
                    # Fill rate increases with time on book
                    # 30s: 3% per check, 60s: 6%, 120s: 12%
                    fill_rate = min(0.03 * (age / 30), 0.15)
                    fill_rate *= min(market.volume / 50000, 2.0)
                    if random.random() < fill_rate:
                        self.om.fill_order(order, order.price)

            elif order.side == "SELL":
                if order.price <= market.best_ask and market.best_ask < 1:
                    fill_rate = min(0.03 * (age / 30), 0.15)
                    fill_rate *= min(market.volume / 50000, 2.0)
                    if random.random() < fill_rate:
                        self.om.fill_order(order, order.price)

    async def _reprice(self):
        """Cancel stale orders and place new ones at current best prices."""
        # Cancel all live orders
        for oid, order in list(self.om.orders.items()):
            if order.status == "live":
                await self.om.cancel_order(order)

        # For each market, determine if we should enter or exit
        for slug, market in self.markets.items():
            if market.best_bid <= 0 or market.best_ask >= 1:
                continue
            if market.spread < self.cfg.min_spread:
                continue

            mid = (market.best_bid + market.best_ask) / 2

            # If we have a position, place exit order
            if slug in self.om.positions:
                pos = self.om.positions[slug]
                hold_sec = time.time() - pos.opened

                # Exit strategy depends on how long we've been holding
                if hold_sec > self.cfg.max_hold_sec * 0.8:
                    # Getting close to timeout — exit aggressively at bid
                    exit_price = round(market.best_bid, 4)
                elif mid > pos.entry_price + 0.005:
                    # In profit — exit slightly above mid
                    exit_price = round(mid + 0.003, 4)
                elif mid > pos.entry_price - 0.005:
                    # Near breakeven — exit at mid
                    exit_price = round(mid, 4)
                else:
                    # Underwater — cut loss at bid
                    exit_price = round(market.best_bid, 4)

                await self.om.place_limit(
                    slug=slug,
                    token=market.yes_token,
                    side="SELL",
                    price=exit_price,
                    size_usd=pos.cost,
                )
                continue

            # No position — check if we can enter
            ok, reason = self.om.can_enter()
            if not ok:
                continue

            # Place buy order inside the spread
            # Target: capture spread_target of the spread
            half_spread = market.spread / 2
            buy_price = round(mid - max(0.005, half_spread - self.cfg.spread_target), 4)

            # Don't go below best_bid
            buy_price = max(buy_price, round(market.best_bid + 0.001, 4))

            if buy_price <= 0 or buy_price >= 1:
                continue

            await self.om.place_limit(
                slug=slug,
                token=market.yes_token,
                side="BUY",
                price=buy_price,
                size_usd=self.cfg.per_order,
            )

    def _final_report(self):
        """Print final report."""
        wins = [t for t in self.om.trades if t.pnl > 0]
        losses = [t for t in self.om.trades if t.pnl <= 0]
        total_pnl = sum(t.pnl for t in self.om.trades)
        wr = len(wins) / max(1, len(self.om.trades))
        avg_hold = sum(t.hold_sec for t in self.om.trades) / max(1, len(self.om.trades))

        # Close remaining positions at last known price
        for slug, pos in list(self.om.positions.items()):
            m = self.markets.get(slug)
            price = m.yes_price if m else pos.entry_price
            self.om.force_exit_position(slug, price, "shutdown")

        LOG.info(f"""
{'='*60}
  FINAL REPORT
{'='*60}
  Capital:         ${self.om.capital:.2f} (started ${self.cfg.capital:.2f})
  P&L:             ${self.om.daily_pnl:+.2f} ({self.om.daily_pnl/self.cfg.capital*100:+.1f}%)
  Peak:            ${self.om.peak:.2f}
  Max Drawdown:    {self.om.drawdown:.1%}
  
  Total Trades:    {len(self.om.trades)}
  Wins:            {len(wins)}
  Losses:          {len(losses)}
  Win Rate:        {wr:.0%}
  Avg Hold:        {avg_hold:.0f}s
  
  Avg Win:         ${sum(t.pnl for t in wins)/max(1,len(wins)):+.3f}
  Avg Loss:        ${sum(t.pnl for t in losses)/max(1,len(losses)):-.3f}
  Profit Factor:   {sum(t.pnl for t in wins)/max(0.001, abs(sum(t.pnl for t in losses))):.2f}
{'='*60}""")

        if self.om.trades:
            LOG.info("  Recent trades:")
            for t in self.om.trades[-10:]:
                emoji = "🟢" if t.pnl > 0 else "🔴"
                LOG.info(f"    {emoji} {t.slug[:35]:<35} {t.hold_sec:>4.0f}s | ${t.pnl:+.3f} | {t.reason}")


# ══════════════════════════════════════════════════════════════
# SCAN MODE — just print opportunities
# ══════════════════════════════════════════════════════════════

async def cmd_scan(cfg: Config):
    markets = await discover_markets(cfg)
    print(f"\n{'='*70}")
    print(f"  SCALPING TARGETS — {len(markets)} markets")
    print(f"  Filter: fee-free | spread≥3¢ | liq≥$2K | vol≥$1K | price 5-95¢")
    print(f"{'='*70}\n")

    import math
    for i, m in enumerate(markets):
        mid = (m.best_bid + m.best_ask) / 2
        buy_at = round(mid - max(0.005, m.spread/2 - 0.02), 4)
        sell_at = round(mid + 0.005, 4)
        profit_per = (sell_at - buy_at) * (cfg.per_order / buy_at)

        print(f"  {i+1:>2}. {m.question[:55]}")
        print(f"      Spread: {m.spread*100:.1f}¢ | Bid: ${m.best_bid:.3f} | Ask: ${m.best_ask:.3f} | Mid: ${mid:.3f}")
        print(f"      Liq: ${m.liquidity:,.0f} | Vol: ${m.volume:,.0f}")
        print(f"      → Buy @ ${buy_at:.3f} | Sell @ ${sell_at:.3f} | Est profit: ${profit_per:.2f}/trade")
        print()

    # Show exposure model
    print(f"  {'─'*60}")
    print(f"  💰 With ${cfg.capital:.0f} capital:")
    print(f"     Exposed: ${cfg.capital * cfg.max_exposure_pct:.0f} ({cfg.max_exposure_pct:.0%})")
    print(f"     Reserve: ${cfg.capital * (1-cfg.max_exposure_pct):.0f}")
    print(f"     Per order: ${cfg.per_order:.0f} × {cfg.max_concurrent} concurrent")
    print(f"     Markets watched: {len(markets)}")
    print()


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Polymarket Scalper")
    parser.add_argument("--scan", action="store_true", help="Discover scalping targets")
    parser.add_argument("--paper", action="store_true", help="Paper trade")
    parser.add_argument("--live", action="store_true", help="Live trading")
    parser.add_argument("--capital", type=float, default=None)
    parser.add_argument("--per-order", type=float, default=None)
    args = parser.parse_args()

    cfg = Config()
    if args.capital:
        cfg.capital = args.capital
    if args.per_order:
        cfg.per_order = args.per_order

    if args.scan:
        asyncio.run(cmd_scan(cfg))
    elif args.paper:
        scalper = Scalper(cfg, paper=True)
        asyncio.run(scalper.start("paper"))
    elif args.live:
        if not cfg.is_live:
            LOG.error("Missing POLYMARKET_PRIVATE_KEY or POLYMARKET_FUNDER in .env")
            sys.exit(1)
        scalper = Scalper(cfg, paper=False)
        asyncio.run(scalper.start("live"))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
