#!/usr/bin/env python3
"""Stamp docs/index.html with a content hash, for the dashboard's update check.

    python execution/stamp_app.py           # show whether the stamp is current
    python execution/stamp_app.py --write   # bring it up to date

WHY THIS EXISTS. GitHub Pages serves the dashboard with Cache-Control:
max-age=600 and it is installed to the iPhone Home Screen, so a deploy can sit
unseen behind a cached copy while the owner looks straight at the old layout.
Neither of the page's own refreshes helps: "Refresh now" and pull-to-refresh
refetch the DATA, and the stale JavaScript that renders it keeps running. On
2026-09-20 that cost the owner twenty minutes hunting a change that was already
live on the server.

So the page compares the build stamp inside the document it is RUNNING against
the one the server is serving, and offers a reload when they differ.

WHY A HASH AND NOT A DATE. The check is only as good as the stamp, and a stamp
a human has to remember to bump is a stamp that goes stale. This one is derived
from the file's own bytes, and test_commands.py fails when it does not match -
so index.html cannot change without the stamp changing, and the update banner
cannot quietly stop working.

Deliberately NOT the ETag: GitHub Pages derives that from mtime and size, and
the site rebuilds hourly, so an unchanged page would raise the banner every
hour until the owner learned to ignore it.
"""
import hashlib
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(os.path.dirname(HERE), "docs", "index.html")
TAG_RE = re.compile(r'(<meta name="app-build" content=")([^"]*)(">)')


def compute(text):
    """The hash of the page with the stamp itself blanked.

    Blanked, not removed, so stamping cannot change the length and chase its
    own tail.
    """
    body = TAG_RE.sub(lambda m: m.group(1) + m.group(3), text)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def current(text):
    m = TAG_RE.search(text)
    return m.group(2) if m else None


def main():
    text = io.open(PAGE, encoding="utf-8", newline="").read()
    if not TAG_RE.search(text):
        print("no app-build meta in docs/index.html - the update check is gone")
        return 2
    want, have = compute(text), current(text)
    if want == have:
        print("stamp is current: %s" % want)
        return 0
    if "--write" not in sys.argv:
        print("stamp is STALE: page is %s, meta says %s" % (want, have))
        print("run: python execution/stamp_app.py --write")
        return 1
    io.open(PAGE, "w", encoding="utf-8", newline="").write(
        TAG_RE.sub(lambda m: m.group(1) + want + m.group(3), text, count=1))
    print("stamped %s -> %s" % (have, want))
    return 0


if __name__ == "__main__":
    sys.exit(main())
