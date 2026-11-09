#!/usr/bin/env python3
"""Golden test: the Positions tab's bot cards, their HKD Total P&L and Today,
the holdings summary, net worth's Today line, and the notes toggle under the
Growth chart.

Run from this directory:  python test_position_cards.py

The owner, Mon 9 Nov 2026: "on the position cards, can total P&L and today
P&L also show on the card?" and, for the notes under the compare chart,
"please give them a hidden / display - / + button". What must stay true:
  * Total = IB's own mark (the newest day_parts row, the same publish as net
    worth) against IB's average cost (fill + buy fee), both in HKD at that
    row's rate - so the amount and the % agree in sign and the % is the share
    move. On the real 9 Nov 17:25 UTC rows: 4661.T (yen) +3,265 HKD +20.8%,
    the hand-bought MSFT +412 +5.1%, COP (bought that day) +201 +1.6%, and
    +8,563 +6.7% over the nine;
  * Today on a card IS that holding's line in dayBreakdown - the line the
    Calendar's day sheet prints - so the two can never disagree. A holding
    whose trade the ledger does not hold yet (COP until the 23:35 UTC sweep)
    is estimated and marked, never printed as its whole value (+12,837);
  * the summary's Today is the sum of the same cards' lines, and net worth's
    Today is the Calendar cell for the day (+2,845, +1.35%);
  * Best/Worst P&L sort by the % the card shows;
  * every string from a data file is escaped, and nothing throws when the
    breakdowns, history, ledgers or the bot's config are missing;
  * the chart notes are folded away by default, a tap shows them without a
    rebuild, and the choice is kept on the phone (storage inside try).
Display only: nothing here is read by the bot. The node parts are skipped
(and say so) where node is not installed; the wiring checks run everywhere.
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
NO_NODE = []

# Mon 9 Nov 2026 as the owner saw it at ~18:00 UTC, verbatim from the repo's
# data files at 44b286a2: the 8 Nov close and the 17:25 UTC snapshot from
# day_parts.json, the bot's holdings from bot_state.json, the history's last
# two days, each holding's opening fill and the two 9 Nov fills from the
# ledger (COP's buy and CRM's sale filled at the US open and were not in it
# yet), and the hourly build's names and prices from data.json.
PARTS = {"days": {
    "2026-11-08": {"cash": {"USD": 8791}, "exc": 0,
                   "fx": {"HKD": 1.0, "JPY": 0.0494806, "USD": 7.8401766},
                   "ib": {"4661": "4661.T", "7272": "7272.T"}, "nl": 210141,
                   "pos": {"4661.T": [100.0, 17738.8, "JPY"], "7272.T": [100.0, 8510.66, "JPY"],
                           "ADP": [9.0, 21331.47, "USD"], "APA": [54.0, 18979.66, "USD"],
                           "CRM": [7.0, 11725.38, "USD"], "IQV": [8.0, 17060.22, "USD"],
                           "LLY": [1.0, 10651.66, "USD"], "MSFT": [2.0, 8332.7, "USD"],
                           "VEEV": [6.0, 14253.44, "USD"], "ZBRA": [5.0, 12661.89, "USD"]},
                   "src": "live", "ts": "2026-11-08T23:25:14Z"},
    "2026-11-09": {"cash": {"JPY": 171121, "USD": 8675}, "exc": 0,
                   "fx": {"HKD": 1.0, "JPY": 0.04966185, "USD": 7.841896},
                   "ib": {"4661": "4661.T"}, "nl": 212986,
                   "pos": {"4661.T": [100.0, 18941.03, "JPY"], "ADP": [9.0, 21323.68, "USD"],
                           "APA": [54.0, 18993.86, "USD"], "COP": [12.0, 12836.95, "USD"],
                           "IQV": [8.0, 17441.63, "USD"], "LLY": [1.0, 10961.01, "USD"],
                           "MSFT": [2.0, 8535.67, "USD"], "VEEV": [6.0, 14463.28, "USD"],
                           "ZBRA": [5.0, 12970.5, "USD"]},
                   "src": "live", "ts": "2026-11-09T17:25:11Z"}}}
HIST = {"series": [{"d": "2026-11-07", "nl": 209981, "exc": 0},
                   {"d": "2026-11-08", "nl": 210141, "exc": 0},
                   {"d": "2026-11-09", "nl": 212986, "exc": 0}],
        "flows": [{"d": "2026-07-31", "amt": 23746, "note": "deposit"}]}
POS = [  # symbol, ib_symbol, qty, avg_cost, ccy, signal entry, stop
    ("VEEV", "VEEV", 6.0, 303.1667, "USD", 307.0, 278.32500699999997),
    ("APA", "APA", 54.0, 46.7785, "USD", 47.77, 42.04),
    ("ZBRA", "ZBRA", 5.0, 312.2, "USD", 361.13, 317.79),
    ("ADP", "ADP", 9.0, 286.9111, "USD", 285.37, 292.382992),
    ("COP", "COP", 12.0, 134.2833, "USD", 136.27, 121.4),
    ("LLY", "LLY", 1.0, 1129.66, "USD", 1149.36, 1329.15187),
    ("MSFT", "MSFT", 2.0, 517.94, "USD", 521.08, 526.124244),
    ("4661.T", "4661", 100.0, 3156.5232, "JPY", 3168.0, 3510.002464),
    ("IQV", "IQV", 8.0, 255.775, "USD", 258.44, 238.9448085),
]
BOT = {"updated": "2026-11-09 17:25 UTC", "netliq": 212986, "base_ccy": "HKD",
       "cash": {"JPY": 171121, "USD": 8675}, "excluded_cash": 0, "activity": [],
       "config": {"max_hold_bars": 60},
       "positions": [{"symbol": s, "ib_symbol": ib, "qty": q, "avg_cost": c, "ccy": k,
                      "entry": e, "stop": st} for s, ib, q, c, k, e, st in POS]}


def _fill(day, ts, sym, side, qty, price, ccy, ref=None, has_ref=True, **kw):
    f = {"execId": sym + day + side, "date": day, "ts": ts, "symbol": sym, "sec_type": "STK",
         "side": side, "qty": qty, "price": price, "ccy": ccy, "commission": 1.0,
         "commission_ccy": ccy, "source": "api", "flags": []}
    if has_ref:
        f["order_ref"] = ref
    f.update(kw)
    return f


FILLS = [
    _fill("2026-09-08", "2026-09-08 13:30", "LLY", "BOT", 1.0, 1128.66, "USD", has_ref=False),
    _fill("2026-10-23", "2026-10-23 13:30", "MSFT", "BOT", 2.0, 517.44, "USD", None),
    _fill("2026-10-29", "2026-10-29 00:00", "4661", "BOT", 100.0, 3154.0, "JPY",
          "mps-14015752-B-20261028100045", commission=252.32),
    _fill("2026-10-29", "2026-10-29 13:39", "ADP", "BOT", 9.0, 286.8, "USD", "mps-4661-B-1"),
    _fill("2026-11-03", "2026-11-03 14:30", "VEEV", "BOT", 6.0, 303.0, "USD", "mps-1"),
    _fill("2026-11-03", "2026-11-03 14:30", "ZBRA", "BOT", 5.0, 312.0, "USD", "mps-2"),
    _fill("2026-11-03", "2026-11-03 14:30", "IQV", "BOT", 8.0, 255.65, "USD", "mps-3"),
    _fill("2026-11-04", "2026-11-04 14:30", "APA", "BOT", 54.0, 46.76, "USD", "mps-4"),
    {"execId": "0000d5db.6f3dbb5a.01.01", "date": "2026-11-09", "ts": "2026-11-09 00:00",
     "symbol": "USD", "sec_type": "CASH", "side": "SLD", "qty": 1.0, "price": 158.239,
     "ccy": "JPY", "commission": 0.0, "commission_ccy": "JPY", "source": "api", "flags": []},
    {"execId": "00011ebe.6af0f3a4.01.01", "date": "2026-11-09", "ts": "2026-11-09 00:00",
     "symbol": "7272", "sec_type": "STK", "side": "SLD", "qty": 100.0, "price": 1711.0,
     "ccy": "JPY", "commission": 136.88, "commission_ccy": "JPY", "source": "api",
     "flags": ["ccy_repaired"], "order_ref": "mps-14017331-S-20261106100033"},
]
IDX = [{"sym": "VEEV", "name": "Veeva Systems", "price": 303.455},
       {"sym": "APA", "name": "APA Corporation", "price": 44.41},
       {"sym": "ZBRA", "name": "Zebra Technologies", "price": 330.5951},
       {"sym": "ADP", "name": "Automatic Data Processing", "price": 300.75},
       {"sym": "COP", "name": "ConocoPhillips", "price": 135.59},
       {"sym": "LLY", "name": "Eli Lilly", "price": 1397.65},
       {"sym": "MSFT", "name": "Microsoft", "price": 542.71},
       {"sym": "4661.T", "name": "ORIENTAL LAND CO., LTD.", "price": 3773.0},
       {"sym": "IQV", "name": "IQVIA", "price": 275.42}]
ADOPT = [{"symbol": "MSFT", "ib_symbol": "MSFT", "entry": 521.08, "stop": 481.29,
          "hw": 524.51, "entry_date": "2026-10-23"}]
# one LLY dividend, ex-date after the buy, to check the HKD dividend line
DIVS = [{"id": "d1", "symbol": "LLY", "amount": 1.5, "ccy": "USD", "date": "2026-10-01",
         "ex_date": "2026-09-15", "type": "div"}]

_HARNESS = r"""
const fs=require('fs'), vm=require('vm');
const [pagePath, fixPath]=process.argv.slice(2);
process.on('unhandledRejection',()=>{});
const html=fs.readFileSync(pagePath,'utf8');
const src=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const els={};
const cl=()=>{ const s=new Set(); return {add(c){s.add(c)},remove(c){s.delete(c)},
  contains(c){return s.has(c)}, toggle(c,f){ if(f===undefined?!s.has(c):f) s.add(c); else s.delete(c); },
  has:s}; };
const fake=()=>({innerHTML:'',textContent:'',style:{},dataset:{},classList:cl(),
  addEventListener(){},setAttribute(){},getAttribute(){return null},remove(){},
  appendChild(){},querySelector(){return null},querySelectorAll(){return []},
  closest(){return null},parentElement:null,getContext(){return null}});
const store={};
// the owner's phone at 18:00 UTC on Mon 9 Nov 2026
const NOW=Date.parse('2026-11-09T18:00:00Z');
class FD extends Date{ constructor(...a){ if(a.length) super(...a); else super(NOW); }
  static now(){ return NOW; } }
const ctx={console, setTimeout, clearTimeout, setInterval(){}, clearInterval(){}, Date:FD,
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
ctx.F=JSON.parse(fs.readFileSync(fixPath,'utf8'));
ctx.STORE=store;
vm.runInContext(String.raw`(async()=>{
  const tick=async()=>{ await new Promise(r=>setTimeout(r,0)); await new Promise(r=>setTimeout(r,0)); };
  await tick(); await tick();                       // let boot() settle on the stubs
  // the vs-S&P card fetches (and would set CAL) behind every render: not here
  loadBench=async()=>{};
  await tick();
  const R={};
  const J=o=>JSON.parse(JSON.stringify(o));
  const page=()=>document.querySelector('#v-positions').innerHTML;
  const setAll=()=>{
    DATA={positions:[], index:J(F.idx)}; IDX=DATA.index; BOT=J(F.bot); CAL=J(F.hist);
    PARTS=J(F.parts); PARTS_STAMP=BOT.updated; FILLS=J(F.fills); DIVS=J(F.divs); DIVS_OK=true;
    _divsRequested=true; ADOPT=J(F.adopt); ADOPT_OK=true; BENCH={err:true};
  };
  // ---- pure: Total ----
  setAll();
  const L=posLatest(PARTS);
  R.latest=L.d;
  R.total={};
  BOT.positions.forEach(bp=>{ const t=posTotal(bp, L.row, (IDX.find(o=>o.sym===bp.symbol)||{}).price);
    R.total[bp.symbol]={amt:t.amt, pct:t.pct, P:t.P, V:t.V, cost:t.cost, ib:t.ib, est:t.est, r:t.r}; });
  const msft=BOT.positions.find(p=>p.symbol==='MSFT');
  R.fbQty=posTotal(Object.assign({},msft,{qty:3}), L.row, 542.71);            // a run between publishes
  R.fbNoRate=posTotal(msft, null, 542.71);                                    // no day_parts at all
  R.fbNoCost=posTotal(Object.assign({},msft,{avg_cost:null}), L.row, 542.71); // IB gave no cost
  R.hkd=posTotal({symbol:'0700.HK', qty:100, avg_cost:500, ccy:'HKD'}, null, 510);
  // ---- pure: Today = the holding's line in dayBreakdown ----
  const D=posDay(CAL, PARTS); R.D=D;
  const B=dayBreakdown(D, CAL, PARTS, FILLS, DIVS);
  R.rows={}; (B.rows||[]).forEach(x=>{ R.rows[x.sym]={amt:x.amt, base:x.base, paid:x.paid, tq:x.tq,
    vb:x.vb, pending:!!x.pending, closed:!!x.closed}; });
  R.today={};
  BOT.positions.forEach(bp=>{ R.today[bp.symbol]=posTodayOf(B, bp.symbol, bp,
    PARTS.days[D].fx[bp.ccy], true, true, D); });
  R.todayNone=posTodayOf({why:'x'}, 'VEEV', BOT.positions[0], 7.8, true, true, D);
  R.todayLoading=posTodayOf(null, 'VEEV', BOT.positions[0], 7.8, true, true, D);
  R.todayQty=posTodayOf(B, 'VEEV', Object.assign({}, BOT.positions[0], {qty:7}), 7.8, true, true, D);
  R.cell=calCell(CAL, D);
  // Sunday 8 Nov: no trades, IB re-marked the yen holding, the US ones did not move
  const B8=dayBreakdown('2026-11-08', {series:CAL.series.slice(0,2), flows:[]},
    {days:{'2026-11-07':Object.assign(J(F.parts.days['2026-11-08']),{ts:'2026-11-07T23:25:14Z'}),
           '2026-11-08':F.parts.days['2026-11-08']}}, [], []);
  R.sun=posTodayOf(B8, 'ADP', BOT.positions.find(p=>p.symbol==='ADP'), 7.84, false, false, '2026-11-08');
  R.label=[posDayLabel('2026-11-09','2026-11-09'), posDayLabel('2026-11-08','2026-11-09')];
  R.wd=[posWeekdays('2026-10-29','2026-11-09'), posWeekdays('2026-11-09','2026-11-09'),
        posWeekdays('2026-11-06','2026-11-08'), posWeekdays('', '2026-11-09')];
  R.fmt=[posSigned(3265.1), posSigned(-1212.4), posSigned(-0.4), posPc(20.83), posPc(-0.04),
         posPc(-9.64), posPx(3814,'JPY'), posPx(1129.66,'USD'), posPx(121.4,'USD'), posPx(0.0123,'USD')];
  R.alerts=[posBotAlert({level:'trail', msg:'Raise your stop to ~3604.5 (chandelier trail moved up).'}),
            posBotAlert({level:'sell', msg:'⚠ SELL — closed below its 200-day average (50). The engine exits here.'}),
            posBotAlert({level:'sell', msg:'⚠ SELL — your cut-loss 99.6 was hit.'}), posBotAlert(null)];
  // ---- the page itself ----
  setAll(); renderPositions(); R.html=page();
  // the hourly price says APA is up 28%; IB's own mark says down 4.1%: the
  // sort must follow the card, which shows IB's
  IDX.find(o=>o.sym==='APA').price=60;
  STORE.mps_pos_sort='pnl'; renderPositions(); R.best=page(); delete STORE.mps_pos_sort;
  CAL_YM='2026-11'; renderCal(); R.cal=document.querySelector('#v-calendar').innerHTML;
  // a hostile symbol and name from the data files
  setAll();
  BOT.positions.push({symbol:'<img src=x>', ib_symbol:'<b>', qty:1, avg_cost:1, ccy:'U<SD', entry:1, stop:0.5});
  IDX.push({sym:'<img src=x>', name:'<script>alert(1)</script>', price:2});
  try{ renderPositions(); R.hostile=page(); }catch(e){ R.hostileErr=String(e&&e.stack||e); }
  // nothing loaded yet: no breakdowns, history, ledgers or config
  setAll(); PARTS=null; CAL=null; FILLS=null; DIVS=null; DIVS_OK=false; BENCH=null; delete BOT.config;
  PARTS_TRIED=BOT.updated;                          // no fetch: the stubs 404 anyway
  try{ renderPositions(); R.bare=page(); }catch(e){ R.bareErr=String(e&&e.stack||e); }
  setAll(); PARTS={err:true}; CAL=null; FILLS=null; DIVS=null; BENCH={err:true}; delete BOT.config;
  try{ renderPositions(); R.failed=page(); }catch(e){ R.failedErr=String(e&&e.stack||e); }
  // ---- day_parts freshness: swap on success only, one try per publish ----
  setAll(); const keep=PARTS; let calls=0;
  fetch=()=>{ calls++; return Promise.resolve({ok:false,status:503,json:async()=>({}),text:async()=>''}); };
  R.refFail=await refreshParts(); R.kept=PARTS===keep;
  PARTS_STAMP='old'; PARTS_TRIED=null; calls=0;
  ensurePosParts(); await tick(); ensurePosParts(); await tick(); ensurePosParts(); await tick();
  R.tries=calls;                                    // 2 fetches (url + local fallback), once
  const fresh=J(F.parts); fresh.days['2026-11-09'].nl=1;
  fetch=()=>Promise.resolve({ok:true,status:200,json:async()=>fresh,text:async()=>''});
  BOT.updated='2026-11-09 18:25 UTC'; R.refOk=await refreshParts();
  R.swapped=PARTS.days['2026-11-09'].nl===1&&PARTS_STAMP==='2026-11-09 18:25 UTC';
  console.log('@@'+JSON.stringify(R)+'@@');
})()`, ctx);
"""

_NOTES_HARNESS = r"""
const fs=require('fs'), vm=require('vm');
const [pagePath]=process.argv.slice(2);
process.on('unhandledRejection',()=>{});
const html=fs.readFileSync(pagePath,'utf8');
const src=html.match(/<script>([\s\S]*?)<\/script>/)[1];
function run(stored){
  const els={};
  const cl=()=>{ const s=new Set(); return {add(c){s.add(c)},remove(c){s.delete(c)},
    contains(c){return s.has(c)}, toggle(c,f){ if(f===undefined?!s.has(c):f) s.add(c); else s.delete(c); },
    list:()=>[...s]}; };
  const fake=()=>({innerHTML:'',textContent:'',style:{},dataset:{},classList:cl(),
    addEventListener(){},setAttribute(){},getAttribute(){return null},remove(){},
    appendChild(){},querySelector(){return null},querySelectorAll(){return []},
    closest(){return null},parentElement:null,getContext(){return null}});
  const store={}; if(stored!==undefined) store.mps_bench_notes=stored;
  const ctx={console, setTimeout, clearTimeout, setInterval(){}, clearInterval(){},
    document:{querySelector:s=>(els[s]=els[s]||fake()), querySelectorAll:()=>[],
      getElementById:id=>(els['#'+id]=els['#'+id]||fake()), createElement:()=>fake(),
      body:fake(), head:fake()},
    localStorage:stored==='THROW'
      ?{getItem(){throw new Error('blocked')}, setItem(){throw new Error('blocked')}}
      :{getItem:k=>(k in store?store[k]:null), setItem:(k,v)=>{store[k]=String(v)}},
    addEventListener(){}, location:{reload(){}}, scrollY:0, alert(){}, confirm:()=>false,
    prompt:()=>null, navigator:{}, open(){}};
  ctx.window=ctx;
  ctx.fetch=()=>Promise.resolve({ok:false,status:404,json:async()=>({}),text:async()=>''});
  vm.createContext(ctx);
  vm.runInContext(src, ctx);
  return vm.runInContext(String.raw`(()=>{
    const days=['2026-10-19','2026-10-20','2026-10-21','2026-10-22','2026-10-23'];
    const hist={series:days.map((d,i)=>({d, nl:200000+i*1000, exc:0})), flows:[]};
    BENCH={hist, bench:{prices:days.map((d,i)=>[d,500+i]), generated_at:'2026-10-24T00:00:00Z'},
           others:[], sims:{complete_through:'2026-10-23', weights:{trend:0.3},
             trend:{series:days.map((d,i)=>[d,1+i*0.01])},
             filter:{series:days.map((d,i)=>[d,0]), lots:[]}}};
    BOT=null; BENCH_WIN=0;
    const out={};
    const rd=()=>{ try{ return localStorage.getItem('mps_bench_notes'); }catch(e){ return null; } };
    renderBench(); out.html=document.querySelector('#bench-card').innerHTML; out.notes=BENCH_NOTES;
    // the tap: a button inside the card
    const card=document.createElement('div'); const attrs={};
    const btn={textContent:'', setAttribute:(k,v)=>{attrs[k]=v}, closest:s=>s==='.bench'?card:null};
    if(!BENCH_NOTES) card.classList.add('notes-off');
    benchNotesToggle(btn);
    out.t1={notes:BENCH_NOTES, text:btn.textContent, aria:attrs['aria-expanded'],
            off:card.classList.contains('notes-off'), stored:rd()};
    renderBench(); out.rebuilt=document.querySelector('#bench-card').innerHTML;
    benchNotesToggle(btn);
    out.t2={notes:BENCH_NOTES, text:btn.textContent, aria:attrs['aria-expanded'],
            off:card.classList.contains('notes-off'), stored:rd()};
    benchNotesToggle(null);                         // never throws without a button
    return out;
  })()`, ctx);
}
let R;
try{ R={none:run(undefined), one:run('1'), zero:run('0'), junk:run('yes'), blocked:run('THROW')}; }
catch(e){ console.error(e&&e.stack||e); process.exit(1); }
console.log('@@'+JSON.stringify(R)+'@@');
"""


def _node():
    return shutil.which("node") or shutil.which(os.environ.get("MPS_NODE") or "")


def _run(harness, *extra):
    node = _node()
    if not node:
        return None
    tmp = tempfile.mkdtemp(prefix="mps-poscards-")
    try:
        js = os.path.join(tmp, "h.js")
        io.open(js, "w", encoding="utf-8").write(harness)
        args = [node, js, PAGE]
        if extra:
            fx = os.path.join(tmp, "fix.json")
            io.open(fx, "w", encoding="utf-8").write(json.dumps(extra[0]))
            args.append(fx)
        p = subprocess.run(args, capture_output=True, timeout=120)
        out = p.stdout.decode("utf-8", "replace")
        assert p.returncode == 0 and "@@" in out, \
            "the page script failed under node:\n" + p.stderr.decode("utf-8", "replace")[-3000:]
        return json.loads(out.split("@@")[1])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _fixture():
    return {"parts": PARTS, "hist": HIST, "bot": BOT, "fills": FILLS, "idx": IDX,
            "adopt": ADOPT, "divs": DIVS}


def _card(html, sym):
    """The markup of one bot card."""
    i = html.index('data-sym="%s" onclick' % sym)
    j = html.find('<div class="card tap-card"', i)
    return html[i:j if j > 0 else len(html)]


def _expected():
    """Total in HKD from the fixture, the spec's own arithmetic."""
    row = PARTS["days"]["2026-11-09"]
    out = {}
    for s, _ib, q, c, k, _e, _st in POS:
        r = row["fx"][k]
        v = row["pos"][s][1]
        p = v / r / q
        out[s] = {"amt": v - c * q * r, "pct": (p / c - 1) * 100, "P": p}
    return out


def t1_total_in_hkd_from_ibs_own_mark(R):
    if R is None:
        print("t1 node not on PATH - the page's script was NOT run")
        NO_NODE.append("t1")
        return
    E = _expected()
    assert R["latest"] == "2026-11-09"
    for s, e in E.items():
        t = R["total"][s]
        assert t["ib"] and not t["est"], (s, t)
        assert abs(t["amt"] - e["amt"]) < 1e-6 and abs(t["pct"] - e["pct"]) < 1e-9, (s, t, e)
    want = {"VEEV": (199, 1.4), "APA": (-815, -4.1), "ZBRA": (729, 6.0), "ADP": (1074, 5.3),
            "COP": (201, 1.6), "LLY": (2102, 23.7), "MSFT": (412, 5.1), "4661.T": (3265, 20.8),
            "IQV": (1396, 8.7)}
    for s, (a, p) in want.items():
        t = R["total"][s]
        assert (round(t["amt"]), round(t["pct"], 1)) == (a, p), (s, t)
    # the yen holding: IB's own mark is 3,814 JPY, cost at today's rate
    j = R["total"]["4661.T"]
    assert abs(j["P"] - 3814.0) < 0.01 and abs(j["cost"] - 3156.5232 * 100 * 0.04966185) < 1e-6, j
    assert round(sum(t["amt"] for t in R["total"].values())) == 8563
    assert round(sum(t["V"] for t in R["total"].values())) == 136468
    # fallbacks
    q = R["fbQty"]
    assert not q["ib"] and q["est"] and q["P"] == 542.71 and abs(q["V"] - 542.71 * 3 * 7.841896) < 1e-6, q
    n = R["fbNoRate"]
    assert n["amt"] is None and n["r"] is None and abs(n["local"] - (542.71 - 517.94) * 2) < 1e-9, n
    c = R["fbNoCost"]
    assert c["paidEst"] and c["est"] and c["C"] == 521.08, c
    h = R["hkd"]
    assert h["r"] == 1 and abs(h["amt"] - 1000) < 1e-9, "an HKD holding needs no rate: %r" % h
    print("t1 Total = IB's mark vs IB's average cost at today's rate: 4661.T +3,265 HKD +20.8%, "
          "MSFT +412, COP +201, nine holdings +8,563 HKD on 136,468; fallbacks marked OK")


def t2_today_is_the_calendar_line(R):
    if R is None:
        print("t2 node not on PATH - skipped")
        return
    assert R["D"] == "2026-11-09"
    want = {"4661.T": (1137, 6.4), "IQV": (378, 2.2), "LLY": (307, 2.9), "ZBRA": (306, 2.4),
            "VEEV": (207, 1.4), "MSFT": (201, 2.4), "APA": (10, 0.1), "ADP": (-12, -0.1)}
    for s, (a, p) in want.items():
        t, row = R["today"][s], R["rows"][s]
        assert t["state"] == "ok" and not t["imputed"], (s, t)
        # the card's number IS the day sheet's line, to the last digit
        assert t["amt"] == row["amt"], (s, t, row)
        assert (round(t["amt"]), round(t["pct"], 1)) == (a, p), (s, t)
    # COP: bought at the US open, not in the ledger until 23:35 UTC. The sheet
    # line is its whole value; the card prices the new shares at IB's cost.
    cop, row = R["today"]["COP"], R["rows"]["COP"]
    assert row["pending"] and round(row["amt"]) == 12837, row
    exp = 12836.95 - 12 * 134.2833 * 7.841896
    assert cop["imputed"] and abs(cop["amt"] - exp) < 1e-6, cop
    assert abs(cop["amt"] - R["total"]["COP"]["amt"]) < 1e-6, "bought today: Today = Total"
    assert round(cop["pct"], 1) == 1.6 and cop["em"] == "bought today" and cop["opened"], cop
    assert R["todayNone"]["state"] == "none" and R["todayLoading"]["state"] == "loading"
    assert R["todayQty"]["state"] == "missing", "a snapshot older than the holding was used"
    s = R["sun"]
    assert s["state"] == "ok" and round(s["amt"]) == 0 and s["em"] == "weekend", s
    assert R["label"] == ["Today", "Sun 8 Nov"], R["label"]
    assert R["wd"] == [7, 0, 0, None], R["wd"]
    c = R["cell"]
    assert round(c["pnl"]) == 2845 and "%.2f" % c["pct"] == "1.35", c
    assert R["fmt"] == ["+3,265", "\u22121,212", "0", "+20.8%", "0.0%", "\u22129.6%", "3,814",
                        "1,129.66", "121.40", "0.0123"], R["fmt"]
    a = R["alerts"]
    assert a[0] == {"level": "trail", "msg": "The bot should lift this cut-loss to about 3,604.5 "
                    "at its next run; nothing for you to do."}, a[0]
    assert a[1]["level"] == "sell" and a[1]["msg"].startswith("Below its 200-day average: the bot "
                                                             "sells this at its next run"), a[1]
    assert a[2] == {"level": "sell", "msg": "At or under its cut-loss: the bot sells this at "
                    "its next run."}, a[2]
    assert a[3] is None
    print("t2 Today is each holding's own day-sheet line (4661.T +1,137 +6.4%, ADP -12); COP "
          "bought today is estimated at IB's cost (+200, not +12,837); weekend, labels, "
          "formats and the bot's wording OK")


def t3_the_page(R):
    if R is None:
        print("t3 node not on PATH - skipped")
        return
    for k in ("hostileErr", "bareErr", "failedErr"):
        assert k not in R, "renderPositions threw (%s):\n%s" % (k, R[k])
    h = R["html"]
    # net worth: the Calendar's cell, and a tap opens its sheet
    assert ('<button type="button" class="nw-today" onclick="calDay(\'2026-11-09\')">Today'
            in h), h[:3000]
    nw = h[h.index('class="nw-today"'):h.index("</button>", h.index('class="nw-today"'))]
    assert "+2,845 HKD" in nw and "(+1.35%)" in nw and "breakdown ›" in nw, nw
    assert "+2,845</b>" in R["cal"] and "+1.35%" in R["cal"], "the calendar cell is not the same"
    # the summary under "Investment positions", before the sort chips
    i = h.index("Investment positions")
    summ = h[i:h.index("setPosSort", i)]
    assert "9 holdings · worth" in summ and "136,468 HKD" in summ, summ
    assert "+8,563 <small>HKD</small>" in summ and "+6.7%" in summ, summ
    today = sum(R["today"][s]["amt"] for s in R["today"])
    assert round(today) == 2734, today
    assert ("+%s <small>HKD</small>" % format(round(today), ",")) in summ, summ
    assert "IB prices at 17:25 UTC" in summ, summ
    assert "Sold today: CRM, 7272.T (on the calendar)" in summ, summ
    # the cards
    j = _card(h, "4661.T")
    for want in ("ORIENTAL LAND CO., LTD.", "18,941 <small", "100 shares · JPY",
                 "+3,265 <small>HKD</small>", "+20.8%", "since 29 Oct",
                 "+1,137 <small>HKD</small>", "+6.4%", "Paid (avg)", "3,156.52", "Price now",
                 "3,814", '<div class="lv t"><span>Cut-loss</span><b>3,510</b><em>8.0% below</em>',
                 "+1,755 HKD (+11.2%)", "a gain locked in", "Bought 29 Oct",
                 "held 7 of max 60 trading days", "paid 0.4% below the signal (3,168)",
                 'data-src="bot"', "Sell on IB 🤖"):
        assert want in j, "4661.T card lacks %r:\n%s" % (want, j)
    assert "61647" not in j and "qty 100" not in j and "auto-traded on IB" not in j
    m = _card(h, "MSFT")
    assert "Bought by you 23 Oct, run by the bot" in m and "+412 <small>HKD</small>" in m, m
    assert "signal" not in m, "a hand buy compared with a signal it never had"
    c = _card(h, "COP")
    assert "+201 <small>HKD</small>" in c and "bought today" in c and "\u2248" in c, c
    assert "12,837 <small" in c, "worth"
    assert "12,836" not in c.split("12,837 <small")[1] and "+12,837" not in c, \
        "COP's whole value printed as today's gain"
    assert "Bought today" in c and "paid 1.5% below the signal (136.27)" in c, c
    assert '<div class="lv s"><span>Cut-loss</span><b>121.40</b><em>11.0% below</em>' in c, c
    assert "\u22121,212 HKD (\u22129.6%)" in c and "a gain locked in" not in c, c
    a = _card(h, "ADP")
    assert "paid 0.5% above the signal (285.37)" in a and "\u221212 <small>HKD</small>" in a, a
    lly = _card(h, "LLY")
    assert "Dividends received:" in lly and "+12 HKD" in lly and "with them" in lly \
        and "+2,114 HKD" in lly, lly
    assert "held 44 of max 60 trading days" in lly
    # the footnote, once, under the last card
    assert h.count("Amounts in HKD at") == 1 and h.index("Amounts in HKD at") > h.rindex("tap-card")
    # Best P&L: by the % the card shows, from IB's price
    order = [s for s in ("LLY", "4661.T", "IQV", "ZBRA", "ADP", "MSFT", "COP", "VEEV", "APA")]
    pos = [R["best"].index('data-sym="%s" onclick' % s) for s in order]
    assert pos == sorted(pos), "Best P&L is not in the cards' order: %r" % order
    # escaped, everywhere
    x = R["hostile"]
    assert "<img src=x>" not in x and "<script>alert(1)</script>" not in x and "U<SD" not in x, \
        "a value from the data files reached the page unescaped"
    assert "&lt;img src=x&gt;" in x and "&lt;script&gt;" in x
    # before anything has loaded: placeholders, no Today line, no summary
    b = R["bare"]
    assert "nw-today" not in b and "trading days" not in b and "Investment positions" in b
    assert "4661.T" in b and "\u2026" in b, b[-3000:]
    f = R["failed"]
    assert "+61,647.68 <small>JPY</small>" in f and "+49.54 <small>USD</small>" in f, \
        "with no rate the card falls back to the stock's own currency"
    assert "nw-today" not in f and "Amounts in HKD" not in f, "HKD claimed with no rate"
    print("t3 the page: net worth's Today = the calendar cell, summary = the cards' sum "
          "(+8,563 / +2,734), every card's figures, hand buy and today's buy worded right, "
          "Best P&L follows the cards, all escaped, no throw with nothing loaded OK")


def t4_day_parts_stay_fresh(R):
    if R is None:
        print("t4 node not on PATH - skipped")
        return
    assert R["refFail"] is False and R["kept"], "a failed refresh dropped the breakdowns in hand"
    assert R["tries"] == 2, "a failing day_parts was fetched %d times for one publish" % R["tries"]
    assert R["refOk"] is True and R["swapped"], "a new publish's breakdowns were not swapped in"
    print("t4 day_parts: a failed refresh keeps what is shown, one try per publish, a new "
          "publish swaps in OK")


def t5_the_notes_toggle(N):
    if N is None:
        print("t5 node not on PATH - skipped")
        return
    for k in ("none", "zero", "junk", "blocked"):
        v = N[k]
        assert v["notes"] is False, k
        assert '<div class="card bench notes-off"' in v["html"], (k, v["html"][:200])
        assert ('<button type="button" class="note-tog" aria-expanded="false" '
                'aria-controls="bench-notes"') in v["html"].replace("\n      ", " "), k
        assert "+ Show notes</button>" in v["html"], k
    o = N["one"]
    assert o["notes"] is True and '<div class="card bench"' in o["html"] and \
        "\u2212 Hide notes</button>" in o["html"], o["html"][:300]
    h = N["none"]["html"]
    # the text stays in the page, only hidden; the figures and the SIMULATED header do not fold
    assert 'id="bench-notes" class="nm bnote"' in h and "far too short to judge" in h
    assert '<tr class="bnote"><td colspan="4" class="nm"' in h, "the filter caveat is not foldable"
    assert '<div class="nm bnote" style="font-size:11px;opacity:.75">Tap a row' in h
    assert "Simulated — not real money, not traded" in h and "Profit after money in/out" in h
    assert h.index("Your worst dip") < h.index("note-tog") < h.index('id="bench-notes"')
    t1, t2 = N["none"]["t1"], N["none"]["t2"]
    assert t1 == {"notes": True, "text": "\u2212 Hide notes", "aria": "true", "off": False,
                  "stored": "1"}, t1
    assert '<div class="card bench"' in N["none"]["rebuilt"], "the rebuild lost the choice"
    assert t2 == {"notes": False, "text": "+ Show notes", "aria": "false", "off": True,
                  "stored": "0"}, t2
    b = N["blocked"]["t1"]
    assert b["notes"] is True and b["stored"] is None, "blocked storage must not stop the tap"
    print("t5 the notes: hidden by default (and on junk or blocked storage), '1' shows them, "
          "the tap flips class, text and aria-expanded without a rebuild and is kept OK")


def t6_the_wiring():
    """Text checks that run without node."""
    page = io.open(PAGE, encoding="utf-8").read()
    k = "'mps_bench_notes'"
    assert page.count(k) == 2, "mps_bench_notes must be read once and written once"
    for line in page.split("\n"):
        if k in line:
            assert "try{" in line, "a mps_bench_notes storage access outside try: %r" % line
    assert ".bench.notes-off .bnote{display:none}" in page
    assert "#bench-card:empty{min-height:720px}" in page
    for css in (".pl2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));",
                ".lv3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));",
                ".lv3 .lv{min-width:0;"):
        assert css in page, css
    assert "el.innerHTML=staleNote+botNote+mfHtml+`<div id=\"bench-card\"></div>`" in page
    # the bot never reads the page; the page only reads dayBreakdown's lines
    assert "base:base, vb:vb, paid:traded[sym]||0, tq:tqty[sym]||0" in page
    assert "R=dayBreakdown(D, CAL, PARTS, FILLS, DIVS)" in page, "Today must be the calendar's own"
    # day_parts: swapped only on success, reset on a full refresh, refetched per publish
    assert "PARTS_TRIED=null; refreshParts()" in page
    assert "if(PARTS&&!PARTS.err&&PARTS_STAMP===want) return;" in page
    print("t6 wiring: the notes key read/written once inside try, foldable notes, phone-safe "
          "grids, the positions prefix, Today from dayBreakdown, day_parts refresh OK")


if __name__ == "__main__":
    R = _run(_HARNESS, _fixture())
    t1_total_in_hkd_from_ibs_own_mark(R)
    t2_today_is_the_calendar_line(R)
    t3_the_page(R)
    t4_day_parts_stay_fresh(R)
    N = _run(_NOTES_HARNESS)
    t5_the_notes_toggle(N)
    t6_the_wiring()
    print("ALL POSITION CARD TESTS PASS" if not NO_NODE else
          "ALL POSITION CARD TESTS EXCEPT THE PAGE SCRIPT (no node) - PARTIAL PASS")
