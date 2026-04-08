# Execution Plan

## Startup Sequence

```
1. Load config from .env
2. Validate API credentials (if live mode)
3. Check wallet USDC balance
4. Scan active markets via Gamma API
5. Filter markets (liquidity > $8K, spread < 3%, volume > $5K)
6. Initialize Bayesian estimators per market
7. Connect WebSocket → subscribe to market token IDs
8. Start signal computation loop
```

## Real-Time Loop (Per Tick)

```
FOR EACH price_update:

  1. UPDATE Bayesian estimator with exchange + market signals
  
  2. CHECK EXIT CONDITIONS for open positions
     - Profit ≥ 4% → SELL
     - Loss ≥ 8% → SELL
     - Held > 300 ticks → SELL
  
  3. COMPUTE SCORE
     - EV = f(exchange_price, market_ask)
     - KL = f(market_price, related_market_price)
     - ΔP = P_now − P_lookback
     - LMSR = f(price_impact)
     - Risk = f(spread, inventory)
     - Score = 0.35×EV + 0.20×KL + 0.20×ΔP + 0.15×LMSR − 0.10×Risk
  
  4. IF Score > 0.50 AND filters pass:
     → Kelly sizing → Place limit order
  
  5. SAVE state (every 60s)
```

## Order Placement

```
1. Pre-flight: balance check, market active, order book exists
2. Construct: { asset_id, side: "BUY", size, price, order_type: "GTC" }
3. Sign: HMAC-SHA256(timestamp + method + path + body)
4. Submit: POST /orders
5. Handle: 201=success, 401=retry, 429=backoff
```

## Exit Logic

```
pnl_pct = (current_price − entry_price) / entry_price

IF pnl_pct ≥ +4%: EXIT (profit_target)
IF pnl_pct ≤ −8%: EXIT (stop_loss)
IF ticks_held > 300: EXIT (time_decay)
```

## Paper vs Live

| Mode | Auth Required | Order Placement | Risk |
|------|--------------|-----------------|------|
| Paper | No | Simulated | None |
| Live | Yes | Real CLOB API | Real money |

### Transition Checklist
1. ✅ Paper backtest shows positive returns
2. ✅ API credentials validated
3. ✅ Wallet funded with USDC
4. ✅ Test with $1-5 first
5. ✅ Confirm fills match expected prices
6. ✅ Scale up to full position sizes
