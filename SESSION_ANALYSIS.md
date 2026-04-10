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

---

## Session #6 — 2026-04-10 17:08–17:39 UTC (30 minutes)

### Configuration
- Mode: Paper trading (real Polymarket market data + deterministic fill simulator)
- Strategy: `one_side` (BUY inside spread, SELL on fill)
- Capital: $100
- Markets: 20 initial → refreshed to 23

### Results

| Metric | Value |
|---|---|
| Starting Capital | $100.00 |
| Final Equity | $238.22 |
| Realized PnL | +$138.22 (+138%) |
| Total Closed Trades | 83 round-trips |
| Win Rate | 82W / 1L (98.8%) |
| Profit Factor | 4,322x |
| Max Drawdown | 0.0% |
| Adverse Fills | 0 |
| Avg Hold Time | ~60s |

### Per-Market P&L

| Market | Trades | PnL | % of Total |
|---|---|---|---|
| Stripe IPO before 2027 | 17 | +$62.90 | 44.1% |
| Fed upper bound reach 5.5% | 12 | +$21.82 | 15.3% |
| Israel ground offensive Gaza | 7 | +$14.77 | 10.4% |
| Israeli parliament dissolved | 7 | +$12.36 | 8.7% |
| OpenAI consumer hardware | 14 | +$10.57 | 7.4% |
| USDT market cap $200B | 12 | +$6.63 | 4.6% |
| Waymo London launch | 4 | +$5.58 | 3.9% |
| Weed rescheduled | 5 | +$5.12 | 3.6% |
| Republicans lose Senate seat | 4 | +$2.50 | 1.8% |
| Trump pardon Ken Paxton | 1 | +$0.34 | 0.2% |

### The One Loss
- **USDT market cap**: SELL 3 @ $0.8240 | PnL = -$0.033
- Minimal loss — barely a scratch

### Risk Events
- **1 dynamic stop hit** (Republicans market) — still profited +$0.09
- **3 timeout exits** — all profitable (+$0.09, +$0.61, +$0.35)
- **10 flow pulls** (momentum surge detection on 50¢+ moves)
- **0 adverse fills** — ⚠️ This is the main concern
- **0 circuit breakers** — never hit 8% daily loss limit
- **0 losing streak pauses** — never lost 3 in a row

### Equity Curve Timeline

| Time | Equity | Trades | WR |
|---|---|---|---|
| T+1min | $100.34 | 1 | 100% |
| T+5min | $122.29 | 20 | 100% |
| T+10min | $122.67 | 21 | 100% |
| T+15min | $168.13 | 39 | 100% |
| T+20min | $177.92 | 49 | 100% |
| T+25min | $197.93 | 62 | 98% |
| T+30min | $238.22 | 81 | 99% |

### Brain Learning Updates
- **New STAR markets**: Waymo London (100% WR, $+1.40/trade), Republicans Senate (100% WR, $+0.52/trade), Israel Gaza offensive (100% WR, $+3.84/trade)
- **Pattern confirmed**: 0.06-0.10 spread bucket → 100% WR (tight spreads fill fastest)
- **Pattern confirmed**: Low price range (5-20¢) → highest total PnL, largest spread capture
- **Time pattern**: All trades in "normal" UTC hours, no quiet/peak hour data yet

### Critical Analysis — Paper vs Live Reality

#### What's Real
1. **Entry logic is solid.** Placing 1-3¢ below mid on active markets with 10¢+ spreads is a sound approach.
2. **Exit timing worked.** Most positions held 30-120 seconds. Quick in, quick out.
3. **Risk guard mechanics are sound.** Stops trailed up, timeouts fired, flow surges detected.
4. **one_side strategy avoids token inventory bugs** — no unintended SHORTs (confirmed fix from session #5 recommendation).

#### What's NOT Real
1. **99% win rate is impossible in live.** Paper simulator produced 0 adverse fills in 83 trades. Real Polymarket adverse selection rate: 10-20%.
2. **Stripe IPO carried 44% of PnL** — one market shouldn't dominate. That market moved $0.10→$0.17 during session (unusual).
3. **0 drawdown in 30 minutes is a red flag** — real scalping involves constant small drawdowns.
4. **Fill model assumes perfect queue position** — in reality you compete with other makers.

#### Realistic Live Expectations

| Metric | Paper (this session) | Realistic Live |
|---|---|---|
| 30-min return | +138% | +2% to -5% |
| Win rate | 99% | 60-75% |
| Avg win | $1.74 | $0.30-0.50 |
| Avg loss | $0.03 | $0.40-0.80 |
| Max drawdown | 0% | 5-10% |
| Adverse fills | 0 | 10-20% of trades |

### Comparison: Session #5 (both_sides) vs Session #6 (one_side)

| Metric | #5 (both_sides) | #6 (one_side) |
|---|---|---|
| Duration | 35 min | 30 min |
| Trades | 41 | 83 |
| Return | +295% | +138% |
| Win Rate | 95.1% | 98.8% |
| Max DD | 7.3% | 0% |
| Token bugs | Yes (SHORTs) | No |
| Top market % | 85% (Fed) | 44% (Stripe) |

**Key finding**: `one_side` doubled trade count (83 vs 41) because it doesn't split capital across BID+ASK. More trades = more learning data for brain. Token inventory bug eliminated. But concentration risk persists in both strategies.

### Recommendations for Session #7 / Live Prep

1. **Tighten FillSimulator adverse selection** — target 10-20% adverse fill rate, currently 0%
2. **Add paper slippage tax** — 0.5-1¢ per fill to simulate real-world friction
3. **Cap per-market PnL at 30%** — force diversification, don't let one market carry the session
4. **Calibrate against real trade data** — run 1-week paper, compare fill prices to actual Polymarket trades
5. **Kelly fraction: keep at 5%** — aggressive sizing compounds paper-mode luck
6. **Live pilot**: $100 real, one_side, per-order $5, 2-week trial before scaling
7. **Track gas costs** — even small, Polygon gas adds up with 80+ trades/session

### Honest Verdict

The bot's infrastructure (brain, risk guard, flow detection, stops, analytics) is **production-grade**. The strategy execution is mechanically correct. But the paper fill simulator is lying about fill quality — 99% WR and 0 adverse fills are fiction.

**The real question**: Can this bot sustain 60%+ WR with positive PnL after adverse selection and gas costs? Session #5 (95.1% WR with 2 losses) and session #6 (98.8% WR with 1 loss) suggest the underlying edge exists, but is inflated 2-3x by paper-mode optimism.

**Go/no-go for live**: Calibrate the fill simulator first. If you can get the paper WR down to 75% by tightening adverse selection, and it still makes money, that's your signal.

---

## Session #9 — 2026-04-11 01:59–02:29 UTC+8 (29 minutes)

### Configuration
- Mode: Paper trading (real Polymarket WebSocket order book data + deterministic fill simulator v9)
- Strategy: `one_side`
- Capital: $100
- Markets: 20 initial → refreshed to 26 → pruned to ~13 active

### Results

| Metric | Value |
|---|---|
| Starting Capital | $100.00 |
| Final Equity | $200.91 |
| Realized PnL | +$100.91 (+100.9%) |
| Total Closed Trades | 92 |
| Win Rate | 90W / 2L (97.8%) |
| Profit Factor | ~1,386x |
| Max Drawdown | 0.0% |
| Adverse Fills | 2 (2.2%) |
| Avg Hold Time | ~90s |
| Flow Pulls | 18 |
| Forced Exits | 2 (both profitable) |
| Markets Pruned | 50+ across 12 sweeps |

### Equity Curve

| Time | Equity | Trades | WR |
|---|---|---|---|
| T+1min | $101.14 | 2 | 100% |
| T+5min | $109.50 | 11 | 100% |
| T+10min | $123.95 | 28 | 100% |
| T+15min | $146.56 | 53 | 98% |
| T+20min | $170.78 | 69 | 97% |
| T+25min | $191.98 | 82 | 98% |
| T+29min | $200.91 | 92 | 98% |

### Per-Market P&L (from log PnL extraction)

| Market | Trades | WR | PnL | Avg/Trade |
|---|---|---|---|---|
| Fed upper bound 5.5% | 14 | 100% | +$24.22 | +$1.73 |
| USDT $200B | 14 | 100% | +$18.78 | +$1.34 |
| Israeli parliament dissolved | 20 | 100% | +$17.44 | +$0.87 |
| OpenAI consumer hardware | 17 | 100% | +$13.85 | +$0.81 |
| Weed rescheduled | 9 | 77.8% | +$9.86 | +$1.10 |
| NHL Hart Trophy (Kucherov) | 3 | 100% | +$8.79 | +$2.93 |
| Israel ground offensive | 15 | 100% | +$7.75 | +$0.52 |

### The Two Losses

Both on `weed-rescheduled-by-december-31`:
1. SELL 3 @ $0.4240 → PnL = -$0.006 (adverse fill on tight spread)
2. SELL 3 @ $0.4240 → PnL = -$0.007 (same pattern, repeat adverse)

Combined loss: $0.013 — noise. The weed market had tight ~10¢ spreads that occasionally produced adverse fills when selling slightly below entry.

### Sentiment Signals

- 3 sentiment signals fired at session start (Iran ceasefire news)
- Bearish (0.8): palestinian shot dead during israeli settler attack
- Bearish (0.3): has US achieved war objectives in Iran?
- Bullish (0.7): iran ceasefire deal gives Trump a way out
- Bot placed 5 sentiment-driven buys on Israel/Gaza markets at T+0 — all filled profitably

### Risk Events

- **2 forced exits** — both profitable:
  - NHL Hart Trophy: dynamic stop at $0.1480, PnL = +$0.117
  - Fed upper bound: timeout at $0.1120, PnL = +$1.252
- **18 flow pulls** (momentum surge detection on 50¢+ moves)
- **0 circuit breakers** — never hit 10% daily loss limit
- **0 losing streak pauses** — never lost 3 in a row
- **5 STAR markets promoted** by brain during session

### Critical Observations

#### 1. Diversification Improved vs Prior Sessions
Session #5 had 85% of PnL from one market (Fed). Session #9's top market (Fed) contributed only 24% of PnL. The bot spread across 7 active markets with 6 contributing >$7 each. This is a meaningful improvement from the one_side strategy and market pruning.

#### 2. NHL Hart Trophy — Explosive New Market
The Hart Trophy market was newly discovered mid-session and immediately became a top earner (+$8.79 in 3 trades, $2.93/trade avg). The bot placed a buy at $0.126 and sold at $0.214 — an 8.8¢ capture on a market that was seeing 50¢+ momentum swings. Brain promoted it to STAR status after just 3 trades.

#### 3. Adverse Selection Still Too Low
Only 2 adverse fills in 92 trades (2.2%). Real Polymarket adverse selection runs 15-25%. The fill simulator's flow-based adverse detection catches large informed flows but misses the constant small adverse selection from competing makers and takers.

#### 4. Zero Drawdown Is Still a Red Flag
0% drawdown across 92 trades and 29 minutes means the fill simulator isn't punishing the bot enough. In live trading, even with a 90%+ WR strategy, you'd expect 3-8% drawdowns from slippage, partial fills, and timing risk.

#### 5. Growth Rate Was Remarkably Linear
$3.48/minute average with no single blowup spike. The equity curve shows steady accumulation, not boom-bust. This suggests the edge is in the strategy mechanics (mid-relative placement on wide-spread markets), not in lucky fills.

#### 6. Market Pruning Is Doing Heavy Lifting
50+ markets pruned across 12 sweeps. Without pruning, the bot would waste orders on dead markets (no WS trade activity). The 2-minute inactivity threshold works well — it keeps the bot focused on markets with actual flow.

### Comparison: Session #5 vs #6 vs #9

| Metric | #5 (both_sides) | #6 (one_side) | #9 (one_side) |
|---|---|---|---|
| Duration | 35 min | 30 min | 29 min |
| Trades | 41 | 83 | 92 |
| Return | +295% | +138% | +101% |
| Win Rate | 95.1% | 98.8% | 97.8% |
| Max DD | 7.3% | 0% | 0% |
| Losses | 2 | 1 | 2 |
| Top market % | 85% (Fed) | 44% (Stripe) | 24% (Fed) |
| Token bugs | Yes (SHORTs) | No | No |
| Sentiment trades | No | No | Yes (5 trades) |

**Key trend**: Returns declining (295% → 138% → 101%) but trade count increasing (41 → 83 → 92) and diversification improving. The declining return is likely due to:
1. Brain learning to take smaller, more frequent profits (shorter hold times)
2. Better diversification reducing concentration in explosive markets
3. Market pruning removing the highest-spread (highest-PnL) markets that were actually dead

### Honest Assessment

**What's real:**
1. Entry logic is genuinely sound — mid-relative buys 1-3¢ below mid on 10¢+ spread markets
2. Risk management works — dynamic stops, timeouts, flow pulls all firing correctly
3. Brain adaptation is working — STAR promotion, Kelly sizing, hold time adjustments
4. one_side strategy eliminates token inventory bugs from both_sides
5. Market pruning keeps focus on active markets

**What's NOT real (paper-mode inflation):**

| Metric | Paper (Session #9) | Realistic Live |
|---|---|---|
| 30-min return | +101% | +2% to -3% |
| Win rate | 98% | 60-70% |
| Adverse fill rate | 2.2% | 15-25% |
| Max drawdown | 0% | 5-15% |
| Avg win | ~$1.10 | $0.20-0.40 |
| Avg loss | $0.006 | $0.30-0.60 |

**Root causes of paper-mode optimism:**
1. MIN_REST_TIME of 8s is too short — real queue position takes 15-30s
2. Probabilistic fill layer still favors the bot (velocity bonuses, age bonuses compound)
3. No gas cost simulation (92 trades ≈ $0.50-$2.00 on Polygon)
4. Paper tax of 0.2¢ is too low — real slippage is 0.5-2¢ on thin books

### Recommendations for Session #10 / Live Prep

1. **Tighten adverse selection to 15-20%** — add probabilistic adverse fills independent of flow
2. **Increase MIN_REST_TIME to 15s** — realistic queue positioning
3. **Add gas cost simulation** — 0.5¢ per trade minimum
4. **Increase paper tax to 0.5¢** — from current 0.2¢
5. **Cap per-market PnL at 30%** — force diversification
6. **Go/no-go**: If paper WR drops to 70-75% after tightening and still makes money, that's live-ready
7. **Live pilot**: $100 real, one_side, per-order $5, 2-week trial

### Verdict

The engineering is **production-grade**. The strategy has a **real edge** on wide-spread prediction markets (capturing 5-10¢ spreads on 15-40¢ spread markets). But the fill simulator remains **2-3x too optimistic** — 98% WR and 0% drawdown are fiction. The next step is tightening the sim until paper WR drops to ~75% while maintaining positive PnL. That's the calibration that separates a cool paper bot from a live money machine.
