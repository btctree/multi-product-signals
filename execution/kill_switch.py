#!/usr/bin/env python3
"""The kill-switch's peak, net of money moved in and out - one rule, shared.

The kill-switch blocks NEW entries (never exits) while NetLiq is more than 8%
below its peak. ib_bot.py enforces it; daily_signal.py (the Telegram digest,
python 3.9, which must never import ib_bot) reports it. They must agree, or the
digest says "kill switch active" on a day the bot is buying - so the rule lives
here, stdlib only, the way market_clock.py is shared.

WHY THE PEAK IS NOT SIMPLY THE HIGHEST NETLIQ. Until 2026-10-23 it was, and
nothing lowered it for a withdrawal. The owner's GBP 2,250 deposit (23,746 HKD,
2026-07-31) was in the account when the peak was taken, and 32,000 HKD was
withdrawn on 2026-08-02 - so the stored peak (240,603) stood above anything the
account could reach by trading. Entries were blocked 28 Sep-1 Oct and from
15 Oct while the real fall from the real high (230,661 on 13 Oct) was never
worse than 5.4%. The backtest the strategy was validated on has no such gate at
all, so every false trigger is a pure departure from it.

THE RULE. Track the peak in TRADING WEALTH: NetLiq minus the net money paid in
so far, from the hand-kept "flows" list in data/netliq_history.json (the same
list the dashboard uses). A recorded deposit or withdrawal moves NetLiq and that
net sum together, so it is neither a new high nor a fall. The gate compares
today's NetLiq with that peak expressed in today's money (peak wealth + net
money in). Earmarked cash needs nothing: NetLiq is already net of it. A
withdrawal that is NOT entered in flows still reads as a fall - which is why
flows must be kept.
"""
import json
import os
from pathlib import Path

DAILY_LOSS_KILL = float(os.environ.get("DAILY_LOSS_KILL", "0.08"))


def history_path():
    """Overridable (MPS_NETLIQ_HISTORY) so a test never reads the live file;
    worked out per call, never held at module level."""
    default = Path(__file__).resolve().parent.parent / "data" / "netliq_history.json"
    return Path(os.environ.get("MPS_NETLIQ_HISTORY", str(default)))


def _today():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def flows_and_history_peak(path=None, today=None):
    """(net money paid in up to today, highest trading wealth in the published
    history), or (None, None) when the file is missing or unreadable."""
    try:
        h = json.loads(Path(path or history_path()).read_text(encoding="utf-8-sig"))
        flows = [(str(f["d"])[:10], float(f["amt"])) for f in (h.get("flows") or [])]
    except Exception:
        return None, None
    today = today or _today()
    net_in = sum(a for d, a in flows if d <= today)
    best = None
    for r in h.get("series") or []:
        try:
            d, v = str(r["d"])[:10], float(r["nl"])
        except (KeyError, TypeError, ValueError):
            continue
        if v <= 0 or d > today:
            continue
        w = v - sum(a for fd, a in flows if fd <= d)
        best = w if best is None else max(best, w)
    return net_in, best


def _wealth_peak(state, w, hist_best):
    """The stored wealth peak, or - the first time - the history's, never the
    old _peak_netliq, which is exactly the stale number this replaces."""
    pw = state.get("_peak_wealth")
    if not isinstance(pw, (int, float)) or isinstance(pw, bool):
        pw = hist_best if hist_best is not None else w
    return max(float(pw), w)


def check(state, nl, path=None, today=None, kill=None):
    """For the bot: (killed, peak in today's money, basis) and the state updated
    - "_peak_wealth" is the gate's input, "_peak_netliq" the same peak in today's
    money for the log, the HALT row and older readers. When the history cannot
    be read it keeps the old rule and says so in the basis."""
    kill = DAILY_LOSS_KILL if kill is None else kill
    net_in, hist_best = flows_and_history_peak(path, today)
    if net_in is None:
        peak = max(state.get("_peak_netliq", nl), nl)
        state["_peak_netliq"] = peak
        return nl < peak * (1 - kill), peak, "raw NetLiq peak - flows unreadable"
    pw = _wealth_peak(state, nl - net_in, hist_best)
    state["_peak_wealth"] = pw
    peak = pw + net_in
    state["_peak_netliq"] = peak
    return nl < peak * (1 - kill), peak, "net of deposits/withdrawals"


def peak_for_report(state, nl, path=None, today=None):
    """For the digest, which writes nothing: the peak in today's money by the
    same rule, or the stored _peak_netliq when the history cannot be read."""
    net_in, hist_best = flows_and_history_peak(path, today)
    if net_in is None:
        return state.get("_peak_netliq") or nl
    return _wealth_peak(state, nl - net_in, hist_best) + net_in
