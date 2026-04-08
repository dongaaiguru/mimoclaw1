"""
MIMOCLAW v3 — Polymarket Trading Bot
=====================================
Two engines, real data, no simulations:
  1. Dependency Arbitrage — exploit logical mispricing between related markets
  2. Market Making — capture spread on fee-free markets

Usage:
  python bot.py --scan          # Discover markets + dependencies + check violations
  python bot.py --paper         # Paper trade with live prices
  python bot.py --live          # Live trading (requires .env API keys)
  python bot.py --live --capital 100

Requires: pip install aiohttp websockets python-dotenv
"""

import asyncio
import json
import os
import re
import sys
import time
import hmac
import hashlib
import logging
import argparse
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum

import aiohttp
import websockets
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("mimoclaw")


# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

@dataclass
class Config:
    capital: float = 100.0
    arb_pct: float = 0.55          # 55% to dependency arb
    mm_pct: float = 0.45           # 45% to market making
    max_position_pct: float = 0.20 # max 20% per trade
    max_drawdown: float = 0.12     # 12% circuit breaker
    max_daily_trades: int = 30
    max_concurrent: int = 4
    min_liquidity: float = 2000.0
    arb_min_violation: float = 0.03  # 3% minimum
    arb_max_hold_hours: float = 24.0
    mm_half_spread: float = 0.015   # 1.5¢ from mid
    mm_max_markets: int = 5
    order_max_age_sec: int = 600
    paper: bool = True

    # API
    api_key: str = field(default_factory=lambda: os.getenv("POLYMARKET_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("POLYMARKET_API_SECRET", ""))
    api_passphrase: str = field(default_factory=lambda: os.getenv("POLYMARKET_API_PASSPHRASE", ""))
    clob_url: str = "https://clob.polymarket.com"
    gamma_url: str = "https://gamma-api.polymarket.com"
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    @property
    def is_live(self) -> bool:
        return not self.paper and all([self.api_key, self.api_secret, self.api_passphrase])


# ═══════════════════════════════════════════════════════════════
# DATA TYPES
# ═══════════════════════════════════════════════════════════════

@dataclass
class Market:
    slug: str
    question: str
    yes_price: float
    no_price: float
    volume: float
    liquidity: float
    fee_free: bool
    yes_token: str
    no_token: str
    event_slug: str
    last_update: float = 0.0

    @property
    def bid(self) -> float:
        return self.yes_price

    @property
    def ask(self) -> float:
        return self.yes_price


class DepType(Enum):
    SUBSET = "subset"       # A implies B: P(B) >= P(A)
    SUPERSET = "superset"   # B implies A: P(A) >= P(B)
    MUTEX = "mutex"         # can't both be true: P(A)+P(B) <= 1


@dataclass
class Dep:
    a: str
    b: str
    dep_type: DepType
    confidence: float
    reason: str


@dataclass
class Violation:
    dep: Dep
    pa: float
    pb: float
    edge: float
    action_a: str  # "BUY" or "SELL"
    action_b: str


@dataclass
class Order:
    id: str
    slug: str
    token: str
    side: str
    price: float
    size: float
    engine: str
    status: str = "pending"
    created: float = field(default_factory=time.time)
    exchange_id: str = ""

@dataclass
class Position:
    slug: str
    entry: float
    size: float
    shares: float
    engine: str
    opened: float = field(default_factory=time.time)


# ═══════════════════════════════════════════════════════════════
# POLYMARKET API
# ═══════════════════════════════════════════════════════════════

class API:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._s: Optional[aiohttp.ClientSession] = None

    async def open(self):
        self._s = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

    async def close(self):
        if self._s: await self._s.close()

    async def get_events(self, limit=200) -> list:
        try:
            async with self._s.get(f"{self.cfg.gamma_url}/events",
                                   params={"active": "true", "closed": "false", "limit": limit}) as r:
                return await r.json() if r.status == 200 else []
        except Exception as e:
            log.error(f"API error: {e}")
            return []

    def _sign(self, method: str, path: str, body: str = "") -> dict:
        ts = str(int(time.time() * 1000))
        sig = hmac.new(self.cfg.api_secret.encode(),
                       (ts + method + path + body).encode(),
                       hashlib.sha256).hexdigest()
        return {"Content-Type": "application/json", "API-KEY": self.cfg.api_key,
                "API-SIGNATURE": sig, "API-TIMESTAMP": ts,
                "API-PASSPHRASE": self.cfg.api_passphrase}

    async def place_order(self, token: str, side: str, price: float, size: float) -> dict:
        if not self.cfg.is_live:
            return {"success": True, "order_id": f"paper_{int(time.time()*10000)}"}
        body = json.dumps({"asset_id": token, "side": side,
                           "size": str(round(size, 2)), "price": str(round(price, 4)),
                           "order_type": "GTC"})
        try:
            async with self._s.post(f"{self.cfg.clob_url}/orders",
                                    headers=self._sign("POST", "/orders", body),
                                    data=body) as r:
                return {**await r.json(), "success": r.status in (200, 201)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def cancel_order(self, order_id: str) -> dict:
        if not self.cfg.is_live:
            return {"success": True}
        try:
            async with self._s.delete(f"{self.cfg.clob_url}/orders/{order_id}",
                                      headers=self._sign("DELETE", "/orders")) as r:
                return {"success": r.status in (200, 204)}
        except Exception as e:
            return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════
# WEBSOCKET LIVE DATA
# ═══════════════════════════════════════════════════════════════

class Feed:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.books: Dict[str, dict] = {}
        self._running = False

    async def start(self, tokens: List[str]):
        self._running = True
        delay = 1
        while self._running:
            try:
                async with websockets.connect(self.cfg.ws_url, ping_interval=20) as ws:
                    delay = 1
                    for i in range(0, len(tokens), 50):
                        await ws.send(json.dumps({
                            "assets_ids": tokens[i:i+50],
                            "type": "market",
                            "custom_feature_enabled": True,
                        }))
                    log.info(f"WS subscribed to {len(tokens)} tokens")

                    async for msg in ws:
                        if msg == "PONG": continue
                        data = json.loads(msg) if isinstance(msg, str) else None
                        if not data: continue
                        items = data if isinstance(data, list) else [data]
                        for item in items:
                            if not isinstance(item, dict): continue
                            aid = item.get("asset_id", "")
                            if not aid: continue
                            book = self.books.setdefault(aid, {"bids": [], "asks": []})
                            t = item.get("type", "")
                            if t == "book":
                                book["bids"] = sorted([(float(p), float(s)) for p, s in item.get("bids", [])], reverse=True)
                                book["asks"] = sorted([(float(p), float(s)) for p, s in item.get("asks", [])])
                            elif t == "best_bid_ask":
                                bb, ba = item.get("best_bid"), item.get("best_ask")
                                if bb: book["bids"] = [(float(bb[0]), float(bb[1]))]
                                if ba: book["asks"] = [(float(ba[0]), float(ba[1]))]
            except Exception as e:
                log.warning(f"WS error: {e}, reconnect in {delay}s")
                if self._running:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)

    async def stop(self):
        self._running = False

    def best(self, token: str) -> Tuple[Optional[float], Optional[float]]:
        book = self.books.get(token)
        if not book: return None, None
        bb = book["bids"][0][0] if book["bids"] else None
        ba = book["asks"][0][0] if book["asks"] else None
        return bb, ba


# ═══════════════════════════════════════════════════════════════
# MARKET DISCOVERY
# ═══════════════════════════════════════════════════════════════

class Markets:
    def __init__(self, api: API, cfg: Config):
        self.api = api
        self.cfg = cfg
        self.all: Dict[str, Market] = {}

    async def refresh(self) -> List[Market]:
        events = await self.api.get_events()
        result = {}
        for ev in events:
            tags = ev.get("tags", [])
            if tags and isinstance(tags[0], dict):
                tags = [t.get("label", "").lower() for t in tags]
            else:
                tags = [str(t).lower() for t in tags]

            for m in ev.get("markets", []):
                if m.get("closed") or not m.get("active"):
                    continue
                slug = m.get("slug", "")
                if not slug: continue

                fee_free = not m.get("feesEnabled", True) or any(
                    t in tags for t in ["geopolitics", "world"])

                try:
                    prices = json.loads(m.get("outcomePrices", "[0.5, 0.5]"))
                    tokens = json.loads(m.get("clobTokenIds", "[]"))
                except (json.JSONDecodeError, IndexError):
                    continue

                liq_raw = m.get("liquidityClob")
                liq = float(liq_raw) if liq_raw else 0.0
                if liq < self.cfg.min_liquidity: continue

                result[slug] = Market(
                    slug=slug,
                    question=m.get("question", ""),
                    yes_price=float(prices[0]) if prices else 0.5,
                    no_price=float(prices[1]) if len(prices) > 1 else 0.5,
                    volume=float(m.get("volume", 0)),
                    liquidity=liq,
                    fee_free=fee_free,
                    yes_token=tokens[0] if tokens else "",
                    no_token=tokens[1] if len(tokens) > 1 else "",
                    event_slug=ev.get("slug", ""),
                    last_update=time.time(),
                )

        self.all = result
        log.info(f"Discovered {len(result)} markets")
        return list(result.values())

    def update_prices(self, feed: Feed):
        for m in self.all.values():
            if m.yes_token:
                bb, ba = feed.best(m.yes_token)
                if bb is not None and ba is not None:
                    m.yes_price = (bb + ba) / 2
                    m.no_price = 1.0 - m.yes_price
                    m.last_update = time.time()

    def token_map(self) -> Dict[str, str]:
        """token_id -> slug mapping"""
        m = {}
        for mk in self.all.values():
            if mk.yes_token: m[mk.yes_token] = mk.slug
            if mk.no_token: m[mk.no_token] = mk.slug
        return m


# ═══════════════════════════════════════════════════════════════
# DEPENDENCY DETECTION
# ═══════════════════════════════════════════════════════════════

class DepScanner:
    MONTHS = ['january','february','march','april','may','june',
              'july','august','september','october','november','december']
    STOP = {'will','the','a','an','by','in','at','is','be','of','to','and','or','any','for','on','its','it'}

    def scan(self, markets: Dict[str, Market]) -> List[Dep]:
        by_event: Dict[str, List[Market]] = {}
        for m in markets.values():
            by_event.setdefault(m.event_slug, []).append(m)

        deps = []
        for group in by_event.values():
            for i, ma in enumerate(group):
                for mb in group[i+1:]:
                    d = self._detect(ma, mb)
                    if d: deps.append(d)

        log.info(f"Found {len(deps)} dependencies")
        return deps

    def _detect(self, ma: Market, mb: Market) -> Optional[Dep]:
        qa, qb = ma.question.lower(), mb.question.lower()

        # Time-based subset (e.g., "by June" vs "by December")
        d = self._time_dep(qa, qb, ma.slug, mb.slug)
        if d: return d

        # Threshold subset (e.g., >$1B vs >$500M)
        d = self._threshold_dep(qa, qb, ma.slug, mb.slug)
        if d: return d

        # Mutual exclusion (different winners in same race)
        d = self._mutex_dep(qa, qb, ma.slug, mb.slug)
        if d: return d

        return None

    def _time_dep(self, qa, qb, sa, sb) -> Optional[Dep]:
        def month_idx(t):
            for i, m in enumerate(self.MONTHS):
                if m in t: return i
            return None

        ma, mb = month_idx(qa), month_idx(qb)
        if ma is None or mb is None or ma == mb: return None

        wa = set(qa.split()) - set(self.MONTHS) - self.STOP
        wb = set(qb.split()) - set(self.MONTHS) - self.STOP
        overlap = len(wa & wb) / max(len(wa | wb), 1)
        if overlap < 0.5: return None

        if ma < mb:
            return Dep(sa, sb, DepType.SUBSET, overlap, f"month {ma}<{mb}")
        return Dep(sa, sb, DepType.SUPERSET, overlap, f"month {mb}<{ma}")

    def _threshold_dep(self, qa, qb, sa, sb) -> Optional[Dep]:
        pa = re.findall(r'(\d+(?:\.\d+)?)\s*(b|billion|m|million|k|thousand)', qa, re.I)
        pb = re.findall(r'(\d+(?:\.\d+)?)\s*(b|billion|m|million|k|thousand)', qb, re.I)
        if not pa or not pb: return None

        def norm(v, u):
            n = float(v)
            u = u.lower()
            return n * 1000 if u in ('b','billion') else n if u in ('m','million') else n/1000

        va, vb = norm(pa[0][0], pa[0][1]), norm(pb[0][0], pb[0][1])
        wa = set(qa.split()) - self.STOP
        wb = set(qb.split()) - self.STOP
        overlap = len(wa & wb) / max(len(wa | wb), 1)
        if overlap < 0.4: return None

        if va > vb: return Dep(sa, sb, DepType.SUBSET, overlap, f"${va}M>${vb}M")
        if vb > va: return Dep(sa, sb, DepType.SUPERSET, overlap, f"${vb}M>${va}M")
        return None

    def _mutex_dep(self, qa, qb, sa, sb) -> Optional[Dep]:
        if "win" not in qa or "win" not in qb: return None
        ma = re.search(r'([\w][\w\s-]*?)\s+(?:will\s+)?win', qa)
        mb = re.search(r'([\w][\w\s-]*?)\s+(?:will\s+)?win', qb)
        if not ma or not mb: return None
        ea, eb = ma.group(1).strip(), mb.group(1).strip()
        if ea != eb and len(ea) > 2 and len(eb) > 2:
            return Dep(sa, sb, DepType.MUTEX, 0.8, f"'{ea}' vs '{eb}'")
        return None

    def violations(self, deps: List[Dep], markets: Dict[str, Market]) -> List[Violation]:
        opps = []
        for d in deps:
            ma, mb = markets.get(d.a), markets.get(d.b)
            if not ma or not mb: continue
            pa, pb = ma.yes_price, mb.yes_price
            min_v = 0.03

            if d.dep_type == DepType.SUBSET and pb < pa - min_v:
                opps.append(Violation(d, pa, pb, pa - pb, "SELL", "BUY"))
            elif d.dep_type == DepType.SUPERSET and pa < pb - min_v:
                opps.append(Violation(d, pa, pb, pb - pa, "BUY", "SELL"))
            elif d.dep_type == DepType.MUTEX and pa + pb > 1.0 + min_v:
                opps.append(Violation(d, pa, pb, pa + pb - 1.0, "SELL", "SELL"))

        opps.sort(key=lambda o: o.edge, reverse=True)
        return opps


# ═══════════════════════════════════════════════════════════════
# RISK + ORDER MANAGEMENT
# ═══════════════════════════════════════════════════════════════

class Risk:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.peak = cfg.capital
        self.cash = cfg.capital
        self.trades = 0
        self.day_start = time.time()
        self.positions: Dict[str, Position] = {}
        self.orders: Dict[str, Order] = {}
        self.log: List[dict] = []

    def can_trade(self, size: float) -> Tuple[bool, str]:
        if self.trades >= self.cfg.max_daily_trades: return False, "daily_limit"
        dd = (self.peak - self.cash) / max(self.peak, 0.01)
        if dd >= self.cfg.max_drawdown: return False, f"drawdown({dd:.0%})"
        if size > self.cash * self.cfg.max_position_pct: return False, "too_large"
        if self.cash - size < self.cash * 0.10: return False, "reserve"
        if len(self.positions) >= self.cfg.max_concurrent: return False, "max_pos"
        return True, "ok"

    def open(self, slug: str, entry: float, size: float, shares: float, engine: str):
        self.cash -= size
        self.positions[slug] = Position(slug, entry, size, shares, engine)

    def close(self, slug: str, exit_price: float) -> float:
        pos = self.positions.pop(slug, None)
        if not pos: return 0.0
        pnl = pos.shares * exit_price - pos.size
        self.cash += pos.shares * exit_price
        self.peak = max(self.peak, self.cash)
        self.trades += 1
        self.log.append({"pnl": pnl, "engine": pos.engine, "slug": slug})
        if time.time() - self.day_start > 86400:
            self.trades = 0; self.day_start = time.time()
        return pnl

    @property
    def pnl(self) -> float: return self.cash - self.cfg.capital
    @property
    def dd(self) -> float: return (self.peak - self.cash) / max(self.peak, 0.01)
    @property
    def win_rate(self) -> float:
        if not self.log: return 0
        return sum(1 for t in self.log if t['pnl'] > 0) / len(self.log)


# ═══════════════════════════════════════════════════════════════
# BOT
# ═══════════════════════════════════════════════════════════════

class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.api = API(cfg)
        self.feed = Feed(cfg)
        self.markets = Markets(self.api, cfg)
        self.deps = DepScanner()
        self.risk = Risk(cfg)
        self.running = False
        self._dep_list: List[Dep] = []
        self._last_refresh = 0
        self._last_dep_scan = 0

    async def run(self):
        mode = "LIVE" if self.cfg.is_live else "PAPER"
        log.info(f"MIMOCLAW v3 | {mode} | ${self.cfg.capital:.0f} | "
                f"Arb {self.cfg.arb_pct:.0%} + MM {self.cfg.mm_pct:.0%}")

        await self.api.open()
        self.running = True

        # Discover
        await self.markets.refresh()
        if not self.markets.all:
            log.error("No markets found"); return

        self._dep_list = self.deps.scan(self.markets.all)

        # Start WS
        tokens = list(self.markets.token_map().keys())
        if tokens:
            ws_task = asyncio.create_task(self.feed.start(tokens))
        else:
            ws_task = None

        log.info("Trading loop started")
        tick = 0
        try:
            while self.running:
                # Refresh markets every 5 min
                if time.time() - self._last_refresh > 300:
                    await self.markets.refresh()
                    self._last_refresh = time.time()
                if time.time() - self._last_dep_scan > 600:
                    self._dep_list = self.deps.scan(self.markets.all)
                    self._last_dep_scan = time.time()

                # Update prices from WS
                self.markets.update_prices(self.feed)

                # Run engines
                await self._arb_tick(tick)
                await self._mm_tick(tick)

                # Status
                if tick > 0 and tick % 120 == 0:
                    log.info(f"T={tick} | ${self.risk.cash:.2f} ({self.risk.pnl:+.2f}) | "
                            f"DD:{self.risk.dd:.0%} | Pos:{len(self.risk.positions)} | "
                            f"WR:{self.risk.win_rate:.0%}")

                tick += 1
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            if ws_task: ws_task.cancel()
            await self.api.close()
            self._report()

    async def _arb_tick(self, tick: int):
        """Dependency arbitrage — check for violations and enter/exit."""
        opps = self.deps.violations(self._dep_list, self.markets.all)

        for v in opps[:2]:
            key = f"{v.dep.a}|{v.dep.b}"
            ma = self.markets.all.get(v.dep.a)
            mb = self.markets.all.get(v.dep.b)
            if not ma or not mb: continue

            if key in self.risk.positions:
                # Exit check
                corrected = False
                if v.dep.dep_type == DepType.SUBSET:
                    corrected = mb.yes_price >= ma.yes_price - 0.025
                elif v.dep.dep_type == DepType.SUPERSET:
                    corrected = ma.yes_price >= mb.yes_price - 0.025
                elif v.dep.dep_type == DepType.MUTEX:
                    corrected = ma.yes_price + mb.yes_price <= 1.025

                held_h = (time.time() - self.risk.positions[key].opened) / 3600
                if corrected or held_h > self.cfg.arb_max_hold_hours:
                    # Close both legs
                    pos = self.risk.positions[key]
                    current_avg = (ma.yes_price + mb.yes_price) / 2
                    pnl = self.risk.close(key, current_avg)
                    log.info(f"ARB EXIT | {'corrected' if corrected else 'timeout'} | PnL:${pnl:+.2f}")
                continue

            # Entry
            size = min(self.cfg.capital * self.cfg.arb_pct * 0.12,
                       self.cfg.capital * self.cfg.max_position_pct)
            ok, _ = self.risk.can_trade(size)
            if not ok: continue

            # Place both legs
            token_a = ma.yes_token if v.action_a == "BUY" else ma.yes_token
            token_b = mb.yes_token if v.action_b == "BUY" else mb.yes_token
            price_a = ma.yes_price if v.action_a == "BUY" else ma.yes_price
            price_b = mb.yes_price if v.action_b == "BUY" else mb.yes_price

            r1 = await self.api.place_order(token_a, v.action_a, price_a, size)
            if not r1.get("success"): continue

            r2 = await self.api.place_order(token_b, v.action_b, price_b, size)
            if not r2.get("success"):
                await self.api.cancel_order(r1.get("order_id", ""))
                continue

            entry_price = (ma.yes_price + mb.yes_price) / 2
            self.risk.open(key, entry_price, size * 2, size, "arb")
            log.info(f"ARB ENTRY | {v.dep.a[:30]} ↔ {v.dep.b[:30]} | "
                    f"Edge:{v.edge:.1%} | ${size:.0f}x2")

    async def _mm_tick(self, tick: int):
        """Market making — place bid/ask quotes on fee-free markets."""
        if tick % 30 != 0: return

        fee_free = [m for m in self.markets.all.values() if m.fee_free]
        fee_free.sort(key=lambda m: m.liquidity, reverse=True)
        mm_cap = self.cfg.capital * self.cfg.mm_pct
        cap_per = mm_cap / max(min(len(fee_free), self.cfg.mm_max_markets), 1)

        for m in fee_free[:self.cfg.mm_max_markets]:
            if m.yes_price < 0.02 or m.yes_price > 0.98: continue

            size = round(cap_per * 0.12, 2)
            if size < 1.0: continue

            # Cancel stale orders for this market
            for oid, o in list(self.risk.orders.items()):
                if o.slug == m.slug and time.time() - o.created > 60:
                    await self.api.cancel_order(o.exchange_id)
                    o.status = "canceled"

            # Skip if already have active orders
            active = [o for o in self.risk.orders.values()
                      if o.slug == m.slug and o.status == "active"]
            if active: continue

            # Place bid (buy below mid)
            bid_price = round(m.yes_price - self.cfg.mm_half_spread, 4)
            if bid_price < 0.01: continue
            ok, _ = self.risk.can_trade(size)
            if ok:
                r = await self.api.place_order(m.yes_token, "BUY", bid_price, size)
                if r.get("success"):
                    oid = r.get("order_id", "")
                    self.risk.orders[oid] = Order(
                        oid, m.slug, m.yes_token, "BUY", bid_price, size, "mm",
                        status="active", exchange_id=oid)

            # Place ask if we have a position
            if m.slug in self.risk.positions:
                ask_price = round(m.yes_price + self.cfg.mm_half_spread, 4)
                r = await self.api.place_order(m.yes_token, "SELL", ask_price, size)
                if r.get("success"):
                    oid = r.get("order_id", "")
                    self.risk.orders[oid] = Order(
                        oid, m.slug, m.yes_token, "SELL", ask_price, size, "mm",
                        status="active", exchange_id=oid)

    def _report(self):
        log.info(f"""
╔═══════════════════════════════════════════════╗
║  FINAL: ${self.risk.cash:.2f} | PnL: ${self.risk.pnl:+.2f} ({self.risk.pnl/self.cfg.capital:+.1%})  ║
║  Trades: {self.risk.trades} | WR: {self.risk.win_rate:.0%} | DD: {self.risk.dd:.0%}       ║
╚═══════════════════════════════════════════════╝""")


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

async def cmd_scan(cfg: Config):
    """Discover markets + dependencies + check for violations."""
    api = API(cfg)
    await api.open()
    mkts = Markets(api, cfg)
    await mkts.refresh()

    print(f"\n{'='*60}")
    print(f"  {len(mkts.all)} fee-free markets found")
    print(f"{'='*60}")
    for m in sorted(mkts.all.values(), key=lambda x: x.liquidity, reverse=True)[:15]:
        print(f"  {m.slug:<40} P:{m.yes_price:.3f} Liq:${m.liquidity:,.0f}")

    scanner = DepScanner()
    deps = scanner.scan(mkts.all)
    violations = scanner.violations(deps, mkts.all)

    print(f"\n  {len(deps)} dependencies found")
    print(f"  {len(violations)} active violations")
    for v in violations[:10]:
        print(f"  [{v.dep.dep_type.value}] {v.dep.a[:30]} ↔ {v.dep.b[:30]} "
              f"Edge:{v.edge:.1%} pa={v.pa:.3f} pb={v.pb:.3f}")

    # Test WS
    tokens = list(mkts.token_map().keys())[:20]
    if tokens:
        print(f"\n  Testing WS ({len(tokens)} tokens)...")
        feed = Feed(cfg)
        ws_task = asyncio.create_task(feed.start(tokens))
        await asyncio.sleep(8)
        await feed.stop()
        ws_task.cancel()
        print(f"  Got live data for {len(feed.books)} assets")
        for aid, book in list(feed.books.items())[:5]:
            bb, ba = feed.best(aid)
            if bb and ba:
                slug = mkts.token_map().get(aid, aid[:16])
                print(f"    {slug:<35} Bid:{bb:.4f} Ask:{ba:.4f}")

    await api.close()


async def cmd_trade(cfg: Config):
    bot = Bot(cfg)
    await bot.run()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scan", action="store_true")
    parser.add_argument("--paper", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--capital", type=float, default=100.0)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(message)s')

    cfg = Config(capital=args.capital, paper=not args.live)

    if args.scan:
        asyncio.run(cmd_scan(cfg))
    elif args.paper:
        cfg.paper = True
        asyncio.run(cmd_trade(cfg))
    elif args.live:
        cfg.paper = False
        asyncio.run(cmd_trade(cfg))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
