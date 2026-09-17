#!/usr/bin/env python3
"""Golden tests for the bot's stamped HKD pocket (earmark.bot_pocket).

Run from this directory:  python test_bot_pocket.py

What this closes. The earmark used to be min(marker, HKD held), which reaches
into the bot's own HKD whenever the marker is above the operator's own money -
marked before the GBP converts, or left set after the withdrawal. NetLiq was
understated by the bot's funding, and the spend guard read that funding as
earmarked and converted ANOTHER ~14,500 of USD into HKD that may never be sold
back. Four automatic remedies failed (see earmark.py); the fourth counted the
operator's own GBP->HKD conversion as the bot's because the rows look alike.

The pocket identifies the bot's executions POSITIVELY, by the "mps-" order_ref
IB echoes from the cOID every bot order sends. With H = HKD held, M = marker,
P = pocket, E = exclusion:

    E     = min(M, max(0, H - clamp(P, 0, H)))      (P known)
    spend = max(0, min(P, H - E_frozen) - committed)
    P unknown -> exactly the old min(M, H) and the old spend formula.

The six scenarios use the map's own numbers (M = 23,746; FX leg +14,500; SEHK
buy 14,100; sale 15,000; a 7 HKD residue that is the operator's), each with
today's figure beside it so the change is visible, not just the result.
Nothing here touches /root: the earmark directory, the orders ledger and every
ledger path point at temp dirs set BEFORE the modules are imported.
"""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("IB_BACKEND", "web")
_TMP = Path(tempfile.mkdtemp(prefix="mps-pocket-"))
os.environ["MPS_EARMARK_DIR"] = str(_TMP / "earmark")
os.environ["MPS_ORDERS_LEDGER"] = str(_TMP / "orders_ledger.jsonl")
(_TMP / "earmark").mkdir()
os.environ.pop("EXCLUDED_CASH", None)

import earmark                                     # noqa: E402
import broker                                      # noqa: E402
import fills_capture                               # noqa: E402
import ib_bot                                      # noqa: E402
import ib_orders                                   # noqa: E402

BASE = ib_bot.BASE_CCY                             # "HKD"
EDIR = _TMP / "earmark"
REPO = Path(__file__).resolve().parent.parent
ANCHOR = "2026-09-01 00:00"
M = 23746.0
NOW = datetime(2026, 9, 17, 23, 35, 20, tzinfo=timezone.utc)
assert str(earmark.MARKER_FILE).startswith(str(EDIR)), earmark.MARKER_FILE
assert ib_orders.ORDERS_LEDGER == str(_TMP / "orders_ledger.jsonl")

_ABSENT = object()


def row(eid, ts, sym, sec, side, qty, price, ccy, com=0.0, com_ccy=None,
        ref=_ABSENT, oid=None, source="api", con_id=0, flags=None):
    r = {"execId": eid, "date": ts[:10], "ts": ts, "symbol": sym, "con_id": con_id,
         "sec_type": sec, "side": side, "qty": qty, "price": price, "ccy": ccy,
         "commission": com, "commission_ccy": ccy if com_ccy is None else com_ccy,
         "gbp_rate": None, "gbp_rate_commission": None, "source": source,
         "name": "", "exchange": "", "flags": flags or []}
    if ref is not _ABSENT:
        r["order_ref"], r["order_id"] = ref, oid
    return r


def stamp(conid, side="B"):
    return "mps-%s-%s-20260917233512" % (conid, side)


# The rows every scenario is built from. The confirmation row is a real-shaped
# bot EUR.USD leg: it moves no HKD, it only proves IB echoes the stamp.
CONFIRM = row("c1", "2026-09-02 21:15", "EUR", "CASH", "BOT", 1539.0, 1.15988, "USD",
              2.0, ref=stamp(12087792), oid=111)
POT = row("op1", "2026-09-05 07:43", "GBP", "CASH", "SLD", 2000.0, 11.873, BASE)   # +23,746, unstamped
POT_APP = dict(POT, execId="op2", order_ref=None, order_id=222)   # the key present, no stamp
FX_LEG = row("fx1", "2026-09-06 23:35", "USD", "CASH", "SLD", 1856.0, 7.8125, BASE,
             ref=stamp(12345777, "S"), oid=333)                   # +14,500, the bot's
HK_BUY = row("hk1", "2026-09-07 01:30", "2359", "STK", "BOT", 100.0, 141.0, BASE,
             ref=stamp(4403), oid=444)                            # -14,100, the bot's
HK_SELL = row("hk2", "2026-09-08 01:30", "2359", "STK", "SLD", 100.0, 150.0, BASE,
              ref=stamp(4403, "S"), oid=555)                      # +15,000, the bot's


class Patch:
    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(ib_bot, k)
            setattr(ib_bot, k, v)
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(ib_bot, k, v)
        reset_run()


def reset_run():
    ib_bot._FX_COMMITTED.clear()
    ib_bot._FX_PENDING_CCY.clear()
    ib_bot._FX_PENDING.clear()
    ib_bot._EARMARK_RUN.clear()
    ib_bot._EXC_APPLIED.clear()
    ib_bot._POCKET_RUN.clear()
    del ib_bot.PLACED[:]


def clean_dir():
    for f in EDIR.iterdir():
        f.unlink()


def mark(amount):
    earmark.set_marker(amount)


def pocket_of(rows, anchor=ANCHOR):
    return earmark.bot_pocket(rows, anchor, BASE)[0]


def run_numbers(H, P, committed=0.0):
    """(E for NetLiq, spend) exactly as run() derives them: net_liq's exclusion,
    the freeze after reservations, then _spendable_base."""
    reset_run()
    ib_bot._POCKET_RUN.update(active=True, p=P, confirmed=P is not None)
    e = earmark.exclusion(H, P)
    with Patch(cash_by_ccy=lambda ib: {BASE: H}):
        ib_bot._FX_COMMITTED[BASE] = committed
        ib_bot._POCKET_RUN.update(active=True, p=P, confirmed=P is not None)
        ib_bot._EARMARK_RUN["base"] = min(earmark.exclusion(H, P), max(0.0, H - committed))
        spend = ib_bot._spendable_base(None)
    return e, spend


def today_numbers(H, committed=0.0):
    """The pre-pocket rule on the same balances, for the side-by-side."""
    e = earmark.effective(H)
    return e, max(0.0, H - min(e, max(0.0, H - committed)) - committed)


def close(a, b, tol=1e-6):
    return abs(a - b) <= tol


# ---------------------------------------------------------------- tests ----
def t1_hkd_delta_reads_the_real_row_shapes():
    d = earmark.hkd_delta
    # API CASH rows: symbol = pair BASE, ccy = QUOTE (data/fills_ledger.jsonl)
    assert close(d(POT), 23746.0)                                   # SLD GBP, receive HKD
    assert close(d(FX_LEG), 14500.0)                                # SLD USD.HKD
    usd_buy = row("x", "2026-09-06 13:33", "USD", "CASH", "BOT", 135.99, 7.84597, BASE)
    assert close(d(usd_buy), -1066.9734603, 1e-6)                   # BOT USD.HKD pays HKD
    hkd_jpy = row("x", "2026-09-06 09:20", "HKD", "CASH", "BOT", 2000.0, 20.355, "JPY")
    assert d(hkd_jpy) == 2000.0                                     # HKD is the pair base
    assert d(dict(hkd_jpy, side="SLD")) == -2000.0
    seed = row("x", "2026-07-20 00:00", "HKD.JPY", "CASH", "SLD", 14208, 20.70, "JPY",
               source="estimate")
    assert d(seed) == -14208.0                                      # the seed spelling
    # HKD stock and commissions
    assert d(HK_BUY) == -14100.0 and d(HK_SELL) == 15000.0
    assert close(d(dict(HK_BUY, commission=55.5)), -14155.5)
    assert close(d(row("x", "t", "GBP", "CASH", "SLD", 1750.0, 10.61405, BASE, 15.68)),
                 18558.9075)
    assert close(d(dict(CONFIRM, commission=15.67, commission_ccy=BASE)), -15.67)
    assert d(dict(POT, commission=3.0, commission_ccy="")) == 23743.0   # old empty field -> ccy
    # not HKD at all
    assert d(CONFIRM) is None
    assert d(row("x", "t", "USD", "CASH", "SLD", 1847.0, 156.055, "JPY", 312.26)) is None
    assert d(row("x", "t", "UNH", "STK", "BOT", 5.0, 300.0, "USD", 1.0)) is None
    # a guessed currency: unknown when it might be HKD, resolved by con_id when
    # the ledger knows the pair, and never HKD for an alphabetic ticker
    g = row("x", "t", "USD", "CASH", "SLD", 2.0, 7.8, "USD", con_id=12345777,
            flags=["ccy_guessed"])
    assert d(g) == "unknown"
    assert close(d(g, BASE, {"12345777": BASE}), 15.6)
    assert d(dict(g, symbol="HKD")) == -2.0                         # base known: fine
    assert d(row("x", "t", "2359", "STK", "BOT", 100, 141, "USD", flags=["ccy_guessed"])) \
        == "unknown"
    assert d(row("x", "t", "DELL", "STK", "BOT", 4, 120, "USD", flags=["ccy_guessed"])) is None
    print("t1 HKD delta reads the real ledger row shapes OK")


def t2_golden_real_operator_conversion_counts_zero():
    """The row that broke remedy #4: GBP.HKD SLD 1750 @ 10.61405, 2026-08-31.

    Read from the REAL ledger. It carries no stamp, so it is the operator's pot
    and P must be 0 - bot_base('2026-08-31') returned 18,601 and effective
    returned 0 instead of 18,559."""
    rows = [json.loads(l) for l in (REPO / "data" / "fills_ledger.jsonl")
            .read_text(encoding="utf-8").splitlines() if l.strip()]
    gbp = [r for r in rows if r.get("execId") == "0002247a.6a94e317.01.01"]
    assert len(gbp) == 1, "the golden row is gone from the ledger"
    g = gbp[0]
    assert (g["symbol"], g["sec_type"], g["side"], g["qty"], g["ccy"]) == \
        ("GBP", "CASH", "SLD", 1750.0, BASE), g
    assert "order_ref" not in g                    # captured before the key existed
    since = [r for r in rows if str(r.get("ts")) >= "2026-08-31 00:00"]
    # unconfirmed (no row anywhere carries an mps- stamp yet): the fallback
    p, detail = earmark.bot_pocket(rows, "2026-08-31 00:00", BASE)
    assert p is None and "not confirmed" in detail["reason"], detail
    # confirmed by one stamped bot fill: the operator's 18,559 is NOT the bot's
    p, detail = earmark.bot_pocket(rows + [CONFIRM], "2026-08-31 00:00", BASE)
    assert p == 0.0, (p, detail)
    assert earmark.bot_pocket([g, CONFIRM], "2026-08-31 00:00", BASE)[0] == 0.0
    assert detail["unstamped_in_ignored"] >= 1, detail
    mark(18559)
    assert earmark.exclusion(18581.0, p) == 18559.0     # not the 0 remedy #4 returned
    assert earmark.exclusion(18581.0, None) == 18559.0
    assert len(since) >= 5          # not vacuous: the window really holds the month's rows
    print("t2 golden: the real 2026-08-31 operator conversion counts 0 OK")


def t3_pocket_rules():
    rows = [CONFIRM, POT, FX_LEG, HK_BUY]
    assert pocket_of(rows) == 400.0                 # 14,500 - 14,100; the pot adds nothing
    assert pocket_of(rows + [POT_APP]) == 400.0     # key present, not stamped: still pot
    sweep_in = row("s1", "2026-09-06 21:15", "USD", "CASH", "SLD", 2.0, 7.835, BASE)
    assert pocket_of(rows + [sweep_in]) == 400.0    # unstamped HKD-in: ignored
    sweep_out = row("s2", "2026-09-07 13:33", "USD", "CASH", "BOT", 10.0, 7.8, BASE)
    assert close(pocket_of(rows + [sweep_out]), 322.0)   # unstamped HKD-out: charged
    old_bot = dict(FX_LEG, execId="old", ts="2026-08-20 23:35")
    assert pocket_of(rows + [old_bot]) == 400.0     # before the anchor: ignored
    undated = dict(FX_LEG, execId="nd", ts="")
    p, detail = earmark.bot_pocket(rows + [undated], ANCHOR, BASE)
    assert p == 400.0 and detail["undated"] == 1, detail
    neg = pocket_of([CONFIRM, HK_BUY])
    assert neg == -14100.0                          # raw sum may be negative; callers clamp
    # the fallbacks
    assert earmark.bot_pocket(rows, None, BASE)[0] is None           # no anchor
    assert earmark.bot_pocket([POT, dict(FX_LEG, order_ref="IB-123")], ANCHOR, BASE)[0] \
        is None                                                      # nothing stamped
    guessed = row("g1", "2026-09-10 00:00", "USD", "CASH", "BOT", 5.0, 7.8, "USD",
                  flags=["ccy_guessed"], con_id=999)
    p, detail = earmark.bot_pocket(rows + [guessed], ANCHOR, BASE)
    assert p is None and "unknown" in detail["reason"], detail
    # ...but the same con_id with a known quote elsewhere resolves it
    known = row("k1", "2026-08-01 00:00", "USD", "CASH", "SLD", 1.0, 7.8, BASE, con_id=999)
    assert close(pocket_of(rows + [guessed, known]), 400.0 - 39.0)
    print("t3 pocket counts stamped fully, unstamped out only OK")


def t4_canary_order_id_join():
    led = Path(ib_orders.ORDERS_LEDGER)
    led.write_text("\n".join([
        json.dumps({"ts": "x", "event": "submit", "coid": "mps-1", "conid": 1}),
        "{not json",
        json.dumps({"ts": "x", "event": "submitted", "coid": "mps-1", "order_id": "333"}),
        json.dumps({"ts": "x", "event": "submitted", "coid": "mps-2", "order_id": None}),
    ]) + "\n", encoding="utf-8")
    ids = earmark.bot_submitted_order_ids(str(led))
    assert ids == {"333"}, ids
    assert earmark.bot_submitted_order_ids(str(_TMP / "nope.jsonl")) == set()
    assert earmark.bot_submitted_order_ids(None) == set()
    # IB returned the bot's own FX leg (order 333) WITHOUT its stamp
    dropped = dict(FX_LEG, order_ref=None, order_id=333)
    p, detail = earmark.bot_pocket([CONFIRM, POT, dropped], ANCHOR, BASE, ids)
    assert p is None and detail["broken"] == ["fx1"] and "CANARY" in detail["reason"], detail
    assert detail["confirmed"] is False
    # a non-mps ref on a bot order trips it too (int/str order_id parity)
    p, _ = earmark.bot_pocket([CONFIRM, dict(FX_LEG, order_ref="IB-x", order_id="333 ")],
                              ANCHOR, BASE, ids)
    assert p is None
    # the operator's app order (not in the bot's ledger) is just pot
    assert earmark.bot_pocket([CONFIRM, POT_APP, FX_LEG], ANCHOR, BASE, ids)[0] == 14500.0
    # a row with NO key (captured before the key existed) is not evidence
    no_key = {k: v for k, v in FX_LEG.items() if k not in ("order_ref", "order_id")}
    assert earmark.bot_pocket([CONFIRM, no_key], ANCHOR, BASE, ids)[0] == 0.0
    led.unlink()
    print("t4 canary: a bot order back without its stamp disables the pocket OK")


def t5_scenario_1_2_marks_before_or_after_converting():
    mark(M)
    # 1. marks before converting: H = 7, the GBP has not landed
    p = pocket_of([CONFIRM])
    assert (p, run_numbers(7.0, p)) == (0.0, (7.0, 0.0))
    assert today_numbers(7.0) == (7.0, 0.0)
    # ...the conversion lands (unstamped, the operator's)
    p = pocket_of([CONFIRM, POT])
    assert (p, run_numbers(23753.0, p)) == (0.0, (23746.0, 0.0))
    assert today_numbers(23753.0) == (23746.0, 7.0)   # today the 7 residue was spendable
    # 2. converts then marks: the same numbers, whenever the marker is set
    earmark.set_marker(0)
    assert run_numbers(23753.0, 0.0) == (0.0, 0.0)    # unmarked pot: in NetLiq, never spent
    assert today_numbers(23753.0) == (0.0, 23753.0)   # today the bot could spend the pot
    mark(M)
    assert run_numbers(23753.0, 0.0) == (23746.0, 0.0)
    print("t5 scenarios 1-2: marking before or after the conversion OK")


def t6_scenario_3_bot_funds_an_hk_buy():
    mark(M)
    # 3. marker live and COVERED: FX leg +14,500 lands on top of the pot
    p = pocket_of([CONFIRM, POT, FX_LEG])
    assert p == 14500.0
    assert run_numbers(38253.0, p) == (23746.0, 14500.0)
    assert today_numbers(38253.0) == (23746.0, 14507.0)
    # the buy fills
    p = pocket_of([CONFIRM, POT, FX_LEG, HK_BUY])
    assert (p, run_numbers(24153.0, p)) == (400.0, (23746.0, 400.0))
    assert today_numbers(24153.0) == (23746.0, 407.0)
    # 3b. the GBP has NOT converted yet: the old limitation's home ground
    p = pocket_of([CONFIRM, FX_LEG])
    assert run_numbers(14507.0, p) == (7.0, 14500.0)
    assert today_numbers(14507.0) == (14507.0, 0.0)   # bot funding excluded, unspendable
    p = pocket_of([CONFIRM, FX_LEG, HK_BUY])
    assert run_numbers(407.0, p) == (7.0, 400.0)
    assert today_numbers(407.0) == (407.0, 0.0)
    p = pocket_of([CONFIRM, FX_LEG, HK_BUY, POT])
    assert run_numbers(24153.0, p) == (23746.0, 400.0)
    # the working buy before it fills: committed is subtracted once, not twice
    p = pocket_of([CONFIRM, FX_LEG])
    assert run_numbers(14507.0, p, committed=14170.5) == (7.0, 329.5)
    print("t6 scenario 3: covered and uncovered bot funding OK")


def t7_scenario_4_5_proceeds_and_withdrawal():
    mark(M)
    # 4. the bot sells a HK stock and holds the proceeds; stale marker, H was 7
    p = pocket_of([CONFIRM, HK_SELL])
    assert run_numbers(15007.0, p) == (7.0, 15000.0)
    assert today_numbers(15007.0) == (15007.0, 0.0)
    p = pocket_of([CONFIRM, HK_SELL, POT])            # ...with the pot present
    assert run_numbers(38753.0, p) == (23746.0, 15000.0)
    # 5. operator withdraws but leaves the marker (a withdrawal is no execution)
    p = pocket_of([CONFIRM, POT])
    assert run_numbers(7.0, p) == (7.0, 0.0)
    p = pocket_of([CONFIRM, POT, FX_LEG])             # ...then the bot funds
    assert run_numbers(14507.0, p) == (7.0, 14500.0)
    assert today_numbers(14507.0) == (14507.0, 0.0)
    # ...and the order did not go through: the next run must NOT re-convert
    calls = []
    for P, expect_calls in ((p, 0), (None, 1)):
        reset_run()
        with Patch(cash_by_ccy=lambda ib: {BASE: 14507.0, "USD": 4000.0},
                   fund_from_nonbase=lambda ib, c, short, dry, buffer=1.02:
                   calls.append(short) or True):
            ib_bot._POCKET_RUN.update(active=True, p=P)
            ib_bot._EARMARK_RUN["base"] = min(earmark.exclusion(14507.0, P), 14507.0)
            assert ib_bot.ensure_ccy(None, BASE, 14100.0, False) is True
        assert len(calls) == expect_calls, (P, calls)
    assert close(calls[0], 14100.0)                   # today: ~14,100 more USD into HKD
    print("t7 scenarios 4-5: sale proceeds and withdrawal-then-funding OK")


def t8_scenario_6_ib_fee_sweep():
    mark(M)
    # the bot's leg carries a 15.67 HKD commission; IB sweeps 2 USD into HKD to
    # pay it (unstamped HKD-in). H is unchanged and so is E.
    fee_leg = dict(CONFIRM, execId="c2", commission=15.67, commission_ccy=BASE)
    sweep = row("s1", "2026-09-06 21:15", "USD", "CASH", "SLD", 2.0, 7.835, BASE)
    p = pocket_of([CONFIRM, fee_leg, sweep])
    assert close(p, -15.67)
    assert run_numbers(7.0, p) == (7.0, 0.0)          # clamped: no negative pocket
    assert run_numbers(23753.0, pocket_of([CONFIRM, POT, fee_leg, sweep])) == (23746.0, 0.0)
    # an unmatched unstamped HKD-in adds at most its size, only when uncovered
    assert close(run_numbers(7.0 + 15.67, pocket_of([CONFIRM, sweep]))[0], 22.67)
    # an unstamped HKD-OUT (the shape of 2026-07-27's 135.99 USD bought with
    # HKD) is charged to the pocket, so the pot figure holds
    usd_buy = row("s3", "2026-09-08 13:33", "USD", "CASH", "BOT", 135.99, 7.84597, BASE)
    p = pocket_of([CONFIRM, POT, FX_LEG, usd_buy])
    assert close(p, 14500.0 - 1066.9734603)
    e, spend = run_numbers(38253.0 - 1066.9734603, p)
    assert close(e, 23746.0) and close(spend, 14500.0 - 1066.9734603), (e, spend)
    print("t8 scenario 6: IB fee sweep leaves the exclusion alone OK")


def t9_unconfirmed_stamping_is_exactly_todays_rule():
    mark(M)
    unstamped = [dict(r, order_ref=None, order_id=None) for r in (POT, FX_LEG, HK_BUY)]
    p, detail = earmark.bot_pocket(unstamped, ANCHOR, BASE)
    assert p is None and not detail["confirmed"]
    for H, committed in ((7.0, 0.0), (14507.0, 0.0), (38253.0, 14100.0), (24153.0, 0.0),
                         (0.0, 0.0), (-500.0, 0.0), (14307.0, 14100.0)):
        assert earmark.exclusion(H, None) == earmark.effective(H)
        assert run_numbers(H, None, committed) == today_numbers(H, committed), (H, committed)

    # ...and NetLiq through net_liq itself, inside a run with p None
    class AV:
        def __init__(self, tag, value, currency):
            self.tag, self.value, self.currency = tag, value, currency

    class IBv:
        def accountValues(self):
            return [AV("NetLiquidation", "300000", BASE), AV("CashBalance", "14507", BASE)]

    reset_run()
    ib_bot._POCKET_RUN.update(active=True, p=None)
    assert ib_bot.net_liq(IBv()) == 300000.0 - 14507.0
    assert ib_bot._EXC_APPLIED == {"base": 14507.0, "bot": None}
    ib_bot._POCKET_RUN.update(active=True, p=14500.0)
    assert ib_bot.net_liq(IBv()) == 300000.0 - 7.0
    assert ib_bot._EXC_APPLIED == {"base": 7.0, "bot": 14500.0}
    reset_run()
    print("t9 unconfirmed stamping gives exactly today's numbers OK")


# ------------------------------------------------------ run-level harness --
class Ex:
    def __init__(self, r):
        self.execId = r["execId"]
        self.time = datetime.strptime(r["ts"], "%Y-%m-%d %H:%M")
        self.side, self.shares, self.price = r["side"], r["qty"], r["price"]
        self.order_ref, self.order_id = r.get("order_ref"), r.get("order_id")


class Con:
    def __init__(self, r):
        self.symbol, self.currency, self.conId = r["symbol"], r["ccy"], r["con_id"]
        self.secType, self.exchange = r["sec_type"], "IDEALPRO"


class Com:
    def __init__(self, r):
        self.commission, self.currency = r["commission"], r["commission_ccy"]


class Fill:
    def __init__(self, r):
        self.execution, self.contract, self.commissionReport = Ex(r), Con(r), Com(r)


def fills(*rows):
    return [Fill(r) for r in rows]


class AccountIB:
    """Account values plus an executions read; records every order sent."""

    def __init__(self, hkd, execs=(), exec_raises=False, netliq=300000.0):
        self.hkd, self.execs, self.exec_raises = hkd, list(execs), exec_raises
        self.netliq, self.placed, self.exec_reads = netliq, [], 0

    def reqExecutions(self, *a, **k):
        self.exec_reads += 1
        if self.exec_raises:
            raise RuntimeError("trades read failed")
        return self.execs

    def accountValues(self):
        return [broker.AccountValue("NetLiquidation", str(self.netliq), BASE),
                broker.AccountValue("CashBalance", str(self.hkd), BASE),
                broker.AccountValue("CashBalance", "4000", "USD")]

    def reqAllOpenOrders(self):
        pass

    def openTrades(self):
        return []

    def sleep(self, *a):
        pass

    def qualifyContracts(self, *c):
        return list(c)

    def positions(self):
        return []

    def placeOrder(self, contract, order):
        self.placed.append((order.action, order.totalQuantity, contract.symbol))
        raise AssertionError("no order may reach the wire in this harness")

    def disconnect(self):
        pass


def write_ledger(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def snapshot_files():
    return {p.name: p.read_bytes() for p in sorted(EDIR.iterdir())}


def t10_anchor_only_on_live_h_below_1_and_never_dry():
    mark(M)
    ledger = _TMP / "fills_anchor.jsonl"
    write_ledger(ledger, [CONFIRM])
    before_ledger = ledger.read_bytes()
    with Patch(FILLS_LEDGER=ledger, _now_utc=lambda: NOW):
        clean_dir()
        mark(M)
        # H >= 1: no anchor, however live
        out = ib_bot._sweep_pocket(AccountIB(500.0, fills(CONFIRM)), dry=False)
        assert out["anchor"] is None and out["p"] is None, out
        assert not earmark.ANCHOR_FILE.exists()
        # H < 1 under --dry: previewed in memory, NOT written
        before = snapshot_files()
        out = ib_bot._sweep_pocket(AccountIB(0.4, fills(CONFIRM)), dry=True)
        assert out["anchor"] == "2026-09-17 23:35" and out["p"] == 0.0, out
        assert snapshot_files() == before, "--dry wrote into the earmark dir"
        assert ledger.read_bytes() == before_ledger, "--dry touched the fills ledger"
        # H < 1 live: written
        out = ib_bot._sweep_pocket(AccountIB(0.4, fills(CONFIRM)), dry=False)
        assert earmark.read_anchor() == "2026-09-17 23:35" == out["anchor"], out
        assert out["p"] == 0.0 and out["confirmed"] is True
        # an existing anchor is left alone while HKD is held...
        earmark.write_anchor("2026-09-01 00:00")
        out = ib_bot._sweep_pocket(AccountIB(14507.0, fills(CONFIRM, FX_LEG)), dry=False)
        assert earmark.read_anchor() == "2026-09-01 00:00" and out["p"] == 14500.0, out
        # ...and advanced when it reaches 0 again (negative counts too)
        out = ib_bot._sweep_pocket(AccountIB(-50.0, fills(CONFIRM)), dry=False)
        assert earmark.read_anchor() == "2026-09-17 23:35", out
        # an execution sharing the minute: the anchor is NOT moved
        earmark.write_anchor("2026-09-01 00:00")
        same = dict(HK_BUY, execId="same", ts="2026-09-17 23:35")
        out = ib_bot._sweep_pocket(AccountIB(0.0, fills(CONFIRM, same)), dry=False)
        assert earmark.read_anchor() == "2026-09-01 00:00", out
        assert ledger.read_bytes() == before_ledger, "the sweep appended to the ledger"
    clean_dir()
    print("t10 anchor moves only on a live run seeing HKD < 1, never under --dry OK")


def t11_sweep_refuses_an_unreadable_or_partial_executions_read():
    mark(M)
    ledger = _TMP / "fills_partial.jsonl"
    recent = dict(HK_BUY, execId="recent", ts="2026-09-16 01:30")
    write_ledger(ledger, [CONFIRM, FX_LEG, recent])
    with Patch(FILLS_LEDGER=ledger, _now_utc=lambda: NOW):
        clean_dir()
        mark(M)
        earmark.write_anchor(ANCHOR)
        # the read fails: never "no fills"
        out = ib_bot._sweep_pocket(AccountIB(407.0, exec_raises=True), dry=False)
        assert out["p"] is None and out["active"] is True, out
        # the read is empty although the ledger holds a 1-day-old fill: partial
        out = ib_bot._sweep_pocket(AccountIB(407.0, []), dry=False)
        assert out["p"] is None, out
        # a complete read: the stamped buy is in, P = 400
        out = ib_bot._sweep_pocket(AccountIB(407.0, fills(recent)), dry=False)
        assert out["p"] == 400.0, out
        # the fresh copy supplies an order_ref the ledger row never had
        bare = {k: v for k, v in FX_LEG.items() if k not in ("order_ref", "order_id")}
        write_ledger(ledger, [bare])
        out = ib_bot._sweep_pocket(AccountIB(14507.0, fills(FX_LEG)), dry=False)
        assert out["p"] == 14500.0 and out["confirmed"], out
        # an IB object without reqExecutions (the socket rollback, a test fake)
        out = ib_bot._sweep_pocket(object(), dry=False)
        assert out["p"] is None, out
    clean_dir()
    rows, missing = earmark.merge_executions(
        [dict(CONFIRM, ts="2026-09-15 00:00"), dict(POT, source="manual", ts="2026-09-16 00:00"),
         dict(FX_LEG, ts="2026-08-01 00:00")], [], NOW)
    assert missing == ["c1"], missing                   # recent api only; manual/old ignored
    print("t11 unreadable or partial executions read falls back OK")


def t12_in_run_conversion_into_hkd_grows_the_pocket():
    class Tr:
        def __init__(self, status):
            self.orderStatus = broker._OrderStatus(status)
            self.log = []

    class FxIB(AccountIB):
        def __init__(self, status="Filled"):
            AccountIB.__init__(self, 7.0)
            self.status = status

        def qualifyContracts(self, *c):
            for x in c:
                x.conId = 12345777
            return list(c)

        def placeOrder(self, contract, order):
            self.placed.append((order.action, order.totalQuantity, contract.localSymbol))
            return Tr(self.status)

    rate = lambda ib, a, b: 7.8125 if (a, b) == ("USD", BASE) else 1.0   # noqa: E731
    with Patch(confirm=lambda m: True, fx_rate=rate):
        ib_bot._POCKET_RUN.update(active=True, p=0.0)
        ib = FxIB()
        assert ib_bot._fx_order(ib, "USD", BASE, "SELL", 1856, False, "USD->HKD",
                                target=BASE, src_ccy="USD", src_qty=1856) is True
        assert ib_bot._POCKET_RUN["p"] == 14500.0
        # BASE as the pair's base: BUY HKD.JPY receives qty HKD
        assert ib_bot._fx_order(FxIB(), BASE, "JPY", "BUY", 14100, False, "JPY->HKD",
                                target=BASE, src_ccy="JPY", src_qty=280000) is True
        assert ib_bot._POCKET_RUN["p"] == 28600.0
        # not filled: nothing added
        ib_bot._FX_PENDING.clear()
        assert ib_bot._fx_order(FxIB("Submitted"), "USD", BASE, "SELL", 1856, False,
                                "USD->HKD", target=BASE) is False
        assert ib_bot._POCKET_RUN["p"] == 28600.0
        # into another currency: nothing added
        ib_bot._FX_PENDING.clear()
        assert ib_bot._fx_order(FxIB(), "USD", "JPY", "SELL", 1847, False, "USD->JPY",
                                target="JPY") is True
        assert ib_bot._POCKET_RUN["p"] == 28600.0
        # --dry never transmits and never grows it
        ib = FxIB()
        assert ib_bot._fx_order(ib, "USD", BASE, "SELL", 1856, True, "USD->HKD",
                                target=BASE) is True
        assert ib.placed == [] and ib_bot._POCKET_RUN["p"] == 28600.0
        # pocket unknown: stays unknown
        ib_bot._POCKET_RUN.update(p=None)
        assert ib_bot._fx_order(FxIB(), "USD", BASE, "SELL", 1856, False, "USD->HKD",
                                target=BASE) is True
        assert ib_bot._POCKET_RUN["p"] is None
    print("t12 an in-run conversion into HKD grows the pocket by its order size OK")


def run_scenario(tmp, hkd, execs, dry):
    """ib_bot.run() end to end on one HK candidate (2359.HK, 100 x 141 = 14,100)
    with REAL net_liq, _sweep_pocket, reserve_working_cash, the freeze,
    ensure_ccy and _spendable_base. Only the edges are stubbed."""
    state = tmp / "state.json"
    state.write_text("{}", encoding="utf-8")
    signals = {"generated": "2026-09-17", "actions": [
        {"symbol": "2359.HK", "action": "BUY", "price": 141.0, "score": 9, "stop": 120}]}
    ib = AccountIB(hkd, execs)
    funded, sent, published = [], [], []

    class C:
        def __init__(self):
            self.symbol, self.currency, self.secType, self.conId = "2359", BASE, "STK", 4403

    stubs = dict(
        STATE=state, IB=lambda: ib, connect_or_heal=lambda *a, **k: None,
        get_json=lambda url: signals, _now_utc=lambda: NOW,
        warm_fx_memory=lambda ib, actions: None, held_positions=lambda ib: {},
        to_ib=lambda ysym: C(), currency_of=lambda ysym: BASE,
        entry_blocked_reason=lambda ysym, ccy: None, lot_size=lambda ib, c: 100,
        fx_rate=lambda ib, a, b: 1.0 if a == b else 7.8,
        fund_from_nonbase=lambda ib, c, short, dry, buffer=1.02: funded.append(short) or False,
        place=lambda ib, c, action, qty, price, dry, reason="", mkt=False:
            sent.append((action, qty, c.symbol, dry)) or "sent",
        publish_state=lambda ib, st, nl: published.append(nl),
        FILLS_LEDGER=tmp / "fills.jsonl")
    with Patch(**stubs):
        ib_bot.run(dry=dry)
        exc = dict(ib_bot._EXC_APPLIED)
    return ib, funded, sent, published, exc


def t13_run_end_to_end_withdrawal_then_funding():
    """Scenario 5 through run(): the marker was left set after the withdrawal,
    last run's stamped FX leg (+14,500) filled but its HK buy was refused. The
    bot must size and spend its own HKD - not convert ~14,100 more USD - and a
    live run writes the pocket file with the new buy pending; --dry writes
    nothing and places nothing."""
    tmp = Path(tempfile.mkdtemp(prefix="mps-pocket-run-", dir=str(_TMP)))
    write_ledger(tmp / "fills.jsonl", [CONFIRM, POT])
    clean_dir()
    mark(M)
    earmark.write_anchor(ANCHOR)
    ledger_before = (tmp / "fills.jsonl").read_bytes()

    before = snapshot_files()
    ib, funded, sent, published, exc = run_scenario(tmp, 14507.0, fills(FX_LEG), dry=True)
    assert snapshot_files() == before, "--dry wrote the anchor or the pocket file"
    assert (tmp / "fills.jsonl").read_bytes() == ledger_before
    assert published == [] and ib.placed == []
    assert funded == [], funded                                # no re-conversion
    assert sent == [("BUY", 100, "2359", True)], sent
    assert exc == {"base": 7.0, "bot": 14500.0}, exc           # NetLiq excludes 7, not 14,507

    ib, funded, sent, published, exc = run_scenario(tmp, 14507.0, fills(FX_LEG), dry=False)
    assert funded == [] and sent == [("BUY", 100, "2359", False)], (funded, sent)
    assert published == [300000.0 - 7.0], published
    body = json.loads(earmark.POCKET_FILE.read_text(encoding="utf-8"))
    assert body["confirmed"] is True and body["p"] == 14500.0, body
    assert body["pending"] == 14170.5 and body["anchor"] == ANCHOR, body   # 14,100 x 1.005
    assert body["at"] == "2026-09-17T23:35:20Z"
    assert (tmp / "fills.jsonl").read_bytes() == ledger_before  # capture stays in publish_state
    # a publisher reading that file while the buy works: over-excludes (safe)
    assert earmark.publisher_exclusion(14507.0, NOW) == (min(M, 14507.0 - 329.5), 329.5)

    # CONTROL - the same balances with stamping unconfirmed: today's behaviour,
    # the bot's own 14,500 is excluded and it asks to convert 14,100 more
    clean_dir()
    mark(M)
    earmark.write_anchor(ANCHOR)
    unstamped = dict(FX_LEG, order_ref=None, order_id=None)
    write_ledger(tmp / "fills.jsonl", [dict(CONFIRM, order_ref=None), POT])
    ib, funded, sent, published, exc = run_scenario(tmp, 14507.0, fills(unstamped), dry=False)
    assert funded == [14100.0] and sent == [], (funded, sent)
    assert exc == {"base": 14507.0, "bot": None}, exc
    assert published == [300000.0 - 14507.0], published
    body = json.loads(earmark.POCKET_FILE.read_text(encoding="utf-8"))
    assert body["confirmed"] is False and body["p"] is None, body
    assert earmark.publisher_exclusion(14507.0, NOW) == (14507.0, None)
    clean_dir()
    print("t13 run(): withdrawal then funding spends the bot's own HKD; dry writes nothing OK")


def t14_publishers_fall_back_on_a_stale_or_missing_pocket_file():
    clean_dir()
    mark(M)
    H = 14507.0
    fallback = (earmark.effective(H), None)
    assert earmark.publisher_exclusion(H, NOW) == fallback               # missing
    earmark.write_pocket(14500.0, 0.0, True, ANCHOR, NOW - timedelta(hours=37))
    assert earmark.publisher_exclusion(H, NOW) == fallback               # stale
    earmark.write_pocket(14500.0, 0.0, True, ANCHOR, NOW + timedelta(hours=2))
    assert earmark.publisher_exclusion(H, NOW) == fallback               # from the future
    earmark.write_pocket(None, 0.0, True, ANCHOR, NOW)
    assert earmark.publisher_exclusion(H, NOW) == fallback               # p unknown
    earmark.write_pocket(14500.0, 0.0, False, ANCHOR, NOW)
    assert earmark.publisher_exclusion(H, NOW) == fallback               # unconfirmed
    earmark.POCKET_FILE.write_text("{torn", encoding="utf-8")
    assert earmark.publisher_exclusion(H, NOW) == fallback               # corrupt
    # fresh and confirmed
    earmark.write_pocket(14500.0, 0.0, True, ANCHOR, NOW - timedelta(hours=35))
    assert earmark.publisher_exclusion(H, NOW) == (7.0, 14500.0)
    # a bot buy still working: P - pending, over-excluding until it fills
    earmark.write_pocket(14500.0, 14100.0, True, ANCHOR, NOW)
    assert earmark.publisher_exclusion(H, NOW) == (14107.0, 400.0)
    assert earmark.publisher_exclusion(407.0, NOW) == (7.0, 400.0)       # ...filled
    earmark.write_pocket(14500.0, 20000.0, True, ANCHOR, NOW)
    assert earmark.publisher_exclusion(H, NOW) == (H, 0.0)               # never negative

    # ib_bot.net_liq OUTSIDE a run (publish_only, ib_commands) reads the file
    reset_run()
    earmark.write_pocket(14500.0, 0.0, True, ANCHOR, datetime.now(timezone.utc))
    assert ib_bot.net_liq(AccountIB(H)) == 300000.0 - 7.0
    assert ib_bot._EXC_APPLIED == {"base": 7.0, "bot": 14500.0}
    clean_dir()
    mark(M)
    assert ib_bot.net_liq(AccountIB(H)) == 300000.0 - H                  # no file: today
    assert ib_bot._EXC_APPLIED == {"base": H, "bot": None}

    # publish_web end to end, with git stubbed and every path in a temp dir
    import subprocess
    import publish_web
    import ib_web
    repo = Path(tempfile.mkdtemp(prefix="mps-pw-", dir=str(_TMP)))
    (repo / "data").mkdir()
    (repo / "state.json").write_text('{"map": {}, "pos": {}}', encoding="utf-8")
    old = (publish_web.STATE, publish_web.DATA, publish_web.REPO, ib_web.snapshot,
           subprocess.run, publish_web.sys.argv)

    class Done:
        returncode, stdout, stderr = 0, "", ""

    try:
        publish_web.STATE, publish_web.DATA, publish_web.REPO = \
            repo / "state.json", repo / "data", repo
        ib_web.snapshot = lambda: {"positions": [], "netliq": 300000.0,
                                   "cash": {BASE: H, "USD": 4000.0}}
        subprocess.run = lambda *a, **k: Done()
        publish_web.sys.argv = ["publish_web.py"]
        for pocket_file, want in ((None, (H, None)), ((14500.0, 0.0), (7.0, 14500))):
            clean_dir()
            mark(M)
            if pocket_file:
                earmark.write_pocket(pocket_file[0], pocket_file[1], True, ANCHOR,
                                     datetime.now(timezone.utc))
            assert publish_web.main() == 0
            snap = json.loads((repo / "data" / "bot_state.json").read_text(encoding="utf-8"))
            assert (snap["excluded_cash"], snap["earmark_bot_hkd"]) == want, snap
            assert snap["netliq"] == round(300000.0 - want[0]), snap
            assert snap["earmark_marker"] == M
    finally:
        (publish_web.STATE, publish_web.DATA, publish_web.REPO, ib_web.snapshot,
         subprocess.run, publish_web.sys.argv) = old
    clean_dir()
    print("t14 publishers use a fresh pocket file and fall back on a stale or missing one OK")


def t15_never_sell_hkd_guards_untouched_with_a_known_pocket():
    assert ib_bot.FX_CONVERT is False
    reset_run()
    ib_bot._POCKET_RUN.update(active=True, p=50000.0)

    class Refuse:
        qualify_calls = 0

        def qualifyContracts(self, *a):
            Refuse.qualify_calls += 1
            raise AssertionError("reached order construction")

    # BUY USD.HKD pays HKD; SELL HKD.JPY sells HKD: both refused before a contract
    assert ib_bot._fx_order(Refuse(), "USD", BASE, "BUY", 100, False, "HKD->USD") is False
    assert ib_bot._fx_order(Refuse(), BASE, "JPY", "SELL", 100, False, "HKD->JPY") is False
    assert Refuse.qualify_calls == 0
    assert ib_bot._fx_order_pair(None, BASE, "USD", 100.0, 10.0, False) is False
    picked = []
    with Patch(cash_by_ccy=lambda ib: {BASE: 60000.0, "JPY": 83346.0},
               fx_rate=lambda ib, a, b: 20.0,
               _fx_order_pair=lambda ib, s, d, qs, qd, dry: picked.append(s) or True):
        ib_bot._POCKET_RUN.update(active=True, p=50000.0)
        assert ib_bot.fund_from_nonbase(None, "USD", 100.0, False) is True
        assert picked == ["JPY"], picked               # the HKD pocket is never a source
    with Patch(cash_by_ccy=lambda ib: {BASE: 60000.0}):
        ib_bot._POCKET_RUN.update(active=True, p=50000.0)
        assert ib_bot.fund_from_nonbase(None, "USD", 100.0, False) is False
    reset_run()
    print("t15 never-sell-HKD guards untouched with a known pocket OK")


def t16_capture_keeps_order_ref_additively():
    trades = [
        {"execution_id": "e1", "symbol": "USD", "sec_type": "CASH", "conid": 12345777,
         "side": "S", "size": "1856", "price": "7.8125", "commission": "0",
         "exchange": "IDEALPRO", "currency": BASE, "trade_time": "20260917-23:35:12",
         "order_ref": "mps-12345777-S-20260917233512", "order_id": 987654},
        {"execution_id": "e2", "symbol": "GBP", "sec_type": "CASH", "conid": 12345775,
         "side": "S", "size": "1750", "price": "10.61405", "commission": "15.68",
         "exchange": "IDEALPRO", "currency": BASE, "trade_time": "20260930-07:43:00"},
    ]
    old_trades = ib_orders.trades
    try:
        ib_orders.trades = lambda days=7: trades
        got = broker.IB().reqExecutions()
        assert [f.execution.order_ref for f in got] == \
            ["mps-12345777-S-20260917233512", None], got
        assert [f.execution.order_id for f in got] == [987654, None]

        def boom(days=7):
            raise RuntimeError("503")
        ib_orders.trades = boom
        assert broker.IB().reqExecutions() == []            # the tax sweep: unchanged
        try:
            broker.IB().reqExecutions(strict=True)
            raise AssertionError("strict read swallowed a failure")
        except RuntimeError:
            pass
        ib_orders.trades = lambda days=7: trades

        led = _TMP / "capture.jsonl"
        old_ledger = fills_capture.LEDGER
        fills_capture.LEDGER = led
        try:
            class CapIB:
                def reqExecutions(self, *a, **k):
                    return broker.IB().reqExecutions()

                def sleep(self, *a):
                    pass
            fills_capture.capture(CapIB(), lambda ccy: 0.094)
            rows = [json.loads(l) for l in led.read_text(encoding="utf-8").splitlines()]
        finally:
            fills_capture.LEDGER = old_ledger
    finally:
        ib_orders.trades = old_trades
    by = {r["execId"]: r for r in rows}
    assert by["e1"]["order_ref"] == "mps-12345777-S-20260917233512"
    assert by["e1"]["order_id"] == 987654
    assert by["e2"]["order_ref"] is None and "order_id" in by["e2"]
    seeds = [r for r in rows if r.get("source") == "estimate"]
    assert seeds and all("order_ref" not in r for r in seeds)
    # every pre-existing key is still there with the same meaning
    for k in ("execId", "date", "ts", "symbol", "con_id", "sec_type", "side", "qty",
              "price", "ccy", "commission", "commission_ccy", "gbp_rate",
              "gbp_rate_commission", "source", "name", "exchange", "flags"):
        assert k in by["e1"], k
    assert (by["e1"]["ts"], by["e1"]["side"], by["e1"]["qty"], by["e1"]["gbp_rate"]) == \
        ("2026-09-17 23:35", "SLD", 1856.0, 0.094)
    # a socket-era Execution (no order_ref attribute) gets no keys at all
    socket_row = fills_capture.fill_row(Fill(dict(HK_BUY, ts="2026-09-07 01:30")))
    assert "order_ref" in socket_row                   # our Fake carries the attribute...

    class SockEx:
        execId, side, shares, price = "s1", "BOT", 1.0, 10.0
        time = datetime(2026, 9, 7, 1, 30)

    class SockFill:
        execution, contract, commissionReport = SockEx(), Con(HK_BUY), Com(HK_BUY)
    assert "order_ref" not in fills_capture.fill_row(SockFill())   # ...ib_async's does not
    # uk_cgt reads by key: the extra keys change nothing
    import uk_cgt
    from datetime import date
    base = [row("a", "2026-07-01 10:00", "T", "STK", "BOT", 100, 10.0, "USD", 1.0),
            row("b", "2026-08-01 10:00", "T", "STK", "SLD", 100, 12.0, "USD", 1.0)]
    for r in base:
        r["gbp_rate"] = r["gbp_rate_commission"] = 0.75
    stamped = [dict(r, order_ref=stamp(1), order_id=9) for r in base]
    a = uk_cgt.compute(base, today=date(2026, 12, 1), div_rows=[])
    b = uk_cgt.compute(stamped, today=date(2026, 12, 1), div_rows=[])
    a.pop("generated", None)
    b.pop("generated", None)
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
    print("t16 capture keeps order_ref/order_id additively; uk_cgt unaffected OK")


if __name__ == "__main__":
    try:
        t1_hkd_delta_reads_the_real_row_shapes()
        t2_golden_real_operator_conversion_counts_zero()
        t3_pocket_rules()
        t4_canary_order_id_join()
        t5_scenario_1_2_marks_before_or_after_converting()
        t6_scenario_3_bot_funds_an_hk_buy()
        t7_scenario_4_5_proceeds_and_withdrawal()
        t8_scenario_6_ib_fee_sweep()
        t9_unconfirmed_stamping_is_exactly_todays_rule()
        t10_anchor_only_on_live_h_below_1_and_never_dry()
        t11_sweep_refuses_an_unreadable_or_partial_executions_read()
        t12_in_run_conversion_into_hkd_grows_the_pocket()
        t13_run_end_to_end_withdrawal_then_funding()
        t14_publishers_fall_back_on_a_stale_or_missing_pocket_file()
        t15_never_sell_hkd_guards_untouched_with_a_known_pocket()
        t16_capture_keeps_order_ref_additively()
    finally:
        reset_run()
    print("ALL BOT POCKET TESTS PASS")
