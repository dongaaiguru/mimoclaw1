# Polymarket Scalper v5

Multi-strategy, brain-powered, news-aware trading engine for Polymarket.

## What's in v5

**Risk Management (all enforced, not decorative):**
- Dynamic stop losses — ATR-based, trailing, time-decaying
- Quiet hours — hard stop during 3-6 AM UTC, no trading
- Resolution filter — blocks markets <4h from resolution
- Losing streak detection — pauses after 3 consecutive losses
- Daily loss limit — stops at 8% daily drawdown
- Daily profit target — winds down at 10% daily gain
- Adverse selection fills — paper mode is harder than live

**Alpha Sources:**
- Sentiment trading — enters on bullish/bearish news within 15s head start
- News avoidance — pulls orders on breaking news, dumps positions
- Adaptive brain — learns win rates, risk scores, optimal conditions per market

**Infrastructure:**
- Live token splitting — CTF integration for SELL orders in live mode
- Bankroll compounding — bet sizes scale with account growth, shrink on losses
- Deposit awareness — add $100, sizes double immediately
- Rolling drawdown — protects accumulated gains, not just starting capital
- SQLite analytics — Sharpe ratio, hourly performance, equity curve

## Architecture

```
polymarket-scalper/
├── bot.py              # Core classes (Config, Market, Brain, Feed, etc.)
├── bot_v5.py           # Main engine — run this
├── supervisor.py       # AI market pre-checker
├── brain.json          # Persistent learning state
├── bankroll.json       # Capital tracking (created at runtime)
├── analytics.db        # Trade analytics (created at runtime)
└── modules/
    ├── token_manager.py    # Live token splitting
    ├── fill_simulator.py   # Adverse selection simulation
    ├── dynamic_stops.py    # ATR/trailing/time-decay stops
    ├── sentiment.py        # News sentiment trading
    ├── analytics.py        # SQLite analytics
    ├── bankroll.py         # Dynamic capital tracking
    └── risk_guard.py       # Risk enforcement layer
```

## Setup

```bash
cd polymarket-scalper
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your Polymarket private key and funder address
```

## Usage

```bash
# Discover markets
python3 bot_v5.py --scan

# Paper trade (adverse selection sim — harder than live)
python3 bot_v5.py --paper

# Paper trade with market making
python3 bot_v5.py --paper --strategy both_sides

# Live trading
python3 bot_v5.py --live --capital 100 --strategy one_side

# Analytics report
python3 bot_v5.py --analytics

# Brain status
python3 bot_v5.py --brain

# AI Supervisor (run in separate terminal before bot)
python3 supervisor.py --precheck
python3 bot_v5.py --paper --supervised
python3 supervisor.py --watch
python3 supervisor.py --emergency-stop
```

## Strategies

**`--strategy one_side`** (default, recommended for $100)
Place BUY orders inside the spread, SELL on fill. Lower risk, higher turnover.

**`--strategy both_sides`**
Simultaneous BID + ASK. Earns the spread regardless of direction. Halved per-order size.

## How $100 Compounds

```
Day 1:  $100 → $8/order, 5 max positions
Day 5:  $128 → $10/order (auto-scaled)
Day 10: $163 → $13/order
Deposit $100 more:
Day 10: $263 → $21/order (doubled immediately)
```

The bankroll manager tracks effective capital (starting + deposits - withdrawals + PnL) and scales all sizing dynamically.

## Risk Limits

| Control | Default |
|---|---|
| Max daily loss | 8% of capital |
| Daily profit target | 10% (then wind down) |
| Max drawdown | 10% → 8% → 5% (tightens with gains) |
| Losing streak | 3 consecutive → pause 10 min |
| Resolution | Block markets <4h from close |
| Position size | 5-15% of capital (dynamic) |
| Max concurrent | 3-10 positions (scales with capital) |

## Disclaimer

Educational and research purposes. Trading prediction markets involves substantial risk of loss.
