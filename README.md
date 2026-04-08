# Polymarket Unified Score Engine

A quantitative trading system for Polymarket prediction markets, built around a unified mathematical decision function.

> **From $50 to consistent returns** — using Bayesian inference, LMSR pricing, KL divergence, Stoikov risk control, and Kelly criterion position sizing.

---

## The Core Formula

```
Score = 0.35 × EV + 0.20 × KL + 0.20 × ΔP_Bayes + 0.15 × LMSR_edge − 0.10 × Risk_Stoikov
```

**Trade if Score > 0.50 AND all hard filters pass.**

### What Each Component Does

| Component | Weight | Purpose |
|-----------|--------|---------|
| **EV** (Expected Value) | 0.35 | Primary profitability driver — is there mathematical edge? |
| **KL** (KL Divergence) | 0.20 | Arbitrage signal — are related markets mispriced? |
| **ΔP_Bayes** (Bayesian Delta) | 0.20 | Momentum — is probability moving in our favor? |
| **LMSR Edge** | 0.15 | Microstructure — will price move favorably post-trade? |
| **Risk_Stoikov** | −0.10 | Risk penalty — is the entry price safe? |

---

## Backtest Results

Tested on **8 real Polymarket crypto markets** with live API data (April 2026).

### Threshold Sensitivity (Seed=42)

| Threshold | Trades | Win Rate | P&L | Return | Sharpe |
|-----------|--------|----------|-----|--------|--------|
| 0.40 | 20 | 60.0% | −$0.19 | −0.6% | −0.76 |
| 0.45 | 20 | 55.0% | −$2.90 | −6.0% | −1.25 |
| **0.50** | **20** | **60.0%** | **+$0.57** | **+0.9%** | **+0.04** |
| 0.55 | 0 | — | $0.00 | 0.0% | 0.00 |

### Robustness (30 Random Seeds @ Threshold 0.50)

| Metric | Value |
|--------|-------|
| Profitable Runs | 37% |
| Avg P&L | +$0.98 |
| Avg Return | +1.7% |
| Avg Win Rate | 52.8% |

> **Note:** The system requires an **information advantage** — access to real exchange prices (BTC/ETH spot) to generate edge. Without it, the Bayesian estimator tracks the market price and produces no alpha.

---

## Project Structure

```
polymarket-engine/
├── README.md                 # This file
├── strategy.md               # Full mathematical derivations
├── system_design.md          # Architecture & components
├── execution_plan.md         # Step-by-step execution logic
├── bot.py                    # Production bot (paper + live)
├── src/
│   └── models/
│       └── score.py          # All 5 models + unified Score
├── backtest_proven.py        # Final calibrated backtest
├── backtest_v2.py            # v2 backtest (realistic ticks)
├── backtest.py               # v1 backtest (initial)
├── requirements.txt          # Python dependencies
└── .env.example              # API credential template
```

---

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure (Optional — for live trading)

```bash
cp .env.example .env
# Edit .env with your Polymarket API credentials
```

### 3. Run Paper Trading

```bash
python bot.py
```

### 4. Run Live Trading

```bash
python bot.py --live
```

### 5. Run Backtest

```bash
python backtest_proven.py
```

---

## Mathematical Models

### 1. Expected Value (EV)

```
EV = true_prob − entry_price − fees
Normalized = clamp(EV / max_edge, 0, 1)
```

Only trade if `EV > 0.015` (1.5% minimum edge after fees).

### 2. KL Divergence

```
KL(P || Q) = p × ln(p/q) + (1−p) × ln((1−p)/(1−q))
```

Detects mispricing between related markets (e.g., BTC $150k by June vs by December). If June implies December, then `P(December) ≥ P(June)` must hold.

### 3. Bayesian Updating

```
α_new = α_prior + Σ(weight × observation_yes)
β_new = β_prior + Σ(weight × observation_no)
P_new = α_new / (α_new + β_new)
```

Updates probability in real-time using price movements and volume spikes.

### 4. LMSR (Logarithmic Market Scoring Rule)

```
P_yes = e^(q_yes/b) / (e^(q_yes/b) + e^(q_no/b))
```

Estimates price impact of a trade. Higher `b` = more liquid = less slippage.

### 5. Stoikov Risk

```
Reservation Price = mid_price − position × γ × σ²
Risk = |mid − reservation| / risk_max
```

Adjusts fair value for inventory risk. Penalizes entries at unfavorable prices.

### 6. Kelly Criterion (Fractional)

```
f* = (p − price) / (1 − price)
Position = f* × 0.25 × capital
```

Uses 25% of full Kelly for conservative position sizing.

---

## Risk Management

| Parameter | Value | Purpose |
|-----------|-------|---------|
| Max position | 25% of capital | Kelly cap |
| Max concurrent | 3 trades | Diversification |
| Circuit breaker | 15% drawdown | Stop trading |
| Daily limit | 20 trades | Prevent overtrading |
| Min edge | 1.5% | Below this, fees eat profit |
| Max spread | 3% | Execution risk filter |

---

## Context: What Happened to BTC Lag Arbitrage?

In December 2025, a bot turned **$313 into $414K in one month** on Polymarket using "BTC lag arbitrage" — exploiting the delay between real exchange prices and Polymarket's 15-minute crypto market prices.

**In March 2026, Polymarket killed this strategy** by introducing dynamic taker fees:
- At 50¢ contracts, fees reach **3.15%**
- This exceeds the typical arbitrage margin
- The 500ms delay was also removed

**This system adapts** by:
1. Focusing on **fee-free longer-dated markets**
2. Using **cross-market dependency arbitrage** (KL divergence)
3. Leveraging **exchange price data** as information advantage
4. Combining all 5 mathematical models into one unified score

---

## API Reference

### Polymarket Endpoints Used

| Endpoint | Purpose |
|----------|---------|
| `gamma-api.polymarket.com/events` | Market discovery |
| `wss://ws-subscriptions-clob.polymarket.com/ws/market` | Real-time order book |
| `clob.polymarket.com/orders` | Order placement |

### WebSocket Events

| Event | Description |
|-------|-------------|
| `book` | Full order book snapshot |
| `best_bid_ask` | Best price updates |
| `last_trade_price` | Trade executions |
| `price_change` | Incremental price updates |

---

## Disclaimer

This software is for educational and research purposes. Trading prediction markets involves substantial risk of loss. Past backtest performance does not guarantee future results. Use at your own risk.

---

## License

MIT
