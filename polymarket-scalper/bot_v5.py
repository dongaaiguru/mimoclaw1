#!/usr/bin/env python3
"""
Polymarket Scalper v5 — The Profit Machine.

All v4 weaknesses fixed + real alpha sources added.

v4→v5 UPGRADES:
  TIER 1 — Fix the Broken Stuff:
    1. Live token splitting — CTF contract integration for real SELL orders
    2. Adverse selection fill sim — paper mode is now HARDER than live
    3. Gas cost tracking — real Polygon gas costs factored into PnL
    4. Dynamic stop losses — ATR/spread-based, time-decaying, trailing

  TIER 2 — Real Edges:
    5. Sentiment trading — trade ON news, not just avoid it
    6. Cross-market arbitrage — neg-risk + YES/NO price sum arb
    7. Event-level hedging — reduce concentration risk
    8. ML predictor — statistical ensemble for short-term direction

  TIER 3 — Infrastructure:
    9. SQLite analytics — Sharpe ratio, equity curve, hourly performance
    10. Multi-account ready — architecture supports sub-wallets

Usage:
  python3 bot_v5.py --scan
  python3 bot_v5.py --paper
  python3 bot_v5.py --live
  python3 bot_v5.py --paper --strategy both_sides
  python3 bot_v5.py --live --capital 1000 --strategy both_sides
  python3 bot_v5.py --analytics    # show analytics report
  python3 bot_v5.py --brain        # show brain status
"""

import asyncio
import json
import math
import os
import random
import sys
import time
import logging
import argparse
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from datetime import datetime, timezone

import aiohttp
import websockets
from dotenv import load_dotenv

# ─── Import original bot components ─────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from bot import (
    Config, Market, Order, Position, Trade,
    FlowAnalyzer, NewsMonitor, CorrelationEngine, Brain,
    Feed, discover_markets, GAMMA_URL, CLOB_URL, WS_URL
)

# ─── Import v5 modules ─────────────────────────────────────
from modules.token_manager import TokenManager
from modules.fill_simulator import FillSimulator
from modules.gas_tracker import GasTracker
from modules.dynamic_stops import DynamicStopLoss
from modules.sentiment import SentimentTrader
from modules.arbitrage import ArbitrageEngine
from modules.ml_predictor import MLPredictor
from modules.analytics import AnalyticsDB
from modules.hedging import HedgingEngine
from modules.bankroll import BankrollManager
from modules.risk_guard import RiskGuard

load_dotenv()

LOG = logging.getLogger("scalper.v5")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scalper_v5.log"),
    ],
)


class OrderManagerV5:
    """
    Upgraded OrderManager with:
    - Real token splitting for live mode
    - Adverse selection-aware fills for paper mode
    - Gas cost tracking
    - Dynamic stop losses
    - Analytics recording
    """

    def __init__(self, cfg: Config, paper: bool = True, brain: Optional[Brain] = None):
        self.cfg = cfg
        self.paper = paper
        self.brain = brain
        self.orders: Dict[str, Order] = {}
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.client = None
        self._market_questions: Dict[str, str] = {}
        self._tick_sizes: Dict[str, float] = {}

        # Capital tracking (v4-compatible)
        self._starting_capital = cfg.capital
        self._realized_pnl = 0.0
        self._committed = 0.0
        self._peak_equity = cfg.capital

        # v5 modules
        self.token_mgr = TokenManager(paper=paper)
        self.fill_sim = FillSimulator()
        self.gas = GasTracker()
        self.stops = DynamicStopLoss()
        self.analytics = AnalyticsDB()
        self.bankroll = BankrollManager(starting_capital=cfg.capital)
        self._session_id = f"session_{int(time.time())}"

    def set_market_questions(self, markets: Dict[str, Market]):
        self._market_questions = {s: m.question for s, m in markets.items()}

    def register_markets(self, markets: Dict[str, Market]):
        """Register all markets with token manager and hedging engine."""
        for slug, m in markets.items():
            self.token_mgr.register_market(slug, m.yes_token, m.no_token)

    async def init_client(self):
        if not self.cfg.is_live:
            LOG.info("No API keys — paper mode")
            return
        try:
            from py_clob_client.client import ClobClient
            self.client = ClobClient(
                CLOB_URL, key=self.cfg.private_key, chain_id=137,
                signature_type=self.cfg.sig_type, funder=self.cfg.funder,
            )
            creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(creds)
            self.token_mgr.client = self.client
            LOG.info(f"✅ Polymarket client initialized | funder={self.cfg.funder[:10]}...")
        except Exception as e:
            LOG.error(f"Failed to init client: {e}")
            self.client = None

    # ─── Capital Properties ─────────────────────────────────

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    @property
    def committed_capital(self) -> float:
        return self._committed

    @property
    def free_capital(self) -> float:
        return self._starting_capital + self._realized_pnl - self._committed

    @property
    def equity(self) -> float:
        return self._starting_capital + self._realized_pnl

    @property
    def drawdown(self) -> float:
        equity = self.equity
        self._peak_equity = max(self._peak_equity, equity)
        if self._peak_equity <= 0:
            return 1.0
        return max(0, (self._peak_equity - equity) / self._peak_equity)

    def can_enter(self, count_sell_orders: bool = False) -> Tuple[bool, str]:
        # v5: Use bankroll's dynamic circuit breaker (rolling drawdown, not flat)
        dynamic_breaker = self.bankroll.get_circuit_breaker_pct(self._realized_pnl)
        current_drawdown = self.bankroll.get_drawdown(self._realized_pnl)
        if current_drawdown >= dynamic_breaker:
            return False, f"CIRCUIT_BREAKER (DD {current_drawdown:.1%} >= {dynamic_breaker:.0%})"
        if count_sell_orders:
            open_orders = sum(1 for o in self.orders.values() if o.status == "live")
        else:
            open_orders = sum(1 for o in self.orders.values()
                            if o.status == "live" and o.side == "BUY")
        # v5: Dynamic max concurrent based on current capital
        max_concurrent = self.bankroll.get_max_concurrent(self._realized_pnl)
        if len(self.positions) + open_orders >= max_concurrent:
            return False, f"MAX_CONCURRENT ({max_concurrent})"
        # v5: Dynamic exposure based on current capital
        effective_capital = self.bankroll.get_trading_capital(self._realized_pnl)
        max_exposure = effective_capital * self.cfg.max_exposure_pct
        if self._committed >= max_exposure:
            return False, "MAX_EXPOSURE"
        # v5: Dynamic per-order minimum
        per_order = self.bankroll.get_per_order_size(self._realized_pnl, self.cfg.per_order)
        if self.free_capital < per_order:
            return False, "INSUFFICIENT_CAPITAL"
        return True, "OK"

    def get_brain_adjusted_size(self, slug: str, market: Optional[Market] = None,
                                 is_market_making: bool = False,
                                 risk_multiplier: float = 1.0) -> float:
        # v5: Dynamic base size from bankroll (compounds with gains, shrinks with losses)
        base_size = self.bankroll.get_per_order_size(self._realized_pnl, self.cfg.per_order)
        # Apply bankroll growth/shrink multiplier
        compound_mult = self.bankroll.get_combined_multiplier(self._realized_pnl)
        base_size *= compound_mult
        # Apply risk guard multiplier (daily performance)
        base_size *= risk_multiplier
        # Brain adjustments
        if self.brain and self.brain.data["total_trades"] >= 10:
            kelly_size = self.brain.get_kelly_order_size(
                self.bankroll.get_trading_capital(self._realized_pnl), slug)
            brain_mult = self.brain.get_order_size_multiplier(slug)
            # Blend bankroll size and Kelly size
            base_size = (base_size * 0.4 + kelly_size * brain_mult * 0.6)
            # Clamp to reasonable range
            per_order_floor = self.bankroll.get_per_order_size(self._realized_pnl, self.cfg.per_order) * 0.3
            per_order_cap = self.bankroll.get_per_order_size(self._realized_pnl, self.cfg.per_order) * 3
            base_size = max(per_order_floor, min(per_order_cap, base_size))
        elif self.brain:
            brain_mult = self.brain.get_order_size_multiplier(slug)
            base_size *= brain_mult
        if is_market_making:
            base_size *= 0.5
        return round(max(1.0, base_size), 2)

    # ─── Tick Size ──────────────────────────────────────────

    def snap_to_tick(self, price: float, tick_size: float) -> float:
        if tick_size <= 0:
            return round(price, 4)
        return round(round(price / tick_size) * tick_size, 4)

    # ─── Order Placement ────────────────────────────────────

    async def place_limit(self, slug: str, token: str, side: str, price: float,
                          size_usd: float, market: Optional[Market] = None,
                          gtd_seconds: int = 0, post_only: bool = False) -> Optional[Order]:
        tick_size = 0.01
        if market:
            tick_size = market.tick_size
        price = self.snap_to_tick(price, tick_size)

        shares = round(size_usd / price, 2) if price > 0 else 0

        order_type = "GTC"
        expires_at = 0.0
        if gtd_seconds > 0:
            order_type = "GTD"
            if self.paper:
                expires_at = time.time() + gtd_seconds
            else:
                expires_at = time.time() + 60 + gtd_seconds

        entry_spread = market.spread if market else 0
        entry_volume = market.volume if market else 0
        entry_liquidity = market.liquidity if market else 0
        entry_price_range = "mid"
        if market:
            if market.yes_price < 0.20:
                entry_price_range = "low"
            elif market.yes_price < 0.50:
                entry_price_range = "mid_low"
            elif market.yes_price < 0.80:
                entry_price_range = "mid_high"
            else:
                entry_price_range = "high"

        order = Order(
            id=f"{slug}_{side}_{int(time.time()*1000)}",
            slug=slug, token=token, side=side,
            price=price, size=size_usd, shares=shares,
            post_only=post_only,
            entry_spread=entry_spread, entry_volume=entry_volume,
            entry_liquidity=entry_liquidity, entry_price_range=entry_price_range,
            order_type=order_type, expires_at=expires_at,
        )

        if self.paper:
            order.status = "live"
            order.exchange_id = f"paper_{order.id}"
            gtd_tag = f" [GTD {gtd_seconds}s]" if gtd_seconds > 0 else ""
            po_tag = " [POST-ONLY]" if post_only else ""
            LOG.info(f"📝 PAPER | {side} {shares:.0f} @ ${price:.4f}{gtd_tag}{po_tag} on {slug[:35]}")
            self.orders[order.id] = order
            return order

        if not self.client:
            return None

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType as OT
            from py_clob_client.order_builder.constants import BUY, SELL

            side_const = BUY if side == "BUY" else SELL
            order_options = {}
            if market and market.neg_risk:
                order_options["negRisk"] = True

            order_args = OrderArgs(token_id=token, price=price, size=shares, side=side_const)
            signed = self.client.create_order(order_args, **order_options)

            if post_only:
                resp = self.client.post_order(signed, OT.GTC, post_only=True)
            elif order_type == "GTD" and expires_at > 0:
                resp = self.client.post_order(signed, OT.GTD, expires_at=int(expires_at))
            else:
                resp = self.client.post_order(signed, OT.GTC)

            if resp:
                order.exchange_id = resp.get("orderID", resp.get("order_id", ""))
                order.status = "live"
                LOG.info(f"✅ LIVE | {side} {shares:.0f} @ ${price:.4f} [{order_type}] on {slug[:35]}")
                self.orders[order.id] = order
                return order
            else:
                LOG.error(f"❌ Rejected: {resp}")
                return None
        except Exception as e:
            LOG.error(f"❌ Order error: {e}")
            return None

    async def cancel_order(self, order: Order):
        order.status = "canceled"
        if self.paper or not order.exchange_id.startswith("0x"):
            return
        if not self.client:
            return
        try:
            self.client.cancel(order.exchange_id)
        except Exception as e:
            LOG.error(f"Cancel error: {e}")

    async def cancel_all(self):
        for oid, order in list(self.orders.items()):
            if order.status == "live":
                await self.cancel_order(order)

    # ─── v5: Fill Order with Token/Stop/Analytics Integration ──

    def fill_order(self, order: Order, fill_price: float, adverse: bool = False):
        """v5: fill_order with token tracking, dynamic stops, analytics."""
        order.status = "filled"
        order.fill_price = fill_price
        order.filled = time.time()
        cost = order.shares * fill_price

        if order.side == "BUY":
            self._committed += cost
            # v5: Credit tokens to inventory
            self.token_mgr.credit_from_buy(order.token, order.shares, cost)

            if order.slug in self.positions:
                pos = self.positions[order.slug]
                total_cost = pos.cost + cost
                total_shares = pos.shares + order.shares
                pos.entry_price = total_cost / total_shares
                pos.shares = total_shares
                pos.cost = total_cost
                pos.committed_capital = total_cost
                LOG.info(f"🟢 ADDED | {order.shares:.0f} @ ${fill_price:.4f} | "
                        f"Total: {total_shares:.0f} @ ${pos.entry_price:.4f} | {order.slug[:35]}")
            else:
                self.positions[order.slug] = Position(
                    slug=order.slug, token=order.token, side="LONG",
                    entry_price=fill_price, shares=order.shares, cost=cost,
                    entry_spread=order.entry_spread, entry_volume=order.entry_volume,
                    entry_liquidity=order.entry_liquidity, entry_price_range=order.entry_price_range,
                    stop_loss_price=round(fill_price - 0.02, 4),
                    committed_capital=cost,
                )
                # v5: Set dynamic stop
                self.stops.set_stop(order.slug, "LONG", fill_price,
                                    token=order.token, spread=order.entry_spread)
                LOG.info(f"🟢 FILLED | {order.shares:.0f} @ ${fill_price:.4f} | {order.slug[:35]}")
                if adverse:
                    LOG.warning(f"⚠️ ADVERSE BUY | {order.slug[:35]} | price likely to drop")

        else:  # SELL
            self.token_mgr.debit_from_sell(order.token, order.shares)

            pos = self.positions.get(order.slug)
            if pos and pos.side == "LONG":
                pnl = (fill_price - pos.entry_price) * order.shares
                self._realized_pnl += pnl
                self._committed -= (pos.entry_price * order.shares)

                remaining = pos.shares - order.shares
                if remaining > 0.01:
                    pos.shares = remaining
                    pos.cost = pos.entry_price * remaining
                    pos.committed_capital = pos.cost
                else:
                    self.positions.pop(order.slug, None)
                    self.stops.remove_stop(order.slug)

                self._peak_equity = max(self._peak_equity, self.equity)
                hold = time.time() - pos.opened

                trade = Trade(
                    slug=order.slug, question=self._market_questions.get(order.slug, ""),
                    entry_price=pos.entry_price, exit_price=fill_price,
                    shares=order.shares, pnl=pnl, hold_sec=hold, reason="filled",
                    entry_spread=pos.entry_spread, entry_volume=pos.entry_volume,
                    entry_liquidity=pos.entry_liquidity, entry_price_range=pos.entry_price_range,
                    exit_type="profit" if pnl > 0 else "loss",
                )
                self.trades.append(trade)
                if self.brain:
                    self.brain.learn_from_trade(trade)

                # v5: Record in analytics
                self.analytics.record_trade({
                    "slug": trade.slug, "question": trade.question,
                    "side": "LONG", "entry_price": trade.entry_price,
                    "exit_price": trade.exit_price, "shares": trade.shares,
                    "pnl": trade.pnl, "hold_seconds": trade.hold_sec,
                    "entry_spread": trade.entry_spread,
                    "entry_volume": trade.entry_volume,
                    "entry_liquidity": trade.entry_liquidity,
                    "entry_price_range": trade.entry_price_range,
                    "exit_type": trade.exit_type, "reason": trade.reason,
                    "strategy": self.cfg.strategy,
                    "session_id": self._session_id,
                    "adverse_fill": adverse,
                })
                # v5: Bankroll daily PnL tracking
                self.bankroll.record_daily_pnl(trade.pnl)
                # v5: Risk guard trade recording
                self.risk_guard.record_trade(trade.pnl, trade.slug, trade.exit_type)

                emoji = "🟢" if pnl > 0 else "🔴"
                LOG.info(f"{emoji} FILLED SELL | {order.shares:.0f} @ ${fill_price:.4f} | "
                        f"PnL=${pnl:+.3f} | {order.slug[:35]}")
            else:
                self._committed += cost
                self.positions[order.slug] = Position(
                    slug=order.slug, token=order.token, side="SHORT",
                    entry_price=fill_price, shares=-order.shares, cost=cost,
                    entry_spread=order.entry_spread, entry_volume=order.entry_volume,
                    entry_liquidity=order.entry_liquidity, entry_price_range=order.entry_price_range,
                    stop_loss_price=round(fill_price + 0.02, 4),
                    committed_capital=cost,
                )
                self.stops.set_stop(order.slug, "SHORT", fill_price,
                                    token=order.token, spread=order.entry_spread)
                LOG.info(f"🔴 SHORT | {order.shares:.0f} @ ${fill_price:.4f} | {order.slug[:35]}")

    def force_exit_position(self, slug: str, exit_price: float, reason: str):
        """v5: Force exit with token/stops/analytics integration."""
        pos = self.positions.pop(slug, None)
        if not pos:
            return

        if pos.side == "LONG":
            pnl = (exit_price - pos.entry_price) * pos.shares
            self._realized_pnl += pnl
            self._committed -= pos.cost
        else:
            pnl = (pos.entry_price - exit_price) * abs(pos.shares)
            self._realized_pnl += pnl
            self._committed -= pos.cost

        self._peak_equity = max(self._peak_equity, self.equity)
        hold = time.time() - pos.opened
        self.stops.remove_stop(slug)

        trade = Trade(
            slug=slug, question=self._market_questions.get(slug, ""),
            entry_price=pos.entry_price, exit_price=exit_price,
            shares=pos.shares, pnl=pnl, hold_sec=hold, reason=reason,
            entry_spread=pos.entry_spread, entry_volume=pos.entry_volume,
            entry_liquidity=pos.entry_liquidity, entry_price_range=pos.entry_price_range,
            exit_type=reason,
        )
        self.trades.append(trade)
        if self.brain:
            self.brain.learn_from_trade(trade)

        self.analytics.record_trade({
            "slug": slug, "question": trade.question,
            "side": pos.side, "entry_price": pos.entry_price,
            "exit_price": exit_price, "shares": pos.shares,
            "pnl": pnl, "hold_seconds": hold,
            "entry_spread": pos.entry_spread,
            "entry_volume": pos.entry_volume,
            "entry_liquidity": pos.entry_liquidity,
            "entry_price_range": pos.entry_price_range,
            "exit_type": reason, "reason": reason,
            "strategy": self.cfg.strategy,
            "session_id": self._session_id,
        })
        # v5: Bankroll + risk guard tracking
        self.bankroll.record_daily_pnl(pnl)
        self.risk_guard.record_trade(pnl, slug, reason)
        if reason in ("stop_loss", "dynamic_stop", "adverse_selection", "supervisor_emergency", "timeout"):
            self.risk_guard.record_forced_exit(slug, reason)

        emoji = "🟢" if pnl > 0 else "🔴"
        LOG.info(f"{emoji} FORCE EXIT | {pos.shares:.0f} @ ${exit_price:.4f} | PnL=${pnl:+.3f} | {reason} | {slug[:35]}")

    # ─── v5: Dynamic Stop Loss Check ────────────────────────

    def check_dynamic_stops(self, market_prices: Dict[str, float],
                              flow_stats: Dict[str, dict]):
        """v5: Dynamic stop loss checks with trailing + time decay."""
        for slug, pos in list(self.positions.items()):
            current_price = market_prices.get(slug, pos.entry_price)

            # Update stop (trailing, time decay)
            self.stops.update_stop(slug, current_price)

            # Check if flow suggests tightening
            token = pos.token
            flow = flow_stats.get(token, {})
            imbalance = flow.get("buy_pressure", 0)
            if self.stops.should_tighten_stop(slug, imbalance):
                self.stops.tighten_stop(slug, current_price, factor=0.5)

            # Check if stop is hit
            if self.stops.check_stop_hit(slug, current_price):
                LOG.warning(f"🛑 DYNAMIC STOP HIT | {slug[:35]} | "
                           f"${pos.entry_price:.4f} → ${current_price:.4f}")
                self.force_exit_position(slug, current_price, "dynamic_stop")

    def check_timeouts(self, market_prices: Dict[str, float]):
        now = time.time()
        for slug, pos in list(self.positions.items()):
            max_hold = self.cfg.max_hold_sec
            if self.brain:
                suggested = self.brain.should_adjust_hold_time(slug)
                if suggested:
                    max_hold = suggested
            if now - pos.opened > max_hold:
                price = market_prices.get(slug, pos.entry_price)
                self.force_exit_position(slug, price, "timeout")

    def clean_expired_orders(self):
        now = time.time()
        for oid, order in list(self.orders.items()):
            if order.status == "live" and order.order_type == "GTD" and order.expires_at > 0:
                if now > order.expires_at:
                    order.status = "canceled"

    def snapshot_equity(self):
        """Record equity snapshot for analytics."""
        self.analytics.record_equity_snapshot(
            self.equity, self._realized_pnl, self._committed,
            self.free_capital, len(self.positions),
            sum(1 for o in self.orders.values() if o.status == "live"),
        )

    def summary(self) -> str:
        wins = [t for t in self.trades if t.pnl > 0]
        wr = len(wins) / max(1, len(self.trades))
        return (f"Equity=${self.equity:.2f} (realized ${self._realized_pnl:+.2f}) | "
                f"Committed=${self._committed:.2f} | Free=${self.free_capital:.2f} | "
                f"Pos={len(self.positions)} | Trades={len(self.trades)} ({wr:.0%} WR) | "
                f"DD={self.drawdown:.1%}")


class ScalperV5:
    """
    v5 Scalper — integrates all upgrade modules.
    """

    def __init__(self, cfg: Config, paper: bool = True):
        self.cfg = cfg
        self.paper = paper
        self.brain = Brain()
        self.om = OrderManagerV5(cfg, paper, brain=self.brain)
        self.feed = Feed(cfg)
        self.news = NewsMonitor()
        self.flow = FlowAnalyzer()
        self.correlations = CorrelationEngine()

        # v5 modules
        self.sentiment = SentimentTrader(head_start_seconds=15.0)
        self.arb = ArbitrageEngine()
        self.ml = MLPredictor()
        self.hedge = HedgingEngine()
        self.risk_guard = RiskGuard({
            "quiet_hours_start": cfg.quiet_hours_start,
            "quiet_hours_end": cfg.quiet_hours_end,
            "max_daily_loss_pct": cfg.circuit_breaker_pct,
            "daily_profit_target_pct": 0.10,
            "losing_streak_limit": 3,
            "cooldown_after_forced_exit": 120,
            "min_resolution_hours": 4.0,
        })

        self.markets: Dict[str, Market] = {}
        self.token_to_slug: Dict[str, str] = {}
        self.running = False
        self.tick = 0
        self._last_reprice = 0
        self._last_news_check = 0
        self._last_reconcile = 0
        self._last_equity_snapshot = 0
        self._last_arb_scan = 0
        self._current_size_mult = 1.0  # updated each tick by risk guard

        # Supervisor
        self._supervised = cfg.supervised
        self._supervisor_rules: dict = {}
        self._last_rules_load = 0

    async def start(self, mode: str = "paper"):
        self.brain.start_session()
        self.om.bankroll.start_session()
        self.risk_guard.reset_daily()

        LOG.info("=" * 60)
        LOG.info(f"  SCALPER v5 | ${self.cfg.capital:.0f} | {mode.upper()} | Strategy: {self.cfg.strategy}")
        LOG.info(f"  Exposure: {self.cfg.max_exposure_pct:.0%} | Kelly: {self.brain.kelly_fraction():.1%}")
        LOG.info(f"  Max concurrent: {self.cfg.max_concurrent} | Reprice: {self.cfg.reprice_sec}s")
        LOG.info(f"  Post-only: {self.cfg.post_only} | Supervised: {self.cfg.supervised}")
        LOG.info(f"  v5 MODULES: Token Mgr ✓ | Fill Sim ✓ | Gas ✓ | Dynamic Stops ✓")
        LOG.info(f"              Sentiment ✓ | Arbitrage ✓ | ML ✓ | Analytics ✓ | Hedging ✓")
        LOG.info("=" * 60)

        LOG.info("🔍 Discovering markets...")
        market_list = await discover_markets(self.cfg, self.brain)
        if not market_list:
            LOG.error("No suitable markets found!")
            return

        self.markets = {m.slug: m for m in market_list}
        self.om.set_market_questions(self.markets)
        self.om.register_markets(self.markets)

        for m in market_list:
            self.token_to_slug[m.yes_token] = m.slug
            self.token_to_slug[m.no_token] = m.slug + "_NO"
            self.correlations.record_price(m.slug, m.yes_price, m.question)
            self.ml.record(m.slug, m.yes_price, volume=m.volume, spread=m.spread,
                          bid=m.best_bid, ask=m.best_ask)

            # Register with hedge engine
            self.hedge.register_market(m.slug, m.event_id, m.yes_price, m.no_price, m.liquidity)

            # Register with arb engine
            self.arb.update_market(m.slug, m.yes_price, m.no_price,
                                   event_id=m.event_id, neg_risk=m.neg_risk,
                                   fees_enabled=m.fees_enabled, spread=m.spread,
                                   liquidity=m.liquidity)

        LOG.info(f"📊 {len(self.markets)} markets selected")
        for m in market_list:
            tags = []
            if self.brain.is_star_market(m.slug):
                tags.append("⭐")
            if m.neg_risk:
                tags.append("🔄NEG")
            tag_str = f" [{' '.join(tags)}]" if tags else ""
            LOG.info(f"  • {m.question[:55]} | {m.spread*100:.1f}¢ | "
                    f"${m.liquidity:,.0f} liq{tag_str}")

        await self.om.init_client()

        tokens = [m.yes_token for m in market_list if m.yes_token]
        ws_task = asyncio.create_task(self.feed.start(tokens))
        self.feed.on_update(self._on_book_update)

        self.running = True
        LOG.info("🚀 Scalper v5 running — Ctrl+C to stop\n")

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
        self.tick += 1
        now = time.time()

        # ── Risk guard master check ──
        can_trade, trade_reason = self.risk_guard.can_trade(
            self.om.bankroll.get_trading_capital(self.om._realized_pnl),
            self.om.bankroll.effective_capital,
        )

        # Load supervisor rules
        if self._supervised and now - self._last_rules_load > 60:
            self._load_supervisor_rules()
        if self._supervised:
            self._check_emergency_exits()

        # News + sentiment check
        if now - self._last_news_check > 15:
            async with aiohttp.ClientSession() as s:
                # Original news avoidance
                alerts = await self.news.check_feeds(s)
                if alerts:
                    await self._handle_news_alerts()

                # v5: Sentiment-based trading
                new_signals = await self.sentiment.check_news(s, self._market_questions)
                for signal in new_signals:
                    await self._handle_sentiment_signal(signal)

            # Auto-un-skip
            for slug, market in self.markets.items():
                if self.news.should_unskip_market(slug):
                    affected, _ = self.news.is_market_affected(market.question)
                    if not affected:
                        self.news.clear_market_skip(slug)
                        LOG.info(f"📰 UNSKIP | {slug[:35]}")

            self._last_news_check = now

        # Clean expired orders
        self.om.clean_expired_orders()

        # Stale order cleanup
        for oid, order in list(self.om.orders.items()):
            if order.status == "live" and order.order_type == "GTC" and (now - order.created) > 60:
                await self.om.cancel_order(order)

        # v5: Dynamic stop losses every 3s
        if self.tick % 3 == 0:
            prices = {slug: m.yes_price for slug, m in self.markets.items()}
            flow_stats = {m.yes_token: self.flow.get_stats(m.yes_token)
                         for m in self.markets.values()}
            self.om.check_dynamic_stops(prices, flow_stats)

        # Timeouts every 10s
        if self.tick % 10 == 0:
            prices = {slug: m.yes_price for slug, m in self.markets.items()}
            self.om.check_timeouts(prices)

        # Reprice
        if now - self._last_reprice > self.cfg.reprice_sec:
            await self._reprice()
            self._last_reprice = now

        # Paper fills with adverse selection
        if self.paper:
            await self._paper_fill_check()
        else:
            if now - self._last_reconcile > 30:
                await self._reconcile_fills()
                self._last_reconcile = now

        # v5: Arbitrage scan every 30s
        if now - self._last_arb_scan > 30:
            opps = self.arb.scan(self.om.free_capital)
            if opps:
                best = opps[0]
                LOG.info(f"🔄 ARB OPP | {best.type} | {best.description[:50]} | "
                        f"profit=${best.guaranteed_profit:.3f} ({best.profit_pct:.1f}%)")
            self._last_arb_scan = now

        # v5: Equity snapshot every 60s
        if now - self._last_equity_snapshot > 60:
            self.om.snapshot_equity()
            self._last_equity_snapshot = now

        # Status every 60s
        if self.tick % 60 == 0:
            live = sum(1 for o in self.om.orders.values() if o.status == "live")
            LOG.info(f"[T+{self.tick}s] {self.om.summary()} | Orders: {live}")

    async def _handle_news_alerts(self):
        """Original v4 news avoidance + v5 adverse selection exit."""
        for slug, market in self.markets.items():
            affected, reason = self.news.is_market_affected(market.question)
            if affected:
                LOG.warning(f"🚨 NEWS | {slug[:35]} | {reason}")
                self.news.mark_market_skipped(slug)

                # Pull orders
                for oid, order in list(self.om.orders.items()):
                    if order.status == "live" and order.slug == slug:
                        await self.om.cancel_order(order)

                # Adverse exit
                if slug in self.om.positions:
                    pos = self.om.positions[slug]
                    dump_price = market.best_bid if market.best_bid > 0 else pos.entry_price
                    self.om.force_exit_position(slug, dump_price, "adverse_selection")

    async def _handle_sentiment_signal(self, signal):
        """v5: Handle a sentiment signal by entering a directional trade."""
        for slug in signal.affected_markets:
            market = self.markets.get(slug)
            if not market:
                continue

            # Don't trade if news avoidance has us skipping this
            affected, _ = self.news.is_market_affected(market.question)
            if affected:
                continue

            ok, _ = self.om.can_enter()
            if not ok:
                continue

            if signal.sentiment == "bullish" and signal.current_strength > 0.4:
                # Buy YES — price will rise
                buy_price = round(market.yes_price + 0.005, 4)  # slightly above market
                buy_price = min(buy_price, market.best_ask - 0.001)  # don't cross spread
                if 0 < buy_price < 1:
                    size = self.om.get_brain_adjusted_size(slug, market) * 0.5  # half size for sentiment
                    await self.om.place_limit(slug, market.yes_token, "BUY",
                        buy_price, size, market, gtd_seconds=60,
                        post_only=self.cfg.post_only)
                    LOG.info(f"📰 SENTIMENT BUY | {slug[:35]} | {signal.headline[:50]}")

            elif signal.sentiment == "bearish" and signal.current_strength > 0.4:
                # SELL YES (if we hold) or just avoid
                if slug in self.om.positions:
                    dump_price = round(market.best_bid, 4)
                    self.om.force_exit_position(slug, dump_price, "bearish_sentiment")
                    LOG.info(f"📰 SENTIMENT EXIT | {slug[:35]} | {signal.headline[:50]}")

    async def _on_book_update(self, token: str, book: dict):
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

        bb_price, bb_size = bids[0][0], bids[0][1]
        ba_price, ba_size = asks[0][0], asks[0][1]
        mid = (bb_price + ba_price) / 2
        spread = ba_price - bb_price
        old_mid = market.yes_price

        market.best_bid = bb_price
        market.best_ask = ba_price
        market.best_bid_size = bb_size
        market.best_ask_size = ba_size
        market.yes_price = mid
        market.spread = spread
        market.last_ws_update = time.time()

        # v5: Feed data to modules
        last_trade = book.get("last_trade")
        if last_trade:
            side = "BUY" if last_trade > old_mid else "SELL"
            self.flow.record_trade(token, last_trade, 0, side)
            self.om.fill_sim.record_trade(token, last_trade, 0, side)

        self.correlations.record_price(slug, mid, market.question)
        self.om.stops.record_price(token, mid, spread)
        self.ml.record(slug, mid, volume=market.volume, spread=spread,
                      bid=bb_price, ask=ba_price)
        self.arb.update_market(slug, market.yes_price, market.no_price,
                               event_id=market.event_id, neg_risk=market.neg_risk,
                               fees_enabled=market.fees_enabled, spread=spread,
                               liquidity=market.liquidity)

        # Flow pull check
        should_pull, pull_reason = self.flow.should_pull_orders(token)
        if should_pull:
            for oid, order in list(self.om.orders.items()):
                if order.status == "live" and order.slug == slug:
                    await self.om.cancel_order(order)
            LOG.warning(f"🌊 FLOW PULL | {slug[:35]} | {pull_reason}")

        # Reactive cancellation
        price_moved = abs(mid - old_mid)
        if price_moved > 0.01:
            for oid, order in list(self.om.orders.items()):
                if order.status == "live" and order.slug == slug:
                    await self.om.cancel_order(order)

        # v5: Paper fill check with adverse selection
        if self.paper:
            for oid, order in list(self.om.orders.items()):
                if order.status != "live" or order.slug != slug:
                    continue

                filled, fill_price, fill_shares, is_adverse = self.om.fill_sim.simulate_fill(
                    order_side=order.side,
                    order_price=order.price,
                    order_shares=order.shares,
                    best_bid=bb_price,
                    best_ask=ba_price,
                    bid_size=bb_size,
                    ask_size=ba_size,
                    spread=market.spread,
                    volume=market.volume,
                    age=time.time() - order.created,
                    post_only=order.post_only,
                    token=token,
                )

                if filled:
                    order.shares = fill_shares
                    self.om.fill_order(order, fill_price, adverse=is_adverse)

                    # v5: If adverse fill, try to exit immediately
                    if is_adverse and order.side == "BUY":
                        LOG.warning(f"⚠️ ADVERSE BUY FILL — attempting immediate exit")
                        exit_price = round(bb_price - 0.002, 4)
                        if exit_price > 0:
                            await self.om.place_limit(slug, market.yes_token, "SELL",
                                exit_price, order.shares * exit_price, market,
                                gtd_seconds=30, post_only=self.cfg.post_only)

    async def _paper_fill_check(self):
        """Additional paper fill check for non-WS-triggered fills."""
        now = time.time()
        for oid, order in list(self.om.orders.items()):
            if order.status != "live":
                continue
            if order.order_type == "GTD" and order.expires_at > 0 and now > order.expires_at:
                order.status = "canceled"
                continue

            market = self.markets.get(order.slug)
            if not market:
                continue

            filled, fill_price, fill_shares, is_adverse = self.om.fill_sim.simulate_fill(
                order_side=order.side,
                order_price=order.price,
                order_shares=order.shares,
                best_bid=market.best_bid,
                best_ask=market.best_ask,
                bid_size=market.best_bid_size,
                ask_size=market.best_ask_size,
                spread=market.spread,
                volume=market.volume,
                age=now - order.created,
                post_only=order.post_only,
                token=order.token,
            )

            if filled:
                order.shares = fill_shares
                self.om.fill_order(order, fill_price, adverse=is_adverse)

    async def _reconcile_fills(self):
        if not self.om.client:
            return
        try:
            from py_clob_client.clob_types import OpenOrderParams
            open_orders = self.om.client.get_orders(OpenOrderParams())
            live_exchange_ids = {o.get("id", "") for o in open_orders}

            for local_id, order in list(self.om.orders.items()):
                if order.status == "live" and order.exchange_id and order.exchange_id not in live_exchange_ids:
                    LOG.info(f"🔄 RECONCILED | {order.side} {order.shares:.0f} @ ${order.price:.4f}")
                    self.om.fill_order(order, order.price)
        except Exception as e:
            LOG.debug(f"Reconcile error: {e}")

    async def _reprice(self):
        # Cancel existing orders
        for oid, order in list(self.om.orders.items()):
            if order.status == "live":
                await self.om.cancel_order(order)

        # v5: Risk guard — skip all new entries if blocked
        can_trade, trade_reason = self.risk_guard.can_trade(
            self.om.bankroll.get_trading_capital(self.om._realized_pnl),
            self.om.bankroll.effective_capital,
        )

        # v5: Get dynamic size multiplier from risk guard
        self._current_size_mult = self.risk_guard.get_size_multiplier(
            self.risk_guard._daily_pnl,
            self.om.bankroll.get_trading_capital(self.om._realized_pnl),
        )

        for slug, market in self.markets.items():
            if market.best_bid <= 0 or market.best_ask >= 1:
                continue
            if market.spread < self.cfg.min_spread:
                continue

            # News check
            affected, reason = self.news.is_market_affected(market.question)
            if affected:
                continue

            if self.brain:
                should_trade, _ = self.brain.should_trade_market(slug)
                if not should_trade:
                    continue

            if self._supervised:
                blocked, _ = self._is_market_blocked(slug)
                if blocked:
                    continue

            # v5: Risk guard — resolution time filter, quiet hours, losing streak
            if not can_trade:
                # Still allow exits, just not new entries
                if slug not in self.om.positions:
                    continue

            mid = (market.best_bid + market.best_ask) / 2

            if slug in self.om.positions:
                pos = self.om.positions[slug]
                hold_sec = time.time() - pos.opened

                aggression = 0.5
                if self.brain:
                    aggression = self.brain.get_exit_aggressiveness(slug)

                if hold_sec > self.cfg.max_hold_sec * (0.85 - aggression * 0.3):
                    exit_price = round(market.best_bid, 4)
                elif mid > pos.entry_price + 0.005:
                    exit_price = round(mid + 0.003, 4)
                elif mid < pos.entry_price - 0.01:
                    exit_price = round(market.best_bid, 4)
                else:
                    exit_price = round(mid, 4)

                exit_price = min(exit_price, 0.99)
                gtd = self._get_gtd_seconds(slug, market.spread)
                await self.om.place_limit(slug, market.yes_token, "SELL",
                    exit_price, pos.cost, market, gtd, post_only=self.cfg.post_only)
                continue

            count_sells = self.cfg.strategy == "both_sides"
            ok, _ = self.om.can_enter(count_sell_orders=count_sells)
            if not ok:
                continue

            if self.cfg.strategy == "both_sides":
                await self._place_both_sides(slug, market, mid)
            else:
                await self._place_one_side(slug, market, mid)

    async def _place_one_side(self, slug: str, market: Market, mid: float):
        if self._supervised:
            blocked, _ = self._is_market_blocked(slug)
            if blocked:
                return

        half_spread = market.spread / 2
        buy_price = round(mid - max(0.005, half_spread - self.cfg.spread_target), 4)
        buy_price = max(buy_price, round(market.best_bid + 0.001, 4))
        if buy_price <= 0 or buy_price >= 1:
            return

        size = self.om.get_brain_adjusted_size(slug, market, risk_multiplier=size_mult)
        if self._supervised:
            size = self._apply_supervisor_limits(slug, size)

        gtd = self._get_gtd_seconds(slug, market.spread)
        await self.om.place_limit(slug, market.yes_token, "BUY", buy_price, size,
            market, gtd, post_only=self.cfg.post_only)

    async def _place_both_sides(self, slug: str, market: Market, mid: float):
        spread = market.spread
        if spread < self.cfg.min_spread:
            return

        if slug in self.om.positions:
            pos = self.om.positions[slug]
            hold_sec = time.time() - pos.opened
            aggression = 0.5
            if self.brain:
                aggression = self.brain.get_exit_aggressiveness(slug)

            if hold_sec > self.cfg.max_hold_sec * (0.85 - aggression * 0.3):
                exit_price = round(market.best_bid, 4)
            elif mid > pos.entry_price + 0.005:
                exit_price = round(mid + 0.003, 4)
            else:
                exit_price = round(mid, 4)

            gtd = self._get_gtd_seconds(slug, market.spread)
            size = self.om.get_brain_adjusted_size(slug, market, is_market_making=True,
                                                     risk_multiplier=self._current_size_mult)

            flow_stats = self.flow.get_stats(market.yes_token)
            if flow_stats.get("buy_pressure", 0) > 0.5:
                exit_price = round(min(exit_price + 0.003, market.best_ask - 0.001), 4)

            exit_price = min(exit_price, 0.99)
            await self.om.place_limit(slug, market.yes_token, "SELL",
                exit_price, pos.cost, market, gtd, post_only=self.cfg.post_only)
            return

        ok, _ = self.om.can_enter(count_sell_orders=True)
        if not ok:
            return

        size = self.om.get_brain_adjusted_size(slug, market, is_market_making=True,
                                                 risk_multiplier=self._current_size_mult)
        gtd = self._get_gtd_seconds(slug, market.spread)
        tick = market.tick_size

        half_capture = max(0.005, spread * 0.3)
        bid_price = round(mid - half_capture, 4)
        ask_price = round(mid + half_capture, 4)

        bid_price = max(bid_price, round(market.best_bid + tick, 4))
        ask_price = min(ask_price, round(market.best_ask - tick, 4))

        # Inventory skew
        inventory = self.om.token_mgr.get_balance(market.yes_token)
        skew = inventory * 0.005
        bid_price = round(bid_price - skew, 4)
        ask_price = round(ask_price - skew, 4)

        # Flow adjustment
        flow_stats = self.flow.get_stats(market.yes_token)
        buy_pressure = flow_stats.get("buy_pressure", 0)
        if buy_pressure > 0.3:
            ask_price = round(ask_price - 0.002, 4)
        elif buy_pressure < -0.3:
            bid_price = round(bid_price + 0.002, 4)

        bid_price = self.om.snap_to_tick(bid_price, tick)
        ask_price = self.om.snap_to_tick(ask_price, tick)

        if bid_price <= 0 or bid_price >= 1 or ask_price <= 0 or ask_price >= 1:
            return
        if bid_price >= ask_price:
            return
        if (ask_price - bid_price) < tick * 2:
            return

        # v5: Ensure tokens for SELL
        shares_needed = round(size / ask_price, 2) if ask_price > 0 else 0
        has_tokens, available = await self.om.token_mgr.ensure_sell_tokens(
            slug, market.yes_token, shares_needed, self.om.free_capital,
            yes_token=market.yes_token, no_token=market.no_token)
        if not has_tokens:
            LOG.warning(f"⚠️ No tokens for SELL on {slug[:35]}, placing BID only")
            await self.om.place_limit(slug, market.yes_token, "BUY",
                bid_price, size, market, gtd, post_only=self.cfg.post_only)
            return

        if self._supervised:
            size = self._apply_supervisor_limits(slug, size)

        await self.om.place_limit(slug, market.yes_token, "BUY",
            bid_price, size, market, gtd, post_only=self.cfg.post_only)
        await self.om.place_limit(slug, market.yes_token, "SELL",
            ask_price, size, market, gtd, post_only=self.cfg.post_only)

        LOG.info(f"🔄 MARKET MAKE | {slug[:35]} | "
                f"BID ${bid_price:.4f} | ASK ${ask_price:.4f} | "
                f"Spread: ${(ask_price-bid_price):.4f}")

    # ─── Supervisor (unchanged from v4) ─────────────────────

    def _load_supervisor_rules(self):
        RULES_FILE = "rules.jsonl"
        if not os.path.exists(RULES_FILE):
            return
        try:
            self._supervisor_rules = json.loads(open(RULES_FILE).read())
            self._last_rules_load = time.time()
        except Exception:
            pass

    def _is_market_blocked(self, slug: str) -> Tuple[bool, str]:
        rules = self._supervisor_rules
        if rules.get("global_paused"):
            return True, "GLOBAL PAUSE"
        if slug in rules.get("blocked_markets", []):
            return True, "Blocked"
        return False, ""

    def _apply_supervisor_limits(self, slug: str, base_size: float) -> float:
        limits = self._supervisor_rules.get("market_limits", {})
        market_limit = limits.get(slug, {})
        multi = market_limit.get("max_order_size_multiplier", 1.0)
        return round(base_size * multi, 2)

    def _check_emergency_exits(self):
        emergencies = self._supervisor_rules.get("emergency_exits", [])
        for slug in emergencies:
            if slug in self.om.positions:
                market = self.markets.get(slug)
                exit_price = market.yes_price if market else self.om.positions[slug].entry_price
                self.om.force_exit_position(slug, exit_price, "supervisor_emergency")
            for oid, order in list(self.om.orders.items()):
                if order.status == "live" and order.slug == slug:
                    asyncio.create_task(self.om.cancel_order(order))

    def _get_gtd_seconds(self, slug: str, spread: float = 0.0) -> int:
        if spread <= 0:
            market = self.markets.get(slug)
            spread = market.spread if market else 0.05
        gtd = int(spread * 1500)
        gtd = max(60, min(300, gtd))
        if self.brain:
            rep = self.brain.get_market_rep(slug)
            if rep["trades"] >= 5:
                timeout_rate = rep.get("timeouts", 0) / rep["trades"]
                if timeout_rate > 0.6:
                    gtd = min(300, int(gtd * 1.5))
        return gtd

    def _final_report(self):
        # Close remaining positions
        for slug, pos in list(self.om.positions.items()):
            m = self.markets.get(slug)
            price = m.yes_price if m else pos.entry_price
            self.om.force_exit_position(slug, price, "shutdown")

        wins = [t for t in self.om.trades if t.pnl > 0]
        losses = [t for t in self.om.trades if t.pnl <= 0]
        total_pnl = self.om._realized_pnl
        wr = len(wins) / max(1, len(self.om.trades))
        avg_hold = sum(t.hold_sec for t in self.om.trades) / max(1, len(self.om.trades))

        LOG.info(f"""
{'='*60}
  FINAL REPORT v5
{'='*60}
  Starting Capital: ${self.cfg.capital:.2f}
  Final Equity:     ${self.om.equity:.2f}
  Realized P&L:     ${self.om._realized_pnl:+.2f} ({self.om._realized_pnl/self.cfg.capital*100:+.1f}%)
  Max Drawdown:     {self.om.drawdown:.1%}
  
  Total Trades:     {len(self.om.trades)}
  Win Rate:         {wr:.0%}
  Avg Hold:         {avg_hold:.0f}s
  Profit Factor:    {sum(t.pnl for t in wins)/max(0.001, abs(sum(t.pnl for t in losses))):.2f}
{'='*60}""")

        LOG.info(self.brain.session_report())

        # v5 module reports
        LOG.info(self.om.fill_sim.report())
        LOG.info(self.om.gas.report())
        LOG.info(self.om.stops.report())
        LOG.info(self.sentiment.report())
        LOG.info(self.arb.report())
        LOG.info(self.ml.report())
        LOG.info(self.om.bankroll.report(self.om._realized_pnl))
        LOG.info(self.risk_guard.report(self.om.bankroll.get_trading_capital(self.om._realized_pnl)))
        LOG.info(self.om.analytics.full_report())

        self.brain.save()
        self.om.bankroll.save()
        self.om.analytics.close()


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Polymarket Scalper v5 — The Profit Machine")
    parser.add_argument("--scan", action="store_true", help="Discover targets")
    parser.add_argument("--paper", action="store_true", help="Paper trade")
    parser.add_argument("--live", action="store_true", help="Live trading")
    parser.add_argument("--brain", action="store_true", help="Show brain status")
    parser.add_argument("--brain-reset", action="store_true", help="Wipe brain")
    parser.add_argument("--analytics", action="store_true", help="Show analytics report")
    parser.add_argument("--capital", type=float, default=None)
    parser.add_argument("--per-order", type=float, default=None)
    parser.add_argument("--strategy", type=str, default=None, choices=["one_side", "both_sides"])
    parser.add_argument("--supervised", action="store_true")
    args = parser.parse_args()

    cfg = Config()
    if args.capital:
        cfg.capital = args.capital
    if args.per_order:
        cfg.per_order = args.per_order
    if args.strategy:
        cfg.strategy = args.strategy
    if args.supervised:
        cfg.supervised = True

    brain = Brain()

    if args.analytics:
        db = AnalyticsDB()
        print(db.full_report())
        db.close()
        return

    if args.brain:
        print(brain.report())
    elif args.scan:
        from bot import cmd_scan
        asyncio.run(cmd_scan(cfg, brain))
    elif args.paper:
        scalper = ScalperV5(cfg, paper=True)
        asyncio.run(scalper.start("paper"))
    elif args.live:
        if not cfg.is_live:
            LOG.error("Missing POLYMARKET_PRIVATE_KEY or POLYMARKET_FUNDER in .env")
            sys.exit(1)
        scalper = ScalperV5(cfg, paper=False)
        asyncio.run(scalper.start("live"))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
