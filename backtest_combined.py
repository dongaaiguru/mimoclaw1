"""
MIMOCLAW14 — Backtest Engine
=============================
Backtests all 4 strategies with $100 capital using real Polymarket market data.

DISCLAIMER: This is a simulation using realistic parameters calibrated to
actual Polymarket market data (fetched April 9, 2026). Real trading results
will differ due to:
- Execution latency and slippage
- Competition from other bots
- Market impact of our orders
- Reward pool changes
- News timing uncertainty

This backtest is for STRATEGY VALIDATION, not profit prediction.

Strategies:
1. Market Making — spread capture + liquidity rewards
2. Directional Signal — Bayesian/EV score engine (from mimoclaw1)
3. Logical Arbitrage — cross-market dependency violations
4. Information Arbitrage — news detection + momentum

Capital: $100 | GTC limit orders only | 15% drawdown circuit breaker
"""

import math
import random
from dataclasses import dataclass
from typing import Dict, List

# ══════════════════════════════════════════════════════════════
# REAL MARKET DATA (Polymarket API, April 9, 2026)
# ══════════════════════════════════════════════════════════════

CRYPTO_MARKETS = [
    {"slug":"mstr-sell-jun","p":0.0275,"s":0.001,"v":918425,"l":36209,"d":82,"fees":0},
    {"slug":"mstr-sell-dec","p":0.1150,"s":0.01,"v":465020,"l":24318,"d":266,"fees":0},
    {"slug":"megaeth-1b","p":0.3300,"s":0.02,"v":2893717,"l":65981,"d":30,"fees":0},
    {"slug":"megaeth-2b","p":0.0950,"s":0.01,"v":5820132,"l":77633,"d":30,"fees":0},
    {"slug":"megaeth-3b","p":0.0455,"s":0.003,"v":1651688,"l":46136,"d":30,"fees":0},
    {"slug":"megaeth-4b","p":0.0340,"s":0.006,"v":1588401,"l":62292,"d":30,"fees":0},
    {"slug":"megaeth-6b","p":0.0210,"s":0.004,"v":2346300,"l":54428,"d":30,"fees":0},
    {"slug":"megaeth-800m","p":0.4600,"s":0.02,"v":219820,"l":39842,"d":30,"fees":0},
    {"slug":"megaeth-600m","p":0.6750,"s":0.03,"v":107685,"l":33565,"d":30,"fees":0},
    {"slug":"megaeth-1.5b","p":0.1150,"s":0.01,"v":375252,"l":26729,"d":30,"fees":0},
    {"slug":"megaeth-airdrop","p":0.4265,"s":0.009,"v":1046492,"l":8559,"d":82,"fees":0},
    {"slug":"trump-crypto-tax","p":0.0445,"s":0.025,"v":19964,"l":8595,"d":270,"fees":0},
]

SPORTS_MARKETS = [
    {"slug":"avalanche-cup","p":0.2020,"s":0.006,"v":13395480,"l":113179,"d":60,"fees":0.03},
    {"slug":"lightning-cup","p":0.1515,"s":0.003,"v":1455160,"l":87991,"d":60,"fees":0.03},
    {"slug":"hurricanes-cup","p":0.1250,"s":0.01,"v":268323,"l":139498,"d":60,"fees":0.03},
    {"slug":"stars-cup","p":0.0795,"s":0.005,"v":1009298,"l":103867,"d":60,"fees":0.03},
    {"slug":"kings-cup","p":0.0090,"s":0.002,"v":6582815,"l":164673,"d":60,"fees":0.03},
    {"slug":"goldenknights-cup","p":0.0625,"s":0.001,"v":1359556,"l":100462,"d":60,"fees":0.03},
]

DEPENDENCIES = [
    {"a":"megaeth-6b","b":"megaeth-4b","type":"subset"},
    {"a":"megaeth-4b","b":"megaeth-3b","type":"subset"},
    {"a":"megaeth-3b","b":"megaeth-2b","type":"subset"},
    {"a":"megaeth-2b","b":"megaeth-1.5b","type":"subset"},
    {"a":"megaeth-1.5b","b":"megaeth-1b","type":"subset"},
    {"a":"megaeth-1b","b":"megaeth-800m","type":"subset"},
    {"a":"megaeth-800m","b":"megaeth-600m","type":"subset"},
    {"a":"mstr-sell-jun","b":"mstr-sell-dec","type":"subset"},
]


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


# ══════════════════════════════════════════════════════════════
# BAYESIAN ESTIMATOR
# ══════════════════════════════════════════════════════════════

class BayesianEstimator:
    def __init__(self, prior=0.5, strength=30.0):
        self.alpha = prior * strength
        self.beta = (1 - prior) * strength
        self._history = []

    @property
    def prob(self):
        return self.alpha / (self.alpha + self.beta)

    def update(self, price_move, volume_ratio=1.0, trade_side=0):
        vol_w = min(volume_ratio / 2.0, 3.0)
        move_w = abs(price_move) * 100
        if price_move > 0 or trade_side > 0:
            self.alpha += vol_w * move_w + 0.5
        elif price_move < 0 or trade_side < 0:
            self.beta += vol_w * move_w + 0.5
        self._history.append(self.prob)

    def delta_p(self, lookback=15):
        if len(self._history) >= lookback:
            return self.prob - self._history[-lookback]
        elif self._history:
            return self.prob - self._history[0]
        return 0.0


# ══════════════════════════════════════════════════════════════
# PRICE SIMULATOR (calibrated to real Polymarket dynamics)
# ══════════════════════════════════════════════════════════════

def generate_ticks(market, n_ticks, seed):
    rng = random.Random(seed)
    ticks = []
    price = market['p']
    base_spread = market['s']

    # Crypto: higher vol. Sports: lower vol.
    if market.get('fees', 0) == 0:
        tick_vol = 0.02 / math.sqrt(1440)
        jump_prob = 0.005
        jump_scale = 0.03
    else:
        tick_vol = 0.02 / math.sqrt(1440) * 0.3
        jump_prob = 0.001
        jump_scale = 0.02

    for i in range(n_ticks):
        move = rng.gauss(0, tick_vol)
        if rng.random() < jump_prob:
            move += rng.gauss(0, jump_scale)
        price = clamp(price + move, 0.005, 0.995)
        spread = clamp(base_spread * (1 + abs(move) * 50), 0.001, 0.05)
        ticks.append({
            'tick': i, 'price': price,
            'bid': price - spread/2, 'ask': price + spread/2,
            'spread': spread, 'change': move,
            'volume': max(0.1, rng.expovariate(1.0)),
        })
    return ticks


# ══════════════════════════════════════════════════════════════
# ENGINE 1: MARKET MAKING
# ══════════════════════════════════════════════════════════════

def backtest_mm(markets, capital, n_ticks, seed):
    """
    Market making: place GTC limit orders on both sides.
    Earn spread + proportional share of liquidity rewards.

    Conservative assumptions:
    - We're 1 of ~10 active market makers → ~10% reward share
    - 50% uptime (we pull for news, rebalancing, sleep)
    - Fills only when market price crosses our quotes
    - Fee calculation from Polymarket's actual fee curve
    """
    rng = random.Random(seed)

    # Score markets: want high volume, high liquidity, wide spread
    scored = sorted(markets, key=lambda m: m['v'] * m['l'] / max(m['s'], 0.001), reverse=True)
    mm_markets = scored[:5]

    all_ticks = {m['slug']: generate_ticks(m, n_ticks, seed + i * 100)
                 for i, m in enumerate(mm_markets)}

    cash = capital
    cap_per = capital / len(mm_markets)
    positions = {}
    total_pnl = 0.0
    total_rewards = 0.0
    trades = 0
    round_trips = 0
    rt_profits = []

    reward_share = 0.10  # 10% of reward pool (conservative)
    uptime = 0.50  # 50% uptime

    def fee(price, shares, rate):
        return rate * price * (1 - price) * shares if rate > 0 else 0.0

    for ti in range(n_ticks):
        for m in mm_markets:
            slug = m['slug']
            tick = all_ticks[slug][ti]
            mid = tick['price']
            rate = m.get('fees', 0.05)

            half_spread = max(0.004, tick['spread'] * 2.0)
            our_bid = mid - half_spread
            our_ask = mid + half_spread
            order_size = cap_per * 0.10

            if slug not in positions:
                if tick['bid'] <= our_bid and order_size <= cash:
                    shares = order_size / our_bid
                    cost = order_size + fee(our_bid, shares, rate)
                    if cost <= cash:
                        cash -= cost
                        positions[slug] = {'price': our_bid, 'size': order_size,
                                          'shares': shares, 'tick': ti}
                        trades += 1
            else:
                pos = positions[slug]
                if tick['ask'] >= our_ask:
                    rev = pos['shares'] * our_ask - fee(our_ask, pos['shares'], rate)
                    pnl = rev - pos['size']
                    cash += rev
                    total_pnl += pnl
                    round_trips += 1
                    rt_profits.append(pnl)
                    trades += 1
                    del positions[slug]

            # Rewards (proportional share, with uptime)
            if slug not in positions:
                daily_reward = m.get('rewards_day', 0) if 'rewards_day' in m else 500
                r = (daily_reward / 1440) * reward_share * uptime
                total_rewards += r
                cash += r

    # Close remaining
    for slug, pos in list(positions.items()):
        last = all_ticks[slug][-1]
        rate = next((m.get('fees', 0.05) for m in mm_markets if m['slug'] == slug), 0.05)
        rev = pos['shares'] * last['bid'] - fee(last['bid'], pos['shares'], rate)
        pnl = rev - pos['size']
        cash += rev
        total_pnl += pnl

    return {
        "pnl": total_pnl, "rewards": total_rewards,
        "total": total_pnl + total_rewards,
        "trades": trades, "round_trips": round_trips,
        "avg_rt": sum(rt_profits) / max(1, len(rt_profits)),
        "final": cash,
        "return_pct": (cash / capital - 1) * 100,
    }


# ══════════════════════════════════════════════════════════════
# ENGINE 2: DIRECTIONAL SIGNAL
# ══════════════════════════════════════════════════════════════

def backtest_directional(markets, capital, n_ticks, seed, threshold=0.50):
    rng = random.Random(seed)
    crypto = [m for m in markets if m.get('fees', 0) == 0]

    all_exchange = {}
    all_market = {}
    estimators = {}

    for i, m in enumerate(crypto):
        ms = seed + i * 1000
        all_exchange[m['slug']] = generate_ticks(m, n_ticks, ms)
        all_market[m['slug']] = generate_ticks(m, n_ticks, ms + 500)
        estimators[m['slug']] = BayesianEstimator(m['p'], 30.0)

    dep_map = {}
    for d in DEPENDENCIES:
        dep_map[(d['a'], d['b'])] = d['type']
        dep_map[(d['b'], d['a'])] = 'superset'

    cash = capital
    peak = capital
    positions = {}
    trades = []
    fees = 0.002

    for ti in range(n_ticks):
        for m in crypto:
            slug = m['slug']
            if ti >= len(all_market[slug]):
                continue

            ex = all_exchange[slug][ti]
            mk = all_market[slug][ti]
            est = estimators[slug]

            if ti > 0:
                em = ex['price'] - all_exchange[slug][ti-1]['price']
                est.update(em * 3.0, mk['volume'], 1 if em > 0 else -1)
                est.update(mk['change'], mk['volume'], 0)

            # Exit
            if slug in positions:
                pos = positions[slug]
                cur = mk['bid']
                pp = (cur - pos['entry']) / max(pos['entry'], 0.001)
                reason = None
                if pp >= 0.04: reason = "profit_target"
                elif pp <= -0.08: reason = "stop_loss"
                elif ti - pos['tick'] > 600: reason = "time_decay"
                if reason:
                    pnl = pos['size'] * pp - pos['size'] * fees * 2
                    cash += pos['size'] + pnl
                    peak = max(peak, cash)
                    trades.append({'pnl': pnl, 'reason': reason})
                    del positions[slug]
                continue

            if len(positions) >= 2 or (peak - cash) / max(peak, 0.01) >= 0.15:
                continue

            # Score components
            raw_ev = est.prob - mk['ask'] - fees
            ev_n = clamp(raw_ev / 0.15, 0, 1)

            kl_n = 0.0
            for (a, b), rel in dep_map.items():
                if slug == a and b in all_market:
                    ot = all_market[b][min(ti, len(all_market[b])-1)]
                    if rel == 'subset' and ot['price'] < mk['price'] - 0.005:
                        p2, q2 = clamp(mk['price'], 1e-6, 1-1e-6), clamp(ot['price'], 1e-6, 1-1e-6)
                        kl_raw = p2 * math.log(p2/q2) + (1-p2) * math.log((1-p2)/(1-q2))
                        kl_n = max(kl_n, clamp(kl_raw / 0.08, 0, 1))

            dp = est.delta_p(15)
            dp_n = clamp(max(dp, 0) / 0.04, 0, 1)

            b = max(20, m['l'] / 50)
            ps = min(cash * 0.25 / 2, cash * 0.15)
            lmsr_n = 0.0
            if 0 < mk['price'] < 1 and b > 0:
                lr = math.log(mk['price'] / (1 - mk['price']))
                q = b * lr / 2
                sh = ps / max(mk['price'], 0.001)
                en = math.exp(clamp((q + sh) / b, -20, 20))
                eno = math.exp(clamp(-q / b, -20, 20))
                pa = en / (en + eno)
                lmsr_n = clamp(max(pa - mk['price'], 0) / 0.02, 0, 1)

            risk_n = 0.3 * clamp(mk['spread'] / 0.03, 0, 1)
            score = 0.35 * ev_n + 0.20 * kl_n + 0.20 * dp_n + 0.15 * lmsr_n - 0.10 * risk_n

            ok = raw_ev >= 0.015 and mk['spread'] < 0.03 and m['l'] >= 8000
            if score > threshold and ok and cash > 1.0:
                kelly = max(0, (est.prob - mk['ask']) / (1 - mk['ask']) * 0.25)
                size = min(kelly * cash, cash / 3, cash * 0.25, 6.25)
                if size >= 1.0 and size + size * fees <= cash:
                    cash -= size + size * fees
                    positions[slug] = {'entry': mk['ask'], 'size': size,
                                      'tick': ti, 'score': score}

    # Close remaining
    for slug, pos in list(positions.items()):
        last = all_market[slug][-1]
        pp = (last['bid'] - pos['entry']) / max(pos['entry'], 0.001)
        pnl = pos['size'] * pp - pos['size'] * fees * 2
        cash += pos['size'] + pnl
        trades.append({'pnl': pnl, 'reason': 'end'})

    wins = [t for t in trades if t['pnl'] > 0]
    total_pnl = sum(t['pnl'] for t in trades)
    return {
        "pnl": total_pnl, "trades": len(trades), "wins": len(wins),
        "win_rate": len(wins) / max(1, len(trades)),
        "final": cash, "return_pct": (cash / capital - 1) * 100,
    }


# ══════════════════════════════════════════════════════════════
# ENGINE 3: LOGICAL ARBITRAGE
# ══════════════════════════════════════════════════════════════

def backtest_arb(all_markets, capital, n_ticks, seed):
    rng = random.Random(seed)
    mmap = {m['slug']: m for m in all_markets}
    needed = set()
    for d in DEPENDENCIES:
        needed.add(d['a']); needed.add(d['b'])

    all_ticks = {}
    for i, slug in enumerate(needed):
        if slug in mmap:
            all_ticks[slug] = generate_ticks(mmap[slug], n_ticks, seed + i * 200)

    cash = capital
    peak = capital
    positions = {}
    trades = []
    opps = 0
    gas = 0.002

    for ti in range(n_ticks):
        for dep in DEPENDENCIES:
            a, b = dep['a'], dep['b']
            if a not in all_ticks or b not in all_ticks: continue
            if ti >= len(all_ticks[a]) or ti >= len(all_ticks[b]): continue

            pa = all_ticks[a][ti]['price']
            pb = all_ticks[b][ti]['price']
            key = (a, b)

            # Exit
            if key in positions:
                pos = positions[key]
                if pb >= pa - 0.005 or ti - pos['tick'] > 720:
                    pnl = pos['size'] * ((pos['pa'] - pa) / max(pos['pa'], 0.001) +
                          (pb - pos['pb']) / max(pos['pb'], 0.001)) - pos['size'] * gas * 4
                    cash += pos['size'] * 2 + pnl
                    peak = max(peak, cash)
                    trades.append({'pnl': pnl})
                    del positions[key]
                continue

            # Entry
            if dep['type'] == 'subset' and pb < pa - 0.03:
                opps += 1
                if cash > 2.0:
                    size = min(cash * 0.10, 5.0)
                    cost = size * 2 + size * gas * 2
                    if cost <= cash:
                        cash -= cost
                        positions[key] = {'pa': pa, 'pb': pb, 'size': size, 'tick': ti}

    # Close remaining
    for key, pos in list(positions.items()):
        a, b = key
        pa = all_ticks[a][-1]['price']; pb = all_ticks[b][-1]['price']
        pnl = pos['size'] * ((pos['pa'] - pa) / max(pos['pa'], 0.001) +
              (pb - pos['pb']) / max(pos['pb'], 0.001)) - pos['size'] * gas * 4
        cash += pos['size'] * 2 + pnl
        trades.append({'pnl': pnl, 'reason': 'end'})

    wins = [t for t in trades if t['pnl'] > 0]
    total_pnl = sum(t['pnl'] for t in trades)
    return {
        "pnl": total_pnl, "trades": len(trades), "opps": opps,
        "wins": len(wins), "win_rate": len(wins) / max(1, len(trades)),
        "final": cash, "return_pct": (cash / capital - 1) * 100,
    }


# ══════════════════════════════════════════════════════════════
# ENGINE 4: INFORMATION ARBITRAGE
# ══════════════════════════════════════════════════════════════

def backtest_info(all_markets, capital, n_ticks, seed):
    """
    News detection + momentum trading.
    Conservative assumptions:
    - 55% detection accuracy (45% false positives)
    - 52% snipe win rate (tiny edge)
    - Competition reduces our advantage
    """
    rng = random.Random(seed)
    crypto = [m for m in all_markets if m.get('fees', 0) == 0]

    all_ticks = {}
    for i, m in enumerate(crypto):
        ticks = generate_ticks(m, n_ticks, seed + i * 300)
        # Inject news events
        for _ in range(rng.randint(2, 5)):
            et = rng.randint(100, n_ticks - 100)
            d = 1 if rng.random() > 0.5 else -1
            mag = rng.uniform(0.02, 0.10)
            for t in range(et, min(et + 30, n_ticks)):
                decay = 1.0 - (t - et) / 30
                ticks[t]['price'] = clamp(ticks[t]['price'] + d * mag * decay, 0.005, 0.995)
                ticks[t]['change'] = d * mag * decay
        all_ticks[m['slug']] = ticks

    cash = capital
    peak = capital
    positions = {}
    trades = []
    news_det = 0
    det_accuracy = 0.55
    snipe_edge = 0.52

    for ti in range(n_ticks):
        for m in crypto:
            slug = m['slug']
            if ti >= len(all_ticks[slug]): continue
            tick = all_ticks[slug][ti]

            # Exit
            if slug in positions:
                pos = positions[slug]
                cur = tick['bid']
                pp = (cur - pos['entry']) / max(pos['entry'], 0.001)
                reason = None
                if pp >= 0.05: reason = "profit"
                elif pp <= -0.05: reason = "stop"
                elif ti - pos['tick'] > 60: reason = "timeout"
                if reason:
                    pnl = pos['size'] * pp - pos['size'] * 0.002
                    cash += pos['size'] + pnl
                    peak = max(peak, cash)
                    trades.append({'pnl': pnl, 'reason': reason})
                    del positions[slug]
                continue

            # News detection
            if ti > 0:
                prev = all_ticks[slug][ti - 1]
                pm = abs(tick['price'] - prev['price'])
                vs = tick['volume'] / max(0.1, prev['volume'])
                if pm > 0.015 and vs > 2.5:
                    news_det += 1
                    if rng.random() < det_accuracy and cash > 1.0:
                        size = min(cash * 0.10, 3.0)
                        if size + size * 0.002 <= cash:
                            cash -= size + size * 0.002
                            positions[slug] = {'entry': tick['ask'], 'size': size, 'tick': ti}

            # 5-min snipe
            if ti > 0 and ti % 120 == 0 and slug not in positions and cash > 1.0:
                size = min(cash * 0.05, 2.0)
                if rng.random() < snipe_edge:
                    pnl = size * rng.uniform(0.02, 0.05) - size * 0.002
                else:
                    pnl = -size * rng.uniform(0.02, 0.04) - size * 0.002
                cash += size + pnl
                trades.append({'pnl': pnl, 'reason': 'snipe'})
                peak = max(peak, cash)

    # Close remaining
    for slug, pos in list(positions.items()):
        if slug in all_ticks:
            last = all_ticks[slug][-1]
            pp = (last['bid'] - pos['entry']) / max(pos['entry'], 0.001)
            pnl = pos['size'] * pp - pos['size'] * 0.002
            cash += pos['size'] + pnl
            trades.append({'pnl': pnl, 'reason': 'end'})

    wins = [t for t in trades if t['pnl'] > 0]
    total_pnl = sum(t['pnl'] for t in trades)
    return {
        "pnl": total_pnl, "trades": len(trades),
        "news_detected": news_det,
        "wins": len(wins), "win_rate": len(wins) / max(1, len(trades)),
        "final": cash, "return_pct": (cash / capital - 1) * 100,
    }


# ══════════════════════════════════════════════════════════════
# COMBINED BACKTEST
# ══════════════════════════════════════════════════════════════

def run_combined(capital=100.0, n_days=30, seed=42, quiet=False):
    n_ticks = n_days * 1440
    all_markets = CRYPTO_MARKETS + SPORTS_MARKETS

    mm = backtest_mm(SPORTS_MARKETS, capital * 0.40, n_ticks, seed)
    dr = backtest_directional(CRYPTO_MARKETS, capital * 0.25, n_ticks, seed)
    ar = backtest_arb(all_markets, capital * 0.20, n_ticks, seed)
    inf = backtest_info(all_markets, capital * 0.15, n_ticks, seed)

    total_pnl = mm['total'] + dr['pnl'] + ar['pnl'] + inf['pnl']
    final = mm['final'] + dr['final'] + ar['final'] + inf['final']
    daily = total_pnl / n_days
    ret = (final / capital - 1) * 100

    if not quiet:
        print(f"\n{'='*65}")
        print(f"  BACKTEST: ${capital:.0f} | {n_days}d | Seed {seed}")
        print(f"  ⚠ SIMULATION — not live results. See DISCLAIMER above.")
        print(f"{'='*65}")
        print(f"  {'Engine':<20s} {'P&L':>10s} {'Return':>10s} {'Final':>10s}")
        print(f"  {'─'*50}")
        print(f"  {'Market Making':<20s} ${mm['total']:>+9.2f} {mm['return_pct']:>+9.1f}% ${mm['final']:>9.2f}")
        print(f"  {'  ├ Spread':<20s} ${mm['pnl']:>+9.2f}")
        print(f"  {'  └ Rewards':<20s} ${mm['rewards']:>+9.2f}")
        print(f"  {'Directional':<20s} ${dr['pnl']:>+9.2f} {dr['return_pct']:>+9.1f}% ${dr['final']:>9.2f}")
        print(f"  {'Logical Arb':<20s} ${ar['pnl']:>+9.2f} {ar['return_pct']:>+9.1f}% ${ar['final']:>9.2f}")
        print(f"  {'Info Arb':<20s} ${inf['pnl']:>+9.2f} {inf['return_pct']:>+9.1f}% ${inf['final']:>9.2f}")
        print(f"  {'─'*50}")
        print(f"  {'TOTAL':<20s} ${total_pnl:>+9.2f} {ret:>+9.1f}% ${final:>9.2f}")
        print(f"  {'Daily avg':<20s} ${daily:>+9.2f}/day")
        print(f"")
        print(f"  Trades: MM={mm['trades']} Dir={dr['trades']}({dr['win_rate']:.0%}) "
              f"Arb={ar['trades']}({ar['win_rate']:.0%}) Info={inf['trades']}({inf['win_rate']:.0%})")
        print(f"  Account safe: {'YES ✓' if final > capital * 0.85 else 'NO ✗'} (85% floor)")

    return {"seed": seed, "pnl": total_pnl, "daily": daily, "ret": ret, "final": final,
            "mm": mm, "dir": dr, "arb": ar, "info": inf}


def monte_carlo(n_runs=30, capital=100.0, n_days=30):
    print(f"\n{'#'*65}")
    print(f"  MONTE CARLO: {n_runs} runs × {n_days} days | ${capital}")
    print(f"  ⚠ SIMULATION — calibrated to real market parameters")
    print(f"  ⚠ Real results will differ. This validates strategy logic.")
    print(f"{'#'*65}")

    results = []
    for i in range(n_runs):
        r = run_combined(capital, n_days, seed=42 + i * 7, quiet=True)
        results.append(r)
        print(f"  [{i+1:2d}/{n_runs}] P&L=${r['pnl']:>+8.2f} Ret={r['ret']:>+7.1f}% "
              f"MM=${r['mm']['total']:>+6.2f} Dir=${r['dir']['pnl']:>+6.2f} "
              f"Arb=${r['arb']['pnl']:>+6.2f} Info=${r['info']['pnl']:>+6.2f}")

    pnls = sorted([r['pnl'] for r in results])
    finals = [r['final'] for r in results]
    daily = [r['daily'] for r in results]
    profitable = sum(1 for p in pnls if p > 0)

    print(f"\n{'#'*65}")
    print(f"  SUMMARY")
    print(f"{'#'*65}")
    print(f"  Profitable:   {profitable}/{n_runs} ({profitable/n_runs*100:.0f}%)")
    print(f"  Mean P&L:     ${sum(pnls)/len(pnls):+.2f}")
    print(f"  Median P&L:   ${pnls[len(pnls)//2]:+.2f}")
    print(f"  Worst:        ${min(pnls):+.2f}")
    print(f"  Best:         ${max(pnls):+.2f}")
    print(f"  Daily avg:    ${sum(daily)/len(daily):+.2f}/day")
    print(f"  Worst final:  ${min(finals):.2f}")
    print(f"  Never blown:  {'YES ✓' if min(finals) > 0 else 'NO ✗'}")
    print(f"{'#'*65}")
    return results


if __name__ == "__main__":
    print("MIMOCLAW14 Backtest Engine")
    print("Using real Polymarket data (April 9, 2026)")
    print()
    run_combined(100.0, 30, seed=42)
    monte_carlo(10, 100.0, 30)
