# Strategy — Unified Mathematical Decision Engine

## The Formula

```
Score = 0.35 × EV + 0.20 × KL + 0.20 × ΔP_Bayes + 0.15 × LMSR_edge − 0.10 × Risk_Stoikov
```

**Decision Rule:** Trade if `Score > 0.50` AND all hard filters pass.

---

## Component Definitions

### 1. Expected Value (EV) — Weight 0.35

The primary driver of profitability. Measures whether we have mathematical edge.

```
EV_raw = true_prob − entry_price − fees
EV_normalized = clamp(EV_raw / max_edge, 0, 1)
```

- `true_prob` = Bayesian posterior (informed by exchange data)
- `entry_price` = best ask
- `fees` = 0.002 (gas only for fee-free markets)
- `max_edge` = 0.15 (15% theoretical maximum)

**Hard filter:** `EV_raw > 0.015`

### 2. KL Divergence — Weight 0.20

Detects structural mispricing between related markets.

```
KL(P || Q) = p × ln(p/q) + (1−p) × ln((1−p)/(1−q))
```

**Relationship types:**
- `subset`: Market A outcome implies Market B outcome (e.g., BTC $150k by June → BTC $150k by December)
- `superset`: Market B outcome implies Market A outcome

**Constraint:** If A is a subset of B, then `P(B) ≥ P(A)`.

### 3. Bayesian DeltaP — Weight 0.20

Captures momentum — is probability moving in our favor?

```
ΔP = P_current − P_lookback
ΔP_normalized = clamp(max(ΔP, 0) / delta_max, 0, 1)
```

Only positive momentum contributes to the score.

**Critical:** The Bayesian estimator must use an **external information source** (exchange prices) as its prior. Without this, it tracks the market price and produces no edge.

### 4. LMSR Edge — Weight 0.15

Predicts post-trade price movement.

```
P_yes = e^(q_yes/b) / (e^(q_yes/b) + e^(q_no/b))
LMSR_edge = max(price_after_buy − price_before, 0) / edge_max
```

### 5. Stoikov Risk — Weight −0.10 (PENALTY)

Risk should REDUCE the score, not add to it.

```
Reservation Price = mid_price − position × γ × σ²
Risk = 0.7 × inventory_risk + 0.3 × spread_risk
```

---

## Hard Filters

| Filter | Threshold | Why |
|--------|-----------|-----|
| EV > 0.015 | 1.5% minimum edge | Below this, fees destroy profit |
| Spread < 0.03 | 3% max spread | Wide spreads = execution risk |
| Liquidity > $8K | Minimum depth | Can't exit illiquid positions |
| Daily trades < 20 | Rate limit | Prevent overtrading |
| Drawdown < 15% | Circuit breaker | Prevent ruin |

---

## Information Advantage

The system's edge comes from having **better information than the market**:

1. **Exchange prices**: Real BTC/ETH spot prices lead Polymarket prices by 2-5 ticks
2. **Cross-market dependencies**: Logical relationships between markets are often mispriced
3. **Volume analysis**: Large trades signal informed money

Without an information advantage, the Bayesian estimator converges to the market price and generates no edge.

---

## Capital Strategy ($10–$50)

| Capital | Max Position | Max Concurrent | Reserve |
|---------|-------------|----------------|---------|
| $10 | $2.50 | 2 | $5.00 |
| $25 | $6.25 | 2 | $12.50 |
| $50 | $12.50 | 3 | $25.00 |

**Growth projection (based on backtest):**
```
$50 → $50.50 (+1%) — ~20 trades over ~2 weeks at 0.50 threshold
```

---

## Why 0.50 Threshold?

Backtesting across 30 random seeds:

| Threshold | Profitable Seeds | Avg Return |
|-----------|-----------------|------------|
| 0.40 | 7% | −6.8% |
| 0.45 | 10% | −5.9% |
| **0.50** | **37%** | **+1.7%** |
| 0.55 | 0% | 0.0% |

At 0.50, we get the best balance of selectivity and sample size.
