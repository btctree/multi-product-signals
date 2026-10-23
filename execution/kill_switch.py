#!/usr/bin/env python3
"""The kill-switch's peak, net of money moved in and out - one rule, shared.

The kill-switch blocks NEW entries (never exits) while NetLiq is more than 8%
below its peak. ib_bot.py enforces it; daily_signal.py (the Telegram digest,
python 3.9, which must never import ib_bot) reports it. They must agree, or the
digest says "kill switch active" on a day the bot is buying - so the rule lives
here, stdlib only, the way market_clock.py is shared.

WHY THE PEAK IS NOT SIMPLY THE HIGHEST NETLIQ. Until 2026-10-23 it was, stored in
state.json, and nothing lowered it for a withdrawal. The owner's GBP 2,250
deposit (23,746 HKD, 2026-07-31) was in the account when that peak was taken, and
32,000 HKD was withdrawn on 2026-08-02 - so the stored peak (240,603) stood above
anything the account could reach by trading. Entries were blocked 28 Sep-1 Oct
and from 15 Oct while the real fall from the real high (230,661 on 13 Oct) was
never worse than 5.4%. The backtest the strategy was validated on has no such
gate at all, so every false trigger is a pure departure from it.

THE RULE. Work in TRADING WEALTH: NetLiq minus the net money paid in so far,
from the hand-kept "flows" list in data/netliq_history.json (the same list the
dashboard uses). The peak is the best trading wealth in the published day-end
history (and today's own), expressed in today's money (+ net money in now). A
recorded deposit or withdrawal moves NetLiq and that net sum together, so it is
neither a new high nor a fall. Earmarked cash needs nothing: NetLiq is already
net of it.

RECOMPUTED ON EVERY RUN, NOTHING REMEMBERED. A first version kept the wealth
peak in state.json and only ever raised it. Board review 2026-10-23 showed why
that is wrong here: flows are typed in by hand after the money moves, so any run
that saw a deposit before its flows entry counted would have frozen that deposit
in as a new high - permanently. Recomputing from the history means a late,
mis-dated or corrected flows entry heals itself on the next run. The one manual
override - to re-risk after a GENUINE fall - is an explicit date,
state["_kill_peak_since"] = "YYYY-MM-DD": the peak is then taken only from that
day on. Flows must be dated the day the money actually moved.
"""
import json
import math
import os
import re
from pathlib import Path

DAILY_LOSS_KILL = float(os.environ.get("DAILY_LOSS_KILL", "0.08"))
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def history_path():
    """Overridable (MPS_NETLIQ_HISTORY) so a test never reads the live file;
    worked out per call, never held at module level."""
    default = Path(__file__).resolve().parent.parent / "data" / "netliq_history.json"
    return Path(os.environ.get("MPS_NETLIQ_HISTORY", str(default)))


def _today():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _num(v):
    """A finite float, or None. JSON's NaN and Infinity are refused: a NaN peak
    makes every comparison False and would leave the gate open for good."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def read_history(path=None):
    """(flows [(day, amount)], series [(day, nl)], warnings), or (None, None,
    [why]) when the file cannot be used at all. A single bad row is skipped
    with a warning rather than discarding the whole history."""
    try:
        h = json.loads(Path(path or history_path()).read_text(encoding="utf-8-sig"))
    except Exception as e:
        return None, None, ["netliq_history.json unreadable (%s)" % str(e)[:80]]
    if not isinstance(h, dict):
        return None, None, ["netliq_history.json is not an object"]
    raw_f, raw_s = h.get("flows") or [], h.get("series") or []
    if not isinstance(raw_f, list) or not isinstance(raw_s, list):
        return None, None, ["netliq_history.json flows/series are not lists"]
    flows, series, warn = [], [], []
    for f in raw_f:
        d = f.get("d") if isinstance(f, dict) else None
        a = _num(f.get("amt")) if isinstance(f, dict) else None
        if not (isinstance(d, str) and _DAY.match(d)) or a is None:
            warn.append("flows row ignored, needs a YYYY-MM-DD 'd' and a number 'amt': %s"
                        % str(f)[:80])
            continue
        flows.append((d, a))
    for r in raw_s:
        d = r.get("d") if isinstance(r, dict) else None
        v = _num(r.get("nl")) if isinstance(r, dict) else None
        if isinstance(d, str) and _DAY.match(d) and v is not None and v > 0:
            series.append((d, v))
    return flows, series, warn


def peak_today(flows, series, nl, today, since=None):
    """(peak in today's money, net money in up to today). The best trading
    wealth over the history up to today (from `since` on, when given) and
    today's own, plus the net money in now."""
    net_in = sum(a for d, a in flows if d <= today)
    best = nl - net_in
    for d, v in series:
        if d > today or (since and d < since):
            continue
        best = max(best, v - sum(a for fd, a in flows if fd <= d))
    return best + net_in, net_in


def _since(state, warn):
    s = state.get("_kill_peak_since")
    if s is None:
        return None
    if isinstance(s, str) and _DAY.match(s):
        return s
    warn.append("_kill_peak_since ignored, needs YYYY-MM-DD: %r" % (s,))
    return None


def check(state, nl, path=None, today=None, kill=None):
    """For the bot: (killed, peak in today's money, basis, warnings).

    Writes only state["_peak_netliq"] - a REPORT of the peak used, never read
    back by this rule. When the history cannot be used it keeps the old rule
    (the stored peak, ratcheted) and says so in the basis. Never raises."""
    kill = DAILY_LOSS_KILL if kill is None else kill
    today = today or _today()
    warn = []
    try:
        flows, series, warn = read_history(path)
        if flows is None:
            raise ValueError(warn[0] if warn else "no history")
        peak, _ = peak_today(flows, series, float(nl), today, _since(state, warn))
        if not math.isfinite(peak):
            raise ValueError("peak not finite")
        state["_peak_netliq"] = peak
        return nl < peak * (1 - kill), peak, "net of deposits/withdrawals", warn
    except Exception as e:
        old = _num(state.get("_peak_netliq"))
        peak = max(old, float(nl)) if old is not None else float(nl)
        state["_peak_netliq"] = peak
        return (nl < peak * (1 - kill), peak, "raw NetLiq peak - history unusable",
                warn + ["%s" % str(e)[:120]])


def peak_for_report(state, nl, path=None, today=None):
    """For the digest, which writes nothing: (peak in today's money, warnings),
    by exactly the rule check() gates on."""
    today = today or _today()
    try:
        flows, series, warn = read_history(path)
        if flows is None:
            raise ValueError(warn[0] if warn else "no history")
        peak, _ = peak_today(flows, series, float(nl), today, _since(state, warn))
        if not math.isfinite(peak):
            raise ValueError("peak not finite")
        return peak, warn
    except Exception as e:
        old = _num(state.get("_peak_netliq"))
        return (max(old, float(nl)) if old is not None else float(nl),
                ["kill-switch history unusable (%s) - using the stored peak" % str(e)[:80]])
