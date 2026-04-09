# Polymarket Scalper v4

Brain-powered, multi-strategy trading engine for Polymarket with AI supervisor. Learns from every trade to avoid repeating losses and double down on winners.

## v4 Upgrades

- **Book-Depth-Aware Paper Fills** — uses actual bid/ask levels and sizes from WebSocket feed, models queue position, partial fills, and spread-adjusted fill rates
- **Realized Capital Tracking** — separates realized P&L from committed capital. Drawdown only counts realized losses, not capital deployed in open positions
- **Fixed Short-Selling Accounting** — symmetric P&L for long and short positions. No double-counting of capital on close
- **Spread-Proportional GTD** — wider spread = longer order life. 3¢ spread → 45s, 20¢ spread → 300s. Plus 60s Polymarket security threshold for live mode
- **Both-Sides Capital Guard** — halved per-order size in market making mode. All live orders counted in exposure check
- **Aggressive Neg-Risk Quoting** — decoupled from entry checks, wider NO-side quoting on multi-outcome events
- **News Decay + Auto-Un-Skip** — alerts lose weight exponentially (120s half-life). Markets auto-un-skip after 10 minutes with no fresh alerts
- **Post-Only Orders** — guarantee maker status. Orders that would cross the spread are rejected instead of executing as taker
- **AI Supervisor** — rule-based market filtering with zero per-order latency. Supervisor pre-researches markets, bot executes at full speed
- **Self-Impact Modeling** — estimates price impact of own orders, adjusts exit targets accordingly
- **Token Inventory Tracking** — Polymarket CLOB requires owning tokens to SELL. Tracks inventory, auto-splits USDC when needed
- **Live Fill Reconciliation** — polls exchange for filled orders every 30s to catch WebSocket misses

## Features

- **Adaptive Brain** — persists learning across sessions in `brain.json`. Tracks per-market win rates, risk scores, optimal entry conditions, and time-of-day patterns
- **True Market Making** — simultaneous bid+ask orders inside the spread with inventory skew and flow-aware quoting
- **News Monitoring** — polls Reuters/AP/BBC RSS feeds, pulls orders on breaking news to avoid adverse selection
- **Order Flow Analysis** — detects volume spikes, buy/sell pressure, and momentum surges
- **Kelly Criterion** — optimal position sizing based on historical win rate and win/loss ratio
- **Tick Size Awareness** — snaps prices to valid market ticks, no rejected orders
- **Correlation Tracking** — detects when related markets haven't moved yet
- **Time-of-Day Patterns** — learns which hours/days produce the best results
- **Stop Losses** — 2¢ automatic stops on every position (long and short)
- **AI Supervisor** — pre-checks markets for risks (resolution timing, price extremes, liquidity), blocks bad markets, reduces sizes on risky ones

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

### AI Supervisor

```bash
# Terminal 1: Pre-check markets (run BEFORE starting bot, takes ~30s)
python3 supervisor.py --precheck

# Terminal 2: Start the bot with supervisor rules
python3 bot.py --paper --supervised --strategy both_sides

# Terminal 3 (optional): Background monitor (updates rules every 60s)
python3 supervisor.py --watch

# Emergency stop (halts all trading immediately)
python3 supervisor.py --emergency-stop

# Check supervisor rules
python3 supervisor.py --status
```

The supervisor does NOT gate individual orders (that would add 6-10s latency). Instead, it:
- Pre-researches all markets before trading starts
- Writes `rules.jsonl` with approved/blocked/limited markets
- Bot reads rules every 60s (~1ms) — zero per-order latency
- Background watcher detects emergencies (market closing, price extreme)
- Emergency exits queued for immediate execution on next bot tick

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

- **Circuit breaker**: Stops at 10% daily drawdown (realized P&L only)
- **Max exposure**: 50% of capital at risk
- **Stop losses**: 2¢ automatic per position
- **News protection**: Pulls orders on breaking news, force-exits positions
- **Flow protection**: Pulls orders on 3x volume spikes
- **Max hold**: 5 minutes per position (brain-adjustable)
- **Quiet hours**: Warns during low-liquidity windows (3-6 AM UTC)
- **Post-only**: Guarantees maker status, no accidental taker fills
- **AI Supervisor**: Blocks risky markets, reduces sizes, emergency exits

## Disclaimer

This software is for educational and research purposes. Trading prediction markets involves substantial risk of loss. Use at your own risk.
