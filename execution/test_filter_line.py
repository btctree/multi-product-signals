#!/usr/bin/env python3
"""Golden test: the "Sim: you with market filter (shadow)" line on the
Positions tab's Growth vs S&P 500 card.

Run from this directory:  python test_filter_line.py

The owner asked on 2026-10-27 for one more line on the card: the account's own
time-weighted growth with the bot BUYs the 100-day market filter would have
skipped taken out, their money left as idle cash. engine/build_sim_lines.py
publishes c, the skipped lots' value less cost in HKD for each day, in
docs/sim_lines.json "filter"; the page draws NetLiq - c with the account's own
arithmetic. What must stay true:

  * with c = 0 every day the line IS the account line, to the last digit -
    money paid in and taken out handled the same way, the same days;
  * one skipped lot that gained HK$1,000 over a flat HK$200,000 account puts
    the filter line 0.5% below the account at the end (1,000 / 200,000), and
    the table says the account is ahead by 0.5%;
  * it runs on the account's own days: the Bitcoin lines' complete_through
    does not cut it; a day the file has no figure for, or a figure that is
    not a number, ends it there ("to <day>") - never a guess;
  * its row is a stored show/hide switch like the others, in its own colour,
    with the one caveat right under it (a live filter would have used those
    slots for other buys, which it does not guess), and the footnote names the
    buys it takes out - every value from the file escaped;
  * the account line and the profit do not move by one cent with it added.

The node parts are skipped (and say so) where node is not installed (set
MPS_NODE to its path to run them); the wiring checks in t4 run everywhere.
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
  const days=['2026-10-19','2026-10-20','2026-10-21','2026-10-22','2026-10-23'];
  const cl=days.map((d,i)=>[d,500+i]), g='2026-10-24T00:00:00Z';
  const zero=days.map(d=>[d,0]);
  // 1. c = 0: the account itself - an uneven NetLiq with money in and out
  const s1=days.map((d,i)=>({d, nl:[200000,203500,226100,196000,198400][i]}));
  const fl=[{d:'2026-10-21', amt:23746}, {d:'2026-10-22', amt:-32000}];
  const Z=benchCompute(s1,fl,cl,0,g,[],{filter:{series:zero}});
  const Zn=benchCompute(s1,fl,cl,0,g,[],null);
  R.zero={you:Z.you, youDD:Z.youDD, profit:[Z.profit,Zn.profit], youN:Zn.you,
          f:Z.sims.find(s=>s.key==='sim_filter')};
  // 2. a flat account, one skipped lot gaining 1,000 HKD across the window
  const flat=days.map(d=>({d, nl:200000}));
  const gain=days.map((d,i)=>[d,250*i]);
  const G=benchCompute(flat,[],cl,0,g,[],{filter:{series:gain}});
  R.gain={you:G.you, f:G.sims.find(s=>s.key==='sim_filter')};
  // 3. complete_through (Bitcoin) before the end: the Bitcoin line stops, the filter does not
  const tr=days.map((d,i)=>[d,Math.pow(1.10,i)]);
  const C=benchCompute(flat,[],cl,0,g,[],{complete_through:'2026-10-21',
    trend:{series:tr}, filter:{series:gain}});
  R.ct=C.sims.map(s=>({key:s.key, last:s.last, cut:s.cut}));
  // 4. a missing day and a figure that is not a number end the line there
  const M=benchCompute(flat,[],cl,0,g,[],{filter:{series:gain.filter(r=>r[0]!=='2026-10-22')}});
  const N=benchCompute(flat,[],cl,0,g,[],{filter:{series:gain.map(r=>r[0]==='2026-10-22'?[r[0],null]:r)}});
  const S=benchCompute(flat,[],cl,0,g,[],{filter:{series:gain.map(r=>r[0]==='2026-10-21'?[r[0],'250']:r)}});
  const late=benchCompute(flat,[],cl,0,g,[],{filter:{series:gain.slice(1)}});    // no first day
  R.stop={miss:M.sims[0], nan:N.sims[0], str:S.sims[0], late:late.sims};
  // 5. the card itself
  const card=()=>document.querySelector('#bench-card').innerHTML;
  const draw=k=>{ try{ renderBench(); }catch(e){ R[k+'Err']=String(e&&e.stack||e); } return card(); };
  BENCH={hist:{series:flat.map(r=>({...r, exc:0})), flows:[]},
         bench:{prices:cl, generated_at:g}, others:[],
         sims:{complete_through:'2026-10-23', filter:{series:gain,
           judged_through:'2026-10-21 23:40 UTC', first_live:null,
           lots:[{sym:'7733', mkt:'JP', fill:'2026-09-07', sold:'2026-10-15', pl:-1676},
                 {sym:'<img src=x onerror=alert(1)>', mkt:'J"P', fill:'2026-10-26', sold:null,
                  pl:1000.4}]}}};
  BOT=null; BENCH_WIN=0;
  R.html=draw('card');
  BENCH.sims.filter.first_live='2026-10-29'; R.live=draw('live');
  BENCH.sims.filter.lots=[]; BENCH.sims.filter.series=zero; R.none=draw('none');
  benchToggle('sim_filter'); R.store=localStorage.getItem('mps_bench_hide');
  R.hidden=draw('hidden');
  benchToggle('sim_filter'); R.store2=localStorage.getItem('mps_bench_hide');
  console.log('@@'+JSON.stringify(R)+'@@');
})()`, ctx);
"""


def _run():
    """The harness result, or None when node is not installed."""
    node = shutil.which("node") or shutil.which(os.environ.get("MPS_NODE") or "")
    if not node:
        return None
    tmp = tempfile.mkdtemp(prefix="mps-filterline-node-")
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


def t1_c_zero_is_the_account_and_a_gain_taken_out_shows(R):
    if R is None:
        print("t1 node not on PATH - the page's script was NOT run")
        return False
    z = R["zero"]
    f = z["f"]
    assert f, "no filter line drawn from an all-zero series"
    # exactly the account: every point, the end, the worst dip - money in and out included
    assert f["data"] == z["you"], (f["data"], z["you"])
    assert f["end"] == z["you"][-1] and f["dd"] == z["youDD"] and not f["cut"], f
    # ...and the account itself did not move with the line added
    assert z["you"] == z["youN"] and z["profit"][0] == z["profit"][1], z
    gl = R["gain"]
    assert all(v == 0 for v in gl["you"]), "the flat account is not flat"
    end = gl["f"]["end"]
    assert abs(end - (-0.5)) < 1e-9, "a 1,000 HKD gain taken out of 200,000 should be -0.5%%: %r" % end
    assert abs(gl["f"]["dd"] - (-0.005)) < 1e-12, gl["f"]
    print("t1 c = 0 draws the account line itself (with a deposit and a withdrawal in the "
          "window); a skipped lot's 1,000 HKD gain over a flat 200,000 account ends the "
          "line 0.5% below it OK")
    return True


def t2_its_own_days_and_it_stops_rather_than_guess(R):
    if R is None:
        print("t2 node not on PATH - skipped")
        return
    ct = {s["key"]: s for s in R["ct"]}
    assert ct["sim_crypto"]["last"] == 2 and ct["sim_crypto"]["cut"], ct
    assert ct["sim_filter"]["last"] == 4 and not ct["sim_filter"]["cut"], \
        "the Bitcoin bars' complete_through cut the filter line"
    st = R["stop"]
    for k in ("miss", "nan", "str"):
        s = st[k]
        want = 2 if k != "str" else 1
        assert s["last"] == want and s["cut"] and s["data"][want + 1:] == [None] * (4 - want), (k, s)
    assert st["late"] == [], "a filter line drawn without the window's first day"
    print("t2 the filter line runs on the account's days past complete_through; a missing "
          "day or a figure that is not a number ends it there OK")


def t3_the_row_the_caveat_and_the_footnote(R):
    if R is None:
        print("t3 node not on PATH - skipped")
        return
    for k in ("cardErr", "liveErr", "noneErr", "hiddenErr"):
        assert k not in R, "renderBench threw (%s):\n%s" % (k, R[k])
    h = R["html"]
    assert ('<tr data-bk="sim_filter" onclick="benchToggle(\'sim_filter\')" '
            'style="cursor:pointer">') in h, "the filter row is not its line's switch"
    assert "Sim: you with market filter (shadow)" in h
    assert '<span class="bk-dot" style="color:#e07b39">' in h, "the filter line lost its colour"
    # the caveat sits right under the Sim rows, before the table ends
    row = h.index('data-bk="sim_filter"')
    cav = h.index("A live filter would have used those slots for other buys")
    assert row < cav < h.index("</table>"), "the caveat is not under the filter row"
    assert "white-space:normal" in h[h.rindex("<tr", 0, cav):cav], "the caveat must wrap at 320px"
    # the account is 0%, the line -0.5%: "ahead 0.5%"
    r = h[row:h.index("</tr>", row)]
    assert "ahead 0.5%" in r and "-0.5%" in r, r
    # the footnote: which buys, escaped; the caveat; where the record comes from
    assert "7733 (JP, bought 2026-09-07, sold 2026-10-15, −1,676 HKD)" in h, h[-2500:]
    assert "&lt;img src=x onerror=alert(1)&gt; (J&quot;P, bought 2026-10-26, still held, " \
           "+1,000 HKD), results to 2026-10-23 — with their money left as idle cash" in h
    assert "<img src=x" not in h, "a value from the file reached the page unescaped"
    assert "which this line does not guess" in h
    assert "Buys to 2026-10-21 were judged from the index history; later ones come from " \
           "the bot’s own shadow record." in h
    assert "from 2026-10-29 by the bot’s own shadow record." in R["live"]
    assert "would have skipped. None would have been so far, so it is your account." in R["none"]
    # the Bitcoin sentence only with a Bitcoin line on the card
    assert "Bitcoin bar" not in h, "the footnote talks about Bitcoin lines that are not drawn"
    assert "too short to judge it." in h
    # the switch: stored, kept by the rebuild
    assert R["store"] == '["sim_filter"]' and R["store2"] == "[]", (R["store"], R["store2"])
    assert ('<tr data-bk="sim_filter" onclick="benchToggle(\'sim_filter\')" '
            'style="cursor:pointer;opacity:.45">') in R["hidden"]
    print("t3 its row is a stored switch in its own colour, the caveat right under it and "
          "wrapping, the footnote names the skipped buys escaped and says where they were "
          "judged OK")


def t4_the_wiring():
    """Text checks that run without node."""
    page = io.open(PAGE, encoding="utf-8").read()
    flat = page.replace(" ", "").replace("\n", "")
    assert "['sim_crypto','sim_m1','sim_filter']" in page, "sim_filter is not a known line key"
    assert "out.sims=benchSims(out.labels,ra,sims,lv);" in flat, "the levels never reach the line"
    assert "lv.nl.push(pts[i].nl);lv.fl.push(f);" in flat
    assert "constbase=(lv.nl[i-1]-c0)+lv.fl[i],now=lv.nl[i]-c1;" in flat, \
        "the filter step is no longer the account's arithmetic on nl - c"
    assert "returnbase>0?now/base:1;" in flat, "base <= 0 must leave the line where the account's is"
    # its colour is no other line's on the card
    for other in ("#29d086", "#93a4c0", "#ffc14d", "#4d9bff", "#b48cff", "#ff7ad9"):
        assert other != "#e07b39"
    assert page.count("#e07b39") == 1, "the filter colour is used somewhere else on the page"
    print("t4 wiring: the key is whitelisted, NetLiq levels and flows reach the line, its "
          "step is nl - c with the account's arithmetic, its colour is its own OK")


if __name__ == "__main__":
    R = _run()
    ran = t1_c_zero_is_the_account_and_a_gain_taken_out_shows(R)
    t2_its_own_days_and_it_stops_rather_than_guess(R)
    t3_the_row_the_caveat_and_the_footnote(R)
    t4_the_wiring()
    print("ALL FILTER LINE TESTS PASS" if ran else
          "ALL FILTER LINE TESTS EXCEPT THE PAGE SCRIPT (t1-t3: no node) - PARTIAL PASS")
