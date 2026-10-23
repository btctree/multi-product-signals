#!/usr/bin/env python3
"""Golden tests for handing a hand-bought holding to the bot (adopt_holdings).

Run from this directory:  python test_adopt.py

The exit loop manages only holdings in state['map'], which the bot writes when
IT buys. On 2026-10-23 the owner bought MSFT by hand on the strategy's own
signal and asked for it to be managed like the bot's own buy. execution/adopt.json
carries what that entry would have recorded; adopt_holdings seeds it once. These
tests pin down that it seeds exactly that, only while the holding is held, never
over another instrument, never from a broken or stale line, never twice, never
raises, and writes nothing a --dry run would publish - and that the page does not
let the owner's hand-placed fill mark a bot order as filled.
"""
import io
import json
import math
import os
import shutil
import subprocess
import tempfile
from datetime import date

os.environ.setdefault("IB_BACKEND", "web")
import testenv                                     # noqa: E402
_TMP = testenv.isolate("mps-adopt-")

import ib_bot                                      # noqa: E402
testenv.assert_isolated()

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PAGE = os.path.join(ROOT, "docs", "index.html")
TODAY = date(2026, 10, 24)
MSFT = {"symbol": "MSFT", "ib_symbol": "MSFT", "entry": 521.08, "stop": 481.29,
        "hw": 524.51, "entry_date": "2026-10-23"}
HELD = {"MSFT": (object(), 2.0), "HPQ": (object(), 59.0)}
_n = [0]
NO_NODE = []          # page checks that could not run (no node): said in the last line


def afile(items, raw=None):
    _n[0] += 1
    p = os.path.join(str(_TMP), "adopt%d.json" % _n[0])
    io.open(p, "w", encoding="utf-8").write(raw if raw is not None else json.dumps({"adopt": items}))
    return p


def adopt(state, items=None, held=HELD, dry=False, lot="2026-10-23", path=None, raw=None):
    del ib_bot.PLACED[:]
    p = path or afile(items if items is not None else [MSFT], raw)
    return ib_bot.adopt_holdings(state, held, dry=dry, path=p, today=TODAY,
                                 lot_date=lambda s: lot)


def t1_adopts_what_the_bots_own_entry_would_record():
    st = {"map": {"HPQ": "HPQ"}, "pos": {"HPQ": {"entry": 30, "stop": 28}}}
    assert adopt(st) == ["MSFT"]
    assert st["map"]["MSFT"] == "MSFT" and st["map"]["HPQ"] == "HPQ"
    assert st["pos"]["MSFT"] == {"entry": 521.08, "hw": 524.51, "stop": 481.29,
                                 "entry_date": "2026-10-23", "adopted": "2026-10-24"}
    assert st["pos"]["HPQ"] == {"entry": 30, "stop": 28}, "another holding was touched"
    rows = list(ib_bot.PLACED)
    assert len(rows) == 1 and rows[0]["action"] == "ADOPT" and rows[0]["status"] == "notice"
    assert rows[0]["symbol"] == "MSFT" and rows[0]["qty"] == 2.0 and rows[0]["ccy"] == "USD"
    assert "481.29" in rows[0]["reason"]
    # hw is optional: the bot's own entry starts its high at the entry price
    st2 = {}
    adopt(st2, [dict(MSFT, hw=None)])
    assert st2["pos"]["MSFT"]["hw"] == 521.08
    print("t1 adopts exactly the approved record (entry, cut-loss, high, fill-day entry date) OK")


def t2_once_only():
    st = {}
    adopt(st)
    first = json.dumps(st, sort_keys=True)
    assert adopt(st) == [] and not ib_bot.PLACED, "adopted twice"
    assert json.dumps(st, sort_keys=True) == first
    # the exit loop has since ratcheted the stop: a later run must not reset it
    st["pos"]["MSFT"].update(stop=485.04, hw=524.51)
    adopt(st)
    assert st["pos"]["MSFT"]["stop"] == 485.04, "adoption reset a ratcheted stop"
    print("t2 a holding is adopted once and its ratcheted stop is never reset OK")


def t3_only_while_held_long():
    for held in ({}, {"MSFT": (object(), 0)}, {"MSFT": (object(), -2.0)}, {"MSF": (object(), 2)}):
        st = {}
        assert adopt(st, held=held) == [] and "MSFT" not in st.get("pos", {})
        assert not ib_bot.PLACED
    print("t3 nothing is adopted unless it is held long OK")


def t4_never_over_another_instrument():
    st = {"map": {"MSFT": "MSFT.X"}, "pos": {"MSFT.X": {"entry": 1, "stop": 0.5}}}
    assert adopt(st) == []
    assert st["map"]["MSFT"] == "MSFT.X" and "MSFT" not in st["pos"]
    # a stale pos left by an older lot of the SAME symbol, with no map entry, is
    # replaced: it describes shares no longer held
    st = {"pos": {"MSFT": {"entry": 300, "stop": 250, "entry_date": "2026-01-02"}}}
    assert adopt(st) == ["MSFT"] and st["pos"]["MSFT"]["entry"] == 521.08
    # ...and so is one the bot left WITH its map entry: nothing removes map/pos
    # after a sale, so an older lot's record must not pass for this lot's - its
    # 2026-01-02 entry_date would fire the time stop on the first run
    st = {"map": {"MSFT": "MSFT"},
          "pos": {"MSFT": {"entry": 300, "hw": 330, "stop": 290, "entry_date": "2026-01-02"}}}
    assert adopt(st) == ["MSFT"], "an older lot's leftover record blocked the adoption"
    assert st["pos"]["MSFT"]["entry_date"] == "2026-10-23" and st["pos"]["MSFT"]["stop"] == 481.29
    # ...even one only days older: a bot lot stopped out on the 20th and bought
    # back by hand on the 23rd (verify round, 2026-10-23)
    st = {"map": {"MSFT": "MSFT"},
          "pos": {"MSFT": {"entry": 540, "hw": 545, "stop": 528, "entry_date": "2026-10-15"}}}
    assert adopt(st) == ["MSFT"] and st["pos"]["MSFT"]["stop"] == 481.29, st
    # a LATER record (the bot bought it back itself afterwards) is left alone
    st = {"map": {"MSFT": "MSFT"},
          "pos": {"MSFT": {"entry": 560, "hw": 570, "stop": 540, "entry_date": "2026-11-20"}}}
    assert adopt(st) == [] and st["pos"]["MSFT"]["stop"] == 540
    # a record whose entry_date is not a string never passes for this lot
    st = {"map": {"MSFT": "MSFT"}, "pos": {"MSFT": {"entry": 1, "stop": 0.5, "entry_date": 20261101}}}
    assert adopt(st) == ["MSFT"]
    print("t4 never adopts over another instrument, and replaces an older lot's record OK")


def t5_broken_lines_are_refused():
    bad = [dict(MSFT, stop=530), dict(MSFT, stop=521.08), dict(MSFT, entry=float("nan")),
           dict(MSFT, entry="x"), dict(MSFT, stop=-1), dict(MSFT, hw=500),
           dict(MSFT, entry=float("inf")), dict(MSFT, entry_date="2026-10-25"),
           dict(MSFT, entry_date="23/10/2026"), dict(MSFT, entry_date=None),
           dict(MSFT, entry=None), dict(MSFT, symbol=""), "MSFT", None, 7]
    for b in bad:
        st = {}
        assert adopt(st, [b]) == [], b
        assert "MSFT" not in st.get("pos", {}) and not ib_bot.PLACED, b
    # one broken line does not stop a good one after it
    st = {}
    assert adopt(st, [dict(MSFT, stop=999), MSFT]) == ["MSFT"]
    print("t5 a broken line is refused and never stops the others OK")


def t6_a_different_lot_is_refused():
    st = {}
    assert adopt(st, lot="2027-03-01") == [], "a later re-purchase was adopted with old prices"
    assert adopt(st, lot="2026-07-01") == []
    assert adopt(st, lot=None) == ["MSFT"], "no ledger record: the holding is the proof"
    st = {}
    assert adopt(st, lot="2026-10-26") == ["MSFT"], "a fill a few days after entry_date"
    print("t6 a lot bought far from entry_date is refused OK")


def t7_dry_seeds_in_memory_only():
    st = {}
    assert adopt(st, dry=True) == ["MSFT"]
    assert st["pos"]["MSFT"]["stop"] == 481.29, "the preview would not evaluate its exits"
    assert not ib_bot.PLACED, "--dry queued a dashboard row"
    print("t7 --dry seeds in memory (so the preview runs its exits) and queues nothing OK")


def t8_a_broken_file_never_raises():
    for raw in ("", "{not json", "[]", '{"adopt": 5}', '{"adopt": null}', "null"):
        st = {}
        assert adopt(st, raw=raw) == [] and st == {}, raw
    st = {}
    assert ib_bot.adopt_holdings(st, HELD, path=os.path.join(str(_TMP), "none.json")) == []
    assert st == {}, "a missing file touched state"
    print("t8 a missing, empty or malformed file adopts nothing and never raises OK")


def t9_the_committed_list():
    p = os.path.join(HERE, "adopt.json")
    raw = json.loads(io.open(p, encoding="utf-8").read())
    m = [x for x in raw["adopt"] if x["symbol"] == "MSFT"][0]
    # the engine's own cut-loss on the 2026-10-22 signal close
    want = round(max(521.08 - 3.5 * 11.3698, 0.88 * 521.08), 2)
    assert m["stop"] == want == 481.29 and m["entry"] == 521.08
    assert m["entry_date"] == "2026-10-23" and m["hw"] >= m["entry"]
    # and adopt_holdings accepts it as committed
    del ib_bot.PLACED[:]
    st = {}
    assert ib_bot.adopt_holdings(st, HELD, path=p, today=TODAY,
                                 lot_date=lambda s: "2026-10-23") == ["MSFT"]
    for x in raw["adopt"]:
        for k in ("entry", "stop"):
            assert isinstance(x[k], (int, float)) and math.isfinite(x[k])
    print("t9 the committed adopt.json holds the engine's own MSFT numbers OK")


def t10_run_adopts_before_the_exits():
    src = io.open(os.path.join(HERE, "ib_bot.py"), encoding="utf-8").read()
    body = src[src.index("def run(dry=False):"):]
    i = body.index("held = held_positions(ib)")
    j = body.index("adopt_holdings(state, held, dry)")
    k = body.index("# ---- EXITS first")
    assert i < j < k, "adoption must run after the holdings are read and before the exits"
    print("t10 the run adopts after reading holdings and before the exit loop OK")


def t12_an_aborted_run_keeps_the_adopt_row():
    """The adoption is saved with an aborted run's state, so no later run adds
    its History row again: the row must travel in _unpublished_activity. The
    HALT notice is still left out - the next run re-adds that one itself."""
    saved = []
    old = ib_bot.save_state
    ib_bot.save_state = lambda s: saved.append(json.loads(json.dumps(s)))
    try:
        st = {}
        adopt(st)
        ib_bot.PLACED.append({"time": "t", "action": "HALT", "qty": 0, "symbol": "ENTRIES",
                              "status": "notice"})
        ib_bot._save_state_on_abort(st, False, RuntimeError("boom"))
    finally:
        ib_bot.save_state = old
        del ib_bot.PLACED[:]
    assert saved, "the aborted run did not save state"
    rows = saved[-1].get("_unpublished_activity") or []
    assert [r["action"] for r in rows] == ["ADOPT"], rows
    assert saved[-1]["pos"]["MSFT"]["stop"] == 481.29
    print("t12 an aborted run keeps the adoption AND its History row, not the HALT row OK")


_HARNESS = r"""
const fs=require('fs'), vm=require('vm');
const [pagePath]=process.argv.slice(2);
process.on('unhandledRejection',()=>{});
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
  const act=[{time:'2026-10-22 23:35 UTC', action:'BUY', symbol:'MSFT', qty:2, status:'sent',
              limit:'530', ccy:'USD', reason:'entry signal'},
             {time:'2026-10-22 23:35 UTC', action:'BUY', symbol:'HPQ', qty:5, status:'sent',
              limit:'31', ccy:'USD', reason:'entry signal'}];
  FILLS=[{ts:'2026-10-23 13:31:00', date:'2026-10-23', symbol:'MSFT', side:'BOT', qty:2,
          price:517.44, sec_type:'STK', order_ref:null},
         {ts:'2026-10-23 13:31:00', date:'2026-10-23', symbol:'HPQ', side:'BOT', qty:5,
          price:30.1, sec_type:'STK', order_ref:'mps-123'}];
  let m=reconcileFills(act); R.msft=m.get(0)||null; R.hpq=m.get(1)||null;
  FILLS[0]={...FILLS[0]}; delete FILLS[0].order_ref;     // before 17 Sep: no key at all
  R.old=reconcileFills(act).get(0)||null;
  // The History list itself: one hand order filled in two executions, a bot
  // order with its own fill, a HALT notice and an ADOPT notice.
  DATA={backtest_trades:[]};
  BOT={updated:'2026-10-24 00:00 UTC', activity:[
    {time:'2026-10-22 23:35 UTC', action:'BUY', symbol:'HPQ', qty:5, status:'sent',
     limit:'31', ccy:'USD', reason:'entry signal'},
    {time:'2026-10-23 09:00 UTC', action:'HALT', qty:0, symbol:'ENTRIES', limit:'', ccy:'HKD',
     reason:'kill-switch', status:'notice', error:''},
    {time:'2026-10-24 23:35 UTC', action:'ADOPT', qty:2, symbol:'MSFT', limit:'', ccy:'USD',
     reason:'your own IB buy, now managed by the bot: cut-loss 481.29', status:'notice', error:''}]};
  FILLS=[{ts:'2026-10-23 13:30:05', date:'2026-10-23', symbol:'MSFT', side:'BOT', qty:1,
          price:517.0, sec_type:'STK', order_ref:null, order_id:7},
         {ts:'2026-10-23 13:30:09', date:'2026-10-23', symbol:'MSFT', side:'BOT', qty:1,
          price:517.88, sec_type:'STK', order_ref:null, order_id:7},
         {ts:'2026-10-23 13:31:00', date:'2026-10-23', symbol:'HPQ', side:'BOT', qty:5,
          price:30.1, sec_type:'STK', order_ref:'mps-123'}];
  let err=null;
  try{ renderActivityMode(''); }catch(e){ err=String(e&&e.stack||e); }
  R.err=err; R.html=document.querySelector('#v-history').innerHTML;
  console.log('@@'+JSON.stringify(R)+'@@');
})()`, ctx);
"""


def t11_a_hand_fill_never_fills_a_bot_order():
    node = shutil.which("node") or shutil.which(os.environ.get("MPS_NODE") or "")
    if not node:
        print("t11 node not on PATH - the page's script was NOT run")
        NO_NODE.append("t11")
        return
    tmp = tempfile.mkdtemp(prefix="mps-adopt-node-")
    try:
        js = os.path.join(tmp, "h.js")
        io.open(js, "w", encoding="utf-8").write(_HARNESS)
        p = subprocess.run([node, js, PAGE], capture_output=True, timeout=60)
        out = p.stdout.decode("utf-8", "replace")
        assert p.returncode == 0 and "@@" in out, p.stderr.decode("utf-8", "replace")[-2000:]
        R = json.loads(out.split("@@")[1])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert R["msft"] is None, "the owner's own MSFT shares marked a bot BUY as filled: %r" % R
    assert R["hpq"] and R["hpq"]["full"], "a bot fill no longer fills its bot order"
    assert R["old"] and R["old"]["full"], "fills recorded before order_ref existed stopped matching"
    assert R["err"] is None, "History threw:\n%s" % R["err"]
    rows = R["html"].split('<div class="list-row"')[1:]
    hand = [r for r in rows if "YOU, IN IB" in r]
    assert len(hand) == 1, "the two executions of one hand order are not one row: %d" % len(hand)
    assert "517.44" in hand[0] and "qty 2" in hand[0], "not the weighted fill: %s" % hand[0][:400]
    adopt_r = [r for r in rows if ">ADOPT<" in r]
    assert len(adopt_r) == 1 and 'class="pnl-pos"' in adopt_r[0], "ADOPT row missing or red"
    assert "limit" not in adopt_r[0].split('class="nm"')[1], "the ADOPT notice shows an order limit"
    halt = [r for r in rows if ">HALT<" in r]
    assert len(halt) == 1 and "limit" not in halt[0].split('class="nm"')[1]
    hpq = [r for r in rows if "<b>HPQ</b>" in r]
    assert len(hpq) == 1 and "FILLED @" in hpq[0] and "limit 31" in hpq[0], hpq
    order = [("ADOPT" if ">ADOPT<" in r else "HAND" if "YOU, IN IB" in r else
              "HALT" if ">HALT<" in r else "HPQ") for r in rows]
    assert order == ["ADOPT", "HAND", "HALT", "HPQ"], "not newest first: %r" % order
    print("t11 a hand-placed fill never marks a bot order filled; History shows the hand "
          "order once at its weighted price and ADOPT as a notice OK")


if __name__ == "__main__":
    t1_adopts_what_the_bots_own_entry_would_record()
    t2_once_only()
    t3_only_while_held_long()
    t4_never_over_another_instrument()
    t5_broken_lines_are_refused()
    t6_a_different_lot_is_refused()
    t7_dry_seeds_in_memory_only()
    t8_a_broken_file_never_raises()
    t9_the_committed_list()
    t10_run_adopts_before_the_exits()
    t11_a_hand_fill_never_fills_a_bot_order()
    t12_an_aborted_run_keeps_the_adopt_row()
    print("ALL ADOPT TESTS PASS" if not NO_NODE else
          "ALL ADOPT TESTS EXCEPT THE PAGE SCRIPT (%s: no node) - PARTIAL PASS"
          % ", ".join(NO_NODE))
