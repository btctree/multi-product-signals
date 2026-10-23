#!/usr/bin/env python3
"""Golden tests for a day's closing NetLiq and the calendar's day breakdown.

Run from this directory:  python test_day_close.py

Tapping a day in the P&L Calendar breaks its number down from
data/day_parts.json, which only the hourly publisher writes (the day's last at
23:25 UTC), beside the same NetLiq it puts in netliq_history. Until 2026-10-23
the 23:35 trading run then overwrote that NetLiq with its own reading, so the
total and its breakdown came from two moments ten minutes apart and the gap sat
in "Unexplained": -567 HKD on 8 Oct and +549 on 9 Oct. These tests pin down
that the bot leaves a recent publisher row in place (and still writes its own
when there is none), that the page names an older gap exactly, and that on the
published history nothing larger than rounding is left unnamed.
"""
import io
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone, timedelta

os.environ.setdefault("IB_BACKEND", "web")
import testenv                                     # noqa: E402
_TMP = testenv.isolate("mps-dayclose-")

import ib_bot                                      # noqa: E402
testenv.assert_isolated()

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PAGE = os.path.join(ROOT, "docs", "index.html")
DAY = "2026-10-23"
CLOSE = datetime(2026, 10, 23, 23, 25, 12, tzinfo=timezone.utc)
_n = [0]


def parts(rows):
    _n[0] += 1
    p = os.path.join(str(_TMP), "parts%d.json" % _n[0])
    io.open(p, "w", encoding="utf-8").write(json.dumps({"days": rows}))
    return p


def t1_the_publishers_close_stands():
    stands = ib_bot._publisher_row_stands
    ser = [{"d": "2026-10-22", "nl": 220000}, {"d": DAY, "nl": 221637, "exc": 0}]
    row = {"ts": "2026-10-23T23:25:12Z", "nl": 221637, "exc": 0, "pos": {}}
    p = parts({DAY: row})
    at = CLOSE + timedelta(minutes=10)                     # the 23:35 run
    assert stands(ser, p, DAY, now=at), "the 23:25 close with its breakdown was overwritten"
    # one missed hourly publish (22:25) is still the close; three hours is not
    assert stands(ser, p, DAY, now=CLOSE + timedelta(minutes=110))
    assert not stands(ser, p, DAY, now=CLOSE + timedelta(hours=3)), \
        "a breakdown from mid-afternoon kept the bot from writing the close"
    # the row was not the reading that breakdown was taken with
    assert not stands([{"d": DAY, "nl": 221000}], p, DAY, now=at)
    # nothing for today yet, or no breakdown for today: write ours, as before
    assert not stands([{"d": "2026-10-22", "nl": 1}], p, DAY, now=at)
    assert not stands(ser, parts({"2026-10-22": row}), DAY, now=at)
    assert not stands(ser, os.path.join(str(_TMP), "missing.json"), DAY, now=at)
    # unreadable things never raise, and never stop the bot writing
    bad = os.path.join(str(_TMP), "bad.json")
    io.open(bad, "w", encoding="utf-8").write("{not json")
    assert not stands(ser, bad, DAY, now=at)
    assert not stands(ser, parts({DAY: dict(row, ts="2026-10-23 23:25 UTC")}), DAY, now=at)
    assert not stands(ser, parts({DAY: dict(row, nl=None)}), DAY, now=at)
    assert not stands(ser, parts({DAY: dict(row, ts="2026-10-24T01:00:00Z")}), DAY, now=at), \
        "a breakdown stamped in the future was trusted"
    assert not stands(ser + [{"d": DAY, "nl": 221637}], p, DAY, now=at), "a doubled day"
    # netted by a different earmark than this run's (a lagging pocket file):
    # the run's fresher reading replaces it, as before
    assert not stands(ser, p, DAY, exc=500, now=at), "an over-excluded row was kept"
    ser_e = [{"d": DAY, "nl": 221637, "exc": 500}]
    assert stands(ser_e, parts({DAY: dict(row, exc=500)}), DAY, exc=500, now=at)
    assert not stands(ser_e, p, DAY, exc=500, now=at), "row and breakdown disagree on exc"
    print("t1 the bot leaves the publisher's recent close alone, and only that OK")


def t2_the_bot_still_writes_its_own_otherwise():
    src = io.open(os.path.join(HERE, "ib_bot.py"), encoding="utf-8").read()
    body = src[src.index("def publish_state("):]
    body = body[:body.index("\ndef ")]
    i = body.index("_publisher_row_stands(ser,")
    assert "exc=exc_pub" in body[i:i + 160], "the run's own earmark is not compared"
    j = body.index('ser.append({"d": today, "nl": round(nl), "exc": round(exc_pub or 0)})')
    k = body.index('hist["series"] = sorted(ser')
    assert i < body.index("else:", i) < j < k, \
        "today's row is no longer written when the publisher's does not stand"
    print("t2 publish_state writes its own row whenever the publisher's does not stand OK")


NO_NODE = []          # names of page checks that could not run


def _node():
    """node, from PATH or MPS_NODE. Missing is not a failure (the VM may have
    none), but it is said in the suite's last line, which run_all_tests prints."""
    return shutil.which("node") or shutil.which(os.environ.get("MPS_NODE") or "")


_HARNESS = r"""
const fs=require('fs');
const [pagePath, casesPath]=process.argv.slice(2);
const html=fs.readFileSync(pagePath,'utf8');
function grab(name){
  const i=html.indexOf('function '+name+'(');
  if(i<0) throw new Error('no function '+name);
  let d=0;
  for(let k=html.indexOf('{',i);k<html.length;k++){
    if(html[k]==='{') d++;
    else if(html[k]==='}'){ d--; if(!d) return html.slice(i,k+1); }
  }
}
eval(grab('partsTs')+';'+grab('dayBreakdown')+';'+grab('leftoverLabel')
     +';globalThis.dayBreakdown=dayBreakdown;globalThis.leftoverLabel=leftoverLabel;');
const C=JSON.parse(fs.readFileSync(casesPath,'utf8'));
const R={};
R.synth=dayBreakdown(C.synth.day, C.synth.hist, C.synth.parts, [], []);
R.cases={};
for(const k in C.cases){ const c=C.cases[k]; const r=dayBreakdown(c.day, c.hist, c.parts, [], []);
  R.cases[k]={timing:r.timing, resid:r.resid, total:r.total, earmark:r.earmark, label:leftoverLabel(r)}; }
R.labels=C.labels.map(r=>leftoverLabel(r));
R.live=[];
if(C.live){
  const fills=C.live.fills, divs=C.live.divs;
  C.live.hist.series.forEach(e=>{
    const r=dayBreakdown(e.d, C.live.hist, C.live.parts, fills, divs);
    if(!r.why) R.live.push({d:e.d, src:r.src, earmark:r.earmark, timing:r.timing, resid:r.resid,
                            label:leftoverLabel(r)});
  });
}
console.log('@@'+JSON.stringify(R)+'@@');
"""


def _jsonl(p):
    out = []
    if os.path.exists(p):
        for line in io.open(p, encoding="utf-8"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def t3_the_page_names_the_gap_exactly():
    """A day whose closing figure was read after its breakdown.

    Holdings 1,000 -> 1,080 between the two breakdowns (+80); the closing
    figures were read later at 1,000 and 1,100. The +20 between the breakdown
    and the close is named as its own line, and nothing is left over.
    """
    node = _node()
    if not node:
        print("t3 node not on PATH - the page's script was NOT run")
        NO_NODE.append("t3/t4")
        return
    d0, d1 = "2026-10-08", "2026-10-09"
    row = lambda nl, v, ts: {"ts": ts, "nl": nl, "exc": 0, "src": "live",
                             "fx": {"HKD": 1.0}, "pos": {"MSFT": [2, v, "HKD"]}, "cash": {}}
    cases = {"synth": {"day": d1,
                       "hist": {"series": [{"d": d0, "nl": 1000}, {"d": d1, "nl": 1100}], "flows": []},
                       "parts": {"days": {d0: row(1000, 1000, d0 + "T23:25:00Z"),
                                          d1: row(1080, 1080, d1 + "T23:25:00Z")}}}}
    # The earmark moved between a breakdown and the close it is compared with:
    # the bot's later reading set 100 HKD aside. Nothing moved; the 100 is the
    # earmark, not the minutes after the breakdown.
    cash = lambda nl, exc, ts, hkd: {"ts": ts, "nl": nl, "exc": exc, "src": "live",
                                     "fx": {"HKD": 1.0}, "pos": {"X": [1, 1000, "HKD"]},
                                     "cash": {"HKD": hkd}}
    cases["cases"] = {
        "earmark_in_gap": {"day": d1,
            "hist": {"series": [{"d": d0, "nl": 1100, "exc": 0}, {"d": d1, "nl": 1000, "exc": 100}],
                     "flows": []},
            "parts": {"days": {d0: cash(1100, 0, d0 + "T23:25:00Z", 100),
                               d1: cash(1100, 0, d1 + "T23:25:00Z", 100)}}},
        # 1 Oct 2026 as published: 22,436 earmarked, 22,400 withdrawn, holdings
        # +3,048, and the two closes read after their breakdowns (+1, +4)
        "oct01": {"day": "2026-10-01",
            "hist": {"series": [{"d": "2026-09-30", "nl": 218557, "exc": 22436},
                                {"d": "2026-10-01", "nl": 221646, "exc": 0}], "flows": []},
            "parts": {"days": {
                "2026-09-30": {"ts": "2026-09-30T23:25:10Z", "nl": 218558, "exc": 22436,
                               "src": "live", "fx": {"HKD": 1.0},
                               "pos": {"X": [1, 218558, "HKD"]}, "cash": {"HKD": 22436}},
                "2026-10-01": {"ts": "2026-10-01T23:25:12Z", "nl": 221642, "exc": 0,
                               "src": "live", "fx": {"HKD": 1.0},
                               "pos": {"X": [1, 221606, "HKD"]}, "cash": {"HKD": 36}}}}},
    }
    cases["labels"] = [
        {"resid": 0.4}, {"resid": 5}, {"resid": -9.9},
        {"resid": 36, "earmark": -22436}, {"resid": 36, "earmark": 100},
        {"resid": -50, "earmark": 100}, {"resid": -150, "earmark": 100},
        {"resid": 50, "earmark": -100, "unpriced": 1},
        {"resid": 40, "src": "reconstructed"}, {"resid": 40, "src": "reconstructed", "unpriced": 2},
        {"resid": 40, "src": "live"}, {"resid": None},
    ]
    dp = os.path.join(ROOT, "data", "day_parts.json")
    hp = os.path.join(ROOT, "data", "netliq_history.json")
    if os.path.exists(dp) and os.path.exists(hp):
        cases["live"] = {"hist": json.load(io.open(hp, encoding="utf-8")),
                         "parts": json.load(io.open(dp, encoding="utf-8")),
                         "fills": _jsonl(os.path.join(ROOT, "data", "fills_ledger.jsonl")),
                         "divs": _jsonl(os.path.join(ROOT, "data", "dividends_ledger.jsonl"))}
    tmp = tempfile.mkdtemp(prefix="mps-dayclose-node-")
    try:
        js, cj = os.path.join(tmp, "h.js"), os.path.join(tmp, "c.json")
        io.open(js, "w", encoding="utf-8").write(_HARNESS)
        io.open(cj, "w", encoding="utf-8").write(json.dumps(cases))
        p = subprocess.run([node, js, PAGE, cj], capture_output=True, timeout=120)
        out = p.stdout.decode("utf-8", "replace")
        assert p.returncode == 0 and "@@" in out, \
            "the page script failed under node:\n" + p.stderr.decode("utf-8", "replace")[-3000:]
        R = json.loads(out.split("@@")[1])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    s = R["synth"]
    assert round(s["total"]) == 100, s
    assert round(s["timing"]) == 20, "the gap after the breakdown is not named: %r" % s
    assert abs(s["resid"]) < 0.5, "something is still unexplained: %r" % s
    e = R["cases"]["earmark_in_gap"]
    assert round(e["total"]) == -100 and abs(e["timing"]) < 0.5,         "an earmark set aside after the breakdown was booked as movement: %r" % e
    assert round(e["resid"]) == -100 and e["label"][0] == "Moved into the earmark", e
    o = R["cases"]["oct01"]
    assert round(o["total"]) == 3089 and round(o["timing"]) == 5 and round(o["resid"]) == 36, o
    assert o["label"][0] == "Released from the earmark", o
    names = [x[0] if x else None for x in R["labels"]]
    assert names == [None, "Rounding", "Rounding",
                     "Released from the earmark", "Unexplained",
                     "Moved into the earmark", "Unexplained",
                     "Unexplained",
                     "Rebuild difference", "Unexplained",
                     "Unexplained", None], names
    print("t3 the page names the minutes after the breakdown exactly, gross of the "
          "earmark, and labels every leftover by its cause OK")
    if not R["live"]:
        print("t4 no local day_parts/history - skipped")
        return
    # Every day recorded live: at most rounding is left, except a day the
    # earmark moved, where the leftover is the earmarked cash that stayed.
    bad = [x for x in R["live"] if x["src"] == "live" and not x["earmark"]
           and abs(x["resid"]) >= 10]
    assert not bad, "live days with more than rounding unnamed: %r" % bad
    n = sum(1 for x in R["live"] if x["src"] == "live")
    # the days the earmark moved get the earmark's name, not "Unexplained"
    em = [x for x in R["live"] if x["src"] == "live" and x["earmark"] and abs(x["resid"]) >= 10]
    assert all(x["label"] and x["label"][0] != "Unexplained" for x in em), em
    print("t4 every live day (%d) is accounted for to within rounding OK" % n)


def t5_the_sheet_says_what_the_leftover_is():
    page = io.open(PAGE, encoding="utf-8").read()
    for label in ("Moved after the breakdown", "Rounding", "Released from the earmark",
                  "Rebuild difference"):
        assert "'" + label + "'" in page, "the day sheet lost its %r line" % label
    assert "r.timing" in page and "explained+=timing" in page.replace(" ", "")
    print("t5 the day sheet names every kind of leftover OK")


if __name__ == "__main__":
    t1_the_publishers_close_stands()
    t2_the_bot_still_writes_its_own_otherwise()
    t3_the_page_names_the_gap_exactly()
    t5_the_sheet_says_what_the_leftover_is()
    print("ALL DAY CLOSE TESTS PASS" if not NO_NODE else
          "ALL DAY CLOSE TESTS EXCEPT THE PAGE SCRIPT (%s: no node) - PARTIAL PASS"
          % ", ".join(NO_NODE))
