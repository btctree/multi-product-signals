#!/usr/bin/env python3
"""Tests: the 22:40 UTC post-close build request (owner decision 2026-11-17).

Run from this directory:  python test_post_close_build.py

execution/post_close_build.py runs on the VM from a cron line at 22:40 UTC
Monday to Friday. It commits ONE tracked file, data/post_close_build.json, in
a commit WITHOUT [skip ci], so .github/workflows/daily.yml's push trigger
builds signals after every market has closed and settled - in time for the
23:35 trading run. Locked down:
  * refused on Saturday and Sunday (UTC), and before today's US close has
    settled (22:30 UTC in winter, 21:30 in US summer), touching no git at all;
  * idempotent per UTC date: a request for today on origin/main means nothing
    to do - unless it was made before the settle;
  * ONLY that file is committed (`commit --only`), with the VM's ib-bot
    identity; other modified, staged and untracked files stay as they were;
  * the message carries no skip token ([skip ci], [ci skip], [no ci], ...);
  * --dry writes, commits and pushes nothing;
  * a push failure, a wrong branch or a commit holding anything else exits 1
    and pushes nothing more; a lock held by another copy exits 0 doing
    nothing; nothing ever raises past main();
  * the index lock of a racing publisher is waited out;
  * the file parses as Python 3.9 and imports only the standard library,
    gitpush, runlock and market_clock.
Two layers: stubbed git (every branch of the logic, no subprocess), and real
git against a temporary bare repository standing in for GitHub - no network.
Global and system git config are switched off, so the commit has to work with
the identity the program passes, as on the VM. Nothing here touches /root.
"""
import ast
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("IB_BACKEND", "web")
import testenv                                     # noqa: E402
_TMP = testenv.isolate("mps-postclose-")

_GCFG = _TMP / "empty.gitconfig"
_GCFG.write_text("", encoding="utf-8")
os.environ["GIT_CONFIG_GLOBAL"] = str(_GCFG)
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
os.environ["GIT_TERMINAL_PROMPT"] = "0"
for _v in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
           "GIT_COMMITTER_EMAIL", "GIT_DIR", "GIT_WORK_TREE", "EMAIL"):
    os.environ.pop(_v, None)

import gitpush                                     # noqa: E402
import post_close_build as P                       # noqa: E402
import runlock                                     # noqa: E402
testenv.assert_isolated()

UTC = timezone.utc
HERE = Path(__file__).resolve().parent
REAL_RUN = subprocess.run
OP = ["-c", "user.email=op@example.com", "-c", "user.name=operator"]


def at(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


MON_2240 = at(2026, 11, 16, 22, 40, 3)          # winter: US settled 22:30 UTC


# ------------------------------------------------------------ stubbed git --
class R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def verb(args):
    """The git subcommand, past any -c pairs."""
    i = 0
    while i < len(args) and args[i] == "-c":
        i += 2
    return args[i] if i < len(args) else ""


class FakeGit:
    """git(args) -> R, by subcommand; a list of R is used up in order."""

    def __init__(self, **by_verb):
        self.calls, self.by = [], by_verb
        self.by.setdefault("symbolic-ref", R(0, "main\n"))
        self.by.setdefault("show-names", R(0, P.REL + "\n"))
        self.by.setdefault("log", None)

    def __call__(self, args):
        self.calls.append(list(args))
        v = verb(args)
        if v == "show" and "--name-only" in args:
            v = "show-names"
        r = self.by.get(v, R(0))
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        if v == "log" and r is None:
            msg = [a for a in self.calls if verb(a) == "commit"]
            subject = msg[-1][msg[-1].index("-m") + 1].splitlines()[0] if msg else ""
            return R(0, subject + "\n")
        return r

    def verbs(self):
        return [verb(a) for a in self.calls]


def run_stub(now, git, push_ok=True, dry=False, repo=None):
    repo = Path(repo or tempfile.mkdtemp(prefix="stub-", dir=str(_TMP)))
    pushes, sleeps, lines = [], [], []
    old_log = P.log
    P.log = lambda m: lines.append(m)
    try:
        rc = P.request_build(repo, now, dry=dry, git=git,
                             push=lambda: pushes.append(1) or push_ok,
                             sleep=lambda s: sleeps.append(s))
    finally:
        P.log = old_log
    return rc, repo, pushes, sleeps, lines


def origin_doc(day, requested_at):
    return R(0, json.dumps({"requested_at": requested_at, "for_close": day,
                            "reason": P.REASON}))


def t1_weekend_and_too_early_are_refused_without_git():
    for now in (at(2026, 11, 21, 22, 40), at(2026, 11, 22, 22, 40)):      # Sat, Sun
        g = FakeGit()
        rc, repo, pushes, _, lines = run_stub(now, g)
        assert rc == 0 and g.calls == [] and pushes == [], (now, g.calls)
        assert [l for l in lines if l.startswith("refused:") and "no market closed" in l], lines
        assert not (repo / P.REL).exists()
    # before today's US settle: winter 22:30, US summer 21:30
    for now, settle in ((at(2026, 11, 16, 15, 0), "22:30"), (at(2026, 11, 16, 22, 29, 59), "22:30"),
                        (at(2026, 9, 16, 21, 0), "21:30")):
        g = FakeGit()
        rc, repo, pushes, _, lines = run_stub(now, g)
        assert rc == 0 and g.calls == [] and pushes == [], (now, g.calls)
        assert [l for l in lines if "too early" in l and settle in l], (now, lines)
    # at or after the settle it goes ahead (summer: 21:31 is post-close)
    for now in (at(2026, 11, 16, 22, 30), at(2026, 9, 16, 21, 31)):
        g = FakeGit(show=R(128, "", "fatal: path does not exist"))
        rc, repo, pushes, _, lines = run_stub(now, g)
        assert rc == 0 and pushes == [1] and "commit" in g.verbs(), (now, g.verbs(), lines)
    print("t1 Saturday, Sunday and before the US settle: refused, no git touched OK")


def t2_already_requested_today_on_origin_is_a_no_op():
    g = FakeGit(show=origin_doc("2026-11-16", "2026-11-16T22:40:05Z"))
    rc, repo, pushes, _, lines = run_stub(MON_2240, g)
    assert rc == 0 and pushes == [], (rc, pushes)
    assert g.verbs() == ["symbolic-ref", "fetch", "show"], g.verbs()
    assert g.calls[2] == ["show", "origin/main:" + P.REL], g.calls[2]
    assert not (repo / P.REL).exists()
    assert [l for l in lines if "already requested for 2026-11-16" in l], lines
    # yesterday's request, or one made before today's settle, does not count
    for doc in (origin_doc("2026-11-13", "2026-11-13T22:40:02Z"),
                origin_doc("2026-11-16", "2026-11-16T15:00:00Z"),
                origin_doc("2026-11-16", "soon"), R(0, "not json")):
        g = FakeGit(show=doc)
        rc, repo, pushes, _, lines = run_stub(MON_2240, g)
        assert rc == 0 and pushes == [1], (doc.stdout, rc, pushes, lines)
    # a failed fetch is said, and judged on the last fetched origin/main
    g = FakeGit(fetch=R(1, "", "fatal: unable to access"),
                show=origin_doc("2026-11-16", "2026-11-16T22:40:05Z"))
    rc, repo, pushes, _, lines = run_stub(MON_2240, g)
    assert rc == 0 and pushes == [] and [l for l in lines if "git fetch failed" in l], lines
    print("t2 today's request already on origin: nothing done; an early or old one is not it OK")


def t3_the_request_commits_one_file_without_a_skip_token():
    g = FakeGit(show=R(128, "", "fatal: path 'data/post_close_build.json' does not exist"))
    rc, repo, pushes, _, lines = run_stub(MON_2240, g)
    assert rc == 0 and pushes == [1], (rc, lines)
    doc = json.loads((repo / P.REL).read_text(encoding="utf-8"))
    assert doc == {"requested_at": "2026-11-16T22:40:03Z", "for_close": "2026-11-16",
                   "reason": "post-close signals build"}, doc
    raw = (repo / P.REL).read_bytes()
    assert raw.endswith(b"}\n") and b"\r" not in raw, raw
    assert not [p for p in (repo / "data").iterdir() if p.name != "post_close_build.json"]
    assert g.verbs() == ["symbolic-ref", "fetch", "show", "add", "commit", "show", "log"], \
        g.verbs()
    assert g.calls[3] == ["add", "--", P.REL], g.calls[3]
    c = g.calls[4]
    assert c[:4] == gitpush.IDENT, c
    assert c[4:] == ["commit", "--only", "-q", "-m", P.commit_message("2026-11-16"), "--", P.REL], c
    msg = c[c.index("-m") + 1]
    assert msg.splitlines()[0] == "signals: post-close build request for 2026-11-16", msg
    assert [l for l in lines if l.startswith("requested:")], lines
    print("t3 one file, --only, the ib-bot identity, then the push OK")


def t4_no_skip_token_in_any_message():
    for day in ("2026-11-16", "2026-11-20", "2027-01-04"):
        msg = P.commit_message(day).lower()
        for tok in P.SKIP_TOKENS + ("skip ci", "ci skip", "no ci", "skip actions",
                                    "actions skip", "***no_ci***"):
            assert tok not in msg, (tok, msg)
    src = (HERE / "post_close_build.py").read_text(encoding="utf-8")
    body = src[src.index("def commit_message"):src.index("def request_doc")]
    assert "skip" not in body.split('"""', 2)[2].lower(), "no skip wording in the message text"
    print("t4 the commit message carries no skip token, in any spelling OK")


def t5_dry_run_changes_nothing():
    g = FakeGit(show=R(128))
    rc, repo, pushes, _, lines = run_stub(MON_2240, g, dry=True)
    assert rc == 0 and pushes == [], (rc, pushes)
    assert g.verbs() == ["symbolic-ref", "fetch", "show"], g.verbs()
    assert not (repo / "data").exists(), list(repo.iterdir())
    assert [l for l in lines if l.startswith("--dry: would write") and "nothing written" in l], lines
    print("t5 --dry: the would-be file and commit are logged, nothing written or pushed OK")


def t6_failures_exit_1_and_push_nothing_more():
    # the push refused (gitpush has already logged why)
    g = FakeGit(show=R(128))
    rc, repo, pushes, _, lines = run_stub(MON_2240, g, push_ok=False)
    assert rc == 1 and pushes == [1] and [l for l in lines if "FAILED: push refused" in l], lines
    # the checkout on another branch: nothing committed
    g = FakeGit(**{"symbolic-ref": R(0, "release/x\n")})
    rc, repo, pushes, _, lines = run_stub(MON_2240, g)
    assert rc == 1 and pushes == [] and "commit" not in g.verbs(), g.verbs()
    # a commit that somehow holds another file is undone, never pushed
    g = FakeGit(show=R(128), **{"show-names": R(0, P.REL + "\ndata/bot_state.json\n")})
    rc, repo, pushes, _, lines = run_stub(MON_2240, g)
    assert rc == 1 and pushes == [], (rc, pushes)
    assert g.calls[-1] == ["reset", "--soft", "HEAD~1"], g.calls[-1]
    # add or commit refused for good
    for v in ("add", "commit"):
        g = FakeGit(show=R(128), **{v: R(1, "", "fatal: something broke")})
        rc, repo, pushes, _, lines = run_stub(MON_2240, g)
        assert rc == 1 and pushes == [] and [l for l in lines if "FAILED: git " + v in l], (v, lines)
    # main() never raises: a bad argument, and git itself exploding
    assert P.main(["--bogus"], now=MON_2240, repo=_TMP) == 1
    old = gitpush._git

    def boom(*a, **k):
        raise RuntimeError("git exploded")
    gitpush._git = boom
    try:
        assert P.main([], now=MON_2240, repo=Path(tempfile.mkdtemp(dir=str(_TMP)))) == 1
    finally:
        gitpush._git = old
    print("t6 push refused, wrong branch, a stray file, git errors: exit 1, nothing raised OK")


def t7_a_racing_publishers_index_lock_is_waited_out():
    lock = R(128, "", "fatal: Unable to create '/x/.git/index.lock': File exists.")
    g = FakeGit(show=R(128), add=[lock, lock, R(0)], commit=[lock, R(0)])
    rc, repo, pushes, sleeps, lines = run_stub(MON_2240, g)
    assert rc == 0 and pushes == [1], (rc, lines)
    assert g.verbs().count("add") == 3 and g.verbs().count("commit") == 2, g.verbs()
    assert sleeps == [P.LOCK_WAIT_S] * 3, sleeps
    # a lock that never clears gives up after LOCK_TRIES
    g = FakeGit(show=R(128), add=[lock])
    rc, repo, pushes, sleeps, lines = run_stub(MON_2240, g)
    assert rc == 1 and g.verbs().count("add") == P.LOCK_TRIES and pushes == [], g.verbs()
    print("t7 a held index lock is retried, then given up after %d tries OK" % P.LOCK_TRIES)


def t8_python39_and_imports():
    src = (HERE / "post_close_build.py").read_text(encoding="utf-8")
    tree = ast.parse(src, filename="post_close_build.py", feature_version=(3, 9))
    imported = set()
    for node in ast.walk(tree):
        assert type(node).__name__ != "Match", "a match statement needs 3.10"
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(str(node.module).split(".")[0])
        elif isinstance(node, ast.Attribute):
            assert node.attr != "UTC", "datetime.UTC is 3.11+"
    assert imported <= {"json", "os", "sys", "time", "datetime", "pathlib",
                        "gitpush", "runlock", "market_clock"}, imported
    for dep in ("gitpush.py", "runlock.py", "market_clock.py"):
        ast.parse((HERE / dep).read_text(encoding="utf-8"), filename=dep, feature_version=(3, 9))
    print("t8 parses as Python 3.9; stdlib + gitpush, runlock, market_clock only OK")


# -------------------------------------------------------------- real git --
def git(repo, *args, check=True):
    r = REAL_RUN(["git", "-C", str(repo)] + list(args), capture_output=True, text=True,
                 timeout=60)
    if check:
        assert r.returncode == 0, "git %s: %s" % (" ".join(args), r.stderr)
    return r


def out(repo, *args):
    return git(repo, *args).stdout.strip()


def world(tag):
    """A bare 'GitHub' with one commit on main, and the VM checkout cloned
    from it. Returns (origin, vm)."""
    d = Path(tempfile.mkdtemp(prefix="pc-%s-" % tag, dir=str(_TMP)))
    origin, seed, vm = d / "origin.git", d / "seed", d / "vm"
    REAL_RUN(["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True)
    git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    REAL_RUN(["git", "init", "-q", str(seed)], check=True, capture_output=True)
    git(seed, "symbolic-ref", "HEAD", "refs/heads/main")
    for rel, body in {"engine/app.py": "VERSION = 1\n",
                      "data/bot_state.json": '{\n "netliq": 100\n}\n',
                      "data/netliq_history.json": '{\n "series": []\n}\n'}.items():
        p = seed / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8", newline="\n")
    git(seed, "add", "-A")
    git(seed, *OP, "commit", "-q", "-m", "seed")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-q", "origin", "main")
    REAL_RUN(["git", "clone", "-q", str(origin), str(vm)], check=True, capture_output=True)
    return origin, vm


def run_main(vm, now, argv=()):
    lines = []
    old_log = P.log
    P.log = lambda m: lines.append(m)
    try:
        rc = P.main(list(argv), now=now, repo=vm)
    finally:
        P.log = old_log
    return rc, lines


def t9_real_git_one_file_on_origin_dirty_files_untouched():
    origin, vm = world("one")
    # a dirty VM checkout: a modified tracked file, a staged change, an untracked file
    (vm / "data" / "bot_state.json").write_text('{\n "netliq": 222\n}\n', encoding="utf-8",
                                                newline="\n")
    (vm / "engine" / "app.py").write_text("VERSION = 2\n", encoding="utf-8", newline="\n")
    git(vm, "add", "engine/app.py")
    (vm / "data" / "scratch.json").write_text("{}\n", encoding="utf-8")
    before = out(vm, "status", "--porcelain")
    head0 = out(origin, "rev-parse", "main")

    rc, lines = run_main(vm, MON_2240)
    assert rc == 0, lines
    head1 = out(origin, "rev-parse", "main")
    assert head1 != head0 and out(origin, "rev-parse", "main~1") == head0
    files = out(origin, "show", "--name-only", "--format=", "main").splitlines()
    assert files == [P.REL], files
    msg = out(origin, "log", "-1", "--format=%B", "main")
    assert msg.splitlines()[0] == "signals: post-close build request for 2026-11-16", msg
    assert not [t for t in P.SKIP_TOKENS if t in msg.lower()], msg
    assert out(origin, "log", "-1", "--format=%an <%ae>|%cn <%ce>", "main") == \
        "ib-bot <bot@vm>|ib-bot <bot@vm>"
    doc = json.loads(out(origin, "show", "main:" + P.REL))
    assert doc["for_close"] == "2026-11-16" and doc["requested_at"] == "2026-11-16T22:40:03Z", doc
    # everything else exactly as it was: modified, staged and untracked
    after = out(vm, "status", "--porcelain")
    assert after == before, (before, after)
    assert (vm / "data" / "bot_state.json").read_text(encoding="utf-8") == '{\n "netliq": 222\n}\n'
    assert out(vm, "diff", "--cached", "--name-only") == "engine/app.py"
    assert out(origin, "show", "main:engine/app.py") == "VERSION = 1"
    assert not (vm / ".git" / "rebase-merge").exists()

    # the same day again: already on origin, nothing new
    rc, lines = run_main(vm, at(2026, 11, 16, 22, 55))
    assert rc == 0 and out(origin, "rev-parse", "main") == head1, lines
    assert [l for l in lines if "already requested for 2026-11-16" in l], lines
    # --dry the next day: nothing
    rc, lines = run_main(vm, at(2026, 11, 17, 22, 40), ["--dry"])
    assert rc == 0 and out(origin, "rev-parse", "main") == head1, lines
    # the next day for real: a new request on top
    rc, lines = run_main(vm, at(2026, 11, 17, 22, 40, 1))
    assert rc == 0 and out(origin, "rev-parse", "main~1") == head1, lines
    assert json.loads(out(origin, "show", "main:" + P.REL))["for_close"] == "2026-11-17"
    assert out(origin, "show", "--name-only", "--format=", "main") == P.REL
    print("t9 real git: only the request reaches origin, as ib-bot; dirty files untouched; "
          "once a day OK")


def t10_real_git_origin_moved_and_the_lock():
    origin, vm = world("moved")
    # the operator deploys after the VM's last fetch
    op = Path(tempfile.mkdtemp(prefix="op-", dir=str(_TMP))) / "op"
    REAL_RUN(["git", "clone", "-q", str(origin), str(op)], check=True, capture_output=True)
    (op / "engine" / "app.py").write_text("VERSION = 3\n", encoding="utf-8", newline="\n")
    git(op, *OP, "commit", "-q", "-am", "deploy")
    git(op, "push", "-q", "origin", "main")
    deploy = out(origin, "rev-parse", "main")
    rc, lines = run_main(vm, MON_2240)
    assert rc == 0, lines
    assert out(origin, "rev-parse", "main~1") == deploy
    assert out(origin, "show", "--name-only", "--format=", "main") == P.REL
    assert out(origin, "show", "main:engine/app.py") == "VERSION = 3"
    assert not (vm / ".git" / "rebase-merge").exists()
    # another copy holds the lock: this one does nothing, exit 0
    origin, vm = world("lock")
    head0 = out(origin, "rev-parse", "main")
    with runlock.hold("test", wait_s=0, path=str(vm / ".git" / P.LOCK_NAME)) as got:
        assert got
        rc, lines = run_main(vm, MON_2240)
    assert rc == 0 and out(origin, "rev-parse", "main") == head0, lines
    assert [l for l in lines if "another post_close_build is running" in l], lines
    print("t10 real git: replayed over an operator's deploy; a held lock does nothing OK")


if __name__ == "__main__":
    t1_weekend_and_too_early_are_refused_without_git()
    t2_already_requested_today_on_origin_is_a_no_op()
    t3_the_request_commits_one_file_without_a_skip_token()
    t4_no_skip_token_in_any_message()
    t5_dry_run_changes_nothing()
    t6_failures_exit_1_and_push_nothing_more()
    t7_a_racing_publishers_index_lock_is_waited_out()
    t8_python39_and_imports()
    t9_real_git_one_file_on_origin_dirty_files_untouched()
    t10_real_git_origin_moved_and_the_lock()
    print("ALL POST-CLOSE BUILD TESTS PASS")
