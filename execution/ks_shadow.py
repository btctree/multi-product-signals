#!/usr/bin/env python3
"""The kill switch with a 20-weekday timer, in SHADOW mode: one rule, shared.

LOG ONLY. Nothing here can place, size, skip, delay or reorder an order. The
owner approved it on 2026-11-05 as "agree to run in shadow, also add a line on
performance compare chart to show this". ib_bot.py asks it once per run and
only annotates the BUYs it sends anyway; engine/build_sim_lines.py loads this
file by path (stdlib only, nothing under execution/ joins the build's import
path) to draw the display-only line on the Growth card. The old kill switch
(KILL_SWITCH, kill_switch.check) is untouched and stays off.

THE RULE, as the 5 Nov 2026 kill-switch study tested it (arm X8N20; its
INTERP.md fixed the details, copied here):
  * A CLOSE is a weekday's day-end NetLiq: its row in data/netliq_history.json
    (the publisher's 23:25 UTC reading or the bot's 23:35 one, whichever the
    day kept). The 23:35 UTC trading run's own NetLiq stands in for today's
    close at that run. Weekend rows are hourly marks and are never read.
  * TRADING WEALTH = NetLiq minus the net money paid in up to that day, from
    the hand-kept "flows" list - the reading of execution/kill_switch.py
    (read_history, peak_today), so a recorded deposit or withdrawal is never
    a high or a fall. The peak is the best trading wealth since the first row
    or the last reset, today's included; the line is (1 - 8%) of that peak in
    today's money (peak + net money in to today).
  * TRIP: a close at or below the line, with no block running. The block
    starts that day: weekday 1. That day's 23:35 run places no buys.
  * COUNT: every weekday counts, holidays included, and so does a weekday
    with no row. The timer runs on whatever NetLiq does - recovering does not
    end it, falling further does not restart it.
  * THE LAST DAY is weekday 20. Its morning run is still blocked; at its 23:35
    run the peak resets to that day's trading wealth, and that run may buy. A
    last day with no row resets at the next row. Then the rule can trip again
    from the new peak.
  * EXITS are never blocked.

WHICH RUN AN ORDER BELONGS TO. The study's simulator runs twice a weekday:
09:00, which only obeys a block already running, and 23:35, which reads the
close and can trip it. Live, the 23:35 UTC run is the evening run (every bot
order since 20 Jul at 23:00 UTC or later was placed by it); the 10:00 UTC
catch-up, its 12:00 retry and anything else before 23:00 obey the last
weekday's close before their day. So an order placed at time t is governed
by governing_close(t): t's own day from EVENING_FROM_HOUR UTC on a weekday,
else the last weekday before t's day - which also makes a Sunday 23:35 run
obey Friday's close.

NOTHING REMEMBERED. Every call replays the rule from the first history row
(2026-07-20). Nothing is read from or written to state.json, so nothing can
go stale, and a corrected or late flows entry heals at the next run, as in
kill_switch.py. One consequence, by design: a run decides on its own NetLiq,
and every later replay on the close the history recorded for that day, which
can be the publisher's reading ten minutes earlier. Only a close within a few
hundred HKD of the line can be judged both ways.

An unreadable or missing history, or settings that are not usable, is
"unknown": bot_view() says why and never raises, and an unknown view blocks
nothing and tags nothing.
"""
import datetime as dt
import importlib.util
import math
from pathlib import Path

TRIP = 0.08               # ib_bot.KS_SHADOW_TRIP
DAYS = 20                 # ib_bot.KS_SHADOW_DAYS
# From this UTC hour on a weekday a run, or an order, is the evening (23:35)
# run's, and that day's NetLiq is its close.
EVENING_FROM_HOUR = 23

_KS = {}


def _kill_switch():
    """execution/kill_switch.py, loaded by path once (it is stdlib only and
    reads its paths per call): the history is parsed exactly as the old
    switch parses it, whoever loaded this file."""
    if "m" not in _KS:
        path = Path(__file__).resolve().parent / "kill_switch.py"
        spec = importlib.util.spec_from_file_location("_mps_ks_kill_switch", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _KS["m"] = mod
    return _KS["m"]


def read_history(path=None):
    """kill_switch.read_history: (flows [(day, amount)], series [(day, nl)],
    warnings), or (None, None, [why])."""
    return _kill_switch().read_history(path)


def _date(day):
    return dt.date.fromisoformat(str(day)[:10])


def is_weekday(day):
    return _date(day).weekday() < 5


def add_weekdays(day, k):
    """The weekday k weekdays after `day` (a weekday), k >= 0."""
    d = _date(day)
    while k > 0:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            k -= 1
    return d.isoformat()


def prev_weekday(day):
    """The last weekday strictly before `day`."""
    d = _date(day) - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d.isoformat()


def weekdays(first, last):
    """Every weekday from `first` to `last`, both included, as YYYY-MM-DD."""
    d, end = _date(first), _date(last)
    out = []
    while d <= end:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def net_in(flows, day):
    """The net money paid in up to and including `day`."""
    return sum(a for d, a in flows if d <= day)


def settings_ok(x, n):
    """A trip strictly between 0 and 1 and a whole number of days, at least 2
    (the trip day is weekday 1, and the study's shortest arm was 10)."""
    try:
        return (isinstance(n, int) and not isinstance(n, bool) and n >= 2
                and math.isfinite(float(x)) and 0.0 < float(x) < 1.0)
    except Exception:
        return False


def replay(flows, closes, x, n, through):
    """The rule walked over every weekday from the first close to `through`.

    closes: {YYYY-MM-DD: NetLiq} - weekday closes only (see closes_at). The
    study's simulator (ksim.KSim._timer) and its real-account reading
    (a2_real.timer) step exactly so, with the comparison written as the
    simulator writes it: NetLiq <= (1 - x) x peak, the peak in that day's
    money. Returns
      first, through, on (a block running after `through`), count (its
      weekday, 1..n), peak_wealth and peak_day (the current peak of trading
      wealth and the day it was set or reset), skipped (weekdays with no close
      outside a block), and blocks: one dict per trip, oldest first -
        start   the trip day, weekday 1
        end     the last day, weekday n: the day of the reset, or when it is
                still to come the day planned for it (moved on a day when
                the last day has no close)
        reset   True once the peak has been reset
        nl, peak, line, fall, peak_day   the close, the peak and the line in
                that day's money, the fall (0.08 = 8%) and the peak's day
        reset_nl   the close the peak was reset on (None until then)
        waited  last days that had no close, so the reset waited."""
    if not closes:
        raise ValueError("no day-end close to replay")
    if not settings_ok(x, n):
        raise ValueError("kill-switch shadow settings not usable: trip %r, days %r" % (x, n))
    first = min(closes)
    flows = sorted(flows)
    peak, peak_day = -math.inf, None
    on, cnt, cur = False, 0, None
    blocks, skipped = [], []
    for d in weekdays(first, through):
        nl = closes.get(d)
        if on:
            cnt += 1
            if cnt >= n:
                if nl is None:
                    # the last day has no close: the reset waits for the next one
                    cnt = n - 1
                    cur["waited"].append(d)
                    cur["end"] = add_weekdays(d, 1)
                    continue
                peak, peak_day, on = nl - net_in(flows, d), d, False
                cur.update(end=d, reset=True, reset_nl=nl)
                cur = None
            continue
        if nl is None:
            skipped.append(d)
            continue
        w = nl - net_in(flows, d)
        if w > peak:
            peak, peak_day = w, d
        p_now = peak + net_in(flows, d)
        if nl <= (1.0 - x) * p_now:
            on, cnt = True, 1
            cur = {"start": d, "end": add_weekdays(d, n - 1), "reset": False,
                   "nl": nl, "peak": p_now, "line": (1.0 - x) * p_now,
                   "fall": 1.0 - nl / p_now, "peak_day": peak_day, "reset_nl": None,
                   "waited": []}
            blocks.append(cur)
    if peak_day is None:
        raise ValueError("no day-end close on or before %s" % through)
    return {"first": first, "through": through, "on": on, "count": cnt if on else 0,
            "peak_wealth": peak, "peak_day": peak_day, "blocks": blocks,
            "skipped": skipped}


def closes_at(series, now, today_nl=None):
    """(closes, through, evening) for a run - or the sim-lines build - at
    `now` (aware UTC). closes: every weekday row dated before today (the last
    row of a day listed twice). At the evening (a weekday, from
    EVENING_FROM_HOUR UTC) today's close is `today_nl` - the run's own NetLiq
    - or, when that is None, today's history row if there is one (the build).
    through: today once today's close is in, else the last weekday before."""
    today = now.astimezone(dt.timezone.utc).date().isoformat()
    evening = is_weekday(today) and now.astimezone(dt.timezone.utc).hour >= EVENING_FROM_HOUR
    rows = {}
    for d, v in series:
        rows[d] = v
    closes = {d: v for d, v in rows.items() if d < today and is_weekday(d)}
    if evening:
        v = today_nl if today_nl is not None else rows.get(today)
        if v is not None:
            closes[today] = float(v)
    through = today if today in closes else prev_weekday(today)
    return closes, through, evening


def governing_close(t):
    """The weekday whose close an order placed at `t` (aware UTC) obeys: its
    own day from EVENING_FROM_HOUR on a weekday (the 23:35 run), else the last
    weekday before its day."""
    t = t.astimezone(dt.timezone.utc)
    day = t.date().isoformat()
    if is_weekday(day) and t.hour >= EVENING_FROM_HOUR:
        return day
    return prev_weekday(day)


def block_for(blocks, t):
    """The block that would have stopped a buy ordered at `t`, or None: the
    block whose start <= governing_close(t) < its end. The last day's evening
    run buys again, its morning run does not."""
    g = governing_close(t)
    for b in blocks:
        if b["start"] <= g < b["end"]:
            return b
    return None


def public_block(b):
    return {"start": b["start"], "end": b["end"], "reset": bool(b["reset"]),
            "fall_pct": round(b["fall"] * 100, 2), "netliq": round(b["nl"]),
            "peak": round(b["peak"]), "line": round(b["line"]),
            "peak_date": b["peak_day"]}


def view(flows, series, nl, now, x=TRIP, n=DAYS):
    """What the shadow says at a run at `now` with this run's NetLiq `nl`.
    Pure; raises on anything unusable (bot_view turns that into unknown)."""
    nl = float(nl)
    if not (math.isfinite(nl) and nl > 0):
        raise ValueError("this run's NetLiq is not usable (%r)" % (nl,))
    closes, through, evening = closes_at(series, now, today_nl=nl)
    r = replay(flows, closes, x, n, through)
    today = now.astimezone(dt.timezone.utc).date().isoformat()
    net_now = net_in(sorted(flows), today)
    peak_now = r["peak_wealth"] + net_now
    if not (math.isfinite(peak_now) and peak_now > 0):
        raise ValueError("the peak is not usable (%r)" % (peak_now,))
    cur = r["blocks"][-1] if r["on"] else None
    return {
        "status": "ok",
        # this run's NetLiq is today's close (the 23:35 run) or only a mark
        "evening": evening,
        "close_through": through, "first_day": r["first"],
        "netliq": round(nl), "peak": round(peak_now),
        "peak_wealth": round(r["peak_wealth"]), "peak_date": r["peak_day"],
        "line": round((1.0 - x) * peak_now),
        "fall_pct": round((1.0 - nl / peak_now) * 100, 2),
        "would_block": bool(r["on"]),
        "block_start": cur["start"] if cur else None,
        "block_end": cur["end"] if cur else None,
        "block_day": (len(weekdays(cur["start"], today)) if cur else None),
        "trips": [public_block(b) for b in r["blocks"]],
    }


def bot_view(nl, now, x=TRIP, n=DAYS, path=None):
    """view() on the history file (kill_switch.history_path, overridable by
    MPS_NETLIQ_HISTORY). Never raises: anything unusable is
    {"status": "unknown", "why": ..., "would_block": None}."""
    try:
        flows, series, warn = read_history(path)
        if flows is None:
            raise ValueError(warn[0] if warn else "netliq_history.json not usable")
        return view(flows, series, nl, now, x, n)
    except Exception as e:
        return {"status": "unknown", "why": str(e)[:160], "would_block": None}
