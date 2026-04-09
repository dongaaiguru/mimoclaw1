"""
PolyEdge v2 — Research-Optimized Polymarket Trading Bot
========================================================
Combines 5 proven strategies for consistent daily returns on $100:

  1. Fee-Free Spread Capture — GTC limit orders on geopolitics markets (zero fees)
  2. Liquidity Rewards — Two-sided orders on sports markets ($5M/month pool)
  3. Dependency Arb — Cross-market logical mispricing ($29M proven edge)
  4. Maker Rebates — Earn 20-25% of taker fees back daily
  5. News Momentum — Ride informed flow on high-volume anomalies

Built on live market research (3,440 markets scanned April 2026).
Incorporates findings from academic paper (arxiv 2508.03474),
@defiance_cr's open-source MM bot, and Polymarket reward formulas.

Target: 2% daily ($2 on $100)
Realistic: 1-3% depending on market conditions

Usage:
    python -m polyedge --scan              # Discovery mode
    python -m polyedge --paper             # Paper trading
    python -m polyedge --live              # Live trading
    python -m polyedge --live --capital 100 --target 0.02
    python -m polyedge --scan --sports     # Focus on sports rewards
"""

__version__ = "2.0.0"
