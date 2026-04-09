# Polymarket Scalper v5 — The Profit Machine

Multi-strategy, brain-powered, news-aware trading engine for Polymarket with real alpha sources.

## v4 → v5 Upgrades

### Tier 1 — Fixed the Broken Stuff
- **Live Token Splitting** — CTF contract integration via `TokenManager`. Auto-splits USDC → YES+NO tokens for SELL orders. Falls back to market buy if split unavailable.
- **Adverse Selection Fill Simulator** — paper mode is now HARDER than live. Fills come faster when price moves against you (realistic informed trader simulation). Post-only rejections modeled. Partial fills with realistic size distribution.
- **Gas Cost Tracking** — `GasTracker` estimates Polygon gas for each operation (split/merge/erc20 approve). Checks if arb opportunities are profitable after gas. Tracks MATIC/USD price from CoinGecko.
- **Dynamic Stop Losses** — ATR-based stops (volatility-adjusted), spread-based stops (wider spreads = wider stops), time-decaying stops (tighten as hold time increases), trailing stops (move up with profit, never down). Flow-based tightening when order flow is against you.

### Tier 2 — Real Edges
- **Sentiment Trading** — `SentimentTrader` monitors Reuters/AP/BBC RSS, classifies bullish/bearish/neutral sentiment, matches to markets via category keywords + word overlap. Enters directional trades on strong signals within 15-second head start window. Decaying signal strength (news gets priced in).
- **Cross-Market Arbitrage** — `ArbitrageEngine` detects: YES/NO price sum ≠ 1.0 (guaranteed profit), neg-risk multi-outcome mispricing (buy all outcomes < 95¢), cross-event correlation anomalies.
- **Event-Level Hedging** — `HedgingEngine` tracks exposure per event, detects concentration risk (>25% in one event), suggests hedges (buying other outcomes), calculates optimal hedge ratios.
- **ML Predictor** — `MLPredictor` statistical ensemble of 7 signals: momentum (multi-timeframe ROC), mean reversion (deviation from rolling mean), volume-price divergence (volume spike + flat price = pending move), spread compression (tightening spread = consensus forming), flow imbalance (buy/sell pressure), time-of-day patterns, volatility regime. Self-calibrating weights based on historical accuracy.

### Tier 3 — Infrastructure
- **SQLite Analytics** — `AnalyticsDB` tracks every trade, equity snapshots, ML predictions, arb opportunities. Reports: Sharpe ratio, profit factor, max drawdown, per-market breakdown, hourly/daily performance, equity curve. All queryable.
- **Multi-Account Ready** — architecture supports sub-wallets (add `TokenManager` per account).

## Architecture

```
polymarket-scalper/
├── bot.py              # Original v4 engine (unchanged)
├── bot_v5.py           # v5 engine (integrates all modules)
├── supervisor.py       # AI supervisor (market pre-check)
├── brain.json          # Persistent learning state
├── analytics.db        # SQLite analytics (created at runtime)
├── requirements.txt
├── .env.example
└── modules/
    ├── __init__.py
    ├── token_manager.py    # Live token splitting
    ├── fill_simulator.py   # Adverse selection sim
    ├── gas_tracker.py      # Polygon gas costs
    ├── dynamic_stops.py    # ATR/trailing/time-decay stops
    ├── sentiment.py        # News sentiment trading
    ├── arbitrage.py        # Cross-market arb detection
    ├── ml_predictor.py     # Statistical prediction
    ├── analytics.py        # SQLite analytics
    └── hedging.py          # Event-level hedging
```

## Setup

```bash
cd polymarket-scalper
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your Polymarket private key and funder address
```

### Getting your credentials

1. Go to [polymarket.com](https://polymarket.com) → Settings
2. Export your **Private Key**
3. Copy your **Wallet Address** (the proxy/funder address shown in profile)
4. Paste both into `.env`

## Usage

```bash
# v5 (recommended)
python3 bot_v5.py --scan                    # Discover targets
python3 bot_v5.py --paper                   # Paper trade with adverse selection sim
python3 bot_v5.py --paper --strategy both_sides  # Market making mode
python3 bot_v5.py --live                    # Live trading
python3 bot_v5.py --live --capital 1000 --strategy both_sides
python3 bot_v5.py --analytics               # Show analytics dashboard

# v4 (original, still works)
python3 bot.py --scan
python3 bot.py --paper
python3 bot.py --brain

# AI Supervisor
python3 supervisor.py --precheck            # Pre-check markets (run first)
python3 bot_v5.py --paper --supervised      # Start with supervisor rules
python3 supervisor.py --watch               # Background monitor
python3 supervisor.py --emergency-stop      # Halt all trading
```

## Strategies

### `--strategy one_side` (default)
Place BUY orders inside the spread, SELL on fill. Simple, lower risk.

### `--strategy both_sides`
True market making. Simultaneous BID + ASK orders inside the spread. Earns the spread regardless of direction. Halved per-order size for safety. Token manager auto-splits USDC for SELL orders in live mode.

## How It Works

1. **Discover** — Finds best fee-free markets (spread ≥ 3¢, liq ≥ $3K, vol ≥ $2K), brain-filtered
2. **Connect** — WebSocket for real-time order book updates
3. **Analyze** — ML predictor + sentiment + arbitrage scanner run continuously
4. **Quote** — Places bid+ask inside the spread (market making) or bid-only (one-side)
5. **Fill** — Adverse selection-aware fills (paper) or exchange reconciliation (live)
6. **Exit** — Dynamic stops (ATR + trailing + time-decay), brain-informed timing, flow-aware repricing
7. **Learn** — Every trade updates brain, analytics DB, ML weights, sentiment outcomes
8. **Hedge** — Event-level risk management, concentration alerts, hedge suggestions

## Risk Controls

- **Circuit breaker**: Stops at 10% daily drawdown (realized P&L only)
- **Max exposure**: 50% of capital at risk
- **Dynamic stops**: ATR/spread-based, time-decaying, trailing
- **News protection**: Pulls orders on breaking news, force-exits positions
- **Sentiment exits**: Bearish news → immediate dump
- **Flow protection**: Pulls orders on 3x volume spikes
- **Max hold**: 5 minutes per position (brain-adjustable)
- **Quiet hours**: Warns during low-liquidity windows (3-6 AM UTC)
- **Post-only**: Guarantees maker status, no accidental taker fills
- **AI Supervisor**: Blocks risky markets, reduces sizes, emergency exits
- **Event hedging**: Concentration risk alerts, hedge suggestions

## Analytics

```bash
python3 bot_v5.py --analytics
```

Shows:
- Overall win rate, PnL, profit factor
- Sharpe ratio, max drawdown
- Per-market breakdown
- Hourly performance (best/worst hours)
- Daily performance (last 7 days)
- ML prediction accuracy
- Arbitrage execution stats
- Gas cost tracking
- Fill simulator stats (adverse selection rate)

## Disclaimer

This software is for educational and research purposes. Trading prediction markets involves substantial risk of loss. Use at your own risk.
