#!/usr/bin/env python3
"""Recover the cut-loss path from the repo's own hourly commits (one-off).

data/bot_state.json has been committed by the VM roughly hourly since
2026-07-20, and every commit carries each holding's cut-loss as it stood then.
Walking those commits oldest-first and folding them through
stop_history.record() gives the same file the publishers now keep up to date -
one row per symbol per day its stop changed - without inventing a single value.

    python execution/backfill_stop_history.py            # write data/stop_history.json
    python execution/backfill_stop_history.py --dry      # print what it would write

It REBUILDS the file from those commits each time (carrying over any row the
live publishers added since the newest commit it read), so running it twice is
harmless. It only ever reads git history: no network, no /root, no IB.
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import stop_history                                     # noqa: E402

STATE = "data/bot_state.json"
OUT = os.path.join(REPO, "data", "stop_history.json")


def git(*args, **kw):
    """git output, or None when git failed - a silent empty string would make
    an aborted walk look like a clean one. TZ=UTC so a commit's day is the day
    the VM published it, whatever timezone this desktop is in."""
    env = dict(os.environ, TZ="UTC")
    r = subprocess.run(["git", "-C", REPO] + list(args),
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        if not kw.get("quiet"):
            sys.stderr.write("git %s failed: %s\n"
                             % (" ".join(args[:2]), (r.stderr or "").strip()[:160]))
        return None
    return r.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="write nothing")
    ap.add_argument("--ref", default="origin/main", help="branch to read history from")
    a = ap.parse_args()

    log = git("log", a.ref, "--date=format-local:%Y-%m-%d", "--format=%h %cd",
              "--", STATE)
    if log is None:
        print("could not read the history of %s on %s - nothing written" % (STATE, a.ref))
        return 2
    revs = [ln.split() for ln in log.splitlines() if ln.strip()]
    revs.reverse()                                       # oldest first
    print("%d commits of %s on %s" % (len(revs), STATE, a.ref))
    old, status = stop_history.read(OUT)
    if status == "damaged":
        print("%s is present but unreadable - repair or delete it first" % OUT)
        return 2
    # REBUILT, not appended to. Folding the whole history into an existing file
    # would compare each old commit against the NEWEST value already stored,
    # read every one as a change, and append it with a past date - the rows come
    # back doubled and out of order. The commits are the whole record, so the
    # rebuild is complete by construction; rows the publishers added after the
    # last commit read here are carried over below.
    doc = {"updated": None, "stops": {}}
    newest_day = revs[-1][1] if revs else ""
    seen = changes = skipped = 0
    for h, day in revs:
        raw = git("show", "%s:%s" % (h, STATE), quiet=True)
        if not raw:
            skipped += 1
            continue
        try:
            state = json.loads(raw.lstrip("﻿"))
        except ValueError:
            skipped += 1                                 # a torn old commit
            continue
        seen += 1
        changes += stop_history.record(doc, state.get("positions") or [], day=day)
    # Anything the live publishers recorded after the newest commit walked here
    # (an hour's worth, at most) is kept rather than dropped.
    carried = 0
    for sym, rows in (old.get("stops") or {}).items():
        for d, v in rows:
            if str(d) > newest_day:
                carried += stop_history.record(doc, [{"symbol": sym, "stop": v}], day=str(d))
    if carried:
        print("carried %d row(s) newer than the last commit read" % carried)
    syms = {s: len(v) for s, v in doc.get("stops", {}).items() if v}
    print("read %d versions (%d unreadable, skipped) | %d recorded changes | %d symbols"
          % (seen, skipped, changes, len(syms)))
    if seen == 0:
        print("no version could be read - nothing written")
        return 2
    for s in sorted(syms, key=lambda k: -syms[k])[:10]:
        rows = doc["stops"][s]
        print("  %-9s %3d rows  %s %.4f -> %s %.4f"
              % (s, syms[s], rows[0][0], rows[0][1], rows[-1][0], rows[-1][1]))
    if a.dry:
        print("--dry: nothing written")
        return 0
    stop_history.save(OUT, doc)
    print("wrote", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
