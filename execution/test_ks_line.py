#!/usr/bin/env python3
"""Golden test: the "Sim: you + kill switch (log only)" line on the Positions
tab's Growth vs S&P 500 card, and the kill switch's one status sentence.

Run from this directory:  python test_ks_line.py

The owner approved the timed kill switch in shadow on 2026-11-05 - "agree to
run in shadow, also add a line on performance compare chart to show this".
engine/build_sim_lines.py publishes c, the value less cost in HKD of the bot
buys ordered while the rule would have blocked new buys, in
docs/sim_lines.json "killswitch"; the page draws NetLiq - c with the market
filter line's own arithmetic (the shared `levels` step). The bot's last
trading run publishes what the rule says now as bot_state.json
"kill_switch_shadow", and the page says it in one sentence. What must stay
true:

  * with c = 0 every day (no trip yet) the line IS the account line, to the
    last digit - money in and out included; a blocked lot that gained
    HK$1,000 over a flat HK$200,000 account ends it 0.5% below;
  * a day the file has no figure for is stepped across on levels, as the
    filter line does, and the Bitcoin lines' complete_through never cuts it;
  * its row is a stored show/hide switch in its own colour, with the caveat
    and the status sentence under the Sim rows: "the account is 8.4% below
    its peak; it would block new buys until the 23:35 UTC run on 2 Dec", "it
    would not block now", "unknown" - every value from either file escaped;
  * the footnote names the replayed blocks and the buys taken out;
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
  // 1. no trip: c = 0, the account itself - an uneven NetLiq with money in and out
  const s1=days.map((d,i)=>({d, nl:[200000,203500,226100,196000,198400][i]}));
  const fl=[{d:'2026-10-21', amt:23746}, {d:'2026-10-22', amt:-32000}];
  const Z=benchCompute(s1,fl,cl,0,g,[],{killswitch:{series:zero}});
  const Zn=benchCompute(s1,fl,cl,0,g,[],null);
  R.zero={you:Z.you, youDD:Z.youDD, profit:[Z.profit,Zn.profit], youN:Zn.you,
          k:Z.sims.find(s=>s.key==='sim_ks')};
  // 2. a flat account, one blocked lot gaining 1,000 HKD across the window
  const flat=days.map(d=>({d, nl:200000}));
  const gain=days.map((d,i)=>[d,250*i]);
  const G=benchCompute(flat,[],cl,0,g,[],{killswitch:{series:gain}});
  R.gain={you:G.you, k:G.sims.find(s=>s.key==='sim_ks')};
  // 3. the filter and the kill switch side by side on the same c: the same line
  const B2=benchCompute(s1,fl,cl,0,g,[],{filter:{series:gain}, killswitch:{series:gain},
    complete_through:'2026-10-21', trend:{series:days.map((d,i)=>[d,Math.pow(1.1,i)])}});
  R.both=B2.sims.map(s=>({key:s.key, data:s.data, last:s.last, cut:s.cut}));
  // 4. a day the file lacks is stepped across; a figure that is not a number ends it
  const M=benchCompute(flat,[],cl,0,g,[],{killswitch:{series:gain.filter(r=>r[0]!=='2026-10-22')}});
  const N=benchCompute(flat,[],cl,0,g,[],{killswitch:{series:gain.map(r=>r[0]==='2026-10-22'?[r[0],null]:r)}});
  R.stop={miss:M.sims[0], nan:N.sims[0]};
  // 5. the card, with the bot's status in its four shapes
  const card=()=>document.querySelector('#bench-card').innerHTML;
  const draw=k=>{ try{ renderBench(); }catch(e){ R[k+'Err']=String(e&&e.stack||e); } return card(); };
  BENCH={hist:{series:flat.map(r=>({...r, exc:0})), flows:[]},
         bench:{prices:cl, generated_at:g}, others:[],
         sims:{complete_through:'2026-10-23', killswitch:{series:gain, trip:0.08, days:20,
           first_close:'2026-07-20',
           blocks:[{start:'2026-10-21', end:'2026-11-17', reset:false, fall_pct:8.05}],
           lots:[{sym:'7272', order:'2026-10-22 23:35 UTC', fill:'2026-10-23', sold:null, pl:294},
                 {sym:'<img src=x onerror=alert(1)>', order:'2026-10-22 23:35 UTC',
                  fill:'2026-10-23', sold:'2026-10-23', pl:-1000.4}]}}};
  const KS={mode:'shadow', at:'2026-11-05 23:35 UTC', trip:0.08, days:20, status:'ok',
            netliq:211303, peak:230661, line:212208, fall_pct:8.39, would_block:true,
            block_start:'2026-11-05', block_end:'2026-12-02', block_day:1};
  BOT={kill_switch_shadow:KS}; BENCH_WIN=0;
  R.block=draw('block');
  BOT={kill_switch_shadow:{...KS, would_block:false, block_start:null, block_end:null}};
  R.mark=draw('mark');                          // 8.39% below, but not on a close
  BOT={kill_switch_shadow:{...KS, fall_pct:6.97, would_block:false, block_end:null}};
  R.calm=draw('calm');
  BOT={kill_switch_shadow:{mode:'shadow', status:'unknown', why:'<b>x</b> unreadable',
                           would_block:null}};
  R.unknown=draw('unknown');
  BOT={kill_switch_shadow:{...KS, mode:'off'}}; R.off=draw('off');
  BOT=null; R.nobot=draw('nobot');
  // no kill switch line in the file, the bot's status there: the sentence alone
  const keep=BENCH.sims.killswitch; delete BENCH.sims.killswitch;
  BENCH.sims.filter={series:zero, lots:[]};
  BOT={kill_switch_shadow:KS}; R.noline=draw('noline');
  delete BENCH.sims.filter; BENCH.sims.killswitch=keep;
  // no trip yet: the footnote says so
  BENCH.sims.killswitch={series:zero, trip:0.08, days:20, first_close:'2026-07-20',
                         blocks:[], lots:[]};
  BOT=null; R.none=draw('none');
  benchToggle('sim_ks'); R.store=localStorage.getItem('mps_bench_hide');
  R.hidden=draw('hidden');
  benchToggle('sim_ks'); R.store2=localStorage.getItem('mps_bench_hide');
  console.log('@@'+JSON.stringify(R)+'@@');
})()`, ctx);
"""


def _run():
    """The harness result, or None when node is not installed."""
    node = shutil.which("node") or shutil.which(os.environ.get("MPS_NODE") or "")
    if not node:
        return None
    tmp = tempfile.mkdtemp(prefix="mps-ksline-node-")
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


def t1_no_trip_is_the_account_and_a_blocked_gain_shows(R):
    if R is None:
        print("t1 node not on PATH - the page's script was NOT run")
        return False
    z = R["zero"]
    k = z["k"]
    assert k, "no kill switch line drawn from an all-zero series"
    assert k["data"] == z["you"], (k["data"], z["you"])
    assert k["end"] == z["you"][-1] and k["dd"] == z["youDD"] and not k["cut"], k
    assert z["you"] == z["youN"] and z["profit"][0] == z["profit"][1], z
    g = R["gain"]
    assert all(v == 0 for v in g["you"]), "the flat account is not flat"
    assert abs(g["k"]["end"] - (-0.5)) < 1e-9, g["k"]
    assert abs(g["k"]["dd"] - (-0.005)) < 1e-12, g["k"]
    print("t1 no trip: the kill switch line IS the account (a deposit and a withdrawal in "
          "the window); a blocked lot's 1,000 HKD gain over 200,000 ends it 0.5% below OK")
    return True


def t2_the_filter_arithmetic_on_its_own_days(R):
    if R is None:
        print("t2 node not on PATH - skipped")
        return
    both = {s["key"]: s for s in R["both"]}
    # the same c gives the same line on both levels lines: one arithmetic
    assert both["sim_ks"]["data"] == both["sim_filter"]["data"], both
    # complete_through (Bitcoin) cuts the trend line, never this one
    assert both["sim_crypto"]["cut"] and both["sim_ks"]["last"] == 4 \
        and not both["sim_ks"]["cut"], both
    m, n = R["stop"]["miss"], R["stop"]["nan"]
    assert m["last"] == 4 and not m["cut"] and m["data"][3] is None, m
    assert abs(m["end"] - (-0.5)) < 1e-9, m
    assert n["last"] == 2 and n["cut"] and n["data"][3:] == [None, None], n
    print("t2 the same c draws the same line as the filter's, on the account's days past "
          "complete_through; a missing day is stepped across, a non-number ends it OK")


def t3_the_row_the_status_sentence_and_the_footnote(R):
    if R is None:
        print("t3 node not on PATH - skipped")
        return
    for k in ("blockErr", "markErr", "calmErr", "unknownErr", "offErr", "nobotErr",
              "nolineErr", "noneErr", "hiddenErr"):
        assert k not in R, "renderBench threw (%s):\n%s" % (k, R[k])
    h = R["block"]
    assert ('<tr data-bk="sim_ks" onclick="benchToggle(\'sim_ks\')" '
            'style="cursor:pointer">') in h, "the kill switch row is not its line's switch"
    assert "Sim: you + kill switch (log only)" in h
    assert '<span class="bk-dot" style="color:#22d3ee">' in h, "the line lost its colour"
    row = h.index('data-bk="sim_ks"')
    r = h[row:h.index("</tr>", row)]
    assert "ahead 0.5%" in r and "-0.5%" in r, r
    # the caveat and the status, under the Sim rows, wrapping at phone width
    cav = h.index("Kill switch line: the bot buys ordered while the 8% / 20-weekday kill "
                  "switch would have blocked new buys are taken out and their money left idle.")
    assert row < cav < h.index("</table>"), "the caveat is not under the Sim rows"
    assert "white-space:normal" in h[h.rindex("<tr", 0, cav):cav], "the row must wrap at 320px"
    want = ("Kill switch, log only: the account is 8.4% below its peak; it would block new "
            "buys until the 23:35 UTC run on 2 Dec.")
    assert want in h and cav < h.index(want) < h.index("</table>"), h[cav:cav + 600]
    assert ("Kill switch, log only: the account is 8.4% below its peak; it would not block "
            "now (only a day-end close can trip it).") in R["mark"], R["mark"][-3000:]
    assert ("Kill switch, log only: the account is 7.0% below its peak; it would not block "
            "now.") in R["calm"]
    u = R["unknown"]
    assert ("Kill switch, log only: unknown on the bot’s last run (&lt;b&gt;x&lt;/b&gt; "
            "unreadable); it blocks nothing either way.") in u, u[-3000:]
    assert "<b>x</b>" not in u, "a value from bot_state.json reached the page unescaped"
    for k in ("off", "nobot"):
        assert "Kill switch, log only" not in R[k], k
        assert "Kill switch line: the bot buys ordered" in R[k], "the caveat needs no status"
    nl = R["noline"]
    assert "Kill switch line:" not in nl and 'data-bk="sim_ks"' not in nl, "a line with no file"
    assert want in nl, "the bot's status is shown even before the line is built"
    # the footnote: the blocks the build replayed and the buys it takes out, escaped
    assert ("“Sim: you + kill switch (log only)” is your account without the bot buys "
            "ordered while the kill switch would have blocked new buys: a day-end close of "
            "your trading wealth (NetLiq less the money paid in) 8% or more below its peak "
            "blocks them for 20 weekdays from that day, whatever the account does, and then "
            "the peak resets.") in h, h[-3000:]
    assert ("Replayed from your own day-end history since 2026-07-20, it would have fired "
            "once: 2026-10-21 to 2026-11-17 (still running).") in h, h[-3000:]
    assert ("Taken out: 7272 (ordered 2026-10-22, bought 2026-10-23, still held, +294 HKD) "
            "and &lt;img src=x onerror=alert(1)&gt; (ordered 2026-10-22, bought 2026-10-23, "
            "sold 2026-10-23, −1,000 HKD), their money left as idle cash") in h, h[-3000:]
    assert "<img src=x" not in h, "a value from the file reached the page unescaped"
    assert "every order is still sent" in h
    assert ("Replayed from your own day-end history since 2026-07-20, it has not fired, "
            "so the line is your account.") in R["none"]
    # the switch: stored, kept by the rebuild
    assert R["store"] == '["sim_ks"]' and R["store2"] == "[]", (R["store"], R["store2"])
    assert ('<tr data-bk="sim_ks" onclick="benchToggle(\'sim_ks\')" '
            'style="cursor:pointer;opacity:.45">') in R["hidden"]
    print("t3 its row is a stored switch in its own colour; the caveat and the bot's status "
          "sentence (blocking until the 23:35 run, not blocking, unknown) sit under the Sim "
          "rows, escaped; the footnote names the replayed blocks and the buys taken out OK")


def t4_the_wiring():
    """Text checks that run without node."""
    page = io.open(PAGE, encoding="utf-8").read()
    flat = page.replace(" ", "").replace("\n", "").replace("\r", "")
    assert "'sim_filter','sim_ks']" in page, "sim_ks is not a known line key"
    assert "constK=lv?rowsOf(sims.killswitch):null;" in flat, "the line is not read from the file"
    assert "name:'Sim:you+killswitch(logonly)',grow:levels});" in flat, \
        "the kill switch line does not step on the filter line's levels arithmetic"
    # the History label: on the bot's own field, never the filter's
    assert "${x.shadow_ks?`" in page and "kill switch would have blocked</span>" in page
    # its colour is no other line's, and only the line and its History label use it
    for other in ("#29d086", "#93a4c0", "#ffc14d", "#4d9bff", "#b48cff", "#ff7ad9", "#e07b39"):
        assert other.lower() != "#22d3ee"
    assert page.count("#22d3ee") == 2, "the kill switch colour is used somewhere else"
    # the status reads the bot's published block, and nothing the bot reads
    assert "BOT&&BOT.kill_switch_shadow" in flat
    print("t4 wiring: the key is whitelisted, the line comes from sim_lines.json "
          "killswitch on the shared levels step, its History label reads shadow_ks, its "
          "colour is its own OK")


if __name__ == "__main__":
    R = _run()
    ran = t1_no_trip_is_the_account_and_a_blocked_gain_shows(R)
    t2_the_filter_arithmetic_on_its_own_days(R)
    t3_the_row_the_status_sentence_and_the_footnote(R)
    t4_the_wiring()
    print("ALL KILL SWITCH LINE TESTS PASS" if ran else
          "ALL KILL SWITCH LINE TESTS EXCEPT THE PAGE SCRIPT (t1-t3: no node) - PARTIAL PASS")
