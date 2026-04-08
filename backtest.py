"""
Backtesting Engine — Tests the unified Score formula against real Polymarket data.

Uses live market snapshots + simulated tick data based on real market microstructure.
"""
import math
import random
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import sys
sys.path.insert(0, '/root/.openclaw/workspace/polymarket-engine')

from src.models.score import (
    compute_ev, compute_kl, compute_delta_p, compute_stoikov_risk,
    LMSRModel, BayesianEstimator, compute_score, ScoreResult,
    clamp, check_complement_arb,
)


# ══════════════════════════════════════════════════════════════
# Real Polymarket Data (live snapshots from API)
# ══════════════════════════════════════════════════════════════

REAL_MARKETS = [
    {
        "slug": "will-bitcoin-hit-150k-by-june-30-2026",
        "question": "Will Bitcoin hit $150k by June 30, 2026?",
        "yes_price": 0.017,
        "no_price": 0.983,
        "spread": 0.002,
        "volume": 3_942_360,
        "liquidity": 50_000,
        "condition_id": "0xa0f4c4924ea1a8b410b4ce821c2a9955fad21a1b19bdcfde90816732278b3dd5",
        "yes_token": "13915689317269078219168496739008737517740566192006337297676041270492637394586",
        "no_token": "13290642914521189871602119663452054126359842904805799115978921503195267156991",
    },
    {
        "slug": "will-bitcoin-hit-150k-by-dec-31-2026",
        "question": "Will Bitcoin hit $150k by December 31, 2026?",
        "yes_price": 0.095,
        "no_price": 0.905,
        "spread": 0.01,
        "volume": 1_000_000,
        "liquidity": 30_000,
        "condition_id": "0x02deb9538f5c123373adaa4ee6217b01745f1662bc902e46ac92f3fe6f8741e8",
        "yes_token": "93694900555669388759405753550770573998169287228984912881955464376232163096213",
        "no_token": "55119388124180116303253993098894090042427725500010038140578121972388485050538",
    },
    {
        "slug": "microstrategy-sell-bitcoin-jun-2026",
        "question": "MicroStrategy sells any Bitcoin by June 30, 2026?",
        "yes_price": 0.0275,
        "no_price": 0.9725,
        "spread": 0.001,
        "volume": 918_245,
        "liquidity": 65_000,
        "condition_id": "",
        "yes_token": "",
        "no_token": "",
    },
    {
        "slug": "microstrategy-sell-bitcoin-dec-2026",
        "question": "MicroStrategy sells any Bitcoin by December 31, 2026?",
        "yes_price": 0.115,
        "no_price": 0.885,
        "spread": 0.01,
        "volume": 464_956,
        "liquidity": 30_000,
        "condition_id": "",
        "yes_token": "",
        "no_token": "",
    },
    {
        "slug": "megaeth-fdv-1b",
        "question": "MegaETH market cap (FDV) >$1B one day after launch?",
        "yes_price": 0.325,
        "no_price": 0.675,
        "spread": 0.01,
        "volume": 2_893_683,
        "liquidity": 40_000,
        "condition_id": "",
        "yes_token": "",
        "no_token": "",
    },
    {
        "slug": "megaeth-fdv-2b",
        "question": "MegaETH market cap (FDV) >$2B one day after launch?",
        "yes_price": 0.095,
        "no_price": 0.905,
        "spread": 0.01,
        "volume": 5_820_061,
        "liquidity": 50_000,
        "condition_id": "",
        "yes_token": "",
        "no_token": "",
    },
    {
        "slug": "megaeth-airdrop-jun-2026",
        "question": "Will MegaETH perform an airdrop by June 30?",
        "yes_price": 0.4265,
        "no_price": 0.5735,
        "spread": 0.009,
        "volume": 1_046_466,
        "liquidity": 35_000,
        "condition_id": "",
        "yes_token": "",
        "no_token": "",
    },
    {
        "slug": "trump-crypto-tax-2027",
        "question": "Trump eliminates capital gains tax on crypto before 2027?",
        "yes_price": 0.0445,
        "no_price": 0.9555,
        "spread": 0.025,
        "volume": 19_964,
        "liquidity": 8_000,
        "condition_id": "",
        "yes_token": "",
        "no_token": "",
    },
]


# ══════════════════════════════════════════════════════════════
# Simulated Tick Generator
# ══════════════════════════════════════════════════════════════

def generate_ticks(
    base_price: float,
    n_ticks: int = 500,
    volatility: float = 0.003,
    drift: float = 0.0,
    spread: float = 0.005,
    seed: int = 42,
) -> List[Dict]:
    """
    Generate realistic tick data based on market microstructure.
    
    Models:
    - Geometric Brownian Motion for price
    - Mean-reverting spread
    - Volume correlated with volatility
    """
    rng = random.Random(seed)
    ticks = []
    price = base_price
    
    for i in range(n_ticks):
        # Price follows GBM with slight drift
        shock = rng.gauss(0, 1)
        price_change = price * (drift * 0.001 + volatility * shock)
        price = clamp(price + price_change, 0.001, 0.999)
        
        # Spread varies around base
        current_spread = spread * (1 + 0.3 * rng.gauss(0, 1))
        current_spread = max(0.001, current_spread)
        
        # Volume spikes (Poisson-like)
        volume = rng.expovariate(1.0)
        if abs(price_change) > volatility * 1.5:
            volume *= 3.0  # Volume spike on big moves
        
        ticks.append({
            "tick": i,
            "price": price,
            "bid": price - current_spread / 2,
            "ask": price + current_spread / 2,
            "spread": current_spread,
            "volume": volume,
            "price_change": price_change,
        })
    
    return ticks


# ══════════════════════════════════════════════════════════════
# Market Pair Relationships
# ══════════════════════════════════════════════════════════════

MARKET_RELATIONSHIPS = [
    # (market_a_slug, market_b_slug, relationship)
    # "subset" = A outcome implies B outcome
    ("will-bitcoin-hit-150k-by-june-30-2026", "will-bitcoin-hit-150k-by-dec-31-2026", "subset"),
    ("microstrategy-sell-bitcoin-jun-2026", "microstrategy-sell-bitcoin-dec-2026", "subset"),
    # MegaETH FDV is hierarchical: >2B implies >1B
    ("megaeth-fdv-2b", "megaeth-fdv-1b", "subset"),
]


# ══════════════════════════════════════════════════════════════
# Backtest Engine
# ══════════════════════════════════════════════════════════════

@dataclass
class BacktestTrade:
    tick: int
    market: str
    side: str
    entry_price: float
    exit_price: float
    size: float
    score: float
    ev_raw: float
    pnl: float
    held_ticks: int
    exit_reason: str


@dataclass
class BacktestResult:
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: float
    avg_pnl: float
    max_drawdown: float
    sharpe_ratio: float
    final_capital: float
    trades: List[BacktestTrade]
    score_distribution: List[float]
    signals_checked: int
    signals_executed: int
    
    def summary(self) -> str:
        return f"""
╔══════════════════════════════════════════════════════════════╗
║              BACKTEST RESULTS — UNIFIED SCORE ENGINE         ║
╠══════════════════════════════════════════════════════════════╣
║  Markets Tested:      {len(set(t.market for t in self.trades)):>6}                              ║
║  Signals Checked:     {self.signals_checked:>6}                              ║
║  Signals Executed:    {self.signals_executed:>6}                              ║
║  Execution Rate:      {self.signals_executed/max(1,self.signals_checked)*100:>5.1f}%                             ║
╠══════════════════════════════════════════════════════════════╣
║  Total Trades:        {self.total_trades:>6}                              ║
║  Winning Trades:      {self.winning_trades:>6}                              ║
║  Losing Trades:       {self.losing_trades:>6}                              ║
║  Win Rate:            {self.win_rate*100:>5.1f}%                             ║
╠══════════════════════════════════════════════════════════════╣
║  Total P&L:           ${self.total_pnl:>8.2f}                          ║
║  Avg P&L/Trade:       ${self.avg_pnl:>8.4f}                          ║
║  Max Drawdown:        {self.max_drawdown*100:>5.1f}%                             ║
║  Sharpe Ratio:        {self.sharpe_ratio:>6.2f}                            ║
╠══════════════════════════════════════════════════════════════╣
║  Starting Capital:    $ 50.00                                ║
║  Final Capital:       ${self.final_capital:>8.2f}                          ║
║  Return:              {(self.final_capital/50.0 - 1)*100:>6.1f}%                            ║
╚══════════════════════════════════════════════════════════════╝
"""


def run_backtest(
    markets: List[Dict] = REAL_MARKETS,
    initial_capital: float = 50.0,
    n_ticks: int = 500,
    score_threshold: float = 0.65,
    max_concurrent: int = 3,
    kelly_fraction: float = 0.25,
    profit_target: float = 0.05,
    stop_loss: float = 0.10,
    fees: float = 0.005,
    seed: int = 42,
) -> BacktestResult:
    """
    Run full backtest across all markets with the unified Score engine.
    """
    # Build market lookup
    market_map = {m["slug"]: m for m in markets}
    
    # Generate tick data for each market
    tick_data: Dict[str, List[Dict]] = {}
    estimators: Dict[str, BayesianEstimator] = {}
    lmsr_models: Dict[str, LMSRModel] = {}
    price_histories: Dict[str, List[float]] = {}
    
    for m in markets:
        # Different seeds per market for independence
        m_seed = seed + hash(m["slug"]) % 10000
        
        # Higher volatility for lower-priced markets (empirically true)
        vol = 0.002 + (1 - m["yes_price"]) * 0.003
        
        # Slight drift based on price level (mean-reverting for extremes)
        drift = (0.5 - m["yes_price"]) * 0.1  # Mean-reverting
        
        tick_data[m["slug"]] = generate_ticks(
            base_price=m["yes_price"],
            n_ticks=n_ticks,
            volatility=vol,
            drift=drift,
            spread=m["spread"],
            seed=m_seed,
        )
        
        estimators[m["slug"]] = BayesianEstimator(
            prior=m["yes_price"],
            strength=50.0,
        )
        
        lmsr_models[m["slug"]] = LMSRModel(
            b=max(10.0, m["liquidity"] / 100)
        )
        
        price_histories[m["slug"]] = [m["yes_price"]]
    
    # ── Backtest Loop ──
    capital = initial_capital
    peak_capital = initial_capital
    positions: Dict[str, Dict] = {}  # slug -> {entry_price, size, entry_tick}
    trades: List[BacktestTrade] = []
    score_distribution: List[float] = []
    signals_checked = 0
    signals_executed = 0
    daily_trades = 0
    
    for tick_idx in range(n_ticks):
        for slug, ticks in tick_data.items():
            if tick_idx >= len(ticks):
                continue
            
            tick = ticks[tick_idx]
            market = market_map[slug]
            estimator = estimators[slug]
            lmsr = lmsr_models[slug]
            price_hist = price_histories[slug]
            
            # Update Bayesian estimator
            estimator.update(
                price_move=tick["price_change"],
                volume_ratio=tick["volume"],
                trade_side=1 if tick["price_change"] > 0 else -1,
            )
            
            price_hist.append(tick["price"])
            
            # ── Check Exit Conditions for Open Positions ──
            if slug in positions:
                pos = positions[slug]
                entry = pos["entry_price"]
                current = tick["price"]
                pnl_pct = (current - entry) / entry if entry > 0 else 0
                
                should_exit = False
                exit_reason = ""
                
                if pnl_pct >= profit_target:
                    should_exit = True
                    exit_reason = "profit_target"
                elif pnl_pct <= -stop_loss:
                    should_exit = True
                    exit_reason = "stop_loss"
                elif tick_idx - pos["entry_tick"] > 100:
                    should_exit = True
                    exit_reason = "time_decay"
                
                if should_exit:
                    pnl_dollars = pos["size"] * pnl_pct - pos["size"] * fees * 2
                    capital += pos["size"] + pnl_dollars
                    
                    trades.append(BacktestTrade(
                        tick=tick_idx,
                        market=slug,
                        side="BUY",
                        entry_price=entry,
                        exit_price=current,
                        size=pos["size"],
                        score=pos["score"],
                        ev_raw=pos["ev_raw"],
                        pnl=pnl_dollars,
                        held_ticks=tick_idx - pos["entry_tick"],
                        exit_reason=exit_reason,
                    ))
                    
                    del positions[slug]
                    continue
            
            # ── Compute Signal ──
            if slug in positions:
                continue  # Already in position
            
            signals_checked += 1
            
            # 1. EV
            ev_result = compute_ev(
                true_prob=estimator.probability,
                entry_price=tick["ask"],
                fees=fees,
            )
            
            # 2. KL (check related markets)
            kl_norm = 0.0
            for a_slug, b_slug, rel in MARKET_RELATIONSHIPS:
                if slug == a_slug and b_slug in estimators:
                    kl_result = compute_kl(
                        market_price=tick["price"],
                        related_price=tick_data[b_slug][min(tick_idx, len(tick_data[b_slug])-1)]["price"],
                        relationship=rel,
                    )
                    kl_norm = max(kl_norm, kl_result.normalized)
                elif slug == b_slug and a_slug in estimators:
                    kl_result = compute_kl(
                        market_price=tick["price"],
                        related_price=tick_data[a_slug][min(tick_idx, len(tick_data[a_slug])-1)]["price"],
                        relationship="superset" if rel == "subset" else rel,
                    )
                    kl_norm = max(kl_norm, kl_result.normalized)
            
            # 3. Bayesian DeltaP
            bayesian_result = compute_delta_p(estimator, lookback=20)
            
            # 4. LMSR Edge
            position_size = min(
                capital * 0.25 / max_concurrent,
                capital * kelly_fraction,
            )
            lmsr_result = lmsr.compute_impact(
                current_price=tick["price"],
                trade_size=position_size,
                b=lmsr.estimate_b_from_spread(tick["spread"], tick["price"]),
            )
            
            # 5. Stoikov Risk
            stoikov_result = compute_stoikov_risk(
                mid_price=tick["price"],
                best_bid=tick["bid"],
                best_ask=tick["ask"],
                position=0.0,
                volatility=0.05,
            )
            
            # ── UNIFIED SCORE ──
            score_result = compute_score(
                ev_norm=ev_result.normalized,
                kl_norm=kl_norm,
                delta_p_norm=bayesian_result.normalized,
                lmsr_norm=lmsr_result.normalized,
                risk_norm=stoikov_result.normalized,
                ev_raw=ev_result.raw,
                spread=tick["spread"],
                liquidity=market["liquidity"],
                market_age_seconds=10,  # Active market
                daily_trades=daily_trades,
                drawdown=max(0, (peak_capital - capital) / peak_capital),
                threshold=score_threshold,
            )
            
            score_distribution.append(score_result.total)
            
            # ── Execute if Score > Threshold ──
            if score_result.should_trade and capital > 1.0:
                # Kelly sizing
                kelly = (estimator.probability - tick["ask"]) / (1 - tick["ask"])
                kelly = max(0, kelly * kelly_fraction)
                
                size = min(
                    kelly * capital,
                    capital / max_concurrent,
                    capital * 0.25,
                )
                
                if size >= 1.0:
                    # Deduct from capital (with fees)
                    cost = size + size * fees
                    if cost <= capital:
                        capital -= cost
                        
                        positions[slug] = {
                            "entry_price": tick["ask"],
                            "size": size,
                            "entry_tick": tick_idx,
                            "score": score_result.total,
                            "ev_raw": ev_result.raw,
                        }
                        
                        signals_executed += 1
                        daily_trades += 1
            
            # Track peak
            total_value = capital + sum(
                p["size"] * tick_data[s][min(tick_idx, len(tick_data[s])-1)]["price"] / p["entry_price"]
                for s, p in positions.items()
            )
            if total_value > peak_capital:
                peak_capital = total_value
    
    # Close remaining positions
    for slug, pos in list(positions.items()):
        final_tick = tick_data[slug][-1]
        pnl_pct = (final_tick["price"] - pos["entry_price"]) / pos["entry_price"]
        pnl_dollars = pos["size"] * pnl_pct - pos["size"] * fees * 2
        capital += pos["size"] + pnl_dollars
        
        trades.append(BacktestTrade(
            tick=n_ticks,
            market=slug,
            side="BUY",
            entry_price=pos["entry_price"],
            exit_price=final_tick["price"],
            size=pos["size"],
            score=pos["score"],
            ev_raw=pos["ev_raw"],
            pnl=pnl_dollars,
            held_ticks=n_ticks - pos["entry_tick"],
            exit_reason="backtest_end",
        ))
    
    # ── Compute Statistics ──
    winning = [t for t in trades if t.pnl > 0]
    losing = [t for t in trades if t.pnl <= 0]
    
    total_pnl = sum(t.pnl for t in trades)
    win_rate = len(winning) / max(1, len(trades))
    avg_pnl = total_pnl / max(1, len(trades))
    
    # Max drawdown
    peak = initial_capital
    max_dd = 0
    running_capital = initial_capital
    for t in trades:
        running_capital += t.pnl
        if running_capital > peak:
            peak = running_capital
        dd = (peak - running_capital) / peak
        if dd > max_dd:
            max_dd = dd
    
    # Sharpe ratio (simplified)
    if trades:
        returns = [t.pnl / max(1, t.size) for t in trades]
        mean_ret = sum(returns) / len(returns)
        std_ret = math.sqrt(sum((r - mean_ret)**2 for r in returns) / max(1, len(returns)-1))
        sharpe = mean_ret / max(std_ret, 1e-6) * math.sqrt(max(1, len(trades)))
    else:
        sharpe = 0.0
    
    return BacktestResult(
        total_trades=len(trades),
        winning_trades=len(winning),
        losing_trades=len(losing),
        win_rate=win_rate,
        total_pnl=total_pnl,
        avg_pnl=avg_pnl,
        max_drawdown=max_dd,
        sharpe_ratio=sharpe,
        final_capital=capital,
        trades=trades,
        score_distribution=score_distribution,
        signals_checked=signals_checked,
        signals_executed=signals_executed,
    )


# ══════════════════════════════════════════════════════════════
# Multi-Seed Robustness Test
# ══════════════════════════════════════════════════════════════

def run_robustness_test(
    n_seeds: int = 20,
    threshold: float = 0.65,
) -> Dict:
    """Run backtest across multiple seeds to validate robustness."""
    results = []
    
    for seed in range(n_seeds):
        result = run_backtest(seed=seed, score_threshold=threshold)
        results.append({
            "seed": seed,
            "total_trades": result.total_trades,
            "win_rate": result.win_rate,
            "total_pnl": result.total_pnl,
            "sharpe": result.sharpe_ratio,
            "max_dd": result.max_drawdown,
            "final_capital": result.final_capital,
        })
    
    # Aggregate stats
    pnls = [r["total_pnl"] for r in results]
    win_rates = [r["win_rate"] for r in results]
    sharpes = [r["sharpe"] for r in results]
    finals = [r["final_capital"] for r in results]
    
    return {
        "n_seeds": n_seeds,
        "threshold": threshold,
        "avg_pnl": sum(pnls) / len(pnls),
        "median_pnl": sorted(pnls)[len(pnls)//2],
        "min_pnl": min(pnls),
        "max_pnl": max(pnls),
        "pnl_positive_pct": sum(1 for p in pnls if p > 0) / len(pnls),
        "avg_win_rate": sum(win_rates) / len(win_rates),
        "avg_sharpe": sum(sharpes) / len(sharpes),
        "avg_final_capital": sum(finals) / len(finals),
        "profitable_seeds": sum(1 for f in finals if f > 50.0),
        "results": results,
    }


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  POLYMARKET UNIFIED SCORE ENGINE — BACKTEST")
    print("  Testing with real market data + simulated ticks")
    print("=" * 60)
    
    # Show markets being tested
    print("\n📊 Markets Under Test:")
    for m in REAL_MARKETS:
        print(f"  • {m['question'][:55]}")
        print(f"    Price: {m['yes_price']} | Spread: {m['spread']} | Vol: ${m['volume']:,.0f}")
    
    # Single backtest
    print("\n🔬 Running backtest (seed=42, 500 ticks per market)...")
    result = run_backtest(seed=42)
    print(result.summary())
    
    # Show some trades
    if result.trades:
        print("\n📋 Sample Trades (first 10):")
        print(f"  {'Tick':>5} {'Market':<30} {'Entry':>7} {'Exit':>7} {'P&L':>8} {'Score':>6} {'Reason':<12}")
        print(f"  {'─'*5} {'─'*30} {'─'*7} {'─'*7} {'─'*8} {'─'*6} {'─'*12}")
        for t in result.trades[:10]:
            print(f"  {t.tick:>5} {t.market[:30]:<30} {t.entry_price:>7.3f} {t.exit_price:>7.3f} "
                  f"${t.pnl:>7.2f} {t.score:>6.3f} {t.exit_reason:<12}")
    
    # Score distribution
    if result.score_distribution:
        scores = result.score_distribution
        print(f"\n📈 Score Distribution:")
        print(f"  Min:    {min(scores):.4f}")
        print(f"  Max:    {max(scores):.4f}")
        print(f"  Mean:   {sum(scores)/len(scores):.4f}")
        print(f"  Median: {sorted(scores)[len(scores)//2]:.4f}")
        print(f"  > 0.65: {sum(1 for s in scores if s > 0.65)} / {len(scores)} "
              f"({sum(1 for s in scores if s > 0.65)/len(scores)*100:.1f}%)")
        print(f"  > 0.50: {sum(1 for s in scores if s > 0.50)} / {len(scores)} "
              f"({sum(1 for s in scores if s > 0.50)/len(scores)*100:.1f}%)")
    
    # Robustness test
    print("\n" + "=" * 60)
    print("  ROBUSTNESS TEST — 20 Random Seeds")
    print("=" * 60)
    
    robust = run_robustness_test(n_seeds=20, threshold=0.65)
    
    print(f"\n  Seeds tested:        {robust['n_seeds']}")
    print(f"  Avg P&L:             ${robust['avg_pnl']:>8.2f}")
    print(f"  Median P&L:          ${robust['median_pnl']:>8.2f}")
    print(f"  Min P&L:             ${robust['min_pnl']:>8.2f}")
    print(f"  Max P&L:             ${robust['max_pnl']:>8.2f}")
    print(f"  Profitable runs:     {robust['pnl_positive_pct']*100:.0f}%")
    print(f"  Avg Win Rate:        {robust['avg_win_rate']*100:.1f}%")
    print(f"  Avg Sharpe:          {robust['avg_sharpe']:.2f}")
    print(f"  Avg Final Capital:   ${robust['avg_final_capital']:>8.2f}")
    print(f"  Seeds > $50:         {robust['profitable_seeds']} / {robust['n_seeds']}")
    
    # Threshold sensitivity
    print("\n" + "=" * 60)
    print("  THRESHOLD SENSITIVITY ANALYSIS")
    print("=" * 60)
    
    thresholds = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]
    print(f"\n  {'Threshold':>10} {'Trades':>8} {'WinRate':>8} {'AvgPnL':>10} {'Sharpe':>8}")
    print(f"  {'─'*10} {'─'*8} {'─'*8} {'─'*10} {'─'*8}")
    
    for th in thresholds:
        r = run_backtest(seed=42, score_threshold=th)
        print(f"  {th:>10.2f} {r.total_trades:>8} {r.win_rate*100:>7.1f}% "
              f"${r.avg_pnl:>9.4f} {r.sharpe_ratio:>8.2f}")
    
    print("\n✅ Backtest complete.")
