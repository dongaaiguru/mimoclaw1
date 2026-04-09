# PolyEdge Strategy Research — Complete Analysis
## Compiled April 9, 2026

---

## EXECUTIVE SUMMARY: What Actually Makes Money on Polymarket

After analyzing research papers ($40M in extracted arbitrage from 86M transactions), 
top trader interviews, open-source bots, and live market data, here are the **5 proven 
strategies ranked by suitability for $100 capital**:

| # | Strategy | Monthly Return | Win Rate | Min Capital | Risk |
|---|---|---|---|---|---|
| 1 | Fee-Free Spread Capture | 5-15% | 70-80% | $50 | Low |
| 2 | Liquidity Rewards + Rebates | 3-8% | N/A | $100 | Low-Med |
| 3 | Cross-Market Logical Arb | 2-5% per trade | 70-80% | $20 | Low |
| 4 | News Momentum Scalping | 8-15% | 60-70% | $50 | High |
| 5 | AI Probability Arbitrage | 3-8% | 65-75% | $100 | Medium |

**Target: 2% daily ($2 on $100) requires combining strategies 1+2+3.**

---

## STRATEGY 1: Fee-Free Spread Capture (BEST FOR $100)

**What:** Place GTC limit orders inside the spread on fee-free markets (geopolitics, world).
**Why it works:** No taker fees = entire spread is profit. Most traders are directional 
(betting on outcomes), creating natural spread for liquidity providers.

**Live data from our scan (April 9, 2026):**
- 42 markets with ≥3¢ spread, fee-free, ≥$5K liquidity
- Top: USDT $200B (15.7¢ spread), FL Senate (8¢), Ostium token (5¢)
- Estimated daily with $50 allocation: $1.50-$3.00

**Key insight from @defiance_cr (Polymarket's top MM):**
- $10K capital → $200-800/day at peak
- Two-sided orders get ~3x reward multiplier vs one-sided
- Formula: S(v,s) = ((v-s)/v)² × b — closer to midpoint = exponentially higher rewards
- He made 12.47% in 3 weeks on $10K ($1,247 profit)

**How to optimize for $100:**
- Focus on 3-5 markets with 5¢+ spreads
- Place orders 1-2¢ from midpoint (tight = more fills)
- $10-20 per position, GTC orders
- Cancel and re-price every 60s
- Exit if market moves >3¢ against you

---

## STRATEGY 2: Liquidity Rewards + Maker Rebates

**What:** Polymarket pays you to provide liquidity. Two programs:

### A. Liquidity Rewards ($5M in April 2026)
- EPL soccer games: $10,000/game ($2,800 pre-game + $7,200 live)
- La Liga: $3,300/game  
- Serie A: $900/game
- Formula rewards: two-sided orders, tight spreads, consistent quoting
- Sampled every minute, distributed daily at midnight UTC

### B. Maker Rebates (20-25% of taker fees)
- Crypto: 20% rebate on taker fees
- Sports/Politics/Finance: 25% rebate
- Paid daily in USDC to your wallet
- You compete only with other makers in same market

**Why this matters for $100:**
- Even small positions earn rewards if you're consistent
- Sports markets have guaranteed reward pools
- Rebates reduce your effective cost to zero on fee-enabled markets
- The reward formula QUADRATICALLY favors tight spreads

**Practical approach:**
- Target pre-game soccer markets (predictable, low volatility)
- Place bid+ask 1-2¢ from midpoint
- Small size ($5-10) but consistent (24/7 quoting)
- Rewards stack on top of spread capture profit

---

## STRATEGY 3: Cross-Market Logical Arb

**What:** Exploit pricing inconsistencies between logically related markets.
**Academic proof:** $40M extracted from 86M Polymarket transactions (arxiv 2508.03474).
$29M of that was from cross-market dependency, not simple YES+NO arb.

**Real examples found in our scan:**
- Trump deport <200K at 10¢ vs deport 300-400K at 36.5¢ (26.5% edge)
- Lower threshold should always ≥ higher threshold

**Three sub-strategies:**

### A. Threshold Dependencies
"If $500M happens, then $200M definitely happened"
→ P(>$200M) MUST be ≥ P(>$500M)
→ If not, buy the underpriced, sell the overpriced

### B. Time Dependencies  
"If it happens by June, it definitely happens by December"
→ P(by June) ≤ P(by December)
→ If June > December, sell June, buy December

### C. Cumulative Probability
Mutual exclusive outcomes must sum to ≤100%
If sum >102%, sell the overpriced outcomes

**For $100:**
- Need only $5-10 per leg
- 2-leg trades (buy A, sell B)
- Holding period: hours to days
- Edge is mathematical, not predictive

---

## STRATEGY 4: News Momentum Scalping

**What:** Detect breaking news, trade before market adjusts.
**Proven:** AI bot captured 13¢ spread in 8 minutes on Trump indictment news.
**Weather bots:** Making $24K/month by comparing forecasts to market prices.

**How it works for low capital:**
1. Monitor RSS feeds (Reuters, AP, Bloomberg free tier)
2. When breaking news hits market you're in, trade immediately
3. Trail stop-loss: enter at 0.34, exit at 0.49 = 15¢ profit
4. Speed is everything — seconds matter

**Risk:** Highest of all strategies. Can lose 20-30% on wrong direction.

**For $100:**
- Only use $15 (15% allocation)
- Pre-identify 3-5 markets where news can move prices
- Set alerts, not auto-trades (unless you build NLP pipeline)
- Target 5-10¢ moves, tight stops

---

## STRATEGY 5: The Open-Source Bot Approach

**Proven bot by @defiance_cr:** github.com/warproxxx/poly-maker
- Analyzes historical price volatility across timeframes
- Ranks markets by risk-adjusted reward potential  
- Places two-sided orders automatically
- Key insight: "Some markets barely move but offer huge rewards 
  relative to their volatility. Finding these gems is where profit lies."

**What we should steal from this:**
1. Volatility scoring (3h, 24h, 7d, 30d price movement)
2. Reward-to-volatility ratio ranking
3. Automatic spread width adjustment (tight in calm, wide in volatile)
4. Inventory limits per market

---

## WHAT THE TOP 0.04% DO

From the UCLA research and TRM Labs analysis:
- 668 wallets with >$1M profit = 71% of all profits
- Top trader: $2M from 4,049 trades ($496/trade average)
- **They're not predicting outcomes. They're extracting mathematical edge.**
- Three main strategies: arbitrage, market making, information advantage
- They use dedicated Polygon RPC nodes for sub-100ms execution

---

## OPTIMAL $100 STRATEGY MIX

Based on all research, here's the optimal allocation:

| Strategy | Allocation | Capital | Expected Daily | Risk |
|---|---|---|---|---|
| Fee-Free Spread Capture | 40% | $40 | $0.60-1.50 | Low |
| Sports Liquidity Rewards | 25% | $25 | $0.30-0.80 | Low |
| Logical Arb (when found) | 20% | $20 | $0.20-1.00 | Low |
| News Momentum | 15% | $15 | $0.00-0.50 | High |
| **TOTAL** | **100%** | **$100** | **$1.10-3.80** | **Mixed** |

**Realistic 2% daily ($2.00): Achievable on days with good spread markets + 1 arb trade.**

---

## WHAT MAKES THE BOT BETTER THAN THE ORIGINAL CODE

The original `mimoclaw1` code failed because:
1. BTC lag arbitrage was killed (March 2026 fee changes)
2. Bayesian estimator needs real exchange price feed (no alpha without it)
3. Backtest showed Sharpe 0.04 (coin flip)
4. No liquidity rewards awareness
5. No cross-platform or cross-market dependency detection

Our bot adds:
1. ✅ Fee-free market detection and exploitation
2. ✅ Real-time dependency scanning (threshold, time, mutex)
3. ✅ Liquidity rewards optimization (two-sided, tight spread)
4. ✅ Small order sizing for better fill rates on $100
5. ✅ Volatility-aware spread width
6. ✅ Multiple strategy engines running in parallel
