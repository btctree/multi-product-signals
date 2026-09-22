#!/usr/bin/env python3
"""Push the VM checkout's state commit even when origin moved in between.

Both VM publishers - ib_bot.publish_state (the trading run, --publish-only and
the phone-command poller) and publish_web.py (hourly at :25) - commit
data/bot_state.json and its neighbours on top of whatever the last :25
`git fetch; git reset --hard origin/main` left, then push. Until 2026-09-21
that push was one try with no fetch and no rebase (board review 2026-09-21).
When origin moved in between - the operator deploying, the signals bot's daily
commit - the push was refused as non-fast-forward, the only trace was "state
publish 'push' skipped" in bot.log, and the next :25 reset threw the local
commit away. publish_web then rebuilds bot_state.json from origin's copy, which
never had that run's rows, so every PLACED, REJECTED and HALT row of the run
was gone for good: they only ever existed in the run's memory.

push_with_retry() pushes; when the push fails it replays our commit on top of
origin (`git pull --rebase --autostash origin main`, the same step
.github/workflows/daily.yml runs before its own push) and pushes again, up to
`attempts` pushes in all. A side effect worth knowing: that pull also brings
origin's newest code into the checkout, which the next :25 reset would have
done anyway. Nothing imports code after the push - it is the publishers' last
step.

It must NEVER leave the checkout mid-rebase. `git reset --hard origin/main`
(the :25 cron) does not end a rebase: HEAD stays detached, the rebase stays "in
progress", and every later plain `git push` fails with "You are not currently
on a branch" - every publish, every hour, until someone logs in. So:
  - a rebase that stops on a conflict is aborted at once and the push given up.
    `rebase --abort` puts the branch back on our own commit, exactly as before
    the pull. A conflict means the same data file was edited on GitHub by hand
    (the netliq_history flows, say), and merging those is a human's call; the
    commit stays local and the next :25 reset drops it, as it always did;
  - a pull cut off by its timeout is checked and aborted the same way;
  - a rebase found ALREADY in progress on entry was left by a publisher killed
    mid-pull (a reboot, the OOM killer). `rebase --quit` drops its bookkeeping
    and keeps HEAD, the index and the files - so the caller's fresh commit,
    made on that detached HEAD, survives - and `main` is put back on it.

The publishers can overlap (a long trading run and the :25 publish, or a phone
command), so the whole push-and-rebase runs under a file lock in the
checkout's .git directory - runlock.hold, the same primitive as the order
lock, on a file of its own. Only a helper holding it ever rebases, so a rebase
found under the lock is never another publisher's live one.

Never raises: publishing is best-effort and the caller carries on either way.
Python 3.9 compatible (publish_web.py runs under the VM's python3).
"""
import subprocess
from pathlib import Path

import runlock

BRANCH = "main"
# The identity every VM commit already carries. A rebase re-commits ours and an
# autostash commits the dirty files, and the VM has no git identity of its own
# (hence the -c on each publisher's commit), so without it the rebase fails.
IDENT = ["-c", "user.email=bot@vm", "-c", "user.name=ib-bot"]
LOCK_NAME = "mps-push.lock"                       # inside the checkout's .git


class _Failed:
    """Stands in for a CompletedProcess when git timed out or could not start."""

    def __init__(self, why):
        self.returncode, self.stdout, self.stderr = -1, "", why


def _git(repo, args, timeout):
    try:
        return subprocess.run(["git", "-C", str(repo)] + list(args),
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return _Failed("timed out after %ss" % timeout)   # run() killed it
    except OSError as e:
        return _Failed("git did not start: %s" % e)


def _why(r):
    """The one line of git's output worth a log line: the conflict, the refusal,
    the fatal error - else the first line. Trimmed like the old messages."""
    lines = [l.strip() for l in ((r.stderr or "") + "\n" + (r.stdout or "")).splitlines()
             if l.strip()]
    for key in ("CONFLICT", "[rejected]", "fatal:", "error:"):
        for l in lines:
            if key in l:
                return l[:120]
    return lines[0][:120] if lines else "exit %s" % r.returncode


def _git_dir(repo, timeout):
    r = _git(repo, ["rev-parse", "--git-dir"], timeout)
    out = (r.stdout or "").strip() if r.returncode == 0 else ""
    if not out:
        return repo / ".git"
    p = Path(out)
    return p if p.is_absolute() else repo / p


def _rebasing(gitdir):
    # rebase-merge: the merge backend (git >= 2.26 default); rebase-apply: the
    # older apply backend. Either one means "a rebase is in progress".
    return any((gitdir / d).exists() for d in ("rebase-merge", "rebase-apply"))


def _abort(repo, gitdir, log, timeout):
    r = _git(repo, IDENT + ["rebase", "--abort"], timeout)
    if _rebasing(gitdir):
        log("  !! rebase --abort failed (%s) - the next publish clears it; by hand: "
            "git -C %s rebase --abort" % (_why(r), repo))
        return False
    return True


def _clear_stranded(repo, gitdir, log, timeout):
    """A rebase left by a killed publisher: drop it, keep the caller's commit."""
    log("  !! a rebase was left in progress (a publisher killed mid-pull) - "
        "clearing it and keeping this commit")
    r = _git(repo, ["rebase", "--quit"], timeout)
    if _rebasing(gitdir):
        log("  !! rebase --quit failed (%s) - push skipped; by hand: "
            "git -C %s rebase --abort" % (_why(r), repo))
        return False
    if _git(repo, ["symbolic-ref", "-q", "HEAD"], timeout).returncode != 0:
        # A rebase runs on a detached HEAD and the caller committed there. Put
        # main back on that commit, or the plain push has no branch to push.
        r = _git(repo, ["checkout", "-B", BRANCH], timeout)
        if r.returncode != 0:
            log("  !! could not put %s back on this commit (%s) - push skipped"
                % (BRANCH, _why(r)))
            return False
    return True


def _push(repo, gitdir, log, attempts, timeout):
    if _rebasing(gitdir) and not _clear_stranded(repo, gitdir, log, timeout):
        return False
    for n in range(1, attempts + 1):
        r = _git(repo, ["push"], timeout)
        if r.returncode == 0:
            if n > 1:
                log("  pushed on attempt %d of %d, replayed onto origin/%s"
                    % (n, attempts, BRANCH))
            return True
        if n == attempts:
            log("  !! push failed %d times (%s) - the commit stays local and the "
                "next :25 reset drops it" % (attempts, _why(r)))
            return False
        log("  note: push refused, attempt %d of %d (%s) - replaying onto origin/%s"
            % (n, attempts, _why(r), BRANCH))
        p = _git(repo, IDENT + ["pull", "--rebase", "--autostash", "origin", BRANCH],
                 timeout)
        if _rebasing(gitdir):
            # stopped on a conflict, or cut off by the timeout mid-way: either
            # way it is ours, and it must not outlive this call
            _abort(repo, gitdir, log, timeout)
            log("  !! push given up: replaying onto origin/%s stopped (%s) - rebase "
                "aborted, checkout back on our own commit; the next :25 reset drops "
                "it" % (BRANCH, _why(p)))
            return False
        if p.returncode != 0:
            log("  note: pull --rebase failed (%s) - pushing again anyway" % _why(p))
    return False


def _lockable(path):
    try:
        with open(str(path), "a"):
            return True
    except OSError:
        return False


def push_with_retry(repo, log, attempts=3, timeout=60):
    """Push `repo`'s branch to origin. When the push fails, replay our commit(s)
    onto origin/main and push again - `attempts` pushes in all. Each git command
    keeps the caller's `timeout` (seconds).

    True once pushed. False when every attempt failed, the replay hit a real
    conflict (aborted: the checkout is back on our own commit, clean), or
    another publisher held the push lock too long - each logged via `log`.
    Never raises, and never returns with a rebase in progress."""
    try:
        repo = Path(repo)
        attempts = max(1, int(attempts))
        gitdir = _git_dir(repo, timeout)
        lock = gitdir / LOCK_NAME
        if not (gitdir.is_dir() and _lockable(lock)):
            # no .git to hold a lock in: push unlocked, as before 2026-09-21
            return _push(repo, gitdir, log, attempts, timeout)
        wait_s = 2 * timeout
        with runlock.hold("gitpush", wait_s=wait_s, path=str(lock)) as got:
            if not got:
                log("  !! push skipped: another publisher held the push lock for "
                    "%ss - the commit stays local" % wait_s)
                return False
            return _push(repo, gitdir, log, attempts, timeout)
    except Exception as e:
        try:
            log("  note: push skipped (%s)" % e)
        except Exception:
            pass                                  # not even a broken log may raise
        return False
