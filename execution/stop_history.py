#!/usr/bin/env python3
"""Where each holding's cut-loss has been, day by day.

The bot's trailing stop only ever ratchets UP (ib_bot keeps a high-water mark
and never lowers the stop), but only its CURRENT value is published, so the
dashboard could draw a flat line and nothing else: the owner could not see when
the cut-loss was raised, or how far it has travelled from where it started.
Every step is already on record - the VM commits data/bot_state.json hourly -
so this file simply keeps it in one place the page can read
(backfill_stop_history.py recovered the path back to 2026-07-20 from those
commits; the publishers keep it going from here).

Shape, deliberately small - one row per symbol per day it CHANGED:

    {"updated": "2026-09-24 14:25 UTC",
     "stops": {"FFIV": [["2026-07-24", 352.9], ["2026-07-28", 361.2], ...]}}

Rules, the same for the backfill and the live publishers:
  * a value equal to the last recorded one is not a change and is not stored;
  * two changes on one day collapse to that day's LAST value, which is what a
    daily chart of closes can show;
  * a symbol keeps its rows after it is sold - a re-bought lot is charted from
    its own entry date, and the old rows cost a few bytes.
Never raises: a publish must not fail over a chart.
"""
import io
import json
import os
from datetime import datetime, timezone

MAX_ROWS = 800                       # per symbol; ~3 years of daily ratchets


def load(path):
    try:
        with io.open(path, encoding="utf-8-sig") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("stops"), dict):
            return d
    except Exception:
        pass
    return {"updated": None, "stops": {}}


def record(doc, positions, day=None):
    """Fold today's published positions into `doc`. Returns the number of rows
    added or amended. `positions` is bot_state.json's own list of rows."""
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stops, n = doc.setdefault("stops", {}), 0
    for p in positions or []:
        try:
            sym = str(p.get("symbol") or "").strip()
            stop = p.get("stop")
            if not sym or stop is None:
                continue
            stop = float(stop)
        except (TypeError, ValueError):
            continue
        rows = stops.setdefault(sym, [])
        if rows and str(rows[-1][0]) == day:
            if abs(float(rows[-1][1]) - stop) > 1e-9:
                rows[-1] = [day, stop]           # same day: the last value wins
                n += 1
            continue
        if rows and abs(float(rows[-1][1]) - stop) <= 1e-9:
            continue                              # unchanged: nothing to record
        rows.append([day, stop])
        n += 1
        if len(rows) > MAX_ROWS:
            del rows[:len(rows) - MAX_ROWS]
    return n


def save(path, doc):
    """tmp + os.replace, so a killed publish cannot leave a torn file that the
    dashboard (and the next publish) would fail to read."""
    doc["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tmp = str(path) + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, str(path))


def update(path, positions, log=None, day=None):
    """load + record + save, best effort. True when the file was written.

    A missing file is created even with nothing to record: both publishers name
    it in `git add`, and git fails the whole add on a pathspec that matches no
    file (the same trap dividends_ledger.jsonl is touched for)."""
    try:
        doc = load(path)
        n = record(doc, positions, day=day)
        if not n and os.path.exists(str(path)):
            return False
        save(path, doc)
        if log:
            log("  cut-loss history: %d change(s) recorded" % n)
        return True
    except Exception as e:                         # never cost a publish
        if log:
            log("  !! cut-loss history NOT updated (%s)" % e)
        return False
