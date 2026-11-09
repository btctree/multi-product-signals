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
for(const k in C.cases){ const c=C.cases[k];
  const r=dayBreakdown(c.day, c.hist, c.parts, c.fills||[], c.divs||[]);
  R.cases[k]={timing:r.timing, resid:r.resid, total:r.total, earmark:r.earmark, label:leftoverLabel(r),
              rows:(r.rows||[]).map(x=>({sym:x.sym, amt:x.amt, pending:!!x.pending,
                                         suspect:!!x.suspect, unvalued:!!x.unvalued})),
              pend:r.pend||[], fxBy:r.fxBy||{}, conv:r.conv, fees:r.fees, trades:r.trades||[],
              suspect:r.suspect||[], unvalued:r.unvalued||[], intraday:r.intraday,
              newest:r.newest, asOf:r.asOf}; }
R.labels=C.labels.map(r=>leftoverLabel(r));
R.live=[];
if(C.live){
  const fills=C.live.fills, divs=C.live.divs;
  C.live.hist.series.forEach(e=>{
    const r=dayBreakdown(e.d, C.live.hist, C.live.parts, fills, divs);
    if(!r.why) R.live.push({d:e.d, src:r.src, earmark:r.earmark, timing:r.timing, resid:r.resid,
                            newest:r.newest, label:leftoverLabel(r)});
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


# Mon 9 Nov 2026 as the owner saw it at ~18:00 UTC: the 8 Nov close and the
# 17:25 UTC snapshot, verbatim from data/day_parts.json, with the two fills the
# ledger held. 7272's sale is the guessed-USD row (JPNNEXT); COP's buy and
# CRM's sale filled at the US open and were not in the ledger yet (fills are
# swept only by the bot's 10:00 and 23:35 UTC runs). The page printed 7272.T
# +1,333,207, COP +12,837, CRM -11,728 and "Unexplained" -1,333,086.
NOV9 = {
    "day": "2026-11-09",
    "hist": {"series": [{"d": "2026-11-08", "nl": 210141, "exc": 0},
                        {"d": "2026-11-09", "nl": 212986, "exc": 0}], "flows": []},
    "parts": {"days": {
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
                       "src": "live", "ts": "2026-11-09T17:25:11Z"}}},
    "fills": [
        {"execId": "0000d5db.6f3dbb5a.01.01", "date": "2026-11-09", "ts": "2026-11-09 00:00",
         "symbol": "USD", "con_id": 15016059, "sec_type": "CASH", "side": "SLD", "qty": 1.0,
         "price": 158.239, "ccy": "JPY", "commission": 0.0, "commission_ccy": "JPY",
         "source": "api", "exchange": "IDEALPRO", "flags": []},
        {"execId": "00011ebe.6af0f3a4.01.01", "date": "2026-11-09", "ts": "2026-11-09 00:00",
         "symbol": "7272", "con_id": 14017331, "sec_type": "STK", "side": "SLD", "qty": 100.0,
         "price": 1711.0, "ccy": "USD", "commission": 136.88, "commission_ccy": "",
         "gbp_rate": None, "source": "api", "exchange": "JPNNEXT",
         "flags": ["rate_missing", "ccy_guessed"]},
    ],
}


def _breakdown_cases():
    """Synthetic days for the guards added on 2026-11-09, each with one cause."""
    d0, d1 = "2026-11-05", "2026-11-06"
    hist = lambda n0, n1: {"series": [{"d": d0, "nl": n0, "exc": 0}, {"d": d1, "nl": n1, "exc": 0}],
                           "flows": []}
    fx = {"HKD": 1.0, "JPY": 0.05, "USD": 7.8}
    row = lambda nl, pos, cash, ts, rates=fx: {"ts": ts, "nl": nl, "exc": 0, "src": "live",
                                               "fx": dict(rates), "pos": pos, "cash": cash,
                                               "ib": {"7272": "7272.T"}}
    jp_sell = {"execId": "s1", "date": d1, "ts": d1 + " 00:00", "symbol": "7272", "sec_type": "STK",
               "side": "SLD", "qty": 100.0, "price": 1710.0, "ccy": "USD", "commission": 137.0,
               "commission_ccy": "", "source": "api", "flags": ["rate_missing", "ccy_guessed"]}
    # held 8,500 HKD of 7272.T; sold 100 @ 1,710 JPY less 137 JPY: +43.15 on the day
    jp = {"day": d1, "hist": hist(8500, 8543.15),
          "parts": {"days": {d0: row(8500, {"7272.T": [100, 8500, "JPY"]}, {}, d0 + "T23:25:00Z"),
                             d1: row(8543.15, {}, {"JPY": 170863}, d1 + "T23:25:00Z")}}}
    out = {"guessed_jpy": dict(jp, fills=[jp_sell])}
    # the same sale recorded as plain USD, no flag: the price only fits the holding as JPY
    out["wrong_ccy"] = dict(jp, fills=[dict(jp_sell, flags=[], commission_ccy="USD")])
    # a recorded price 100 times too high, in the right currency: a line bigger
    # than the holding is not printed as a gain
    out["suspect"] = dict(jp, fills=[dict(jp_sell, ccy="JPY", flags=[], commission_ccy="JPY",
                                          price=171000.0)])
    # a same-day round trip on a venue nobody knows: held at neither snapshot,
    # so nothing says what currency it was in. Left out and named - its +5,000
    # JPY (250 HKD) is in the cash, so it is the leftover.
    out["unvalued"] = {"day": d1, "hist": hist(1000, 1250),
                       "parts": {"days": {d0: row(1000, {"X": [1, 1000, "HKD"]}, {}, d0 + "T23:25:00Z"),
                                          d1: row(1250, {"X": [1, 1000, "HKD"]}, {"JPY": 5000},
                                                  d1 + "T23:25:00Z")}},
                       "fills": [dict(jp_sell, symbol="9999", execId="u1", commission=0.0),
                                 dict(jp_sell, symbol="9999", execId="u2", side="BOT",
                                      price=1660.0, commission=0.0)]}
    # COP bought, CRM sold at the US open, neither in the ledger yet; USD cash
    # 115 lower. One estimate for the pair (USD), nothing left over.
    usd = {"HKD": 1.0, "USD": 7.84}
    a = row(11725 + 1000 + 8791 * 7.84, {"CRM": [7, 11725, "USD"], "X": [1, 1000, "HKD"]},
            {"USD": 8791}, d0 + "T23:25:00Z", usd)
    b = row(12837 + 1000 + 8676 * 7.84, {"COP": [12, 12837, "USD"], "X": [1, 1000, "HKD"]},
            {"USD": 8676}, d1 + "T17:25:00Z", usd)
    out["pending_pair"] = {"day": d1, "hist": hist(a["nl"], b["nl"]),
                           "parts": {"days": {d0: a, d1: b}}}
    # a buy in the holdings with NO cash change behind it: nothing can be
    # estimated, and the leftover says what it is
    b2 = dict(b, pos={"CRM": [7, 11725, "USD"], "COP": [12, 12837, "USD"], "X": [1, 1000, "HKD"]},
              cash={"USD": 8791}, nl=a["nl"] + 12837)
    out["pending_no_cash"] = {"day": d1, "hist": hist(a["nl"], b2["nl"]),
                              "parts": {"days": {d0: a, d1: b2}}}
    # 4 Nov's shape: EUR held overnight, 466 of it sold at 10:00 at 1.1258 while
    # the evening snapshot's cross is 8.75/7.79 = 1.1232. Converting early was
    # worth +9.31 HKD; it belongs in the EUR exchange-rate line, not the leftover.
    e0 = {"HKD": 1.0, "EUR": 8.80, "USD": 7.84}
    e1 = {"HKD": 1.0, "EUR": 8.75, "USD": 7.79}
    ca = row(1000 + 485 * 8.80 + 100 * 7.84, {"X": [1, 1000, "HKD"]}, {"EUR": 485, "USD": 100},
             d0 + "T23:25:00Z", e0)
    cb = row(1000 + 19 * 8.75 + (100 + 466 * 1.1258) * 7.79, {"X": [1, 1000, "HKD"]},
             {"EUR": 19, "USD": 100 + 466 * 1.1258}, d1 + "T23:25:00Z", e1)
    eur = {"execId": "c1", "date": d1, "ts": d1 + " 10:00", "symbol": "EUR", "sec_type": "CASH",
           "side": "SLD", "qty": 466.0, "price": 1.1258, "ccy": "USD", "commission": 0.0,
           "commission_ccy": "USD", "source": "api", "flags": []}
    out["live_conversion"] = {"day": d1, "hist": hist(ca["nl"], cb["nl"]),
                              "parts": {"days": {d0: ca, d1: cb}}, "fills": [eur]}
    # the owner's month-end GBP: deposited, converted and earmarked, never held
    # at a snapshot. Its conversion is NOT booked against the snapshot rates.
    g = dict(e1, GBP=10.55)
    ga = row(1000 + 100 * 7.84, {"X": [1, 1000, "HKD"]}, {"USD": 100}, d0 + "T23:25:00Z", e0)
    gb = row(1000 + 100 * 7.79, {"X": [1, 1000, "HKD"]}, {"USD": 100}, d1 + "T23:25:00Z", g)
    gbp = dict(eur, execId="g1", symbol="GBP", qty=1750.0, price=10.61405, ccy="HKD",
               commission=0.0, commission_ccy="HKD")
    out["gbp_passthrough"] = {"day": d1, "hist": hist(ga["nl"], gb["nl"]),
                              "parts": {"days": {d0: ga, d1: gb}}, "fills": [gbp]}
    # 9 Nov itself: as the ledger had it, after the VM repairs the 7272 row, and
    # once the 23:35 run has swept COP and CRM (prices that fit the USD cash)
    out["nov9"] = dict(NOV9)
    healed = [dict(f) for f in NOV9["fills"]]
    healed[1].update(ccy="JPY", commission_ccy="JPY", flags=["ccy_repaired"])
    out["nov9_repaired"] = dict(NOV9, fills=healed)
    swept = healed + [
        {"execId": "cop", "date": "2026-11-09", "ts": "2026-11-09 14:30", "symbol": "COP",
         "sec_type": "STK", "side": "BOT", "qty": 12.0, "price": 133.67, "ccy": "USD",
         "commission": 1.0, "commission_ccy": "USD", "source": "api", "flags": []},
        {"execId": "crm", "date": "2026-11-09", "ts": "2026-11-09 14:30", "symbol": "CRM",
         "sec_type": "STK", "side": "SLD", "qty": 7.0, "price": 213.0, "ccy": "USD",
         "commission": 1.0, "commission_ccy": "USD", "source": "api", "flags": []}]
    out["nov9_swept"] = dict(NOV9, fills=swept)
    return out


def _check_breakdown_cases(R):
    """The assertions for _breakdown_cases, against the page's own output."""
    C = R["cases"]
    big = lambda c, lim: [x for x in c["rows"] if abs(x["amt"]) >= lim
                          and not (x["pending"] or x["suspect"] or x["unvalued"])]
    for k in ("guessed_jpy", "wrong_ccy"):
        c = C[k]
        assert abs(c["resid"]) < 1 and not big(c, 1000), "%s: %r" % (k, c)
        t = c["trades"][0]
        assert (t["ccy"], t["feeCcy"], t["fixed"]) == ("JPY", "JPY", True), (k, t)
        assert abs(c["fees"] - 6.85) < 0.01, (k, c["fees"])
        assert [round(x["amt"], 2) for x in c["rows"]] == [50.0], (k, c["rows"])
    s = C["suspect"]
    assert s["suspect"] == ["7272.T"] and not big(s, 1000), s
    assert s["label"][0] == "Trade not valued" and "7272.T" in s["label"][1], s["label"]
    assert abs(s["resid"]) < 100, "the impossible line went into the total: %r" % s["resid"]
    u = C["unvalued"]
    assert [x["sym"] for x in u["unvalued"]] == ["9999", "9999"], u
    assert u["label"][0] == "Trade not valued" and round(u["resid"]) == 250, u
    assert [x["sym"] for x in u["rows"]] == ["X"], "a trade with no currency made a line: %r" % u["rows"]
    p = C["pending_pair"]
    assert sorted(x["sym"] for x in p["rows"] if x["pending"]) == ["COP", "CRM"], p["rows"]
    assert len(p["pend"]) == 1 and p["pend"][0]["ccy"] == "USD", p["pend"]
    assert abs(p["pend"][0]["est"] - (12837 - 11725 - 115 * 7.84)) < 0.01, p["pend"]
    assert [x["sym"] for x in p["pend"][0]["syms"]] == ["COP", "CRM"], "biggest first"
    assert abs(p["resid"]) < 1 and p["label"] is None, p
    assert p["intraday"] is True and p["newest"] is True and p["asOf"] == "17:25", p
    n = C["pending_no_cash"]
    assert n["pend"][0]["est"] is None, n["pend"]
    assert n["label"][0] == "Trades not yet recorded" and "COP bought" in n["label"][1], n["label"]
    v = C["live_conversion"]
    conv = -466 * 8.75 + 466 * 1.1258 * 7.79
    assert abs(v["conv"] - conv) < 1e-6 and abs(conv - 9.31) < 0.01, v
    assert abs(v["fxBy"]["EUR"] - (485 * -0.05 + conv)) < 1e-6, v["fxBy"]
    assert abs(v["resid"]) < 0.01, "converting before the rate moved is still unexplained: %r" % v
    gp = C["gbp_passthrough"]
    assert gp["conv"] == 0 and "GBP" not in gp["fxBy"], gp
    assert abs(gp["resid"]) < 0.01, gp
    for k in ("nov9", "nov9_repaired"):
        c = C[k]
        assert all(abs(x["amt"]) < 100000 for x in c["rows"] if not x["suspect"]), \
            "%s: a line the size of the account is back: %r" % (k, c["rows"])
        assert not big(c, 10000), "%s: a +/-10k line on a day of 2,845: %r" % (k, big(c, 10000))
        cc = {x["sym"]: x for x in c["rows"]}
        assert cc["COP"]["pending"] and cc["CRM"]["pending"], cc
        assert abs(cc["7272.T"]["amt"]) < 1000 and not cc["7272.T"]["suspect"], cc["7272.T"]
        assert c["fees"] < 50, "136.88 JPY of commission read as USD again: %r" % c["fees"]
        assert len(c["pend"]) == 1 and 0 < c["pend"][0]["est"] < 1000, c["pend"]
        assert abs(c["resid"]) < 10, "%s leftover %r" % (k, c["resid"])
        assert c["label"] is None or c["label"][0] == "Rounding", c["label"]
        assert round(c["total"]) == 2845
    w = C["nov9_swept"]
    assert not [x for x in w["rows"] if x["pending"]] and not w["pend"], w
    assert abs(w["resid"]) < 10 and not big(w, 10000), w


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
        # 2026-11-09: trades in the holdings the ledger does not hold yet, an
        # estimate that was booked (so not the reason), a trade not valued
        {"resid": 900, "newest": True,
         "pend": [{"ccy": "USD", "est": None,
                   "syms": [{"sym": "COP", "dq": 12}, {"sym": "CRM", "dq": -7}]}]},
        {"resid": 900, "newest": False,
         "pend": [{"ccy": "USD", "est": None, "syms": [{"sym": "COP", "dq": 12}]}]},
        {"resid": 40, "src": "live", "pend": [{"ccy": "USD", "est": 207, "syms": []}]},
        {"resid": 4, "pend": [{"ccy": "USD", "est": None, "syms": []}]},
        {"resid": 50, "suspect": ["7272.T"]},
        {"resid": 50, "unvalued": [{"sym": "9999"}], "earmark": -100},
    ]
    dp = os.path.join(ROOT, "data", "day_parts.json")
    hp = os.path.join(ROOT, "data", "netliq_history.json")
    if os.path.exists(dp) and os.path.exists(hp):
        cases["live"] = {"hist": json.load(io.open(hp, encoding="utf-8")),
                         "parts": json.load(io.open(dp, encoding="utf-8")),
                         "fills": _jsonl(os.path.join(ROOT, "data", "fills_ledger.jsonl")),
                         "divs": _jsonl(os.path.join(ROOT, "data", "dividends_ledger.jsonl"))}
    cases["cases"].update(_breakdown_cases())
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
                     "Unexplained", None,
                     "Trades not yet recorded", "Trades not recorded", "Unexplained", "Rounding",
                     "Trade not valued", "Trade not valued"], names
    L = R["labels"]
    assert L[12][1].startswith("COP bought, CRM sold today"), L[12]
    assert "corporate action" in L[13][1], L[13]
    _check_breakdown_cases(R)
    print("t3 the page names the minutes after the breakdown exactly, gross of the "
          "earmark, and labels every leftover by its cause OK")
    print("t3b 9 Nov replayed: no 1.3M line, COP/CRM shown as trades not recorded yet "
          "(+207 estimated), a guessed or impossible currency never printed as P&L, "
          "conversions booked at the snapshot rates OK")
    if not R["live"]:
        print("t4 no local day_parts/history - skipped")
        return
    # Every day recorded live: at most rounding is left, except a day the
    # earmark moved, where the leftover is the earmarked cash that stayed.
    # The newest day may be mid-session: trades in its holdings that the ledger
    # does not hold yet, with no estimate that could be booked, are named as
    # such - on that day only. Any older day must add up.
    bad = [x for x in R["live"] if x["src"] == "live" and not x["earmark"]
           and abs(x["resid"]) >= 10
           and not (x.get("newest") and x["label"]
                    and x["label"][0] == "Trades not yet recorded")]
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
