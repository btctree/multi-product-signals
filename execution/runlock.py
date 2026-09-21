#!/usr/bin/env python3
"""One lock for everything on the VM that can send an order.

The trading run (ib_bot, 23:35 and 09:00 UTC) and the phone-command poller
(ib_commands, every 10 minutes) both place SELLs on the same account, and
cron starts them in the same minute. Nothing serialized them, so a phone SELL
and the bot's own exit for the same holding could both be sent - each reading
the book before the other's order existed. Board review 2026-09-21.

Usage - the whole critical section runs under one exclusive lock:

    with runlock.hold("ib_commands", wait_s=0) as got:
        if not got:
            log("trading run in progress - commands wait for the next poll")
            return
        ...

    with runlock.hold("ib_bot", wait_s=600) as got:
        if not got:
            raise SystemExit("could not take the run lock")
        ...

wait_s=0 means "try once": the poller skips a turn rather than queueing behind
a trading run - its commands are not marked done, so the next poll picks them
up. The trading run waits, because it must not be skipped.

The lock is an OS file lock (fcntl on the VM; msvcrt on a Windows checkout,
which is where the tests run). It is released automatically if the process
dies, so a crashed run can never leave the account locked.
"""
import contextlib
import os
import time

LOCK_FILE = os.environ.get("MPS_LOCK_FILE", "/root/mps_run.lock")

try:
    import fcntl                                   # the VM

    def _try(fh):
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, OSError):
            return False

    def _release(fh):
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
except ImportError:                                 # a Windows checkout
    import msvcrt

    def _try(fh):
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _release(fh):
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


@contextlib.contextmanager
def hold(who, wait_s=0, poll_s=2.0, path=None):
    """Yield True while holding the lock, False if it could not be taken.

    Never raises for contention: the caller decides whether to skip or abort.
    """
    path = path or LOCK_FILE
    fh = open(path, "a+")
    got = False
    try:
        deadline = time.monotonic() + max(0.0, float(wait_s))
        while True:
            got = _try(fh)
            if got or time.monotonic() >= deadline:
                break
            time.sleep(poll_s)
        if got:
            try:
                fh.seek(0)
                fh.truncate()
                fh.write("%s %d %s\n" % (who, os.getpid(),
                                         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
                fh.flush()
            except OSError:
                pass                                 # the lock is what matters
        yield got
    finally:
        if got:
            _release(fh)
        fh.close()
