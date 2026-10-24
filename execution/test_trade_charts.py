#!/usr/bin/env python3
"""Golden tests for the holding chart and tapping an Activity row (2026-10-24).

Run from this directory:  python test_trade_charts.py

Owner's request: the holding chart in Positions should carry the same lines as a
signal's chart - price, the 200-day and 50-day averages, where price crossed the
200-day, the cut-loss and the entry - from a little before the buy to the last
close, with the cut-loss as it moved; and tapping a trade in History > Activity
should open its chart: the holding while it is held, the whole trade once sold.
This runs the page's own script under node against made-up products and a
ledger, and checks the window, the marks and every route a tap can take.
"""
import io
import json
import os
import shutil
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(os.path.dirname(HERE), "docs", "index.html")

_HARNESS = r"""
const fs=require('fs'), vm=require('vm');
const [pagePath]=process.argv.slice(2);
process.on('unhandledRejection',e=>{ console.error('UNHANDLED', e&&e.stack||e); process.exit(3); });
const html=fs.readFileSync(pagePath,'utf8');
const src=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const els={};
const fake=()=>({innerHTML:'',textContent:'',style:{},dataset:{},
  classList:{add(){},remove(){},contains(){return false}},
  addEventListener(){},setAttribute(){},getAttribute(){return null},remove(){},
  appendChild(){},querySelector(){return null},querySelectorAll(){return []},
  closest(){return null},parentElement:null,getContext(){return null}});
const store={};
const ctx={console, setTimeout, clearTimeout, setInterval(){}, clearInterval(){},
  document:{querySelector:s=>(els[s]=els[s]||fake()), querySelectorAll:()=>[],
    getElementById:id=>(els['#'+id]=els['#'+id]||fake()), createElement:()=>fake(),
    body:fake(), head:fake()},
  localStorage:{getItem:k=>(k in store?store[k]:null), setItem:(k,v)=>{store[k]=String(v)}},
  addEventListener(){}, location:{reload(){}}, scrollY:0, alert(){}, confirm:()=>false,
  prompt:()=>null, navigator:{}, open(){}};
ctx.window=ctx;
// 300 weekdays of a stock that dips below its 200-day and back above it.
const days=[]; let t=Date.parse('2025-08-01T00:00:00Z');
while(days.length<300){ const d=new Date(t); if(d.getUTCDay()%6) days.push(d.toISOString().slice(0,10)); t+=864e5; }
const px=days.map((d,i)=> i<230 ? 100+i*0.2 : (i<245 ? 146-(i-230)*4 : 86+(i-245)*2));
const prod={prices:days.map((d,i)=>[d,+px[i].toFixed(2)]), sma200:[]};
ctx.fetch=(u)=>Promise.resolve(String(u).startsWith('products/')
  ? {ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(prod)),text:async()=>''}
  : {ok:false,status:404,json:async()=>({}),text:async()=>''});
vm.createContext(ctx);
vm.runInContext(src, ctx);
ctx.DAYS=days; ctx.PROD=prod;
vm.runInContext(String.raw`(async()=>{
  const tick=async()=>{ await new Promise(r=>setTimeout(r,0)); await new Promise(r=>setTimeout(r,0)); };
  await tick();
  loadChartLib=async()=>true;
  Chart=function(cv,cfg){ this.data=cfg.data; this.destroy=()=>{}; };
  const D=i=>DAYS[i];
  const R={};
  IDX=['AAA','BBB','CCC','DDD','EEE'].map(s=>({sym:s, market:'US', price:150}));
  DATA={positions:[], backtest_trades:[]};
  ADOPT=[]; ADOPT_OK=true; STOPS={AAA:[[D(260), 80],[D(280), 95]], BBB:[[D(150), 90]]}; STOPS_OK=true;
  const pos=(s,ac)=>({symbol:s, ib_symbol:s, qty:10, avg_cost:ac, ccy:'USD', entry:ac, stop:95});
  const fill=(s,i,side,q,pr,ref)=>({ts:D(i)+' 13:30:00', date:D(i), symbol:s, side, qty:q, price:pr, sec_type:'STK', order_ref:ref});
  const row=(i,a,s,q,st,extra)=>Object.assign({time:D(i)+' 23:35 UTC', action:a, symbol:s, qty:q, status:st, limit:'1', ccy:'USD', reason:'r'}, extra||{});
  BOT={updated:'2099-01-01 00:00 UTC', netliq:1, cash:{}, positions:[pos('AAA',111),pos('BBB',120),pos('CCC',150),pos('DDD',140)],
       activity:[
        row(149,'BUY','AAA',10,'sent'), row(199,'SELL','AAA',10,'sent'), row(259,'BUY','AAA',10,'sent'),
        Object.assign(row(262,'HALT','ENTRIES',0,'notice'),{ccy:'HKD'}),
        row(266,'BUY','ZZZ',5,'REJECTED'), row(267,'SELL','AAA',10,'REJECTED'),
        row(270,'BUY','ZZZ',5,'sent'),
        Object.assign(row(276,'ADOPT','EEE',10,'notice'),{reason:'your own IB buy, now managed by the bot: cut-loss 9, entry 10 ('+D(240)+')'}),
        Object.assign(row(277,'ADOPT','CCC',10,'notice'),{reason:'your own IB buy ... ('+D(295)+')'})]};
  FILLS=[fill('AAA',150,'BOT',10,130,'mps-1'), fill('AAA',201,'SLD',10,140,'mps-2'), fill('AAA',260,'BOT',10,110.5,'mps-3'),
         fill('BBB',150,'BOT',10,119,'mps-4'),
         fill('CCC',295,'BOT',10,149,null),
         fill('DDD',200,'BOT',10,130,'mps-5'),
         {ts:D(270)+' 13:30:00', date:D(270), symbol:'DDD', side:'SLD', qty:10, price:139, sec_type:'STK', order_ref:'mps-6'},
         {ts:D(270)+' 15:00:00', date:D(270), symbol:'DDD', side:'BOT', qty:10, price:141, sec_type:'STK', order_ref:'mps-7'},
         fill('EEE',240,'BOT',10,90,null), fill('EEE',280,'SLD',10,130,'mps-8')];
  renderPositions();
  async function sheet(sym){
    POS_SHEET=sym; POS_CHART=null;
    renderPosSheet(JSON.parse(JSON.stringify(PROD))); await tick();
    const ds=POS_CHART?POS_CHART.data.datasets:[];
    const pick=l=>{ const d=ds.find(x=>x.label===l); return d?d.data.map((v,i)=>v==null?null:[i,v]).filter(Boolean):null; };
    return {labels:ds.map(d=>d.label), first:POS_CHART.data.labels[0], n:POS_CHART.data.labels.length,
            cross:pick('Crossed the 200-day'), bought:pick('Bought'), sold:pick('Sold (part)'),
            entry0:(pick('Entry (you paid)')||[])[0]||null, cut:pick('Cut-loss (as it moved)'),
            html:document.querySelector('#pos-sheet').innerHTML};
  }
  R.AAA=await sheet('AAA'); R.BBB=await sheet('BBB'); R.CCC=await sheet('CCC'); R.DDD=await sheet('DDD');
  // the crossings sigFacts finds, to check the dots land exactly on them
  R.cross=sigFacts(JSON.parse(JSON.stringify(PROD))).cross.map(c=>c.i);
  // Activity rows and every tap route
  renderActivityMode('');
  R.actHtml=document.querySelector('#v-history').innerHTML;
  R.tap=ACT_TAP.map(t=>t?{side:t.side, d:t.d, sym:t.sym, rej:!!t.rejected}:null);
  const routes=[];
  for(let n=0;n<ACT_TAP.length;n++){
    POS_SHEET=null; TRADE_KEY=null;
    if(ACT_TAP[n]) actTap(n);
    await tick();
    routes.push({n, pos:POS_SHEET, trade:TRADE_KEY, note:(TRADE_KEY||'').startsWith('note|')
      ? document.querySelector('#trade-sheet').innerHTML : null});
  }
  R.routes=routes;
  // no ledger: says so instead of guessing
  const keep=FILLS; FILLS=[]; POS_SHEET=null; TRADE_KEY=null; refreshFills=()=>{};
  actTap(ACT_TAP.findIndex(t=>t&&t.side==='SELL'&&!t.rejected)); await tick();
  R.noLedger={pos:POS_SHEET, html:document.querySelector('#trade-sheet').innerHTML}; FILLS=keep;
  // the trade sheet still shows what the engine saw (shared rows)
  TRADE_KEY='AAA|'+D(201);
  renderTradeSheet(JSON.parse(JSON.stringify(PROD)), 'AAA', D(201), null, 'AAA');
  R.trade=document.querySelector('#trade-sheet').innerHTML;
  R.days={sell:D(201), d130:D(130), d240:D(240), d200:D(200)};
  console.log('@@'+JSON.stringify(R)+'@@');
})()`, ctx);
"""


def run():
    node = shutil.which("node") or shutil.which(os.environ.get("MPS_NODE") or "")
    if not node:
        return None
    tmp = tempfile.mkdtemp(prefix="mps-tradecharts-")
    try:
        js = os.path.join(tmp, "h.js")
        io.open(js, "w", encoding="utf-8").write(_HARNESS)
        p = subprocess.run([node, js, PAGE], capture_output=True, timeout=120)
        out = p.stdout.decode("utf-8", "replace")
        assert p.returncode == 0 and "@@" in out, p.stderr.decode("utf-8", "replace")[-2500:]
        return json.loads(out.split("@@")[1])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t1_holding_chart(R):
    A, B, C, D_, d = R["AAA"], R["BBB"], R["CCC"], R["DDD"], R["days"]
    for want in ("Price", "200-day average", "50-day average", "Crossed the 200-day",
                 "Cut-loss (as it moved)", "Entry (you paid)", "Bought"):
        assert want in A["labels"], "holding chart lost %r: %r" % (want, A["labels"])
    # a long hold opens 20 sessions before the buy (bought at 150 -> from 130, 170 bars)
    assert B["first"] == d["d130"] and B["n"] == 170, (B["first"], B["n"])
    assert B["bought"] == [[20, 119.0]] and B["entry0"] == [20, 120.0], (B["bought"], B["entry0"])
    # a young one reaches back to 60 sessions (bought at 295 -> from 240)
    assert C["first"] == d["d240"] and C["n"] == 60 and C["bought"] == [[55, 149.0]], C["bought"]
    assert "only 5 daily closes" in C["html"], "young note missing"
    # AAA (bought at 260): the dots sit exactly on the crossings inside the window
    want = sorted(i - 240 for i in R["cross"] if i >= 240)
    assert [i for i, _ in (A["cross"] or [])] == want and want, (A["cross"], want)
    assert A["cut"][0][0] == 20, "the cut-loss line does not start on the buy day: %r" % A["cut"][:2]
    assert "What the engine saw when it bought" in A["html"] and "Oversold dip: RSI(3)" in A["html"]
    assert "Last crossed the 200-day" in A["html"] and "50-day vs 200-day" in A["html"]
    # bought by hand (no bot order ref): named for what it is
    assert "The signal close before your buy" in C["html"] and "What the engine saw" not in C["html"]
    # a same-day sell-out and re-buy: the old lot's exit is not drawn on the new holding
    assert D_["sold"] is None and D_["bought"] and len(D_["bought"]) == 1, (D_["sold"], D_["bought"])
    print("t1 the holding chart: every signal line, the window from 20 sessions before the buy "
          "(60 for a young one), crossings exactly, the lot's own fills only OK")


def t2_activity_taps(R):
    # newest first: CCC bought by hand, ADOPT CCC, ADOPT EEE, ZZZ buy (no fill), AAA sell
    # (refused), ZZZ buy (refused), HALT, AAA buy (held), EEE bought by hand (later sold),
    # AAA sell, AAA old buy (closed)
    sides = [t and (t["side"] + ("!" if t["rej"] else "")) for t in R["tap"]]
    assert sides == ["BUY", "ADOPT", "ADOPT", "BUY", "SELL!", "BUY!", None, "BUY", "BUY", "SELL", "BUY"], sides
    assert R["actHtml"].count('onclick="actTap(') == 10, "only trade rows are tappable"
    assert "order:1;flex:none" in R["actHtml"], "the Chart hint is not on the right edge"
    r = {x["n"]: x for x in R["routes"]}
    sell = "AAA|" + R["days"]["sell"]
    assert r[0]["pos"] == "CCC", "a hand buy still held does not open its holding"
    assert r[1]["pos"] == "CCC", "a held take-over does not open its holding"
    assert r[2]["trade"] and r[2]["trade"].startswith("EEE|"), "a sold take-over does not open its trade: %r" % r[2]
    assert r[3]["trade"] == "note|ZZZ" and "No fill is recorded" in r[3]["note"], r[3]
    assert r[4]["pos"] == "AAA", "a refused sell of a held stock should open the holding"
    assert r[5]["trade"] == "note|ZZZ" and "IB refused this order" in r[5]["note"], r[5]
    assert r[7]["pos"] == "AAA", "a buy still held does not open the holding"
    assert r[8]["trade"] == r[2]["trade"], "a hand buy later sold does not open its whole trade: %r" % r[8]
    assert r[9]["trade"] == sell, "a sell does not open its disposal: %r" % r[9]
    assert r[10]["trade"] == sell, "a closed buy does not open its whole trade: %r" % r[10]
    nl = R["noLedger"]
    assert nl["pos"] is None and "has not loaded yet" in nl["html"], "no ledger: should say so"
    assert "Oversold dip: RSI(3) under 25" in R["trade"] and "52-week high" in R["trade"], \
        "the trade sheet lost the engine-saw rows"
    print("t2 every Activity trade opens its holding or its whole trade; refused orders, a sold "
          "take-over, an unfilled order and a missing ledger each say so; notices stay untappable OK")


if __name__ == "__main__":
    R = run()
    if R is None:
        print("t1/t2 node not on PATH - the page's script was NOT run")
        print("ALL TRADE CHART TESTS EXCEPT THE PAGE SCRIPT (t1, t2: no node) - PARTIAL PASS")
    else:
        t1_holding_chart(R)
        t2_activity_taps(R)
        print("ALL TRADE CHART TESTS PASS")
