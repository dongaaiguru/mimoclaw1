# MIMOCLAW — Polymarket Trading Bot

Automated trading bot for Polymarket prediction markets.

## Strategy

Two engines, real data, no simulations:

1. **Dependency Arbitrage** — Exploit logical mispricing between related markets
   - Time-based: "X by June" implies "X by December" → P(Dec) ≥ P(June)
   - Threshold: ">$1B" implies ">$500M" → P(>$500M) ≥ P(>$1B)
   - Mutual exclusion: Two candidates can't both win same race → P(A)+P(B) ≤ 1

2. **Market Making** — Capture spread on fee-free markets
   - Polymarket geopolitical markets have ZERO taker fees
   - Place limit orders on both sides, pocket the spread

## Quick Start

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your Polymarket API credentials (optional for scan mode)

# Discover markets and dependencies
python bot.py --scan

# Paper trade with live prices
python bot.py --paper

# Live trading
python bot.py --live --capital 100
```

## Verified Live Data (April 9, 2026)

- 1,140 fee-free markets discovered
- 9,638 dependencies detected
- Real-time WebSocket order book data
- Active violations found and logged

## Risk Management

- 12% drawdown circuit breaker
- 20% max position size
- 4 max concurrent positions
- GTC limit orders only (never market orders)

## Disclaimer

This software is for educational and research purposes. Trading prediction markets involves substantial risk of loss.
