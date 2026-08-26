#!/usr/bin/env python3
"""On-demand Telegram command handler for the Multi Product signal bot.

Polls getUpdates and answers /update (or the word "update") with the same
net-worth / positions / P&L report the daily digest sends. READ-ONLY: it never
places an order and never moves the P&L baseline (only the scheduled 23:40 run
does that), so the "since last digest" figure keeps measuring from the last
scheduled run rather than resetting on every tap.

SECURITY: the bot username is public, so anyone can message it. This replies
ONLY to the configured TELEGRAM_CHAT_ID; every other chat is logged and ignored.

Runs from cron every couple of minutes. getUpdates offsets are persisted so a
command is answered exactly once; a crash mid-handle re-answers at worst once.

Config: /root/telegram.env (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID), mode 600.
State:  /root/telegram_offset.json (last processed update_id).
"""
import json
import os
import sys
import time
import datetime
import urllib.request
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import daily_signal as ds  # noqa: E402  shared report builder + telegram helpers

OFFSET_FILE = os.environ.get("MPS_TG_OFFSET", "/root/telegram_offset.json")
LOCK = os.environ.get("MPS_TG_LOCK", "/tmp/mps_tg_poll.lock")


def log(*a):
    print(datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), *a,
          file=sys.stderr)


def get_updates(token, offset):
    url = ("https://api.telegram.org/bot%s/getUpdates?timeout=0&offset=%d"
           "&allowed_updates=%s" % (token, offset, urllib.parse.quote('["message"]')))
    req = urllib.request.Request(url, headers={"User-Agent": "mps-tg-poll"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    if not d.get("ok"):
        raise RuntimeError("getUpdates: %s" % json.dumps(d)[:200])
    return d["result"]


def load_offset():
    try:
        return int(json.load(open(OFFSET_FILE, encoding="utf-8")).get("offset", 0))
    except Exception:
        return 0


def save_offset(off):
    tmp = OFFSET_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"offset": off}, f)
    os.replace(tmp, OFFSET_FILE)


def is_command(text):
    if not text:
        return False
    t = text.strip().lower().lstrip("/")
    t = t.split("@", 1)[0]                      # /update@BotName -> update
    return t in ("update", "u", "status", "refresh", "start")


def main():
    # single-instance guard: cron every 2 min must not overlap a slow run
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(LOCK) < 300:
                return 0                         # a recent run holds the lock
        except OSError:
            pass
        os.remove(LOCK)                          # stale lock, reclaim
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            return 0
    try:
        cfg = ds.load_cfg()
        token = cfg["TELEGRAM_TOKEN"]
        owner = str(cfg["TELEGRAM_CHAT_ID"])
        offset = load_offset()
        updates = get_updates(token, offset)
        if not updates:
            return 0

        max_id = offset - 1
        want_report = False
        for u in updates:
            max_id = max(max_id, u["update_id"])
            m = u.get("message") or {}
            chat = str((m.get("chat") or {}).get("id", ""))
            text = m.get("text", "")
            if chat != owner:
                log("ignored message from chat %s: %r" % (chat, text[:40]))
                continue
            if is_command(text):
                want_report = True               # collapse a burst into ONE reply

        # advance the offset FIRST so a send failure cannot loop the same command
        save_offset(max_id + 1)

        if want_report:
            msg, _snap = ds.build_report(on_demand=True)   # note: no save_snapshot
            mid = ds.send_message(token, owner, msg)
            log("answered /update -> message_id %s" % mid)
        return 0
    finally:
        try:
            os.remove(LOCK)
        except OSError:
            pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log("UNHANDLED: %r" % e)
        sys.exit(1)
