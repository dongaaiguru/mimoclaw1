"""Entry point: python -m polyedge"""

import sys
import asyncio
import argparse
import logging

from .core import Config
from .bot import PolyEdgeBot

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)


def main():
    parser = argparse.ArgumentParser(description="PolyEdge Bot v2 — Polymarket Trading")
    parser.add_argument("--scan", action="store_true", help="Discovery mode (no trading)")
    parser.add_argument("--paper", action="store_true", help="Paper trading with live prices")
    parser.add_argument("--live", action="store_true", help="Live trading (requires .env)")
    parser.add_argument("--sports", action="store_true", help="Focus on sports liquidity rewards")
    parser.add_argument("--capital", type=float, default=100.0, help="Starting capital")
    parser.add_argument("--target", type=float, default=0.02, help="Daily target (0.02 = 2%%)")
    args = parser.parse_args()

    if not any([args.scan, args.paper, args.live]):
        args.scan = True

    cfg = Config(
        capital=args.capital,
        daily_target=args.capital * args.target,
    )

    if args.sports:
        cfg.fee_free_spread_pct = 0.20
        cfg.rewards_pct = 0.50
        cfg.arb_pct = 0.20
        cfg.news_pct = 0.10

    if args.live and not cfg.is_live:
        print("ERROR: Live mode requires POLYMARKET_API_KEY, POLYMARKET_API_SECRET, "
              "POLYMARKET_API_PASSPHRASE in .env file")
        sys.exit(1)

    mode = "scan"
    if args.paper: mode = "paper"
    elif args.live: mode = "live"

    bot = PolyEdgeBot(cfg)
    asyncio.run(bot.run(mode))


if __name__ == "__main__":
    main()
