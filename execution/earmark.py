#!/usr/bin/env python3
"""The earmark: base-currency cash that is NOT trading capital.

One number, in the base currency, set from the phone (ib_commands' EARMARK
command) or by hand, and read by everything that reports the investable pool or
sizes against it: ib_bot's net_liq and publisher, publish_web, and the Telegram
digest. This module exists so those four agree BY CONSTRUCTION - they each
carried their own copy of the arithmetic, and the copies had already drifted.

The operator's routine is a GBP deposit at each month end, converted to HKD in
the account and withdrawn days later. That money is not investable, so it must
not inflate NetLiq: position sizing is NetLiq/15 and the 8% kill switch measures
drawdown against it, on a peak that is monotonic and cash-flow-naive.

THE RULE, with H = HKD cash held, M = marker, P = the bot's own HKD pocket:

    stamping confirmed:      exclusion = min(M, max(0, H - clamp(P, 0, H)))
    otherwise (fallback):    exclusion = min(M, max(0, H))       - effective()

The cap is what stops a forgotten marker understating NetLiq forever. It is not
hypothetical: on 2026-08-31 an 18,559 marker outlived its withdrawal, NetLiq read
191,875 instead of 210,434, and the kill switch tripped on a drawdown that never
happened.

WHY THE POCKET EXISTS
Since 2026-09-12 the HKD balance has two sources: the operator's pass-through
money and HKD the bot buys to fund a SEHK entry. The plain cap cannot tell them
apart, so whenever M exceeded the operator's own HKD (marked before the GBP
converted, or left set after the withdrawal) it reached into the bot's funding:
NetLiq understated by that much, and the spend guard read the bot's own HKD as
earmarked and converted ANOTHER ~14,500 of USD into HKD, which may never be sold
back.

Four earlier designs tried to close that and each introduced a worse bug:
  (1) the cap taken against the raw balance, then _FX_COMMITTED subtracted again:
      spendable went negative and ensure_ccy converted need PLUS committed;
  (2) a down-only ratchet latched on the 7 HKD sampled before the GBP converted;
  (3) arming once the bot's own funding covered the marker floored the exclusion
      on the bot's money;
  (4) measuring the bot's HKD from the fills ledger since the marker date counted
      the operator's own GBP->HKD conversion as the bot's - the same row shape.
The pocket avoids all four. P is built ONLY from executions IB returns with the
bot's own client order id (order_ref "mps-...", which every bot order already
sends as cOID - ib_orders.make_coid), so the operator's conversion is excluded by
POSITIVE identification rather than inferred from its shape (4). No balance-
derived value is stored or ratcheted (2, 3): E is recomputed from M, the live H
and immutable executions. P comes out of H once, and the spend guard subtracts
committed once (1).

    stamped ("mps-") rows      count fully, HKD in and HKD out
    unstamped HKD-OUT rows     are charged to P (IB fee sweeps, FXCONV)
    unstamped HKD-IN rows      are the operator's pot and add nothing

Unidentified HKD therefore counts as pot: the exclusion errs toward
over-excluding, which only understates NetLiq and is fixed by clearing the
marker, never toward spending the transfer money.

WHEN THE POCKET IS NOT USED (bot_pocket returns None -> the fallback rule, which
is exactly what ran this account before):
  - no anchor yet. The anchor is a UTC minute stamp written ONLY by a live ib_bot
    run that sees H < 1 at its start - the one moment neither a pot nor a pocket
    can exist. Rows before it are ignored, so pocket drift (levies charged outside
    the fill commission, interest, dividends) is reset every time H reaches 0;
  - stamping not confirmed: no row anywhere has an "mps-" order_ref, so IB has
    never been seen echoing the stamp and "unstamped" means nothing yet;
  - the canary: a row that DOES carry an order_ref key which is not "mps-", on an
    order_id the bot itself recorded as submitted in ib_orders' orders ledger.
    That is proof IB dropped the stamp on a bot fill, and every number built on
    the stamp is suspect;
  - a row after the anchor whose HKD side cannot be priced (currency guessed);
  - a COVERAGE GAP (board review 2026-09-17, "The pocket never checks for gaps
    in coverage"). IB's trades read reaches back 7 days, and the git fills
    ledger only grows at the end of a finished run - and loses unpushed rows to
    publish_web's hourly git reset. An SEHK buy that filled while the gateway
    was down for 7+ days was in neither, while the stamped conversion that paid
    for it was: P too HIGH by the buy, the direction that spends the transfer
    money. So every live run keeps what IB returned in EXECS_FILE on the VM and
    stamps COVERED_FILE once the read is known complete; an anchor older than
    COVERAGE_MAX_DAYS is only trusted while that stamp is younger than it
    (coverage_gap). A gap deletes the anchor - the pocket re-anchors at the next
    live run that sees H < 1 - and an EMPTY read while IB accepted a bot order
    in that window is not trusted either.

THE POCKET FILE is how the hourly publishers, the digest, ib_commands and
publish_only (none of which sweep executions) see P. A live ib_bot run writes P
at the end of the run plus `pending` - HKD committed to bot HK BUY orders still
working or placed that run - and the publishers use max(0, P - pending) while the
file is younger than POCKET_MAX_AGE_H. Until the next run re-sweeps, a working
bot buy is therefore subtracted before it fills and the gap reads as pot, and so
does a bot sale or a bot conversion that fills after the run: the file lags
toward OVER-excluding, which is the safe direction. A missing, unconfirmed or
stale file falls back to the plain cap. A live run DELETES the file right after
its sweep, before any order can be sent, so a run that dies after an HK buy
(exception, Ctrl-C, SIGKILL, reboot) leaves no file claiming pending 0 - the
publishers fall back until a run finishes and writes it again.

THE ONE UNSAFE RESIDUE: HKD that leaves without an execution the pocket can see
(fees, levies or stamp duty booked outside the fill commission, interest) lowers
H but not P, so the pot is under-excluded by that amount - tens of HKD, reset
whenever the anchor moves. Between runs an unstamped sweep does the same until
the next run charges it.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Overridable so tests never touch /root.
DIR = Path(os.environ.get("MPS_EARMARK_DIR", "/root"))
MARKER_FILE = DIR / "excluded_cash"
# The pocket's start line (UTC minute, the fills ledger's own ts format) and the
# last live run's pocket, for the processes that cannot sweep executions.
ANCHOR_FILE = DIR / "earmark_anchor"
POCKET_FILE = DIR / "earmark_pocket.json"
# Coverage (see the module docstring): every execution a live run read from IB,
# kept on the VM, and the UTC second of the last read known to be complete.
EXECS_FILE = DIR / "earmark_execs.jsonl"
COVERED_FILE = DIR / "earmark_covered"
# IB's trades window is 7 days; a day's margin, because whether "days=7" means
# 168 hours or calendar days is not documented.
COVERAGE_MAX_DAYS = 6.0
# The cache keeps IB's window plus a day (the partial-read check reads rows up
# to 5 days old), and never drops a row at or after the anchor.
EXECS_KEEP_DAYS = 8.0
ANCHOR_MARGIN_DAYS = 1.0
AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# A run every ~12h (23:35 and 09:00 UTC) refreshes the file; 36h survives one
# missed run and no more, so a dead bot cannot keep a stale pocket alive.
POCKET_MAX_AGE_H = 36.0
# The bot's client order id prefix - ib_orders.make_coid. Not imported from
# there: publish_web imports this module and must never import ib_orders.
STAMP = "mps-"
TS_FORMAT = "%Y-%m-%d %H:%M"          # fills_capture's `ts`, UTC


def marker():
    """The raw number the operator set, or 0.0 if nothing is marked.

    EXCLUDED_CASH wins over the file so a one-off run can override it without
    touching the operator's state.
    """
    raw = os.environ.get("EXCLUDED_CASH")
    if raw is None:
        try:
            raw = MARKER_FILE.read_text().strip()
        except Exception:
            return 0.0
    try:
        v = float(str(raw).strip() or 0)
    except (TypeError, ValueError):
        return 0.0
    if v != v or v in (float("inf"), float("-inf")) or v <= 0:
        return 0.0                                   # NaN, inf, 0, negative: nothing marked
    return v


def effective(base_cash, persist=None):
    """The FALLBACK rule: min(marker, base cash held).

    Used whenever the bot's pocket is not known (see the module docstring).
    `persist` is accepted and ignored - it exists so callers written against an
    earlier, stateful version keep working.
    """
    m = marker()
    if m <= 0:
        return 0.0
    return min(m, max(0.0, float(base_cash or 0.0)))


def bot_share(base_cash, pocket):
    """The part of the base cash held that is the bot's own: clamp(P, 0, H).

    None when the pocket is unknown. This is the figure published as
    earmark_bot_hkd - the P actually used, not the raw ledger sum, which can
    exceed H (drift) or go negative (an unstamped sweep charged to it)."""
    if pocket is None:
        return None
    held = max(0.0, float(base_cash or 0.0))
    try:
        p = float(pocket)
    except (TypeError, ValueError):
        return None
    if p != p:
        return None
    return min(max(0.0, p), held)


def exclusion(base_cash, pocket=None):
    """How much to exclude: min(M, max(0, H - clamp(P, 0, H))).

    With pocket None this is exactly effective(): the rule this account ran
    before stamping existed, and still the answer whenever P is not known."""
    own = bot_share(base_cash, pocket)
    if own is None:
        return effective(base_cash)
    m = marker()
    if m <= 0:
        return 0.0
    held = max(0.0, float(base_cash or 0.0))
    return min(m, max(0.0, held - own))


def set_marker(amount, today=None):
    """Set the marker. Returns what was written. `today` is accepted and ignored."""
    try:
        amt = max(0.0, float(amount or 0))
    except (TypeError, ValueError):
        amt = 0.0
    if amt != amt or amt in (float("inf"), float("-inf")):
        amt = 0.0
    MARKER_FILE.write_text("%.2f\n" % amt)
    return amt


# ---------------------------------------------------------------- pocket ----
def _atomic_write(path, text):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def utc_minute(now=None):
    return (now or datetime.now(timezone.utc)).strftime(TS_FORMAT)


def read_anchor():
    """The anchor minute stamp, or None when there is none (or it is malformed -
    a garbled anchor must not silently widen or narrow the window)."""
    try:
        raw = ANCHOR_FILE.read_text(encoding="utf-8").strip()
        datetime.strptime(raw, TS_FORMAT)
        return raw
    except Exception:
        return None


def write_anchor(stamp):
    """ONLY ib_bot.run(dry=False), on seeing H < 1 at the start of the run."""
    datetime.strptime(stamp, TS_FORMAT)              # refuse to write junk
    _atomic_write(ANCHOR_FILE, stamp + "\n")
    return stamp


def clear_anchor():
    """ONLY ib_bot.run(dry=False), on a coverage gap. A missing anchor is fine;
    any other failure raises, so the caller never stamps coverage over an
    anchor it could not remove."""
    try:
        ANCHOR_FILE.unlink()
    except FileNotFoundError:
        pass


def _is_stamped(row):
    return str(row.get("order_ref") or "").startswith(STAMP)


def hkd_delta(row, base="HKD", ccy_by_conid=None):
    """What one execution did to the `base` cash balance.

    Returns a float, None when the row does not touch `base`, or the string
    "unknown" when it might and cannot be priced.

    Real ledger shapes (data/fills_ledger.jsonl): API CASH rows carry the pair's
    BASE currency as `symbol` ("GBP") and its QUOTE as `ccy` ("HKD"); the old
    seed rows spell the pair out ("HKD.JPY"). Price and commission are in the
    quote currency. An older row may have an empty commission_ccy, which
    fills_capture would have filled with `ccy`.
    """
    base = str(base).upper()
    try:
        qty = abs(float(row.get("qty") or 0))
        price = float(row.get("price") or 0)
        com = abs(float(row.get("commission") or 0))
    except (TypeError, ValueError):
        return "unknown"
    buy = str(row.get("side") or "").upper() in ("BOT", "BUY")
    sec = str(row.get("sec_type") or "").upper()
    sym = str(row.get("symbol") or "").upper()
    ccy = str(row.get("ccy") or "").upper()
    guessed = "ccy_guessed" in (row.get("flags") or []) or not ccy
    if guessed:
        # fills_capture books an unknown currency as USD and flags it. The same
        # con_id elsewhere in the ledger, with a real currency, answers it; with
        # nothing to go on the row may be HKD, and pricing it as USD would drop
        # a real HKD debit from the pocket - the unsafe direction.
        known = (ccy_by_conid or {}).get(str(row.get("con_id") or ""))
        if known:
            ccy, guessed = known, False
    amt = None
    if sec == "CASH":
        if "." in sym:
            b, q = sym.split(".", 1)
        else:
            b, q = sym, ("" if guessed else ccy)
        if b == base:
            amt = qty if buy else -qty                   # BOT receives the base
        elif q == base:
            amt = -(qty * price) if buy else qty * price  # BOT pays the quote
        elif not q:
            return "unknown"
    else:
        if guessed:
            # SEHK codes are numeric. An alphabetic ticker with a guessed
            # currency is a US/EU name on an unmapped venue, never HKD.
            if sym.split(".")[0].isdigit():
                return "unknown"
        elif ccy == base:
            amt = -(qty * price) if buy else qty * price
    com_ccy = str(row.get("commission_ccy") or ccy or "").upper()
    if com and com_ccy == base:
        amt = (amt or 0.0) - com                         # always a debit
    return amt


def bot_submitted_order_ids(path):
    """order_ids the bot recorded as submitted in ib_orders' ORDERS_LEDGER.

    Best effort, for the canary only: a missing or unreadable file, or a bad
    line, is simply no evidence. Never raises."""
    out = set()
    if not path:
        return out
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    if r.get("event") == "submitted" and r.get("order_id") not in (None, ""):
                        out.add(str(r["order_id"]).strip())
                except Exception:
                    continue
    except Exception:
        return set()
    return out


def merge_executions(ledger_rows, fresh_rows, now=None, window_days=5.0,
                     cached_rows=None):
    """Ledger rows plus the executions IB returns right now, deduped by execId.

    fills_capture never rewrites a row, so an execution captured before the
    order_ref key existed keeps lacking it on disk; the fresh copy (IB returns
    7 days) supplies it here, in memory only.

    cached_rows are earlier IB reads kept on the VM (EXECS_FILE). They fill in
    execIds the ledger lacks - a row publish_web's git reset wiped before it
    was pushed - exactly as a fresh row would, but they are NOT "seen" by this
    read.

    Also returns the execIds of API ledger or cache rows younger than
    `window_days` that the fresh read did NOT return. /iserver/account/trades
    covers 7 days, so a gap means the read was empty or partial - and a fill
    missing from it (the 01:30 SEHK buy the 23:35 sweep never saw) would leave P
    too HIGH, which is the one direction that can spend the transfer money. The
    caller must not trust the pocket then.
    """
    now = now or datetime.now(timezone.utc)
    by = {}
    for r in ledger_rows or []:
        eid = r.get("execId") if isinstance(r, dict) else None
        if eid:
            by[eid] = dict(r)                            # later lines win, as uk_cgt reads it
    for c in cached_rows or []:
        eid = c.get("execId") if isinstance(c, dict) else None
        if not eid:
            continue
        if eid not in by:
            by[eid] = dict(c)
        elif "order_ref" not in by[eid] and "order_ref" in c:
            by[eid]["order_ref"] = c.get("order_ref")
            by[eid]["order_id"] = c.get("order_id")
    seen = set()
    for f in fresh_rows or []:
        eid = f.get("execId") if isinstance(f, dict) else None
        if not eid:
            continue
        seen.add(eid)
        if eid not in by:
            by[eid] = dict(f)
        elif "order_ref" not in by[eid] and "order_ref" in f:
            by[eid]["order_ref"] = f.get("order_ref")
            by[eid]["order_id"] = f.get("order_id")
    missing = []
    for eid, r in by.items():
        if r.get("source") != "api" or eid in seen:
            continue
        try:
            t = datetime.strptime(str(r.get("ts") or "")[:16], TS_FORMAT) \
                .replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if (now - t).total_seconds() < window_days * 86400:
            missing.append(eid)
    return list(by.values()), sorted(missing)


# ------------------------------------------------------------- coverage ----
def _row_time(ts):
    try:
        return datetime.strptime(str(ts or "")[:16], TS_FORMAT).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def read_exec_cache():
    """(rows, intact) - the executions earlier LIVE runs read from IB.

    intact is False when the file is missing, unreadable, or holds a line that
    is not an execution. update_exec_cache only ever replaces it atomically, so
    a bad line means damage, and the row it held may have been an HKD debit:
    coverage_gap then cannot vouch for anything older than IB's own window.
    Never raises."""
    try:
        text = EXECS_FILE.read_text(encoding="utf-8")
    except Exception:
        return [], False
    rows, intact = [], True
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            intact = False
            continue
        if isinstance(r, dict) and r.get("execId"):
            rows.append(r)
        else:
            intact = False
    return rows, intact


def update_exec_cache(cached, fresh, anchor, now=None):
    """ONLY ib_bot.run(dry=False), once a strict executions read has succeeded.

    cached + fresh deduped by execId, the latest read winning (IB may report a
    commission after the first read). Rows older than BOTH the anchor less
    ANCHOR_MARGIN_DAYS and EXECS_KEEP_DAYS are dropped, so the file stays small,
    but a row at or after the anchor never is: once IB's 7-day window has moved
    past a fill, this file is where the pocket still sees it. With no anchor
    only the last EXECS_KEEP_DAYS are kept - an anchor set later starts after
    them. Written atomically; raises on a write failure, and the caller then
    writes no coverage stamp. Returns the rows written."""
    now = now or datetime.now(timezone.utc)
    by = {}
    for r in list(cached or []) + list(fresh or []):
        eid = r.get("execId") if isinstance(r, dict) else None
        if eid:
            by[eid] = dict(r)
    cutoff = now - timedelta(days=EXECS_KEEP_DAYS)
    a = _row_time(anchor) if anchor else None
    if a is not None:
        cutoff = min(cutoff, a - timedelta(days=ANCHOR_MARGIN_DAYS))
    keep = [r for r in by.values()
            if _row_time(r.get("ts")) is None or _row_time(r.get("ts")) >= cutoff]
    _atomic_write(EXECS_FILE, "".join(json.dumps(r, sort_keys=True) + "\n" for r in keep))
    return keep


def read_covered():
    """The last complete executions read (UTC datetime), or None. Never raises."""
    try:
        raw = COVERED_FILE.read_text(encoding="utf-8").strip()
        return datetime.strptime(raw, AT_FORMAT).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def write_covered(now=None):
    """ONLY ib_bot.run(dry=False): IB's executions were read in full at `now`,
    nothing flagged the read as partial, and every row it returned is already in
    EXECS_FILE."""
    stamp = (now or datetime.now(timezone.utc)).strftime(AT_FORMAT)
    _atomic_write(COVERED_FILE, stamp + "\n")
    return stamp


def coverage_gap(anchor, covered, cache_intact, now=None, max_days=None):
    """None when every fill since `anchor` can have been seen, else why not.

    A fill is only certain to be known if a complete read happened within IB's
    7-day window after it. That holds when either:
      * the anchor is younger than COVERAGE_MAX_DAYS - this run's own read
        reaches back past it; or
      * the last complete read (`covered`) is, and the cache is intact. By
        induction every fill from the anchor to `covered` is then on disk: each
        complete read wrote its rows to the cache before its stamp, the cache
        never drops a row after the anchor, and a run that found a gap deleted
        the anchor - so an anchor that is still there has had none.
    No anchor: nothing to vouch for, the pocket is not used anyway."""
    if not anchor:
        return None
    now = now or datetime.now(timezone.utc)
    max_days = COVERAGE_MAX_DAYS if max_days is None else max_days
    a = _row_time(anchor)
    if a is None:
        return "the anchor %r cannot be read" % (anchor,)
    horizon = now - timedelta(days=max_days)
    if a >= horizon:
        return None
    if covered is None:
        return ("no complete executions read is on record, and the anchor %s is older "
                "than %g days" % (anchor, max_days))
    if not cache_intact:
        return ("the executions cache %s is missing or damaged, and the anchor %s is "
                "older than %g days" % (EXECS_FILE.name, anchor, max_days))
    if covered < horizon:
        return ("the last complete executions read was %s, more than %g days ago - a "
                "fill since then may have left IB's 7-day window unseen"
                % (covered.strftime(AT_FORMAT), max_days))
    return None


def bot_submissions(path, since=None):
    """{order_id: submitted_at} for the bot's orders IB ACCEPTED - ib_orders'
    ORDERS_LEDGER "submitted" events carrying an order id - at or after `since`.

    Best effort: a missing or unreadable file, a bad line or an unparseable time
    is simply no evidence. Never raises."""
    out = {}
    if not path:
        return out
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    if r.get("event") != "submitted" or r.get("order_id") in (None, ""):
                        continue
                    t = datetime.fromisoformat(str(r.get("ts")).replace("Z", "+00:00"))
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=timezone.utc)
                    if since is None or t >= since:
                        out[str(r["order_id"]).strip()] = t
                except Exception:
                    continue
    except Exception:
        return {}
    return out


def unmatched_submissions(rows, submissions, placed_before):
    """order_ids from `submissions` placed at or before `placed_before` that no
    execution in `rows` carries, sorted."""
    filled = {str(r.get("order_id")).strip() for r in rows or []
              if isinstance(r, dict) and r.get("order_id") not in (None, "")}
    return sorted(oid for oid, t in (submissions or {}).items()
                  if t <= placed_before and oid not in filled)


def bot_pocket(rows, anchor, base="HKD", submitted_ids=None):
    """(P_raw, detail) - the bot's own `base` cash since `anchor` - or
    (None, detail) when stamping is not confirmed and the fallback rule applies.

    P_raw is a plain sum and may be negative (an unstamped sweep charged to an
    empty pocket) or exceed the balance (charges IB books outside executions).
    Callers clamp it to [0, H]; see exclusion().
    """
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    detail = {"anchor": anchor, "confirmed": False, "stamped": 0,
              "unstamped_out": 0, "unstamped_in_ignored": 0, "undated": 0,
              "reason": ""}
    detail["confirmed"] = any(_is_stamped(r) for r in rows)
    sub = {str(x).strip() for x in (submitted_ids or ()) if x not in (None, "")}
    broken = [r for r in rows
              if "order_ref" in r and not _is_stamped(r)
              and r.get("order_id") not in (None, "")
              and str(r.get("order_id")).strip() in sub]
    if broken:
        detail["confirmed"] = False
        detail["broken"] = [r.get("execId") for r in broken]
        # Name the rows. One unverified way in is IB giving its own commission
        # conversion (the small USD.HKD legs at a bot fill's exact timestamp)
        # the bot order's order_id without its order_ref; a human has to look.
        detail["reason"] = ("CANARY: %d fill(s) on orders the bot submitted came back "
                            "without its %s order_ref (%s) - IB is not echoing the "
                            "stamp, so the pocket cannot be trusted"
                            % (len(broken), STAMP, "; ".join(
                                "%s %s %s %s order %s ref %r" % (
                                    r.get("execId"), r.get("side"), r.get("qty"),
                                    r.get("symbol"), r.get("order_id"), r.get("order_ref"))
                                for r in broken[:3])))
        return None, detail
    if not anchor:
        detail["reason"] = "no anchor yet (set by a live run that sees HKD < 1)"
        return None, detail
    if not detail["confirmed"]:
        detail["reason"] = ("stamping not confirmed: no execution carries an %s "
                            "order_ref yet" % STAMP)
        return None, detail
    ccy_by_conid = {}
    for r in rows:
        if r.get("ccy") and "ccy_guessed" not in (r.get("flags") or []) \
                and "." not in str(r.get("symbol") or "") and r.get("con_id"):
            ccy_by_conid.setdefault(str(r["con_id"]), str(r["ccy"]).upper())
    p = 0.0
    for r in rows:
        ts = str(r.get("ts") or "")[:16]
        if len(ts) != 16:
            detail["undated"] += 1                       # cannot place it against the anchor
            continue
        if ts < anchor:
            continue
        d = hkd_delta(r, base, ccy_by_conid)
        if d == "unknown":
            detail["reason"] = ("execution %s after the anchor might move %s but its "
                                "currency is unknown" % (r.get("execId"), base))
            return None, detail
        if d is None:
            continue
        if _is_stamped(r):
            detail["stamped"] += 1
            p += d
        elif d < 0:
            detail["unstamped_out"] += 1                 # IB sweeps and FXCONV: the bot's cost
            p += d
        else:
            detail["unstamped_in_ignored"] += 1          # the operator's pot
    return p, detail


def write_pocket(p, pending, confirmed, anchor=None, now=None):
    """ONLY ib_bot.run(dry=False), at the end of the run."""
    now = now or datetime.now(timezone.utc)
    body = {"p": (None if p is None else round(float(p), 2)),
            "pending": round(max(0.0, float(pending or 0.0)), 2),
            "confirmed": bool(confirmed and p is not None),
            "anchor": anchor,
            "at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    _atomic_write(POCKET_FILE, json.dumps(body, indent=1) + "\n")
    return body


def published_pocket(now=None, max_age_h=None):
    """P_eff = max(0, P - pending) from the last live run's pocket file, or None
    (fallback rule) when the file is missing, unreadable, unconfirmed or older
    than POCKET_MAX_AGE_H."""
    max_age_h = POCKET_MAX_AGE_H if max_age_h is None else max_age_h
    try:
        d = json.loads(POCKET_FILE.read_text(encoding="utf-8"))
        if not d.get("confirmed") or d.get("p") is None:
            return None
        p, pending = float(d["p"]), float(d.get("pending") or 0.0)
        at = datetime.strptime(d["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None
    if p != p or pending != pending:
        return None
    age_h = ((now or datetime.now(timezone.utc)) - at).total_seconds() / 3600.0
    if not (-1.0 <= age_h <= max_age_h):
        return None
    return max(0.0, p - max(0.0, pending))


def publisher_exclusion(base_cash, now=None):
    """(exclusion, bot_share) for every reader that does not sweep executions
    itself: publish_web, the digest, and ib_bot.net_liq outside a run."""
    p = published_pocket(now)
    return exclusion(base_cash, p), bot_share(base_cash, p)
