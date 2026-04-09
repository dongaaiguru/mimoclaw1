# Polymarket Scalper v3

Brain-powered, multi-strategy trading engine for Polymarket. Learns from every trade to avoid repeating losses and double down on winners.

## Features

- **Adaptive Brain** — persists learning across sessions in `brain.json`. Tracks per-market win rates, risk scores, optimal entry conditions, and time-of-day patterns
- **True Market Making** — simultaneous bid+ask orders inside the spread with inventory skew and flow-aware quoting
- **News Monitoring** — polls Reuters/AP/BBC RSS feeds, pulls orders on breaking news to avoid adverse selection
- **Order Flow Analysis** — detects volume spikes, buy/sell pressure, and momentum surges
- **Kelly Criterion** — optimal position sizing based on historical win rate and win/loss ratio
- **GTD Orders** — auto-expiring orders tuned per market fill rate
- **Tick Size Awareness** — snaps prices to valid market ticks, no rejected orders
- **Neg Risk Detection** — capital-efficient trading on multi-outcome events
- **Correlation Tracking** — detects when related markets haven't moved yet
- **Time-of-Day Patterns** — learns which hours/days produce the best results
- **Stop Losses** — 2¢ automatic stops on every position (long and short)

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
# Discover scalping targets (brain-informed, no API key needed)
python3 bot.py --scan

# Paper trade with live prices and learning
python3 bot.py --paper

# Paper trade with market making strategy
python3 bot.py --paper --strategy both_sides

# Live trading
python3 bot.py --live

# Custom capital and strategy
python3 bot.py --live --capital 1000 --per-order 30 --strategy both_sides

# View brain status and learned rules
python3 bot.py --brain

# Show available strategies
python3 bot.py --strategies

# Reset brain (start fresh)
python3 bot.py --brain-reset
```

## Strategies

### `--strategy one_side` (default)
Place BUY orders inside the spread, SELL on fill. Simple, lower risk.

### `--strategy both_sides`
True market making. Places simultaneous BUY and SELL orders inside the spread. Earns the spread regardless of direction. Uses inventory skew and flow analysis to adjust quotes.

## How It Works

1. **Discover** — Finds best fee-free markets (spread ≥ 3¢, liq ≥ $3K, vol ≥ $2K), brain-filtered
2. **Connect** — WebSocket for real-time order book updates
3. **Quote** — Places bid+ask inside the spread (market making) or bid-only (one-side)
4. **Fill** — Brain records entry conditions, adjusts sizing via Kelly Criterion
5. **Exit** — Brain-informed exit timing, stop losses, flow-aware repricing
6. **Learn** — Every trade updates market reputation, pattern buckets, time patterns, avoid/star lists

## Risk Controls

- **Circuit breaker**: Stops at 10% daily drawdown
- **Max exposure**: 50% of capital at risk
- **Stop losses**: 2¢ automatic per position
- **News protection**: Pulls orders on breaking news
- **Flow protection**: Pulls orders on 3x volume spikes
- **Max hold**: 5 minutes per position (brain-adjustable)
- **Quiet hours**: Warns during low-liquidity windows (3-6 AM UTC)

## Disclaimer

This software is for educational and research purposes. Trading prediction markets involves substantial risk of loss. Use at your own risk.
