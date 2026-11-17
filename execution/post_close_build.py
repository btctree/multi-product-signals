#!/usr/bin/env python3
"""Ask GitHub for a signals build once every market has closed - 22:40 UTC.

WHY (owner decision 2026-11-17, "the 22:40 update plus the alarm"). The 23:35
UTC trading run decides a market only on a signal build that STARTED after that
market's close + 90 minutes (market_clock.last_settled_close, ib_bot
_build_behind_close). For the US that is 22:30 UTC in winter (21:30 in US
summer). The signal build, .github/workflows/daily.yml, is scheduled hourly at
:05 but GitHub fires it only 3 to 6 times a day, and the VM's own state commits
carry [skip ci], so on most nights no build started between 22:30 and 23:35:
since 1 Nov only 3 of 11 nights had one. The 23:35 run then deferred every US
exit and entry to the 10:00 London catch-up, and the 23:40 digest said
"BUY: none" with the names under DECIDED AFTER THE CLOSE.

daily.yml also builds on EVERY push to main (no paths filter). So this program,
run by a VM cron line at 22:40 UTC Monday to Friday, pushes one tiny tracked
file - data/post_close_build.json - in a commit WITHOUT [skip ci]. GitHub then
starts a build within a minute or two, its price download starts after 22:30,
and the build's "generated_at" counts as post-close for every market: 22:30 to
00:00 UTC is the only window in which the US, Europe, Hong Kong and Japan are
all closed and settled (Tokyo opens at 00:00 UTC).

    {"requested_at": "2026-11-17T22:40:03Z", "for_close": "2026-11-17",
     "reason": "post-close signals build"}

for_close is the UTC date, which at 22:40 UTC is also the New York session
date. No market calendar, by the owner's choice: on a US holiday the request
still goes out and the build simply finds no new bar. In US summer the close
settles at 21:30 UTC, so 22:40 is 70 minutes late and the build is still
post-close; in winter it settles at 22:30 UTC.

WHAT IT GUARANTEES
  * Idempotent per UTC date: when origin/main already holds today's request
    it does nothing. Two copies at once (a hand run beside cron) are kept
    apart by a lock file in the checkout's .git directory.
  * Refuses on Saturday and Sunday (UTC): nothing closed that day. Refuses
    before today's US close has settled (market_clock.last_settled_close),
    too: such a build would not count as post-close, and a hand run in the
    afternoon must not use up the day's request and so stop the 22:40 one.
    A request on origin made before that settle does not count as done.
  * Commits ONLY that file (`git commit --only`), with the identity every VM
    commit carries (gitpush.IDENT). Other dirty or staged files in the
    checkout are left exactly as they were.
  * Pushes with gitpush.push_with_retry: the push lock, fetch/rebase on a
    refusal, never a rebase left in progress.
  * Never raises past main(): it logs and exits non-zero on a failure (1),
    0 when the request is out or was not needed.
  * --dry prints what it would write and commit, and changes nothing.

Python 3.9 compatible and standard library only, apart from gitpush, runlock
and market_clock (itself stdlib only, and the one copy of the session table) -
the VM runs it under /usr/bin/python3, which is 3.9.

Run:  /usr/bin/python3 post_close_build.py        request today's build
      /usr/bin/python3 post_close_build.py --dry  say what it would do
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import gitpush
import runlock
from market_clock import last_settled_close, parse_generated_at

# The checkout: execution/.. on the VM (/root/multi-product-signals). MPS_REPO
# overrides it, as for publish_web and daily_signal.
REPO = Path(os.environ.get("MPS_REPO") or str(Path(__file__).resolve().parent.parent))
REL = "data/post_close_build.json"
BRANCH = gitpush.BRANCH
REASON = "post-close signals build"
LOCK_NAME = "mps-post-close.lock"                  # inside the checkout's .git
# Strings GitHub reads in a commit message as "do not run push workflows".
# None may appear in ours - the whole point of the commit is to start a build.
SKIP_TOKENS = ("[skip ci]", "[ci skip]", "[no ci]", "[skip actions]",
               "[actions skip]", "skip-checks:")
# Another git command (a phone-command publish at :40, the :25 publisher still
# running) can hold the index lock for a moment: wait and try again.
LOCK_TRIES, LOCK_WAIT_S = 5, 3.0
GIT_TIMEOUT_S = 60


def log(msg):
    print("[post_close_build %s] %s"
          % (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"), msg), flush=True)


def stamp(t):
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def commit_message(day):
    """The request's commit message. It must start a build, so it carries none
    of SKIP_TOKENS - not even inside a sentence about them."""
    return ("signals: post-close build request for %s\n\n"
            "Asks GitHub Actions (.github/workflows/daily.yml, which builds on "
            "every push to main) for a signals build after every market has "
            "closed and settled, so the 23:35 UTC trading run reads a post-close "
            "build. Written by execution/post_close_build.py from the VM's "
            "22:40 UTC cron line." % day)


def request_doc(now):
    return {"requested_at": stamp(now), "for_close": now.strftime("%Y-%m-%d"),
            "reason": REASON}


def us_settle_today(now):
    """Today's US close + SESSION_SETTLE_MIN as aware UTC, from the shared
    market clock - 22:30 UTC in winter, 21:30 in US summer. None when the zone
    data is unreadable (then nothing is refused on it)."""
    end = now.astimezone(timezone.utc).replace(hour=23, minute=59, second=59,
                                               microsecond=0)
    s = last_settled_close("", end)
    if s is None or s.strftime("%Y-%m-%d") != end.strftime("%Y-%m-%d"):
        return None
    return s


def _origin_has(git, day, settle):
    """(True, requested_at) when origin/main already holds `day`'s request,
    made at or after `settle` (when known)."""
    r = git(["show", "%s/%s:%s" % ("origin", BRANCH, REL)])
    if r.returncode != 0:
        return False, None                     # no such file on origin yet
    try:
        doc = json.loads(r.stdout or "")
    except ValueError:
        return False, None
    if not isinstance(doc, dict) or doc.get("for_close") != day:
        return False, None
    at = parse_generated_at(doc.get("requested_at"))
    if settle is not None and (at is None or at < settle):
        return False, None                     # an early request is not the one
    return True, doc.get("requested_at")


def _git_retrying(git, args, sleep):
    """git(args), retried while another git process holds the index lock."""
    r = None
    for n in range(1, LOCK_TRIES + 1):
        r = git(args)
        text = (r.stderr or "") + (r.stdout or "")
        if r.returncode == 0 or ".lock" not in text or n == LOCK_TRIES:
            return r
        log("note: git %s waited on a lock (attempt %d of %d) - %s"
            % (args[0] if args[0] != "-c" else "commit", n, LOCK_TRIES,
               gitpush._why(r)))
        sleep(LOCK_WAIT_S)
    return r


def _write_atomic(path, doc):
    """tmp + os.replace. The tmp name matches .gitignore's data/*.json.tmp*,
    so one left by a kill never shows up as untracked."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("%s.tmp.%d" % (path.name, os.getpid()))
    try:
        with open(str(tmp), "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(doc, indent=1) + "\n")
        os.replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def request_build(repo, now, dry=False, git=None, push=None, sleep=time.sleep):
    """The whole job, minus the lock. Returns the exit code: 0 = requested, or
    nothing to do (weekend, already on origin, --dry); 1 = failed.

    `git(args)` runs one git command in `repo` and returns an object with
    returncode/stdout/stderr (gitpush._git by default); `push()` pushes and
    returns True/False (gitpush.push_with_retry by default). Tests stub both."""
    repo = Path(repo)
    if git is None:
        def git(args):
            return gitpush._git(repo, args, GIT_TIMEOUT_S)
    if push is None:
        def push():
            return gitpush.push_with_retry(repo, log, attempts=3, timeout=90)
    now = now.astimezone(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    if now.weekday() >= 5:
        log("refused: %s is a %s (UTC) - no market closed today, no build requested"
            % (day, now.strftime("%A")))
        return 0
    settle = us_settle_today(now)
    if settle is not None and now < settle:
        log("refused: too early - today's US close settles at %s UTC; a build "
            "started now would not count as post-close, so nothing is requested"
            % settle.strftime("%H:%M"))
        return 0

    b = git(["symbolic-ref", "--short", "-q", "HEAD"])
    branch = (b.stdout or "").strip() if b.returncode == 0 else ""
    if branch and branch != BRANCH:
        log("!! FAILED: the checkout is on branch %r, not %s - nothing committed"
            % (branch, BRANCH))
        return 1

    f = git(["fetch", "origin", BRANCH, "-q"])
    if f.returncode != 0:
        log("note: git fetch failed (%s) - judging 'already requested' on the "
            "last fetched origin/%s" % (gitpush._why(f), BRANCH))
    done, at = _origin_has(git, day, settle)
    if done:
        log("already requested for %s (at %s, on origin/%s) - nothing to do"
            % (day, at, BRANCH))
        return 0

    doc = request_doc(now)
    msg = commit_message(day)
    if any(t in msg.lower() for t in SKIP_TOKENS):          # cannot happen; must not
        log("!! FAILED: the commit message would carry a skip-ci token - not committed")
        return 1
    if dry:
        log("--dry: would write %s = %s, commit only that file as %r and push it "
            "to origin/%s - nothing written, committed or pushed"
            % (REL, json.dumps(doc), msg.splitlines()[0], BRANCH))
        return 0

    _write_atomic(repo / REL, doc)
    a = _git_retrying(git, ["add", "--", REL], sleep)
    if a.returncode != 0:
        log("!! FAILED: git add %s (%s)" % (REL, gitpush._why(a)))
        return 1
    # --only with a path: the commit holds that path alone, whatever else is
    # staged; everything else in the index and the work tree stays as it was.
    c = _git_retrying(git, gitpush.IDENT + ["commit", "--only", "-q", "-m", msg,
                                            "--", REL], sleep)
    if c.returncode != 0:
        log("!! FAILED: git commit (%s)" % gitpush._why(c))
        return 1
    s = git(["show", "--name-only", "--format=", "HEAD"])
    files = [l.strip() for l in (s.stdout or "").splitlines() if l.strip()]
    if files != [REL]:
        # Belt and braces: --only cannot do this. Undo our commit, keep the
        # files and the index as they were, push nothing.
        git(["reset", "--soft", "HEAD~1"])
        log("!! FAILED: the commit held %r, not just %s - undone, nothing pushed"
            % (files, REL))
        return 1
    if not push():
        log("!! FAILED: push refused - the request stays local and the next :25 "
            "reset drops it; the 23:35 run alarms if no post-close build arrives")
        return 1
    h = git(["log", "-1", "--format=%s", "HEAD"])
    if (h.stdout or "").strip() != msg.splitlines()[0]:
        # Another publisher committed on top before the push: GitHub reads only
        # the newest commit of a push for [skip ci], so its tag may have
        # swallowed this trigger. Said, not retried - the alarm is the backstop.
        log("note: HEAD after the push is %r, not this request - if it carries "
            "[skip ci] GitHub may not build" % (h.stdout or "").strip()[:80])
    log("requested: %s pushed for %s (%s) - GitHub builds on this push"
        % (REL, day, doc["requested_at"]))
    return 0


def main(argv=None, now=None, repo=None):
    """Exit code: 0 requested or nothing to do, 1 failed. Never raises."""
    try:
        argv = sys.argv[1:] if argv is None else list(argv)
        unknown = [a for a in argv if a != "--dry"]
        if unknown:
            log("!! FAILED: unknown argument(s) %s - usage: post_close_build.py [--dry]"
                % " ".join(unknown))
            return 1
        repo = Path(repo or REPO)
        now = now or datetime.now(timezone.utc)
        gitdir = repo / ".git"
        lock = gitdir / LOCK_NAME if gitdir.is_dir() else None
        if lock is None:
            return request_build(repo, now, dry="--dry" in argv)
        with runlock.hold("post_close_build", wait_s=0, path=str(lock)) as got:
            if not got:
                log("another post_close_build is running - this one does nothing")
                return 0
            return request_build(repo, now, dry="--dry" in argv)
    except Exception as e:
        try:
            log("!! FAILED: %r" % (e,))
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
