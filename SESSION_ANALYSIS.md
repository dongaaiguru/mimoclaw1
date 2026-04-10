# Polymarket Scalper v5 — Session Analysis

## Session #5 — 2026-04-10 16:11–16:48 UTC (35 minutes)

### Configuration
- Mode: Paper trading (real Polymarket market data + deterministic fill simulator v9)
- Strategy: `both_sides` (market making)
- Capital: $100
- Markets: 20 initial → pruned to ~12 active

### Results

| Metric | Value |
|---|---|
| Starting Capital | $100.00 |
| Final Equity | $395.24 |
| Realized PnL | +$295.24 (+295%) |
| Total Closed Trades | 41 |
| Win Rate | 39W / 2L (95.1%) |
| Profit Factor | 34.79 |
| Sharpe Ratio | 0.524 |
| Max Drawdown | 7.3% ($110.75 → $102.71) |
| Avg Hold Time | ~90s |

### Top Markets by PnL

| Market | Trades | WR | PnL | Avg/Trade |
|---|---|---|---|---|
| Fed upper bound 5.5% | 18 | 100% | +$250.31 | +$13.91 |
| Israel annex by Jun 30 | 1 | 100% | +$17.03 | +$17.03 |
| Israeli parliament dissolved | 5 | 100% | +$7.05 | +$1.41 |
| OpenAI hardware product | 6 | 100% | +$3.92 | +$0.65 |
| US forces in Gaza | 2 | 100% | +$3.74 | +$1.87 |
| Andy Beshear pres run | 1 | 100% | +$22.21 | +$22.21* |
| Stripe IPO | timeout | — | -$8.04 | — |

*Beshear was a timeout exit on a SHORT that happened to be profitable.

### Losses

1. **Stripe IPO timeout** — Bot opened SHORT 115 shares at $0.144 on the SELL side. The 75s GTD expired and the position was force-exited at $0.145. Net loss: -$8.04. Root cause: token inventory mismatch — the bot tried to SELL more tokens than it owned, opening an unintended SHORT.

2. **One small adverse fill** — Likely a partial fill on a SELL that slightly underwater.

### Critical Observations

#### 1. Extreme Winner Concentration
**85% of profits came from one market** ("Will the Fed's upper bound reach 5.5% or higher before 2027"). This market had 60-90¢ momentum swings per 30s window, which is extremely unusual. The bot captured this by buying dips around $0.06-$0.13 and selling rips around $0.22-$0.24 repeatedly.

**Risk**: This is not a repeatable edge. Remove the Fed market and the return drops to ~$45 on $100 (45%), which is still good but reflects the bot's actual market-making capability more honestly.

#### 2. Paper Mode vs Live Mode Gap
The fill simulator v9 is deterministic (only fills on real book crosses), but still more favorable than live:
- Paper gets perfect queue position — in reality, other makers compete for the same price level
- No gas costs factored in (Polygon gas is ~0.001-0.01 MATIC per tx, but adds up with 40+ trades)
- Slippage model is 0.2¢ "paper tax" — real slippage on thin books is 1-5¢
- Partial fills in paper are 30-80% — in reality, you might get 5% filled on wide-spread markets

#### 3. Token Inventory Bug
The bot occasionally tries to SELL more tokens than it holds, opening unintended SHORT positions. This happened on:
- Stripe IPO (SHORT 115 shares)
- USDT market cap (SHORT 5 shares)
- OpenAI hardware (SHORT at $0.414 on SELL side)

These SHORTs are risky because the bot doesn't have a proper SHORT exit strategy — it relies on the SELL side of its both_sides strategy, but the SHORT fills happen on the ASK side, which is the wrong direction.

#### 4. Flow Pull Sensitivity
The bot pulled orders on 6 "momentum surge" events with movements of 50-93¢. On prediction markets, 50¢+ moves in 30s ARE significant (this is a 50%+ price change), so the flow pull logic is working correctly. However, it may be too aggressive — some of these surges were noise from thin books.

#### 5. Brain Learning is Working
The brain correctly identified:
- **STAR markets**: Fed upper bound (100% WR), Israeli parliament (100% WR), OpenAI hardware (100% WR)
- **Pattern rules**: 0.06-0.10 spread → 100% WR (tight spreads fill faster)
- **Price buckets**: "low" price range (5-20¢) has highest PnL ($277) — these are the markets where the bot captures the most spread

### Recommendations for Next Session

1. **Reduce Kelly fraction to 5-10%** — 25% is too aggressive for prediction markets
2. **Fix token inventory tracking** — Don't open SHORTs; track exact token balances
3. **Cap per-market exposure** — No single market should represent >30% of total PnL
4. **Add gas cost tracking** — Even in paper mode, simulate 0.5¢ gas per trade
5. **Increase paper tax to 0.5¢** — More realistic slippage model
6. **Test one_side strategy** — Both_sides creates token inventory problems; one_side is simpler
7. **Run during peak hours (14-22 UTC)** — Brain shows all trades were in "normal" hours; peak should have better fills

### Honest Assessment

This bot is **well-built but not magic**. The v9 fill simulator, brain learning, flow analysis, and risk management are all solid engineering. The +295% return is real data but concentrated in one anomalously volatile market. The bot's actual edge — market making on wide-spread prediction markets — probably generates 20-50% returns per session with proper risk management, which is still excellent if sustainable.

**Next step**: Run live with $100 real capital, one_side strategy, per-order $5, for 2 weeks. Track actual fill rates, slippage, and gas costs. If win rate stays above 60% with positive PnL after gas, scale up.
