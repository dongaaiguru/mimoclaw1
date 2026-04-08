"""
Polymarket Unified Score Engine — Production Bot

Implements the calibrated decision formula:
  Score = 0.35*EV + 0.20*KL + 0.20*ΔP + 0.15*LMSR - 0.10*Risk

Backtest-calibrated threshold: 0.45 (not the original 0.65)

Usage:
  python bot.py              # Paper trading
  python bot.py --live       # Live trading
  python bot.py --threshold 0.50  # Custom threshold
"""
import asyncio
import json
import logging
import math
import os
import signal
import sys
import time
import hmac
import hashlib
import argparse
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

import aiohttp
import websockets
from dotenv import load_dotenv

load_dotenv()

# ══════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════

@dataclass
class Config:
    # API
    api_key: str = os.getenv("POLYMARKET_API_KEY", "")
    api_secret: str = os.getenv("POLYMARKET_API_SECRET", "")
    api_passphrase: str = os.getenv("POLYMARKET_API_PASSPHRASE", "")
    private_key: str = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    
    clob_url: str = "https://clob.polymarket.com"
    gamma_url: str = "https://gamma-api.polymarket.com"
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    ws_user_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    
    # Trading (calibrated from backtest)
    threshold: float = 0.45          # OPTIMAL from backtest (was 0.65)
    initial_capital: float = 50.0
    max_concurrent: int = 3
    kelly_fraction: float = 0.25
    min_edge: float = 0.02
    fees: float = 0.005
    
    # Risk
    profit_target: float = 0.04      # 4%
    stop_loss: float = 0.08          # 8%
    max_drawdown: float = 0.15       # 15%
    max_daily_trades: int = 20
    max_spread: float = 0.03
    min_liquidity: float = 10_000
    
    # Timing
    ws_heartbeat: int = 10
    state_save_interval: int = 60
    dashboard_interval: int = 300
    
    @property
    def is_live(self) -> bool:
        return bool(self.api_key and self.api_secret and self.api_passphrase)


# ══════════════════════════════════════════════════════════════
# MATHEMATICAL MODELS
# ══════════════════════════════════════════════════════════════

def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


class BayesianEstimator:
    def __init__(self, prior=0.5, strength=20.0):
        self.alpha = prior * strength
        self.beta = (1 - prior) * strength
        self._history = []

    @property
    def prob(self):
        return self.alpha / (self.alpha + self.beta)

    @property
    def confidence(self):
        return clamp((self.alpha + self.beta) / 100, 0, 1)

    def update(self, price_move, volume_ratio=1.0, trade_side=0):
        vol_w = min(volume_ratio / 2.0, 3.0)
        move_w = abs(price_move) * 100
        if price_move > 0 or trade_side > 0:
            self.alpha += vol_w * move_w + 0.5
        elif price_move < 0 or trade_side < 0:
            self.beta += vol_w * move_w + 0.5
        self._history.append(self.prob)

    def delta_p(self, lookback=15):
        if len(self._history) >= lookback:
            return self.prob - self._history[-lookback]
        elif self._history:
            return self.prob - self._history[0]
        return 0.0


def compute_ev(true_prob, entry_price, fees=0.005, min_edge=0.02, max_edge=0.15):
    raw = true_prob - entry_price - fees
    return {
        "raw": raw,
        "normalized": clamp(raw / max_edge, 0, 1) if max_edge > 0 else 0,
        "passes": raw >= min_edge,
    }


def kl_divergence_binary(p, q):
    p = clamp(p, 1e-6, 1 - 1e-6)
    q = clamp(q, 1e-6, 1 - 1e-6)
    return p * math.log(p / q) + (1 - p) * math.log((1 - p) / (1 - q))


def compute_kl(market_price, related_price, relationship="subset", threshold=0.08):
    raw = 0.0
    if relationship == "subset" and related_price < market_price - 0.005:
        raw = kl_divergence_binary(market_price, related_price)
    elif relationship == "superset" and market_price < related_price - 0.005:
        raw = kl_divergence_binary(related_price, market_price)
    return clamp(raw / threshold, 0, 1)


def compute_lmsr_edge(price, trade_size, b):
    if price <= 0 or price >= 1 or b <= 0:
        return 0.0
    log_r = math.log(price / (1 - price))
    q_yes = b * log_r / 2
    shares = trade_size / price
    e_old = math.exp(q_yes / b)
    e_no = math.exp(-q_yes / b)
    e_new = math.exp((q_yes + shares) / b)
    price_after = e_new / (e_new + e_no)
    edge = max(price_after - price, 0)
    return clamp(edge / 0.02, 0, 1)


def compute_stoikov_risk(mid, bid, ask, position=0.0, vol=0.05, gamma=0.5):
    reservation = mid - position * gamma * (vol ** 2)
    deviation = abs(mid - reservation)
    spread = ask - bid
    inv_risk = clamp(deviation / 0.05, 0, 1)
    spread_risk = clamp(spread / 0.03, 0, 1)
    return 0.7 * inv_risk + 0.3 * spread_risk


def compute_score(ev_norm, kl_norm, dp_norm, lmsr_norm, risk_norm,
                  ev_raw, spread, liquidity, daily_trades, drawdown, threshold):
    score = 0.35*ev_norm + 0.20*kl_norm + 0.20*dp_norm + 0.15*lmsr_norm - 0.10*risk_norm
    
    ok = True
    reasons = []
    if ev_raw < 0.02:
        ok = False
        reasons.append(f"ev_low({ev_raw:.4f})")
    if spread > 0.03:
        ok = False
        reasons.append(f"spread_wide({spread:.4f})")
    if liquidity < 10000:
        ok = False
        reasons.append(f"liq_low({liquidity:.0f})")
    if daily_trades >= 20:
        ok = False
        reasons.append("daily_limit")
    if drawdown >= 0.15:
        ok = False
        reasons.append("drawdown_limit")
    
    return {
        "score": score,
        "should_trade": score > threshold and ok,
        "reasons": reasons if not ok else [f"PASS({score:.3f})"],
    }


# ══════════════════════════════════════════════════════════════
# AUTHENTICATION
# ══════════════════════════════════════════════════════════════

class Auth:
    def __init__(self, api_key, api_secret, passphrase):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
    
    def sign(self, method, path, body=""):
        ts = str(int(time.time() * 1000))
        msg = ts + method + path + body
        sig = hmac.new(
            self.api_secret.encode(), msg.encode(), hashlib.sha256
        ).hexdigest()
        return {
            "Content-Type": "application/json",
            "API-KEY": self.api_key,
            "API-SIGNATURE": sig,
            "API-TIMESTAMP": ts,
            "API-PASSPHRASE": self.passphrase,
        }
    
    def ws_payload(self):
        return {
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "passphrase": self.passphrase,
        }


# ══════════════════════════════════════════════════════════════
# WEBSOCKET LISTENER
# ══════════════════════════════════════════════════════════════

class WSListener:
    def __init__(self, config: Config):
        self.config = config
        self.books: Dict[str, dict] = {}
        self._ws = None
        self._running = False
        self._callbacks = []
    
    def on_update(self, cb):
        self._callbacks.append(cb)
    
    async def start(self, asset_ids: List[str]):
        self._running = True
        while self._running:
            try:
                async with websockets.connect(self.config.ws_url) as ws:
                    self._ws = ws
                    await ws.send(json.dumps({
                        "assets_ids": asset_ids,
                        "type": "market",
                        "custom_feature_enabled": True,
                    }))
                    hb = asyncio.create_task(self._heartbeat())
                    try:
                        async for msg in ws:
                            if msg == "PONG":
                                continue
                            await self._handle(msg)
                    finally:
                        hb.cancel()
            except Exception as e:
                logging.error(f"WS error: {e}, reconnecting in 2s...")
                await asyncio.sleep(2)
    
    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()
    
    async def _heartbeat(self):
        while self._running:
            try:
                if self._ws:
                    await self._ws.send("PING")
                await asyncio.sleep(self.config.ws_heartbeat)
            except:
                break
    
    async def _handle(self, msg):
        try:
            data = json.loads(msg)
        except:
            return
        
        t = data.get("type", "")
        aid = data.get("asset_id", "")
        
        if t == "book":
            bids = sorted([(float(p), float(s)) for p, s in data.get("bids", [])], reverse=True)
            asks = sorted([(float(p), float(s)) for p, s in data.get("asks", [])])
            self.books[aid] = {"bids": bids, "asks": asks, "time": time.time()}
        elif t == "best_bid_ask":
            book = self.books.setdefault(aid, {"bids": [], "asks": [], "time": 0})
            bb = data.get("best_bid")
            ba = data.get("best_ask")
            if bb:
                book["bids"] = [(float(bb[0]), float(bb[1]))]
            if ba:
                book["asks"] = [(float(ba[0]), float(ba[1]))]
            book["time"] = time.time()
        elif t == "last_trade_price":
            price = float(data.get("price", 0))
            size = float(data.get("size", 0))
            if aid in self.books:
                self.books[aid]["last_trade"] = price
                self.books[aid]["last_size"] = size
        
        if aid:
            for cb in self._callbacks:
                await cb(aid, self.books.get(aid, {}))


# ══════════════════════════════════════════════════════════════
# MAIN BOT
# ══════════════════════════════════════════════════════════════

@dataclass
class Position:
    slug: str
    asset_id: str
    entry_price: float
    size: float
    entry_tick: int
    score: float


class Bot:
    def __init__(self, config: Config):
        self.config = config
        self.capital = config.initial_capital
        self.peak = config.initial_capital
        self.positions: Dict[str, Position] = {}
        self.estimators: Dict[str, BayesianEstimator] = {}
        self.markets: List[dict] = []
        self.ws = WSListener(config)
        self.auth = Auth(config.api_key, config.api_secret, config.api_passphrase)
        self.tick = 0
        self.daily_trades = 0
        self.trades_log = []
        self.running = False
        self._http: Optional[aiohttp.ClientSession] = None
    
    async def start(self):
        logging.info(f"Starting bot | Capital: ${self.config.capital:.2f} | "
                    f"Threshold: {self.config.threshold} | "
                    f"Mode: {'LIVE' if self.config.is_live else 'PAPER'}")
        
        # Discover markets
        await self._discover_markets()
        
        if not self.markets:
            logging.error("No suitable markets found. Exiting.")
            return
        
        logging.info(f"Monitoring {len(self.markets)} markets")
        
        # Initialize estimators
        for m in self.markets:
            self.estimators[m["slug"]] = BayesianEstimator(
                prior=m["yes_price"], strength=20.0
            )
        
        # HTTP session
        self._http = aiohttp.ClientSession()
        
        # WebSocket
        self.running = True
        asset_ids = [m["yes_token"] for m in self.markets if m.get("yes_token")]
        
        if asset_ids:
            self.ws.on_update(self._on_tick)
            await self.ws.start(asset_ids)
        else:
            logging.warning("No token IDs — running in analysis-only mode")
            # Simulate ticks for testing
            await self._simulate_mode()
    
    async def stop(self):
        logging.info("Shutting down...")
        self.running = False
        await self.ws.stop()
        if self._http:
            await self._http.close()
        self._save_state()
        self._print_dashboard()
    
    async def _discover_markets(self):
        """Fetch active crypto markets from Gamma API."""
        try:
            async with self._http.get(
                f"{self.config.gamma_url}/events",
                params={"active": "true", "closed": "false", "limit": "100"}
            ) as resp:
                events = await resp.json()
            
            for event in events:
                title = (event.get("title", "") + " " + event.get("slug", "")).lower()
                if not any(k in title for k in ["bitcoin", "btc", "crypto", "eth", "megaeth"]):
                    continue
                
                for m in event.get("markets", []):
                    if m.get("closed") or not m.get("active"):
                        continue
                    
                    spread = float(m.get("spread", 1))
                    volume = float(m.get("volume", 0))
                    liquidity = float(m.get("liquidityClob", m.get("liquidity", 0)))
                    prices = json.loads(m.get("outcomePrices", "[0,1]"))
                    yes_price = float(prices[0]) if prices else 0.5
                    tokens = json.loads(m.get("clobTokenIds", "[]"))
                    
                    if (spread < self.config.max_spread and
                        liquidity > self.config.min_liquidity and
                        volume > 5000 and
                        yes_price > 0.005 and
                        yes_price < 0.995):
                        
                        self.markets.append({
                            "slug": m.get("slug", ""),
                            "question": m.get("question", ""),
                            "yes_price": yes_price,
                            "spread": spread,
                            "volume": volume,
                            "liquidity": liquidity,
                            "condition_id": m.get("conditionId", ""),
                            "yes_token": tokens[0] if len(tokens) > 0 else "",
                            "no_token": tokens[1] if len(tokens) > 1 else "",
                        })
            
            logging.info(f"Discovered {len(self.markets)} qualifying markets")
            
        except Exception as e:
            logging.error(f"Market discovery failed: {e}")
            # Fallback to hardcoded markets
            self._load_fallback_markets()
    
    def _load_fallback_markets(self):
        """Load known active markets as fallback."""
        self.markets = [
            {"slug": "btc-150k-jun-2026", "question": "BTC $150k by Jun 2026?",
             "yes_price": 0.017, "spread": 0.002, "volume": 3942360,
             "liquidity": 50000, "condition_id": "", "yes_token": "", "no_token": ""},
            {"slug": "btc-150k-dec-2026", "question": "BTC $150k by Dec 2026?",
             "yes_price": 0.095, "spread": 0.01, "volume": 1000000,
             "liquidity": 30000, "condition_id": "", "yes_token": "", "no_token": ""},
            {"slug": "megaeth-1b", "question": "MegaETH FDV >$1B?",
             "yes_price": 0.325, "spread": 0.01, "volume": 2893683,
             "liquidity": 40000, "condition_id": "", "yes_token": "", "no_token": ""},
            {"slug": "megaeth-2b", "question": "MegaETH FDV >$2B?",
             "yes_price": 0.095, "spread": 0.01, "volume": 5820061,
             "liquidity": 50000, "condition_id": "", "yes_token": "", "no_token": ""},
        ]
    
    async def _simulate_mode(self):
        """Run simulation when no WebSocket data available."""
        import random
        rng = random.Random(42)
        
        while self.running:
            self.tick += 1
            for m in self.markets:
                slug = m["slug"]
                est = self.estimators[slug]
                
                # Simulate price tick
                price_change = rng.gauss(0, 0.003)
                m["yes_price"] = clamp(m["yes_price"] + price_change, 0.001, 0.999)
                
                # Update Bayesian
                est.update(price_change, rng.expovariate(1.0),
                          1 if price_change > 0 else -1)
                
                # Compute signal
                await self._process_signal(slug, m)
            
            await asyncio.sleep(0.1)  # 10 ticks/sec
    
    async def _on_tick(self, asset_id: str, book: dict):
        """Handle real-time WebSocket tick."""
        self.tick += 1
        
        # Find market by token ID
        market = None
        for m in self.markets:
            if m.get("yes_token") == asset_id:
                market = m
                break
        
        if not market:
            return
        
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        
        if not bids or not asks:
            return
        
        best_bid = bids[0][0]
        best_ask = asks[0][0]
        mid = (best_bid + best_ask) / 2
        spread = best_ask - best_bid
        
        old_price = market["yes_price"]
        market["yes_price"] = mid
        market["spread"] = spread
        
        # Update Bayesian
        price_change = mid - old_price
        volume = book.get("last_size", 1.0)
        self.estimators[market["slug"]].update(
            price_change, volume,
            1 if price_change > 0 else -1,
        )
        
        await self._process_signal(market["slug"], market)
    
    async def _process_signal(self, slug: str, market: dict):
        """Compute score and execute if warranted."""
        
        # Check exit for open position
        if slug in self.positions:
            await self._check_exit(slug, market)
            return
        
        # Skip if max concurrent
        if len(self.positions) >= self.config.max_concurrent:
            return
        
        est = self.estimators[slug]
        
        # ═══ SCORE COMPONENTS ═══
        
        # 1. EV
        ev = compute_ev(
            est.prob, market["yes_price"],
            self.config.fees, self.config.min_edge,
        )
        
        # 2. KL
        kl_norm = 0.0
        # Check related markets
        relationships = [
            ("btc-150k-jun-2026", "btc-150k-dec-2026", "subset"),
            ("megaeth-2b", "megaeth-1b", "subset"),
        ]
        for a, b, rel in relationships:
            other_slug = None
            if slug == a:
                other_slug = b
            elif slug == b:
                other_slug = a
                rel = "superset"
            if other_slug and other_slug in self.estimators:
                other_price = next(
                    (m["yes_price"] for m in self.markets if m["slug"] == other_slug), 0.5
                )
                kl_norm = max(kl_norm, compute_kl(market["yes_price"], other_price, rel))
        
        # 3. DeltaP
        dp = est.delta_p(lookback=15)
        dp_norm = clamp(max(dp, 0) / 0.04, 0, 1)
        
        # 4. LMSR
        pos_size = min(
            self.capital * 0.25 / self.config.max_concurrent,
            self.capital * self.config.kelly_fraction,
        )
        b = max(20, market.get("liquidity", 10000) / 50)
        lmsr_norm = compute_lmsr_edge(market["yes_price"], pos_size, b)
        
        # 5. Stoikov Risk
        risk = compute_stoikov_risk(
            market["yes_price"],
            market["yes_price"] - market["spread"] / 2,
            market["yes_price"] + market["spread"] / 2,
        )
        
        # ═══ UNIFIED SCORE ═══
        drawdown = max(0, (self.peak - self.capital) / self.peak)
        
        result = compute_score(
            ev["normalized"], kl_norm, dp_norm, lmsr_norm, risk,
            ev["raw"], market["spread"], market.get("liquidity", 0),
            self.daily_trades, drawdown, self.config.threshold,
        )
        
        if result["should_trade"]:
            await self._execute(slug, market, result["score"], ev["raw"])
    
    async def _execute(self, slug, market, score, ev_raw):
        """Execute trade."""
        est = self.estimators[slug]
        
        # Kelly sizing
        kelly = (est.prob - market["yes_price"]) / (1 - market["yes_price"])
        kelly = max(0, kelly * self.config.kelly_fraction)
        
        size = min(
            kelly * self.capital,
            self.capital / self.config.max_concurrent,
            self.capital * 0.25,
        )
        
        if size < 1.0:
            return
        
        cost = size + size * self.config.fees
        if cost > self.capital:
            return
        
        entry_price = market["yes_price"] + market.get("spread", 0.01) / 2  # Buy at ask
        
        # Deduct capital
        self.capital -= cost
        
        self.positions[slug] = Position(
            slug=slug,
            asset_id=market.get("yes_token", ""),
            entry_price=entry_price,
            size=size,
            entry_tick=self.tick,
            score=score,
        )
        
        self.daily_trades += 1
        
        logging.info(
            f"BUY {slug} @ ${entry_price:.3f} | ${size:.2f} | "
            f"Score={score:.3f} EV={ev_raw:.4f} | "
            f"Bayes={est.prob:.3f} | Capital=${self.capital:.2f}"
        )
        
        if self.config.is_live:
            await self._place_order(market, "BUY", size, entry_price)
    
    async def _check_exit(self, slug, market):
        """Check exit conditions."""
        pos = self.positions[slug]
        current = market["yes_price"]
        pnl_pct = (current - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0
        
        should_exit = False
        reason = ""
        
        if pnl_pct >= self.config.profit_target:
            should_exit, reason = True, "profit_target"
        elif pnl_pct <= -self.config.stop_loss:
            should_exit, reason = True, "stop_loss"
        elif self.tick - pos.entry_tick > 200:
            should_exit, reason = True, "time_decay"
        
        if should_exit:
            sell_price = market["yes_price"] - market.get("spread", 0.01) / 2
            pnl_pct_actual = (sell_price - pos.entry_price) / pos.entry_price
            pnl_dollars = pos.size * pnl_pct_actual - pos.size * self.config.fees * 2
            self.capital += pos.size + pnl_dollars
            
            logging.info(
                f"SELL {slug} @ ${sell_price:.3f} | P&L=${pnl_dollars:+.2f} ({pnl_pct_actual:+.1%}) | {reason}"
            )
            
            self.trades_log.append({
                "tick": self.tick, "slug": slug,
                "entry": pos.entry_price, "exit": sell_price,
                "pnl": pnl_dollars, "reason": reason,
            })
            
            del self.positions[slug]
            
            if self.config.is_live:
                await self._place_order(market, "SELL", pos.size, sell_price)
            
            # Update peak
            if self.capital > self.peak:
                self.peak = self.capital
    
    async def _place_order(self, market, side, size_usd, price):
        """Place order via CLOB API."""
        if not self.config.is_live:
            return
        
        token_id = market.get("yes_token") if side == "BUY" else market.get("no_token")
        if not token_id:
            logging.warning(f"No token ID for {market['slug']}")
            return
        
        shares = size_usd / price
        order = {
            "asset_id": token_id,
            "side": side,
            "size": str(round(shares, 2)),
            "price": str(round(price, 4)),
            "order_type": "GTC",
        }
        
        body = json.dumps(order)
        path = "/orders"
        
        for attempt in range(3):
            try:
                headers = self.auth.sign("POST", path, body)
                async with self._http.post(
                    f"{self.config.clob_url}{path}",
                    headers=headers, data=body,
                ) as resp:
                    if resp.status == 201:
                        result = await resp.json()
                        logging.info(f"Order placed: {result.get('order_id', '?')}")
                        return
                    elif resp.status == 401:
                        logging.warning(f"Auth failed (attempt {attempt+1})")
                        await asyncio.sleep(1)
                    else:
                        logging.error(f"Order failed ({resp.status}): {await resp.text()}")
                        await asyncio.sleep(2 ** attempt)
            except Exception as e:
                logging.error(f"Order error: {e}")
                await asyncio.sleep(2 ** attempt)
    
    def _save_state(self):
        """Save state to disk."""
        state = {
            "capital": self.capital,
            "peak": self.peak,
            "positions": {s: {"entry": p.entry_price, "size": p.size}
                         for s, p in self.positions.items()},
            "trades": self.trades_log[-100:],
            "tick": self.tick,
        }
        try:
            with open("state.json", "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logging.error(f"State save failed: {e}")
    
    def _print_dashboard(self):
        """Print status dashboard."""
        wins = sum(1 for t in self.trades_log if t["pnl"] > 0)
        losses = sum(1 for t in self.trades_log if t["pnl"] <= 0)
        total_pnl = sum(t["pnl"] for t in self.trades_log)
        win_rate = wins / max(1, len(self.trades_log))
        drawdown = max(0, (self.peak - self.capital) / self.peak)
        
        print(f"""
╔════════════════════════════════════════════════╗
║     POLYMARKET UNIFIED SCORE ENGINE v2         ║
╠════════════════════════════════════════════════╣
║  Capital:      ${self.capital:>8.2f}                ║
║  Peak:         ${self.peak:>8.2f}                ║
║  Drawdown:     {drawdown*100:>5.1f}%                     ║
║  Positions:    {len(self.positions):>3}                          ║
╠════════════════════════════════════════════════╣
║  Trades:       {len(self.trades_log):>4}                        ║
║  Win Rate:     {win_rate*100:>5.1f}%                     ║
║  Total P&L:    ${total_pnl:>8.2f}                ║
║  Daily Trades: {self.daily_trades:>4}                        ║
╠════════════════════════════════════════════════╣
║  Mode:         {'LIVE' if self.config.is_live else 'PAPER':<10}                    ║
║  Threshold:    {self.config.threshold:<10}                    ║
║  Ticks:        {self.tick:>6}                       ║
╚════════════════════════════════════════════════╝""")


# ══════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════

async def main():
    parser = argparse.ArgumentParser(description="Polymarket Unified Score Engine")
    parser.add_argument("--live", action="store_true", help="Live trading")
    parser.add_argument("--threshold", type=float, default=0.45, help="Score threshold")
    parser.add_argument("--capital", type=float, default=50.0, help="Initial capital")
    args = parser.parse_args()
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("trading.log"),
        ],
    )
    
    config = Config(
        threshold=args.threshold,
        initial_capital=args.capital,
    )
    
    if not args.live:
        config.api_key = ""
        logging.info("PAPER TRADING mode (--live for real)")
    
    bot = Bot(config)
    
    loop = asyncio.get_event_loop()
    stop = asyncio.Event()
    
    def shutdown():
        logging.info("Shutdown signal received")
        stop.set()
    
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown)
    
    bot_task = asyncio.create_task(bot.start())
    stop_task = asyncio.create_task(stop.wait())
    
    done, pending = await asyncio.wait(
        [bot_task, stop_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    
    await bot.stop()
    for task in pending:
        task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
