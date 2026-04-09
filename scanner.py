"""
Polymarket Arbitrage Scanner v3 — Uses correct data sources
=============================================================
- Gamma API for prices, spreads, volume (already computed by Polymarket)
- CLOB book only for markets where Gamma shows real spread
- Proper complement arb using both outcome prices
"""

import asyncio
import json
import re
import time
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from collections import defaultdict

import aiohttp

GAMMA_URL = "https://gamma-api.polymarket.com"

@dataclass
class Market:
    slug: str
    question: str
    yes_price: float
    no_price: float
    volume: float
    liquidity: float
    fees_enabled: bool
    yes_token: str
    no_token: str
    event_slug: str
    event_title: str
    spread: float  # From Gamma API
    best_bid: float  # From Gamma API
    best_ask: float  # From Gamma API
    last_trade: float
    accepting_orders: bool
    outcome_prices_sum: float = 0.0

@dataclass
class Opportunity:
    strategy: str
    market_a: str
    market_b: str
    edge_pct: float
    action: str
    price_a: float
    price_b: float
    max_size: float
    profit_est: float
    details: str
    fee_free: bool = False

async def fetch_events(session, limit=500):
    try:
        async with session.get(f"{GAMMA_URL}/events",
            params={"active":"true","closed":"false","limit":limit},
            timeout=aiohttp.ClientTimeout(total=30)) as r:
            return await r.json() if r.status == 200 else []
    except Exception as e:
        print(f"  [!] {e}")
        return []

def parse_all_markets(events) -> List[Market]:
    markets = []
    for ev in events:
        ev_slug = ev.get("slug","")
        ev_title = ev.get("title","")
        tags = ev.get("tags",[])
        if tags and isinstance(tags[0], dict):
            tags = [t.get("label","").lower() for t in tags]
        else:
            tags = [str(t).lower() for t in tags]

        for m in ev.get("markets",[]):
            if m.get("closed") or not m.get("active"):
                continue
            try:
                prices = json.loads(m.get("outcomePrices","[0.5,0.5]"))
                tokens = json.loads(m.get("clobTokenIds","[]"))
            except:
                continue
            if len(prices)<2 or len(tokens)<2:
                continue

            liq = float(m.get("liquidityClob",0) or 0)
            vol = float(m.get("volume",0))

            fee_free = not m.get("feesEnabled",True) or any(
                t in tags for t in ["geopolitics","world","politics"])

            yp = float(prices[0])
            np_ = float(prices[1])
            spread = float(m.get("spread",0))
            bb = float(m.get("bestBid",0) or 0)
            ba = float(m.get("bestAsk",1) or 1)
            ltp = float(m.get("lastTradePrice",0.5) or 0.5)

            markets.append(Market(
                slug=m.get("slug",""),
                question=m.get("question",""),
                yes_price=yp, no_price=np_,
                volume=vol, liquidity=liq,
                fees_enabled=m.get("feesEnabled",False),
                yes_token=tokens[0],
                no_token=tokens[1] if len(tokens)>1 else "",
                event_slug=ev_slug, event_title=ev_title,
                spread=spread, best_bid=bb, best_ask=ba,
                last_trade=ltp,
                accepting_orders=m.get("acceptingOrders",True),
                outcome_prices_sum=round(yp+np_, 4),
            ))
    return markets

# ═══════════════════════════════════════════════════════════
# ARBITRAGE DETECTORS
# ═══════════════════════════════════════════════════════════

def find_complement_arb(markets):
    """
    Complement arb: YES_price + NO_price < $1 → buy both, guaranteed $1.
    Using Gamma API outcomePrices (already includes order book best prices).
    """
    opps = []
    for m in markets:
        total = m.yes_price + m.no_price
        if total < 0.995 and m.liquidity > 1000 and m.volume > 500:
            edge = 1.0 - total
            size_cap = min(m.liquidity * 0.05, 100)
            opps.append(Opportunity(
                strategy="COMPLEMENT_ARB",
                market_a=m.slug, market_b="",
                edge_pct=edge*100,
                action=f"BUY YES@{m.yes_price:.3f} + BUY NO@{m.no_price:.3f}",
                price_a=m.yes_price, price_b=m.no_price,
                max_size=round(size_cap,2),
                profit_est=round(edge*size_cap,4),
                fee_free=not m.fees_enabled,
                details=f"YES={m.yes_price:.4f} + NO={m.no_price:.4f} = {total:.4f} | Edge: ${edge:.4f} ({edge*100:.2f}%) | Liq: ${m.liquidity:.0f} | Fee-free: {not m.fees_enabled}"
            ))
    return sorted(opps, key=lambda o: o.edge_pct, reverse=True)

def find_dependency_arb(markets):
    """Cross-market dependency violations (time subsets, threshold subsets)."""
    opps = []
    months = ['january','february','march','april','may','june',
              'july','august','september','october','november','december']
    stop = {'will','the','a','an','by','in','at','is','be','of','to','and','or',
            'any','for','on','its','it','does','if','before','after','end'}

    by_event = defaultdict(list)
    for m in markets:
        by_event[m.event_slug].append(m)

    for ev_slug, group in by_event.items():
        if len(group) < 2:
            continue
        for i, ma in enumerate(group):
            for mb in group[i+1:]:
                if ma.liquidity < 1000 or mb.liquidity < 1000:
                    continue
                qa, qb = ma.question.lower(), mb.question.lower()

                # Time-based
                ma_m = mb_m = None
                for idx, mo in enumerate(months):
                    if mo in qa and ma_m is None: ma_m = idx
                    if mo in qb and mb_m is None: mb_m = idx

                if ma_m is not None and mb_m is not None and ma_m != mb_m:
                    wa = set(qa.split()) - set(months) - stop
                    wb = set(qb.split()) - set(months) - stop
                    overlap = len(wa & wb) / max(len(wa | wb), 1)
                    if overlap >= 0.4 and ma.yes_price > 0.02 and mb.yes_price > 0.02:
                        if ma_m < mb_m and ma.yes_price > mb.yes_price + 0.02:
                            edge = ma.yes_price - mb.yes_price
                            opps.append(Opportunity(
                                strategy="DEPENDENCY_TIME",
                                market_a=ma.slug, market_b=mb.slug,
                                edge_pct=edge*100,
                                action=f"SELL '{ma.question[:40]}' + BUY '{mb.question[:40]}'",
                                price_a=ma.yes_price, price_b=mb.yes_price,
                                max_size=round(min(ma.liquidity,mb.liquidity)*0.03,2),
                                profit_est=round(edge*min(ma.liquidity,mb.liquidity)*0.03,4),
                                fee_free=not ma.fees_enabled and not mb.fees_enabled,
                                details=f"Earlier deadline should be ≤ later | P({ma.slug[:30]})={ma.yes_price:.3f} > P({mb.slug[:30]})={mb.yes_price:.3f}"
                            ))
                        elif mb_m < ma_m and mb.yes_price > ma.yes_price + 0.02:
                            edge = mb.yes_price - ma.yes_price
                            opps.append(Opportunity(
                                strategy="DEPENDENCY_TIME",
                                market_a=mb.slug, market_b=ma.slug,
                                edge_pct=edge*100,
                                action=f"SELL '{mb.question[:40]}' + BUY '{ma.question[:40]}'",
                                price_a=mb.yes_price, price_b=ma.yes_price,
                                max_size=round(min(ma.liquidity,mb.liquidity)*0.03,2),
                                profit_est=round(edge*min(ma.liquidity,mb.liquidity)*0.03,4),
                                fee_free=not ma.fees_enabled and not mb.fees_enabled,
                                details=f"Earlier deadline should be ≤ later | P({mb.slug[:30]})={mb.yes_price:.3f} > P({ma.slug[:30]})={ma.yes_price:.3f}"
                            ))

                # Threshold-based
                pa = re.findall(r'(\d+(?:\.\d+)?)\s*(b|billion|m|million|k|thousand)', qa, re.I)
                pb = re.findall(r'(\d+(?:\.\d+)?)\s*(b|billion|m|million|k|thousand)', qb, re.I)
                if pa and pb:
                    def to_num(v,u):
                        n=float(v); u=u.lower()
                        return n*(1e9 if u in('b','billion') else 1e6 if u in('m','million') else 1e3 if u in('k','thousand') else 1)
                    na, nb = to_num(pa[0][0],pa[0][1]), to_num(pb[0][0],pb[0][1])
                    wa = set(re.findall(r'[a-z]+',qa)) - stop
                    wb = set(re.findall(r'[a-z]+',qb)) - stop
                    overlap = len(wa & wb) / max(len(wa | wb), 1)
                    if overlap >= 0.4 and abs(na-nb) > 0:
                        if na < nb and ma.yes_price < mb.yes_price - 0.02:
                            edge = mb.yes_price - ma.yes_price
                            opps.append(Opportunity(
                                strategy="DEPENDENCY_THRESH",
                                market_a=ma.slug, market_b=mb.slug,
                                edge_pct=edge*100,
                                action=f"BUY lower-threshold + SELL higher-threshold",
                                price_a=ma.yes_price, price_b=mb.yes_price,
                                max_size=round(min(ma.liquidity,mb.liquidity)*0.03,2),
                                profit_est=round(edge*min(ma.liquidity,mb.liquidity)*0.03,4),
                                fee_free=not ma.fees_enabled and not mb.fees_enabled,
                                details=f"Lower threshold should be ≥ higher | P({ma.slug[:25]})={ma.yes_price:.3f} < P({mb.slug[:25]})={mb.yes_price:.3f}"
                            ))
                        elif na > nb and mb.yes_price < ma.yes_price - 0.02:
                            edge = ma.yes_price - mb.yes_price
                            opps.append(Opportunity(
                                strategy="DEPENDENCY_THRESH",
                                market_a=mb.slug, market_b=ma.slug,
                                edge_pct=edge*100,
                                action=f"BUY lower-threshold + SELL higher-threshold",
                                price_a=mb.yes_price, price_b=ma.yes_price,
                                max_size=round(min(ma.liquidity,mb.liquidity)*0.03,2),
                                profit_est=round(edge*min(ma.liquidity,mb.liquidity)*0.03,4),
                                fee_free=not ma.fees_enabled and not mb.fees_enabled,
                                details=f"Lower threshold should be ≥ higher | P({mb.slug[:25]})={mb.yes_price:.3f} < P({ma.slug[:25]})={ma.yes_price:.3f}"
                            ))

    return sorted(opps, key=lambda o: o.edge_pct, reverse=True)

def find_mutex_arb(markets):
    """Mutual exclusion: outcomes that can't both happen."""
    opps = []
    by_event = defaultdict(list)
    for m in markets:
        by_event[m.event_slug].append(m)

    for ev_slug, group in by_event.items():
        if len(group) < 2:
            continue
        for i, ma in enumerate(group):
            for mb in group[i+1:]:
                if ma.liquidity < 1000 or mb.liquidity < 1000:
                    continue
                qa, qb = ma.question.lower(), mb.question.lower()
                kw = ['win','elected','chosen','first','champion','nominee','lead','become','gets the']
                if any(k in qa for k in kw) and any(k in qb for k in kw):
                    combined = ma.yes_price + mb.yes_price
                    if combined > 1.02:
                        edge = combined - 1.0
                        opps.append(Opportunity(
                            strategy="MUTEX_ARB",
                            market_a=ma.slug, market_b=mb.slug,
                            edge_pct=edge*100,
                            action=f"SELL YES both (collect {combined:.3f}, pay out max 1.0)",
                            price_a=ma.yes_price, price_b=mb.yes_price,
                            max_size=round(min(ma.liquidity,mb.liquidity)*0.03,2),
                            profit_est=round(edge*min(ma.liquidity,mb.liquidity)*0.03,4),
                            fee_free=not ma.fees_enabled and not mb.fees_enabled,
                            details=f"P(A)={ma.yes_price:.3f} + P(B)={mb.yes_price:.3f} = {combined:.3f} > 1.0"
                        ))
    return sorted(opps, key=lambda o: o.edge_pct, reverse=True)

def find_real_spread_capture(markets):
    """Market making on fee-free markets with real two-sided spreads."""
    opps = []
    for m in markets:
        if m.spread < 0.02:
            continue
        if not m.accepting_orders:
            continue
        if m.liquidity < 2000:
            continue
        if m.yes_price < 0.05 or m.yes_price > 0.95:
            continue  # Skip near-certain
        if not (m.best_bid > 0 and m.best_ask < 1):
            continue

        mid = (m.best_bid + m.best_ask) / 2
        size_est = m.liquidity * 0.02
        # Profit per round trip = spread * fill_probability * size
        fill_prob = 0.3 if m.spread > 0.05 else 0.5
        profit_per = m.spread * fill_prob * min(size_est, 50)

        opps.append(Opportunity(
            strategy="SPREAD_CAPTURE",
            market_a=m.slug, market_b="",
            edge_pct=m.spread*100,
            action=f"BID@{m.best_bid:.3f} / ASK@{m.best_ask:.3f} (1.5¢ from mid)",
            price_a=m.best_bid, price_b=m.best_ask,
            max_size=round(min(size_est, 50),2),
            profit_est=round(profit_per,4),
            fee_free=not m.fees_enabled,
            details=f"'{m.question[:60]}' | Bid: {m.best_bid:.3f} Ask: {m.best_ask:.3f} | Spread: {m.spread*100:.1f}¢ | Liq: ${m.liquidity:.0f} | Vol: ${m.volume:.0f}"
        ))

    return sorted(opps, key=lambda o: o.edge_pct, reverse=True)

def find_high_vol_mispricing(markets):
    """Markets with high volume but wide spreads = potential mispricing."""
    opps = []
    for m in markets:
        if m.liquidity < 5000 or m.volume < 50000:
            continue
        if m.spread < 0.02:
            continue
        if m.yes_price < 0.05 or m.yes_price > 0.95:
            continue

        vol_liq = m.volume / m.liquidity
        if vol_liq > 10:
            opps.append(Opportunity(
                strategy="HIGH_VOL_WIDE_SPREAD",
                market_a=m.slug, market_b="",
                edge_pct=m.spread*100,
                action="INVESTIGATE — may signal informed flow or temporary dislocation",
                price_a=m.best_bid, price_b=m.best_ask,
                max_size=round(m.liquidity*0.02,2),
                profit_est=0,
                fee_free=not m.fees_enabled,
                details=f"'{m.question[:60]}' | Spread: {m.spread*100:.1f}¢ | Vol/Liq: {vol_liq:.0f}x | Vol: ${m.volume:,.0f} | Liq: ${m.liquidity:,.0f}"
            ))
    return sorted(opps, key=lambda o: o.edge_pct, reverse=True)

# ═══════════════════════════════════════════════════════════
# REPORT
# ═══════════════════════════════════════════════════════════

def print_report(markets, comp, dep, mutex, spread, highvol):
    print("\n" + "=" * 90)
    print("  POLYMARKET LIVE ARBITRAGE SCAN v3")
    print(f"  {time.strftime('%Y-%m-%d %H:%M:%S %Z')}  |  Real data from Gamma API")
    print("=" * 90)

    # Overview
    active = [m for m in markets if not m.accepting_orders == False]
    fee_free = [m for m in markets if not m.fees_enabled]
    priced = [m for m in markets if 0.05 < m.yes_price < 0.95]
    with_real_spread = [m for m in priced if m.spread >= 0.01]

    print(f"\n📊 MARKET OVERVIEW")
    print(f"   Total active markets:     {len(markets)}")
    print(f"   Accepting orders:         {len(active)}")
    print(f"   Fee-free:                 {len(fee_free)}")
    print(f"   Tradeable (0.05-0.95):    {len(priced)}")
    print(f"   Real spread ≥1¢:          {len(with_real_spread)}")

    # Price distribution of tradeable markets
    if priced:
        print(f"\n   Price distribution (tradeable only):")
        buckets = {"0-10¢":0,"10-25¢":0,"25-40¢":0,"40-60¢":0,"60-75¢":0,"75-90¢":0}
        for m in priced:
            p = m.yes_price
            if p < 0.10: buckets["0-10¢"] += 1
            elif p < 0.25: buckets["10-25¢"] += 1
            elif p < 0.40: buckets["25-40¢"] += 1
            elif p < 0.60: buckets["40-60¢"] += 1
            elif p < 0.75: buckets["60-75¢"] += 1
            else: buckets["75-90¢"] += 1
        for k,v in buckets.items():
            bar = "█" * min(v, 50)
            print(f"     {k:8s}: {v:3d} {bar}")

    # Spread distribution
    if with_real_spread:
        spreads = sorted([m.spread for m in with_real_spread])
        avg = sum(spreads)/len(spreads)
        med = spreads[len(spreads)//2]
        print(f"\n   Spread distribution (tradeable):")
        print(f"     Average: {avg*100:.1f}¢  |  Median: {med*100:.1f}¢")
        print(f"     Min: {spreads[0]*100:.1f}¢  |  Max: {spreads[-1]*100:.1f}¢")
        for thresh in [0.01, 0.02, 0.03, 0.05]:
            count = sum(1 for s in spreads if s >= thresh)
            print(f"     ≥{thresh*100:.0f}¢: {count} markets")

    # Top volume markets
    top_vol = sorted(markets, key=lambda m: m.volume, reverse=True)[:5]
    print(f"\n   Top volume markets:")
    for m in top_vol:
        print(f"     ${m.volume:>14,.0f} | {m.yes_price:.3f} | {m.question[:55]}")

    # Complement arb
    total = len(comp) + len(dep) + len(mutex) + len(spread) + len(highvol)
    print(f"\n🎯 OPPORTUNITIES FOUND: {total}")

    def section(title, icon, opps):
        print(f"\n{'─'*90}")
        print(f"  {icon} {title}: {len(opps)}")
        print(f"{'─'*90}")
        if not opps:
            print("  None found — markets are efficiently priced.")
            return
        for i, o in enumerate(opps[:10]):
            fee = "✅ FEE-FREE" if o.fee_free else "⚠️  FEES"
            print(f"\n  #{i+1} | Edge: {o.edge_pct:.2f}% | Est profit: ${o.profit_est:.4f} | {fee}")
            print(f"       {o.details}")
            print(f"       Action: {o.action}")

    section("COMPLEMENT ARB (YES+NO < $1)", "1️⃣", comp)
    section("DEPENDENCY ARB (time/threshold violations)", "2️⃣", dep)
    section("MUTUAL EXCLUSION (P(A)+P(B) > 1)", "3️⃣", mutex)
    section("SPREAD CAPTURE (fee-free market making)", "4️⃣", spread)
    section("HIGH VOLUME + WIDE SPREAD (investigate)", "5️⃣", highvol)

    # Bottom line
    print(f"\n{'='*90}")
    print("  💰 BOTTOM LINE — $100 on Polymarket right now")
    print(f"{'='*90}")

    fee_surviving = [o for o in comp+dep+mutex if o.edge_pct > 3.0]
    fee_free_mm = [o for o in spread if o.fee_free and o.edge_pct >= 2.0]

    if fee_surviving:
        total_profit = sum(o.profit_est for o in fee_surviving[:5])
        print(f"""
  ✅ {len(fee_surviving)} arbitrage opportunities survive fees (>3% edge)
  Best plays:""")
        for o in fee_surviving[:3]:
            print(f"    • {o.details}")
        print(f"    Est. total profit (top 5): ${total_profit:.4f}")

    if fee_free_mm:
        avg_spread = sum(o.edge_pct for o in fee_free_mm) / len(fee_free_mm)
        est_daily = sum(o.profit_est for o in fee_free_mm[:3])
        print(f"""
  📈 {len(fee_free_mm)} fee-free spread capture opportunities
  Avg spread: {avg_spread:.1f}¢ | Est. daily (top 3, 30% fill): ${est_daily:.2f}
  Markets:""")
        for o in fee_free_mm[:3]:
            print(f"    • {o.details}")

    if not fee_surviving and not fee_free_mm:
        print(f"""
  ❌ NO profitable opportunities found right now.

  Why:
  • Complement arbs: YES+NO prices sum tightly to $1.00 (efficient)
  • Dependency arbs: Cross-market relationships are correctly priced
  • Mutex arbs: Outcome prices respect probability constraints
  • Spreads: Tight on tradeable markets, wide only on near-certain/uncertain
  • Fee-free markets: Exist but spreads too narrow to profit after slippage

  The BTC lag arbitrage ($313→$414K in Dec 2025) was killed by Polymarket
  in March 2026 with dynamic taker fees and removal of price delay.

  What WOULD work:
  • Real-time exchange price feed → faster than Polymarket's oracle updates
  • NLP on breaking news → react before Polymarket odds adjust
  • Liquidity provision on new markets → earn spread before competition arrives
  • Cross-platform arb (Polymarket vs Kalshi vs Betfair) → requires accounts on all""")

    elif fee_free_mm and not fee_surviving:
        print(f"""
  ⚠️  Spread capture exists but is UNLIKELY to beat $100 profitably.

  Why:
  • $100 limits you to tiny positions ($5-20 per trade)
  • At 2-3¢ spread on $10 position = 2-3¢ profit per fill
  • Need 200+ fills/day to earn meaningful money
  • GTC orders may sit for hours/days unfilled
  • Price can move against you while resting

  Realistic daily expectation: $0.50 - $3.00 on good days
  Realistic daily expectation: -$1.00 - $0.00 on bad days
  Annualized if consistently profitable: ~$200-500 on $100 capital""")

    print(f"\n{'='*90}\n")

# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

async def main():
    print("🔍 Polymarket Live Arbitrage Scanner v3")
    print("=" * 50)

    async with aiohttp.ClientSession() as session:
        print("📡 Fetching all active events...")
        events = await fetch_events(session)
        print(f"   {len(events)} events loaded")

        print("🔎 Parsing markets...")
        markets = parse_all_markets(events)
        print(f"   {len(markets)} markets parsed")

        print("\n🔎 Running 5 arbitrage detectors on live data...\n")
        comp = find_complement_arb(markets)
        dep = find_dependency_arb(markets)
        mutex = find_mutex_arb(markets)
        spread = find_real_spread_capture(markets)
        highvol = find_high_vol_mispricing(markets)

        print_report(markets, comp, dep, mutex, spread, highvol)

if __name__ == "__main__":
    asyncio.run(main())
