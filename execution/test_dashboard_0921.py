#!/usr/bin/env python3
"""Golden tests: the dashboard fixes of board review 2026-09-21.

Run from this directory:  python test_dashboard_0921.py

docs/index.html is one page with one inline script, and most of what it gets
wrong is arithmetic nobody sees until a number on the phone is wrong. So where
node is on the PATH this suite RUNS the page's own script (in a node vm, with a
stub DOM and a stub fetch) and checks the numbers; without node it still checks
the page text, and says the rest was skipped.

What must stay true:
  * EARMARK. No P&L figure adds the day's change in earmarked cash ("exc")
    back. Published NetLiq is already net of it, and the owner's monthly
    pass-through (GBP deposited, converted to HKD and earmarked the same day,
    withdrawn early the next month, neither leg in flows) leaves that figure
    flat - adding the change back painted +18,559 then -18,559 that was never
    made or lost. Calendar, day sheet, vs-S&P card and the live profit alike.
  * The stop hint is the bot's own rule, unchanged: the published stop as it
    stands, and the tighten test anchored on the SIGNAL price. The cut-loss
    does not follow dividends or splits - the operator decided against it.
  * A card's dividends are gated on the EX-date when the row has one (a lot
    bought on or after it is not entitled), on the pay date for older rows,
    and a partly sold lot claims none. flex_dividends records ex_date.
  * The Actions tab quotes the header's own headline, not figures typed once.
  * The 5-minute tick re-pulls data.json before the live state.
  * One failed dividend-ledger fetch no longer hides dividends for the session.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

import testenv                                      # first: /root paths to temp
testenv.isolate("mps-dash0921-")
import flex_dividends                               # noqa: E402
testenv.assert_isolated()

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PAGE = os.path.join(ROOT, "docs", "index.html")


def _page(strip=True):
    t = io.open(PAGE, encoding="utf-8").read()
    return t.replace(" ", "") if strip else t


# ---------------------------------------------------------------- page text --
def t1_no_earmark_term_in_any_pnl_figure():
    page = _page()
    for bad in ("dExc", "+out.earmark", "out.earmark+=", "nl+f-", "prev+fl-",
                "-fl+", "+((z.exc||0)-(a.exc||0))"):
        assert bad not in page, "an earmark term is back in the P&L arithmetic: %r" % bad
    assert "consttotal=cur.nl-prv.nl-fl;" in page, "the day sheet total changed shape"
    assert "pnl[d]=s[i].nl-prev-fl;" in page, "the calendar day P&L changed shape"
    assert "constbase=pts[i-1].nl+f;" in page, "the vs-S&P base changed shape"
    # the earmark is still SHOWN: the net-worth line, its own card, the sheet
    assert "HKDearmarked" in page and "Earmarkedcashchangedby" in page
    print("t1 no calendar, card or sheet figure adds the earmark change back OK")


def t2_the_stop_hint_is_the_bots_own_rule():
    page = _page()
    # ib_bot.py  k = 2.0 if price >= st["entry"] + 1.5*atr else 3.5
    assert "constanchor=x.sig_entry!=null?x.sig_entry:x.entry;" in page, \
        "the stop hint no longer anchors on the signal price, as the bot does"
    # the cut-loss does not follow dividends or splits, in the bot or here
    for gone in ("adjSince", "data-adj", "px_ref", "pxRef", "x.adj"):
        assert gone not in page, "a dividend rescale of the stop is back: %r" % gone
    print("t2 the stop hint is the bot's own rule: signal-price anchor, stop as published OK")


def t3_the_other_fixes_are_wired():
    page = _page()
    assert "setInterval(liveTick,5*60*1000)" in page, "the 5-minute tick no longer pulls prices"
    assert "win52.5%" not in page and "CAGR30.8%" not in page, "headline figures typed in again"
    assert "win${h.win},CAGR${h.cagr},maxDD${h.maxdd}" in page, "Actions tab stopped reading the headline"
    assert "if(!r.ok)thrownewError('HTTP'+r.status);" in page, "loadDivs ignores r.ok again"
    assert "if(DIVS_OK)returnDIVS;" in page, "loadDivs latches on a failed fetch again"
    assert "if(!DIVS_OK)_divsRequested=false;" in page, "ensurePosLedgers never retries"
    print("t3 tick, headline and dividend-ledger retry are wired OK")


def t4_flex_rows_carry_the_ex_date():
    xml = """<FlexQueryResponse><FlexStatements><FlexStatement><CashTransactions>
      <CashTransaction type="Dividends" symbol="LLY" conid="1" currency="USD"
        amount="3.00" settleDate="20260910" exDate="20260815" transactionID="1" />
      <CashTransaction type="Withholding Tax" symbol="LLY" conid="1" currency="USD"
        amount="-0.45" settleDate="2026-09-10" exDate="2026/08/15" transactionID="2" />
      <CashTransaction type="Dividends" symbol="MS" conid="2" currency="USD"
        amount="7.40" settleDate="20260715" transactionID="3" />
    </CashTransactions></FlexStatement></FlexStatements></FlexQueryResponse>"""
    rows = {r["id"]: r for r in flex_dividends.parse_cash_transactions(xml)}
    assert rows["1"]["ex_date"] == "2026-08-15", rows["1"]
    assert rows["2"]["ex_date"] == "2026-08-15", "exDate is parsed like the other dates"
    assert rows["1"]["date"] == "2026-09-10", "date stays the PAY date"
    assert rows["3"]["ex_date"] is None, "no Ex Date column: None, readers use the pay date"
    print("t4 flex rows carry ex_date beside the pay date OK")


# ------------------------------------------------------ the page's own script --
_HARNESS = r"""
const fs=require('fs'), vm=require('vm');
const [pagePath, casesPath]=process.argv.slice(2);
process.on('unhandledRejection',()=>{});          // boot() noise against the stubs
const html=fs.readFileSync(pagePath,'utf8');
const src=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const els={};
const fake=()=>({innerHTML:'',textContent:'',style:{},dataset:{},
  classList:{add(){},remove(){},contains(){return false}},
  addEventListener(){},setAttribute(){},getAttribute(){return null},remove(){},
  appendChild(){},querySelector(){return null},querySelectorAll(){return []},
  closest(){return null},parentElement:null});
const store={};
const ctx={console, setTimeout, clearTimeout, setInterval(){}, clearInterval(){},
  document:{querySelector:s=>(els[s]=els[s]||fake()), querySelectorAll:()=>[],
    getElementById:id=>(els['#'+id]=els['#'+id]||fake()), createElement:()=>fake(), body:fake()},
  localStorage:{getItem:k=>(k in store?store[k]:null), setItem:(k,v)=>{store[k]=String(v)}},
  addEventListener(){}, location:{reload(){}}, scrollY:0, alert(){}, confirm:()=>false,
  prompt:()=>null, navigator:{}, open(){}};
ctx.window=ctx;
ctx.fetch=(u,o)=>ctx.ROUTE?ctx.ROUTE(String(u),o)
  :Promise.resolve({ok:false,status:404,json:async()=>({}),text:async()=>''});
vm.createContext(ctx);
vm.runInContext(src, ctx);
ctx.CASES=JSON.parse(fs.readFileSync(casesPath,'utf8'));
const test=String.raw`(async()=>{
  const R={};
  const resp=(st,body)=>({ok:st>=200&&st<300,status:st,
    json:async()=>JSON.parse(body),text:async()=>body});
  const tick=()=>new Promise(r=>setTimeout(r,0));
  await tick(); await tick();                     // let boot() settle on the stubs
  R.exit=[];
  for(const c of CASES.exit){
    globalThis.ROUTE=u=>Promise.resolve(u.startsWith('products/')
      ?resp(200,JSON.stringify(c.product)):resp(404,''));
    R.exit.push(await exitAlert(c.x, c.last));
  }
  const h=CASES.hist;
  const closes=h.series.map((r,i)=>[r.d,500+i]);
  const b=benchCompute(h.series,h.flows,closes,0,'2026-09-30T22:00:00Z');
  const b2=benchCompute(h.series.slice(0,2),h.flows,closes,0,'2026-09-30T22:00:00Z');
  R.bench={profit:b.profit,you:b.you,youDD:b.youDD,profit2:b2.profit,you2:b2.you};
  R.acct=acctToNow(h.series,h.flows,h.series[0].d,null);
  R.acctLive=acctToNow(h.series.slice(0,2),h.flows,h.series[0].d,{d:'2026-08-29',nl:200000});
  R.days=h.series.slice(1).map(r=>dayBreakdown(r.d,h,null,[],[]).total);
  CAL=h; CAL_YM='2026-08'; renderCal();
  R.cal=document.querySelector('#v-calendar').innerHTML;
  R.lots=[];
  for(const c of CASES.lots){
    BOT={positions:[{symbol:c.sym, ib_symbol:c.sym}]}; POSROWS=[];
    FILLS=c.fills; DIVS=c.divs;
    R.lots.push(lotDividends({sym:c.sym}));
  }
  DATA={headline:CASES.headline,actions:[],index:[],universe_count:3};
  renderActions(); R.act=document.querySelector('#v-actions').innerHTML;
  DATA={actions:[],index:[],universe_count:3};
  renderActions(); R.act0=document.querySelector('#v-actions').innerHTML;
  DIVS=null; DIVS_OK=false; FILLS=[]; BOT=null;
  let divResp=resp(503,'busy');
  globalThis.ROUTE=u=>Promise.resolve(u.includes('dividends_ledger')?divResp:resp(404,''));
  await loadDivs(); R.div1={n:DIVS.length, ok:DIVS_OK};
  _divsRequested=false; ensurePosLedgers(); await tick(); await tick();
  R.rearm=_divsRequested;
  divResp=resp(200,'{"id":"a","symbol":"X","amount":1}\n{"id":"a","symbol":"X","amount":2}\n{"id":"b","symbol":"X","amount":3}\n');
  await loadDivs(); R.div2={n:DIVS.length, ok:DIVS_OK, sum:DIVS.reduce((t,r)=>t+r.amount,0)};
  DATA={index:[{sym:'OLD',price:1}]}; IDX=DATA.index;
  globalThis.ROUTE=u=>Promise.resolve(u.startsWith('data.json')
    ?resp(200,JSON.stringify({index:[{sym:'NEW',price:2}]})):resp(404,''));
  await liveTick(); R.tick1=IDX.map(o=>o.sym);
  globalThis.ROUTE=u=>Promise.resolve(u.startsWith('data.json')?resp(500,'oops'):resp(404,''));
  await liveTick(); R.tick2=IDX.map(o=>o.sym);
  globalThis.ROUTE=u=>Promise.reject(new Error('offline'));
  await liveTick(); R.tick3=IDX.map(o=>o.sym);
  return R;
})()`;
vm.runInContext(test, ctx).then(R=>{ process.stdout.write('@@'+JSON.stringify(R)+'@@'); process.exit(0); },
  e=>{ console.error(e&&e.stack||e); process.exit(1); });
"""


def _exit_cases():
    card = {"sma200": 50.0, "atr": 1.0}
    return [
        # 0: a close under the published stop: SELL at that stop, as published
        dict(x={"sym": "T", "entry": 90.0, "sig_entry": 90.0, "stop": 99.6},
             last=99.5, product={"card": card}),
        # 1: the tighten test anchors on the SIGNAL price (90), not the cost
        #    (95): 96 clears 90 + 1.5, so k is 2 and the trail 94
        dict(x={"sym": "S", "entry": 95.0, "sig_entry": 90.0, "stop": 80.0},
             last=96.0, product={"card": card}),
        # 2: a signal price of 100 is not cleared: k stays 3.5, trail 92.5
        dict(x={"sym": "S", "entry": 95.0, "sig_entry": 100.0, "stop": 80.0},
             last=96.0, product={"card": card}),
    ]


def _hist():
    # the documented pass-through: 18,559 lands and is earmarked the same day,
    # leaves (still earmarked) on 1 Sep; the only real move is +1,000 on 31 Aug
    return {"series": [{"d": "2026-08-27", "nl": 200000},
                       {"d": "2026-08-28", "nl": 200000, "exc": 18559},
                       {"d": "2026-08-31", "nl": 201000, "exc": 18559},
                       {"d": "2026-09-01", "nl": 201000, "exc": 0}],
            "flows": []}


def _lots():
    b = lambda d, q: {"symbol": "PANW", "sec_type": "STK", "side": "BOT", "qty": q, "date": d}
    s = lambda d, q: {"symbol": "PANW", "sec_type": "STK", "side": "SLD", "qty": q, "date": d}
    div = lambda pay, ex, amt=5.0: {"id": "d" + pay, "symbol": "PANW", "type": "dividend",
                                    "amount": amt, "date": pay, "ex_date": ex}
    wht = lambda pay, amt=-0.75: {"id": "w" + pay, "symbol": "PANW", "type": "wht",
                                  "amount": amt, "date": pay}
    return [
        # 0: sold after the ex-date, re-bought before the pay date: not this lot's
        dict(sym="PANW", fills=[b("2026-08-01", 10), s("2026-08-20", 10), b("2026-08-21", 10)],
             divs=[div("2026-09-01", "2026-08-15"), wht("2026-09-01")]),
        # 1: held through the ex-date: net of the tax that followed it
        dict(sym="PANW", fills=[b("2026-08-01", 10)],
             divs=[div("2026-09-01", "2026-08-15"), wht("2026-09-01")]),
        # 2: an older row with no ex_date falls back to the pay date, as before
        dict(sym="PANW", fills=[b("2026-08-21", 10)],
             divs=[div("2026-09-01", None), wht("2026-09-01")]),
        # 3: bought ON the ex-date: not entitled
        dict(sym="PANW", fills=[b("2026-08-15", 10)],
             divs=[div("2026-09-01", "2026-08-15"), wht("2026-09-01")]),
        # 4: trimmed 25 -> 5 since entry: claim nothing
        dict(sym="PANW", fills=[b("2026-08-01", 25), s("2026-08-10", 20)],
             divs=[div("2026-09-01", "2026-07-15")]),
    ]


def _node():
    return shutil.which("node")


def t5_the_page_itself_computes_the_right_numbers():
    node = _node()
    if not node:
        print("t5 node not on PATH - the page's script was NOT run (text checks above only)")
        return
    cases = {"exit": _exit_cases(), "hist": _hist(), "lots": _lots(),
             "headline": {"win": "51.6%", "cagr": "30.1%", "maxdd": "-28.5%",
                          "basis": "the bot's own exit rules"}}
    tmp = tempfile.mkdtemp(prefix="mps-dash0921-node-")
    try:
        js = os.path.join(tmp, "harness.js")
        cj = os.path.join(tmp, "cases.json")
        io.open(js, "w", encoding="utf-8").write(_HARNESS)
        io.open(cj, "w", encoding="utf-8").write(json.dumps(cases))
        p = subprocess.run([node, js, PAGE, cj], capture_output=True, timeout=120)
        out = p.stdout.decode("utf-8", "replace")
        assert p.returncode == 0 and "@@" in out, \
            "the page script failed under node:\n" + p.stderr.decode("utf-8", "replace")[-3000:]
        R = json.loads(out.split("@@")[1])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    e = R["exit"]
    assert e[0] == {"level": "sell", "msg": "⚠ SELL — your cut-loss 99.6 was hit."}, e[0]
    assert e[1] == {"level": "trail",
                    "msg": "Raise your stop to ~94 (chandelier trail moved up)."}, e[1]
    assert e[2] == {"level": "trail",
                    "msg": "Raise your stop to ~92.5 (chandelier trail moved up)."}, e[2]
    print("t5b the stop hint: SELL at the published stop, trail anchored on the signal price OK")

    b = R["bench"]
    assert b["profit"] == 1000, "vs-S&P profit counts the pass-through: %r" % b
    assert b["you"][1] == 0 and b["you"][3] == b["you"][2], b["you"]
    assert b["youDD"] == 0, "the withdrawal of earmarked money reads as a dip: %r" % b
    assert b["profit2"] == 0 and b["you2"] == [0, 0], "the deposit day reads as a gain: %r" % b
    assert R["acct"]["profit"] == 1000 and R["acct"]["dd"] == 0, R["acct"]
    assert R["acctLive"]["profit"] == 0, R["acctLive"]
    assert R["days"] == [0, 1000, 0], "day sheet totals count the earmark: %r" % R["days"]
    assert "18,559" not in R["cal"], "the calendar paints the pass-through as P&L"
    assert "+1,000 HKD" in R["cal"], "the month total should be the one real move"
    print("t5c pass-through 18,559: calendar, sheet, card and live profit all read 0 OK")

    L = R["lots"]
    assert L[0] is None, "a dividend earned by the sold lot was credited to the new one: %r" % L[0]
    assert L[1] and abs(L[1]["net"] - 4.25) < 1e-9 and L[1]["n"] == 1, L[1]
    assert L[2] and abs(L[2]["net"] - 4.25) < 1e-9, "old rows must fall back to the pay date"
    assert L[3] is None, "a lot bought on the ex-date is not entitled: %r" % L[3]
    assert L[4] is None, "a trimmed lot must claim nothing: %r" % L[4]
    print("t5d dividends gated on the ex-date, pay date for old rows, trims claim none OK")

    assert "win 51.6%, CAGR 30.1%, maxDD -28.5%" in R["act"], "Actions tab ignores the headline"
    assert "52.5%" not in R["act"] and "30.8%" not in R["act"]
    assert "(figures in the header above)" in R["act0"], "no headline: invent nothing"
    print("t5e the Actions tab quotes the header's headline OK")

    assert R["div1"] == {"n": 0, "ok": False}, R["div1"]
    assert R["rearm"] is False, "ensurePosLedgers did not re-arm after a failed load"
    assert R["div2"] == {"n": 2, "ok": True, "sum": 5}, R["div2"]
    print("t5f a failed dividend fetch is retried, and the retry lands OK")

    assert R["tick1"] == ["NEW"], "the 5-minute tick did not pick up the new prices"
    assert R["tick2"] == ["NEW"] and R["tick3"] == ["NEW"], "a failed tick dropped the data"
    print("t5g the 5-minute tick refreshes prices and keeps them on a failure OK")


if __name__ == "__main__":
    t1_no_earmark_term_in_any_pnl_figure()
    t2_the_stop_hint_is_the_bots_own_rule()
    t3_the_other_fixes_are_wired()
    t4_flex_rows_carry_the_ex_date()
    t5_the_page_itself_computes_the_right_numbers()
    print("ALL DASHBOARD 0921 TESTS PASS")
