"""
Backtest v2 — Recalibrated with realistic prediction market dynamics.

Key fixes:
1. Trending price dynamics (news-driven, not GBM mean-reversion)
2. Faster Bayesian convergence
3. Correlated market pairs for KL divergence
4. Better score normalization
5. Lower threshold calibrated to data
"""
import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import sys
sys.path.insert(0, '/root/.openclaw/workspace/polymarket-engine')

from src.models.score import (
    compute_ev, compute_kl, compute_delta_p, compute_stoikov_risk,
    LMSRModel, BayesianEstimator, compute_score, clamp,
)


# ══════════════════════════════════════════════════════════════
# Real Polymarket Crypto Market Data
# ══════════════════════════════════════════════════════════════

REAL_MARKETS = [
    {
        "slug": "btc-150k-jun-2026",
        "question": "Will Bitcoin hit $150k by June 30, 2026?",
        "yes_price": 0.017,
        "spread": 0.002,
        "volume": 3_942_360,
        "liquidity": 50_000,
    },
    {
        "slug": "btc-150k-dec-2026",
        "question": "Will Bitcoin hit $150k by December 31, 2026?",
        "yes_price": 0.095,
        "spread": 0.01,
        "volume": 1_000_000,
        "liquidity": 30_000,
    },
    {
        "slug": "mstr-sell-jun-2026",
        "question": "MicroStrategy sells Bitcoin by June 30, 2026?",
        "yes_price": 0.0275,
        "spread": 0.001,
        "volume": 918_245,
        "liquidity": 65_000,
    },
    {
        "slug": "mstr-sell-dec-2026",
        "question": "MicroStrategy sells Bitcoin by Dec 31, 2026?",
        "yes_price": 0.115,
        "spread": 0.01,
        "volume": 464_956,
        "liquidity": 30_000,
    },
    {
        "slug": "megaeth-1b",
        "question": "MegaETH FDV >$1B after launch?",
        "yes_price": 0.325,
        "spread": 0.01,
        "volume": 2_893_683,
        "liquidity": 40_000,
    },
    {
        "slug": "megaeth-2b",
        "question": "MegaETH FDV >$2B after launch?",
        "yes_price": 0.095,
        "spread": 0.01,
        "volume": 5_820_061,
        "liquidity": 50_000,
    },
    {
        "slug": "megaeth-airdrop",
        "question": "MegaETH airdrop by June 30?",
        "yes_price": 0.4265,
        "spread": 0.009,
        "volume": 1_046_466,
        "liquidity": 35_000,
    },
]


# ══════════════════════════════════════════════════════════════
# Realistic Tick Generator (News-Driven, Trending)
# ══════════════════════════════════════════════════════════════

def generate_realistic_ticks(
    base_price: float,
    n_ticks: int = 1000,
    volatility: float = 0.002,
    spread: float = 0.005,
    trend_strength: float = 0.3,
    jump_prob: float = 0.02,
    jump_size: float = 0.03,
    seed: int = 42,
) -> List[Dict]:
    """
    Generate realistic prediction market ticks.
    
    Key differences from GBM:
    - Trending regime (prices drift in response to "news")
    - Jump diffusion (sudden moves on information)
    - Volatility clustering (big moves follow big moves)
    - Spread widening on volatility
    """
    rng = random.Random(seed)
    ticks = []
    price = base_price
    trend = 0.0  # Current trend direction
    vol_multiplier = 1.0
    consecutive_ups = 0
    consecutive_downs = 0
    
    for i in range(n_ticks):
        # ── Regime detection ──
        # Persistent trends (simulating news-driven moves)
        if rng.random() < 0.05:  # 5% chance of trend change
            trend = rng.gauss(0, trend_strength)
        
        # Trend persistence
        trend *= 0.98  # Slow decay
        
        # ── Jump diffusion ──
        jump = 0.0
        if rng.random() < jump_prob:
            direction = 1 if rng.random() < 0.5 else -1
            jump = direction * jump_size * rng.expovariate(1.0)
            vol_multiplier = 2.0  # Vol spike after jump
        else:
            vol_multiplier *= 0.95  # Vol decay
            vol_multiplier = max(vol_multiplier, 0.5)
        
        # ── Price update ──
        noise = rng.gauss(0, volatility * vol_multiplier)
        price_change = trend + noise + jump
        old_price = price
        price = clamp(price + price_change, 0.001, 0.999)
        actual_change = price - old_price
        
        # ── Trend tracking (momentum) ──
        if actual_change > 0:
            consecutive_ups += 1
            consecutive_downs = 0
        elif actual_change < 0:
            consecutive_downs += 1
            consecutive_ups = 0
        
        # Momentum: if 3+ consecutive moves in same direction, strengthen trend
        if consecutive_ups >= 3:
            trend += 0.001
        elif consecutive_downs >= 3:
            trend -= 0.001
        
        # ── Spread dynamics ──
        current_spread = spread * (1 + vol_multiplier * 0.5)
        current_spread = max(0.001, min(current_spread, 0.05))
        
        # ── Volume ──
        base_vol = rng.expovariate(1.0)
        if abs(actual_change) > volatility * 2:
            base_vol *= 4.0  # Volume spike
        if abs(jump) > 0:
            base_vol *= 8.0  # Big volume on jumps
        
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
        })
    
    return ticks


def generate_correlated_ticks(
    base_ticks: List[Dict],
    base_price_ratio: float,
    correlation: float = 0.7,
    noise_scale: float = 0.002,
    spread: float = 0.005,
    seed: int = 99,
) -> List[Dict]:
    """
    Generate ticks for a related market, correlated with the base.
    Used for KL divergence testing.
    """
    rng = random.Random(seed)
    ticks = []
    
    for bt in base_ticks:
        # Correlated movement
        correlated_move = bt["price_change"] * correlation
        independent_noise = rng.gauss(0, noise_scale)
        
        # Base price follows the same trends but at a different level
        if not ticks:
            price = bt["price"] * base_price_ratio
        else:
            price = clamp(ticks[-1]["price"] + correlated_move + independent_noise, 0.001, 0.999)
        
        current_spread = spread * (1 + abs(bt.get("jump", 0)) * 20)
        
        ticks.append({
            "tick": bt["tick"],
            "price": price,
            "bid": price - current_spread / 2,
            "ask": price + current_spread / 2,
            "spread": current_spread,
            "volume": bt["volume"] * 0.8,
            "price_change": correlated_move + independent_noise,
        })
    
    return ticks


# ══════════════════════════════════════════════════════════════
# Market Relationships
# ══════════════════════════════════════════════════════════════

RELATIONSHIPS = [
    # (primary, related, type, price_ratio for generating correlated ticks)
    ("btc-150k-jun-2026", "btc-150k-dec-2026", "subset", 5.5),
    ("mstr-sell-jun-2026", "mstr-sell-dec-2026", "subset", 4.2),
    ("megaeth-2b", "megaeth-1b", "subset", 3.4),
]


# ══════════════════════════════════════════════════════════════
# Backtest v2
# ══════════════════════════════════════════════════════════════

@dataclass
class Trade:
    tick: int
    market: str
    entry_price: float
    exit_price: float
    size: float
    score: float
    pnl: float
    exit_reason: str


def run_backtest_v2(
    markets: List[Dict] = REAL_MARKETS,
    initial_capital: float = 50.0,
    n_ticks: int = 1000,
    threshold: float = 0.55,
    max_concurrent: int = 3,
    kelly_frac: float = 0.25,
    profit_target: float = 0.04,
    stop_loss: float = 0.08,
    fees: float = 0.005,
    seed: int = 42,
) -> Dict:
    """Run calibrated backtest v2."""
    
    market_map = {m["slug"]: m for m in markets}
    
    # Generate tick data
    tick_data: Dict[str, List[Dict]] = {}
    estimators: Dict[str, BayesianEstimator] = {}
    lmsr = LMSRModel()
    
    # Primary markets
    for m in markets:
        m_seed = seed + hash(m["slug"]) % 10000
        vol = 0.001 + (1 - m["yes_price"]) * 0.002
        
        tick_data[m["slug"]] = generate_realistic_ticks(
            base_price=m["yes_price"],
            n_ticks=n_ticks,
            volatility=vol,
            spread=m["spread"],
            trend_strength=0.4,
            jump_prob=0.015,
            jump_size=0.02,
            seed=m_seed,
        )
        
        estimators[m["slug"]] = BayesianEstimator(
            prior=m["yes_price"],
            strength=20.0,  # Lower = faster adaptation
        )
    
    # Generate correlated ticks for related markets
    related_ticks: Dict[str, List[Dict]] = {}
    for primary_slug, related_slug, rel_type, ratio in RELATIONSHIPS:
        if primary_slug in tick_data:
            m_seed = seed + hash(related_slug) % 10000
            related_ticks[related_slug] = generate_correlated_ticks(
                tick_data[primary_slug],
                base_price_ratio=ratio,
                correlation=0.6,
                noise_scale=0.001,
                spread=market_map.get(related_slug, {}).get("spread", 0.005),
                seed=m_seed,
            )
    
    # ── Backtest State ──
    capital = initial_capital
    peak_capital = initial_capital
    positions: Dict[str, Dict] = {}
    trades: List[Trade] = []
    all_scores: List[float] = []
    signals_checked = 0
    signals_executed = 0
    daily_trades = 0
    
    # ── Main Loop ──
    for tick_idx in range(n_ticks):
        for slug, ticks in tick_data.items():
            if tick_idx >= len(ticks):
                continue
            
            tick = ticks[tick_idx]
            market = market_map[slug]
            estimator = estimators[slug]
            
            # Update Bayesian
            estimator.update(
                price_move=tick["price_change"],
                volume_ratio=tick["volume"],
                trade_side=1 if tick["price_change"] > 0 else -1,
            )
            
            # ── Exit Check ──
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
                elif tick_idx - pos["entry_tick"] > 200:
                    should_exit, exit_reason = True, "time_decay"
                
                if should_exit:
                    # Sell at bid (market sell)
                    sell_price = tick["bid"]
                    pnl_pct_actual = (sell_price - entry) / entry
                    pnl_dollars = pos["size"] * pnl_pct_actual - pos["size"] * fees * 2
                    capital += pos["size"] + pnl_dollars
                    
                    trades.append(Trade(
                        tick=tick_idx, market=slug,
                        entry_price=entry, exit_price=sell_price,
                        size=pos["size"], score=pos["score"],
                        pnl=pnl_dollars, exit_reason=exit_reason,
                    ))
                    del positions[slug]
                continue
            
            # ── Skip if max concurrent ──
            if len(positions) >= max_concurrent:
                continue
            
            signals_checked += 1
            
            # ══════════════════════════════════════════
            # 1. EV Component (Weight: 0.35)
            # ══════════════════════════════════════════
            true_prob = estimator.probability
            ev_result = compute_ev(
                true_prob=true_prob,
                entry_price=tick["ask"],
                fees=fees,
                min_edge=0.02,  # Lowered from 0.03
                max_edge=0.15,
            )
            
            # ══════════════════════════════════════════
            # 2. KL Divergence Component (Weight: 0.20)
            # ══════════════════════════════════════════
            kl_norm = 0.0
            for primary_slug, related_slug, rel_type, _ in RELATIONSHIPS:
                other_slug = None
                if slug == primary_slug:
                    other_slug = related_slug
                elif slug == related_slug:
                    other_slug = primary_slug
                    rel_type = "superset"
                
                if other_slug and other_slug in related_ticks:
                    other_tick_idx = min(tick_idx, len(related_ticks[other_slug]) - 1)
                    other_price = related_ticks[other_slug][other_tick_idx]["price"]
                    
                    kl_result = compute_kl(
                        market_price=tick["price"],
                        related_price=other_price,
                        relationship=rel_type,
                        threshold=0.08,
                    )
                    kl_norm = max(kl_norm, kl_result.normalized)
            
            # ══════════════════════════════════════════
            # 3. Bayesian DeltaP Component (Weight: 0.20)
            # ══════════════════════════════════════════
            bayesian_result = compute_delta_p(
                estimator, lookback=15, delta_max=0.04
            )
            
            # ══════════════════════════════════════════
            # 4. LMSR Edge Component (Weight: 0.15)
            # ══════════════════════════════════════════
            position_size = min(
                capital * 0.25 / max_concurrent,
                capital * kelly_frac,
            )
            
            lmsr_b = max(20.0, market["liquidity"] / 50)
            lmsr_result = lmsr.compute_impact(
                current_price=tick["price"],
                trade_size=position_size,
                b=lmsr_b,
            )
            
            # ══════════════════════════════════════════
            # 5. Stoikov Risk Component (Weight: -0.10)
            # ══════════════════════════════════════════
            stoikov_result = compute_stoikov_risk(
                mid_price=tick["price"],
                best_bid=tick["bid"],
                best_ask=tick["ask"],
                position=0.0,
                volatility=0.05,
                max_spread=0.04,
            )
            
            # ══════════════════════════════════════════
            # UNIFIED SCORE
            # ══════════════════════════════════════════
            drawdown = max(0, (peak_capital - capital) / peak_capital)
            
            score_result = compute_score(
                ev_norm=ev_result.normalized,
                kl_norm=kl_norm,
                delta_p_norm=bayesian_result.normalized,
                lmsr_norm=lmsr_result.normalized,
                risk_norm=stoikov_result.normalized,
                ev_raw=ev_result.raw,
                spread=tick["spread"],
                liquidity=market["liquidity"],
                market_age_seconds=10,
                daily_trades=daily_trades,
                drawdown=drawdown,
                threshold=threshold,
            )
            
            all_scores.append(score_result.total)
            
            # ── Execute ──
            if score_result.should_trade and capital > 1.0:
                kelly = (true_prob - tick["ask"]) / (1 - tick["ask"])
                kelly = max(0, kelly * kelly_frac)
                
                size = min(
                    kelly * capital,
                    capital / max_concurrent,
                    capital * 0.25,
                )
                
                if size >= 1.0:
                    cost = size + size * fees
                    if cost <= capital:
                        capital -= cost
                        
                        positions[slug] = {
                            "entry_price": tick["ask"],
                            "size": size,
                            "entry_tick": tick_idx,
                            "score": score_result.total,
                        }
                        
                        signals_executed += 1
                        daily_trades += 1
            
            # Track peak
            total_value = capital
            for s, p in positions.items():
                if s in tick_data:
                    idx = min(tick_idx, len(tick_data[s]) - 1)
                    total_value += p["size"] * tick_data[s][idx]["price"] / p["entry_price"]
            if total_value > peak_capital:
                peak_capital = total_value
    
    # Close remaining
    for slug, pos in list(positions.items()):
        final_tick = tick_data[slug][-1]
        sell_price = final_tick["bid"]
        pnl_pct = (sell_price - pos["entry_price"]) / pos["entry_price"]
        pnl_dollars = pos["size"] * pnl_pct - pos["size"] * fees * 2
        capital += pos["size"] + pnl_dollars
        trades.append(Trade(
            tick=n_ticks, market=slug,
            entry_price=pos["entry_price"], exit_price=sell_price,
            size=pos["size"], score=pos["score"],
            pnl=pnl_dollars, exit_reason="backtest_end",
        ))
    
    # Stats
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    total_pnl = sum(t.pnl for t in trades)
    
    if trades:
        returns = [t.pnl / max(1, t.size) for t in trades]
        mean_r = sum(returns) / len(returns)
        std_r = math.sqrt(sum((r - mean_r)**2 for r in returns) / max(1, len(returns)-1))
        sharpe = mean_r / max(std_r, 1e-6) * math.sqrt(len(trades))
    else:
        sharpe = 0
    
    # Max drawdown
    peak = initial_capital
    max_dd = 0
    running = initial_capital
    for t in trades:
        running += t.pnl
        peak = max(peak, running)
        dd = (peak - running) / peak
        max_dd = max(max_dd, dd)
    
    # Score distribution
    score_above_threshold = sum(1 for s in all_scores if s > threshold)
    
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
        "score_pass_rate": score_above_threshold / max(1, len(all_scores)) * 100,
        "avg_score": sum(all_scores) / max(1, len(all_scores)),
        "max_score": max(all_scores) if all_scores else 0,
        "trades": trades,
        "all_scores": all_scores,
    }


# ══════════════════════════════════════════════════════════════
# Multi-Run Analysis
# ══════════════════════════════════════════════════════════════

def print_results(r: Dict, label: str = ""):
    """Pretty-print backtest results."""
    if label:
        print(f"\n{'='*60}")
        print(f"  {label}")
        print(f"{'='*60}")
    
    print(f"""
╔══════════════════════════════════════════════════════════╗
║           UNIFIED SCORE ENGINE — BACKTEST v2             ║
╠══════════════════════════════════════════════════════════╣
║  Signals Checked:    {r['signals_checked']:>6}                             ║
║  Signals Executed:   {r['signals_executed']:>6}                             ║
║  Execution Rate:     {r['execution_rate']:>5.1f}%                            ║
║  Score Pass Rate:    {r['score_pass_rate']:>5.1f}%                            ║
╠══════════════════════════════════════════════════════════╣
║  Total Trades:       {r['total_trades']:>6}                             ║
║  Win Rate:           {r['win_rate']*100:>5.1f}%                            ║
║  Total P&L:          ${r['total_pnl']:>8.2f}                         ║
║  Avg P&L/Trade:      ${r['avg_pnl']:>8.4f}                         ║
║  Max Drawdown:       {r['max_drawdown']*100:>5.1f}%                            ║
║  Sharpe Ratio:       {r['sharpe']:>6.2f}                           ║
╠══════════════════════════════════════════════════════════╣
║  Starting Capital:   $ 50.00                             ║
║  Final Capital:      ${r['final_capital']:>8.2f}                         ║
║  Return:             {r['return_pct']:>6.1f}%                           ║
║  Avg Score:          {r['avg_score']:>6.4f}                           ║
║  Max Score:          {r['max_score']:>6.4f}                           ║
╚══════════════════════════════════════════════════════════╝""")
    
    if r['trades']:
        print(f"\n  📋 Trades:")
        print(f"  {'Tick':>5} {'Market':<22} {'Entry':>7} {'Exit':>7} {'P&L':>8} {'Score':>6} {'Reason':<12}")
        print(f"  {'─'*5} {'─'*22} {'─'*7} {'─'*7} {'─'*8} {'─'*6} {'─'*12}")
        for t in r['trades'][:15]:
            print(f"  {t.tick:>5} {t.market[:22]:<22} {t.entry_price:>7.3f} {t.exit_price:>7.3f} "
                  f"${t.pnl:>7.2f} {t.score:>6.3f} {t.exit_reason:<12}")


def run_robustness(n_seeds: int = 30, threshold: float = 0.55) -> Dict:
    """Multi-seed robustness test."""
    results = []
    for seed in range(n_seeds):
        r = run_backtest_v2(seed=seed, threshold=threshold)
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
    }


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("═" * 60)
    print("  POLYMARKET UNIFIED SCORE ENGINE — BACKTEST v2")
    print("  Real crypto market data + realistic tick simulation")
    print("═" * 60)
    
    print("\n📊 Markets:")
    for m in REAL_MARKETS:
        print(f"  • {m['question'][:55]}")
        print(f"    Price: {m['yes_price']} | Spread: {m['spread']} | Vol: ${m['volume']:,.0f}")
    
    # Single run
    r = run_backtest_v2(seed=42, threshold=0.55)
    print_results(r, "SEED=42, THRESHOLD=0.55")
    
    # Threshold sweep
    print("\n" + "═" * 60)
    print("  THRESHOLD SENSITIVITY")
    print("═" * 60)
    
    print(f"\n  {'Thresh':>7} {'Trades':>7} {'WinRate':>8} {'P&L':>9} {'Return':>8} {'Sharpe':>7} {'Pass%':>6}")
    print(f"  {'─'*7} {'─'*7} {'─'*8} {'─'*9} {'─'*8} {'─'*7} {'─'*6}")
    
    for th in [0.40, 0.45, 0.50, 0.52, 0.55, 0.58, 0.60, 0.65]:
        r = run_backtest_v2(seed=42, threshold=th)
        print(f"  {th:>7.2f} {r['total_trades']:>7} {r['win_rate']*100:>7.1f}% "
              f"${r['total_pnl']:>8.2f} {r['return_pct']:>7.1f}% {r['sharpe']:>7.2f} "
              f"{r['score_pass_rate']:>5.1f}%")
    
    # Robustness
    print("\n" + "═" * 60)
    print("  ROBUSTNESS — 30 Seeds @ Threshold=0.55")
    print("═" * 60)
    
    rob = run_robustness(n_seeds=30, threshold=0.55)
    
    print(f"""
  Seeds:              {rob['n_seeds']}
  Threshold:          {rob['threshold']}
  Avg P&L:            ${rob['avg_pnl']:>8.2f}
  Median P&L:         ${rob['median_pnl']:>8.2f}
  Std P&L:            ${rob['std_pnl']:>8.2f}
  Min P&L:            ${rob['min_pnl']:>8.2f}
  Max P&L:            ${rob['max_pnl']:>8.2f}
  Profitable Runs:    {rob['profitable_pct']:.0f}%
  Avg Win Rate:       {rob['avg_win_rate']*100:.1f}%
  Avg Sharpe:         {rob['avg_sharpe']:.2f}
  Avg Return:         {rob['avg_return']:.1f}%
  Avg Final Capital:  ${rob['avg_final']:.2f}
""")
    
    # Also test at 0.50
    print("═" * 60)
    print("  ROBUSTNESS — 30 Seeds @ Threshold=0.50")
    print("═" * 60)
    
    rob2 = run_robustness(n_seeds=30, threshold=0.50)
    print(f"""
  Profitable Runs:    {rob2['profitable_pct']:.0f}%
  Avg P&L:            ${rob2['avg_pnl']:>8.2f}
  Avg Win Rate:       {rob2['avg_win_rate']*100:.1f}%
  Avg Sharpe:         {rob2['avg_sharpe']:.2f}
  Avg Return:         {rob2['avg_return']:.1f}%
""")
    
    print("✅ Backtest v2 complete.")
