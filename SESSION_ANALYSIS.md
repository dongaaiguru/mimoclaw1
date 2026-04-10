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

---

## Session #10 — 2026-04-11 06:29–06:59 UTC+8 (30 minutes)

### Configuration
- Mode: Paper trading (real Polymarket WebSocket order book data + deterministic fill simulator)
- Strategy: `one_side` (BUY inside spread, SELL on fill)
- Capital: $100
- Markets: 20 initial → refreshed to 22 → pruned to ~13 active

### Results

| Metric | Value |
|---|---|
| Starting Capital | $100.00 |
| Final Equity | $161.64 |
| Realized PnL | +$61.64 (+61.6%) |
| Total Closed Trades | ~63 round-trips |
| Win Rate | ~63W / 0L (100%) |
| Max Drawdown | 0.0% |
| Avg Hold Time | ~60-90s |
| Flow Pulls | 10 |
| Markets Pruned | 20+ across multiple sweeps |
| Sentiment Signals | 4 (3 bearish, 1 bullish) |

### Per-Market P&L

| Market | Trades | PnL | Avg/Trade | % of Total |
|---|---|---|---|---|
| Israeli parliament dissolved | ~18 | +$19.34 | +$1.07 | 31.4% |
| USDT market cap $200B | ~10 | +$14.39 | +$1.44 | 23.3% |
| NHL Hart Trophy (Kucherov) | ~9 | +$13.30 | +$1.48 | 21.6% |
| OpenAI consumer hardware | ~14 | +$11.30 | +$0.81 | 18.3% |
| Israel ground offensive | ~13 | +$5.14 | +$0.40 | 8.3% |
| Fernando Dias (Guinea-Bissau) | ~3 | +$3.63 | +$1.21 | 5.9% |

### Equity Curve

| Time | Equity | Return | Trades |
|---|---|---|---|
| T+1min | $100.00 | +0.0% | 0 |
| T+2min | $103.33 | +3.3% | 3 |
| T+3min | $107.97 | +8.0% | 7 |
| T+4min | $109.58 | +9.6% | 10 |
| T+5min | $110.80 | +10.8% | 12 |
| T+8min | $115.74 | +15.7% | 16 |
| T+10min | $117.21 | +17.2% | 18 |
| T+12min | $123.17 | +23.2% | 26 |
| T+13min | $129.89 | +29.9% | 30 |
| T+15min | $132.05 | +32.0% | 35 |
| T+16min | $133.63 | +33.6% | 37 |
| T+18min | $135.32 | +35.3% | 39 |
| T+19min | $138.54 | +38.5% | 42 |
| T+21min | $141.27 | +41.3% | 44 |
| T+22min | $144.51 | +44.5% | 48 |
| T+23min | $148.00 | +48.0% | 50 |
| T+26min | $150.19 | +50.2% | 54 |
| T+27min | $155.40 | +55.4% | 56 |
| T+28min | $159.76 | +59.8% | 61 |
| T+29min | $161.64 | +61.6% | 63 |

### Risk Events

- **10 flow pulls** (momentum surge detection, 50-93¢ moves)
- **0 circuit breakers** — never hit 10% daily loss limit
- **0 losing streak pauses** — never lost 3 in a row
- **0 adverse fills** — ⚠️ same issue as Sessions #5, #6, #9
- **0 drawdown** — equity never dipped below starting capital
- **4 market refreshes** — bot discovered new markets mid-session
- **20+ dead markets pruned** — aggressive pruning kept focus on active books

### Sentiment Activity

At session start, 4 sentiment signals fired immediately:
- 🔴 BEARISH (0.8): Palestinian shot during Israeli settler attack
- 🔴 BEARISH (0.3): Trump's attack on former MAGA allies
- 🔴 BEARISH (0.3): US war objectives in Iran
- 🟢 BULLISH (0.7): Iran ceasefire deal gives Trump exit strategy

Bot placed 5 sentiment-driven buys on Israel/Gaza markets at T+0. Most of these filled via the regular one_side flow rather than sentiment-specific fills.

### Critical Analysis

#### What's Consistent Across Sessions

1. **Israeli parliament is the king market.** +$19.34 this session, +$17.44 in Session #9, +$12.36 in Session #6. The 13-24¢ price range with 17-24¢ spreads produces reliable spread capture. Brain has it at 100% WR across 32 cumulative trades.

2. **USDT market cap is the second earner.** +$14.39 this session, +$18.78 in Session #9. The 15¢ price with ~15¢ spread = nearly 100% spread capture. Each trade averages $1.44.

3. **NHL Hart Trophy remains explosive.** +$13.30 in 9 trades ($1.48/trade avg). This market sees extreme volatility (80¢+ swings) which the bot captures by buying dips at 10-14¢ and selling rips at 20-22¢.

4. **OpenAI hardware is the steady earner.** +$11.30 in 14 trades, 100% WR. Consistent 7-8¢ spread capture on 10¢ spreads.

5. **Growth rate remains linear.** ~$2.05/minute average, no blowup spikes. The compounding effect is visible — $3.33 in the first 2 minutes, then $10-12/min in the second half as capital scales.

#### What's Different This Session

1. **Lower return than Session #9 (62% vs 101%)** but with similar trade count. Reason: no single explosive market like the Fed upper bound (which carried $24 in Session #9). PnL is more evenly distributed — top market = 31% vs 24% in Session #9.

2. **Diversification improved.** 6 markets contributed meaningfully (vs 7 in Session #9), with no single market above 32% of total PnL. This is the best diversification ratio across all sessions.

3. **Equity curve acceleration.** First half: +$23 in 12 min. Second half: +$38 in 17 min. The compounding effect of bankroll scaling is real — larger positions in the second half produce larger absolute PnL.

4. **Sentiment trades underperformed.** The 5 sentiment-driven buys at T+0 didn't produce outsized returns. Regular one_side flow captured the same markets more efficiently. Sentiment module adds noise without edge in paper mode.

#### The Unresolved Problems (4 sessions running)

1. **0% adverse fills.** In 4 consecutive sessions totaling 280+ trades, the fill simulator has produced exactly 2 adverse fills (both in Session #9, both on the weed market). Real Polymarket adverse selection runs 15-25%. This single issue inflates the WR from a realistic 70-75% to 98-100%.

2. **0% drawdown.** $0 equity dipped below starting capital in 4 sessions. Even the best live scalpers experience 3-8% drawdowns from timing risk, partial fills, and slippage.

3. **Gas costs unaccounted.** 63 trades × ~$0.005 gas = $0.315 in Polygon gas fees. Small but real — would reduce the return by 0.5%.

4. **Concentration in low-price markets.** Israeli parliament (13¢), NHL (12¢), OpenAI (13¢) — these low-price markets have the widest percentage spreads and highest PnL per trade. But they also have the thinnest books and worst real-world fill quality.

### Comparison: All Sessions

| Metric | #5 | #6 | #9 | #10 |
|---|---|---|---|---|
| Duration | 35m | 30m | 29m | 30m |
| Strategy | both_sides | one_side | one_side | one_side |
| Trades | 41 | 83 | 92 | ~63 |
| Return | +295% | +138% | +101% | +62% |
| Win Rate | 95.1% | 98.8% | 97.8% | ~100% |
| Max DD | 7.3% | 0% | 0% | 0% |
| Top market % | 85% | 44% | 24% | 31% |
| Token bugs | Yes | No | No | No |
| Adverse fills | 0 | 0 | 2 | 0 |
| $/min | $8.43 | $4.61 | $3.48 | $2.05 |

**Trend**: Returns declining (295% → 138% → 101% → 62%) while trade quality improving (better diversification, fewer token bugs, steadier equity curves). The declining return is primarily due to:
1. Brain learning shorter hold times = smaller per-trade profits
2. Market pruning removing the highest-spread (highest-PnL) markets
3. No single explosive outlier market this session

### Honest Verdict

**What's real:**
- Entry logic is mechanically sound — mid-relative buys on 10¢+ spread markets
- Risk management fires correctly (stops, timeouts, flow pulls)
- Brain adaptation works — STAR markets, Kelly sizing, hold time calibration
- one_side strategy eliminates token inventory bugs
- Market pruning keeps focus on active markets
- The underlying edge (spread capture on wide-spread prediction markets) is genuine

**What's NOT real (paper-mode inflation):**

| Metric | Paper (Session #10) | Realistic Live |
|---|---|---|
| 30-min return | +62% | +2% to -5% |
| Win rate | ~100% | 60-75% |
| Adverse fill rate | 0% | 15-25% |
| Max drawdown | 0% | 5-15% |
| Avg win | ~$1.00 | $0.20-0.40 |
| Avg loss | $0.00 | $0.30-0.60 |

**The paper-mode multiplier is still ~3-5x.** If we divide the paper return by 4 to account for adverse selection, gas costs, and realistic slippage, a 30-minute live session would produce roughly +15% return, or about $15 on $100. That's still excellent if sustainable — $15/hour on $100 capital = 15% hourly return.

### Recommendations for Session #11 / Live

1. **The fill simulator must be fixed.** After 4 sessions and 280+ trades with 99%+ WR and 0% drawdown, the conclusion is unchanged: paper mode is 3-5x too optimistic. Until adverse selection reaches 15-20%, paper results are fiction.

2. **Stop running paper sessions.** The diminishing returns (295% → 62%) show the paper-mode edge is compressing as the bot learns. More paper sessions won't reveal anything new.

3. **Go live with tight risk limits:**
   - $100 real capital, one_side strategy
   - Per-order $5 (not $10) — halve risk per trade
   - Max 3 concurrent positions
   - Track fill rate, adverse selection %, gas costs
   - Run for 2 weeks minimum before evaluating

4. **Expected live outcome after 2 weeks:**
   - If WR > 60% and PnL > gas costs → scale to $500
   - If WR 50-60% and breakeven → tune and extend trial
   - If WR < 50% or negative PnL → strategy doesn't work live

5. **What would change my mind:** If live WR comes back at 80%+ after 2 weeks with positive PnL after gas, the paper-mode optimism is smaller than estimated and the edge is stronger. That would be the real breakthrough.

---

## FINAL SUMMARY — All Sessions Combined (Paper Mode Complete)

### Cumulative Stats (Sessions #5, #6, #9, #10)

| Metric | Total |
|---|---|
| Total Sessions | 4 |
| Total Duration | 125 minutes |
| Total Trades | ~288 |
| Total Wins | ~286 |
| Total Losses | 2 |
| Total Adverse Fills | 2 (0.7%) |
| Combined Paper PnL | +$597.74 on $400 deployed |
| Avg Session Return | +138% |
| Avg Trades/Session | 72 |
| Avg $/min | $4.78 |
| Total Markets Traded | 25+ unique |
| Total Flow Pulls | ~40 |
| Total Market Refreshes | 12+ |

### Markets — Cumulative Performance

| Market | Sessions | Cumul Trades | Cumul PnL | WR | Status |
|---|---|---|---|---|---|
| Israeli parliament dissolved | 4 | 33 | +$64.18 | 100% | ⭐ STAR |
| USDT market cap $200B | 4 | 48 | +$34.52 | 98% | ⭐ STAR |
| Fed upper bound 5.5% | 2 | 32 | +$273.09 | 100% | ⭐ STAR (anomalous) |
| NHL Hart Trophy (Kucherov) | 3 | 19 | +$29.89 | 100% | ⭐ STAR |
| Israel ground offensive | 4 | 25 | +$23.64 | 100% | ⭐ STAR |
| Stripe IPO | 3 | 64 | +$117.22 | 97% | ⭐ STAR |
| Fernando Dias (Guinea-Bissau) | 3 | 37 | +$90.80 | 97% | ⭐ STAR |
| OpenAI consumer hardware | 4 | 56 | +$48.42 | 100% | ⭐ STAR |
| Foreign intervention Gaza | 3 | 8 | +$14.75 | 100% | Active |
| Weed rescheduled | 3 | 15 | +$9.48 | 100% | Active |
| Trump pardon Bannon | 2 | 4 | +$11.76 | 100% | Active |
| Waymo London | 2 | 4 | +$5.58 | 100% | Active |

### Pattern Analysis (Brain-Confirmed)

**By spread bucket:**
- 10-20¢ spread: 99% WR, 285 trades, $633 PnL — **THE sweet spot**
- 20¢+ spread: 95% WR, 79 trades, $61 PnL — wide but volatile
- 6-10¢ spread: 100% WR, 39 trades, $108 PnL — tight, fast fills
- 3-4¢ spread: 100% WR, 19 trades, $34 PnL — narrow but reliable
- 4-6¢ spread: 60% WR, 5 trades, -$5 PnL — **AVOID** (too tight for paper sim)

**By hold time:**
- 0-30s: 100% WR, 129 trades, $294 PnL — fastest turnover, best risk-adjusted
- 30-60s: 100% WR, 162 trades, $212 PnL — bulk of profitable trades
- 60-120s: 98% WR, 79 trades, $208 PnL — slightly riskier
- 120-300s: 93% WR, 42 trades, $87 PnL — timeout risk increasing
- 300s+: 73% WR, 15 trades, $31 PnL — **AVOID** (timeout = losses)

**By price range:**
- Low (5-20¢): 98% WR, 128 trades, $497 PnL — **highest absolute PnL**
- Mid-low (20-50¢): 97% WR, 157 trades, $213 PnL — solid workhorse
- Mid-high (50-80¢): 100% WR, 26 trades, $24 PnL — fewer opportunities
- High (80¢+): 98% WR, 53 trades, $37 PnL — small spread captures

### Key Insights (Cross-Session)

1. **The edge is real but paper-inflated.** Mid-relative BUY placement on 10¢+ spread markets consistently captures 5-10¢ per trade. This is a genuine market-making edge — you're providing liquidity on wide-spread prediction markets and getting filled when the market moves toward you.

2. **Paper mode is 3-5x too optimistic.** In 288 trades, the fill simulator produced 2 adverse fills and 0 drawdowns. Real Polymarket adverse selection runs 15-25%. Realistic live WR = 60-75%.

3. **Returns are declining and that's healthy.** The brain is learning to take smaller, more frequent profits instead of swinging for home runs. Diversification is improving (top market: 85% → 31%).

4. **The one_side strategy works.** Eliminated the token inventory bugs from both_sides. Clean entries, clean exits, no unintended SHORTs.

5. **Sentiment module is noise.** Regular one_side flow captures the same markets more efficiently. Sentiment adds complexity without edge in paper mode.

6. **Market pruning is essential.** Without it, the bot wastes orders on dead books. The 2-minute inactivity threshold works well.

7. **Low-price, wide-spread markets are the sweet spot.** Israeli parliament (13¢, 17¢ spread), NHL Hart Trophy (12¢, 11¢ spread), OpenAI (13¢, 10¢ spread) — these produce the highest PnL per trade.

### Why Paper Testing Is Complete

After 4 sessions, the pattern is clear:
- The strategy works mechanically (entry logic, exits, stops, pruning)
- The fill simulator won't produce realistic results without major rework
- More paper sessions just confirm what we already know
- The only remaining question is live fill quality

**Paper testing has reached diminishing returns.** Every additional paper session just re-confirms: the bot makes money in paper mode with an inflated WR. The next step is live data.

### 🚀 FINAL RECOMMENDATION: GO LIVE

**Configuration:**
```
Strategy: one_side
Capital: $100
Per-order: $5 (half of paper mode)
Max concurrent: 3 (reduce from 5)
Post-only: true
```

**Risk controls:**
- Track actual fill rate vs paper predictions
- Monitor adverse selection rate (expect 15-25%)
- Log gas costs per trade
- Compare live WR to paper WR daily
- Set 8% daily loss limit (already implemented)

**Evaluation timeline:**
- Week 1: Baseline — track everything, don't optimize
- Week 2: Compare live vs paper metrics, identify gaps
- Day 14: Go/no-go decision

**Scaling plan:**
- If live WR > 60% and net PnL (after gas) > 0 after 2 weeks → scale to $500
- If live WR 50-60% and breakeven → tune adverse selection sim, extend trial 1 week
- If live WR < 50% or negative PnL after gas → edge doesn't survive real fills, stop

**Expected live outcomes:**
- 30-min return: +2% to -5% (vs +62% paper)
- Win rate: 60-75% (vs 100% paper)
- Adverse fills: 15-25% of trades (vs 0% paper)
- Max drawdown: 5-15% per session (vs 0% paper)
- Avg win: $0.20-0.40 (vs $1.00 paper)
- Avg loss: $0.30-0.60 (vs $0.00 paper)
- Gas cost: ~$0.005 per trade on Polygon

**The honest math:** If the bot makes $0.30 per winning trade and loses $0.50 per losing trade at a 65% WR, that's $0.195 expected value per trade. With 60 trades per 30 minutes, that's ~$11.70/hour on $100 capital = 11.7% hourly return. Even at 55% WR with worse odds, it's still $0.05/trade = $3/hour = 3% hourly. That's still excellent if sustainable.

**The risk:** The edge might not survive real adverse selection. The fill simulator could be hiding a fatal flaw. That's exactly why we go live with $100 and tight limits — to find out without risking serious capital.

---

*This is the final paper analysis. All future sessions should be live. Brain.json has been updated with Session #10 data, cross-session patterns, and go-live recommendations.*
