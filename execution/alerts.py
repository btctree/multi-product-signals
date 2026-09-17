#!/usr/bin/env python3
"""Operator alerts: a spool on disk, delivered to Telegram by telegram_poll.

WHY THIS EXISTS
A refused order was visible in exactly one place: a REJECTED row on the
dashboard, and only after a push landed and someone reloaded the page. Nobody
noticed. BEN's trailing-stop exit was refused five times over ~35 h (2026-09-01
23:35 to 09-03 10:54) and only went through on a manual rerun; XYZ's was refused
twice and then lapsed silently when its rule stopped firing - 22 shares still
held on 09-16 with the stop long gone. SNOW, PANW and MC went the same way.

WHY A SPOOL AND NOT A SEND
Only the process that placed an order sees IB's verdict, so DETECTION has to
live in ib_bot (exits) and ib_commands (phone sells). DELIVERY must not: a
Telegram call inside a trading run can hang for 30 s before the entries go out,
IB's refusal text carries HTML (<h4>Market Order Confirmation</h4>) that makes a
parse_mode=HTML send fail outright, and in ib_commands anything that raises
between placeOrder and the DONE save re-executes the SELL ten minutes later. So
the placing process only drops a small file here, and telegram_poll - which
already runs every ~2 min, holds the credentials and a lock, and never touches
IB - drains it. A send that fails leaves the files for the next tick.

THE CONTRACT
  * enqueue() NEVER raises. It writes one JSON file per alert (tmp + os.replace,
    so a reader never sees half a file) under a deterministic name built from
    the key, which makes queuing the same alert twice harmless and lets two
    writers (the 23:35 run and a 23:40 ib_commands poll) run at once.
  * Alert text is PLAIN. drain() html.escape()s all of it, so no caller can
    break delivery by passing IB's own markup through.
  * Stdlib only: telegram_poll imports this under /usr/bin/python3, which has no
    broker library, and it must never pull in ib_bot.

Spool: /root/alert_outbox, or MPS_ALERT_DIR. Tests repoint the DIR attribute.
"""
import html
import json
import os
import re
import sys
import time
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path

# Overridable so tests never touch /root. Read at CALL time, never captured.
DIR = Path(os.environ.get("MPS_ALERT_DIR", "/root/alert_outbox"))

# Only files named a-*.json are alerts. The registry and the episode book live in
# the same directory under other names, so drain() can never send them.
_PREFIX = "a-"
ONCE_NAME = "once_registry.json"
EPISODES_NAME = "exit_episodes.json"
# once=True keys older than this are forgotten. ib_commands drops an issue at
# MAX_AGE_H = 48, so nothing can ask about a key this old again.
ONCE_KEEP_S = 14 * 86400


def log(*a):
    # stderr: telegram_poll's own log stream; ib_bot's cron line merges it (2>&1)
    print("[alerts]", *a, file=sys.stderr, flush=True)


def _now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _path_for(key):
    """Deterministic file name for a key. The readable part is sanitised for
    the filesystem; the checksum keeps two keys that sanitise alike ("cmd a/b"
    and "cmd_a_b") from overwriting each other. crc32, not hashlib: a
    FIPS-hardened host can refuse a hashlib algorithm at call time, and that
    would fail every enqueue - silently, by design of enqueue."""
    k = str(key)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", k)[:60]
    digest = "%08x" % (zlib.crc32(k.encode("utf-8")) & 0xffffffff)
    return DIR / f"{_PREFIX}{safe}-{digest}.json"


def _atomic_write(path, obj):
    """Write JSON so a reader sees the old file or the new one, never a torn one.
    The tmp name is unique per writer, so two processes writing the same key at
    once cannot interleave into one tmp file either."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, type(default)) else default
    except Exception:
        return default


def enqueue(key, text, once=False):
    """Queue one alert. True if a file was written, False otherwise. NEVER raises.

    once=True: skip a key already queued or already delivered. ib_commands'
    exception path retries the same issue every 10 minutes for up to 48 h, and
    the operator needs to hear about it once, not 288 times. The registry is a
    read-modify-write without a lock - its only writer is ib_commands, which
    runs alone - so the worst a race could do is send one alert twice.
    """
    try:
        path = _path_for(key)
        DIR.mkdir(parents=True, exist_ok=True)
        reg = None
        if once:
            reg = _read_json(DIR / ONCE_NAME, {})
            if str(key) in reg or path.exists():
                return False
        _atomic_write(path, {"key": str(key), "text": str(text),
                             "created": time.time(), "id": uuid.uuid4().hex})
        if once:
            now = time.time()
            reg = {k: v for k, v in reg.items()
                   if isinstance(v, (int, float)) and now - v < ONCE_KEEP_S}
            reg[str(key)] = now
            try:
                _atomic_write(DIR / ONCE_NAME, reg)
            except Exception as e:
                # The alert is queued, which is what matters; until it is
                # delivered path.exists() still suppresses a repeat.
                log(f"once-registry not updated for {key} ({str(e)[:80]})")
        return True
    except Exception as e:
        try:
            log(f"could not queue alert {key!r} ({str(e)[:120]})")
        except Exception:
            pass
        return False


# ---------------- refused exits: one alert per episode ----------------
# The bot re-sends a refused exit at every trading run while its rule still
# fires, and a permanent refusal (BEN) refuses every time. A full alert per run
# would train the operator to ignore them, so a symbol gets ONE full alert when
# its first refusal opens an episode, then at most one short line per later run,
# until the position is gone.

def exit_refused(ysym, qty, reason, err, run_stamp):
    """Alert on an exit IB refused. True if an alert was queued. NEVER raises.

    run_stamp identifies the trading run, so a second refusal of the same
    symbol inside one run (there is none today) cannot add a second line.
    """
    try:
        book_path = DIR / EPISODES_NAME
        book = _read_json(book_path, {})
        ep = book.get(ysym)
        if not isinstance(ep, dict):
            ep = None
        if ep and ep.get("last_run") == run_stamp:
            return False
        if ep is None:
            ep = {"first": _now_utc(), "attempts": 1}
            text = (f"⚠️ EXIT REFUSED by IB: SELL {qty} {ysym}\n"
                    f"Exit rule: {reason}\n"
                    f"IB said: {err or '(no message)'}\n"
                    f"Nothing retries it before the next trading run, and that "
                    f"run re-sends it only if the exit rule still fires. "
                    f"Check the position.")
        else:
            ep["attempts"] = int(ep.get("attempts") or 1) + 1
            text = (f"⚠️ {ysym} exit still refused - attempt {ep['attempts']} "
                    f"since {ep.get('first') or '?'}")
        ep.update(last_run=run_stamp, qty=qty, reason=reason,
                  last_err=str(err or "")[:300])
        # Queue BEFORE recording: if the queue fails, the next refusal must
        # still find no episode and send the full alert, not a short line
        # pointing at an alert that never went out.
        queued = enqueue(f"exit-refused-{ysym}-{run_stamp}", text)
        if queued:
            book[ysym] = ep
            try:
                _atomic_write(book_path, book)
            except Exception as e:
                # The alert is out; the cost is that the next refusal repeats
                # the full text instead of a short line. Errs towards telling.
                log(f"episode for {ysym} not recorded ({str(e)[:80]})")
        return queued
    except Exception as e:
        try:
            log(f"exit-refused alert for {ysym} failed ({str(e)[:120]})")
        except Exception:
            pass
        return False


def clear_episodes(held_ysyms):
    """Close the episode of every symbol no longer held. NEVER raises.

    A position that is gone - sold at last, by the bot or by hand - has nothing
    left to refuse; a later re-entry that is refused is a NEW story and gets a
    full alert again."""
    try:
        book_path = DIR / EPISODES_NAME
        if not book_path.exists():
            return
        book = _read_json(book_path, {})
        held = set(held_ysyms or ())
        keep = {k: v for k, v in book.items() if k in held}
        if len(keep) != len(book):
            _atomic_write(book_path, keep)
    except Exception as e:
        try:
            log(f"episode cleanup skipped ({str(e)[:120]})")
        except Exception:
            pass


# ---------------- delivery (telegram_poll only) ----------------

def _fit(text, limit):
    """html-escaped text no longer than `limit`. Cuts the PLAIN text and escapes
    again, so a cut can never land inside an entity (&am...), which Telegram
    would refuse to parse - and a refused send keeps the alert queued forever."""
    esc = html.escape(str(text))
    if len(esc) <= limit:
        return esc
    suffix = " ..."
    t = str(text)[:max(0, limit - len(suffix))]
    while t and len(html.escape(t)) + len(suffix) > limit:
        excess = len(html.escape(t)) + len(suffix) - limit
        t = t[:len(t) - max(1, -(-excess // 6))]     # an escape is at most 6 chars
    return html.escape(t) + suffix


def _queued():
    """Queued alerts, oldest first. An unreadable file is renamed aside (.bad)
    rather than retried every two minutes forever."""
    out = []
    if not DIR.is_dir():
        return out
    for p in DIR.glob(_PREFIX + "*.json"):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict) or "text" not in d:
                raise ValueError("not an alert")
            created = float(d.get("created") or 0)
            # Everything that can fail on ONE file fails inside this try: a
            # single malformed file must be set aside, never block the queue.
        except FileNotFoundError:
            continue                                  # delivered by a racing drain
        except Exception as e:
            log(f"unreadable alert {p.name} set aside ({str(e)[:80]})")
            try:
                os.replace(p, p.with_suffix(".bad"))
            except Exception:
                pass
            continue
        out.append((created, p.name, p, d))
    out.sort(key=lambda r: (r[0], r[1]))
    return [(p, d) for _, _, p, d in out]


def _remove_if_same(path, d):
    """Delete a delivered alert, unless it was rewritten since we read it - a
    re-queued key carries a new id, and that version has not been sent."""
    try:
        cur = _read_json(path, {})
        if cur.get("id") == d.get("id"):
            path.unlink()
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"delivered alert {path.name} not removed ({str(e)[:80]}) - "
            f"it may be sent again")


def drain(send_fn, max_chars=3800):
    """Send queued alerts through send_fn(text); returns how many were delivered.

    NEVER raises. Alerts are joined into as few messages as fit under max_chars
    (Telegram's cap is 4096), oldest first. A message's files are deleted only
    after send_fn returns without raising; on the first failure the rest stay
    queued, in order, for the next tick.
    """
    try:
        items = _queued()
    except Exception as e:
        log(f"alert spool unreadable ({str(e)[:120]})")
        return 0
    batches, cur, cur_len = [], [], 0
    for p, d in items:
        body = _fit(d.get("text", ""), max_chars)
        if cur and cur_len + 2 + len(body) > max_chars:
            batches.append(cur)
            cur, cur_len = [], 0
        cur_len += (2 if cur else 0) + len(body)
        cur.append((p, d, body))
    if cur:
        batches.append(cur)
    delivered = 0
    for i, batch in enumerate(batches):
        try:
            send_fn("\n\n".join(body for _, _, body in batch))
        except Exception as e:
            left = sum(len(b) for b in batches[i:])
            log(f"alert delivery failed - {left} alert(s) kept for the next "
                f"tick ({str(e)[:160]})")
            break
        for p, d, _ in batch:
            _remove_if_same(p, d)
            delivered += 1
    return delivered
