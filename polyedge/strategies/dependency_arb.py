"""Dependency Arbitrage Strategy.

Academic proof: $29M extracted from cross-market dependencies (arxiv 2508.03474).

Three types:
1. Time subset: P(by June) ≤ P(by December)
2. Threshold subset: P(>$200M) ≥ P(>$500M)
3. Cumulative: P(Jan) + P(Feb) + ... + P(Dec) + P(no recession) ≤ 100%

Live findings (April 2026):
- Trump deport <200K at 10¢ vs deport 300-400K at 36.5¢ (26.5% edge)
- Kraken IPO multiple deadline mispricing (9% edge)
"""

import re
import logging
from typing import List, Optional
from collections import defaultdict

from ..core import Config, Market, Signal

log = logging.getLogger("polyedge.strategies.arb")

MONTHS = ['january','february','march','april','may','june',
          'july','august','september','october','november','december']
STOP = {'will','the','a','an','by','in','at','is','be','of','to','and','or',
        'any','for','on','its','it','does','if','before','after','end'}


def scan(markets: List[Market], cfg: Config) -> List[Signal]:
    """Find dependency arbitrage opportunities."""
    by_event = defaultdict(list)
    for m in markets:
        if m.liquidity >= cfg.arb_min_liquidity and 0.02 < m.yes_price < 0.98:
            by_event[m.event_slug].append(m)

    signals = []
    for ev_slug, group in by_event.items():
        if len(group) < 2:
            continue
        for i, ma in enumerate(group):
            for mb in group[i+1:]:
                pair_signals = _check_pair(ma, mb, cfg)
                if pair_signals:
                    signals.extend(pair_signals)

    signals.sort(key=lambda s: s.edge, reverse=True)
    return signals[:6]  # Top 3 pairs (6 legs)


def _check_pair(ma: Market, mb: Market, cfg: Config) -> Optional[List[Signal]]:
    qa, qb = ma.question.lower(), mb.question.lower()

    # ── Time subset ──
    result = _check_time(qa, qb, ma, mb, cfg)
    if result:
        return result

    # ── Threshold subset ──
    result = _check_threshold(qa, qb, ma, mb, cfg)
    if result:
        return result

    return None


def _check_time(qa, qb, ma, mb, cfg) -> Optional[List[Signal]]:
    ma_m = mb_m = None
    for idx, mo in enumerate(MONTHS):
        if mo in qa and ma_m is None: ma_m = idx
        if mo in qb and mb_m is None: mb_m = idx

    if ma_m is None or mb_m is None or ma_m == mb_m:
        return None

    wa = set(qa.split()) - set(MONTHS) - STOP
    wb = set(qb.split()) - set(MONTHS) - STOP
    overlap = len(wa & wb) / max(len(wa | wb), 1)
    if overlap < 0.4:
        return None

    size = min(cfg.capital * cfg.arb_pct / 3, cfg.max_position)

    if ma_m < mb_m and ma.yes_price > mb.yes_price + 0.02:
        edge = ma.yes_price - mb.yes_price
        return [
            Signal("DEPENDENCY_ARB", ma.slug, "SELL", ma.yes_price, size,
                   edge*100, overlap, f"Sell earlier deadline ({ma.yes_price:.3f})",
                   mb.slug, "BUY"),
            Signal("DEPENDENCY_ARB", mb.slug, "BUY", mb.yes_price, size,
                   edge*100, overlap, f"Buy later deadline ({mb.yes_price:.3f})",
                   ma.slug, "SELL"),
        ]
    elif mb_m < ma_m and mb.yes_price > ma.yes_price + 0.02:
        edge = mb.yes_price - ma.yes_price
        return [
            Signal("DEPENDENCY_ARB", mb.slug, "SELL", mb.yes_price, size,
                   edge*100, overlap, f"Sell earlier deadline ({mb.yes_price:.3f})",
                   ma.slug, "BUY"),
            Signal("DEPENDENCY_ARB", ma.slug, "BUY", ma.yes_price, size,
                   edge*100, overlap, f"Buy later deadline ({ma.yes_price:.3f})",
                   mb.slug, "SELL"),
        ]
    return None


def _check_threshold(qa, qb, ma, mb, cfg) -> Optional[List[Signal]]:
    pa = re.findall(r'(\d+(?:\.\d+)?)\s*(b|billion|m|million|k|thousand)', qa, re.I)
    pb = re.findall(r'(\d+(?:\.\d+)?)\s*(b|billion|m|million|k|thousand)', qb, re.I)
    if not pa or not pb:
        return None

    def to_num(v, u):
        n = float(v); u = u.lower()
        return n * (1e9 if u in ('b','billion') else 1e6 if u in ('m','million') else 1e3 if u in ('k','thousand') else 1)

    na, nb = to_num(pa[0][0], pa[0][1]), to_num(pb[0][0], pb[0][1])
    if na == nb:
        return None

    wa = set(re.findall(r'[a-z]+', qa)) - STOP
    wb = set(re.findall(r'[a-z]+', qb)) - STOP
    overlap = len(wa & wb) / max(len(wa | wb), 1)
    if overlap < 0.35:
        return None

    size = min(cfg.capital * cfg.arb_pct / 3, cfg.max_position)

    if na < nb and ma.yes_price < mb.yes_price - 0.02:
        edge = mb.yes_price - ma.yes_price
        return [
            Signal("DEPENDENCY_ARB", ma.slug, "BUY", ma.yes_price, size,
                   edge*100, overlap, f"Buy lower threshold ({ma.yes_price:.3f})",
                   mb.slug, "SELL"),
            Signal("DEPENDENCY_ARB", mb.slug, "SELL", mb.yes_price, size,
                   edge*100, overlap, f"Sell higher threshold ({mb.yes_price:.3f})",
                   ma.slug, "BUY"),
        ]
    elif nb < na and mb.yes_price < ma.yes_price - 0.02:
        edge = ma.yes_price - mb.yes_price
        return [
            Signal("DEPENDENCY_ARB", mb.slug, "BUY", mb.yes_price, size,
                   edge*100, overlap, f"Buy lower threshold ({mb.yes_price:.3f})",
                   ma.slug, "SELL"),
            Signal("DEPENDENCY_ARB", ma.slug, "SELL", ma.yes_price, size,
                   edge*100, overlap, f"Sell higher threshold ({ma.yes_price:.3f})",
                   mb.slug, "BUY"),
        ]
    return None
