#!/usr/bin/env python3
"""Golden tests: the test runner and testenv refuse a checkout under /root and
say how to run the tests instead.

Run from this directory:  python test_root_checkout.py

Final review 2026-09-17: on the VM the checkout is /root/multi-product-signals.
testenv.root_defaults() flags every repo module's own __file__, testenv.REPO and
ib_bot.STATE there, and the audit hook blocks the suite files themselves, so
run_all_tests.py printed "SELF-CHECK FAILED - the /root guard cannot be
trusted" and every suite run directly died in assert_isolated(). The guard is
right: inside that checkout ib_bot.STATE and FILLS_LEDGER are the LIVE files.
So it is not loosened. What is locked down:

  * run_all_tests.main() and testenv.isolate() stop FIRST - before the
    self-check, before a temp dir or a variable is set - with exit code 2;
  * the message names the checkout and the exact command that runs every
    suite from a clean `git archive` export;
  * the same command is in run_all_tests.py's docstring and execution/README.md;
  * only /root is refused: /rooted, /tmp/root and /home/root are not, and a
    path that resolves into /root through a symlink is.

Nothing here touches /root: every /root path is a patched string that is never
opened, and the two processes spawned refuse before they could.
"""
import contextlib
import io
import os
import subprocess
import sys

import testenv                                     # noqa: E402
_TMP = testenv.isolate("mps-rootcheckout-")

import run_all_tests                               # noqa: E402
testenv.assert_isolated()

HERE = os.path.dirname(os.path.abspath(__file__))


class Patch:
    def __init__(self, mod, **kw):
        self.mod, self.kw, self.old = mod, kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(self.mod, k)
            setattr(self.mod, k, v)
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(self.mod, k, v)


def vm_checkout():
    # built here, never a module-level constant: a /root string left on this
    # module would itself fail the runner's /root-default check
    return "/" + "root/multi-product-signals"


def spec_command():
    return ("rm -rf /tmp/mps-test && mkdir -p /tmp/mps-test && git -C "
            + vm_checkout() + " archive HEAD | tar -x -C /tmp/mps-test && cd /tmp/mps-test"
            " && PYTHONIOENCODING=utf-8 IB_BACKEND=web python3.11 execution/run_all_tests.py")


def must_not_run(*a, **k):
    raise AssertionError("ran past the /root checkout refusal")


def t1_runner_refuses_a_checkout_under_root_before_anything_runs():
    repo = vm_checkout()
    for argv in ([], ["-v"], ["--child", os.path.join(HERE, "test_earmark.py")]):
        out = io.StringIO()
        with Patch(run_all_tests, REPO=repo, self_check=must_not_run, suites=must_not_run,
                   child=must_not_run, _run_child=must_not_run), \
                contextlib.redirect_stdout(out):
            rc = run_all_tests.main(argv)
        assert rc == 2, (argv, rc)
        text = out.getvalue()
        assert "REFUSED: this checkout is under /root (%s)" % repo in text, text
        assert "  " + spec_command() + "\n" in text, text
        assert "SELF-CHECK" not in text and "SUITES PASS" not in text, text
    # the drive-letter form Windows gives /root, a trailing slash, /root itself
    # and another checkout under it: all refused, all given the same command
    for other in ("C:\\root\\multi-product-signals", repo + "/", "/" + "root",
                  "/" + "root/elsewhere/mps"):
        out = io.StringIO()
        with Patch(run_all_tests, REPO=other, self_check=must_not_run), \
                contextlib.redirect_stdout(out):
            assert run_all_tests.main([]) == 2, other
        assert "  " + spec_command() + "\n" in out.getvalue(), (other, out.getvalue())
    # only /root: look-alikes are not refused
    for fine in ("/rooted/multi-product-signals", "/tmp/root/multi-product-signals",
                 "/home/root/multi-product-signals", "/tmp/mps-test", run_all_tests.REPO):
        assert testenv.repo_under_root_refusal(fine) is None, fine
    # a symlink into /root is refused as what it resolves to
    with Patch(os.path, realpath=lambda p: vm_checkout()):
        assert testenv.repo_under_root("/opt/mps") is True
        assert "(/opt/mps)" in testenv.repo_under_root_refusal("/opt/mps")
    # the real checkout this suite runs from is not under /root
    assert not testenv.repo_under_root() and not testenv.repo_under_root(run_all_tests.REPO)
    print("t1 run_all_tests.main() refuses a /root checkout first, exit 2, with the command OK")


def t2_isolate_refuses_before_creating_anything():
    before_env = {v: os.environ[v] for v in testenv.PATH_VARS}
    tmpdir = os.path.dirname(str(_TMP))
    made = lambda: sorted(n for n in os.listdir(tmpdir) if n.startswith("mps-refused-"))
    before_dirs = made()
    err = io.StringIO()
    with Patch(testenv, REPO=vm_checkout()), contextlib.redirect_stderr(err):
        try:
            testenv.isolate("mps-refused-")
            raise AssertionError("isolate() must stop")
        except SystemExit as e:
            assert e.code == 2, e.code
    text = err.getvalue()
    assert "REFUSED: this checkout is under /root (%s)" % vm_checkout() in text, text
    assert "  " + spec_command() + "\n" in text, text
    assert made() == before_dirs, "a temp dir was created before the refusal"
    assert {v: os.environ[v] for v in testenv.PATH_VARS} == before_env, "variables changed"
    # a checkout elsewhere isolates as before
    tmp = testenv.isolate("mps-rootcheckout-again-")
    assert str(tmp).startswith(tmpdir) and os.environ["MPS_REPO"] == str(tmp / "repo")
    print("t2 testenv.isolate() refuses a /root checkout before creating anything OK")


def t3_real_processes_exit_2_and_the_docs_carry_the_command():
    # as `python3 run_all_tests.py` and `python3 test_x.py` would end on the VM
    code = ("import sys, run_all_tests as r; r.REPO = %r; "
            "r.self_check = r.suites = None; sys.exit(r.main([]))" % vm_checkout())
    p = subprocess.run([sys.executable, "-c", code], cwd=HERE, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True, encoding="utf-8")
    assert p.returncode == 2 and spec_command() in p.stdout, (p.returncode, p.stdout, p.stderr)
    code = ("import testenv; testenv.REPO = %r; testenv.isolate(); "
            "print('ISOLATED ANYWAY')" % vm_checkout())
    p = subprocess.run([sys.executable, "-c", code], cwd=HERE, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True, encoding="utf-8")
    assert p.returncode == 2 and "ISOLATED ANYWAY" not in p.stdout, (p.returncode, p.stdout)
    assert spec_command() in p.stderr, p.stderr
    # the constant is exactly that line, and the docs carry it word for word
    assert testenv.CLEAN_EXPORT_COMMAND == spec_command()
    for doc in ("run_all_tests.py", "README.md"):
        with open(os.path.join(HERE, doc), encoding="utf-8") as fh:
            assert spec_command() in fh.read(), "%s lacks the clean-export command" % doc
    print("t3 real processes exit 2 with the command; the docstring and README carry it OK")


if __name__ == "__main__":
    t1_runner_refuses_a_checkout_under_root_before_anything_runs()
    t2_isolate_refuses_before_creating_anything()
    t3_real_processes_exit_2_and_the_docs_carry_the_command()
    print("ALL ROOT-CHECKOUT TESTS PASS")
