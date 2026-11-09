#!/usr/bin/env python3
"""Golden tests for a fill's currency: the venue table, the capture's
inference, and the in-place repair of a row written with a guessed currency.

Run from this directory:  python test_fills_ccy.py

THE ROW THAT STARTED IT (data/fills_ledger.jsonl, Mon 9 Nov 2026): the bot's
7272 (Yamaha Motor) SELL 100 @ 1,711 filled at the Tokyo open on JPNNEXT.
IBKR's trades read sends currency: null, the Web API shim derives it from the
venue, and JPNNEXT was not in its table - so the row was written ccy "USD",
ccy_guessed, rate_missing. The dashboard valued 171,100 JPY as 171,100 USD and
put "+1,333,207" against 7272.T and "-1,333,086 Unexplained" on the calendar;
the tax report could not value the sale.

What is pinned here:
  * the venue table knows JPNNEXT/CHIXJ (JPY), the US-only venues, CHF for EBS;
    agrees with contracts.py and ib_orders; never maps SMART or IDEALPRO
  * the shim and fill_row infer a missing currency (con_id, then venue), and a
    GUESSED row's commission_ccy stays "" (see earmark.hkd_delta)
  * the repair rewrites only the guessed line, LF in LF out, byte-identical
    elsewhere, idempotent, with the rate the same sweep stamped that day
  * TRADING: the bot's HKD pocket is the same before and after the repair; the
    one intended change is that a numeric ticker's first fill on JPNNEXT now
    reads "no HKD effect" instead of "unknown" (P computed, not refused)

No network: IB is never contacted (ib_orders.trades is replaced by a list).
"""
import json
import os
from datetime import date
from pathlib import Path

os.environ.setdefault("IB_BACKEND", "web")
import testenv                                     # noqa: E402
_TMP = testenv.isolate("mps-fillsccy-")

import broker                                      # noqa: E402
import contracts                                   # noqa: E402
import earmark                                     # noqa: E402
import fills_capture                               # noqa: E402
import ib_orders                                   # noqa: E402
import uk_cgt                                      # noqa: E402
import venue_ccy                                   # noqa: E402
testenv.assert_isolated()

REPO = Path(__file__).resolve().parent.parent
_n = [0]

# The three real rows, verbatim from data/fills_ledger.jsonl at 44b286a2.
BUY_7272 = ('{"execId": "0000edd7.6ade88fb.01.01", "date": "2026-10-26", "ts": "2026-10-26 00:00", '
            '"symbol": "7272", "con_id": 14017331, "sec_type": "STK", "side": "BOT", "qty": 100.0, '
            '"price": 1750.0, "ccy": "JPY", "commission": 140.0, "commission_ccy": "JPY", '
            '"gbp_rate": 0.00476186, "gbp_rate_commission": 0.00476186, "source": "api", "name": "", '
            '"exchange": "TSEJ", "flags": [], "order_ref": "mps-14017331-B-20261025233535", '
            '"order_id": 156058208}')
USDJPY = ('{"execId": "0000d5db.6f3dbb5a.01.01", "date": "2026-11-09", "ts": "2026-11-09 00:00", '
          '"symbol": "USD", "con_id": 15016059, "sec_type": "CASH", "side": "SLD", "qty": 1.0, '
          '"price": 158.239, "ccy": "JPY", "commission": 0.0, "commission_ccy": "JPY", '
          '"gbp_rate": 0.00471524, "gbp_rate_commission": 0.00471524, "source": "api", "name": "", '
          '"exchange": "IDEALPRO", "flags": [], "order_ref": null, "order_id": 17941824001}')
BAD_7272 = ('{"execId": "00011ebe.6af0f3a4.01.01", "date": "2026-11-09", "ts": "2026-11-09 00:00", '
            '"symbol": "7272", "con_id": 14017331, "sec_type": "STK", "side": "SLD", "qty": 100.0, '
            '"price": 1711.0, "ccy": "USD", "commission": 136.88, "commission_ccy": "", '
            '"gbp_rate": null, "gbp_rate_commission": null, "source": "api", "name": "", '
            '"exchange": "JPNNEXT", "flags": ["rate_missing", "ccy_guessed"], '
            '"order_ref": "mps-14017331-S-20261106100033", "order_id": 300082035}')


def ledger(lines, end="\n"):
    _n[0] += 1
    p = _TMP / ("led%d.jsonl" % _n[0])
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write("".join(l + end for l in lines))
    return p


class Using:
    """Point fills_capture at a scratch ledger for the duration."""
    def __init__(self, path):
        self.path, self.old = path, None

    def __enter__(self):
        self.old, fills_capture.LEDGER = fills_capture.LEDGER, self.path
        return self

    def __exit__(self, *a):
        fills_capture.LEDGER = self.old


class Ex:
    def __init__(self, eid="e", side="SLD", shares=100.0, price=1711.0):
        from datetime import datetime
        self.execId, self.side, self.shares, self.price = eid, side, shares, price
        self.time = datetime(2026, 11, 9, 0, 0)


class Con:
    def __init__(self, sym="7272", ccy="", exch="JPNNEXT", con_id=14017331, sec="STK"):
        self.symbol, self.currency, self.exchange = sym, ccy, exch
        self.conId, self.secType = con_id, sec


class Com:
    def __init__(self, commission=136.88, currency=""):
        self.commission, self.currency = commission, currency


class Fill:
    def __init__(self, ex=None, con=None, com=None):
        self.execution, self.contract, self.commissionReport = ex or Ex(), con or Con(), com


def shim_fills(trades):
    old = ib_orders.trades
    try:
        ib_orders.trades = lambda days=7: trades
        return broker.IB().reqExecutions()
    finally:
        ib_orders.trades = old


def trade(exch, sym="7272", conid=14017331, **kw):
    t = {"execution_id": "x-" + exch, "symbol": sym, "sec_type": "STK", "conid": conid,
         "side": "S", "size": "100", "price": "1711", "commission": "136.88",
         "exchange": exch, "trade_time": "20261109-00:00:01",
         "order_ref": "mps-%s-S-20261106100033" % conid, "order_id": 300082035}
    t.update(kw)
    return t


# ------------------------------------------------------------------ tests ----
def t1_the_venue_table():
    c = venue_ccy.ccy_for
    assert c("JPNNEXT") == "JPY" and c("CHIXJ") == "JPY" and c("TSEJ") == "JPY"
    assert c("jpnnext ") == "JPY", "venue codes are matched case- and space-blind"
    assert c("EBS") == "CHF", "EBS is SIX Swiss (CHF), not EUR"
    assert (c("CPH"), c("SFB"), c("OSE")) == ("DKK", "SEK", "NOK")
    for v in ("ISLAND", "IBKRATS", "EDGX", "EDGEA", "BYX", "BEX", "MEMX", "LTSE",
              "PEARL", "NYSENAT", "DRCTEDGE", "IBEOS", "CHX", "PSX", "OVERNIGHT",
              "NYSE", "NASDAQ", "ARCA"):
        assert c(v) == "USD", v
    # a router and the FX venue have no single currency; unknown stays unknown
    for v in ("SMART", "IDEALPRO", "FXCONV", "", None, "NOSUCHPTS"):
        assert c(v) == "", v
    # the listing exchange answers an unmapped venue, exactly then shortened
    assert c("NOSUCHPTS", "TSEJ") == "JPY"
    assert c("NOSUCHPTS", "NASDAQ.NMS") == "USD"
    assert c("NOSUCHPTS", "OSE.JPN") == "JPY", "Osaka must never shorten to Oslo"
    assert c("ENEXT.BE") == "EUR"
    assert c("TSEJ", "NYSE") == "JPY", "the execution venue comes first"
    # one table: the shim's name for it is the same object
    assert broker._EXCH_CCY is venue_ccy.EXCH_CCY
    # and it agrees with every other place the repo states a venue's currency
    for suf, (exch, ccy) in contracts._EU.items():
        assert c(exch) == ccy, "%s: contracts says %s %s, venue_ccy %r" % (suf, exch, ccy, c(exch))
    for ccy, venues in ib_orders._CCY_EXCHANGES.items():
        for v in venues:
            assert c(v) == ccy, (v, ccy, c(v))
    print("t1 venue table: JPNNEXT/CHIXJ JPY, US venues USD, EBS CHF, SMART never OK")


def t2_the_shim_reads_the_venue():
    old_q = ib_orders.fx_quote_ccy
    try:
        ib_orders.fx_quote_ccy = lambda sym, conid: "HKD"
        got = shim_fills([
            trade("JPNNEXT"),
            trade("NOSUCHPTS", sym="7203", conid=1),
            trade("NOSUCHPTS", sym="7203", conid=2, listing_exchange="TSEJ"),
            trade("ISLAND", sym="MSFT", conid=272093),
            {"execution_id": "fx", "symbol": "USD", "sec_type": "CASH", "conid": 15016061,
             "side": "S", "size": "2", "price": "7.84", "commission": "0",
             "exchange": "IDEALPRO", "trade_time": "20261109-10:00:00"},
            trade("NYSE", sym="X", currency="EUR"),          # IB's own currency wins
        ])
    finally:
        ib_orders.fx_quote_ccy = old_q
    cur = [(f.contract.currency, f.commissionReport.currency) for f in got]
    assert cur == [("JPY", "JPY"), ("", ""), ("JPY", "JPY"), ("USD", "USD"),
                   ("HKD", "HKD"), ("EUR", "EUR")], cur
    print("t2 the Web API shim books JPNNEXT in JPY, reads listing_exchange, leaves "
          "the FX pair lookup alone OK")


def t3_fill_row_infers_and_never_stamps_a_guess():
    rate = lambda ccy: {"JPY": 0.0047, "USD": 0.75}[ccy]
    # nothing known: today's behaviour - USD, guessed, unvalued
    g = fills_capture.fill_row(Fill(con=Con(exch="NOSUCHPTS"), com=Com()), rate)
    assert (g["ccy"], g["gbp_rate"], g["flags"]) == ("USD", None, ["rate_missing", "ccy_guessed"])
    assert g["commission_ccy"] == "", \
        "a guessed row's commission currency must stay empty, not the guess"
    g = fills_capture.fill_row(Fill(con=Con(exch="NOSUCHPTS"), com=None), rate)
    assert g["commission_ccy"] == "" and g["commission"] == 0.0
    # the same con_id earlier in the ledger answers it
    k = fills_capture.fill_row(Fill(con=Con(exch="NOSUCHPTS"), com=Com()), rate,
                               {"14017331": "JPY"})
    assert (k["ccy"], k["commission_ccy"], k["gbp_rate"], k["flags"]) == \
        ("JPY", "JPY", 0.0047, ["ccy_inferred"]), k
    # ...else the venue (the socket backend, or a row the shim left empty)
    v = fills_capture.fill_row(Fill(con=Con(exch="JPNNEXT"), com=Com()), rate)
    assert (v["ccy"], v["commission_ccy"], v["flags"]) == ("JPY", "JPY", ["ccy_inferred"]), v
    # the pocket path (no rate function) values nothing, as before
    v = fills_capture.fill_row(Fill(con=Con(exch="JPNNEXT"), com=Com()))
    assert v["ccy"] == "JPY" and v["gbp_rate"] is None and "rate_missing" in v["flags"]
    # a conversion is not answered by a stock's con_id or by the venue
    fx = fills_capture.fill_row(Fill(con=Con(sym="USD", exch="JPNNEXT", con_id=7, sec="CASH"),
                                     com=Com()), rate, {"7": "JPY"})
    assert fx["ccy"] == "USD" and "ccy_guessed" in fx["flags"]
    # a currency IB DID send is never second-guessed, and a real commission
    # currency that differs from the fill's is kept
    n = fills_capture.fill_row(Fill(con=Con(ccy="USD", exch="NOSUCHPTS"), com=Com(1.0, "USD")),
                               rate, {"14017331": "JPY"})
    assert (n["ccy"], n["commission_ccy"], n["flags"]) == ("USD", "USD", []), n
    j = fills_capture.fill_row(Fill(con=Con(ccy="JPY", exch="TSEJ"), com=Com(1.0, "USD")), rate)
    assert j["commission_ccy"] == "USD"
    # every key the ledger has always had is still written
    for key in ("execId", "date", "ts", "symbol", "con_id", "sec_type", "side", "qty", "price",
                "ccy", "commission", "commission_ccy", "gbp_rate", "gbp_rate_commission",
                "source", "name", "exchange", "flags"):
        assert key in k, key
    print("t3 fill_row infers by con_id then venue, a guess never reaches commission_ccy OK")


EXPECT_7272 = {"ccy": "JPY", "commission_ccy": "JPY", "gbp_rate": 0.00471524,
               "gbp_rate_commission": 0.00471524, "flags": ["ccy_repaired"]}


def t4_the_repair_rewrites_only_the_guessed_line():
    other_day = json.loads(BAD_7272)
    other_day.update(execId="old1", date="2026-10-01", ts="2026-10-01 00:00",
                     symbol="ABCD", con_id=0, exchange="ISLAND")      # venue only, no sweep rate
    today_row = dict(other_day, execId="today1", date="2026-11-10", ts="2026-11-10 14:30")
    lost = dict(other_day, execId="lost1", exchange="NOSUCHPTS", symbol="9999")
    lines = [BUY_7272, "{not json", USDJPY, BAD_7272, "", json.dumps(other_day),
             json.dumps(today_row), json.dumps(lost)]
    p = ledger(lines)
    before = p.read_bytes()
    with Using(p):
        n = fills_capture._heal_guessed_ccy(lambda ccy: 0.7501, today="2026-11-10")
    after = p.read_bytes()
    assert n == 3, n
    assert b"\r" not in after, "the repair wrote CRLF"
    bl, al = before.split(b"\n"), after.split(b"\n")
    assert len(bl) == len(al)
    changed = [i for i, (x, y) in enumerate(zip(bl, al)) if x != y]
    assert changed == [3, 5, 6], changed           # the three guessed rows, nothing else
    r = json.loads(al[3])
    want = dict(json.loads(BAD_7272), **EXPECT_7272)
    assert r == want, r
    assert list(r) == list(json.loads(BAD_7272)), "the row's key order changed"
    o = json.loads(al[5])
    assert (o["ccy"], o["gbp_rate"], o["flags"]) == ("USD", None, ["rate_missing", "ccy_repaired"]), \
        "a rate from another day must never be stamped as that day's: %r" % o
    t = json.loads(al[6])
    assert (t["ccy"], t["gbp_rate"], t["flags"]) == ("USD", 0.7501, ["ccy_repaired"]), t
    lost_after = json.loads(al[7])
    assert lost_after["flags"] == ["rate_missing", "ccy_guessed"], "an unknown venue was guessed"
    # idempotent: nothing left to do, file untouched
    with Using(p):
        assert fills_capture._heal_guessed_ccy(lambda ccy: 9.9, today="2026-11-10") == 0
    assert p.read_bytes() == after
    # a CRLF file keeps its CRLF (a Windows checkout): only the one line changes
    q = ledger([BUY_7272, USDJPY, BAD_7272], end="\r\n")
    b2 = q.read_bytes()
    with Using(q):
        assert fills_capture._heal_guessed_ccy(None, today="2026-12-01") == 1
    a2 = q.read_bytes()
    assert a2.count(b"\r\n") == b2.count(b"\r\n") == 3
    assert a2.split(b"\r\n")[:2] == b2.split(b"\r\n")[:2]
    assert json.loads(a2.split(b"\r\n")[2])["gbp_rate"] == 0.00471524
    # no ledger, no error
    with Using(_TMP / "missing.jsonl"):
        assert fills_capture._heal_guessed_ccy() == 0
    print("t4 the repair rewrites only guessed lines, LF stays LF, idempotent, "
          "rate from the same day's sweep (7272 -> 0.00471524) OK")


def t5_capture_infers_new_fills_and_heals_old_ones():
    p = ledger([BUY_7272, USDJPY, BAD_7272])

    class CapIB:
        def __init__(self, trades):
            self.trades = trades

        def reqExecutions(self, *a, **k):
            return shim_fills(self.trades)

        def sleep(self, *a):
            pass
    with Using(p):
        # a later 7272 fill on a venue nobody has heard of: answered by con_id
        fills_capture.capture(CapIB([trade("NOSUCHPTS", execution_id="new1")]),
                              lambda ccy: 0.0047)
        rows = {r["execId"]: r for r in fills_capture._ledger_rows()}
    assert rows["new1"]["ccy"] == "JPY" and rows["new1"]["flags"] == ["ccy_inferred"], rows["new1"]
    assert rows["new1"]["commission_ccy"] == "JPY" and rows["new1"]["gbp_rate"] == 0.0047
    healed = rows["00011ebe.6af0f3a4.01.01"]
    assert {k: healed[k] for k in EXPECT_7272} == EXPECT_7272, healed
    # (_append's own lines are CRLF when this runs on Windows - text mode - but
    # the three lines written here as LF must still be LF)
    assert p.read_bytes().startswith((BUY_7272 + "\n" + USDJPY + "\n").encode())
    print("t5 capture() answers a currency-less fill by con_id and repairs the old row OK")


def _real_rows():
    led = REPO / "data" / "fills_ledger.jsonl"
    return [json.loads(l) for l in led.read_text(encoding="utf-8").splitlines() if l.strip()]


def t6_the_bot_pocket_is_unchanged_by_the_repair():
    """TRADING PATH. earmark.bot_pocket reads this ledger. The repair must not
    move the bot's own HKD by a cent, on the real ledger, for any anchor."""
    rows = _real_rows()
    # the real ledger with the bad 7272 row, whatever its current state on disk
    bad = [r for r in rows if r.get("execId") != "00011ebe.6af0f3a4.01.01"]
    bad.append(json.loads(BAD_7272))
    p = ledger([json.dumps(r) for r in bad])
    with Using(p):
        fills_capture._heal_guessed_ccy(None, today="2026-11-10")
        good = fills_capture._ledger_rows()
    g7272 = [r for r in good if r["execId"] == "00011ebe.6af0f3a4.01.01"]
    assert len(g7272) == 1 and g7272[0]["ccy"] == "JPY", g7272
    stamped = [r for r in rows if str(r.get("order_ref") or "").startswith("mps-")]
    assert stamped, "the real ledger carries no stamped bot fill - the test is vacuous"
    for anchor in ("2026-10-20 00:00", "2026-11-04 00:00", "2026-11-08 23:35",
                   "2026-11-09 10:00", "2026-11-09 00:00"):
        pb, db = earmark.bot_pocket(bad, anchor, "HKD")
        pg, dg = earmark.bot_pocket(good, anchor, "HKD")
        assert pb == pg and pb is not None, (anchor, pb, pg, db, dg)
        assert db == dg, (anchor, db, dg)
    known = {"14017331": "JPY"}
    assert earmark.hkd_delta(json.loads(BAD_7272), "HKD", known) is None
    assert earmark.hkd_delta(g7272[0], "HKD", known) is None
    assert earmark.hkd_delta(g7272[0], "HKD", {}) is None
    print("t6 the bot's HKD pocket is identical before and after the repair (5 anchors) OK")


def t7_the_one_intended_trading_change():
    """LOUD: a numeric (Japanese) ticker's FIRST fill on JPNNEXT, with no earlier
    fill of that stock in the ledger. The old table left it with no currency,
    so the pocket read it as "unknown" (it might be HKD: SEHK codes are numeric)
    and refused to compute P for the run. Now it is JPY and moves no HKD."""
    t = [trade("JPNNEXT", sym="7203", conid=55555)]
    now = fills_capture.fill_rows(shim_fills(t))
    assert now[0]["ccy"] == "JPY" and earmark.hkd_delta(now[0], "HKD", {}) is None
    saved = venue_ccy.EXCH_CCY
    try:
        venue_ccy.EXCH_CCY = {k: v for k, v in saved.items() if k not in ("JPNNEXT", "CHIXJ")}
        then = fills_capture.fill_rows(shim_fills(t))
    finally:
        venue_ccy.EXCH_CCY = saved
    assert then[0]["ccy"] == "USD" and "ccy_guessed" in then[0]["flags"]
    assert earmark.hkd_delta(then[0], "HKD", {}) == "unknown"
    # a venue that is STILL unknown keeps the fail-safe
    still = fills_capture.fill_rows(shim_fills([trade("NOSUCHPTS", sym="7203", conid=55555)]))
    assert earmark.hkd_delta(still[0], "HKD", {}) == "unknown"
    print("t7 INTENDED CHANGE: a first 7203 fill on JPNNEXT is 'no HKD' (was 'unknown'); "
          "an unknown venue is still 'unknown' OK")


def t8_the_tax_report_values_the_sale():
    buy, sell = json.loads(BUY_7272), json.loads(BAD_7272)
    before = uk_cgt.compute([buy, sell], today=date(2026, 12, 1), div_rows=[])
    p = ledger([BUY_7272, USDJPY, BAD_7272])
    with Using(p):
        fills_capture._heal_guessed_ccy(None, today="2026-12-01")
        rows = fills_capture._ledger_rows()
    after = uk_cgt.compute(rows, today=date(2026, 12, 1), div_rows=[])
    db = [d for d in before["disposals"] if d["symbol"] == "7272"]
    da = [d for d in after["disposals"] if d["symbol"] == "7272"]
    assert db and db[0]["proceeds_unvalued"] and db[0]["gain_gbp"] is None, db
    assert da and not da[0]["proceeds_unvalued"] and da[0]["ccy"] == "JPY", da
    assert da[0]["proceeds_gbp"] == 806.78 and da[0]["basis_quality"] == "captured", da
    assert abs(da[0]["gain_gbp"] - (-27.86)) < 0.011, da
    print("t8 the repaired 7272 sale is valued: proceeds 806.78 GBP, gain -27.86 OK")


if __name__ == "__main__":
    t1_the_venue_table()
    t2_the_shim_reads_the_venue()
    t3_fill_row_infers_and_never_stamps_a_guess()
    t4_the_repair_rewrites_only_the_guessed_line()
    t5_capture_infers_new_fills_and_heals_old_ones()
    t6_the_bot_pocket_is_unchanged_by_the_repair()
    t7_the_one_intended_trading_change()
    t8_the_tax_report_values_the_sale()
    print("ALL FILLS CURRENCY TESTS PASS")
