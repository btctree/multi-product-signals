#!/usr/bin/env python3
"""Run every test suite, and FAIL any suite that touches /root.

Run from this directory:  python run_all_tests.py        (-v for full output)

Why this exists (board review 2026-09-17, "test_bot_pocket t13 runs a live
ib_bot.run() against the real /root exit-attempts memo..."): test_bot_pocket.py
was written in parallel with the refused-exit alerts and never repointed
MPS_ALERT_DIR / MPS_EXIT_ATTEMPTS. Its live run() read and rewrote
/root/exit_attempts.json and /root/alert_outbox/exit_episodes.json. On the
desktop /root is C:\\root, which does not exist, so the suite passed silently;
run as root on the VM it would have erased every owed-exit record. Every suite's
docstring said "nothing here touches /root" - a promise nothing checked.

How: each suite runs in its own interpreter (`run_all_tests.py --child SUITE`)
with a sys.addaudithook installed BEFORE the suite's first import. Two checks:

  1. ACCESS. The hook watches open, os.replace (audited as os.rename), unlink
     (audited as os.remove), rmdir, mkdir, listdir, scandir and the other
     path-taking events in _EVENTS, plus the arguments of a spawned process.
     For any path under /root (or X:\\root on Windows, where "/root" resolves to
     the current drive's \\root) it:
       * BLOCKS the access - the hook raises PermissionError, so on the VM the
         file is never read or written, whatever the suite does next;
       * RECORDS it - a suite that swallows the error (earmark.marker() and most
         readers catch Exception) still fails: the child exits AUDIT_EXIT after
         the suite, whatever the suite itself returned.
     CPython raises no audit event for os.stat / Path.exists, so those are not
     seen; they reveal whether a file exists, never what is in it.
  2. REACHABLE DEFAULTS. After the suite, any module of this repo that is still
     loaded with a module-level path under /root fails it (DEFAULTS_EXIT). An
     access only shows up when a stub lets the code get that far; a default
     left at /root is one changed stub away from it (testenv.py).

A self-check runs first and proves both checks fire on a deliberate case,
including a /root read the caller swallows; if it does not, no suite is run.
Its probes are harmless even with no hook at all: reads, a listdir, and writes
to names that do not exist under a parent that does not exist.

Stdlib only; works on 3.9 (the VM's /usr/bin/python3) through 3.13.

NOT FROM A CHECKOUT UNDER /root. On the VM the repo is /root/multi-product-signals,
where the guard blocks the suite files themselves and ib_bot.STATE and
FILLS_LEDGER are the live files - so the guard is not loosened. main() and
testenv.isolate() stop there with exit code 2. Run the suites from a clean
export instead (git archive, so no untracked live state comes along):

  rm -rf /tmp/mps-test && mkdir -p /tmp/mps-test && git -C /root/multi-product-signals archive HEAD | tar -x -C /tmp/mps-test && cd /tmp/mps-test && PYTHONIOENCODING=utf-8 IB_BACKEND=web python3.11 execution/run_all_tests.py
"""
import os
import runpy
import subprocess
import sys
import tempfile
import time
import traceback

import testenv                       # stdlib only; under_root + root_defaults

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CHILD_TIMEOUT_S = 900
AUDIT_EXIT = 3                       # the child touched a path under /root
DEFAULTS_EXIT = 4                    # a loaded module still defaults to /root

# audit event -> positions of the path arguments it carries
_EVENTS = {
    "open": (0,),
    # os.replace raises "os.rename" (CPython shares internal_rename); both listed
    "os.rename": (0, 1), "os.replace": (0, 1), "os.link": (0, 1), "os.symlink": (0, 1),
    "os.remove": (0,),               # os.unlink and Path.unlink raise this one
    "os.rmdir": (0,), "os.mkdir": (0,), "os.listdir": (0,), "os.scandir": (0,),
    "os.truncate": (0,), "os.chmod": (0,), "os.chown": (0,), "os.utime": (0,),
    "shutil.rmtree": (0,), "shutil.copyfile": (0, 1), "shutil.copymode": (0, 1),
    "shutil.copystat": (0, 1), "shutil.copytree": (0, 1), "shutil.move": (0, 1),
    "glob.glob": (0,), "tempfile.mkstemp": (0,), "tempfile.mkdtemp": (0,),
}
under_root = testenv.under_root


def _text(p):
    try:
        p = os.fspath(p)
    except TypeError:
        return None
    if isinstance(p, bytes):
        p = p.decode("utf-8", "replace")
    return p if isinstance(p, str) and p else None


def _paths(event, args):
    if event == "subprocess.Popen":
        # (executable, args, cwd, env): a spawned process is outside this hook,
        # so an ABSOLUTE /root argument (git -C /root/...) counts here.
        out = [args[0], args[2]] if len(args) > 2 else list(args[:1])
        argv = args[1] if len(args) > 1 else None
        if isinstance(argv, (list, tuple)):
            out.extend(argv)
        elif argv is not None:
            out.extend(str(argv).split())
        # Not os.path.isabs: on Windows since 3.13, "/root/x" is not absolute.
        return [a for a in out if not isinstance(a, int) and _text(a) is not None
                and (_text(a)[:1] in ("/", "\\") or _text(a)[1:2] == ":")]
    return [args[i] for i in _EVENTS.get(event, ()) if i < len(args)]


def install_guard(record):
    """Install the audit hook; every blocked access is appended to `record` as
    (event, path(s), where)."""
    busy = [False]
    stdlib = os.path.normcase(os.path.dirname(os.__file__))

    def hook(event, args):
        if busy[0] or (event not in _EVENTS and event != "subprocess.Popen"):
            return
        busy[0] = True                           # the check itself must not recurse
        try:
            hits = [_text(p) for p in _paths(event, args) if under_root(p)]
            if not hits:
                return
            where, f = [], sys._getframe(1)
            while f is not None and len(where) < 3:
                name = f.f_code.co_filename
                if name != __file__ and not os.path.normcase(name).startswith(stdlib):
                    where.append("%s:%d %s" % (os.path.basename(name), f.f_lineno,
                                               f.f_code.co_name))
                f = f.f_back
            record.append((event, " -> ".join(hits), " <- ".join(where)))
            sys.stderr.write("!! AUDIT: %s %s blocked (%s)\n"
                             % (event, " -> ".join(hits), " <- ".join(where)))
        finally:
            busy[0] = False
        raise PermissionError("test isolation: %s on %r is under /root" % (event, hits[0]))

    sys.addaudithook(hook)


def child(target):
    """Run one suite as __main__ under the guard. Exit code: the suite's own,
    AUDIT_EXIT when it touched /root, DEFAULTS_EXIT when a module it loaded
    still defaults to /root - either of the last two even if the suite passed."""
    import atexit
    record = []
    install_guard(record)                        # before the suite's first import

    def verdict():
        # Registered first, so it runs LAST: an atexit handler of the suite's
        # that touches /root is caught as well.
        sys.stdout.flush()
        if record:
            sys.stderr.write("AUDIT FAIL: %d access(es) under /root:\n" % len(record))
            for event, path, where in record:
                sys.stderr.write("  %s %s  (%s)\n" % (event, path, where))
            sys.stderr.flush()
            os._exit(AUDIT_EXIT)
        bad = testenv.root_defaults()
        if bad:
            sys.stderr.write("AUDIT FAIL: %d module path(s) still default to /root "
                             "(add the variable to testenv.PATH_VARS and call "
                             "testenv.isolate() before the import):\n" % len(bad))
            for b in bad:
                sys.stderr.write("  %s\n" % b)
            sys.stderr.flush()
            os._exit(DEFAULTS_EXIT)
    atexit.register(verdict)
    suite = os.path.abspath(target)
    os.chdir(os.path.dirname(suite))
    sys.path[0] = os.path.dirname(suite)         # as `python test_x.py` would have it
    sys.argv = [suite]
    rc = 0
    try:
        runpy.run_path(suite, run_name="__main__")
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except BaseException:
        traceback.print_exc()
        rc = 1
    sys.exit(rc)


# --------------------------------------------------------------- self-check --
SELF_PROBE = r'''
import os, pathlib, subprocess, sys
# Every probe is harmless even with NO hook: reads, a listdir, writes to names
# that do not exist under a parent that does not exist, and a python -c pass
# that only carries a /root argument. Each one swallows its error, the way
# earmark.marker() and ib_bot's readers do.
N = "/root/.mps-audit-selfcheck-does-not-exist"
probes = [
    lambda: open("/root/exit_attempts.json").read(),
    lambda: pathlib.Path("/root/alert_outbox/exit_episodes.json").read_text(),
    lambda: open(b"/root/.mps-audit-bytes-probe", "rb"),
    lambda: os.listdir("/root"),
    lambda: list(os.scandir("/root")),
    lambda: os.replace(N + "/a.tmp", N + "/a"),
    lambda: pathlib.Path(N + "/a").unlink(),
    lambda: os.mkdir(N + "/sub"),
    lambda: subprocess.run([sys.executable, "-c", "pass", N]),
]
if os.name == "nt":
    probes.append(lambda: open("C:\\root\\exit_attempts.json"))
for fn in probes:
    try:
        fn()
    except BaseException:
        pass
# a temp path is NOT flagged
import tempfile
d = tempfile.mkdtemp(prefix="mps-audit-ok-")
pathlib.Path(d, "x.json").write_text("{}")
os.replace(os.path.join(d, "x.json"), os.path.join(d, "y.json"))
os.listdir(d)
print("probe ran %d /root accesses" % len(probes))
'''

SELF_CLEAN = r'''
import os, pathlib, tempfile
d = tempfile.mkdtemp(prefix="mps-audit-clean-")
pathlib.Path(d, "rootless.json").write_text("{}")   # "root" in a name is fine
os.mkdir(os.path.join(d, "root"))                    # ...and a temp dir called root
os.listdir(d)
print("ALL CLEAN PROBE PASS")
'''

# A module of this repo left holding a /root path, touching nothing.
SELF_DEFAULT = r'''
import os, sys, types
m = types.ModuleType("mps_audit_planted")
m.__file__ = os.path.join(%r, "mps_audit_planted.py")
m.EXIT_ATTEMPTS = "/root/exit_attempts.json"
sys.modules[m.__name__] = m
print("ALL PLANTED PROBE PASS")
''' % HERE


def _run_child(path, cwd, env):
    return subprocess.run([sys.executable, os.path.abspath(__file__), "--child", path],
                          cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, encoding="utf-8", errors="replace",
                          timeout=CHILD_TIMEOUT_S)


def self_check(env):
    """Prove both checks fire. Returns a list of problems (empty = trusted)."""
    problems = []
    for p, want in (("/root", True), ("/root/x/y.json", True), ("/rooted", False),
                    ("/tmp/root/x", False), ("C:\\root\\x", True), ("C:/root", True),
                    ("\\\\?\\C:\\root\\x", True), (b"/root/x", True), (3, False)):
        if under_root(p, resolve=False) is not want:
            problems.append("under_root(%r) should be %s" % (p, want))
    d = tempfile.mkdtemp(prefix="mps-audit-self-")
    scripts = {}
    for name, body in (("probe", SELF_PROBE), ("clean", SELF_CLEAN), ("planted", SELF_DEFAULT)):
        scripts[name] = os.path.join(d, name + ".py")
        with open(scripts[name], "w", encoding="utf-8") as fh:
            fh.write(body)

    r = _run_child(scripts["probe"], d, env)
    if r.returncode != AUDIT_EXIT:
        problems.append("deliberate /root probe exited %s, not %s" % (r.returncode, AUDIT_EXIT))
    if "probe ran" not in r.stdout:
        problems.append("the probe did not run to its end (stdout %r)" % r.stdout[-200:])
    got = [l.split()[2] for l in r.stderr.splitlines() if l.startswith("!! AUDIT:")]
    want = ["open", "open", "open", "os.listdir", "os.scandir", "os.rename",
            "os.remove", "os.mkdir", "subprocess.Popen"] + (["open"] if os.name == "nt" else [])
    if got != want:
        problems.append("blocked events %r, expected %r" % (got, want))
    if "mps-audit-ok-" in r.stderr:
        problems.append("a temp path was flagged")

    r = _run_child(scripts["clean"], d, env)
    if r.returncode != 0 or "ALL CLEAN PROBE PASS" not in r.stdout:
        problems.append("a clean probe failed: rc %s, %r" % (r.returncode, r.stderr[-300:]))

    r = _run_child(scripts["planted"], d, env)
    if r.returncode != DEFAULTS_EXIT or "mps_audit_planted.EXIT_ATTEMPTS" not in r.stderr:
        problems.append("a planted /root default was not caught: rc %s, %r"
                        % (r.returncode, r.stderr[-300:]))
    return problems


def suites():
    out = sorted(os.path.join(HERE, f) for f in os.listdir(HERE)
                 if f.startswith("test_") and f.endswith(".py"))
    eng = os.path.join(REPO, "engine")
    out += sorted(os.path.join(eng, f) for f in os.listdir(eng)
                  if f.startswith("test_") and f.endswith(".py"))
    return out


def main(argv):
    # First, before the self-check: from a checkout under /root the guard
    # blocks the suites themselves and the self-check fails with "the /root
    # guard cannot be trusted", which is the wrong message. Say what to run.
    refusal = testenv.repo_under_root_refusal(REPO)
    if refusal is not None:
        sys.stdout.write(refusal)
        sys.stdout.flush()
        return testenv.REPO_UNDER_ROOT_EXIT                        # 2, nothing run
    if len(argv) >= 2 and argv[0] == "--child":
        child(argv[1])                           # exits
    verbose = "-v" in argv
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["IB_BACKEND"] = "web"
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    problems = self_check(env)
    if problems:
        print("SELF-CHECK FAILED - the /root guard cannot be trusted; no suite was run:")
        for p in problems:
            print("  " + p)
        return 2
    print("self-check: a deliberate /root access and a planted /root default both fail OK")

    failed, all_suites = [], suites()
    for path in all_suites:
        name = os.path.relpath(path, REPO).replace("\\", "/")
        t0 = time.time()
        try:
            r = _run_child(path, os.path.dirname(path), env)
            rc, out, err = r.returncode, r.stdout, r.stderr
        except subprocess.TimeoutExpired:
            rc, out, err = -1, "", "timed out after %ds" % CHILD_TIMEOUT_S
        lines = [l for l in out.splitlines() if l.strip()]
        last = lines[-1] if lines else ""
        ok = rc == 0 and last.startswith("ALL ") and last.endswith("PASS")
        verdict = ("PASS" if ok else "FAIL (/root access)" if rc == AUDIT_EXIT
                   else "FAIL (/root default)" if rc == DEFAULTS_EXIT else "FAIL")
        print("%-36s %-20s %5.1fs  %s" % (name, verdict, time.time() - t0, last))
        if verbose:
            print(out)
        if not ok:
            failed.append(name)
            audit = err.splitlines()
            audit = audit[next((i for i, l in enumerate(audit) if l.startswith("AUDIT FAIL")),
                               len(audit)):]
            for l in audit[:20]:
                print("    " + l.strip())
            if not (last.startswith("ALL ") and last.endswith("PASS")):
                # the suite itself failed too: an audit exit code must not hide it
                tail = [l for l in err.splitlines() if not l.startswith("!! AUDIT")
                        and l not in audit]
                for l in (out.splitlines()[-15:] + tail[-25:]):
                    print("    | " + l)
    if failed:
        print("%d SUITE(S) FAILED: %s" % (len(failed), ", ".join(failed)))
        return 1
    print("ALL %d SUITES PASS, none touched /root or left a /root default loaded"
          % len(all_suites))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
