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
  * The stop hint gets adj AND px_ref through the .pos-alert attributes, and
    follows a dividend or split the bot has not applied yet with exactly the
    rule the bot uses (div_adjust.adjustment_since, ported; run side by side
    here). So an ex-dividend drop no longer shows a red SELL the bot will not
    make. With no px_ref the hint is exactly what it was.
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
import re
import shutil
import subprocess
import sys
import tempfile

import testenv                                      # first: /root paths to temp
testenv.isolate("mps-dash0921-")
import div_adjust                                   # noqa: E402
import flex_dividends                               # noqa: E402
testenv.assert_isolated()

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PAGE = os.path.join(ROOT, "docs", "index.html")


def _page(strip=True):
    t = io.open(PAGE, encoding="utf-8").read()
    return t.replace(" ", "") if strip else t


def _series(closes, start=1):
    return [["2026-09-%02d" % (start + i), c] for i, c in enumerate(closes)]


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


def t2_the_hint_is_fed_adj_and_px_ref():
    page = _page()
    assert "x.sig_entry*(x.adj||1)" in page, "t9 of test_div_adjust relies on this text"
    assert "adj:+b.adj||1," in page, "the row mapping lost adj"
    assert "px_ref:Array.isArray(b.px_ref)?b.px_ref:null," in page, "the row lost px_ref"
    assert 'data-adj="${x.adj==null?\'\':x.adj}"' in page, "the .pos-alert lost data-adj"
    assert "data-px-ref=" in page, "the .pos-alert lost data-px-ref"
    assert "adj:parseFloat(d.dataset.adj)||1," in page, "exitAlert is not given adj"
    assert "px_ref:Array.isArray(pxRef)?pxRef:null," in page, "exitAlert is not given px_ref"
    # the port must use the shared rule's own constants
    m = re.search(r"constADJ_AGREE_TOL=([^,]+),ADJ_NOISE_FLOOR=([^,]+),"
                  r"ADJ_MIN=([^,]+),ADJ_MAX=([^;]+);", page)
    assert m, "the page no longer declares its copy of div_adjust's constants"
    got = tuple(float(v) for v in m.groups())
    want = (div_adjust.ADJ_AGREE_TOL, div_adjust.ADJ_NOISE_FLOOR,
            div_adjust.ADJ_MIN, div_adjust.ADJ_MAX)
    assert got == want, "the page's copy of div_adjust's constants drifted: %r != %r" % (got, want)
    print("t2 adj and px_ref reach exitAlert; the port carries div_adjust's constants OK")


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
  R.adj=CASES.adj.map(c=>adjSince(c[0],c[1]));
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


def _adj_cases():
    """Pairs (ref, prices) for the parity run, covering every branch."""
    pre = _series([95.0, 96.0, 97.0, 98.0, 99.0, 100.0, 101.0, 102.0, 103.0, 104.0])
    ref = div_adjust.px_ref(pre)
    assert len(ref) == 3, ref
    scale = lambda s, f: [[d, round(v * f, 4)] for d, v in s]
    return [
        (ref, scale(pre, 0.995) + [["2026-09-11", 103.0]]),       # a dividend
        (ref, scale(pre, 0.5)),                                   # a 2-for-1
        (ref, pre),                                               # nothing happened
        ([], pre),                                                # no memory yet
        (None, pre),
        (ref, None),
        (ref, [r if r[0] != ref[1][0] else [r[0], r[1] * 0.95] for r in pre]),  # one bad bar
        (ref, [r for r in pre if r[0] == ref[0][0]]),             # one date is not evidence
        (ref, scale(pre, 1.00001)),                               # rounding noise
        (ref, scale(pre, 0.01)),                                  # implausible
        (ref, scale(pre, 0.02)),                                  # the band's edge, 50-for-1
        (ref, scale(pre, 50.0)),
        (ref, scale(pre, 51.0)),
        (ref, [[d, 0] if d == ref[0][0] else [d, v * 0.99] for d, v in pre]),  # a zero close
        (ref, [[d, "x"] for d, v in pre]),                        # unreadable closes
        ([[ref[0][0], 0], [ref[1][0], "bad"], ref[2]], scale(pre, 0.99)),
        ([[ref[0][0], ref[0][1]], [ref[1][0], ref[1][1]]], scale(pre, 0.97)),
    ]


def _exit_cases():
    pre = _series([95.0, 96.0, 97.0, 98.0, 99.0, 100.0, 101.0, 102.0, 103.0, 104.0])
    ref = div_adjust.px_ref(pre)
    post = [[d, round(v * 0.995, 4)] for d, v in pre] + [["2026-09-11", 99.5]]
    f = div_adjust.adjustment_since(ref, post)
    assert f is not None and abs(f - 0.995) < 1e-6, f
    split = [[d, round(v * 0.9, 4)] for d, v in pre] + [["2026-09-11", 96.0]]
    card = {"sma200": 50.0, "atr": 1.0}
    base = {"sym": "T", "entry": 90.0, "sig_entry": 90.0, "adj": 1.0, "stop": 99.6}
    return [
        # 0: ex-dividend close 99.5 under a 99.6 stop: the bot moves it to ~99.10
        dict(x=dict(base, px_ref=ref), last=99.5, product={"card": card, "prices": post}),
        # 1: the same without px_ref (older publisher): exactly today's red SELL
        dict(x=dict(base, px_ref=None), last=99.5, product={"card": card, "prices": post}),
        # 2: a genuine fall through even the rescaled stop still sells
        dict(x=dict(base, px_ref=ref), last=98.0, product={"card": card, "prices": post}),
        # 3: the tighten test anchors on sig_entry x adj x f: 100 x 0.9 = 90, so
        #    at 96 the trail is 96 - 2*1 = 94 (without f it would be 92.5)
        dict(x={"sym": "S", "entry": 95.0, "sig_entry": 100.0, "adj": 1.0, "stop": 80.0,
                "px_ref": ref}, last=96.0, product={"card": card, "prices": split}),
        dict(x={"sym": "S", "entry": 95.0, "sig_entry": 100.0, "adj": 1.0, "stop": 80.0,
                "px_ref": None}, last=96.0, product={"card": card, "prices": split}),
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
    adj = _adj_cases()
    cases = {"adj": adj, "exit": _exit_cases(), "hist": _hist(), "lots": _lots(),
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

    # --- the port agrees with div_adjust on every case, to the last bit
    for (ref, prices), got in zip(adj, R["adj"]):
        want = div_adjust.adjustment_since(ref, prices)
        assert (want is None and got is None) or (want is not None and got == want), \
            "page adjSince %r != div_adjust %r for ref=%r" % (got, want, ref)
    print("t5a the page's adjSince matches div_adjust.adjustment_since on %d cases OK"
          % len(adj))

    e = R["exit"]
    assert e[0] and e[0]["level"] != "sell", "an ex-dividend drop shows a SELL: %r" % e[0]
    assert "99.1" in e[0]["msg"] and "99.6" in e[0]["msg"], e[0]
    assert e[1] == {"level": "sell", "msg": "⚠ SELL — your cut-loss 99.6 was hit."}, \
        "without px_ref the hint must be exactly as before: %r" % e[1]
    assert e[2] and e[2]["level"] == "sell" and "99.1" in e[2]["msg"], \
        "a genuine fall through the rescaled stop must still say SELL: %r" % e[2]
    assert e[3] and e[3]["level"] == "trail" and "~94 " in e[3]["msg"], e[3]
    assert e[4] and e[4]["level"] == "trail" and "~92.5 " in e[4]["msg"], e[4]
    print("t5b ex-dividend drop: no SELL, stop shown 99.60 -> ~99.1; no px_ref: as before OK")

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
    t2_the_hint_is_fed_adj_and_px_ref()
    t3_the_other_fixes_are_wired()
    t4_flex_rows_carry_the_ex_date()
    t5_the_page_itself_computes_the_right_numbers()
    print("ALL DASHBOARD 0921 TESTS PASS")
