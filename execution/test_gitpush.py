#!/usr/bin/env python3
"""Tests: the VM publishers' push survives an origin that moved, and never
leaves the checkout mid-rebase.

Run from this directory:  python test_gitpush.py

Board review 2026-09-21: ib_bot.publish_state and publish_web.py committed and
pushed with no fetch, no rebase and no retry. A push refused as
non-fast-forward (the operator deployed since the :25 reset) was dropped by the
next `git reset --hard origin/main`, and the trading run's PLACED, REJECTED and
HALT rows went with it for good. gitpush.push_with_retry replays the commit
onto origin and pushes again; on a real conflict it aborts cleanly.

Real git, no network: a temporary bare repo stands in for GitHub, one clone is
the VM checkout and another is the operator. Global and system git config are
switched off for the run, so the rebase has to work with the identity the
helper passes, as on the VM, which has none. Nothing here touches /root or the
real repo: every path is under the suite's temp dir.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("IB_BACKEND", "web")
import testenv                                     # noqa: E402
_TMP = testenv.isolate("mps-gitpush-")
os.environ.pop("EXCLUDED_CASH", None)

# Hermetic git: no ~/.gitconfig, no system config, no prompts, no identity from
# the environment - the helper must bring its own, as it has to on the VM.
_GCFG = _TMP / "empty.gitconfig"
_GCFG.write_text("", encoding="utf-8")
os.environ["GIT_CONFIG_GLOBAL"] = str(_GCFG)
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
os.environ["GIT_TERMINAL_PROMPT"] = "0"
for _v in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
           "GIT_COMMITTER_EMAIL", "GIT_DIR", "GIT_WORK_TREE", "EMAIL"):
    os.environ.pop(_v, None)

import gitpush                                     # noqa: E402
import runlock                                     # noqa: E402
import ib_bot                                      # noqa: E402
import ib_web                                      # noqa: E402
import publish_web                                 # noqa: E402
testenv.assert_isolated()

# the operator's and the seed's commits (the helper uses its own, gitpush.IDENT)
OP = ["-c", "user.email=op@example.com", "-c", "user.name=operator"]
REAL_RUN = subprocess.run


def git(repo, *args, check=True):
    r = REAL_RUN(["git", "-C", str(repo)] + list(args),
                 capture_output=True, text=True, timeout=60)
    if check:
        assert r.returncode == 0, "git %s: %s" % (" ".join(args), r.stderr)
    return r


def out(repo, *args):
    return git(repo, *args).stdout.strip()


def world(tag, files=None):
    """A bare 'GitHub' with one commit on main, the VM checkout and the
    operator's clone. Returns (origin, vm, op)."""
    d = Path(tempfile.mkdtemp(prefix="gp-%s-" % tag, dir=str(_TMP)))
    origin, seed, vm, op = d / "origin.git", d / "seed", d / "vm", d / "op"
    REAL_RUN(["git", "init", "-q", "--bare", str(origin)], check=True,
             capture_output=True)
    git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    REAL_RUN(["git", "init", "-q", str(seed)], check=True, capture_output=True)
    git(seed, "symbolic-ref", "HEAD", "refs/heads/main")
    base = {"engine/app.py": "VERSION = 1\n",
            "data/bot_state.json": '{\n "netliq": 100\n}\n',
            "data/netliq_history.json": '{\n "series": [],\n "flows": []\n}\n',
            "data/fills_ledger.jsonl": "",
            "data/tax_report.json": "{}\n",
            "data/day_parts.json": "{}\n"}
    base.update(files or {})
    for name, text in base.items():
        (seed / name).parent.mkdir(parents=True, exist_ok=True)
        (seed / name).write_text(text, encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, *(OP + ["commit", "-q", "-m", "seed"]))
    git(seed, "push", "-q", str(origin), "main")
    for c in (vm, op):
        REAL_RUN(["git", "clone", "-q", str(origin), str(c)], check=True,
                 capture_output=True)
    return origin, vm, op


def commit(repo, files, msg, ident=None):
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", *files.keys())
    git(repo, *((ident or gitpush.IDENT) + ["commit", "-q", "-m", msg]))
    return out(repo, "rev-parse", "HEAD")


def operator_deploys(op, files=None, msg="operator: deploy a fix"):
    sha = commit(op, files or {"engine/app.py": "VERSION = 2\n"}, msg, OP)
    git(op, "push", "-q")
    return sha


def rebasing(repo):
    return any((repo / ".git" / d).exists() for d in ("rebase-merge", "rebase-apply"))


def on_main(repo):
    return git(repo, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip() \
        == "refs/heads/main"


def remote_head(origin):
    return out(origin, "rev-parse", "main")


def subjects(origin, n=5):
    return out(origin, "log", "--format=%s", "-n", str(n), "main").splitlines()


def assert_clean(repo):
    assert not rebasing(repo), "the checkout was left mid-rebase"
    assert on_main(repo), "the checkout is not on main"
    st = out(repo, "status", "--porcelain")
    assert st == "", "the checkout is not clean:\n" + st


def t1_a_push_that_races_a_deploy_lands_both_commits():
    origin, vm, op = world("race")
    deploy = operator_deploys(op)
    commit(vm, {"data/bot_state.json": '{\n "netliq": 101\n}\n'},
           "bot: state update [skip ci]")
    lines = []
    assert gitpush.push_with_retry(vm, lines.append) is True, lines
    assert subjects(origin, 3) == ["bot: state update [skip ci]",
                                   "operator: deploy a fix", "seed"], subjects(origin)
    assert remote_head(origin) == out(vm, "rev-parse", "HEAD")
    assert out(origin, "rev-parse", "main~1") == deploy, "the deploy was not kept"
    # the replayed commit is the VM's, committed with the identity the helper
    # passes - the hermetic config here has none, like the VM
    assert out(origin, "log", "-1", "--format=%an|%cn", "main") == "ib-bot|ib-bot"
    assert (vm / "engine" / "app.py").read_text(encoding="utf-8") == "VERSION = 2\n"
    assert_clean(vm)
    joined = "\n".join(lines)
    assert "push refused, attempt 1 of 3" in joined and "[rejected]" in joined, lines
    assert "pushed on attempt 2 of 3" in joined, lines
    print("t1 a push that races the operator's deploy ends with both commits on origin OK")


def t2_a_true_conflict_leaves_the_clone_clean_and_fails():
    origin, vm, op = world("conflict")
    theirs = operator_deploys(op, {"data/netliq_history.json":
                                   '{\n "series": [],\n "flows": [["2026-09-21", 5000]]\n}\n'},
                              "operator: add a deposit flow by hand")
    ours = commit(vm, {"data/netliq_history.json":
                       '{\n "series": [{"d": "2026-09-21"}],\n "flows": []\n}\n'},
                  "bot: state update [skip ci]")
    lines = []
    assert gitpush.push_with_retry(vm, lines.append) is False, lines
    assert_clean(vm)
    assert out(vm, "rev-parse", "HEAD") == ours, "abort must put main back on our commit"
    assert remote_head(origin) == theirs, "origin must be untouched"
    joined = "\n".join(lines)
    assert "CONFLICT" in joined and "rebase aborted" in joined, lines
    assert "attempt 2" not in joined, "a conflict must stop, not retry"
    # and the checkout still works: the next publish in it pushes normally once
    # the hand edit is on origin and ours no longer disagrees with it
    git(vm, "reset", "-q", "--hard", "origin/main")
    commit(vm, {"data/bot_state.json": '{\n "netliq": 102\n}\n'}, "bot: next")
    assert gitpush.push_with_retry(vm, lines.append) is True
    print("t2 a true conflict is aborted: clone clean on its own commit, push fails OK")


def t3_dirty_tracked_files_survive_the_replay():
    origin, vm, op = world("autostash")
    operator_deploys(op)
    commit(vm, {"data/bot_state.json": '{\n "netliq": 103\n}\n'}, "bot: state")
    (vm / "data" / "day_parts.json").write_text('{"dirty": 1}\n', encoding="utf-8")
    lines = []
    assert gitpush.push_with_retry(vm, lines.append) is True, lines
    assert (vm / "data" / "day_parts.json").read_text(encoding="utf-8") == '{"dirty": 1}\n'
    assert out(vm, "stash", "list") == "", "the autostash was not put back"
    assert not rebasing(vm) and on_main(vm)
    assert remote_head(origin) == out(vm, "rev-parse", "HEAD")
    print("t3 an uncommitted tracked change is stashed around the replay and restored OK")


def t4_a_stranded_rebase_survives_the_25_reset_and_is_cleared():
    origin, vm, op = world("strand")
    operator_deploys(op, {"data/bot_state.json": '{\n "netliq": 1\n}\n'})
    commit(vm, {"data/bot_state.json": '{\n "netliq": 2\n}\n'}, "bot: killed run")
    # a publisher killed mid-pull: the rebase stopped and nobody aborted it
    git(vm, *(gitpush.IDENT + ["pull", "-q", "--rebase", "origin", "main"]), check=False)
    assert rebasing(vm)
    # the :25 cron. This is WHY the helper must never strand a rebase: the
    # reset does not end it, HEAD stays detached, and a plain push fails
    git(vm, "fetch", "-q", "origin", "main")
    git(vm, "reset", "-q", "--hard", "origin/main")
    assert rebasing(vm), "git changed: reset --hard now ends a rebase"
    assert git(vm, "push", check=False).returncode != 0
    # publish_web after the reset: writes, commits (on the detached HEAD), pushes
    mine = commit(vm, {"data/bot_state.json": '{\n "netliq": 3\n}\n'},
                  "bot: state update (web api) [skip ci]")
    lines = []
    assert gitpush.push_with_retry(vm, lines.append) is True, lines
    assert "left in progress" in "\n".join(lines), lines
    assert remote_head(origin) == mine, "the fresh commit is the one pushed"
    assert_clean(vm)
    # and the checkout is back to normal: the next publish is a plain push
    commit(vm, {"data/bot_state.json": '{\n "netliq": 4\n}\n'}, "bot: next hour")
    more = []
    assert gitpush.push_with_retry(vm, more.append) is True and more == [], more
    print("t4 a stranded rebase (which the :25 reset leaves in place) is cleared, commit kept OK")


def t5_a_pull_cut_off_by_its_timeout_is_aborted():
    origin, vm, op = world("timeout")
    operator_deploys(op, {"data/bot_state.json": '{\n "netliq": 1\n}\n'})
    ours = commit(vm, {"data/bot_state.json": '{\n "netliq": 2\n}\n'}, "bot: state")

    def killed_mid_pull(cmd, *a, **k):
        r = REAL_RUN(cmd, *a, **k)
        if "pull" in cmd:                         # the rebase is left stopped...
            raise subprocess.TimeoutExpired(cmd, k.get("timeout"))   # ...and cut off
        return r
    lines = []
    subprocess.run = killed_mid_pull
    try:
        assert gitpush.push_with_retry(vm, lines.append) is False, lines
    finally:
        subprocess.run = REAL_RUN
    assert_clean(vm)
    assert out(vm, "rev-parse", "HEAD") == ours
    assert "timed out after 60s" in "\n".join(lines), lines
    print("t5 a pull cut off by its timeout mid-rebase is aborted, checkout clean OK")


def t6_never_raises():
    lines = []
    assert gitpush.push_with_retry(_TMP / "no-such-dir", lines.append) is False
    empty = Path(tempfile.mkdtemp(prefix="gp-empty-", dir=str(_TMP)))
    assert gitpush.push_with_retry(empty, lines.append, attempts=2) is False

    def boom(*a, **k):
        raise RuntimeError("no git here")
    subprocess.run = boom
    try:
        assert gitpush.push_with_retry(empty, lines.append) is False
        assert gitpush.push_with_retry(empty, None) is False      # even a bad log
    finally:
        subprocess.run = REAL_RUN
    assert any("push skipped (no git here)" in l for l in lines), lines
    print("t6 a missing repo, a non-repo, a broken git or a broken log: False, never raises OK")


def t7_publishers_overlap_through_the_push_lock():
    origin, vm, op = world("lock")
    before = remote_head(origin)
    commit(vm, {"data/bot_state.json": '{\n "netliq": 9\n}\n'}, "bot: state")
    lines = []
    with runlock.hold("other publisher", wait_s=0,
                      path=str(vm / ".git" / gitpush.LOCK_NAME)) as got:
        assert got
        assert gitpush.push_with_retry(vm, lines.append, timeout=1) is False
    assert "push lock" in "\n".join(lines), lines
    assert remote_head(origin) == before, "pushed without the lock"
    assert gitpush.push_with_retry(vm, lines.append) is True, "lock not released"
    print("t7 a publisher waits for another's push, and gives up rather than rebase under it OK")


def t8_publish_web_end_to_end_with_a_moved_origin():
    origin, vm, op = world("web")
    operator_deploys(op)
    state = _TMP / "web_state.json"
    ref = [["2026-09-10", 101.5], ["2026-09-11", 102.0], ["2026-09-12", 101.0]]
    state.write_text(json.dumps({"map": {}, "pos": {
        "NVDA": {"entry": 180.0, "stop": 170.0, "adj": 0.995, "px_ref": ref},
        "DELL": {"entry": 120.0, "stop": 110.0}}}), encoding="utf-8")
    old = (publish_web.STATE, publish_web.DATA, publish_web.REPO, publish_web.log,
           ib_web.snapshot, ib_web.ledger, publish_web.sys.argv)
    lines = []
    try:
        publish_web.STATE, publish_web.DATA, publish_web.REPO = state, vm / "data", vm
        publish_web.log = lambda *a: lines.append(" ".join(str(x) for x in a))
        ib_web.snapshot = lambda: {
            "positions": [{"ib_symbol": "NVDA", "qty": 20.0, "avg_cost": 180.0,
                           "ccy": "USD", "mkt_value": 3800.0, "sec_type": "STK"},
                          {"ib_symbol": "DELL", "qty": 5.0, "avg_cost": 120.0,
                           "ccy": "USD", "mkt_value": 600.0, "sec_type": "STK"}],
            "netliq": 300000.0, "cash": {"HKD": 1000.0, "USD": 4000.0}}
        ib_web.ledger = lambda *a, **k: {
            "BASE": {"netliquidationvalue": 300000.0, "exchangerate": 1},
            "USD": {"cashbalance": 4000.0, "exchangerate": 7.85}}
        publish_web.sys.argv = ["publish_web.py"]
        assert publish_web.main() == 0, lines
    finally:
        (publish_web.STATE, publish_web.DATA, publish_web.REPO, publish_web.log,
         ib_web.snapshot, ib_web.ledger, publish_web.sys.argv) = old
    assert subjects(origin, 2) == ["bot: state update (web api) [skip ci]",
                                   "operator: deploy a fix"], subjects(origin)
    assert any("dashboard updated and pushed" in l for l in lines), lines
    assert_clean(vm)
    snap = json.loads(git(origin, "show", "main:data/bot_state.json").stdout)
    rows = {p["symbol"]: p for p in snap["positions"]}
    # item 2: px_ref published next to adj, exactly as publish_state carries it
    assert rows["NVDA"]["px_ref"] == ref and rows["NVDA"]["adj"] == 0.995, rows
    assert rows["DELL"]["px_ref"] is None and rows["DELL"]["adj"] == 1.0, rows
    print("t8 publish_web lands its commit on a moved origin; rows carry px_ref beside adj OK")


class _AV:
    def __init__(self, tag, value, currency):
        self.tag, self.value, self.currency = tag, value, currency


class _PubIB:
    def positions(self):
        return []

    def accountValues(self):
        return [_AV("NetLiquidation", "100000", "HKD"), _AV("CashBalance", "10", "HKD")]


def t9_the_trading_runs_rows_survive_a_deploy_before_its_push():
    import fills_capture
    import flex_dividends
    import uk_cgt
    origin, vm, op = world("bot")
    operator_deploys(op)
    row = {"time": "2026-09-22 09:00 UTC", "action": "SELL", "qty": 4,
           "symbol": "DELL", "limit": 118.0, "ccy": "USD", "reason": "trailing stop",
           "status": "ok", "error": ""}
    ib_bot.PLACED.append(row)
    lines = []
    old = (ib_bot.__file__, ib_bot.log, fills_capture.capture,
           flex_dividends.capture_if_configured, uk_cgt.build_report)
    try:
        # publish_state finds its checkout from its own file: point it at the VM clone
        ib_bot.__file__ = str(vm / "execution" / "ib_bot.py")
        ib_bot.log = lines.append
        fills_capture.capture = lambda *a, **k: 0
        flex_dividends.capture_if_configured = lambda *a, **k: 0
        uk_cgt.build_report = lambda *a, **k: None
        ib_bot.publish_state(_PubIB(), {"map": {}, "pos": {}}, 100000.0)
    finally:
        (ib_bot.__file__, ib_bot.log, fills_capture.capture,
         flex_dividends.capture_if_configured, uk_cgt.build_report) = old
        del ib_bot.PLACED[:]
    assert any("bot state published to dashboard" in l for l in lines), lines
    assert subjects(origin, 2) == ["bot: state update [skip ci]",
                                   "operator: deploy a fix"], subjects(origin)
    snap = json.loads(git(origin, "show", "main:data/bot_state.json").stdout)
    assert snap["activity"][-1]["symbol"] == "DELL", snap["activity"]
    assert not rebasing(vm) and on_main(vm)
    print("t9 ib_bot.publish_state: the run's activity rows reach origin despite a deploy OK")


def t10_both_publishers_use_the_helper():
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("ib_bot.py", "publish_web.py"):
        src = open(os.path.join(here, name), encoding="utf-8").read()
        assert "gitpush.push_with_retry(" in src, "%s no longer uses the helper" % name
        assert '["push"]' not in src, "%s still has a bare push" % name
    print("t10 both publishers push through gitpush, no bare push left OK")


if __name__ == "__main__":
    probe = REAL_RUN(["git", "--version"], capture_output=True, text=True)
    assert probe.returncode == 0, "these tests need git on PATH"
    t1_a_push_that_races_a_deploy_lands_both_commits()
    t2_a_true_conflict_leaves_the_clone_clean_and_fails()
    t3_dirty_tracked_files_survive_the_replay()
    t4_a_stranded_rebase_survives_the_25_reset_and_is_cleared()
    t5_a_pull_cut_off_by_its_timeout_is_aborted()
    t6_never_raises()
    t7_publishers_overlap_through_the_push_lock()
    t8_publish_web_end_to_end_with_a_moved_origin()
    t9_the_trading_runs_rows_survive_a_deploy_before_its_push()
    t10_both_publishers_use_the_helper()
    sys.stdout.flush()
    print("ALL GITPUSH TESTS PASS")
