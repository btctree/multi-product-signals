#!/usr/bin/env python3
"""Golden test: the Positions tab's "Growth vs S&P 500" card renders.

Run from this directory:  python test_growth_card.py

On 2026-10-23 the card's table header called esc(), which exists only inside
renderActivityMode. Every other suite passed, because none of them renders the
card - the page's own preview was the only thing that showed it blank. This
runs the real page script under node and draws the card from a small history,
so a ReferenceError (or any throw) in renderBench fails here instead.

2026-10-26 (owner's decisions): the card gained range buttons All / 6M / 3M /
2M / 1M on a row of their own, a show/hide switch on every table row (stored
on the phone, kept through the five-minute rebuild; the table is the legend),
and two SIMULATED lines - display only, never traded - built from the
account's own daily returns: "Sim: 70% you + 30% crypto trend" and "Sim: you +
M1 Bitcoin slot", read from docs/sim_lines.json (engine/build_sim_lines.py).
What must stay true:
  * no sims file: no Sim rows, nothing throws;
  * the 70/30 line is 0.70 x the account's return + 0.30 x the sleeve's, each
    day; the M1 line IS the account while the slot is out, and the stock book
    is scaled down (never added to) while it is in;
  * a Sim line stops at complete_through and is compared with the account on
    that same day, and says "to <day>";
  * the account line and the profit do not move by one cent with sims added.
The node parts are skipped (and say so) where node is not installed; the
wiring checks in t4 run everywhere.
"""
import io
import json
import os
import shutil
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PAGE = os.path.join(ROOT, "docs", "index.html")

_HARNESS = r"""
const fs=require('fs'), vm=require('vm');
const [pagePath]=process.argv.slice(2);
process.on('unhandledRejection',()=>{});          // boot() and the chart loader, against stubs
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
ctx.fetch=()=>Promise.resolve({ok:false,status:404,json:async()=>({}),text:async()=>''});
vm.createContext(ctx);
vm.runInContext(src, ctx);
vm.runInContext(String.raw`(async()=>{
  await new Promise(r=>setTimeout(r,0)); await new Promise(r=>setTimeout(r,0));
  const R={};
  const card=()=>document.querySelector('#bench-card').innerHTML;
  const draw=k=>{ try{ renderBench(); }catch(e){ R[k+'Err']=String(e&&e.stack||e); } return card(); };
  const days=['2026-10-19','2026-10-20','2026-10-21','2026-10-22','2026-10-23'];
  const hist={series:days.map((d,i)=>({d, nl:200000+i*1000-(i===2?3000:0), exc:0})), flows:[]};
  const px=(b,s)=>days.map((d,i)=>[d, b+i*s]);
  BENCH={hist, bench:{prices:px(500,2), generated_at:'2026-10-24T00:00:00Z'},
         others:BENCH_OTHERS.map((o,i)=>({...o, prices:px(300+i*100,1)}))};
  BOT=null; BENCH_WIN=0;
  R.html=draw('plain');                                    // no sims file
  const sims=ct=>({complete_through:ct, weights:{trend:0.3, m1:0.2},
    trend:{series:days.map((d,i)=>[d, 1+i*0.01])},
    m1:{fetched_at:'2026-10-24T09:00:00Z', series:days.map((d,i)=>[d, 1+i*0.02, i>=3?1:0])}});
  BENCH.sims=sims('2026-10-23'); R.sims=draw('sims');
  BENCH.sims=sims('2026-10-21'); R.cut=draw('cut');
  BENCH.sims=sims('2026-10-23');
  BENCH_WIN=180; R.six=draw('six'); BENCH_WIN=0;
  benchToggle('sim_m1'); R.store=localStorage.getItem('mps_bench_hide');
  R.hidden=draw('hidden');                                 // the rebuild keeps it
  benchToggle('<img src=x>'); R.store2=localStorage.getItem('mps_bench_hide');
  benchToggle('sim_m1'); R.store3=localStorage.getItem('mps_bench_hide');
  // the five-minute history refresh rebuilds BENCH: the sims must come along
  globalThis.fetch=()=>Promise.resolve({ok:true, json:async()=>hist});
  await refreshBenchHist(); R.kept=!!(BENCH&&BENCH.sims&&BENCH.sims.trend);
  // pure arithmetic, on days with known returns
  const s5=days.map((d,i)=>({d, nl:100000*Math.pow(1.01,i)}));
  const cl=days.map((d,i)=>[d,500+i]), g='2026-10-24T00:00:00Z';
  const tr=days.map((d,i)=>[d,Math.pow(1.10,i)]);
  const m1=on=>days.map((d,i)=>[d,Math.pow(1.05,i),on?1:0]);
  const A=benchCompute(s5,[],cl,0,g,[],{complete_through:'2026-10-23',trend:{series:tr},m1:{series:m1(false)}});
  const N=benchCompute(s5,[],cl,0,g,[],null);
  const C=benchCompute(s5,[],cl,0,g,[],{complete_through:'2026-10-21',trend:{series:tr},m1:{series:m1(true)}});
  const L=benchCompute(s5,[],cl,0,g,[],{trend:{series:tr.slice(1)}});   // no first day: no line
  R.ar={you:A.you, youN:N.you, profit:[A.profit,N.profit], sims:A.sims, simsN:N.sims, cut:C.sims,
        late:L.sims};
  console.log('@@'+JSON.stringify(R)+'@@');
})()`, ctx);
"""


def _run():
    """The harness result, or None when node is not installed."""
    node = shutil.which("node") or shutil.which(os.environ.get("MPS_NODE") or "")
    if not node:
        return None
    tmp = tempfile.mkdtemp(prefix="mps-growth-node-")
    try:
        js = os.path.join(tmp, "h.js")
        io.open(js, "w", encoding="utf-8").write(_HARNESS)
        p = subprocess.run([node, js, PAGE], capture_output=True, timeout=120)
        out = p.stdout.decode("utf-8", "replace")
        assert p.returncode == 0 and "@@" in out, \
            "the page script failed under node:\n" + p.stderr.decode("utf-8", "replace")[-3000:]
        return json.loads(out.split("@@")[1])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t1_the_card_renders(R):
    if R is None:
        print("t1 node not on PATH - the page's script was NOT run")
        return False
    for k in ("plainErr", "simsErr", "cutErr", "sixErr", "hiddenErr"):
        assert k not in R, "renderBench threw (%s):\n%s" % (k, R[k])
    h = R["html"]
    for want in ("Growth vs S&amp;P 500", "Growth vs S&P 500"):
        if want in h:
            break
    else:
        raise AssertionError("the card did not draw: %r" % h[:300])
    for row in ("This account", "S&P 500", "Dow Jones", "Nasdaq-100", "Worst dip<br>to 23 Oct"):
        assert row in h, "the table lost %r" % row
    # five ranges, 90 and 30 kept
    for lab, n in (("All", 0), ("6M", 180), ("3M", 90), ("2M", 60), ("1M", 30)):
        assert 'onclick="benchWin(%d)">%s</button>' % (n, lab) in h, "range button %s lost" % lab
    # no sims file: no Sim rows, no Sim footnote
    assert "Sim:" not in h and "Simulated" not in h, "Sim rows drawn with no sims file"
    print("t1 the growth card renders its table: account, S&P 500, Dow Jones, Nasdaq-100, "
          "five ranges, no Sim rows without the file OK")
    return True


def t2_sim_rows_toggles_and_the_whole_history_note(R):
    if R is None:
        print("t2 node not on PATH - skipped")
        return
    s = R["sims"]
    assert "Simulated — not real money, not traded" in s, "the SIMULATED group header is gone"
    for name in ("Sim: 70% you + 30% crypto trend", "Sim: you + M1 Bitcoin slot"):
        assert name in s, "missing Sim row %r" % name
    assert s.index("Nasdaq-100") < s.index("Simulated —") < s.index("Sim: 70%"), \
        "the Sim rows must sit under their own header, after the real lines"
    assert "not real money and were never traded" in s and "record fetched 2026-10-24" in s, \
        "the footnote no longer says what the Sim lines are"
    for key in ("acct", "spy", "DIA", "QQQ", "sim_crypto", "sim_m1"):
        assert ('<tr data-bk="%s" onclick="benchToggle(\'%s\')" style="cursor:pointer">'
                % (key, key)) in s, "row %s is not its line's switch" % key
    # a line that stops early says so and is compared on ITS last day
    c = R["cut"]
    assert c.count("to 21 Oct</span>") == 2, "the cut Sim rows do not say where they stop"
    ra = [201000 / 200000 - 1, 199000 / 201000 - 1]
    idx = (1 + .7 * ra[0] + .3 * (1.01 / 1.00 - 1)) * (1 + .7 * ra[1] + .3 * (1.02 / 1.01 - 1))
    you2 = (199000 / 200000 - 1) * 100
    gap = you2 - (idx - 1) * 100
    want = ("ahead " if gap >= 0 else "behind ") + "%.1f%%" % abs(gap)
    row = c[c.index('data-bk="sim_crypto"'):]
    row = row[:row.index("</tr>")]
    assert want in row, "70/30 compared on the wrong day: want %r in %r" % (want, row)
    # 6M on a five-day history is the whole of it, and says so
    assert "so 6M shows all of it." in R["six"] and "history starts 19 Oct" in R["six"]
    assert "shows all of it" not in R["sims"], "the All window claims to be a cut"
    # the switch: stored, kept by the rebuild, only for known keys
    assert R["store"] == '["sim_m1"]', R["store"]
    assert ('<tr data-bk="sim_m1" onclick="benchToggle(\'sim_m1\')" '
            'style="cursor:pointer;opacity:.45">') in R["hidden"], "the rebuild lost the hidden row"
    assert '<tr data-bk="sim_crypto" onclick="benchToggle(\'sim_crypto\')" style="cursor:pointer">' \
        in R["hidden"], "hiding one line dimmed another"
    assert R["store2"] == '["sim_m1"]', "a key that is not a line was stored: %r" % R["store2"]
    assert R["store3"] == "[]", R["store3"]
    assert R["kept"], "the five-minute history refresh dropped the Sim lines"
    print("t2 Sim rows under a SIMULATED header, each row a stored switch the rebuild keeps, "
          "a cut line compared on its own day, 6M names the whole history OK")


def t3_the_sim_arithmetic(R):
    if R is None:
        print("t3 node not on PATH - skipped")
        return
    a = R["ar"]
    # the account and its profit do not move with the Sim lines
    assert a["you"] == a["youN"] and a["profit"][0] == a["profit"][1], a
    assert a["simsN"] == [] and a["late"] == [], "a Sim line drawn without its file or first day"
    sims = {s["key"]: s for s in a["sims"]}
    # account +1%/day, sleeve +10%/day: 0.7 x 1% + 0.3 x 10% = 3.7% a day
    for i, v in enumerate(sims["sim_crypto"]["data"]):
        assert abs(v - (1.037 ** i - 1) * 100) < 1e-9, (i, v)
    # M1 out every day: the line IS the account, though the slot's own equity moved
    assert all(abs(x - y) < 1e-9 for x, y in zip(sims["sim_m1"]["data"], a["you"])), sims["sim_m1"]
    # M1 in, cut at 21 Oct: 0.8 x 1% + 0.2 x 5% a day, then nothing drawn
    cut = {s["key"]: s for s in a["cut"]}
    m = cut["sim_m1"]
    assert m["data"][3:] == [None, None] and m["last"] == 2 and m["cut"] and m["to"] == "2026-10-21"
    assert abs(m["end"] - (1.018 ** 2 - 1) * 100) < 1e-9, m
    assert cut["sim_crypto"]["data"][3:] == [None, None]
    print("t3 70/30 = 0.7 x account + 0.3 x sleeve a day (3.7%), M1 out = the account, M1 in "
          "scales the book to 80%, both stop at complete_through, account unchanged OK")


def t4_the_wiring():
    """Text checks that run without node: the parts a refactor could quietly
    drop while every number above still came out right."""
    page = io.open(PAGE, encoding="utf-8").read().replace(" ", "")
    flat = page.replace("\n", "")
    assert "BENCH={hist:h,bench:BENCH.bench,others:BENCH.others,sims:BENCH.sims};" in flat, \
        "refreshBenchHist no longer carries the Sim lines through the five-minute refresh"
    assert "constBENCH_SIM_URL='sim_lines.json';" in page, "the Sim file must be same-origin"
    assert "if(!BENCH_KEYS.includes(k))return;" in page, "benchToggle takes keys that are not lines"
    assert "plugins:{legend:{display:false}," in page, "the Chart.js legend is back beside the table"
    assert "d.hidden=BENCH_HIDE.has(d.benchKey);" in page, "the rebuilt chart forgets hidden lines"
    for k in ("mps_bench_hide", "mps_bench_win"):
        assert page.count("'" + k + "'") == 2, "%s must be read and written once each" % k
        for line in page.split("\n"):
            if "'" + k + "'" in line:
                assert "try{" in line, "a %s storage access outside try: %r" % (k, line)
    assert "borderDash:[6,3,1,3]" in page, "the Sim lines lost their dash-dot"
    assert "#bench-card:empty{min-height:720px}" in page
    print("t4 wiring: Sims carried through the refresh, same-origin file, whitelisted keys, "
          "legend off, hidden lines replayed, storage in try OK")


if __name__ == "__main__":
    R = _run()
    ran = t1_the_card_renders(R)
    t2_sim_rows_toggles_and_the_whole_history_note(R)
    t3_the_sim_arithmetic(R)
    t4_the_wiring()
    print("ALL GROWTH CARD TESTS PASS" if ran else
          "ALL GROWTH CARD TESTS EXCEPT THE PAGE SCRIPT (t1-t3: no node) - PARTIAL PASS")
