#!/usr/bin/env python3
"""One reading of the forward scoreboard. From the repo root:  python engine/forward/run_scoreboard.py

Run it about twice a year. It takes a few minutes and needs Yahoo and, once there are sessions, SEC. It:
  1. checks nothing frozen has moved (frozen.py), and that earlier readings are untouched;
  2. fetches every frozen name from the warm-up date, merges with the store in data/forward_scoreboard/store/;
  3. re-simulates every arm from the frozen first session to the last finished close, with E1 by filing date;
  4. appends one reading to data/forward_scoreboard/readings.json, saves the new bars, and prints the table.
Then commit data/forward_scoreboard/. A second run on the same close writes nothing.

If a download fails badly it stops and writes nothing. If SEC cannot be reached the reading is still written and says, loudly, that
E1 did not run; read again later. SEC_USER_AGENT in the environment, if set, names the client to SEC; otherwise a generic name.

--pipeline-test YYYY-MM-DD runs the same code from a past start and writes nothing, to prove the pipeline works on real prices. Its
figures are on the history the rules were tuned on and are not a reading.

This reads prices, SEC facts, its own folder and data/netliq_history.json. Nothing under execution/ reads any of it.
"""
import argparse
import datetime as dt
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from forward import frozen as FZ          # noqa: E402
from forward import scoreboard as SB      # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="One reading of the forward scoreboard.")
    ap.add_argument("--pipeline-test", metavar="YYYY-MM-DD", help="run from this past start and write nothing")
    a = ap.parse_args(argv)
    manifest = FZ.verify()
    fz = FZ.load()
    data = FZ.ROOT / "data"
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=FZ.ROOT, capture_output=True, text=True, timeout=30).stdout.strip() or None
    except Exception:
        commit = None
    try:
        out = SB.run(fz, dt.datetime.now(dt.timezone.utc), data / "forward_scoreboard" / "readings.json", data / "forward_scoreboard" / "store",
                     data / "netliq_history.json", start=a.pipeline_test, write=not a.pipeline_test, commit=commit, manifest=manifest)
    except SB.ReadingStopped as e:
        print("READING STOPPED: %s" % e)
        return 1
    print(out["text"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
