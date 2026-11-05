"""What was frozen at commit aace4aa7 on 2026-11-05, and the check that it has not moved.

The list, the settings and the rule code never change after the freeze: the whole point is that no hindsight can reach this
test. MANIFEST.json holds a hash of each frozen file and MANIFEST_SHA256 below holds the hash of the manifest, so an edit has
to change all three, on purpose, in plain sight. Hashes read a file with CRLF as LF, so a Windows worktree and a clean export
agree (git core.autocrlf=true here).

Not frozen, on purpose: the fetching, the store, the table and the runner (data_store.py, scoreboard.py, run_scoreboard.py).
They cannot change a rule; if one is fixed later, earlier readings stay as they were.
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FROZEN_DIR = "data/forward_scoreboard/frozen"
MANIFEST_SHA256 = "9e3f5558a2e6415adb7f0be02b3ec97575ecce03cb5c21511024b0ea6d6bc193"
FROZEN_FILES = (FROZEN_DIR + "/list.json", FROZEN_DIR + "/settings.json",
                "engine/forward/live_sim.py", "engine/forward/fp_sim.py", "engine/forward/rules.py", "engine/forward/twins.py",
                "engine/forward/indicators_frozen.py", "engine/forward/model_d3e1_frozen.py")


class FrozenEdited(Exception):
    pass


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def verify(root=ROOT, expected=MANIFEST_SHA256):
    """Raise FrozenEdited unless the manifest and every frozen file are as they were on 2026-11-05."""
    root = Path(root)
    man = root / FROZEN_DIR / "MANIFEST.json"
    if not man.exists():
        raise FrozenEdited("the frozen manifest is missing")
    got = file_hash(man)
    if got != expected:
        raise FrozenEdited("the frozen manifest was edited: %s, expected %s" % (got, expected))
    files = json.loads(man.read_text(encoding="utf-8"))["files"]
    moved = [f for f in FROZEN_FILES if not (root / f).exists() or file_hash(root / f) != files.get(f)]
    if moved:
        raise FrozenEdited("frozen files edited: " + ", ".join(moved))
    return got


class Frozen:
    """The frozen list and settings as the pipeline reads them. Tests build their own, small, with the real rule code."""

    def __init__(self, settings, tickers, market_of, hk_lots, cik):
        self.settings, self.tickers, self.market_of, self.hk_lots, self.cik = settings, tickers, market_of, hk_lots, cik
        self.order = {t: i for i, t in enumerate(tickers)}
        self.sim_tickers = [t for t in tickers if market_of[t] in ("US", "HK", "JP", "EU", "CRYPTO")]


def _read(name, root=ROOT):
    return json.loads((Path(root) / FROZEN_DIR / name).read_text(encoding="utf-8"))


def load(root=ROOT):
    x = _read("list.json", root)
    return Frozen(_read("settings.json", root), x["tickers"], x["market_of"], x["hk_lots"], x["cik"])


FROZEN_PARAMS = _read("settings.json")["rules"]
