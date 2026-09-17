#!/usr/bin/env python3
"""Test isolation: every path the bot defaults to /root, pointed at a temp dir.

Board review 2026-09-17 ("test_bot_pocket t13 runs a live ib_bot.run() against
the real /root exit-attempts memo..."): each suite repointed only the paths it
knew about when it was written. test_bot_pocket.py predates the exit-attempts
memo and the alert spool, so its live run() read and rewrote
/root/exit_attempts.json and /root/alert_outbox. Nothing failed on the desktop,
where /root does not exist; run as root on the VM it would have erased every
owed-exit record. An audit of every suite found the same shape in most of them:
a module imported with a /root default nobody had repointed, reachable the
moment a stub changed.

So one list, used by every suite, BEFORE the first bot module is imported (the
modules read these variables at import time):

    import testenv                                  # first
    _TMP = testenv.isolate("mps-pocket-")
    import ib_bot                                   # noqa: E402
    testenv.assert_isolated()

assert_isolated() fails when a variable did not take effect, and when ANY
loaded module of this repo still holds a module-level path under /root - so a
/root default added later fails every suite that imports it until it is added
to PATH_VARS, instead of slipping past the one file nobody updated.
run_all_tests.py backs this with an audit hook that blocks and fails any real
file access under /root.

Not a test itself (no test_ prefix): run_all_tests.py does not run it.
"""
import os
import re
import sys
import tempfile
from pathlib import Path, PurePath

# Every environment variable a module reads for a path, and the name it gets
# under the suite's temp dir. A new /root default needs its variable here.
PATH_VARS = {
    "MPS_EARMARK_DIR": "earmark",                   # earmark.py (a directory)
    "MPS_ORDERS_LEDGER": "orders_ledger.jsonl",     # ib_orders.py
    "MPS_CONID_CACHE": "conid_cache.json",          # ib_orders.py
    "MPS_ALERT_DIR": "outbox",                      # alerts.py (a directory)
    "MPS_EXIT_ATTEMPTS": "exit_attempts.json",      # ib_bot.py
    "MPS_FX_LAST_GOOD": "fx_last_good.json",        # ib_bot.py
    "MPS_OAUTH_DIR": "oauth",                       # ib_web.py (the credentials)
    "MPS_COMMANDS_DONE": "commands_done.json",      # ib_commands.py
    "MPS_FLEX_CONF": "flex.conf",                   # flex_dividends.py
    "MPS_REPO": "repo",                             # publish_web.py, daily_signal.py
    "MPS_BACKFILL_MARK": "mps_backfill_done",       # publish_web.py
    "MPS_PREV": "daily_signal_prev.json",           # daily_signal.py
    "MPS_MANUAL": "manual_state.json",              # daily_signal.py
    "MPS_ENV": "telegram.env",                      # daily_signal.py
    "MPS_TG_OFFSET": "telegram_offset.json",        # telegram_poll.py
    "MPS_TG_LOCK": "tg_poll.lock",                  # telegram_poll.py (/tmp: the live poller's lock)
}
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
_DRIVE = re.compile(r"^[A-Za-z]:")


def under_root(p, resolve=True):
    """True for /root, anything below it, and X:\\root on Windows (where
    "/root" resolves to the current drive's \\root). `resolve` makes a relative
    path absolute first; os.path.abspath reads only the cwd. Never raises."""
    if isinstance(p, int) or p is None:
        return False                                 # a file descriptor, or no path
    try:
        p = os.fspath(p)
    except TypeError:
        return False
    if isinstance(p, bytes):
        p = p.decode("utf-8", "replace")
    if not isinstance(p, str) or not p:
        return False
    if p.startswith("\\\\?\\") or p.startswith("//?/"):
        p = p[4:]
    if resolve:
        p = os.path.abspath(p)
    p = p.replace("\\", "/")
    if _DRIVE.match(p):
        p = p[2:]
    while "//" in p:
        p = p.replace("//", "/")
    if os.name == "nt":
        p = p.lower()
    return p == "/root" or p.startswith("/root/")


def isolate(prefix="mps-test-"):
    """Point every PATH_VARS variable under a new temp dir and return it.

    Call it BEFORE importing any bot module. Values are overwritten, never
    defaulted: a shell that exported MPS_EARMARK_DIR=/root must not leak in. A
    suite may still set a variable to another temp path afterwards. The earmark
    directory is created, since earmark writes into it without creating it."""
    tmp = Path(tempfile.mkdtemp(prefix=prefix))
    for var, name in PATH_VARS.items():
        os.environ[var] = str(tmp / name)
    (tmp / PATH_VARS["MPS_EARMARK_DIR"]).mkdir()
    return tmp


def root_defaults():
    """'module.NAME = path' for every module-level str/Path attribute under
    /root in any loaded module of this repo."""
    bad = []
    for name, mod in sorted(list(sys.modules.items()), key=lambda kv: kv[0]):
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        f = os.path.normcase(os.path.abspath(f))
        if not f.startswith(os.path.normcase(REPO) + os.sep):
            continue
        for attr, val in list(vars(mod).items()):
            if isinstance(val, (str, PurePath)) and under_root(val, resolve=False):
                bad.append("%s.%s = %s" % (name, attr, val))
    return bad


def assert_isolated():
    """Every PATH_VARS variable is set, off /root and inside the temp dir, and
    no loaded module of this repo still holds a /root path."""
    temp = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
    for var in PATH_VARS:
        val = os.environ.get(var)
        assert val, "%s is not set - call testenv.isolate() first" % var
        assert not under_root(val), "%s=%s is under /root" % (var, val)
        assert os.path.normcase(os.path.abspath(val)).startswith(temp + os.sep), \
            "%s=%s is not in the temp dir" % (var, val)
    bad = root_defaults()
    assert not bad, "still pointing at /root:\n  " + "\n  ".join(bad)
