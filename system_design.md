# System Architecture

## Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    POLYMARKET SCORE ENGINE                    │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌─────────────┐    ┌──────────────┐    ┌──────────────┐    │
│  │  WebSocket   │    │  Signal       │    │  Execution   │    │
│  │  Listener    │───▶│  Engine       │───▶│  Engine      │    │
│  │             │    │              │    │              │    │
│  │ • OrderBook │    │ • EV         │    │ • Limit      │    │
│  │ • Trades    │    │ • KL Div     │    │ • GTC        │    │
│  │ • BestBBA   │    │ • Bayesian   │    │ • HMAC Auth  │    │
│  │ • PING/PONG │    │ • LMSR       │    │ • Retry      │    │
│  └─────────────┘    │ • Stoikov    │    └──────────────┘    │
│                     │              │                          │
│                     │ ╔══════════╗ │    ┌──────────────┐     │
│                     │ ║  SCORE   ║ │    │  Risk        │     │
│                     │ ║ > 0.50   ║ │    │  Manager     │     │
│                     │ ╚══════════╝ │    │              │     │
│                     └──────────────┘    │ • Kelly      │     │
│                                         │ • Drawdown   │     │
│                                         │ • Positions  │     │
│                                         └──────────────┘     │
└─────────────────────────────────────────────────────────────┘
```

## Components

### WebSocket Listener
- Endpoint: `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- Events: `book`, `best_bid_ask`, `last_trade_price`, `price_change`
- Heartbeat: PING every 10 seconds
- Auto-reconnect with exponential backoff

### Signal Engine
Runs the unified Score formula on every price tick:
1. Compute EV (exchange price vs market price)
2. Compute KL divergence (related market check)
3. Compute Bayesian ΔP (momentum)
4. Compute LMSR edge (price impact)
5. Compute Stoikov risk (entry safety)
6. Calculate unified Score
7. Check hard filters
8. Generate trade signal if Score > 0.50

### Execution Engine
- Auth: HMAC-SHA256 signing
- Orders: GTC limit orders only
- Retry: 3 attempts with exponential backoff
- Paper mode: Simulates fills at best bid/ask

### Risk Manager
- Position sizing: Fractional Kelly (0.25x)
- Max concurrent: 3 positions
- Circuit breaker: 15% drawdown
- Daily limit: 20 trades

---

## API Endpoints

### Market Discovery
```
GET https://gamma-api.polymarket.com/events?active=true&closed=false&limit=100
```

### Order Placement
```
POST https://clob.polymarket.com/orders
Headers: API-KEY, API-SIGNATURE, API-TIMESTAMP, API-PASSPHRASE
Body: { asset_id, side, size, price, order_type }
```

### WebSocket Market Channel
```
wss://ws-subscriptions-clob.polymarket.com/ws/market
Subscribe: { assets_ids: [...], type: "market", custom_feature_enabled: true }
```

---

## State Management

- State saved to `state.json` every 60 seconds
- Atomic writes (write to .tmp, then rename)
- On startup: load state, reconcile with exchange

---

## Error Handling

| Error | Action |
|-------|--------|
| WebSocket disconnect | Reconnect (1s → 30s backoff) |
| API 401 | Re-sign request, retry (max 3x) |
| API 429 | Backoff 2^n seconds |
| Order fill failure | Cancel, re-evaluate signal |
| Drawdown > 15% | Circuit breaker — stop all trading |
