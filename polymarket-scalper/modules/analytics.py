"""
Analytics Engine v5 — SQLite-backed trade analytics and reporting.

Replaces the simple brain.json with a proper database for:
1. Full trade history with queryable metrics
2. Sharpe ratio, Sortino ratio, max drawdown duration
3. Per-market breakdowns
4. Hourly/daily performance reports
5. Equity curve tracking
6. Fee drag analysis
7. Strategy comparison (one_side vs both_sides)
"""

import sqlite3
import time
import json
import logging
import os
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta

LOG = logging.getLogger("scalper.analytics")


class AnalyticsDB:
    """
    SQLite-backed analytics database.
    
    Tables:
    - trades: every trade with entry/exit, PnL, timing, conditions
    - equity_snapshots: periodic equity curve data
    - predictions: ML prediction outcomes
    - arb_opportunities: arbitrage detections and outcomes
    - sessions: trading session summaries
    """

    def __init__(self, db_path: str = "analytics.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self):
        """Create tables if they don't exist."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                slug TEXT NOT NULL,
                question TEXT DEFAULT '',
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL,
                shares REAL NOT NULL,
                pnl REAL DEFAULT 0,
                hold_seconds REAL DEFAULT 0,
                entry_spread REAL DEFAULT 0,
                entry_volume REAL DEFAULT 0,
                entry_liquidity REAL DEFAULT 0,
                entry_price_range TEXT DEFAULT '',
                exit_type TEXT DEFAULT '',
                reason TEXT DEFAULT '',
                strategy TEXT DEFAULT 'one_side',
                session_id TEXT DEFAULT '',
                adverse_fill INTEGER DEFAULT 0,
                gas_cost REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS equity_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                equity REAL NOT NULL,
                realized_pnl REAL DEFAULT 0,
                committed REAL DEFAULT 0,
                free_capital REAL DEFAULT 0,
                num_positions INTEGER DEFAULT 0,
                num_orders INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                slug TEXT NOT NULL,
                predicted_direction TEXT NOT NULL,
                actual_direction TEXT,
                confidence REAL DEFAULT 0,
                expected_move REAL DEFAULT 0,
                actual_move REAL DEFAULT 0,
                correct INTEGER DEFAULT 0,
                signals_json TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS arb_opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                arb_type TEXT NOT NULL,
                description TEXT DEFAULT '',
                markets_json TEXT DEFAULT '[]',
                profit_expected REAL DEFAULT 0,
                profit_actual REAL,
                executed INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT UNIQUE NOT NULL,
                started REAL NOT NULL,
                ended REAL,
                strategy TEXT DEFAULT '',
                starting_capital REAL DEFAULT 0,
                ending_capital REAL,
                total_trades INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                total_pnl REAL DEFAULT 0,
                max_drawdown REAL DEFAULT 0,
                sharpe_ratio REAL
            );

            CREATE INDEX IF NOT EXISTS idx_trades_slug ON trades(slug);
            CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp);
            CREATE INDEX IF NOT EXISTS idx_trades_session ON trades(session_id);
            CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_snapshots(timestamp);
        """)
        self.conn.commit()

    def record_trade(self, trade: dict):
        """Record a completed trade."""
        self.conn.execute("""
            INSERT INTO trades (timestamp, slug, question, side, entry_price, exit_price,
                shares, pnl, hold_seconds, entry_spread, entry_volume, entry_liquidity,
                entry_price_range, exit_type, reason, strategy, session_id, adverse_fill, gas_cost)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade.get("timestamp", time.time()),
            trade.get("slug", ""),
            trade.get("question", ""),
            trade.get("side", "LONG"),
            trade.get("entry_price", 0),
            trade.get("exit_price", 0),
            trade.get("shares", 0),
            trade.get("pnl", 0),
            trade.get("hold_seconds", 0),
            trade.get("entry_spread", 0),
            trade.get("entry_volume", 0),
            trade.get("entry_liquidity", 0),
            trade.get("entry_price_range", ""),
            trade.get("exit_type", ""),
            trade.get("reason", ""),
            trade.get("strategy", "one_side"),
            trade.get("session_id", ""),
            1 if trade.get("adverse_fill", False) else 0,
            trade.get("gas_cost", 0),
        ))
        self.conn.commit()

    def record_equity_snapshot(self, equity: float, realized_pnl: float,
                                committed: float, free_capital: float,
                                num_positions: int, num_orders: int):
        """Record an equity snapshot for the equity curve."""
        self.conn.execute("""
            INSERT INTO equity_snapshots (timestamp, equity, realized_pnl, committed,
                free_capital, num_positions, num_orders)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (time.time(), equity, realized_pnl, committed, free_capital, num_positions, num_orders))
        self.conn.commit()

    def record_prediction(self, prediction: dict):
        """Record an ML prediction."""
        self.conn.execute("""
            INSERT INTO predictions (timestamp, slug, predicted_direction, actual_direction,
                confidence, expected_move, actual_move, correct, signals_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            prediction.get("timestamp", time.time()),
            prediction.get("slug", ""),
            prediction.get("predicted_direction", ""),
            prediction.get("actual_direction"),
            prediction.get("confidence", 0),
            prediction.get("expected_move", 0),
            prediction.get("actual_move", 0),
            1 if prediction.get("correct", False) else 0,
            json.dumps(prediction.get("signals", {})),
        ))
        self.conn.commit()

    def record_session(self, session: dict):
        """Record or update a trading session."""
        self.conn.execute("""
            INSERT OR REPLACE INTO sessions (session_id, started, ended, strategy,
                starting_capital, ending_capital, total_trades, wins, losses,
                total_pnl, max_drawdown, sharpe_ratio)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            session.get("session_id", ""),
            session.get("started", time.time()),
            session.get("ended"),
            session.get("strategy", ""),
            session.get("starting_capital", 0),
            session.get("ending_capital"),
            session.get("total_trades", 0),
            session.get("wins", 0),
            session.get("losses", 0),
            session.get("total_pnl", 0),
            session.get("max_drawdown", 0),
            session.get("sharpe_ratio"),
        ))
        self.conn.commit()

    # ─── Query Methods ───────────────────────────────────────

    def get_trades(self, slug: str = None, limit: int = 100,
                    since: float = 0) -> List[dict]:
        """Get trades with optional filters."""
        query = "SELECT * FROM trades WHERE 1=1"
        params = []
        if slug:
            query += " AND slug = ?"
            params.append(slug)
        if since > 0:
            query += " AND timestamp > ?"
            params.append(since)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_win_rate(self, slug: str = None, since: float = 0) -> Tuple[int, int, float]:
        """
        Get win rate.
        Returns (wins, losses, win_rate).
        """
        query = "SELECT COUNT(*) as total, SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins FROM trades WHERE 1=1"
        params = []
        if slug:
            query += " AND slug = ?"
            params.append(slug)
        if since > 0:
            query += " AND timestamp > ?"
            params.append(since)

        row = self.conn.execute(query, params).fetchone()
        total = row["total"]
        wins = row["wins"] or 0
        losses = total - wins
        wr = wins / max(1, total)
        return wins, losses, wr

    def get_sharpe_ratio(self, since: float = 0, risk_free_rate: float = 0.05) -> Optional[float]:
        """
        Calculate Sharpe ratio from trade PnLs.
        
        Sharpe = (mean_return - risk_free_rate) / std_dev_return
        Annualized assuming ~250 trading days.
        """
        query = "SELECT pnl FROM trades WHERE 1=1"
        params = []
        if since > 0:
            query += " AND timestamp > ?"
            params.append(since)

        rows = self.conn.execute(query, params).fetchall()
        if len(rows) < 5:
            return None

        pnls = [r["pnl"] for r in rows]
        mean_pnl = sum(pnls) / len(pnls)
        variance = sum((p - mean_pnl) ** 2 for p in pnls) / len(pnls)
        std_pnl = math.sqrt(variance) if variance > 0 else 0.001

        # Risk-free per-trade (annualized / estimated trades per year)
        rf_per_trade = risk_free_rate / 1000  # assuming ~1000 trades/year

        sharpe = (mean_pnl - rf_per_trade) / max(std_pnl, 0.001)
        return round(sharpe, 3)

    def get_max_drawdown(self, since: float = 0) -> Tuple[float, float, float]:
        """
        Calculate max drawdown from equity curve.
        Returns (max_drawdown_pct, peak_equity, trough_equity).
        """
        query = "SELECT timestamp, equity FROM equity_snapshots"
        params = []
        if since > 0:
            query += " WHERE timestamp > ?"
            params.append(since)
        query += " ORDER BY timestamp"

        rows = self.conn.execute(query, params).fetchall()
        if len(rows) < 2:
            return 0, 0, 0

        peak = rows[0]["equity"]
        max_dd = 0
        trough = peak

        for row in rows:
            equity = row["equity"]
            if equity > peak:
                peak = equity
            dd = (peak - equity) / max(peak, 0.01)
            if dd > max_dd:
                max_dd = dd
                trough = equity

        return max_dd, peak, trough

    def get_market_stats(self, slug: str = None) -> List[dict]:
        """Get per-market statistics."""
        query = """
            SELECT slug, question,
                   COUNT(*) as trades,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(pnl) as total_pnl,
                   AVG(pnl) as avg_pnl,
                   AVG(hold_seconds) as avg_hold,
                   AVG(entry_spread) as avg_spread,
                   SUM(CASE WHEN adverse_fill = 1 THEN 1 ELSE 0 END) as adverse_fills
            FROM trades
            GROUP BY slug
            ORDER BY total_pnl DESC
        """
        rows = self.conn.execute(query).fetchall()
        return [dict(r) for r in rows]

    def get_hourly_performance(self, since: float = 0) -> List[dict]:
        """Get performance grouped by hour of day."""
        query = """
            SELECT
                CAST(strftime('%H', timestamp, 'unixepoch') AS INTEGER) as hour,
                COUNT(*) as trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(pnl) as total_pnl,
                AVG(pnl) as avg_pnl
            FROM trades
        """
        params = []
        if since > 0:
            query += " WHERE timestamp > ?"
            params.append(since)
        query += " GROUP BY hour ORDER BY hour"

        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_daily_performance(self, days: int = 30) -> List[dict]:
        """Get performance grouped by day."""
        since = time.time() - (days * 86400)
        query = """
            SELECT
                DATE(timestamp, 'unixepoch') as date,
                COUNT(*) as trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(pnl) as total_pnl,
                AVG(pnl) as avg_pnl,
                MAX(pnl) as best_trade,
                MIN(pnl) as worst_trade
            FROM trades
            WHERE timestamp > ?
            GROUP BY date ORDER BY date DESC
        """
        rows = self.conn.execute(query, (since,)).fetchall()
        return [dict(r) for r in rows]

    def get_profit_factor(self, since: float = 0) -> float:
        """Calculate profit factor (gross profit / gross loss)."""
        query = "SELECT SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) as gross_profit, SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END) as gross_loss FROM trades"
        params = []
        if since > 0:
            query += " WHERE timestamp > ?"
            params.append(since)

        row = self.conn.execute(query, params).fetchone()
        gp = row["gross_profit"] or 0
        gl = abs(row["gross_loss"] or 0)
        return gp / max(gl, 0.001)

    def get_equity_curve(self, since: float = 0, limit: int = 1000) -> List[Tuple[float, float]]:
        """Get equity curve as list of (timestamp, equity) tuples."""
        query = "SELECT timestamp, equity FROM equity_snapshots"
        params = []
        if since > 0:
            query += " WHERE timestamp > ?"
            params.append(since)
        query += " ORDER BY timestamp LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(query, params).fetchall()
        return [(r["timestamp"], r["equity"]) for r in rows]

    def get_ml_accuracy(self) -> Tuple[int, int, float]:
        """Get ML prediction accuracy."""
        row = self.conn.execute(
            "SELECT COUNT(*) as total, SUM(correct) as correct FROM predictions"
        ).fetchone()
        total = row["total"]
        correct = row["correct"] or 0
        return correct, total - correct, correct / max(1, total)

    def full_report(self) -> str:
        """Generate a comprehensive analytics report."""
        lines = [
            "\n" + "=" * 60,
            "  📊 ANALYTICS REPORT",
            "=" * 60,
        ]

        # Overall stats
        total_row = self.conn.execute("SELECT COUNT(*) as n, SUM(pnl) as total_pnl FROM trades").fetchone()
        total_trades = total_row["n"]
        total_pnl = total_row["total_pnl"] or 0

        if total_trades == 0:
            lines.append("  No trades recorded yet.")
            return "\n".join(lines)

        wins, losses, wr = self.get_win_rate()
        sharpe = self.get_sharpe_ratio()
        max_dd, peak, trough = self.get_max_drawdown()
        pf = self.get_profit_factor()

        lines.extend([
            f"\n  OVERALL",
            f"  Total trades: {total_trades}",
            f"  Win rate: {wins}W/{losses}L ({wr:.1%})",
            f"  Total PnL: ${total_pnl:+.2f}",
            f"  Profit factor: {pf:.2f}",
            f"  Sharpe ratio: {sharpe if sharpe else 'N/A'}",
            f"  Max drawdown: {max_dd:.1%} (peak=${peak:.2f}, trough=${trough:.2f})",
        ])

        # Per-market breakdown
        market_stats = self.get_market_stats()
        if market_stats:
            lines.append(f"\n  TOP MARKETS:")
            for ms in market_stats[:5]:
                ms_wr = ms["wins"] / max(1, ms["trades"])
                lines.append(f"    {ms['slug'][:35]:<35} | {ms['trades']:>3}t | "
                           f"{ms_wr:.0%} WR | ${ms['total_pnl']:+.2f} | "
                           f"adverse={ms['adverse_fills']}")

        # Hourly performance
        hourly = self.get_hourly_performance()
        if hourly:
            lines.append(f"\n  HOURLY PERFORMANCE (UTC):")
            for h in hourly:
                h_wr = h["wins"] / max(1, h["trades"])
                bar = "█" * int(h["total_pnl"] / max(0.01, max(abs(h2["total_pnl"]) for h2 in hourly)) * 15) if hourly else ""
                lines.append(f"    {h['hour']:02d}:00 | {h['trades']:>3}t | {h_wr:.0%} WR | ${h['total_pnl']:>+7.2f} | {bar}")

        # Daily performance (last 7 days)
        daily = self.get_daily_performance(7)
        if daily:
            lines.append(f"\n  DAILY (last 7 days):")
            for d in daily:
                d_wr = d["wins"] / max(1, d["trades"])
                lines.append(f"    {d['date']} | {d['trades']:>3}t | {d_wr:.0%} WR | ${d['total_pnl']:>+7.2f}")

        # ML accuracy
        ml_correct, ml_wrong, ml_acc = self.get_ml_accuracy()
        if ml_correct + ml_wrong > 0:
            lines.append(f"\n  ML PREDICTIONS: {ml_correct}C/{ml_wrong}W ({ml_acc:.1%} accuracy)")

        lines.append("=" * 60)
        return "\n".join(lines)

    def close(self):
        """Close the database connection."""
        self.conn.close()


# Need math import for sharpe calculation
import math
