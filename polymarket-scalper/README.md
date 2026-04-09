# Polymarket Scalper

High-velocity spread capture bot for Polymarket. Places aggressive limit orders inside the spread on fee-free markets, cancels & reprices every 30 seconds, and auto-exits positions held > 5 minutes.

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
# Discover scalping targets (no API key needed)
python3 bot.py --scan

# Paper trade with live prices (simulated fills)
python3 bot.py --paper

# Live trading
python3 bot.py --live

# Custom capital
python3 bot.py --live --capital 100 --per-order 10
```

## How it works

1. **Discover**: Finds 10 best fee-free markets (spread ≥ 3¢, liq ≥ $2K)
2. **Connect**: WebSocket for real-time order book updates
3. **Place**: Limit BUY orders 1-2¢ inside the spread
4. **Fill**: When someone sells into your bid, you own shares
5. **Exit**: Immediately place SELL order at mid + 0.5¢
6. **Reprice**: Cancel all, re-place at current best every 30s
7. **Timeout**: Force-exit any position held > 5 minutes

## Risk controls

- **Circuit breaker**: Stops at 10% daily drawdown
- **Max exposure**: 50% of capital at risk
- **Max positions**: 5 concurrent
- **Max hold**: 5 minutes per position
- **Reserve**: Always 50% cash free for rebalancing

## Disclaimer

This software is for educational and research purposes. Trading prediction markets involves substantial risk of loss. Use at your own risk.
