"""
Polymarket Engine v3 — FINAL BACKTEST
Post-adaptation strategy after BTC lag arbitrage was killed by dynamic fees.

Strategy: Unified Score on fee-free longer-dated markets + LP rebates

Score = 0.35*EV + 0.20*KL + 0.20*ΔP + 0.15*LMSR - 0.10*Risk
Trade if Score > 0.42 AND all hard filters pass
"""
import math, random, sys
sys.path.insert(0, '/root/.openclaw/workspace/polymarket-engine')

from src.models.score import (
    compute_ev, compute_kl, compute_delta_p, compute_stoikov_risk,
    LMSRModel, BayesianEstimator, clamp,
)

MARKETS = [
    {"slug":"btc-150k-jun","p":0.017,"s":0.002,"v":3942360,"l":50000,"d":82},
    {"slug":"btc-150k-dec","p":0.095,"s":0.01,"v":1000000,"l":30000,"d":266},
    {"slug":"mstr-sell-jun","p":0.0275,"s":0.001,"v":918245,"l":65000,"d":82},
    {"slug":"mstr-sell-dec","p":0.115,"s":0.01,"v":464956,"l":30000,"d":266},
    {"slug":"megaeth-1b","p":0.325,"s":0.01,"v":2893683,"l":40000,"d":30},
    {"slug":"megaeth-2b","p":0.095,"s":0.01,"v":5820061,"l":50000,"d":30},
    {"slug":"megaeth-airdrop","p":0.4265,"s":0.009,"v":1046466,"l":35000,"d":82},
    {"slug":"trump-crypto","p":0.0445,"s":0.025,"v":19964,"l":8000,"d":266},
]

RELS = [
    ("btc-150k-jun","btc-150k-dec","subset",5.5),
    ("mstr-sell-jun","mstr-sell-dec","subset",4.2),
    ("megaeth-2b","megaeth-1b","subset",3.4),
]

def gen_ticks(bp, n=2000, vol=0.002, spr=0.005, seed=42):
    rng = random.Random(seed)
    ticks, price, trend, vm = [], bp, 0.0, 1.0
    for i in range(n):
        kl = max(0.01, 0.5*math.exp(-i/500))
        if rng.random()<0.03: trend = rng.gauss(0,0.2)
        trend *= 0.99
        jump = 0.0
        if rng.random()<0.012:
            jump = (1 if rng.random()<0.55 else -1)*0.025*rng.expovariate(1.0)
            vm = 2.5
        else: vm = max(0.5, vm*0.96)
        noise = rng.gauss(0, vol*vm)
        chg = trend + noise + jump
        if price>0.9: chg -= (price-0.9)*0.05
        elif price<0.05: chg += (0.05-price)*0.05
        old = price
        price = clamp(price+chg, 0.001, 0.999)
        cs = max(0.001, min(spr*(1+vm*0.5+abs(jump)*30), 0.05))
        bv = rng.expovariate(1.0)
        if abs(price-old)>vol*1.5: bv *= (1+kl*10)
        if abs(jump)>0: bv *= 5.0
        ticks.append({"tick":i,"price":price,"bid":price-cs/2,"ask":price+cs/2,
                       "spread":cs,"volume":bv,"change":price-old,"kyle":kl,
                       "lp_edge":max(cs-0.005,0)})
    return ticks

def gen_corr(base, ratio, noise=0.0015, spr=0.005, seed=99):
    rng = random.Random(seed)
    ticks = []
    for b in base:
        cm = b["change"]*0.65
        n = rng.gauss(0,noise)
        p = b["price"]*ratio if not ticks else clamp(ticks[-1]["price"]+cm+n, 0.001,0.999)
        cs = spr*(1+abs(b.get("jump",0))*20)
        ticks.append({"tick":b["tick"],"price":p,"bid":p-cs/2,"ask":p+cs/2,
                       "spread":cs,"volume":b["volume"]*0.8,"change":cm+n})
    return ticks

def run(seed=42, threshold=0.42, capital0=50.0, n=2000, fees=0.002):
    mmap = {m["slug"]:m for m in MARKETS}
    td, est, lmsr = {}, {}, LMSRModel()
    for m in MARKETS:
        ms = seed + hash(m["slug"])%10000
        v = 0.001+(1-m["p"])*0.002
        td[m["slug"]] = gen_ticks(m["p"],n,v,m["s"],ms)
        est[m["slug"]] = BayesianEstimator(m["p"],15.0)
    rd = {}
    for p,r,_,rat in RELS:
        if p in td: rd[r] = gen_corr(td[p],rat,seed=seed+hash(r)%10000,spr=mmap.get(r,{"s":0.005})["s"])
    
    cap, peak = capital0, capital0
    pos, trades, scores = {}, [], []
    checked, exe, lp_n, dt = 0,0,0,0
    
    for ti in range(n):
        for sl in list(td.keys()):
            if ti >= len(td[sl]): continue
            t = td[sl][ti]; m = mmap[sl]; e = est[sl]
            e.update(t["change"],t["volume"],1 if t["change"]>0 else -1)
            
            # EXIT
            if sl in pos:
                p = pos[sl]; entry=p["entry"]; cur=t["price"]
                pp = (cur-entry)/entry if entry>0 else 0
                ex,er = False,""
                
                if p.get("strat")=="lp":
                    # LP exits after cycle or if inventory risk too high
                    if ti-p["et"]>=p.get("cycle",20):
                        ex,er=True,"lp_cycle"
                    elif abs(pp)>0.05:  # 5% adverse move on inventory
                        ex,er=True,"lp_inventory_risk"
                else:
                    if pp>=0.04: ex,er=True,"profit_target"
                    elif pp<=-0.08: ex,er=True,"stop_loss"
                    elif ti-p["et"]>400: ex,er=True,"time_decay"
                
                if ex:
                    if p.get("strat")=="lp":
                        # LP: return capital + rebate, minus inventory loss
                        pnl = p.get("reb",0) + p["size"]*min(pp,0)  # Only count losses, not gains
                        cap += p["size"] + pnl
                        trades.append({"tick":ti,"m":sl,"strat":"lp",
                                       "ep":entry,"xp":cur,"sz":p["size"],"sc":p["score"],
                                       "pnl":pnl,"r":er})
                    else:
                        sp = t["bid"]
                        pnl = p["size"]*((sp-entry)/entry) - p["size"]*fees*2
                        cap += p["size"]+pnl
                        trades.append({"tick":ti,"m":sl,"strat":p.get("strat","mispr"),
                                       "ep":entry,"xp":sp,"sz":p["size"],"sc":p["score"],
                                       "pnl":pnl,"r":er})
                    del pos[sl]
                continue
            
            if len(pos)>=3: continue
            checked+=1
            
            # 1. EV
            ev = compute_ev(e.probability, t["ask"], fees, 0.015, 0.15)
            
            # 2. KL
            kn = 0.0
            for pr,rl,rt,_ in RELS:
                os = None; ar = rt
                if sl==pr: os=rl
                elif sl==rl: os=pr; ar="superset"
                if os and os in rd:
                    oi = min(ti,len(rd[os])-1)
                    kr = compute_kl(t["price"],rd[os][oi]["price"],ar,0.08)
                    kn = max(kn, kr.normalized)
            
            # 3. ΔP
            dpr = compute_delta_p(e, lookback=15, delta_max=0.04)
            
            # 4. LMSR
            ps = min(cap*0.25/3, cap*0.25)
            b = max(20, m["l"]/50)
            lr = lmsr.compute_impact(t["price"], ps, b)
            
            # 5. Stoikov + Kyle
            kp = clamp(t["kyle"]/0.5,0,1)
            sr = compute_stoikov_risk(t["price"],t["bid"],t["ask"],0.0,0.05)
            cr = 0.6*sr.total_risk + 0.4*kp
            
            # SCORE
            dd = max(0,(peak-cap)/peak)
            sc = 0.35*ev.normalized + 0.20*kn + 0.20*dpr.normalized + 0.15*lr.normalized - 0.10*cr
            scores.append(sc)
            
            ok = ev.raw>=0.015 and t["spread"]<0.03 and m["l"]>=8000 and dt<20 and dd<0.15
            
            if sc>threshold and ok and cap>1.0:
                k = max(0,(e.probability-t["ask"])/(1-t["ask"])*0.25)
                k *= (1-clamp(t["kyle"]/0.3,0,0.8))
                sz = min(k*cap, cap/3, cap*0.25)
                if sz>=1.0:
                    cost = sz+sz*fees
                    if cost<=cap:
                        cap -= cost
                        pos[sl]={"entry":t["ask"],"size":sz,"et":ti,"score":sc,"strat":"mispr"}
                        exe+=1; dt+=1; continue
            
            # LP REBATE
            if sl not in pos and len(pos)<3:
                le = t.get("lp_edge",0)
                if le>0.003 and t["spread"]>0.008:
                    lsz = min(cap*0.15, cap/3)
                    if lsz>=1.0 and lsz<=cap:
                        lr2 = t["spread"]/0.03
                        if lr2<0.8:
                            cap -= lsz
                            # LP earns rebate over ~20-50 ticks then exits
                            cycle_ticks = 20 + int(abs(hash(str(ti)+sl)) % 30)
                            expected_rebate = lsz * t["spread"] * 0.3  # 30% of spread
                            pos[sl]={"entry":t["price"],"size":lsz,"et":ti,"score":0.5,
                                     "strat":"lp","reb":expected_rebate,"cycle":cycle_ticks}
                            lp_n+=1; dt+=1
            
            # Track peak
            tv = cap
            for s2,p2 in pos.items():
                if s2 in td:
                    idx=min(ti,len(td[s2])-1)
                    if p2.get("strat")=="lp": tv+=p2["size"]+p2.get("reb",0)
                    else: tv+=p2["size"]*td[s2][idx]["price"]/p2["entry"]
            peak=max(peak,tv)
    
    # Close remaining
    for sl,p in list(pos.items()):
        ft=td[sl][-1]
        if p.get("strat")=="lp":
            reb=p.get("reb",0)*2; cap+=p["size"]+reb
            trades.append({"tick":n,"m":sl,"strat":"lp","ep":p["entry"],"xp":ft["price"],
                           "sz":p["size"],"sc":p["score"],"pnl":reb,"r":"lp_cycle"})
        else:
            sp=ft["bid"]; pp=(sp-p["entry"])/p["entry"]
            pnl=p["size"]*pp-p["size"]*fees*2; cap+=p["size"]+pnl
            trades.append({"tick":n,"m":sl,"strat":p.get("strat","mispr"),
                           "ep":p["entry"],"xp":sp,"sz":p["size"],"sc":p["score"],
                           "pnl":pnl,"r":"end"})
    
    wins=[t for t in trades if t["pnl"]>0]
    tpnl=sum(t["pnl"] for t in trades)
    if trades:
        rets=[t["pnl"]/max(1,t["sz"]) for t in trades]
        mr=sum(rets)/len(rets)
        sr2=math.sqrt(sum((r-mr)**2 for r in rets)/max(1,len(rets)-1))
        sh=mr/max(sr2,1e-6)*math.sqrt(len(trades))
    else: sh=0
    pk=capital0; mdd=0; rn=capital0
    for t in trades:
        rn+=t["pnl"]; pk=max(pk,rn); dd2=(pk-rn)/pk; mdd=max(mdd,dd2)
    
    mt=[t for t in trades if t["strat"]=="mispr"]
    lt=[t for t in trades if t["strat"]=="lp"]
    
    return {
        "trades":len(trades),"wins":len(wins),"wr":len(wins)/max(1,len(trades)),
        "pnl":tpnl,"avg":tpnl/max(1,len(trades)),"dd":mdd,"sharpe":sh,
        "final":cap,"ret":(cap/capital0-1)*100,"checked":checked,"exe":exe,
        "er":exe/max(1,checked)*100,"lp":len(lt),"mt":len(mt),
        "mwr":sum(1 for t in mt if t["pnl"]>0)/max(1,len(mt)),
        "lp_pnl":sum(t["pnl"] for t in lt),
        "avg_sc":sum(scores)/max(1,len(scores)),
        "all":trades,
    }

def robust(n=30, th=0.42):
    rs=[run(seed=s,threshold=th) for s in range(n)]
    pnls=[r["pnl"] for r in rs]; fins=[r["final"] for r in rs]
    return {
        "n":n,"th":th,
        "avg_pnl":sum(pnls)/n,"med_pnl":sorted(pnls)[n//2],
        "std":math.sqrt(sum((p-sum(pnls)/n)**2 for p in pnls)/n),
        "min":min(pnls),"max":max(pnls),
        "profit":sum(1 for p in pnls if p>0)/n*100,
        "above50":sum(1 for f in fins if f>50)/n*100,
        "avg_wr":sum(r["wr"] for r in rs)/n,
        "avg_sh":sum(r["sharpe"] for r in rs)/n,
        "avg_ret":sum(r["ret"] for r in rs)/n,
    }

if __name__=="__main__":
    print("═"*60)
    print("  POLYMARKET ENGINE v3 — FINAL BACKTEST")
    print("  Post-dynamic-fees adaptation")
    print("═"*60)
    
    r = run(seed=42, threshold=0.42)
    mp = sum(t["pnl"] for t in r["all"] if t["strat"]=="mispr")
    lp = sum(t["pnl"] for t in r["all"] if t["strat"]=="lp")
    
    print(f"""
╔══════════════════════════════════════════════════════════╗
║      UNIFIED SCORE ENGINE v3 — BACKTEST RESULTS          ║
╠══════════════════════════════════════════════════════════╣
║  Signals Checked:    {r['checked']:>6}                             ║
║  Signals Executed:   {r['exe']:>6}                             ║
║  Execution Rate:     {r['er']:>5.1f}%                            ║
╠══════════════════════════════════════════════════════════╣
║  Total Trades:       {r['trades']:>6}                             ║
║  Win Rate:           {r['wr']*100:>5.1f}%                            ║
║  Total P&L:          ${r['pnl']:>8.2f}                         ║
║  Avg P&L/Trade:      ${r['avg']:>8.4f}                         ║
║  Max Drawdown:       {r['dd']*100:>5.1f}%                            ║
║  Sharpe Ratio:       {r['sharpe']:>6.2f}                           ║
╠══════════════════════════════════════════════════════════╣
║  Mispricing Trades:  {r['mt']:>6}  P&L: ${mp:>7.2f}            ║
║  Mispricing Win%:    {r['mwr']*100:>5.1f}%                            ║
║  LP Rebate Trades:   {r['lp']:>6}  P&L: ${lp:>7.2f}            ║
╠══════════════════════════════════════════════════════════╣
║  Starting Capital:   $ 50.00                             ║
║  Final Capital:      ${r['final']:>8.2f}                         ║
║  Return:             {r['ret']:>6.1f}%                           ║
║  Avg Score:          {r['avg_sc']:>6.4f}                           ║
╚══════════════════════════════════════════════════════════╝""")
    
    if r['all']:
        print(f"\n  📋 Trades:")
        print(f"  {'Tick':>5} {'Market':<16} {'Strat':<6} {'Entry':>7} {'Exit':>7} {'P&L':>8} {'Sc':>5} {'Reason':<12}")
        print(f"  {'─'*5} {'─'*16} {'─'*6} {'─'*7} {'─'*7} {'─'*8} {'─'*5} {'─'*12}")
        for t in r['all'][:20]:
            print(f"  {t['tick']:>5} {t['m'][:16]:<16} {t['strat']:<6} {t['ep']:>7.3f} {t['xp']:>7.3f} "
                  f"${t['pnl']:>7.2f} {t['sc']:>5.3f} {t['r']:<12}")
    
    # Threshold sweep
    print(f"\n{'═'*60}")
    print("  THRESHOLD SENSITIVITY")
    print(f"{'═'*60}")
    print(f"\n  {'Th':>5} {'Trd':>5} {'Win%':>6} {'P&L':>8} {'Ret%':>6} {'Sh':>6}")
    print(f"  {'─'*5} {'─'*5} {'─'*6} {'─'*8} {'─'*6} {'─'*6}")
    for th in [0.35,0.38,0.40,0.42,0.45,0.48,0.50,0.55]:
        r2 = run(seed=42, threshold=th)
        print(f"  {th:>5.2f} {r2['trades']:>5} {r2['wr']*100:>5.1f}% ${r2['pnl']:>7.2f} {r2['ret']:>5.1f}% {r2['sharpe']:>6.2f}")
    
    # Robustness
    print(f"\n{'═'*60}")
    print("  ROBUSTNESS — 30 Seeds @ 0.42")
    print(f"{'═'*60}")
    rob = robust(30, 0.42)
    print(f"""
  Seeds:             {rob['n']}
  Avg P&L:           ${rob['avg_pnl']:>8.2f}
  Median P&L:        ${rob['med_pnl']:>8.2f}
  Std P&L:           ${rob['std']:>8.2f}
  Min/Max:           ${rob['min']:>8.2f} / ${rob['max']:>8.2f}
  Profitable:        {rob['profit']:.0f}%
  Above $50:         {rob['above50']:.0f}%
  Avg Win Rate:      {rob['avg_wr']*100:.1f}%
  Avg Sharpe:        {rob['avg_sh']:.2f}
  Avg Return:        {rob['avg_ret']:.1f}%
""")
    print("✅ Complete.")
