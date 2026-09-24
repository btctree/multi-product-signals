#!/usr/bin/env python3
"""Recover the cut-loss path from the repo's own hourly commits (one-off).

data/bot_state.json has been committed by the VM roughly hourly since
2026-07-20, and every commit carries each holding's cut-loss as it stood then.
Walking those commits oldest-first and folding them through
stop_history.record() gives the same file the publishers now keep up to date -
one row per symbol per day its stop changed - without inventing a single value.

    python execution/backfill_stop_history.py            # write data/stop_history.json
    python execution/backfill_stop_history.py --dry      # print what it would write

It re-reads whatever is already in the file and merges, so running it twice is
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


def git(*args):
    r = subprocess.run(["git", "-C", REPO] + list(args),
                       capture_output=True, text=True)
    return r.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="write nothing")
    ap.add_argument("--ref", default="origin/main", help="branch to read history from")
    a = ap.parse_args()

    revs = [ln.split() for ln in
            git("log", a.ref, "--format=%h %cI", "--", STATE).splitlines() if ln.strip()]
    revs.reverse()                                       # oldest first
    print("%d commits of %s on %s" % (len(revs), STATE, a.ref))
    doc = stop_history.load(OUT)
    seen = changes = 0
    for h, when in revs:
        raw = git("show", "%s:%s" % (h, STATE))
        if not raw:
            continue
        try:
            state = json.loads(raw.lstrip("﻿"))
        except ValueError:
            continue                                     # a torn old commit: skip
        seen += 1
        changes += stop_history.record(doc, state.get("positions") or [],
                                       day=when[:10])
    syms = {s: len(v) for s, v in doc.get("stops", {}).items() if v}
    print("read %d versions | %d recorded changes | %d symbols"
          % (seen, changes, len(syms)))
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
