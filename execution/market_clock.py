#!/usr/bin/env python3
"""Market clock: when is a market's daily bar final enough to decide on?

Shared by ib_bot (python3.11, the trading runs) and daily_signal (the digest,
/usr/bin/python3 = 3.9). STDLIB ONLY and Python 3.9 syntax: no match statement,
no X | Y annotations, no datetime.UTC. daily_signal must never import ib_bot, so
the session table lives here and not there - one table, not two copies that
could drift (review 2026-09-17, "The /update digest still replays exits and
entries without market_decidable").

Why a market can be undecidable: every rule is close-evaluated ("All exits are
close-evaluated", README), but Yahoo fills TODAY's daily bar with the live price
while a market trades, and the product cards carry that bar. The weekday 09:00
UTC run read cards built at 07:35Z on 09-15 and 09-16 - EU about 35 minutes into
its session and HK before its closing auction - so a regime break or a trailing
stop could be decided on an opening dip the close then undid, the hw/stop
ratchet moved on intraday prints, and an EU market sell sent then fills at once,
mid-session.

The clock alone is not enough either (review 2026-09-17, "The settle check uses
the bot's own clock, not the time the card was built"): the cards the bot reads
at 09:00 can come from a build that started BEFORE Tokyo's close settled - on 12
of 14 weekdays from 08-28 to 09-14 the newest build was from ~04:45Z. So
last_settled_close() also gives the instant a build must have STARTED at or
after to hold that market's finished bar; ib_bot compares it with the engine's
"generated_at".

No holiday calendar, by the operator's choice: on a holiday a market is treated
as if it traded, which only defers a decision by one run.
"""
from datetime import datetime, timedelta, timezone

# Regular session per market, keyed by the Yahoo suffix ("" = US): the zone and
# the LOCAL open and close, Monday to Friday. Operator-approved 2026-09-17.
# .HK's close includes the closing auction (16:00-16:10). A symbol whose suffix
# is not listed here, and crypto (-USD), are always decidable - exactly as before
# the clock existed.
MARKET_SESSIONS = {
    "": ("America/New_York", (9, 30), (16, 0)),
    ".HK": ("Asia/Hong_Kong", (9, 30), (16, 10)),
    ".T": ("Asia/Tokyo", (9, 0), (15, 30)),
    ".DE": ("Europe/Berlin", (9, 0), (17, 30)),
    ".PA": ("Europe/Paris", (9, 0), (17, 30)),
    ".AS": ("Europe/Paris", (9, 0), (17, 30)),
    ".BR": ("Europe/Paris", (9, 0), (17, 30)),
    ".LS": ("Europe/Lisbon", (8, 0), (16, 30)),
    ".MC": ("Europe/Madrid", (9, 0), (17, 30)),
    ".MI": ("Europe/Rome", (9, 0), (17, 30)),
    ".SW": ("Europe/Zurich", (9, 0), (17, 30)),
    ".CO": ("Europe/Copenhagen", (9, 0), (17, 0)),
    ".ST": ("Europe/Stockholm", (9, 0), (17, 30)),
    ".OL": ("Europe/Oslo", (9, 0), (16, 20)),
    ".HE": ("Europe/Helsinki", (10, 0), (18, 30)),
    ".VI": ("Europe/Vienna", (9, 0), (17, 30)),
    ".L": ("Europe/London", (8, 0), (16, 30)),
}
# After the close the bar is still not the card's to decide on: the closing
# auction prints, Yahoo publishes late, and the signal build runs hourly at :05.
# 90 minutes covers all three.
SESSION_SETTLE_MIN = 90


def _session(ysym):
    """(suffix, row) for ysym's market; row is None for crypto and unknowns."""
    sym = str(ysym or "").strip().upper()
    if sym.endswith("-USD"):
        return "-USD", None
    suffix = "." + sym.rsplit(".", 1)[1] if "." in sym else ""
    return suffix, MARKET_SESSIONS.get(suffix)


def _as_utc(t):
    """An aware UTC datetime; a naive one is read as UTC."""
    if t.tzinfo is None:
        return t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def market_decidable(ysym, now_utc):
    """(decidable, reason): may ysym's market be judged on its card at now_utc?

    NOT decidable only on a local weekday with local time in
    [open, close + SESSION_SETTLE_MIN) - the one window in which the card's
    newest bar can still be moving. Before the open, the evening and the whole
    weekend are decidable: the newest bar is then the last finished close.

    Pure: no I/O, no clock of its own (the caller passes the instant). The
    reason says why in words fit for the log. Should the zone data itself be
    unreadable the answer is decidable - the behaviour before this check
    existed - with a reason starting "!!" so the caller can say so loudly.
    """
    suffix, row = _session(ysym)
    if suffix == "-USD":
        return True, "crypto trades around the clock"
    if row is None:
        return True, "no session table for %s" % suffix
    zone, (oh, om), (ch, cm) = row
    try:
        from zoneinfo import ZoneInfo
        local = _as_utc(now_utc).astimezone(ZoneInfo(zone))
    except Exception as e:
        return True, "!! no zone data for %s (%s); decided as before" % (zone, str(e)[:60])
    if local.weekday() >= 5:
        return True, "%s weekend" % zone
    t = local.hour * 3600 + local.minute * 60 + local.second + local.microsecond / 1e6
    opens = oh * 3600 + om * 60
    settled = (ch * 60 + cm + SESSION_SETTLE_MIN) * 60
    if opens <= t < settled:
        return False, ("%s session %02d:%02d-%02d:%02d is still open or settling "
                       "(local %s; its bar is final from %02d:%02d)"
                       % (zone, oh, om, ch, cm, local.strftime("%a %H:%M"),
                          settled // 3600, settled % 3600 // 60))
    return True, "%s outside %02d:%02d-%02d:%02d + %d min" % (zone, oh, om, ch, cm,
                                                             SESSION_SETTLE_MIN)


def last_settled_close(ysym, t):
    """The most recent local-weekday close + SESSION_SETTLE_MIN at or before t,
    as an aware UTC datetime.

    This is the newest bar a decision at t may rest on, so a signal build must
    have STARTED its price download at or after it to carry that bar finished.

    None - nothing to wait for - for crypto, for a suffix with no session row,
    and when the zone data is unreadable (market_decidable's "!!" case: decided
    as before). The settle instant is taken on the LOCAL wall clock, exactly as
    market_decidable measures it, so the two never disagree about a DST day.
    """
    suffix, row = _session(ysym)
    if row is None:
        return None
    zone, _opens, (ch, cm) = row
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(zone)
        t = _as_utc(t)
        local_day = t.astimezone(tz).date()
    except Exception:
        return None
    # A weekday close is at most three days back (Monday morning -> Friday);
    # eight covers any week.
    for back in range(8):
        d = local_day - timedelta(days=back)
        if d.weekday() >= 5:
            continue
        settle = (datetime(d.year, d.month, d.day, ch, cm, tzinfo=tz)
                  + timedelta(minutes=SESSION_SETTLE_MIN)).astimezone(timezone.utc)
        if settle <= t:
            return settle
    return None


def parse_generated_at(value):
    """The engine's "generated_at" ("YYYY-MM-DDTHH:MM:SSZ", UTC) as an aware UTC
    datetime, or None when it is missing or cannot be read.

    An explicit offset ("+00:00") is accepted too; a timestamp with NO zone is
    not - there is no telling which clock wrote it, and guessing one could pass
    a stale build as fresh."""
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        v = datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
    except ValueError:
        return None
    if v.tzinfo is None or v.utcoffset() is None:
        return None
    return v.astimezone(timezone.utc)
