#!/usr/bin/env python3
"""Golden tests: the kill switch with a 20-weekday timer runs in SHADOW only.

Run from this directory:  python test_ks_shadow.py

The owner approved it on 2026-11-05: "agree to run in shadow, also add a line
on performance compare chart to show this". The rule is the one the 5 Nov
study tested (arm X8N20) - a day-end close of trading wealth 8% or more below
its peak blocks new buys for 20 weekdays from that day, the timer runs on
whatever the account does, the peak resets on the last day, exits are never
blocked - kept in execution/ks_shadow.py and replayed from the first history
row on every run. Locked down here, with no network:

  * THE RULE IS THE STUDY'S: on synthetic closes the replay blocks exactly the
    runs the study's simulator blocks (KSim._timer, transcribed below line for
    line), and with deposits, withdrawals and missing rows it trips and resets
    exactly where the study's real-account reading did (a2_real.timer,
    transcribed) - also on the repo's own data/netliq_history.json;
  * an order belongs to the evening (23:35 UTC) run from 23:00 UTC on a
    weekday, else it obeys the last weekday's close: the trip day's 23:35 run
    and the last day's morning run are blocked, the last day's 23:35 run buys;
  * SHADOW NEVER CHANGES AN ORDER: through the REAL run(), place() and
    publish_state - only the broker, the rates and the clock are stubbed -
    the orders IB receives, the state map/pos, the slots, the exits and every
    other log line are identical with KS_SHADOW "off" and "shadow"; only the
    "shadow_ks" tag on the BUY rows, two log lines and the published
    kill_switch_shadow block differ;
  * an unreadable, missing or empty history, or unusable settings, are
    "unknown": logged, published, nothing tagged, the same orders, no raise;
  * "off" logs and tags nothing; any other value ("live" included) is NOT
    built and runs as shadow; --dry logs but records nothing;
  * publish_state writes the run's block or carries the last one, and
    publish_web.py carries it untouched; nothing is read from or written to
    state.json for it, and the old KILL_SWITCH stays False and is never run.

Nothing here touches /root: every path is repointed before ib_bot is imported.
"""
import datetime as dt
import json
import math
import os
import random
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("IB_BACKEND", "web")
import testenv                                     # noqa: E402
_TMP = testenv.isolate("mps-ksshadow-")
os.environ.pop("EXCLUDED_CASH", None)
for _v in ("MAX_ORDER_BASE", "TARGET_POSITIONS", "DAILY_LOSS_KILL", "LIMIT_BUFFER",
           "FX_CONVERT", "FX_FUND_NONBASE", "BASE_FUND_BUFFER", "HK_ENABLED"):
    os.environ.pop(_v, None)

import alerts                                      # noqa: E402
import broker                                      # noqa: E402
import ib_bot                                      # noqa: E402
import ib_web                                      # noqa: E402
import ks_shadow as K                              # noqa: E402
testenv.assert_isolated()

BASE = ib_bot.BASE_CCY
HIST = Path(os.environ["MPS_NETLIQ_HISTORY"])      # the temp file kill_switch reads
UTC = timezone.utc
FLOWS = [{"d": "2026-07-31", "amt": 23746, "note": "deposit"},
         {"d": "2026-08-02", "amt": -32000, "note": "withdrawal"}]
TAG = "kill switch would have blocked (8%, 20 weekdays, until 2026-12-02)"


# ------------------------------------------------- the study's two timers --
class KSimTimer:
    """KSim._timer from the study's ksim.py, line for line (self.ks holds x
    and n). Fed every weekday: the 09:00 run first, then the 23:35 run with
    that day's NetLiq - the simulator has no flows and no missing days."""

    def __init__(self, x, n):
        self.ks = {"x": x, "n": n}
        self.t_peak, self.t_on, self.t_count, self.t_len = -math.inf, False, 0, 0
        self.t_resets = []

    def _timer(self, day, run, nl):
        if run != "2335":
            return self.t_on
        if self.t_on:
            self.t_count += 1
            if self.t_count >= self.t_len:
                self.t_resets.append((day, self.t_peak, nl))
                self.t_peak = nl
                self.t_on = False
                return False
            return True
        self.t_peak = max(self.t_peak, nl)
        if nl <= (1.0 - self.ks["x"]) * self.t_peak:
            self.t_on, self.t_count, self.t_len = True, 1, self.ks["n"]
            return True
        return False


def a2_real_timer(S, F, x, n):
    """a2_real.timer from the study's real-account reading, line for line:
    S {day: nl} (weekend rows ignored), F [(day, amt)]. Returns its events
    [("trip", day, wealth, fall) | ("reset", day, wealth)], on, cnt."""
    wd = K.weekdays(min(S), max(S))

    def net_in(d):
        return sum(a for fd, a in F if fd <= d)
    peak, on, cnt = -1e18, False, 0
    ev = []
    for d in wd:
        nl_ = float(S[d]) if d in S else None
        if on:
            cnt += 1
            if cnt >= n:
                val = nl_ - net_in(d) if nl_ is not None else None
                if val is None:
                    cnt = n - 1
                    continue
                ev.append(("reset", d, val))
                peak, on = val, False
            continue
        if nl_ is None:
            continue
        val = nl_ - net_in(d)
        peak = max(peak, val)
        fall = 1 - nl_ / (peak + net_in(d))
        if fall >= x:
            on, cnt = True, 1
            ev.append(("trip", d, val, fall))
    return ev, on, cnt


def at(day, hh, mm=0):
    d = dt.date.fromisoformat(day)
    return datetime(d.year, d.month, d.day, hh, mm, 20, tzinfo=UTC)


def walk(rng, days, start=200000.0, crash=0.03, floor=1000.0):
    """A random NetLiq path with the odd sharp fall, so trips, resets and
    re-trips all happen."""
    v, out = start, {}
    for d in days:
        r = rng.gauss(0.0004, 0.011)
        if rng.random() < crash:
            r -= rng.uniform(0.03, 0.09)
        v = max(floor, v * (1 + r))
        out[d] = round(v, 2)
    return out


def t1_the_replay_blocks_exactly_the_runs_the_study_simulator_blocks():
    seen = {"trips": 0, "resets": 0, "retrips": 0}
    for seed in range(60):
        rng = random.Random(20261105 + seed)
        x, n = rng.choice([(0.08, 20), (0.08, 10), (0.08, 40), (0.12, 20), (0.15, 40)])
        days = K.weekdays("2024-01-01", "2026-11-04")
        nl = walk(rng, days)
        sim = KSimTimer(x, n)
        b09, b23 = {}, {}
        for d in days:
            b09[d] = sim._timer(d, "0900", None)
            b23[d] = sim._timer(d, "2335", nl[d])
        r = K.replay([], nl, x, n, days[-1])
        for d in days:
            assert (K.block_for(r["blocks"], at(d, 10)) is not None) == b09[d], (seed, d, "09:00")
            assert (K.block_for(r["blocks"], at(d, 23, 35)) is not None) == b23[d], (seed, d)
        assert [b["end"] for b in r["blocks"] if b["reset"]] == [z[0] for z in sim.t_resets]
        assert [z[2] for z in sim.t_resets] == [b["reset_nl"] for b in r["blocks"] if b["reset"]]
        assert r["on"] == sim.t_on and (not r["on"] or r["count"] == sim.t_count), seed
        # a full block is n weekdays: n - 1 blocked 23:35 runs and n - 1 blocked 09:00 runs
        for b in r["blocks"]:
            if b["reset"]:
                assert len(K.weekdays(b["start"], b["end"])) == n, b
                span = K.weekdays(b["start"], b["end"])
                assert sum(b23[d] for d in span) == n - 1 and sum(b09[d] for d in span) == n - 1
        seen["trips"] += len(r["blocks"])
        seen["resets"] += sum(b["reset"] for b in r["blocks"])
        seen["retrips"] += max(0, len(r["blocks"]) - 1)
    assert seen["trips"] > 60 and seen["resets"] > 40 and seen["retrips"] > 20, seen
    # 8% exactly trips, as the simulator compares it (NetLiq <= (1 - X) x peak)
    r = K.replay([], {"2026-10-12": 250000.0, "2026-10-13": 230000.0}, 0.08, 20, "2026-10-13")
    assert [b["start"] for b in r["blocks"]] == ["2026-10-13"], r
    r = K.replay([], {"2026-10-12": 250000.0, "2026-10-13": 230000.01}, 0.08, 20, "2026-10-13")
    assert r["blocks"] == [], r
    print("t1 on %d synthetic paths the replay blocks exactly the 09:00 and 23:35 runs the "
          "study's KSim._timer blocks (%d trips, %d resets, %d re-trips), a full block is "
          "N weekdays, 8%% exactly trips OK" % (60, seen["trips"], seen["resets"],
                                                 seen["retrips"]))


def _events(r):
    out = []
    for b in r["blocks"]:
        out.append(("trip", b["start"]))
        if b["reset"]:
            out.append(("reset", b["end"]))
    return sorted(out, key=lambda e: (e[1], e[0] != "trip"))


def t2_with_flows_and_missing_rows_it_is_the_studys_real_account_reading():
    count = 0
    for seed in range(60):
        rng = random.Random(7 + seed)
        x, n = rng.choice([(0.08, 20), (0.08, 10), (0.12, 20)])
        days = K.weekdays("2025-03-03", "2026-11-04")
        nl = walk(rng, days, floor=100000.0)
        # money in and out: NetLiq moves with each flow on its own day on;
        # kept within +-60,000 in all, so NetLiq stays positive as a real
        # history's does (read_history drops a row that is not)
        flows, tot = [], 0.0
        for d in days:
            if rng.random() < 0.02:
                amt = rng.choice([25000.0, -30000.0, 15000.0, -12000.0])
                if abs(tot + amt) <= 60000:
                    flows.append((d, amt))
                    tot += amt
        moved = {}
        for d in days:
            moved[d] = nl[d] + sum(a for fd, a in flows if fd <= d)
        # some weekdays have no row (the bot and the publisher both missed it)
        for d in days[1:-1]:
            if rng.random() < 0.04:
                moved.pop(d)
        ev, on, cnt = a2_real_timer(moved, flows, x, n)
        r = K.replay(flows, moved, x, n, max(moved))
        want = [(e[0], e[1]) for e in ev]
        assert _events(r) == sorted(want, key=lambda e: (e[1], e[0] != "trip")), (seed, ev)
        assert r["on"] == on and (not on or r["count"] == cnt), seed
        trips = [e for e in ev if e[0] == "trip"]
        for b, e in zip(r["blocks"], trips):
            assert abs(b["fall"] - e[3]) < 1e-12 and abs((b["nl"] - net(flows, b["start"]))
                                                         - e[2]) < 1e-6, (b, e)
        count += len(trips)
    assert count > 40, count
    # a recorded deposit is never a high and a withdrawal never a fall
    days = K.weekdays("2026-07-20", "2026-08-07")
    flat = {d: 200000.0 for d in days}
    flows = [("2026-07-31", 23746.0), ("2026-08-03", -32000.0)]
    moved = {d: 200000.0 + sum(a for fd, a in flows if fd <= d) for d in days}
    assert K.replay(flows, moved, 0.08, 20, days[-1])["blocks"] == []
    # ...whereas with its flows left out the same withdrawal trips it - the
    # false trips of 2026 the old switch had
    assert K.replay([], moved, 0.08, 20, days[-1])["blocks"], "the test proves nothing"
    assert K.replay([], flat, 0.08, 20, days[-1])["blocks"] == []
    print("t2 with deposits, withdrawals and missing rows the replay trips and resets on "
          "exactly the days the study's a2_real.timer does (%d trips); a recorded flow is "
          "never a high or a fall OK" % count)


def net(flows, d):
    return sum(a for fd, a in flows if fd <= d)


def t3_on_the_repos_own_history():
    path = Path(testenv.REPO) / "data" / "netliq_history.json"
    flows, series, warn = K.read_history(path)
    assert flows is not None and series, warn
    S = {d: v for d, v in series}
    ev, on, cnt = a2_real_timer(S, flows, 0.08, 20)
    closes = {d: v for d, v in S.items() if K.is_weekday(d)}
    r = K.replay(flows, closes, 0.08, 20, max(closes))
    assert _events(r) == sorted([(e[0], e[1]) for e in ev],
                                key=lambda e: (e[1], e[0] != "trip")), (ev, r["blocks"])
    assert r["on"] == on, (r, on)
    # the facts the study reported, on the closes to 2026-11-04 (the history
    # keeps growing; these days are final): first row 20 Jul, net money in
    # -8,254, the best trading wealth 238,915 on 13 Oct, never 8% below it
    early = {d: v for d, v in closes.items() if d <= "2026-11-04"}
    r4 = K.replay(flows, early, 0.08, 20, "2026-11-04")
    assert r4["first"] == "2026-07-20" and r4["blocks"] == [], r4["blocks"]
    assert round(r4["peak_wealth"]) == 238915 and r4["peak_day"] == "2026-10-13", r4
    assert round(K.net_in(flows, "2026-11-04")) == -8254
    v = K.view(flows, [(d, x) for d, x in series if d <= "2026-11-04"], 214594,
               at("2026-11-05", 10))
    assert (v["line"], v["peak"], v["would_block"]) == (212208, 230661, False), v
    print("t3 on the repo's data/netliq_history.json the replay matches the study's reading "
          "(trips %s); to 2026-11-04: peak wealth 238,915 on 13 Oct, line 212,208 in today's "
          "money, no trip OK" % [b["start"] for b in r["blocks"]])


def t4_which_run_an_order_belongs_to_and_the_timer():
    # a trip on Thu 2026-11-05: weekday 1; Thanksgiving counts; the last day
    # is Wed 2 Dec
    days = K.weekdays("2026-10-01", "2026-12-04")
    nl = {d: (250000.0 if d < "2026-11-05" else 228000.0) for d in days}
    r = K.replay([], nl, 0.08, 20, days[-1])
    assert [(b["start"], b["end"], b["reset"]) for b in r["blocks"]] == [
        ("2026-11-05", "2026-12-02", True)], r["blocks"]
    blocked = lambda t: K.block_for(r["blocks"], t) is not None
    assert not blocked(at("2026-11-05", 10)), "the trip day's morning obeys the day before"
    assert blocked(at("2026-11-05", 23, 35)), "the trip day's 23:35 run places no buys"
    assert blocked(at("2026-11-06", 10)) and blocked(at("2026-11-06", 12))
    assert blocked(at("2026-11-08", 23, 35)), "a Sunday 23:35 run obeys Friday's close"
    assert blocked(at("2026-11-26", 23, 35)), "a US holiday is still a weekday of the block"
    assert blocked(at("2026-12-02", 10)), "the last day's morning run is still blocked"
    assert not blocked(at("2026-12-02", 23, 35)), "the last day's 23:35 run buys again"
    assert not blocked(at("2026-12-03", 0, 10)) and not blocked(at("2026-12-03", 10))
    assert K.governing_close(at("2026-11-07", 10)) == "2026-11-06"
    assert K.governing_close(at("2026-11-09", 22, 59)) == "2026-11-06"
    assert K.governing_close(at("2026-11-09", 23, 0)) == "2026-11-09"
    # recovering inside the block does not end it; falling further does not
    # restart it; after the reset it trips again from the new peak
    nl2 = dict(nl)
    for d in K.weekdays("2026-11-10", "2026-11-20"):
        nl2[d] = 260000.0                              # above the old peak
    nl2["2026-11-23"] = 150000.0                       # a deeper fall
    r2 = K.replay([], nl2, 0.08, 20, days[-1])
    assert [(b["start"], b["end"]) for b in r2["blocks"]] == [("2026-11-05", "2026-12-02")]
    assert r2["peak_wealth"] == 228000.0 and r2["peak_day"] == "2026-12-02", r2
    nl3 = dict(nl, **{"2026-12-03": 228000.0 * 0.92})
    r3 = K.replay([], nl3, 0.08, 20, days[-1])
    assert [b["start"] for b in r3["blocks"]] == ["2026-11-05", "2026-12-03"], r3["blocks"]
    # a weekday with no row inside the block still counts; a last day with no
    # row resets at the next row
    nl4 = {d: v for d, v in nl.items() if d not in ("2026-11-18", "2026-12-02")}
    r4 = K.replay([], nl4, 0.08, 20, days[-1])
    assert [(b["start"], b["end"], b["waited"]) for b in r4["blocks"]] == [
        ("2026-11-05", "2026-12-03", ["2026-12-02"])], r4["blocks"]
    assert blocked(at("2026-12-02", 23, 35)) is False                # the full-row path
    assert K.block_for(r4["blocks"], at("2026-12-02", 23, 35)) is not None
    # a block still running at the end keeps its planned last day
    r5 = K.replay([], {d: v for d, v in nl.items() if d <= "2026-11-12"}, 0.08, 20,
                  "2026-11-12")
    assert r5["on"] and r5["count"] == 6 and r5["blocks"][0]["end"] == "2026-12-02", r5
    print("t4 the trip day's 23:35 run and the last day's morning are blocked, the last "
          "day's 23:35 run buys; weekends obey Friday; holidays and missing rows count; "
          "recovery does not end a block; a re-trip starts from the reset peak OK")


def t5_what_a_run_sees_at_its_hour():
    rows = history_tripping_on("2026-11-05")
    rows += [{"d": "2026-11-01", "nl": 1}, {"d": "2026-11-05", "nl": 200000}]  # a Sunday mark, a live one
    write_history(rows)
    # the 10:00 catch-up: today's row is a mark, never a close
    v = K.bot_view(222002, at("2026-11-05", 10))
    assert v["status"] == "ok" and v["close_through"] == "2026-11-04" and not v["evening"], v
    assert v["would_block"] is False and v["peak"] == 242000 and v["peak_wealth"] == 250254, v
    assert v["peak_date"] == "2026-10-13" and v["line"] == round(0.92 * 242000), v
    assert v["fall_pct"] == round((1 - 222002 / 242000) * 100, 2), v
    # the 23:35 run: its own NetLiq is today's close, the history's mark is not
    v = K.bot_view(222002, at("2026-11-05", 23, 35))
    assert v["evening"] and v["close_through"] == "2026-11-05" and v["would_block"] is True, v
    assert (v["block_start"], v["block_end"], v["block_day"]) == ("2026-11-05", "2026-12-02", 1)
    assert v["trips"] == [{"start": "2026-11-05", "end": "2026-12-02", "reset": False,
                           "fall_pct": 8.26, "netliq": 222002, "peak": 242000,
                           "line": 222640, "peak_date": "2026-10-13"}], v["trips"]
    assert K.bot_view(223000, at("2026-11-05", 23, 35))["would_block"] is False
    # Saturday: Friday's close governs, no trip on a weekend
    rows = history_tripping_on("2026-11-05") + [{"d": "2026-11-05", "nl": 230000},
                                                 {"d": "2026-11-06", "nl": 222002}]
    write_history(rows)
    v = K.bot_view(150000, at("2026-11-07", 23, 35))
    assert v["close_through"] == "2026-11-06" and v["block_start"] == "2026-11-06", v
    assert v["block_day"] == 1 and not v["evening"]
    print("t5 a morning run reads the closes before today, the 23:35 run its own NetLiq as "
          "today's close; a weekend run obeys Friday's OK")


# --------------------------------------------------------------- run() ----
def write_history(rows, flows=FLOWS, raw=None):
    HIST.write_text(raw if raw is not None else json.dumps({"series": rows, "flows": flows}),
                    encoding="utf-8")


def history_tripping_on(day):
    """Weekday rows from 20 Jul to the weekday before `day`, NetLiq moving with
    FLOWS (+23,746 on 31 Jul, -32,000 on 2 Aug) as real money does: trading
    wealth 236,000, and 250,254 at the 13 Oct close (NetLiq 242,000) - never
    8% below its peak. At HK$222,002 (BookIB's NetLiq) the 8% line is
    0.92 x 242,000 = 222,640: tripped."""
    fl = [(f["d"], f["amt"]) for f in FLOWS]
    return [{"d": d, "nl": (250254 if d == "2026-10-13" else 236000) + net(fl, d)}
            for d in K.weekdays("2026-07-20", K.prev_weekday(day))]


HKD_PER = {"HKD": 1.0, "USD": 7.8413, "JPY": 0.04974, "EUR": 8.7932}


def rate(ib_, a, b):
    if a == b:
        return 1.0
    if a in HKD_PER and b in HKD_PER:
        return HKD_PER[a] / HKD_PER[b]
    return 0.0


class Patch:
    def __init__(self, mod, **kw):
        self.mod, self.kw, self.old = mod, kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(self.mod, k)
            setattr(self.mod, k, v)
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(self.mod, k, v)
        for reg in (ib_bot._FX_COMMITTED, ib_bot._FX_PENDING_CCY, ib_bot._FX_PENDING,
                    ib_bot._EARMARK_RUN, ib_bot._EXC_APPLIED, ib_bot._POCKET_RUN,
                    ib_bot._RESERVE_OK, ib_bot._RUN_CONFIG, ib_bot._TICK_CACHE,
                    ib_bot._RUN_MARKET_FILTER, ib_bot._RUN_KS_SHADOW):
            reg.clear()
        del ib_bot._FILTER_SKIPPED_RUN[:]
        del ib_bot._KS_TAGGED_RUN[:]
        del ib_bot.PLACED[:]


def contract(sym, ccy="USD", sec="STK"):
    c = broker.Contract(sym, sec, ccy)
    c.conId = abs(hash((sym, ccy, sec))) % 10 ** 7 + 1
    return c


class BookIB:
    """Account values, positions and orders behind the REAL cash_by_ccy,
    held_positions, reserve_working_cash and place()."""

    def __init__(self, cash, held=(), netliq=222002.0, refuse=()):
        self.cash, self.held = dict(cash), list(held)
        self.netliq, self.refuse = netliq, set(refuse)
        self.placed = []

    def accountValues(self):
        out = [broker.AccountValue("NetLiquidation", str(self.netliq), BASE)]
        return out + [broker.AccountValue("CashBalance", str(v), c)
                      for c, v in self.cash.items()]

    def positions(self):
        return [broker.Position(contract(s, ccy), q, avg) for s, ccy, q, avg in self.held]

    def reqAllOpenOrders(self):
        pass

    def openTrades(self):
        return []

    def sleep(self, *a):
        pass

    def qualifyContracts(self, *cs):
        return list(cs)

    def reqTickers(self, c):
        return [broker.Ticker(last=None)]

    def placeOrder(self, c, order):
        self.placed.append((order.action, order.totalQuantity, c.symbol, order.lmtPrice))
        t = broker.Trade(c, order)
        if c.symbol in self.refuse:
            t.orderStatus = broker._OrderStatus("Inactive")
            t.log.append(broker._LogEntry("order refused by IB"))
        else:
            t.orderStatus = broker._OrderStatus("Submitted")
        return t

    def disconnect(self):
        pass


def buy(sym, price, score, market="US"):
    return {"symbol": sym, "action": "BUY", "price": price, "score": score,
            "stop": round(price * 0.88, 2), "market": market}


CANDS = [buy("NEWA", 250.0, 95), buy("7203.T", 2900.0, 90, market="JP"),
         buy("SAP.DE", 200.0, 85, market="EU"), buy("NEWB", 120.0, 70)]
CASH = {"USD": 20000.0, "JPY": 2000000.0, "EUR": 9000.0}
HELD = [("H%02d" % k, "USD", 1, 10.0) for k in range(8)] + [("7974", "JPY", 100, 9000.0)]
STATE = {"map": {"7974": "7974.T"},
         "pos": {"7974.T": {"entry": 9000, "hw": 9500, "stop": 8000,
                            "entry_date": dt.date.today().isoformat()}}}
CARDS = {"7974.T": {"price": 8500.0, "sma200": 8800.0, "atr": 100.0}}   # a regime-break exit
LAST = {}


def bot_run(ib, run_at, mode="shadow", dry=False, actions=CANDS, **extra):
    """One ib_bot.run() at `run_at` with KS_SHADOW = mode. Returns (exception
    or None, state on disk, log lines, PLACED rows as published, the
    published kill_switch_shadow block or None, publish count)."""
    d = Path(tempfile.mkdtemp(prefix="run-", dir=str(_TMP)))
    path = d / "state.json"
    path.write_text(json.dumps(STATE), encoding="utf-8")
    alerts.DIR = d / "outbox"
    gen = (run_at - dt.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    signals = {"generated": run_at.date().isoformat(), "generated_at": gen,
               "actions": list(actions)}

    def get_json(url):
        if url == ib_bot.SIGNALS_URL:
            return json.loads(json.dumps(signals))
        for ysym, card in CARDS.items():
            if url.endswith("/" + ib_bot.safe_name(ysym) + ".json"):
                return {"card": card, "generated_at": gen}
        raise AssertionError("unexpected fetch " + url)

    lines, rows, published = [], [], []

    def publish(ib_, st, nl):
        published.append(json.loads(json.dumps(ib_bot._ks_shadow_block({}))))
        rows[:] = [dict(r) for r in ib_bot.PLACED]

    stubs = dict(
        STATE=path, IB=lambda: ib, connect_or_heal=lambda *a, **k: None,
        get_json=get_json, _now_utc=lambda: run_at, publish_state=publish,
        fx_rate=rate, lot_size=lambda ib_, c: 100 if c.currency == "JPY" else 1,
        min_tick=lambda ib_, c: 0.01, confirm=lambda msg: True,
        EXIT_ATTEMPTS=d / "exit_attempts.json", FILLS_LEDGER=d / "fills.jsonl",
        ADOPT=d / "adopt.json", KS_SHADOW=mode,
        log=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    stubs.update(extra)
    err = None
    with Patch(ib_bot, **stubs):
        try:
            ib_bot.run(dry=dry)
        except BaseException as e:
            err = e
        if not rows:
            rows[:] = [dict(r) for r in ib_bot.PLACED]
        LAST["ks_reg"] = dict(ib_bot._RUN_KS_SHADOW)        # before the harness clears it
    blk = published[0] if published else None
    return err, json.loads(path.read_text(encoding="utf-8")), lines, rows, blk, len(published)


def strip(lines):
    """Every log line but the shadow's own, the rules line without its mention."""
    out = []
    for l in lines:
        if l.startswith("rules: "):
            out.append(l.replace(", kill switch with timer off", "")
                        .replace(", kill switch with timer 8% / 20 weekdays, log only", ""))
        elif not l.startswith(("kill switch with timer (", "shadow kill switch:",
                               "kill switch shadow")):
            out.append(l)
    return out


def no_tag(rows):
    return [{k: v for k, v in r.items() if k not in ("shadow_ks", "time")} for r in rows]


def t6_shadow_never_changes_an_order():
    write_history(history_tripping_on("2026-11-05"))
    run_at = at("2026-11-05", 23, 35)
    runs = {}
    for mode in ("off", "shadow"):
        ib = BookIB(CASH, held=HELD)
        runs[mode] = (ib,) + bot_run(ib, run_at, mode=mode)
    ib0, e0, st0, l0, r0, b0, p0 = runs["off"]
    ib1, e1, st1, l1, r1, b1, p1 = runs["shadow"]
    assert e0 is None and e1 is None, (e0, e1)
    # the orders IB received - side, size, symbol, limit - are the same, the
    # exit included, and so are the bot's memory and the slots
    assert ib0.placed == ib1.placed, (ib0.placed, ib1.placed)
    assert [a for a, q, s, _ in ib1.placed].count("SELL") == 1 and ib1.placed[0][:3] == (
        "SELL", 100, "7974"), ib1.placed
    assert [s for a, q, s, _ in ib1.placed if a == "BUY"] == ["NEWA", "7203", "SAP", "NEWB"]
    assert st0 == st1, (st0, st1)
    assert p0 == p1 == 1
    assert strip(l0) == strip(l1), [x for x in zip(strip(l0), strip(l1)) if x[0] != x[1]]
    # the History rows differ only by the tag: on every BUY, never the exit
    assert no_tag(r0) == no_tag(r1) and not any("shadow_ks" in r for r in r0)
    tagged = {r["symbol"]: r.get("shadow_ks") for r in r1}
    assert tagged == {"7974": None, "NEWA": TAG, "7203": TAG, "SAP": TAG, "NEWB": TAG}, tagged
    assert not any("shadow" in r for r in r1), "the filter's own field was written"
    # the log: one status line, one summary
    want = ("kill switch with timer (SHADOW - log only, every order is sent as without "
            "it): WOULD BLOCK new buys - trading wealth closed 8.26% below its peak on "
            "2026-11-05 (NetLiq 222,002 against the line 222,640); weekday 1 of 20, buys "
            "would resume at the 23:35 UTC run on 2026-12-02")
    assert want in l1, [l for l in l1 if "kill switch" in l]
    assert ("shadow kill switch: 4 BUY(s) this run would have been blocked - NEWA, 7203.T, "
            "SAP.DE, NEWB; all were sent (log only)") in l1
    assert any("kill switch with timer 8% / 20 weekdays, log only" in l
               for l in l1 if l.startswith("rules: "))
    # the published block
    assert b0 is None and b1["mode"] == "shadow" and b1["status"] == "ok", (b0, b1)
    assert (b1["trip"], b1["days"], b1["would_block"], b1["block_start"], b1["block_end"]) == \
        (0.08, 20, True, "2026-11-05", "2026-12-02"), b1
    assert (b1["peak"], b1["peak_date"], b1["line"], b1["fall_pct"]) == \
        (242000, "2026-10-13", 222640, 8.26), b1
    assert b1["at"] == "2026-11-05 23:35 UTC" and len(b1["trips"]) == 1
    # nothing about it reaches state.json
    assert not [k for k in st1 if "ks" in k.lower() or "kill" in k.lower()], st1
    print("t6 shadow sends the same orders, sizes, exit, state and slots as off; it tags "
          "every BUY 'shadow_ks', logs one status and one summary line, publishes its "
          "block OK")


def t7_a_morning_run_inside_a_block_and_the_last_day():
    # Fri 6 Nov 10:00: the 5 Nov close tripped it; the catch-up obeys it
    rows = history_tripping_on("2026-11-05") + [{"d": "2026-11-05", "nl": 222002}]
    write_history(rows)
    us = [buy("NEWA", 250.0, 95), buy("NEWB", 120.0, 70)]
    out = {}
    for mode in ("off", "shadow"):
        ib = BookIB(CASH, held=HELD)
        out[mode] = (ib,) + bot_run(ib, at("2026-11-06", 10), mode=mode, actions=us)
    (ib0, e0, st0, l0, r0, b0, _), (ib1, e1, st1, l1, r1, b1, _) = out["off"], out["shadow"]
    assert e0 is None and e1 is None and ib0.placed == ib1.placed, (ib0.placed, ib1.placed)
    assert [s for a, q, s, _ in ib1.placed if a == "BUY"] == ["NEWA", "NEWB"], ib1.placed
    assert {r["symbol"]: r.get("shadow_ks") for r in r1 if r["action"] == "BUY"} == \
        {"NEWA": TAG, "NEWB": TAG}, r1
    assert b1["block_day"] == 2 and b1["close_through"] == "2026-11-05" and not b1["evening"]
    # the last day, Wed 2 Dec: its morning run is blocked, its 23:35 run buys
    rows += [{"d": d, "nl": 230000} for d in K.weekdays("2026-11-06", "2026-12-01")]
    write_history(rows)
    ib = BookIB(CASH, held=HELD)
    err, st, lines, rr, blk, _ = bot_run(ib, at("2026-12-02", 10), actions=us)
    assert err is None and blk["would_block"] and blk["block_day"] == 20, blk
    assert all(r.get("shadow_ks") == TAG for r in rr if r["action"] == "BUY") and rr
    ib = BookIB(CASH, held=HELD)
    err, st, lines, rr, blk, _ = bot_run(ib, at("2026-12-02", 23, 35), actions=us)
    assert err is None and blk["would_block"] is False and blk["block_end"] is None, blk
    assert blk["trips"][0]["reset"] is True and blk["peak_date"] == "2026-12-02", blk
    assert not any("shadow_ks" in r for r in rr) and [r["symbol"] for r in rr
                                                      if r["action"] == "BUY"] == ["NEWA", "NEWB"]
    assert any("would not block" in l for l in lines), [l for l in lines if "kill" in l]
    print("t7 the catch-up the day after a trip is tagged; the last day's morning run is "
          "tagged, its 23:35 run is not (the peak resets there) OK")


def t8_an_unusable_history_is_unknown_and_changes_nothing():
    run_at = at("2026-11-05", 23, 35)
    write_history(history_tripping_on("2026-11-05"))
    ref = BookIB(CASH, held=HELD)
    bot_run(ref, run_at, mode="off")
    cases = [("missing", None), ("not json", "{"), ("a list", "[]"),
             ("flows not a list", json.dumps({"series": [{"d": "2026-11-02", "nl": 5}],
                                               "flows": "x"}))]
    for name, raw in cases:
        if raw is None:
            HIST.unlink() if HIST.exists() else None
        else:
            write_history(None, raw=raw)
        ib = BookIB(CASH, held=HELD)
        err, st, lines, rows, blk, pubs = bot_run(ib, run_at)
        assert err is None, (name, repr(err))
        assert ib.placed == ref.placed, (name, ib.placed)
        assert not any("shadow_ks" in r for r in rows), (name, rows)
        assert blk["status"] == "unknown" and blk["why"] and blk["would_block"] is None, (name, blk)
        assert any("kill switch with timer (SHADOW - log only, every order is sent as without "
                   "it): UNKNOWN (" in l and "nothing will be tagged" in l for l in lines), name
        assert pubs == 1
    # no close before today at all (a history with only weekend marks, or
    # none): a catch-up run has nothing to judge - unknown; the 23:35 run
    # starts the rule from its own close, which cannot be 8% below itself
    for raw in (json.dumps({"series": [], "flows": []}),
                json.dumps({"series": [{"d": "2026-11-01", "nl": 5}], "flows": []})):
        write_history(None, raw=raw)
        ib = BookIB(CASH, held=HELD)
        err, st, lines, rows, blk, _ = bot_run(ib, at("2026-11-06", 10))
        assert err is None and blk["status"] == "unknown" and "no day-end close" in blk["why"]
        ib = BookIB(CASH, held=HELD)
        err, st, lines, rows, blk, _ = bot_run(ib, run_at)
        assert err is None and ib.placed == ref.placed, ib.placed
        assert blk["status"] == "ok" and blk["would_block"] is False, blk
        assert blk["first_day"] == "2026-11-05" and not any("shadow_ks" in r for r in rows)
    # settings that are not usable: unknown, never a guess
    write_history(history_tripping_on("2026-11-05"))
    for trip, days in ((0.0, 20), (1.0, 20), (float("nan"), 20), (0.08, 1), (0.08, 2.5)):
        ib = BookIB(CASH, held=HELD)
        err, st, lines, rows, blk, _ = bot_run(ib, run_at, KS_SHADOW_TRIP=trip,
                                               KS_SHADOW_DAYS=days)
        assert err is None and ib.placed == ref.placed, (trip, days)
        assert blk["status"] == "unknown" and "settings" in blk["why"], (trip, days, blk)
        assert not any("shadow_ks" in r for r in rows)
    # a ks_shadow that raises is unknown too, and the exits still ran
    ib = BookIB(CASH, held=HELD)
    import ks_shadow
    with Patch(ks_shadow, bot_view=lambda *a, **k: 1 / 0):
        err, st, lines, rows, blk, _ = bot_run(ib, run_at)
    assert err is None and ib.placed == ref.placed and blk["status"] == "unknown", blk
    print("t8 a missing, unreadable or empty history, unusable settings or a module that "
          "raises: unknown, logged and published; the same orders, no tag, no exception OK")


def t9_off_logs_and_tags_nothing_and_an_unbuilt_mode_runs_as_shadow():
    write_history(history_tripping_on("2026-11-05"))
    run_at = at("2026-11-05", 23, 35)
    ib = BookIB(CASH, held=HELD)
    err, st, lines, rows, blk, _ = bot_run(ib, run_at, mode="off")
    assert err is None and blk is None and not any("shadow_ks" in r for r in rows)
    assert any(l.startswith("kill switch shadow: off") for l in lines), lines
    assert not any("WOULD BLOCK" in l for l in lines)
    assert any("kill switch with timer off," in l for l in lines if l.startswith("rules: "))
    ref = BookIB(CASH, held=HELD)
    bot_run(ref, run_at, mode="shadow")
    for mode in ("live", "on", "", None):
        ib = BookIB(CASH, held=HELD)
        err, st, lines, rows, blk, _ = bot_run(ib, run_at, mode=mode)
        assert err is None and ib.placed == ref.placed, (mode, ib.placed)
        assert any(("KS_SHADOW %r is not built" % (mode,)) in l for l in lines), (mode, lines)
        assert blk["mode"] == "shadow" and {r["symbol"] for r in rows if r.get("shadow_ks")} \
            == {"NEWA", "7203", "SAP", "NEWB"}, (mode, rows)
    # the old switch stays off and is never run by the shadow
    assert ib_bot.KILL_SWITCH is False

    def never(*a, **k):
        raise AssertionError("kill_switch_check ran")
    ib = BookIB(CASH, held=HELD)
    err, st, lines, rows, blk, _ = bot_run(ib, run_at, kill_switch_check=never)
    assert err is None and ib.placed == ref.placed and blk["would_block"] is True
    assert any("kill switch: OFF" in l for l in lines)
    print("t9 off logs and tags nothing; an unbuilt mode runs as shadow and gates nothing; "
          "the old kill switch stays off and is never run OK")


def t10_dry_logs_but_records_nothing():
    write_history(history_tripping_on("2026-11-05"))
    ib = BookIB(CASH, held=HELD)
    err, st, lines, rows, blk, pubs = bot_run(ib, at("2026-11-05", 23, 35), dry=True)
    assert err is None and pubs == 0 and ib.placed == [], (err, pubs, ib.placed)
    assert any("WOULD BLOCK new buys" in l for l in lines), lines
    assert any(l.startswith("shadow kill switch: ") for l in lines), lines
    assert st == STATE and LAST["ks_reg"] == {}, (st, LAST)
    print("t10 --dry logs what the rule says and records nothing OK")


# --------------------------------------------------------------- publishing --
class Done:
    returncode, stdout, stderr = 0, "", ""


class PubIB:
    def positions(self):
        return []

    def accountValues(self):
        return [broker.AccountValue("NetLiquidation", "100000", BASE),
                broker.AccountValue("CashBalance", "10", BASE)]


def seed_repo(prev):
    repo = Path(tempfile.mkdtemp(prefix="repo-", dir=str(_TMP)))
    (repo / "data").mkdir()
    (repo / "execution").mkdir()
    (repo / "execution" / "state.json").write_text('{"map": {}, "pos": {}}', encoding="utf-8")
    (repo / "data" / "bot_state.json").write_text(json.dumps(prev, indent=1), encoding="utf-8")
    return repo


def bot_publish(repo):
    import fills_capture
    import flex_dividends as fd
    import uk_cgt
    old = (ib_bot.__file__, subprocess.run, fills_capture.capture,
           fd.capture_if_configured, uk_cgt.build_report)
    try:
        ib_bot.__file__ = str(repo / "execution" / "ib_bot.py")
        subprocess.run = lambda *a, **k: Done()
        fills_capture.capture = lambda *a, **k: 0
        fd.capture_if_configured = lambda *a, **k: 0
        uk_cgt.build_report = lambda *a, **k: None
        ib_bot.publish_state(PubIB(), {"map": {}, "pos": {}}, 100000.0)
    finally:
        (ib_bot.__file__, subprocess.run, fills_capture.capture,
         fd.capture_if_configured, uk_cgt.build_report) = old
    return json.loads((repo / "data" / "bot_state.json").read_text(encoding="utf-8"))


OLD_KS = {"mode": "shadow", "at": "2026-11-05 10:00 UTC", "trip": 0.08, "days": 20,
          "status": "ok", "would_block": False, "fall_pct": 8.39, "line": 212208,
          "trips": []}


def t11_publish_state_writes_or_carries_the_block_and_publish_web_carries_it():
    ib_bot._RUN_KS_SHADOW.clear()
    ib_bot._RUN_KS_SHADOW.update(dict(OLD_KS, at="2026-11-05 23:35 UTC", would_block=True))
    try:
        snap = bot_publish(seed_repo({"activity": [], "kill_switch_shadow": OLD_KS}))
    finally:
        ib_bot._RUN_KS_SHADOW.clear()
    assert snap["kill_switch_shadow"]["at"] == "2026-11-05 23:35 UTC", snap
    assert snap["kill_switch_shadow"]["would_block"] is True
    # publish_only - no trading run in this process: carried as it is
    snap = bot_publish(seed_repo({"activity": [], "kill_switch_shadow": OLD_KS}))
    assert snap["kill_switch_shadow"] == OLD_KS, snap.get("kill_switch_shadow")
    # nothing to carry: no key, never an invented one
    snap = bot_publish(seed_repo({"activity": []}))
    assert "kill_switch_shadow" not in snap and snap["netliq"] == 100000
    import publish_web
    for prev, want in (({"activity": [], "kill_switch_shadow": OLD_KS}, OLD_KS),
                       ({"activity": []}, None)):
        repo = seed_repo(prev)
        old = (publish_web.STATE, publish_web.DATA, publish_web.REPO, ib_web.snapshot,
               ib_web.ledger, subprocess.run, publish_web.sys.argv,
               publish_web.gitpush.push_with_retry)
        try:
            publish_web.STATE = repo / "execution" / "state.json"
            publish_web.DATA, publish_web.REPO = repo / "data", repo
            ib_web.snapshot = lambda: {"positions": [], "netliq": 100000.0,
                                       "cash": {BASE: 10.0}}
            ib_web.ledger = lambda *a, **k: {"BASE": {"netliquidationvalue": 100000.0,
                                                      "exchangerate": 1}}
            subprocess.run = lambda *a, **k: Done()
            publish_web.gitpush.push_with_retry = lambda *a, **k: True
            publish_web.sys.argv = ["publish_web.py"]
            assert publish_web.main() == 0
        finally:
            (publish_web.STATE, publish_web.DATA, publish_web.REPO, ib_web.snapshot,
             ib_web.ledger, subprocess.run, publish_web.sys.argv,
             publish_web.gitpush.push_with_retry) = old
        snap = json.loads((repo / "data" / "bot_state.json").read_text(encoding="utf-8"))
        assert snap.get("kill_switch_shadow") == want, snap.get("kill_switch_shadow")
    # the config line every live run publishes
    assert ib_bot.effective_config()["kill_switch_shadow"] == "8% / 20 weekdays, log only"
    assert ib_bot.effective_config()["kill_switch"] == "off"
    print("t11 publish_state writes the run's block or carries the last one; publish_web "
          "carries it untouched; config says '8% / 20 weekdays, log only' beside kill_switch "
          "off OK")


if __name__ == "__main__":
    t1_the_replay_blocks_exactly_the_runs_the_study_simulator_blocks()
    t2_with_flows_and_missing_rows_it_is_the_studys_real_account_reading()
    t3_on_the_repos_own_history()
    t4_which_run_an_order_belongs_to_and_the_timer()
    t5_what_a_run_sees_at_its_hour()
    t6_shadow_never_changes_an_order()
    t7_a_morning_run_inside_a_block_and_the_last_day()
    t8_an_unusable_history_is_unknown_and_changes_nothing()
    t9_off_logs_and_tags_nothing_and_an_unbuilt_mode_runs_as_shadow()
    t10_dry_logs_but_records_nothing()
    t11_publish_state_writes_or_carries_the_block_and_publish_web_carries_it()
    print("ALL KILL SWITCH SHADOW TESTS PASS")
