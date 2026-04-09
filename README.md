# PolyEdge v2 — Polymarket Trading Bot

**Research-optimized multi-strategy bot targeting 2% daily returns on $100 capital.**

Built from live market analysis (3,440 markets, April 2026), academic research on $40M in proven arbitrage, and proven market maker strategies.

## What Makes This Different

The original code in this repo looked for BTC lag arbitrage — an edge that was killed by Polymarket in March 2026 with dynamic taker fees and removal of the 15-minute price delay.

**PolyEdge v2 uses the edges that actually exist today:**

| Strategy | Edge | Source |
|---|---|---|
| Fee-Free Spread Capture | Zero fees on geopolitics markets | Live scan: 42 markets with ≥4¢ spreads |
| Liquidity Rewards | $5M/month pool (April 2026) | Polymarket official rewards program |
| Dependency Arb | $29M proven (academic paper) | arxiv 2508.03474 — 86M transactions analyzed |
| Maker Rebates | 20-25% of taker fees returned | Polymarket official rebate program |
| News Momentum | 30s-5min information advantage | AI bots processing news faster than humans |

## Live Scan Results (April 9, 2026)

```
STRATEGY 1: FEE-FREE SPREAD CAPTURE
  BID  $13 @ 0.270 | 5¢ edge  | Perplexity acquired before 2027
  BID  $13 @ 0.815 | 16¢ edge | USDT $200B before 2027
  BID  $13 @ 0.051 | 27¢ edge | Fed rate ≥ 5.5% before 2027

STRATEGY 3: DEPENDENCY ARB
  BUY  $7 @ 0.100 | 26.5% edge | Deport <200K
  SELL $7 @ 0.365 | 26.5% edge | Deport 300-400K

ESTIMATED DAILY INCOME
  Spread capture:  $3.46 (30% fill rate)
  Rewards/rebates: $0.60
  Dependency arb:  $1.23 (20% fill rate)
  TOTAL:           $5.29/day
  ✅ TARGET $2.00/day ACHIEVABLE
```

## Quick Start

```bash
# Install
pip install -r requirements.txt

# Scan mode (discovery, no trading)
python -m polyedge --scan

# Paper trading (simulated fills)
python -m polyedge --paper

# Live trading
cp .env.example .env
# Edit .env with your API credentials
python -m polyedge --live --capital 100
```

## Architecture

```
polyedge/
├── __init__.py              # Package init
├── __main__.py              # Entry point (python -m polyedge)
├── bot.py                   # Main bot orchestrator
├── core/
│   ├── __init__.py          # Config, Market, Signal, Position data types
│   ├── api.py               # Polymarket Gamma + CLOB API client
│   └── risk.py              # Risk manager (circuit breaker, Kelly sizing)
└── strategies/
    ├── __init__.py
    ├── fee_free_spread.py   # Strategy 1: Zero-fee spread capture
    ├── liquidity_rewards.py # Strategy 2: Sports liquidity rewards
    ├── dependency_arb.py    # Strategy 3: Cross-market logical arb
    └── momentum.py          # Strategy 4: News momentum scalping
```

## Strategy Details

### 1. Fee-Free Spread Capture (40% allocation)
Place GTC limit orders inside the spread on geopolitics markets (zero taker fees). 
The entire spread is profit. Based on @defiance_cr's proven approach ($700-800/day on $10K).

### 2. Liquidity Rewards (25% allocation)  
Polymarket pays $5M/month. EPL soccer games: $10,000/game. The reward formula 
quadratically favors tight, two-sided orders (3x multiplier vs one-sided).

### 3. Dependency Arb (20% allocation)
Exploit pricing inconsistencies between logically related markets. Academic paper 
proves $29M was extracted from 86M transactions using this approach.

### 4. News Momentum (15% allocation)
Detect high-volume, wide-spread markets where informed flow may be occurring.

## Risk Controls

- **10% circuit breaker** — Bot stops if $10 lost on $100
- **$5 minimum order** — Polymarket's minimum
- **5 min max order age** — Stale orders auto-cancel
- **$20 max position** — 20% of capital per market
- **Max 8 concurrent** — Diversification limit

## Research Sources

- [Academic paper: $40M in Polymarket arbitrage](https://arxiv.org/abs/2508.03474)
- [@defiance_cr's open-source MM bot](https://github.com/warproxxx/poly-maker)
- [Polymarket liquidity rewards docs](https://docs.polymarket.com/market-makers/liquidity-rewards)
- [Polymarket maker rebates](https://docs.polymarket.com/market-makers/maker-rebates)
- [4 profitable bot strategies (2026)](https://medium.com/illumination/beyond-simple-arbitrage-4-polymarket-strategies-bots-actually-profit-from-in-2026-ddacc92c5b4f)

## License

MIT

---

**Disclaimer:** Trading prediction markets involves substantial risk of loss. 
Past performance does not guarantee future results. Use at your own risk.
