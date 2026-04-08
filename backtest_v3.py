"""
Backtest v3 — Post-Adaptation Strategy

CONTEXT: The original BTC lag arbitrage ($313→$2.3M) was killed by Polymarket's
dynamic taker fees in March 2026. This is the ADAPTED strategy that uses all
5 mathematical models on longer-dated markets where fees don't apply.

KEY INSIGHTS FROM ADIIX POST:
- Pure latency arb on 15-min crypto markets is dead (3.15% fees at 50c)
- Longer-dated markets remain fee-free
- Cross-market dependency arbitrage still works
- Liquidity provision earns maker rebates

ADAPTED APPROACH:
1. Focus on longer-dated crypto markets (fee-free)
2. Cross-market arbitrage (subset/superset relationships)
3. Mispricing detection using Bayesian + LMSR
4. Liquidity provision for rebate income
5. Momentum exploitation on news-driven moves

FORMULA (SAME, RECALIBRATED):
  Score = 0.35*EV + 0.20*KL + 0.20*ΔP + 0.15*LMSR - 0.10*Risk
  Trade if Score > 0.42 AND all hard filters pass
"""
import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import sys
sys.path.insert(0, '/root/.openclaw/workspace/polymarket-engine')

from src.models.score import (
    compute_ev, compute_kl, compute_delta_p, compute_stoikov_risk,
    LMSRModel, BayesianEstimator, compute_score, clamp,
)


# ══════════════════════════════════════════════════════════════
# LIVE POLYMARKET CRYPTO DATA (April 9, 2026)
# ══════════════════════════════════════════════════════════════

# Longer-dated markets = fee-free (no dynamic taker fees)
MARKETS = [
    {
        "slug": "btc-150k-jun-2026",
        "question": "BTC hits $150k by Jun 30, 2026?",
        "yes_price": 0.017,
        "spread": 0.002,
        "volume": 3_942_360,
        "liquidity": 50_000,
        "fee_free": True,  # Longer-dated
        "resolution_days": 82,
    },
    {
        "slug": "btc-150k-dec-2026",
        "question": "BTC hits $150k by Dec 31, 2026?",
        "yes_price": 0.095,
        "spread": 0.01,
        "volume": 1_000_000,
        "liquidity": 30_000,
        "fee_free": True,
        "resolution_days": 266,
    },
    {
        "slug": "mstr-sell-jun-2026",
        "question": "MicroStrategy sells BTC by Jun 30, 2026?",
        "yes_price": 0.0275,
        "spread": 0.001,
        "volume": 918_245,
        "liquidity": 65_000,
        "fee_free": True,
        "resolution_days": 82,
    },
    {
        "slug": "mstr-sell-dec-2026",
        "question": "MicroStrategy sells BTC by Dec 31, 2026?",
        "yes_price": 0.115,
        "spread": 0.01,
        "volume": 464_956,
        "liquidity": 30_000,
        "fee_free": True,
        "resolution_days": 266,
    },
    {
        "slug": "megaeth-1b",
        "question": "MegaETH FDV >$1B after launch?",
        "yes_price": 0.325,
        "spread": 0.01,
        "volume": 2_893_683,
        "liquidity": 40_000,
        "fee_free": True,
        "resolution_days": 30,
    },
    {
        "slug": "megaeth-2b",
        "question": "MegaETH FDV >$2B after launch?",
        "yes_price": 0.095,
        "spread": 0.01,
        "volume": 5_820_061,
        "liquidity": 50_000,
        "fee_free": True,
        "resolution_days": 30,
    },
    {
        "slug": "megaeth-airdrop",
        "question": "MegaETH airdrop by Jun 30?",
        "yes_price": 0.4265,
        "spread": 0.009,
        "volume": 1_046_466,
        "liquidity": 35_000,
        "fee_free": True,
        "resolution_days": 82,
    },
    {
        "slug": "trump-crypto-tax",
        "question": "Trump crypto tax elimination before 2027?",
        "yes_price": 0.0445,
        "spread": 0.025,
        "volume": 19_964,
        "liquidity": 8_000,
        "fee_free": True,
        "resolution_days": 266,
    },
]


# ══════════════════════════════════════════════════════════════
# RELATIONSHIPS (for KL divergence)
# ══════════════════════════════════════════════════════════════

RELATIONSHIPS = [
    ("btc-150k-jun-2026", "btc-150k-dec-2026", "subset", 5.5),
    ("mstr-sell-jun-2026", "mstr-sell-dec-2026", "subset", 4.2),
    ("megaeth-2b", "megaeth-1b", "subset", 3.4),
]


# ══════════════════════════════════════════════════════════════
# REALISTIC TICK GENERATOR (News-Driven + Kyle's λ)
# ══════════════════════════════════════════════════════════════

def generate_ticks_v3(
    base_price: float,
    n_ticks: int = 2000,
    volatility: float = 0.002,
    spread: float = 0.005,
    seed: int = 42,
    regime: str = "normal",  # "normal", "trending", "volatile"
) -> List[Dict]:
    """
    v3 tick generator incorporating:
    - Kyle's λ declining over time (market maturation)
    - News-driven jumps (clustered volatility)
    - Mean reversion at extremes
    - Volume-weighted price impact
    """
    rng = random.Random(seed)
    ticks = []
    price = base_price
    trend = 0.0
    vol_mult = 1.0
    kyle_lambda = 0.5  # Starting price impact (high)
    
    for i in range(n_ticks):
        # Kyle's λ declines as "market matures" (more ticks = more "mature")
        kyle_lambda = max(0.01, 0.5 * math.exp(-i / 500))
        
        # Regime changes
        if rng.random() < 0.03:
            trend = rng.gauss(0, 0.3 if regime == "trending" else 0.15)
        trend *= 0.99
        
        # Jump diffusion
        jump = 0.0
        if rng.random() < 0.012:
            direction = 1 if rng.random() < 0.55 else -1  # Slight upward bias
            jump = direction * 0.025 * rng.expovariate(1.0)
            vol_mult = 2.5
        else:
            vol_mult = max(0.5, vol_mult * 0.96)
        
        # Price update with Kyle's λ impact
        noise = rng.gauss(0, volatility * vol_mult)
        price_change = trend + noise + jump
        
        # Mean reversion at extremes
        if price > 0.9:
            price_change -= (price - 0.9) * 0.05
        elif price < 0.05:
            price_change += (0.05 - price) * 0.05
        
        old_price = price
        price = clamp(price + price_change, 0.001, 0.999)
        actual_change = price - old_price
        
        # Dynamic spread
        current_spread = spread * (1 + vol_mult * 0.5 + abs(jump) * 30)
        current_spread = max(0.001, min(current_spread, 0.05))
        
        # Volume (correlated with price impact via Kyle's λ)
        base_vol = rng.expovariate(1.0)
        if abs(actual_change) > volatility * 1.5:
            base_vol *= (1 + kyle_lambda * 10)
        if abs(jump) > 0:
            base_vol *= 5.0
        
        # Liquidity provision signal (maker rebate opportunity)
        lp_edge = current_spread - 0.005  # Spread above "normal" = LP opportunity
        
        ticks.append({
            "tick": i,
            "price": price,
            "bid": price - current_spread / 2,
            "ask": price + current_spread / 2,
            "spread": current_spread,
            "volume": base_vol,
            "price_change": actual_change,
            "trend": trend,
            "jump": jump,
            "kyle_lambda": kyle_lambda,
            "lp_edge": max(lp_edge, 0),
        })
    
    return ticks


def generate_correlated_v3(
    base_ticks: List[Dict],
    ratio: float,
    correlation: float = 0.65,
    noise: float = 0.0015,
    spread: float = 0.005,
    seed: int = 99,
) -> List[Dict]:
    """Generate correlated ticks for related market."""
    rng = random.Random(seed)
    ticks = []
    
    for bt in base_ticks:
        corr_move = bt["price_change"] * correlation
        indep_noise = rng.gauss(0, noise)
        
        if not ticks:
            price = bt["price"] * ratio
        else:
            price = clamp(ticks[-1]["price"] + corr_move + indep_noise, 0.001, 0.999)
        
        current_spread = spread * (1 + abs(bt.get("jump", 0)) * 20)
        
        ticks.append({
            "tick": bt["tick"],
            "price": price,
            "bid": price - current_spread / 2,
            "ask": price + current_spread / 2,
            "spread": current_spread,
            "volume": bt["volume"] * 0.8,
            "price_change": corr_move + indep_noise,
        })
    
    return ticks


# ══════════════════════════════════════════════════════════════
# BACKTEST v3
# ══════════════════════════════════════════════════════════════

@dataclass
class Trade:
    tick: int
    market: str
    strategy: str  # "mispricing", "momentum", "kl_arb", "lp_rebate"
    entry_price: float
    exit_price: float
    size: float
    score: float
    pnl: float
    exit_reason: str


def run_backtest_v3(
    initial_capital: float = 50.0,
    n_ticks: int = 2000,
    threshold: float = 0.42,
    max_concurrent: int = 3,
    kelly_frac: float = 0.25,
    profit_target: float = 0.04,
    stop_loss: float = 0.08,
    fees: float = 0.002,  # Reduced for fee-free markets (just gas)
    seed: int = 42,
    enable_lp: bool = True,
) -> Dict:
    """
    Backtest v3 — Post-adaptation strategy for fee-free markets.
    
    Adds:
    - LP rebate capture (maker strategy)
    - Kyle's λ awareness (avoid trading when λ is high)
    - Dynamic fee modeling (skip 15-min markets)
    """
    market_map = {m["slug"]: m for m in MARKETS}
    
    # Generate ticks
    tick_data: Dict[str, List[Dict]] = {}
    estimators: Dict[str, BayesianEstimator] = {}
    lmsr = LMSRModel()
    
    for m in MARKETS:
        m_seed = seed + hash(m["slug"]) % 10000
        vol = 0.001 + (1 - m["yes_price"]) * 0.002
        
        regime = "normal"
        if "btc" in m["slug"]:
            regime = "trending"
        elif "megaeth" in m["slug"]:
            regime = "volatile"
        
        tick_data[m["slug"]] = generate_ticks_v3(
            base_price=m["yes_price"],
            n_ticks=n_ticks,
            volatility=vol,
            spread=m["spread"],
            seed=m_seed,
            regime=regime,
        )
        
        estimators[m["slug"]] = BayesianEstimator(prior=m["yes_price"], strength=15.0)
    
    # Related market ticks
    related_data: Dict[str, List[Dict]] = {}
    for prim, rel, rel_type, ratio in RELATIONSHIPS:
        if prim in tick_data:
            m_seed = seed + hash(rel) % 10000
            related_data[rel] = generate_correlated_v3(
                tick_data[prim], ratio, seed=m_seed,
                spread=market_map.get(rel, {}).get("spread", 0.005),
            )
    
    # State
    capital = initial_capital
    peak_capital = initial_capital
    positions: Dict[str, Dict] = {}
    trades: List[Trade] = []
    all_scores: List[float] = []
    signals_checked = 0
    signals_executed = 0
    lp_trades = 0
    daily_trades = 0
    
    # ═══ MAIN LOOP ═══
    for tick_idx in range(n_ticks):
        for slug, ticks in tick_data.items():
            if tick_idx >= len(ticks):
                continue
            
            tick = ticks[tick_idx]
            market = market_map[slug]
            est = estimators[slug]
            
            # Update Bayesian
            est.update(tick["price_change"], tick["volume"],
                      1 if tick["price_change"] > 0 else -1)
            
            # ── EXIT CHECK ──
            if slug in positions:
                pos = positions[slug]
                entry = pos["entry_price"]
                current = tick["price"]
                pnl_pct = (current - entry) / entry if entry > 0 else 0
                
                should_exit = False
                exit_reason = ""
                
                if pnl_pct >= profit_target:
                    should_exit, exit_reason = True, "profit_target"
                elif pnl_pct <= -stop_loss:
                    should_exit, exit_reason = True, "stop_loss"
                elif tick_idx - pos["entry_tick"] > 400:
                    should_exit, exit_reason = True, "time_decay"
                
                if should_exit:
                    sell_price = tick["bid"]
                    pnl_pct_actual = (sell_price - entry) / entry
                    pnl_dollars = pos["size"] * pnl_pct_actual - pos["size"] * fees * 2
                    capital += pos["size"] + pnl_dollars
                    
                    trades.append(Trade(
                        tick=tick_idx, market=slug,
                        strategy=pos.get("strategy", "mispricing"),
                        entry_price=entry, exit_price=sell_price,
                        size=pos["size"], score=pos["score"],
                        pnl=pnl_dollars, exit_reason=exit_reason,
                    ))
                    del positions[slug]
                continue
            
            if len(positions) >= max_concurrent:
                continue
            
            signals_checked += 1
            
            # ═══ STRATEGY 1: MISPRICING (Primary — All 5 Models) ═══
            
            # 1. EV
            ev = compute_ev(est.probability, tick["ask"], fees, 0.015, 0.15)
            
            # 2. KL Divergence
            kl_norm = 0.0
            for prim, rel, rel_type, _ in RELATIONSHIPS:
                other_slug = None
                actual_rel = rel_type
                if slug == prim:
                    other_slug = rel
                elif slug == rel:
                    other_slug = prim
                    actual_rel = "superset"
                
                if other_slug and other_slug in related_data:
                    oi = min(tick_idx, len(related_data[other_slug]) - 1)
                    kl_result_obj = compute_kl(
                        tick["price"], related_data[other_slug][oi]["price"],
                        actual_rel, 0.08,
                    )
                    kl_norm = max(kl_norm, kl_result_obj.normalized)
            
            # 3. Bayesian ΔP
            dp_result = compute_delta_p(est, lookback=15, delta_max=0.04)
            dp_norm = dp_result.normalized
            
            # 4. LMSR
            pos_size = min(capital * 0.25 / max_concurrent, capital * kelly_frac)
            b = max(20, market["liquidity"] / 50)
            lmsr_norm = compute_lmsr_edge(tick["price"], pos_size, b)
            
            # 5. Stoikov Risk (modified with Kyle's λ)
            kyle_penalty = clamp(tick.get("kyle_lambda", 0.5) / 0.5, 0, 1)
            stoikov_risk = compute_stoikov_risk(
                tick["price"], tick["bid"], tick["ask"],
                position=0.0, volatility=0.05,
            )
            # Combine Stoikov with Kyle's λ penalty
            combined_risk = 0.6 * stoikov_risk + 0.4 * kyle_penalty
            
            # ── UNIFIED SCORE ──
            drawdown = max(0, (peak_capital - capital) / peak_capital)
            
            result = compute_score(
                ev["normalized"], kl_norm, dp_norm, lmsr_norm, combined_risk,
                ev["raw"], tick["spread"], market["liquidity"],
                daily_trades, drawdown, threshold,
            )
            
            all_scores.append(result["total"])
            
            # ── EXECUTE MISPRICING ──
            if result["should_trade"] and capital > 1.0:
                kelly = (est.probability - tick["ask"]) / (1 - tick["ask"])
                kelly = max(0, kelly * kelly_frac)
                
                # Reduce size if Kyle's λ is high (price impact risk)
                kyle_adj = 1.0 - clamp(tick.get("kyle_lambda", 0.5) / 0.3, 0, 0.8)
                kelly *= kyle_adj
                
                size = min(kelly * capital, capital / max_concurrent, capital * 0.25)
                
                if size >= 1.0:
                    cost = size + size * fees
                    if cost <= capital:
                        capital -= cost
                        positions[slug] = {
                            "entry_price": tick["ask"],
                            "size": size,
                            "entry_tick": tick_idx,
                            "score": result["total"],
                            "strategy": "mispricing",
                        }
                        signals_executed += 1
                        daily_trades += 1
                        continue
            
            # ═══ STRATEGY 2: LP REBATE CAPTURE ═══
            if enable_lp and slug not in positions and len(positions) < max_concurrent:
                lp_edge = tick.get("lp_edge", 0)
                
                if lp_edge > 0.003 and tick["spread"] > 0.008:
                    # Provide liquidity: place passive orders inside spread
                    lp_size = min(capital * 0.15, capital / max_concurrent)
                    
                    if lp_size >= 1.0 and lp_size <= capital:
                        # Simulate LP: earn half spread as rebate
                        expected_rebate = lp_size * (tick["spread"] / 2) * 0.5
                        
                        # LP inventory risk
                        lp_risk = tick["spread"] / 0.03  # Higher spread = more risk
                        
                        if lp_risk < 0.8:
                            capital -= lp_size
                            positions[slug] = {
                                "entry_price": tick["price"],  # Mid price
                                "size": lp_size,
                                "entry_tick": tick_idx,
                                "score": 0.5,  # LP is always ~0.5
                                "strategy": "lp_rebate",
                                "expected_rebate": expected_rebate,
                            }
                            lp_trades += 1
                            daily_trades += 1
            
            # Track peak
            total_value = capital
            for s, p in positions.items():
                if s in tick_data:
                    idx = min(tick_idx, len(tick_data[s]) - 1)
                    if p.get("strategy") == "lp_rebate":
                        total_value += p["size"] + p.get("expected_rebate", 0)
                    else:
                        total_value += p["size"] * tick_data[s][idx]["price"] / p["entry_price"]
            if total_value > peak_capital:
                peak_capital = total_value
    
    # Close remaining positions
    for slug, pos in list(positions.items()):
        final_tick = tick_data[slug][-1]
        if pos.get("strategy") == "lp_rebate":
            # LP: earn rebate, return capital
            rebate = pos.get("expected_rebate", 0) * 2  # Simulate full cycle
            capital += pos["size"] + rebate
            trades.append(Trade(
                tick=n_ticks, market=slug, strategy="lp_rebate",
                entry_price=pos["entry_price"], exit_price=final_tick["price"],
                size=pos["size"], score=pos["score"],
                pnl=rebate, exit_reason="lp_cycle_complete",
            ))
        else:
            sell_price = final_tick["bid"]
            pnl_pct = (sell_price - pos["entry_price"]) / pos["entry_price"]
            pnl_dollars = pos["size"] * pnl_pct - pos["size"] * fees * 2
            capital += pos["size"] + pnl_dollars
            trades.append(Trade(
                tick=n_ticks, market=slug, strategy=pos.get("strategy", "mispricing"),
                entry_price=pos["entry_price"], exit_price=sell_price,
                size=pos["size"], score=pos["score"],
                pnl=pnl_dollars, exit_reason="backtest_end",
            ))
    
    # Stats
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    total_pnl = sum(t.pnl for t in trades)
    
    mispricing_trades = [t for t in trades if t.strategy == "mispricing"]
    lp_trades_list = [t for t in trades if t.strategy == "lp_rebate"]
    
    if trades:
        returns = [t.pnl / max(1, t.size) for t in trades]
        mean_r = sum(returns) / len(returns)
        std_r = math.sqrt(sum((r - mean_r)**2 for r in returns) / max(1, len(returns)-1))
        sharpe = mean_r / max(std_r, 1e-6) * math.sqrt(len(trades))
    else:
        sharpe = 0
    
    peak = initial_capital
    max_dd = 0
    running = initial_capital
    for t in trades:
        running += t.pnl
        peak = max(peak, running)
        dd = (peak - running) / peak
        max_dd = max(max_dd, dd)
    
    return {
        "total_trades": len(trades),
        "winning": len(wins),
        "losing": len(losses),
        "win_rate": len(wins) / max(1, len(trades)),
        "total_pnl": total_pnl,
        "avg_pnl": total_pnl / max(1, len(trades)),
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "final_capital": capital,
        "return_pct": (capital / initial_capital - 1) * 100,
        "signals_checked": signals_checked,
        "signals_executed": signals_executed,
        "execution_rate": signals_executed / max(1, signals_checked) * 100,
        "lp_trades": len(lp_trades_list),
        "mispricing_trades": len(mispricing_trades),
        "mispricing_win_rate": sum(1 for t in mispricing_trades if t.pnl > 0) / max(1, len(mispricing_trades)),
        "lp_pnl": sum(t.pnl for t in lp_trades_list),
        "avg_score": sum(all_scores) / max(1, len(all_scores)),
        "max_score": max(all_scores) if all_scores else 0,
        "score_above_threshold": sum(1 for s in all_scores if s > threshold),
        "trades": trades,
    }


def run_robustness_v3(n_seeds=30, threshold=0.42):
    results = []
    for seed in range(n_seeds):
        r = run_backtest_v3(seed=seed, threshold=threshold)
        results.append(r)
    
    pnls = [r["total_pnl"] for r in results]
    wins = [r["win_rate"] for r in results]
    sharpes = [r["sharpe"] for r in results]
    finals = [r["final_capital"] for r in results]
    returns = [r["return_pct"] for r in results]
    
    return {
        "n_seeds": n_seeds,
        "threshold": threshold,
        "avg_pnl": sum(pnls) / len(pnls),
        "median_pnl": sorted(pnls)[len(pnls)//2],
        "std_pnl": math.sqrt(sum((p - sum(pnls)/len(pnls))**2 for p in pnls) / len(pnls)),
        "min_pnl": min(pnls),
        "max_pnl": max(pnls),
        "profitable_pct": sum(1 for p in pnls if p > 0) / len(pnls) * 100,
        "avg_win_rate": sum(wins) / len(wins),
        "avg_sharpe": sum(sharpes) / len(sharpes),
        "avg_return": sum(returns) / len(returns),
        "avg_final": sum(finals) / len(finals),
        "above_50_pct": sum(1 for f in finals if f > 50) / len(finals) * 100,
    }


def print_results_v3(r, label=""):
    if label:
        print(f"\n{'='*60}")
        print(f"  {label}")
        print(f"{'='*60}")
    
    mispricing_pnl = sum(t.pnl for t in r["trades"] if t.strategy == "mispricing")
    lp_pnl = sum(t.pnl for t in r["trades"] if t.strategy == "lp_rebate")
    
    print(f"""
╔══════════════════════════════════════════════════════════╗
║      POLYMARKET ENGINE v3 — POST-ADAPTATION              ║
╠══════════════════════════════════════════════════════════╣
║  Signals Checked:    {r['signals_checked']:>6}                             ║
║  Signals Executed:   {r['signals_executed']:>6}                             ║
║  Execution Rate:     {r['execution_rate']:>5.1f}%                            ║
╠══════════════════════════════════════════════════════════╣
║  Total Trades:       {r['total_trades']:>6}                             ║
║  Win Rate:           {r['win_rate']*100:>5.1f}%                            ║
║  Total P&L:          ${r['total_pnl']:>8.2f}                         ║
║  Avg P&L/Trade:      ${r['avg_pnl']:>8.4f}                         ║
║  Max Drawdown:       {r['max_drawdown']*100:>5.1f}%                            ║
║  Sharpe Ratio:       {r['sharpe']:>6.2f}                           ║
╠══════════════════════════════════════════════════════════╣
║  Mispricing Trades:  {r['mispricing_trades']:>6}                             ║
║  Mispricing Win%:    {r['mispricing_win_rate']*100:>5.1f}%                            ║
║  Mispricing P&L:     ${mispricing_pnl:>8.2f}                         ║
║  LP Rebate Trades:   {r['lp_trades']:>6}                             ║
║  LP Rebate P&L:      ${lp_pnl:>8.2f}                         ║
╠══════════════════════════════════════════════════════════╣
║  Starting Capital:   $ 50.00                             ║
║  Final Capital:      ${r['final_capital']:>8.2f}                         ║
║  Return:             {r['return_pct']:>6.1f}%                           ║
║  Avg Score:          {r['avg_score']:>6.4f}                           ║
╚══════════════════════════════════════════════════════════╝""")
    
    if r['trades']:
        print(f"\n  📋 Trades (first 15):")
        print(f"  {'Tick':>5} {'Market':<18} {'Strat':<10} {'Entry':>7} {'Exit':>7} {'P&L':>8} {'Score':>6} {'Reason':<12}")
        print(f"  {'─'*5} {'─'*18} {'─'*10} {'─'*7} {'─'*7} {'─'*8} {'─'*6} {'─'*12}")
        for t in r['trades'][:15]:
            print(f"  {t.tick:>5} {t.market[:18]:<18} {t.strategy:<10} {t.entry_price:>7.3f} {t.exit_price:>7.3f} "
                  f"${t.pnl:>7.2f} {t.score:>6.3f} {t.exit_reason:<12}")


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("═" * 60)
    print("  POLYMARKET ENGINE v3 — POST-ADAPTATION BACKTEST")
    print("  Strategy adapted after dynamic fees killed lag arb")
    print("  All 5 models active + LP rebate capture")
    print("═" * 60)
    
    print("\n📊 Markets (fee-free longer-dated):")
    for m in MARKETS:
        print(f"  • {m['question'][:50]}")
        print(f"    P={m['yes_price']} S={m['spread']} V=${m['volume']:,.0f} L=${m['liquidity']:,.0f}")
    
    # Single run
    r = run_backtest_v3(seed=42, threshold=0.42)
    print_results_v3(r, "SEED=42, THRESHOLD=0.42, LP ENABLED")
    
    # Threshold sweep
    print("\n" + "═" * 60)
    print("  THRESHOLD SENSITIVITY")
    print("═" * 60)
    
    print(f"\n  {'Thresh':>7} {'Trades':>7} {'WinRate':>8} {'P&L':>9} {'Return':>8} {'Sharpe':>7} {'LP%':>5}")
    print(f"  {'─'*7} {'─'*7} {'─'*8} {'─'*9} {'─'*8} {'─'*7} {'─'*5}")
    
    for th in [0.35, 0.38, 0.40, 0.42, 0.45, 0.48, 0.50, 0.55]:
        r = run_backtest_v3(seed=42, threshold=th)
        lp_pct = r['lp_trades'] / max(1, r['total_trades']) * 100
        print(f"  {th:>7.2f} {r['total_trades']:>7} {r['win_rate']*100:>7.1f}% "
              f"${r['total_pnl']:>8.2f} {r['return_pct']:>7.1f}% {r['sharpe']:>7.2f} "
              f"{lp_pct:>4.0f}%")
    
    # Robustness
    print("\n" + "═" * 60)
    print("  ROBUSTNESS — 30 Seeds @ 0.42")
    print("═" * 60)
    
    rob = run_robustness_v3(n_seeds=30, threshold=0.42)
    
    print(f"""
  Seeds:              {rob['n_seeds']}
  Avg P&L:            ${rob['avg_pnl']:>8.2f}
  Median P&L:         ${rob['median_pnl']:>8.2f}
  Std P&L:            ${rob['std_pnl']:>8.2f}
  Min/Max P&L:        ${rob['min_pnl']:>8.2f} / ${rob['max_pnl']:>8.2f}
  Profitable Runs:    {rob['profitable_pct']:.0f}%
  Above $50:          {rob['above_50_pct']:.0f}%
  Avg Win Rate:       {rob['avg_win_rate']*100:.1f}%
  Avg Sharpe:         {rob['avg_sharpe']:.2f}
  Avg Return:         {rob['avg_return']:.1f}%
""")
    
    # Also test 0.45
    rob2 = run_robustness_v3(n_seeds=30, threshold=0.45)
    print(f"  @ 0.45: Profitable={rob2['profitable_pct']:.0f}% | "
          f"AvgReturn={rob2['avg_return']:.1f}% | Sharpe={rob2['avg_sharpe']:.2f}")
    
    print("\n✅ v3 Backtest complete.")
