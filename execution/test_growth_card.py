#!/usr/bin/env python3
"""Golden test: the Positions tab's "Growth vs S&P 500" card renders.

Run from this directory:  python test_growth_card.py

On 2026-10-23 the card's table header called esc(), which exists only inside
renderActivityMode. Every other suite passed, because none of them renders the
card - the page's own preview was the only thing that showed it blank. This
runs the real page script under node and draws the card from a small history,
so a ReferenceError (or any throw) in renderBench fails here instead.
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
  const days=['2026-10-19','2026-10-20','2026-10-21','2026-10-22','2026-10-23'];
  const hist={series:days.map((d,i)=>({d, nl:200000+i*1000-(i===2?3000:0), exc:0})), flows:[]};
  const px=(b,s)=>days.map((d,i)=>[d, b+i*s]);
  BENCH={hist, bench:{prices:px(500,2), generated_at:'2026-10-24T00:00:00Z'},
         others:BENCH_OTHERS.map((o,i)=>({...o, prices:px(300+i*100,1)}))};
  BOT=null; BENCH_WIN=0;
  let err=null;
  try{ renderBench(); }catch(e){ err=String(e&&e.stack||e); }
  console.log('@@'+JSON.stringify({err, html:document.querySelector('#bench-card').innerHTML})+'@@');
})()`, ctx);
"""


def t1_the_card_renders():
    node = shutil.which("node") or shutil.which(os.environ.get("MPS_NODE") or "")
    if not node:
        print("t1 node not on PATH - the page's script was NOT run")
        return False
    tmp = tempfile.mkdtemp(prefix="mps-growth-node-")
    try:
        js = os.path.join(tmp, "h.js")
        io.open(js, "w", encoding="utf-8").write(_HARNESS)
        p = subprocess.run([node, js, PAGE], capture_output=True, timeout=120)
        out = p.stdout.decode("utf-8", "replace")
        assert p.returncode == 0 and "@@" in out, \
            "the page script failed under node:\n" + p.stderr.decode("utf-8", "replace")[-3000:]
        R = json.loads(out.split("@@")[1])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert R["err"] is None, "renderBench threw:\n%s" % R["err"]
    h = R["html"]
    for want in ("Growth vs S&amp;P 500", "Growth vs S&P 500"):
        if want in h:
            break
    else:
        raise AssertionError("the card did not draw: %r" % h[:300])
    for row in ("This account", "S&P 500", "Dow Jones", "Nasdaq-100", "Worst dip<br>to 23 Oct"):
        assert row in h, "the table lost %r" % row
    print("t1 the growth card renders its table: account, S&P 500, Dow Jones, Nasdaq-100 OK")
    return True


if __name__ == "__main__":
    ran = t1_the_card_renders()
    print("ALL GROWTH CARD TESTS PASS" if ran else
          "ALL GROWTH CARD TESTS EXCEPT THE PAGE SCRIPT (t1: no node) - PARTIAL PASS")
