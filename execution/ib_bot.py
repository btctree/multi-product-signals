"""Multi-Product execution bot for Interactive Brokers.

Reads the LIVE D signals from the deployed dashboard, reconciles them against your
IB positions, and places entries / exits / trailing-stop sells. Designed to run
ONCE per invocation (a daily cron on your Oracle VM), idempotently.

SAFE BY DEFAULT:
  * PORT defaults to 4002 (IB Gateway PAPER). Live is 4001 — you change it.
  * CONFIRM_FIRST=True  -> prints every intended order and waits for your Enter.
  * DRY_RUN via --dry    -> compute + print. Places nothing AND writes
    nothing: no state.json, no bot_state.json, no dashboard commit, no conid
    cache update (see _conid_cache_writes). Safe
    to preview a run on the live VM, with ONE exception: connect_or_heal
    runs first and a dry run can still kill a zombie gateway (and arm the
    cooldown that would otherwise heal the next real run). Do not --dry
    against a gateway you suspect is wedged.
  * A per-order cap (HK$50k), max positions and an order value guard are
    enforced. The 8% NetLiq KILL-SWITCH is kept in code but switched OFF
    (KILL_SWITCH, owner decision 2026-10-26).
You flip these to run unattended/live; nothing here connects to a live account
or moves money on its own until you set PORT=4001 and CONFIRM_FIRST=False.

Requires: pip install ib_async requests
Never put credentials in this file — the bot talks to your already-logged-in
IB Gateway over the local socket.
"""
import argparse
import json
import math
import os
import re
import sys
import urllib.request
from pathlib import Path

import alerts
import earmark
from broker import IB, LimitOrder, MarketOrder, Forex
from contracts import to_ib, currency_of

# ---------------- config (env-overridable) ----------------
HOST = os.environ.get("IB_HOST", "127.0.0.1")
PORT = int(os.environ.get("IB_PORT", "4002"))          # 4002 paper / 4001 live
CLIENT_ID = int(os.environ.get("IB_CLIENT_ID", "17"))
BASE_CCY = os.environ.get("IB_BASE_CCY", "HKD")
SIGNALS_URL = os.environ.get(
    "SIGNALS_URL", "https://btctree.github.io/multi-product-signals/data.json")
PRODUCTS_URL = SIGNALS_URL.rsplit("/", 1)[0] + "/products/"
TARGET_POSITIONS = int(os.environ.get("TARGET_POSITIONS", "15"))
# Per-order cap, in BASE_CCY. HK$50,000 since 2026-10-26 (owner decision; it was
# 20,000), as a CODE default: there are several ib_bot invocation sites on the
# VM plus the digest's own copy, and an env var set in one would drift from the
# others. The env var still overrides it - which is exactly why every run now
# publishes the value in effect (effective_config), so a leftover VM setting is
# visible on the dashboard instead of silently cancelling this.
MAX_ORDER_BASE = float(os.environ.get("MAX_ORDER_BASE", "50000"))   # per-order cap
DAILY_LOSS_KILL = float(os.environ.get("DAILY_LOSS_KILL", "0.08"))  # 8% of NetLiq
# THE 8% NETLIQ KILL SWITCH IS OFF since 2026-10-26 (owner decision). Research
# found the rule as coded (a peak with no reset) freezes entries for good after
# a genuine fall, and every trip in this account's life was a false one (a
# deposit or withdrawal moving NetLiq). The gate is kept, not deleted, so it can
# come back with one line: kill_switch.py stays (the digest imports it) and
# kill_switch_check below is unchanged. It is a CODE flag on purpose. NEVER
# switch it off with DAILY_LOSS_KILL=0: the test is `nl < peak * (1 - kill)`,
# so 0 blocks entries on every day that is not a new high - the opposite.
# daily_signal.KILL_SWITCH mirrors this; change both together.
KILL_SWITCH = False
CONFIRM_FIRST = os.environ.get("CONFIRM_FIRST", "1") != "0"
LIMIT_BUFFER = float(os.environ.get("LIMIT_BUFFER", "0.005"))       # marketable limit
# Order value guard (2026-10-26, with the 2.5x larger cap). An entry is re-priced
# at IB's own quote before it is sized: more than this far from the signal price
# and it is skipped - a bad price on either side is what would otherwise turn
# into a bad share count, since place() re-bases the limit to IB's quote but
# keeps the share count, and IB's own order-value warning is auto-confirmed
# (ib_orders.py). Within it, the shares are sized and funded at the HIGHER of
# the two prices, so the order can never be worth more than the cap.
ENTRY_MOVE_MAX = 0.10
# FX_CONVERT stays OFF, and is NOT the switch for the bot's own HKD. With it on,
# ensure_ccy pays every USD/JPY/EUR shortfall from HKD through convert_into,
# with no pocket or earmark check - HKD the bot does not have, i.e. a margin loan
# against the operator's transfer money. The bot spending its OWN stamped HKD is
# a separate, narrow path (OWN_BASE_FUND below). Bot FX out of HKD was dead
# anyway when this was switched off on 2026-07-30: ~USD 1,800 orders sat under
# IDEALPRO's 25k minimum and every one since 23 Jul was rejected as an odd lot.
FX_CONVERT = os.environ.get("FX_CONVERT", "0") != "0"
# Fund a foreign purchase from cash held in OTHER non-base currencies. This is
# NOT the same switch as FX_CONVERT and does not weaken it: FX_CONVERT governs
# paying from BASE_CCY (HKD) at large, which stays off because the HKD balance is
# the operator's transfer funding. This path sells HKD only as the bot's own
# pocket allows (OWN_BASE_FUND) - never an unmarked pot.
FX_FUND_NONBASE = os.environ.get("FX_FUND_NONBASE", "1") != "0"
# Over-convert by this much when buying BASE_CCY to fund an order in it, so a
# tick against us between the conversion and the stock order does not leave the
# purchase a few dollars short. Operator's number, 2026-09-12.
BASE_FUND_BUFFER = float(os.environ.get("BASE_FUND_BUFFER", "1.03"))
# The bot may SELL its OWN HKD - the stamped pocket P (earmark.bot_pocket) - as
# one more funding source for a USD/JPY/EUR order (owner decision 2026-10-26).
# Until now nothing could: HK sale proceeds and the 3% funding-buffer residue
# were stranded in HKD for good. Narrow by construction (_own_base_spendable):
# only while P is known this run, the earmark is frozen and every working
# order's cash is reserved; bounded by min(P, HKD held - earmark), less HKD that
# working HK buys need (with their fee headroom) and OWN_BASE_MARGIN for fees
# booked outside executions. A code flag, like KILL_SWITCH; False is the rollback.
OWN_BASE_FUND = True
OWN_BASE_MARGIN = 100.0
# Currencies left out of the free cash that sizes entries. GBP is the owner's
# month-end pass-through money on its way to HKD (earmark.py), not trading
# capital, and LSE entries are skipped anyway (pence). It stays a funding
# SOURCE, exactly as before - only the sizing ignores it.
SIZING_SKIP_CCY = frozenset(("GBP",))
# Cash parking of idle USD in a T-bill ETF (owner decision 2026-10-26): SHADOW
# mode only - each run logs what it WOULD park or unpark and places nothing.
# SGOV is never a stock slot and never enters state['map'] (no stock exit rule
# may ever sell it). "off" silences the log; a live mode is not built: it goes
# live only after about a week of shadow logs and an account-permission check.
PARK_SYMBOL = "SGOV"
PARK_MODE = "shadow"
PARK_BUFFER_SLOTS = 3        # keep this many slots' worth of USD unparked
PARK_MIN_USD = 1000.0        # smaller moves are not worth the round trip
PARK_REF_PX = 100.5          # SGOV's NAV hardly moves; for the shadow log only
# Per-country market trend filter (owner decision 2026-10-26: "test the country
# filter only, starting in log-only mode"). The rule as backtested: no new buy
# in a market while its home index (US ^GSPC, HK ^HSI, JP ^N225, EU ^STOXX50E)
# closes below its 100-day average, judged on the last finished index bar;
# exits never gated. The engine publishes each index against its average as
# data.json "market_trend" (engine/market_trend.py). SHADOW mode only: every
# BUY the bot sends in a market that is below gets a log line, a "shadow" tag
# on its History row and a row in bot_state.json "market_filter" - and is sent
# exactly as it would be without the filter. Nothing here may touch the exit
# loop, slot counting, sizing, funding or order placement. "off" silences it.
# A live mode is NOT built: it needs the owner's approval after the shadow
# record has been read. A code flag, like PARK_MODE and KILL_SWITCH.
MARKET_FILTER = "shadow"
MARKET_FILTER_MARKETS = ("US", "HK", "JP", "EU")
MARKET_FILTER_KEEP = 50      # would-have-skipped rows kept in state and published
# HK ENTRIES ARE OFF. Two things must land before SEHK can be traded safely:
#   1. the board lot must come from HKEX's List of Securities, not from IB's
#      sizeIncrement - that field is an order-ticket STEP and this account
#      measured it as a flat 100 for every stock on 2026-09-05. HKEX's real
#      lots vary per stock (2359 = 100, 2269 = 500, 1810 = 200), so trusting
#      IB would have sent 300 shares of 2269.HK: an odd lot, which SEHK's
#      continuous market will not auto-match.
#   2. HK limit prices must snap to HKEX's stepped spread table (Second
#      Schedule Part A: 0.02 at HK$20-50, 0.10 at HK$100-200). Today they snap
#      to IB's lowest ladder band, so the first HK order would carry an illegal
#      price increment.
# Exits are NOT affected - a position held must always be sellable. Set
# HK_ENABLED=1 to re-enable once both are in.  Operator-approved 2026-09-12.
HK_ENABLED = os.environ.get("HK_ENABLED", "0") != "0"
# Time stop: exit any position held >= this many trading bars (sell at next
# open, like every other exit). 60 is the VALIDATED engine default the live
# bot had silently omitted (engine_rr.py:30 max_hold=60) — restoring it was
# board-reviewed 2026-08-01: measured twice (+0.8/+0.9pp CAGR, 2.0/2.3pp
# shallower maxDD), it re-enters the -30% DD mandate. 0 disables.
MAX_HOLD_BARS = int(os.environ.get("MAX_HOLD_BARS", "60"))
STATE = Path(__file__).with_name("state.json")
FILLS_LEDGER = Path(__file__).resolve().parent.parent / "data" / "fills_ledger.jsonl"
# Holdings the owner bought by hand in IB and has asked the bot to manage. In the
# repo, not on the VM: it arrives with the git pull before each run, and it is
# reviewed like code. See adopt_holdings.
ADOPT = Path(__file__).with_name("adopt.json")
ADOPT_LOT_DAYS = 10          # the lot held must have been bought this close to entry_date


def log(*a):
    print("[bot]", *a, flush=True)


def _adopt_num(v):
    """A finite positive float, or None."""
    try:
        f = float(v)
    except Exception:
        return None
    return f if math.isfinite(f) and f > 0 else None


def adopt_holdings(state, held, dry=False, path=None, today=None, lot_date=None):
    """Hand a holding the owner bought by hand in IB to the bot's exit rules.

    The exit loop manages only holdings in state['map'], which an entry writes
    when the BOT buys. A share bought by hand in IB has no map entry, so no
    cut-loss, trailing stop, regime or time exit ever runs on it - and it still
    takes one of the 15 slots. On 2026-10-23 the owner bought MSFT on the
    strategy's own signal (the false kill-switch was blocking the bot that night)
    and asked for it to be managed like the bot's own buy.

    execution/adopt.json lists such holdings with what the bot's own entry
    records: the signal close as entry and the signal's cut-loss as stop (and
    optionally the high-water mark since). entry_date is the FILL day - the
    backtest's bars=0-on-fill-day rule, and what ledger_entry_date backfills; the
    bot's own 23:35 UTC buys stamp the signal day, one weekday earlier, so an
    adopted holding reaches the 60-bar time stop one weekday later than an
    identical bot buy. Each one is seeded into state once, and only when ALL of
    this holds; otherwise it is logged and left alone:
      - it is held long right now under its IB symbol;
      - no OTHER instrument already owns that IB symbol in state['map'];
      - entry, stop (below entry) and hw (not below entry) are sane numbers and
        entry_date is a real day, not in the future;
      - the lot held was bought within ADOPT_LOT_DAYS of entry_date, per the
        fills ledger - so an old line can never adopt a later re-purchase with
        stale prices. No ledger record is allowed: the holding is the proof.
    Already managed is a no-op (one log line), so the file can stay as it is -
    but only a record of THIS lot counts: its entry_date no earlier than the
    line's (an adoption writes exactly that date; a later bot re-buy writes a
    later one). map and pos are never removed after a sale, so an older lot's
    record - even one only days older - would otherwise pass for it and the
    exits would run on that lot's stop, high and entry date (review 2026-10-23).
    Such a record goes through the checks below and is replaced, logged. Under --dry the seeding happens in memory only, so the
    preview evaluates the exits a live run would. Never raises: a broken file
    must never cost a run its exits.
    """
    from datetime import date, timedelta
    try:
        p = Path(path) if path is not None else ADOPT
        if not p.exists():
            return []
        raw = json.loads(p.read_text(encoding="utf-8"))
        items = raw.get("adopt") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            log("  !! adopt.json has no 'adopt' list - nothing adopted")
            return []
    except Exception as e:
        log(f"  !! adopt.json unreadable ({str(e)[:80]}) - nothing adopted")
        return []
    today = today or date.today()
    done = []
    for it in items:
        try:
            if not isinstance(it, dict):
                continue
            ysym = str(it.get("symbol") or "").strip()
            ib_sym = str(it.get("ib_symbol") or ysym).strip()
            if not ysym or not ib_sym:
                continue
            if PARK_SYMBOL in (ysym.upper(), ib_sym.upper()):
                # Parked cash, not a stock: a map entry would hand it to the
                # stock exit rules (regime, trailing, 60-bar time stop).
                log(f"  !! adopt {ysym}: {PARK_SYMBOL} is the cash-parking ETF, never "
                    f"a managed stock holding - NOT adopted")
                continue
            smap = state.setdefault("map", {})
            spos = state.setdefault("pos", {})
            ed = str(it.get("entry_date") or "")
            try:
                ed_day = date.fromisoformat(ed)
            except Exception:
                ed_day = None
            rec = spos.get(ysym)
            if (smap.get(ib_sym) == ysym and isinstance(rec, dict) and ed_day
                    and isinstance(rec.get("entry_date"), str)
                    and rec["entry_date"][:10] >= ed_day.isoformat()):
                log(f"  adopt {ysym}: already managed (entry_date "
                    f"{rec['entry_date'][:10]}) - this adopt.json line can be removed")
                continue                       # this lot's record, or a later one
            qty = (held.get(ib_sym) or (None, 0))[1]
            if not qty or qty <= 0:
                log(f"  adopt {ysym}: not held long under {ib_sym} - nothing to adopt")
                continue
            if smap.get(ib_sym) not in (None, ysym):
                log(f"  !! adopt {ysym}: IB symbol {ib_sym} already belongs to "
                    f"{smap[ib_sym]} - NOT adopted")
                continue
            entry, stop = _adopt_num(it.get("entry")), _adopt_num(it.get("stop"))
            hw = _adopt_num(it.get("hw")) if it.get("hw") is not None else entry
            if not (entry and stop and hw and stop < entry <= hw and ed_day
                    and ed_day <= today and len(ed) == 10):
                log(f"  !! adopt {ysym}: entry {it.get('entry')!r} / stop "
                    f"{it.get('stop')!r} / hw {it.get('hw')!r} / entry_date "
                    f"{it.get('entry_date')!r} not usable - NOT adopted")
                continue
            lot = lot_date(ib_sym) if lot_date else ledger_entry_date(ib_sym)
            if lot:
                try:
                    lot_day = date.fromisoformat(str(lot)[:10])
                except Exception:
                    lot_day = None
                if lot_day and not (ed_day - timedelta(days=ADOPT_LOT_DAYS) <= lot_day
                                    <= ed_day + timedelta(days=ADOPT_LOT_DAYS)):
                    log(f"  !! adopt {ysym}: the {qty} held were bought {lot}, not "
                        f"near {ed} - a different lot; NOT adopted")
                    continue
            if isinstance(rec, dict):
                log(f"  adopt {ysym}: replacing an older lot's record (entry_date "
                    f"{rec.get('entry_date')}, stop {rec.get('stop')})")
            smap[ib_sym] = ysym
            spos[ysym] = {"entry": entry, "hw": hw, "stop": stop, "entry_date": ed,
                          "adopted": today.isoformat()}
            done.append(ysym)
            log(f"ADOPT {ysym}: {qty} held, bought by the owner in IB - now managed by "
                f"the exit rules: entry {entry}, cut-loss {stop}, high {hw}, "
                f"entry date {ed}" + (" (in memory only: --dry)" if dry else ""))
            if not dry:
                from datetime import datetime, timezone
                PLACED.append({"time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                               "action": "ADOPT", "qty": qty, "symbol": ysym, "limit": "",
                               "ccy": currency_of(ysym),
                               "reason": f"your own IB buy, now managed by the bot: "
                                         f"cut-loss {stop:g}, entry {entry:g} ({ed})",
                               "status": "notice", "error": ""})
        except Exception as e:
            log(f"  !! adopt: an entry could not be used ({str(e)[:80]}) - skipped")
    return done


# ---- the kill-switch's peak, net of money moved in and out ---------------------
# The rule (why the peak is tracked in trading wealth, net of the hand-kept
# deposits and withdrawals) lives in kill_switch.py since 2026-10-23, so the
# Telegram digest reports exactly the gate this run enforces. Not called at all
# while KILL_SWITCH is False (since 2026-10-26); kept intact for switching back.
def kill_switch_check(state, nl, path=None, today=None):
    """(killed, peak in today's money, basis). See kill_switch.py.

    It runs before the exit loop, and exits must never depend on this gate, so
    nothing here may raise: anything unexpected falls back to the old rule."""
    try:
        import kill_switch
        killed, peak, basis, warn = kill_switch.check(state, nl, path=path, today=today,
                                                      kill=DAILY_LOSS_KILL)
    except Exception as e:                      # belt and braces: check() never raises
        try:
            peak = max(float(state.get("_peak_netliq")), nl)
            if not math.isfinite(peak):
                peak = nl
        except Exception:                       # incl. OverflowError on a huge int
            peak = nl
        killed, basis, warn = nl < peak * (1 - DAILY_LOSS_KILL), "raw NetLiq peak", [str(e)[:120]]
    for w in warn:
        log(f"  !! kill-switch: {w}")
    if basis.startswith("raw"):
        log("  !! kill-switch: data/netliq_history.json could not be used - "
            "using the stored NetLiq peak, which deposits and withdrawals can move")
    return killed, peak, basis


def bars_held(entry_date):
    """Weekdays (Mon-Fri) from entry_date to today, exclusive of entry day.
    Under the 00:35 UTC cron the seeded entry_date IS the fill calendar day
    (matches the backtest's bars=0 on fill day); the conservative bias comes
    from counting the not-yet-closed run day plus exchange holidays — worst
    case ~1.5 trading-weeks EARLY over a full 60-bar hold (HK/JP holiday
    windows), never late. Deterministic and restart-safe: recomputed from the
    stored date each run, no counter to drift on missed runs or restores."""
    from datetime import date, timedelta
    try:
        y, m, d = (int(x) for x in str(entry_date)[:10].split("-"))
        start = date(y, m, d)
    except Exception:
        return 0
    today = date.today()
    if today <= start:
        return 0
    n, cur = 0, start + timedelta(days=1)
    while cur <= today:
        if cur.weekday() < 5:
            n += 1
        cur += timedelta(days=1)
    return n


# ---------------- market clock: decide only on a finished bar ----------------
# The session table, the settle margin and market_decidable live in
# market_clock.py since review 2026-09-17: daily_signal (python 3.9, which must
# not import this module) needs the same rule for the /update digest, and one
# table cannot drift the way two copies would. Re-exported here so every
# existing caller and test keeps working. See market_clock for why a market is
# decided only on a finished bar, and why that bar must also be in the build.
from market_clock import (MARKET_SESSIONS, SESSION_SETTLE_MIN,  # noqa: E402,F401
                          last_settled_close, market_decidable, parse_generated_at)


def ledger_entry_date(sym):
    """Entry date of the CURRENT lot of sym from data/fills_ledger.jsonl:
    the earliest stock BUY fill AFTER the last SELL (a prior round trip must
    not resurrect an old date and fire an immediate time exit on a young
    re-entry). Used to backfill positions opened before the time stop existed
    (hand-over from the running deploy; all 15 current holdings verified).
    Missing/unparseable ledger -> None (caller falls back to today, which only
    ever DELAYS a time exit, never forces one)."""
    try:
        buys, last_sell = [], None
        with open(FILLS_LEDGER, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue              # one bad line must not mask good fills
                if r.get("symbol") != sym or r.get("sec_type") == "CASH":
                    continue
                d = str(r.get("date") or "")[:10]
                if len(d) != 10:
                    continue
                if r.get("side") in ("BOT", "BUY"):
                    buys.append(d)
                elif r.get("side") in ("SLD", "SELL"):
                    if last_sell is None or d > last_sell:
                        last_sell = d
        live = [d for d in buys if last_sell is None or d > last_sell]
        # partial trims (sell while still holding) push the date LATER ->
        # the time stop can only fire earlier, never later — conservative
        return min(live) if live else (min(buys) if buys else None)
    except Exception:
        return None


def safe_name(sym):
    return sym.replace("^", "_IDX_").replace("=", "_EQ_").replace(".", "_")


def get_json(url):
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.load(r)


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(s):
    """Replace state.json atomically: temp file, flush, fsync, os.replace.

    state.json is the ONLY copy of every stop, high-water mark, entry date and
    the map that ties a holding to its exits - it is gitignored and lives on
    the VM alone. It used to be rewritten in place (truncate, then write), so a
    full disk, a SIGKILL or a crash between the two left it empty or
    half-written; load_state then raised on every later run,
    which stops every exit, the phone SELL (ib_commands reads it) and the hourly
    publish together (board review 2026-09-21). Now a reader sees the old file or
    the new one, never a torn one, and the fsync makes the new one survive a
    power cut before os.replace makes it current. Still RAISES on failure: the
    callers log it, and the previous state.json stays in place."""
    tmp = STATE.with_name(STATE.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(s, indent=1))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE)


# ---------------- IB helpers ----------------
def _excluded_cash():
    """Cash sitting in the account that is NOT trading capital - money parked
    on its way somewhere else. Subtracted from NetLiq everywhere, so it cannot
    inflate position sizing, ratchet the kill-switch peak, or show up in the
    dashboard's P&L calendar as profit it never earned.

    Set via /root/excluded_cash (one number, base currency) or EXCLUDED_CASH.
    ONE place on purpose: the crontab has three ib_bot invocation sites and a
    per-site env var would inevitably drift between them.

    CLEAR IT once the money actually leaves, or NetLiq stays understated - the
    amount is logged loudly on every run precisely so that cannot go unnoticed.
    """
    return earmark.marker()


def net_liq(ib):
    nl = 0.0
    vals = ib.accountValues()          # one pass: the cap below reuses it
    for v in vals:
        if v.tag == "NetLiquidation" and v.currency == BASE_CCY:
            nl = float(v.value)
            break
    else:
        for v in vals:
            if v.tag == "NetLiquidation":
                nl = float(v.value)
                break
    held = 0.0
    for v in vals:
        if v.tag == "CashBalance" and v.currency == BASE_CCY:
            held = max(0.0, float(v.value))
            break
    # earmark.exclusion() is the one shared rule: min(M, H - the bot's own HKD)
    # when the bot's stamped pocket is known, else min(M, H) exactly as before.
    # Inside run() the pocket is the one swept from IB's executions at the start
    # of THIS run; outside it (publish_only, ib_commands) it is the last live
    # run's pocket file, net of HK buys still working - see earmark.py. Without
    # the pocket, a marker above the operator's own HKD reached into the bot's
    # funding too; with it, that funding stays in the pool.
    marked = earmark.marker()
    if _POCKET_RUN.get("active"):
        own = earmark.bot_share(held, _POCKET_RUN.get("p"))
        exc = earmark.exclusion(held, _POCKET_RUN.get("p"))
    else:
        exc, own = earmark.publisher_exclusion(held)
    if marked and exc < marked:
        if own is None:
            log(f"  earmarked cash marker is {marked:,.0f} but only {held:,.0f} "
                f"{BASE_CCY} is held - excluding {exc:,.0f}. Either the money has "
                f"left (clear the marker) or it has not converted yet")
        else:
            log(f"  earmarked cash marker is {marked:,.0f} but only "
                f"{held - own:,.0f} of the {held:,.0f} {BASE_CCY} held is not the "
                f"bot's own ({own:,.0f}) - excluding {exc:,.0f}. Either the money "
                f"has left (clear the marker) or it has not converted yet")
    elif own:
        log(f"  bot's own {BASE_CCY} {own:,.0f} of {held:,.0f} held stays in the pool")
    # Remember what was ACTUALLY applied, so the publisher reports the exclusion
    # that this netliq was computed with rather than re-reading the file at the
    # end of the run, an hour and a currency conversion later.
    _EXC_APPLIED["base"] = exc
    _EXC_APPLIED["bot"] = own
    if exc:
        log(f"  excluding {exc:,.0f} {BASE_CCY} earmarked cash "
            f"(NetLiq {nl:,.0f} -> {nl - exc:,.0f})")
        nl -= exc
    return nl


def cash_by_ccy(ib):
    out = {}
    for v in ib.accountValues():
        if v.tag == "CashBalance" and v.currency and v.currency != "BASE":
            out[v.currency] = float(v.value)
    return out


def held_positions(ib):
    """symbol(local) -> (position obj, qty)."""
    out = {}
    for p in ib.positions():
        if p.position != 0:
            out[p.contract.symbol] = (p, p.position)
    return out


def confirm(msg):
    if not CONFIRM_FIRST:
        return True
    try:
        return input(f"  CONFIRM {msg}  [y/N] ").strip().lower() == "y"
    except EOFError:
        log("no TTY for confirm -> skipping (set CONFIRM_FIRST=0 to auto-run)")
        return False


PLACED = []            # orders actually transmitted this run (for the dashboard)
_TICK_CACHE = {}


def min_tick(ib, contract):
    """The venue's minimum price increment (IB rejects limits that violate it,
    Error 110 — e.g. US stocks tick $0.01, JPY stocks tick ¥1)."""
    key = getattr(contract, "conId", 0) or contract.symbol
    if key in _TICK_CACHE:
        return _TICK_CACHE[key]
    tick = 0.01
    try:
        cds = ib.reqContractDetails(contract)
        if cds and cds[0].minTick:
            tick = float(cds[0].minTick)
        # The same answer carries IB's price bands. Keep them now, so a European
        # order's ib_price_bands() costs no second request.
        _keep_price_bands(key, cds)
    except Exception:
        pass
    _TICK_CACHE[key] = tick
    return tick


def _keep_price_bands(key, cds):
    """Cache IB's (lowerEdge, increment) bands from a contract-details answer.

    Returns the sorted bands, or None when the answer is a FAILURE - nothing
    came back, or the web shim handed over its defaults (isFallback). A failure
    is never cached: unlike min_tick's 0.01, a transient error must not strip a
    name of its real bands for the rest of the run. A SUCCESS with no bands (a
    flat increment, or ib_async's ContractDetails, which has no priceBands at
    all) is cached as [] - that is IB's answer, and asking again changes nothing.
    """
    if not cds or getattr(cds[0], "isFallback", False):
        return None
    bands = []
    for pair in getattr(cds[0], "priceBands", None) or []:
        try:
            edge, inc = float(pair[0]), float(pair[1])
        except Exception:
            continue
        if edge == edge and inc == inc and edge >= 0 and 0 < inc < float("inf"):
            bands.append((edge, inc))
    bands.sort()
    _TICK_CACHE[("bands", key)] = bands
    return bands


def ib_price_bands(ib, contract):
    """IB's price-banded tick ladder for this contract, [] when unknown."""
    key = getattr(contract, "conId", 0) or contract.symbol
    if ("bands", key) in _TICK_CACHE:
        return _TICK_CACHE[("bands", key)]
    try:
        return _keep_price_bands(key, ib.reqContractDetails(contract)) or []
    except Exception:
        return []


def band_tick(bands, price):
    """The increment of the largest lowerEdge at or below `price`; 0 if none."""
    tick = 0.0
    for edge, inc in bands:                 # sorted ascending by lowerEdge
        if edge > price + 1e-9:
            break
        tick = inc
    return tick


def ib_band_tick(ib, contract, price):
    """IB's own tick at `price`. minTick is only the LOWEST band of this ladder:
    BAYN went out at 48.4108 on it and Xetra refused, because IB's rule for a
    price near 48 was 0.01. 0.0 when IB gave no bands (the caller's max() then
    ignores it)."""
    return band_tick(ib_price_bands(ib, contract), price)


# Currencies whose exchanges enforce a minimum tradeable unit. Everywhere else a
# single share is tradeable and IB's sizeIncrement must NOT be read as a lot.
_BOARD_LOT_CCY = ("JPY", "HKD")


def entry_blocked_reason(ysym, ccy):
    """Why this candidate may not be ENTERED right now, or None to proceed.

    Entries only. Exits must never consult this: a position already held has to
    stay sellable whatever is wrong with the venue's metadata.
    """
    if str(ccy or "").upper() == "HKD" and not HK_ENABLED:
        return ("HK entries are switched off until the HKEX board-lot and tick "
                "tables are in place (set HK_ENABLED=1 to re-enable)")
    return None


def is_crypto_entry(a):
    """True for a crypto BUY row: a -USD symbol or market CRYPTO.

    The bot does not trade crypto (owner decision 2026-10-26, shipped with the
    free-cash sizing). contracts.to_ib maps -USD to a Crypto PAXOS contract, which
    IBKR Hong Kong does not offer, and the 30% crypto sleeve the research blends
    in is on hold. While a slot was NetLiq/15 a coin rounded to 0 units and was
    dropped without a word; at free cash / free slots ETH sizes to 1 unit, and a
    REJECTED entry still uses a slot and writes state map/pos. So it is refused
    before to_ib and qualifyContracts: no slot, no Web API call, no state write.
    Exits and the phone SELL are untouched - a coin held must stay sellable.
    daily_signal mirrors this test."""
    sym = str(a.get("symbol") or "").upper()
    return sym.endswith("-USD") or str(a.get("market") or "").upper() == "CRYPTO"


def _entry_score(a):
    """The row's score as a number; anything unreadable ranks as 0."""
    try:
        s = float(a.get("score") or 0)
    except (TypeError, ValueError):
        return 0.0
    return s if s == s else 0.0


def _entry_tier(a):
    """The engine's ranking tier: 0 for E rows, 1 for D3 rows, absent = 0."""
    try:
        t = int(a.get("tier") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, t)


def entry_rank(a):
    """Sort key for BUY candidates: tier first, then score, high first.

    D3+E1 (engine/config.py MODEL, 2026-10-26) adds D3 rows - trend dips that
    hold the 0.618 level - ranked AFTER every E row, as the tested model did
    (live_sim card_list). data.json is written already in this order; the bot
    sorts again rather than trust file order. With no tier field at all this is
    exactly the old score-only sort. daily_signal sorts the same way."""
    return (_entry_tier(a), -_entry_score(a))


_HK_LOTS = None


def hk_board_lot(symbol):
    """HKEX's own board lot for a SEHK code, or None if we do not have it.

    data/hk_board_lots.json is built from HKEX's List of Securities - the
    exchange's own file, the only authority on this. IB is asked too, but only
    as a cross-check: its sizeIncrement is an order-ticket STEP on some venues
    (this account measured a flat 100 for every US stock on 2026-09-05), and
    SEHK lots genuinely vary per stock, from 10 shares to 100,000.
    """
    global _HK_LOTS
    if _HK_LOTS is None:
        try:
            p = Path(__file__).resolve().parent.parent / "data" / "hk_board_lots.json"
            _HK_LOTS = json.loads(p.read_text(encoding="utf-8")).get("lots") or {}
            log(f"  HKEX board-lot table: {len(_HK_LOTS)} stocks")
        except Exception as e:
            log(f"  ! no HKEX board-lot table ({str(e)[:70]})")
            _HK_LOTS = {}
    try:
        return _HK_LOTS.get("%04d.HK" % int(str(symbol).split(".")[0]))
    except Exception:
        return None


def lot_size(ib, contract):
    """Smallest number of shares the VENUE will trade. 1 outside board-lot markets.

    Only board-lot markets consult IB. /iserver/contract/<conid>/info-and-rules
    returns sizeIncrement 100 for every stock - DELL, HPE and DXCM alike -
    because it is the order-ticket STEP, not a minimum. The same payload also
    reports fraqInt 4 (fractional trading to four decimals), which cannot
    coexist with a 100-share floor; and the account holds 4 DELL and 33 HPE,
    which a real 100-share minimum would have made impossible.

    Read as a lot, it rounded every US order to zero - a 20-share DXCM buy
    sitting inside the position budget became 20 // 100 * 100 = 0 - and the
    candidate was skipped as "1 board lot exceeds the position size". Nothing
    caught it because reqContractDetails had been FAILING and falling back to
    lot 1: the US path worked by accident until the call started succeeding.
    The last automated US entry was 21 August.

    Japan is the genuine case and is unchanged: TSE will not trade 17 shares of
    6098, so skipping when one lot exceeds the position size is correct there.
    HKD keeps consulting IB because SEHK lots really do vary by stock.

    Returns 0 - UNKNOWN - when a board-lot venue's lot cannot be read. There is
    no HK number to fall back on: HKEX sets the lot per stock (2359 is 100,
    2269 is 500, 1810 is 200 - verified against HKEX's own List of Securities),
    so the JPY-style "assume 100" would have sized 2269 at 300 shares, an odd
    lot that cannot auto-match on SEHK. It would have sat unfilled holding a
    position slot, against an HKD cash balance of 7. The caller must skip.
    """
    key = ("lot", getattr(contract, "conId", 0) or contract.symbol)
    if key in _TICK_CACHE:
        return _TICK_CACHE[key]
    lot = 1
    ccy = str(getattr(contract, "currency", "") or "").upper()
    if ccy == "HKD":
        # HKEX is the authority. Ask IB too, purely to notice a disagreement:
        # if the two ever diverge the exchange wins, because an order rounded
        # to anything but the real lot is an odd lot and will not auto-match.
        hkex = hk_board_lot(getattr(contract, "symbol", ""))
        if hkex:
            try:
                cds = ib.reqContractDetails(contract)
                ms = cds and (getattr(cds[0], "sizeIncrement", None)
                              or getattr(cds[0], "minSize", None))
                if ms and int(float(ms)) != hkex:
                    log(f"  note {contract.symbol}: IB says lot {int(float(ms))}, "
                        f"HKEX says {hkex} — using HKEX")
            except Exception:
                pass
            _TICK_CACHE[key] = hkex
            return hkex
        log(f"  {getattr(contract, 'symbol', '?')}: not in the HKEX board-lot "
            f"table — treating the lot as unknown")
        return 0
    if ccy in _BOARD_LOT_CCY:
        try:
            cds = ib.reqContractDetails(contract)
            if cds:
                ms = (getattr(cds[0], "sizeIncrement", None)
                      or getattr(cds[0], "minSize", None))
                if ms and ms == ms and float(ms) >= 1:
                    lot = int(float(ms))
        except Exception:
            pass
        if lot <= 1 and ccy == "JPY":
            lot = 100          # TSE has been a flat 100 shares since Oct 2018
        if lot <= 1:
            # SEHK only. Either IB did not answer, or it answered 1 - and SEHK
            # trades no equity in 1-share lots, so 1 is not an answer either.
            # NOT cached: a transient reqContractDetails failure must not
            # freeze the symbol as unknown for the rest of the run.
            return 0
    _TICK_CACHE[key] = lot
    return lot


def jp_tick(price):
    """TSE price-step table (coarse/non-TOPIX500 grid — always exchange-valid;
    IB's minTick for JP stocks is often wrong, e.g. 0.1 at ¥24,700)."""
    for lim, t in ((3000, 1), (5000, 5), (30000, 10), (50000, 50),
                   (300000, 100), (500000, 500), (3000000, 1000)):
        if price <= lim:
            return t
    return 5000


def hk_tick(price):
    """HKEX spread table - Rules of the Exchange, Second Schedule, Part A.

    The tick GROWS with price (0.001 below HK$0.25, 0.10 between HK$100 and
    HK$200), so IB's minTick - the FIRST band of a tiered ladder, which
    broker.py reads at incrementRules[0] - is not a legal increment anywhere
    above HK$0.25. Every one of the 2,783 SEHK equities in HKEX's List of
    Securities is Part A, so one table covers the market.
    """
    for lim, t in ((0.25, 0.001), (10.0, 0.005), (20.0, 0.01), (50.0, 0.02),
                   (100.0, 0.05), (200.0, 0.1), (500.0, 0.2), (1000.0, 0.5),
                   (2000.0, 1.0), (5000.0, 2.0), (9995.0, 5.0)):
        if price <= lim:
            return t
    return 5.0


# Currencies whose venues price shares on the MiFID II RTS 11 tick regime: the
# EU/EEA markets and SIX, which applies the same Annex and ESMA's bands. GBP is
# deliberately absent (LSE pence scaling is parked), and USD, HKD and JPY keep
# their own rules byte-for-byte. Under IB_BACKEND=web only EUR reaches place()
# today: the other four have no exchange mapping in ib_orders yet.
EU_TICK_CCY = frozenset(("EUR", "CHF", "DKK", "SEK", "NOK"))

# RTS 11 - Commission Delegated Regulation (EU) 2017/588, Annex - the column for
# the MOST liquid band (average daily number of transactions >= 9,000), checked
# against the EUR-Lex text (CELEX:32017R0588) on 2026-09-17. Art. 2 lets a venue
# apply a tick "equal to or greater than" the Annex value for the share's band,
# and band 6 is the finest value in every row, so NO EU venue may quote a finer
# tick than this: a limit on this grid is never finer than legal. It is a floor,
# not the answer - a band-5 name at 1,569 ticks 0.5, not 0.2 - which is why IB's
# bands and the refusal text still get a say. Ranges are lower-inclusive
# ("20 <= price < 50"), unlike hk_tick's upper-inclusive HKEX table.
_RTS11_BAND6 = ((0.1, 0.0001), (0.2, 0.0001), (0.5, 0.0001), (1.0, 0.0001),
                (2.0, 0.0002), (5.0, 0.0005), (10.0, 0.001), (20.0, 0.002),
                (50.0, 0.005), (100.0, 0.01), (200.0, 0.02), (500.0, 0.05),
                (1000.0, 0.1), (2000.0, 0.2), (5000.0, 0.5), (10000.0, 1.0),
                (20000.0, 2.0), (50000.0, 5.0))


def eu_floor_tick(price):
    """RTS 11 band-6 tick at `price`: the finest tick any EU venue may use.

    Live DBK and BAYN first went out on IB's lowest band, 0.0001, which no RTS 11
    venue allows as a tick at any price of 1 or more. Each wasted a refusal, and
    a name ticking 0.5 could burn all six attempts before the ladder got there.
    """
    for upper, t in _RTS11_BAND6:
        if price < upper - 1e-9:
            return t
    return 10.0


def snap_to_tick(raw, tick):
    lim = round(raw / tick) * tick
    if tick >= 1:
        return int(round(lim))
    return round(lim, 2 if tick >= 0.01 else 4 if tick >= 0.0001 else 6)


def _on_grid(price, tick):
    q = price / tick
    return abs(round(q) - q) < 1e-6


def _common_grid(a, b):
    """The finest tick that is a multiple of both a and b (0.03, 0.1 -> 0.3)."""
    x, y = int(round(a * 1e8)), int(round(b * 1e8))
    if x <= 0 or y <= 0:
        return max(a, b)
    return x * y // math.gcd(x, y) / 1e8


def eu_limit(raw, base_tick, bands):
    """(tick, limit) for an RTS 11 venue: the coarsest of IB's minTick, the band-6
    floor and IB's own band, each read at the price.

    The tick depends on the price, and snapping MOVES the price, so the limit is
    checked against the range it LANDS in, not the one `raw` started in. On the
    Annex's own grid that never bites - every range edge (1, 2, 5, 10, ...) is a
    multiple of the ticks on both sides - but an IB band edge need not be. When
    the landing range wants a coarser tick the price is re-snapped on a grid
    common to both, so it is legal whichever side of the edge it settles on.
    Bounded: anything still off-grid is left to the Error-110 retry.
    """
    def tick_at(p):
        return max(base_tick, eu_floor_tick(p), band_tick(bands, p))

    tick = tick_at(raw)
    lim = snap_to_tick(raw, tick)
    for _ in range(4):
        landed = tick_at(lim)
        if _on_grid(lim, landed):
            break
        tick = _common_grid(tick, landed)
        lim = snap_to_tick(raw, tick)
    return tick, lim


# IB's refusal names the increment it wanted, and broker._translate_error keeps
# that text: "The price 48.4108 does not conform to the minimum price variation
# of 0.01 for this instrument." ib_async's socket Error 110 carries no number,
# so there the rung ladder still does the work.
_STATED_TICK = re.compile(r"minimum price variation of ([0-9.]+)")


def ib_stated_tick(err):
    """The increment IB's Error-110 text asks for, or 0.0 if it names none."""
    m = _STATED_TICK.search(str(err or ""))
    if not m:
        return 0.0
    try:
        t = float(m.group(1).rstrip("."))   # "... variation of 0.01." ends a sentence
    except ValueError:
        return 0.0
    return t if 0 < t < float("inf") else 0.0


def _retry_price(raw, tick, err, refused, ladder):
    """(tick, limit) for the attempt after an Error-110 refusal; limit None when
    there is no new price worth sending.

    IB's stated increment, when it is coarser than the current tick, is used
    exactly: the rungs replayed BAYN as 48.4108 -> 48.411 -> 48.41, one refusal
    more than needed, and have no rung at all for 0.02, 2, 20 or 200 (a 0.02
    name can end on the 0.1 rung, up to 5 cents from the raw price). Otherwise
    the next rung. Either way a price IB
    already refused in this call is never resent - 0.05 and 0.1 both snap 1576.9
    to 1576.9 - so the walk moves on to a coarser rung without spending one of
    the six submissions. A non-positive price is never a legal answer (a SELL
    limit of 0 sells at any price), so the walk stops there.
    """
    stated = ib_stated_tick(err)
    if stated > tick:
        tick = stated
    else:
        coarser = [t for t in ladder if t > tick]
        if not coarser:
            return tick, None
        tick = coarser[0]
    lim = snap_to_tick(raw, tick)
    while any(abs(lim - r) < 1e-9 for r in refused):
        coarser = [t for t in ladder if t > tick]
        if not coarser:
            return tick, None
        tick = coarser[0]
        lim = snap_to_tick(raw, tick)
    if lim <= 0:
        return tick, None
    return tick, lim


def live_base_price(ib, contract, fallback):
    """IB's own view of the price (last trade, else prior close). Guards limit
    prices against stale signal cards — Yahoo's EU end-of-day bars can lag past
    midnight, seen live 24 Jul: MC sell limit priced off a day-old close sat 4%
    above the market and could never fill."""
    try:
        [tk] = ib.reqTickers(contract)
        for v in (tk.last, tk.close, tk.marketPrice()):
            if v and v == v and v > 0:
                return float(v)
    except Exception:
        pass
    return fallback


def place(ib, contract, action, qty, price, dry, reason="", mkt=False, quote=None):
    """quote: IB's price the caller already read and sized the order on (the
    entry's value guard). Used instead of asking IB again, so the limit is set
    from the very quote the share count was checked against - a second read
    that came back wild would otherwise re-base the limit far past what the
    guard and the per-order cap allowed."""
    if qty <= 0:
        return
    base = quote if quote is not None else live_base_price(ib, contract, price)
    if price > 0 and abs(base - price) / price > 0.01:
        log(f"  signal price {price} stale vs IB quote {base} — re-based")
        price = base
    # Regime-break exits go market-at-open: the backtest's exit price IS the
    # next open, and a close-anchored sell limit misses on any down-gap
    # (MC failed to exit two days running before this).
    if mkt and contract.secType == "STK":
        log(f"{action} {qty} {contract.symbol} @ MKT-open ({contract.currency})")
        if dry or not confirm(f"{action} {qty} {contract.symbol} @ MKT"):
            return
        trade = ib.placeOrder(contract, MarketOrder(action, qty, tif="DAY"))
        ib.sleep(3)
        status, err = _order_verdict(trade)
        status = _stock_status(status)
        if status == "REJECTED":
            log(f"  !! ORDER REJECTED: {action} {qty} {contract.symbol} — {err[:140]}")
        from datetime import datetime, timezone
        PLACED.append({"time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                       "action": action, "qty": qty, "symbol": contract.symbol,
                       "limit": "MKT-open", "ccy": contract.currency,
                       "reason": reason, "status": status, "error": err[:160]})
        return status
    raw = price * (1 + LIMIT_BUFFER) if action == "BUY" else price * (1 - LIMIT_BUFFER)
    tick = min_tick(ib, contract)
    if contract.currency == "JPY":
        tick = max(tick, jp_tick(raw))
    elif contract.currency == "HKD":
        tick = max(tick, hk_tick(raw))
    if contract.currency in EU_TICK_CCY:
        # IB's minTick is its lowest band, 0.0001, not a legal tick at any price
        # of 1 or more: DBK and BAYN were each refused on it before a legal
        # price went out. Price on the RTS 11 floor and IB's own band instead.
        tick, lim = eu_limit(raw, tick, ib_price_bands(ib, contract))
    else:
        lim = snap_to_tick(raw, tick)
    log(f"{action} {qty} {contract.symbol} @ ~{lim} ({contract.currency})")
    if dry or not confirm(f"{action} {qty} {contract.symbol} @ {lim}"):
        return
    # place; if the venue rejects the price step (Error 110), self-heal by
    # retrying at IB's stated increment, else the next coarser tick from the
    # ladder (covers venues where IB's minTick metadata is wrong — seen on TSE
    # and Euronext).
    ladder = [0.0001, 0.001, 0.01, 0.05, 0.1, 0.2, 0.5, 1, 5, 10, 50, 100, 500, 1000]
    status, err = "", ""
    # IBKR's percentage-constraint warning may be confirmed only when `lim` is
    # still the price OUR OWN policy produced: reference * (1 + LIMIT_BUFFER),
    # plus one tick for the snap. The bound is LIMIT_BUFFER rather than an
    # invented number because that is the tolerance this system actually has -
    # the backtest models slip as COST_BP per side (US 10bp, JP 15bp, HK 25bp),
    # and the 50bp buffer already exceeds every one of them. Anything wider
    # than the buffer means something re-priced the order, and that is exactly
    # when a price warning should stop it.
    #
    # Safe for a BUY because the limit is a CEILING: a stale reference costs a
    # missed fill or a cheaper one, never a worse price than `lim`. Never for a
    # SELL, where the limit is a FLOOR and staleness sells cheap.
    allow_cap = (action == "BUY" and price > 0
                 and lim <= price * (1.0 + LIMIT_BUFFER) + tick)
    if action == "BUY" and not allow_cap:
        log(f"  note: limit {lim} exceeds the {LIMIT_BUFFER:.2%} buffer over "
            f"{price} — a price-cap warning will be declined")
    sent_lim = lim
    refused = []                      # prices IB refused in THIS call
    for attempt in range(6):
        order = LimitOrder(action, qty, lim, tif="DAY")
        order.allow_price_cap = allow_cap
        trade = ib.placeOrder(contract, order)
        sent_lim = lim                # the price IB actually saw on this attempt
        ib.sleep(3)                   # give IB a moment to accept or reject
        status, err = _order_verdict(trade)
        # Only a genuine price-increment refusal walks the ladder: one that
        # STARTS with ib_async's "Error 110, reqId N: " (socket), or the same
        # shape broker._translate_error gives every web tick refusal. It was
        # the bare substring '110' anywhere, so a refusal echoing a cOID stamped
        # 2026-11-0x, a conid, an ib_async orderId, a quantity of 110 or a
        # price of 110.xx was re-sent under a new cOID - a second live order if
        # IB had in fact taken the first (board review 2026-09-21). The comma
        # keeps out "Error 1100, reqId -1: Connectivity ... lost".
        if status != "REJECTED" or not re.match(r"Error 110, reqId -?\d+: ", err):
            break
        refused.append(lim)
        if attempt == 5:
            # Out of attempts. Re-pricing here would log a retry that never
            # happens and record a limit IB never saw.
            break
        tick, lim = _retry_price(raw, tick, err, refused, ladder)
        if lim is None:
            break                     # no untried price left; sent_lim stands
        log(f"  retrying with coarser tick {tick} -> {lim}")
    status = _stock_status(status)
    if status == "REJECTED":
        log(f"  !! ORDER REJECTED: {action} {qty} {contract.symbol} — {err[:140]}")
    from datetime import datetime, timezone
    PLACED.append({"time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                   "action": action, "qty": qty, "symbol": contract.symbol,
                   "limit": sent_lim, "ccy": contract.currency, "reason": reason,
                   "status": status, "error": err[:160]})
    return status


def _stock_status(status):
    """'sent' for a stock order whose outcome is not yet known.

    place() reads the verdict three seconds after transmitting, and the runs
    that matter place DAY orders BEFORE their session opens - so "not filled
    yet" is the expected answer, not news. Recording that as 'pending' would
    stamp almost every stock order pending FOREVER, because the activity log is
    append-only (publish_state does prev.activity + PLACED) and no later run
    revises an old row. 'sent' claims only what is actually known: it reached
    IB, and Positions is where you see what filled. FX keeps 'pending', because
    there _fx_order really does wait for the fill and act on the answer.
    """
    return "sent" if status == "pending" else status


def _order_verdict(trade):
    """('filled'|'pending'|'REJECTED', error_message) for a just-placed trade.

    'pending' is NOT success. IB accepts an order long before it fills - and
    accepts it just as willingly into a market that is CLOSED, which is how
    three FX conversions and a TSE buy were all recorded 'ok' at 23:35 UTC on
    2026-09-04 (08:35 JST Saturday) while the fills ledger recorded none of
    them. Anything that needs the money to have actually MOVED - FX funding -
    must require 'filled'; the dashboard shows the rest as pending rather than
    implying an execution that has not happened.
    """
    try:
        st = trade.orderStatus.status
        if st in ("Cancelled", "ApiCancelled", "Inactive"):
            msgs = [e.message for e in trade.log if e.message]
            return "REJECTED", (msgs[-1] if msgs else st).strip()
        return ("filled" if st == "Filled" else "pending"), ""
    except Exception:
        # Unreadable status reports pending, never filled: cash that cannot be
        # confirmed must not be spent as though it had arrived.
        return "pending", ""


# ---------------- FX rates & funding ----------------
# IB (IDEALPRO) only quotes certain pairs directly (USDHKD yes, JPYHKD no).
# We resolve any A->B rate by trying the direct pair, its inverse, then a USD
# cross, and we FUND any currency the same way — converting through USD when no
# direct pair exists — instead of skipping the trade.
_RATE_CACHE = {}

# The last LIVE rate per pair, kept across runs. IB's exchange-rate endpoint
# answers nothing while the FX market is shut (Friday evening to Sunday evening),
# and every Friday and Saturday run then skipped EVERY non-HKD entry with "no
# USD/HKD rate to size order" - US stocks already paid for in USD and 24/7 crypto
# included. Seen 2026-09-04, 09-05 and 09-12. An order's SIZE only needs a close
# rate, so a recent remembered one is used for that. Anything that MOVES money
# (fund_from_nonbase, convert_into) or is recorded for tax (fills_capture) asks
# fx_rate_live instead, and never sees a remembered rate.
FX_LAST_GOOD = Path(os.environ.get("MPS_FX_LAST_GOOD", "/root/fx_last_good.json"))
FX_STALE_MAX_H = 96          # Friday's last live rate still covers Monday morning
_STALE_RATES = set()         # (a, b) pairs answered from memory in this run
_FX_REMEMBER = False         # run() arms it for live runs; --dry and tests never write


def _load_last_good():
    try:
        return json.loads(FX_LAST_GOOD.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _remember_rate(a, b, r):
    if not _FX_REMEMBER:
        return
    from datetime import datetime, timezone
    try:
        book = _load_last_good()
        book[f"{a}/{b}"] = {"rate": r, "at": datetime.now(timezone.utc)
                            .strftime("%Y-%m-%dT%H:%M:%SZ")}
        tmp = FX_LAST_GOOD.with_suffix(".tmp")
        tmp.write_text(json.dumps(book, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, FX_LAST_GOOD)
    except Exception:
        pass                     # a convenience only; this run has its live rate


def _remembered_rate(a, b):
    """(rate, age in hours) of the last live a->b rate, or (0.0, None) if none
    is younger than FX_STALE_MAX_H. A stored b->a rate is inverted."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    book = _load_last_good()
    for key, invert in ((f"{a}/{b}", False), (f"{b}/{a}", True)):
        try:
            row = book[key]
            r = float(row["rate"])
            at = datetime.strptime(row["at"], "%Y-%m-%dT%H:%M:%SZ") \
                .replace(tzinfo=timezone.utc)
        except Exception:
            continue
        age_h = (now - at).total_seconds() / 3600
        if r > 0 and r == r and -1 <= age_h <= FX_STALE_MAX_H:
            return (1.0 / r if invert else r), max(age_h, 0.0)
    return 0.0, None


def _pair_mid(ib, pair):
    """(Forex, midpoint) for a 6-char pair, or (None, None) if not quotable."""
    try:
        fx = Forex(pair)
        if not ib.qualifyContracts(fx):
            return None, None
        [t] = ib.reqTickers(fx)
        m = t.midpoint()
        if not m or m != m:                      # NaN -> fall back to last close
            m = t.close
        if not m or m != m or m <= 0:
            return None, None
        return fx, m
    except Exception:
        return None, None


def fx_rate(ib, a, b):
    """Units of <b> per 1 unit of <a> (0.0 if unobtainable). Cached per run.

    When IB quotes nothing (the FX market is shut) this falls back to the last
    live rate, if younger than FX_STALE_MAX_H, and marks the pair stale. Good
    enough to SIZE an order; use fx_rate_live for anything that moves money."""
    if a == b:
        return 1.0
    if (a, b) in _RATE_CACHE:
        return _RATE_CACHE[(a, b)]
    r, stale = 0.0, False
    if _pair_mid(ib, a + b)[1]:                   # Forex(ab) quotes b per a
        r = _pair_mid(ib, a + b)[1]
    elif _pair_mid(ib, b + a)[1]:                 # Forex(ba) quotes a per b -> invert
        r = 1.0 / _pair_mid(ib, b + a)[1]
    elif a != "USD" and b != "USD":               # cross via USD
        ra, rb = fx_rate(ib, a, "USD"), fx_rate(ib, "USD", b)
        r = ra * rb if (ra and rb) else 0.0
        stale = (a, "USD") in _STALE_RATES or ("USD", b) in _STALE_RATES
    if r and not stale:
        _remember_rate(a, b, r)                   # only live rates are remembered
    elif not r:
        r, age_h = _remembered_rate(a, b)
        if r:
            stale = True
            log(f"  no live {a}/{b} rate (FX market shut?) - using the last live "
                f"rate {r:.6g} from {age_h:.0f}h ago, for sizing only")
    if stale:
        _STALE_RATES.add((a, b))
    _RATE_CACHE[(a, b)] = r
    return r


def fx_rate_live(ib, a, b):
    """fx_rate, but 0.0 unless the rate is live right now.

    For conversions and the tax ledger. A remembered rate can be days old: a
    conversion sized on it would miss "only what the order needs", and it could
    not fill while the market is shut anyway."""
    r = fx_rate(ib, a, b)
    return 0.0 if (a, b) in _STALE_RATES else r


def warm_fx_memory(ib, actions):
    """Record a live rate for every currency this run might need to size, so a
    weekend run finds one remembered even for a market with no weekday entry.
    On a weekend the same calls simply load the remembered rates."""
    ccys = {"USD", "EUR", "JPY", "GBP"}
    try:
        ccys |= set(cash_by_ccy(ib))
    except Exception:
        pass
    for a in actions:
        try:
            ccys.add(currency_of(a["symbol"]))
        except Exception:
            pass
    for c in sorted(ccys - {BASE_CCY}):
        try:
            fx_rate(ib, c, BASE_CCY)
        except Exception:
            pass


# An order still live at IB. openTrades() returns the whole day's book - filled
# and cancelled rows included - so every consumer must filter on this or it will
# read history as "in flight". Defined once because _fx_already_working was
# written without it while open_syms and pending_buys had it, and that
# divergence is exactly what board review caught.
_WORKING_STATUS = ("PendingSubmit", "PreSubmitted", "Submitted", "ApiPending")

# Pairs (by conid) and target currencies with a conversion already in flight,
# plus the source cash those unfilled orders have already spoken for.
# Cleared at the top of run() so nothing leaks between runs in one process.
_FX_PENDING = set()
_FX_PENDING_CCY = set()
_FX_COMMITTED = {}
# {"ok": bool} once reserve_working_cash has run: True only when the order book
# was read AND every working order's cash could be priced and reserved. The
# bot's own HKD is sold only when it is True (_own_base_spendable): an HKD claim
# that went unreserved could otherwise be sold out from under a working HK buy.
_RESERVE_OK = {}


def _fx_already_working(ib, conid, sym):
    """True when a conversion on this pair is already in flight.

    Two sources, because either alone leaves a hole:
      - _FX_PENDING catches one placed moments ago in THIS run.
      - openTrades() catches one left working by an EARLIER run. placeOrder
        invalidates that cache, so it re-reads rather than serving a stale book.

    Without this the bot reconverts on every run while the first order sits
    unfilled: the cash never arrives, so the shortfall never closes. Five runs
    separate Friday's 23:35 from Monday's Tokyo open (23:35 and 09:00 daily),
    every one of them able to stack another conversion. The equivalent guard
    for stocks is pending_buys, which excludes secType CASH - correctly, since
    an FX order should not consume a position slot, but the effect was that FX
    had no duplicate check at all.
    """
    if conid and conid in _FX_PENDING:
        return True
    try:
        for t in ib.openTrades():
            if getattr(t.contract, "secType", "") != "CASH":
                continue
            if getattr(t.orderStatus, "status", "") not in _WORKING_STATUS:
                continue          # Filled/Cancelled/Inactive are history, not flight
            if conid and str(getattr(t.contract, "conId", "")) == str(conid):
                return True
            # Only an EXACT pair match. An earlier version also matched the bare
            # base currency, but openTrades rows carry the base as their symbol
            # ("USD"), so that collapsed to "any working USD pair" and one
            # USD.JPY order would have blocked USD.CHF, USD.CAD and USD.SGD too.
            # conid is present on real order rows, so this loses nothing.
            tsym = str(getattr(t.contract, "symbol", "")).upper()
            if tsym and sym and tsym == sym.upper():
                return True
    except Exception as e:
        # openTrades raises rather than reporting an empty book. Funding is
        # optional; an unreadable book must not read as "nothing is working".
        log(f"  ! cannot read working orders ({str(e)[:80]}) — assuming an FX "
            f"order is in flight, not converting")
        return True
    return False


def _own_base_sale(qty, cap):
    """True when selling `qty` BASE_CCY stays inside the bot's own-HKD cap.

    cap is what _own_base_spendable allowed for THIS conversion; None means the
    caller passed no cap, which is every path but the own-pocket source."""
    try:
        return cap is not None and 0 < float(qty) <= float(cap) + 1e-6
    except (TypeError, ValueError):
        return False


def _fx_order(ib, base_ccy, quote_ccy, side, qty, dry, why, target=None,
              src_ccy=None, src_qty=0.0, base_sell_cap=None):
    """Market FX order on Forex(base+quote): side BUY/SELL of `qty` base units.

    Returns True only when the conversion FILLED. An accepted-but-unfilled
    order has moved no money, and the caller must not size a stock order
    against currency that has not arrived.

    base_sell_cap: the bot's own BASE_CCY this order may sell (see
    _own_base_spendable). Only fund_from_nonbase's own-pocket source passes it.
    """
    qty = int(round(qty))
    if qty <= 0:
        return True
    # Independent guard on selling BASE_CCY, at the point of order construction,
    # so that editing a caller cannot quietly widen it. Lifted in two cases only:
    # FX_CONVERT explicitly on (off since 2026-07-30 and never the way to do
    # this - see its comment), or an own-pocket sale: src_ccy is BASE_CCY and
    # the HKD it spends is within the cap the pocket allowed. Anything else that
    # would sell HKD - the operator's transfer funding - is refused here.
    sells_base = ((side == "SELL" and base_ccy == BASE_CCY)
                  or (side == "BUY" and quote_ccy == BASE_CCY))
    own_sale = (sells_base and src_ccy == BASE_CCY
                and _own_base_sale(src_qty, base_sell_cap))
    if sells_base and not FX_CONVERT and not own_sale:
        log(f"  !! refusing to sell {BASE_CCY} ({side} {base_ccy}.{quote_ccy}, {why})"
            f" — FX_CONVERT is off and this is not within the bot's own {BASE_CCY}")
        return False
    fx = Forex(base_ccy + quote_ccy)
    if not ib.qualifyContracts(fx):
        return False
    conid = getattr(fx, "conId", 0)
    pair = f"{base_ccy}.{quote_ccy}"
    if _fx_already_working(ib, conid, pair):
        # Mark the TARGET too. Without this the block is defeated: _fx_order
        # returns False before ever reaching the code that records the pending
        # currency, so fund_from_nonbase falls through and converts a second
        # source (EUR, GBP) for the same shortfall - the exact duplication this
        # guard exists to stop, just from a different balance.
        if target:
            _FX_PENDING_CCY.add(target)
        log(f"  FX {pair} already working — not converting again ({why})")
        return False
    log(f"  FX {side} {qty} {pair} ({why})")
    if dry or not confirm(f"FX {side} {qty} {base_ccy}{quote_ccy}"):
        # `not dry or True` used to sit here: a tautology, True on both branches.
        # It was harmless while the caller ignored this value, but the return now
        # means "the money arrived", so a DECLINED conversion was reporting
        # success and the stock order went ahead against currency the operator
        # had just refused to buy. In dry, treat as satisfied; a live decline is
        # a refusal.
        if dry and own_sale:
            # The preview spends its own pocket as a live run would, in memory
            # only, so its next candidate sees what is left - nothing is written.
            _pocket_spend(src_qty)
        return bool(dry)
    trade = ib.placeOrder(fx, MarketOrder(side, qty))
    # Give a fill longer than the 3s a stock order gets: the caller now treats
    # "not filled" as "not funded" and skips the entry, so a slow status report
    # would cost a trade outright. A spot market order that is going to fill
    # does so in well under a second, making this free on the happy path.
    status, err = "pending", ""
    for _ in range(5):
        ib.sleep(2)
        status, err = _order_verdict(trade)
        if status != "pending":
            break
    if status == "REJECTED":
        log(f"  !! FX ORDER REJECTED: {side} {qty} {pair} — {err[:140]}")
    elif status != "filled":
        # Accepted but unfilled: the venue is shut, or IB is slow. Register the
        # pair so nothing converts it again, and report failure so no stock
        # order is sized against money that has not landed.
        if conid:
            _FX_PENDING.add(conid)
        if target:
            _FX_PENDING_CCY.add(target)
        if src_ccy and src_qty > 0:
            # Reserve the cash this unfilled order will consume. cash_by_ccy
            # reads the LIVE balance, which still shows money the order has
            # already spoken for, so a second target currency in the same run
            # would otherwise spend the same USD twice.
            _FX_COMMITTED[src_ccy] = _FX_COMMITTED.get(src_ccy, 0.0) + src_qty
        log(f"  FX {pair} accepted but NOT filled — treating as unfunded; the "
            f"order stays working and the entry waits for the cash")
    if status == "filled" and target == BASE_CCY:
        _pocket_add_fill(ib, base_ccy, quote_ccy, side, qty)
    if status == "filled" and own_sale:
        # An UNFILLED own sale needs nothing here: src_ccy/src_qty above already
        # reserved its HKD in _FX_COMMITTED, which the pocket file reports as
        # pending and the next candidate subtracts.
        _pocket_spend(src_qty)
    from datetime import datetime, timezone
    PLACED.append({"time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                   "action": f"FX {side}", "qty": qty,
                   "symbol": f"{base_ccy}.{quote_ccy}", "limit": "MKT",
                   "ccy": quote_ccy, "reason": why,
                   "status": status, "error": err[:160]})
    return status == "filled"


def _pocket_add_fill(ib, base_ccy, quote_ccy, side, qty):
    """A conversion into BASE_CCY filled in THIS run: the bot's pocket grew.

    The start-of-run sweep cannot see it, and without this the next HK candidate
    in the same run would find its own freshly bought HKD outside the pocket
    and convert again. An ORDER-SIZE estimate, not the fill: qty itself when
    BASE_CCY is the pair's base (BUY HKD.xxx), qty x rate when it is the quote
    (SELL USD.HKD). No rate means nothing is added - under-counting the pocket
    only stops the bot spending, it never spends the pot. The next run re-sweeps
    the real stamped execution, so the estimate never outlives this run.
    """
    if _run_pocket() is None:
        return
    got = 0.0
    try:
        if base_ccy == BASE_CCY and side == "BUY":
            got = float(qty)
        elif quote_ccy == BASE_CCY and side == "SELL":
            r = fx_rate(ib, base_ccy, BASE_CCY)
            got = float(qty) * r if (r and r == r and r > 0) else 0.0
    except Exception:
        got = 0.0
    if got > 0:
        _POCKET_RUN["p"] = _POCKET_RUN["p"] + got
        log(f"  bot's own {BASE_CCY} pocket +{got:,.0f} from this conversion "
            f"-> {_POCKET_RUN['p']:,.0f}")


def _pocket_spend(amount):
    """An own-pocket sale of BASE_CCY filled in THIS run (or, under --dry, would
    have): the bot's pocket shrank. The mirror of _pocket_add_fill.

    Without it the next candidate in the same run would read the same P and sell
    it again - and since that HKD is gone, the second sale would come out of the
    operator's pot. The end-of-run pocket file would overstate P too. 1% over
    the order-size estimate covers the spread and commission it does not see;
    under-counting P only ever stops spending. The next run re-sweeps the real
    stamped execution, so the estimate never outlives this run."""
    if _run_pocket() is None:
        return
    try:
        spent = float(amount) * 1.01
    except (TypeError, ValueError):
        return
    if spent > 0:
        _POCKET_RUN["p"] = _POCKET_RUN["p"] - spent
        log(f"  bot's own {BASE_CCY} pocket -{spent:,.0f} for this conversion "
            f"-> {_POCKET_RUN['p']:,.0f}")


def convert_into(ib, ccy, need_ccy, dry):
    """Acquire ~need_ccy units of <ccy>, paying from BASE_CCY. Uses a direct pair
    if one exists, else routes through USD (BASE->USD->ccy). Returns False only
    when NO path exists (caller then leaves the order to under-fund = safe)."""
    if ccy == BASE_CCY or need_ccy <= 0:
        return True
    if _pair_mid(ib, ccy + BASE_CCY)[1]:          # BUY ccy, pay BASE  (e.g. USDHKD)
        return _fx_order(ib, ccy, BASE_CCY, "BUY", need_ccy, dry, f"{BASE_CCY}->{ccy}")
    inv = _pair_mid(ib, BASE_CCY + ccy)[1]        # pair is BASE/ccy -> SELL BASE for ccy
    if inv:
        return _fx_order(ib, BASE_CCY, ccy, "SELL", need_ccy / inv, dry, f"{BASE_CCY}->{ccy}")
    # no direct pair (e.g. JPY/HKD): go BASE -> USD -> ccy
    usd_per_ccy = fx_rate_live(ib, ccy, "USD")
    if not usd_per_ccy:
        return False
    need_usd = need_ccy * usd_per_ccy
    if not _fx_order(ib, "USD", BASE_CCY, "BUY", need_usd * 1.02, dry, f"{BASE_CCY}->USD"):
        return False
    if _pair_mid(ib, ccy + "USD")[1]:             # BUY ccy paying USD
        return _fx_order(ib, ccy, "USD", "BUY", need_ccy, dry, f"USD->{ccy}")
    if _pair_mid(ib, "USD" + ccy)[1]:             # pair USD/ccy (e.g. USDJPY) -> SELL USD
        return _fx_order(ib, "USD", ccy, "SELL", need_usd, dry, f"USD->{ccy}")
    return False


def fund_from_nonbase(ib, ccy, short_ccy, dry, buffer=1.02):
    """Buy ~short_ccy of <ccy> using cash held in ANY other non-base currency,
    or the bot's OWN BASE_CCY.

    Never sells HKD that is not provably the bot's. The HKD BALANCE is not a
    source - it carries the operator's transfer funding - but since 2026-10-26
    (owner decision) the bot's own stamped pocket is: (BASE_CCY, own) joins the
    sources when <ccy> is not BASE_CCY, with `own` from _own_base_spendable
    (0 - and so absent - whenever the pocket is unknown, the earmark is not
    frozen or a working order's cash went unreserved). That amount travels with
    the order as base_sell_cap, and _fx_order_pair and _fx_order each refuse a
    BASE_CCY sale above it on their own: three guards still, because one being
    edited away must not silently widen what may be sold.

    Sources are tried largest first BY VALUE in BASE_CCY, so the single balance
    most able to cover the shortfall is used rather than fragmenting across
    several conversions. It used to be by raw units in each source's own
    currency, which put JPY 83,346 (~HK$4.4k) ahead of USD 4,297 (~HK$33.5k).

    `buffer` is carried by the currency that ARRIVES: short_ccy x buffer lands
    in <ccy> whichever way IB quotes the pair (see _fx_order_pair).
    Returns True only if a conversion was actually placed.
    """
    if ccy in _FX_PENDING_CCY:
        # Something is already on its way into this currency - an earlier
        # candidate this run, or a previous run's order that
        # reserve_working_cash found still working. A second conversion from a
        # DIFFERENT source would not match _fx_already_working's pair test, and
        # both would land: twice the currency for one shortfall.
        log(f"  a conversion into {ccy} is already working; not starting another")
        return False
    balances = cash_by_ccy(ib)
    sources = [(c, amt - _FX_COMMITTED.get(c, 0.0)) for c, amt in balances.items()
               if c not in (BASE_CCY, ccy)
               and (amt - _FX_COMMITTED.get(c, 0.0)) > 0]
    if ccy != BASE_CCY:
        own = _own_base_spendable(ib, balances)
        if own > 0:
            sources.append((BASE_CCY, own))
    if not sources:
        log(f"  no non-{BASE_CCY} cash to fund {ccy}; skipping conversion")
        return False
    for src, have in sorted(sources, key=lambda x: _source_rank(ib, *x)):
        rate = fx_rate_live(ib, ccy, src)         # src units per 1 ccy
        if not rate or rate <= 0:
            if (ccy, src) in _STALE_RATES:
                log(f"  no live {ccy}/{src} rate (FX market shut?); not "
                    f"converting on a remembered rate")
            continue
        need_src = short_ccy * rate * buffer      # buffer for slippage/fees
        # The buffer belongs on what ARRIVES, and both sides must carry it.
        # _fx_order_pair orders whichever side is the pair's base: SELL
        # need_src when the source is the base (USD.HKD), BUY need_dst when the
        # target is (EUR.USD, HKD.JPY). Passing the bare short_ccy here dropped
        # the buffer on every BUY-side pair - FX BUY 1539 EUR.USD on 2026-09-12
        # was the shortfall exactly, and HKD bought from JPY got no 3%.
        need_dst = short_ccy * buffer
        if have < need_src:
            log(f"  {src} {have:,.0f} short of the {need_src:,.0f} needed to fund {ccy}")
            continue
        log(f"  funding {ccy} from {src}: converting ~{need_src:,.0f} {src} for "
            f"~{need_dst:,.0f} {ccy} (shortfall {short_ccy:,.0f} x {buffer:g})"
            + (f" - the bot's own {BASE_CCY}, at most {have:,.0f}"
               if src == BASE_CCY else ""))
        # The cap rides only on the own-pocket source, so every other source
        # reaches _fx_order_pair exactly as before (no cap: HKD refused there).
        kw = {"base_sell_cap": have} if src == BASE_CCY else {}
        if _fx_order_pair(ib, src, ccy, need_src, need_dst, dry, **kw):
            return True
        if ccy in _FX_PENDING_CCY:
            # Working but unfilled is NOT a failed source. Falling through here
            # would convert a second balance for the same shortfall and land
            # twice the currency once both fill.
            log(f"  {ccy} conversion is working but unfilled; not starting "
                f"another from a different balance")
            return False
        # Try the NEXT balance rather than giving up. Returning here would
        # strand the trade whenever the largest source happens to fail - a
        # rejected order, a rate glitch - while other funded currencies sit
        # unused. With USD/EUR/GBP/JPY held that is a live scenario, not a
        # theoretical one; every cross among them exists on IB.
        log(f"  {src}->{ccy} did not go through; trying the next balance")
    log(f"  no non-{BASE_CCY} balance could fund {ccy}; order will not be placed")
    return False


def _source_rank(ib, ccy, have):
    """Sort key for a funding source: its value in BASE_CCY, largest first.

    fx_rate, not fx_rate_live: this only ORDERS the candidates, and a remembered
    rate orders them as well as a live one - the conversion itself still insists
    on a live rate. A source whose rate is missing (or whose lookup raises) is
    ranked LAST, by its raw units, never dropped: it may still be the only
    balance able to pay, and the loop's own `have < need_src` test judges that.
    """
    try:
        r = fx_rate(ib, ccy, BASE_CCY)
    except Exception:
        r = 0.0
    if r and r == r and r > 0:
        return (0, -have * r)
    return (1, -have)


def _fx_order_pair(ib, src, dst, qty_src, qty_dst, dry, base_sell_cap=None):
    """Convert src -> dst on whichever spot pair IB lists for them.

    The pair may be quoted either way round - USD.JPY has USD as base, so
    acquiring JPY means SELLing it in USD units; a DST.SRC pair would mean
    BUYing in DST units. Reading the symbol avoids assuming a direction.

    qty_src and qty_dst are the two ends of ONE conversion, and only the pair's
    base end is ordered - so both must already carry the funding buffer.
    qty_src is also what an unfilled order reserves in _FX_COMMITTED, on either
    branch, and the in-run HKD pocket estimate reads the ordered qty.

    base_sell_cap: the bot's own BASE_CCY this conversion may sell, from
    fund_from_nonbase's own-pocket source; None (every other caller) keeps the
    outright refusal of BASE_CCY as a source.
    """
    if src == BASE_CCY and not _own_base_sale(qty_src, base_sell_cap):
        # SELLING the base currency is what this bot may not do with money that
        # is not provably its own: the HKD balance is the operator's transfer
        # funding. The one exception is the bot's own stamped pocket, up to the
        # cap it allowed (owner decision 2026-10-26). BUYING HKD is allowed and
        # used - an HK stock settles in HKD, and funding it from USD/JPY/EUR
        # cash is what keeps the order off an HKD margin loan.
        log(f"  !! refusing FX {src}->{dst}: would sell {BASE_CCY}"
            + ("" if base_sell_cap is None else
               f" beyond the bot's own {base_sell_cap:,.0f}"))
        return False
    why = (f"{src}->{dst}, bot's own {BASE_CCY}" if src == BASE_CCY
           else f"{src}->{dst}")
    import ib_orders
    cid, sym = ib_orders.fx_pair_conid(src, dst)
    if not cid or "." not in (sym or ""):
        log(f"  no IB spot pair for {src}/{dst}; cannot fund")
        return False
    base, quote = sym.split(".", 1)
    # The cap is passed only when one was given, so a stub or wrapper written
    # against the old signature keeps working on every other source.
    kw = {} if base_sell_cap is None else {"base_sell_cap": base_sell_cap}
    if base.upper() == src.upper():
        return _fx_order(ib, base, quote, "SELL", qty_src, dry, why,
                         target=dst, src_ccy=src, src_qty=qty_src, **kw)
    if base.upper() == dst.upper():
        return _fx_order(ib, base, quote, "BUY", qty_dst, dry, why,
                         target=dst, src_ccy=src, src_qty=qty_src, **kw)
    log(f"  unexpected pair {sym} for {src}/{dst}; not guessing a side")
    return False


def reserve_working_cash(ib):
    """Reserve cash claimed by orders left working by an EARLIER run.

    _FX_COMMITTED and _FX_PENDING_CCY are process state, cleared at the top of
    every run, so on their own they only ever protected a single run. An order
    accepted but not yet filled does not reduce CashBalance - IB debits it on
    settlement - so without this the next run reads money that is already spoken
    for as free, and spends it twice. Stock buys and conversions both. Three
    cross-run holes follow, and this closes all of them:

      STOCK - run 1 places a USD limit into a shut session; run 2 starts empty,
      reads the undebited balance and funds a different candidate from the same
      dollars. open_syms and pending_buys claim the SYMBOL and a position slot,
      never the cash.

      SOURCE - run 1 leaves a conversion working having claimed 1,847 USD; run 2
      starts empty, sees the full USD balance (an unfilled order debits no
      CashBalance) and spends the same USD again for a different target.

      TARGET - _fx_already_working matches on the PAIR, so a working USD.JPY
      does not stop run 2 converting EUR into JPY. Only _FX_PENDING_CCY carries
      "something is already on its way into JPY", and it was not rebuilt.

    Both sides of the pair are handled. A SELL spends `qty` of the base and
    acquires the quote; a BUY acquires the base and spends the quote, whose
    amount needs the rate - conservative, and far better than the nothing that
    was reserved before. IB quotes EUR.USD and GBP.USD with USD as the QUOTE, so
    funding EUR or GBP out of USD takes the BUY branch: skipping it would have
    left the source unreserved for every European name in the universe.

    Best-effort by design: a pair or rate we cannot resolve reserves nothing
    rather than guessing, and says so in the log.
    """
    try:
        rows = [t for t in ib.openTrades()
                if getattr(t.orderStatus, "status", "") in _WORKING_STATUS]
    except Exception as e:
        log(f"  ! cannot read working orders to reserve cash ({str(e)[:80]})")
        _RESERVE_OK["ok"] = False
        return
    import ib_orders
    unresolved = 0
    for t in rows:
        if getattr(t.contract, "secType", "") != "CASH":
            # A working STOCK buy claims cash exactly as a conversion does, and
            # for the same reason: CashBalance is not debited until settlement.
            # Skipping these left the cross-run half of the very hole the
            # in-run reservation closes - run 1 places a USD limit into a shut
            # session, run 2 starts with an empty registry and funds another
            # candidate from the same dollars. open_syms and pending_buys do
            # not help: they claim the SYMBOL and a position slot, never the
            # money. A SELL is ignored deliberately - unrealised proceeds are
            # not spendable cash.
            if str(getattr(t.order, "action", "") or "").upper() != "BUY":
                continue
            ccy = str(getattr(t.contract, "currency", "") or "").upper()
            px = getattr(t.order, "lmtPrice", None)
            qy = float(getattr(t.order, "totalQuantity", 0) or 0)
            if ccy and px and qy > 0:
                _FX_COMMITTED[ccy] = _FX_COMMITTED.get(ccy, 0.0) + qy * float(px)
            else:
                unresolved += 1
            continue
        base = str(getattr(t.contract, "symbol", "") or "").upper().split(".")[0]
        qty = float(getattr(t.order, "totalQuantity", 0) or 0)
        side = str(getattr(t.order, "action", "") or "").upper()
        if not base or qty <= 0:
            continue
        try:
            quote = ib_orders.fx_quote_ccy(base, getattr(t.contract, "conId", 0))
        except Exception:
            quote = ""
        if side == "SELL":
            source, amount, target = base, qty, quote
        else:                                  # BUY base, paying the quote
            source, target = quote, base
            rate = fx_rate(ib, base, quote) if quote else 0.0
            amount = qty * rate if rate else 0.0
        if source and amount > 0:
            _FX_COMMITTED[source] = _FX_COMMITTED.get(source, 0.0) + amount
        elif not source or amount <= 0:
            unresolved += 1
        if target:
            _FX_PENDING_CCY.add(target)
    _RESERVE_OK["ok"] = not unresolved
    if _FX_COMMITTED:
        log("working orders reserve: "
            + ", ".join(f"{c} {a:,.0f}" for c, a in sorted(_FX_COMMITTED.items())))
    if _FX_PENDING_CCY:
        log(f"conversions already in flight into: {sorted(_FX_PENDING_CCY)}")
    if unresolved:
        log(f"  ! {unresolved} working order(s) could not be priced; the cash "
            f"they claim is NOT reserved this run")


def _spendable(ib, ccy):
    """Cash in <ccy> that is not already promised to a working FX order.

    CashBalance is NOT reduced by an accepted-but-unfilled order - IB debits it
    on settlement, not on acceptance - so the raw balance still shows money a
    pending conversion has spoken for. fund_from_nonbase nets _FX_COMMITTED off
    its candidate SOURCES, but ensure_ccy's "already funded, nothing to do"
    early-out was reading the raw figure, so a stock order could be sized
    against the very cash a conversion was mid-way through spending.
    """
    return cash_by_ccy(ib).get(ccy, 0.0) - _FX_COMMITTED.get(ccy, 0.0)


_EARMARK_RUN = {}      # base-currency earmark, frozen once per run (see run())
_EXC_APPLIED = {}      # what net_liq() actually subtracted, for the publisher
# The bot's own BASE_CCY pocket for THIS run: {"active", "p", "confirmed",
# "anchor"}. p is None whenever earmark.bot_pocket could not confirm stamping,
# and every consumer then falls back to exactly the pre-pocket rule. Swept from
# IB's executions at the start of run(), grown by conversions into BASE_CCY that
# fill in-run, and written to earmark.POCKET_FILE at the end of a live run.
_POCKET_RUN = {}


def _run_pocket():
    """The in-run pocket, or None outside a run or when it is not known."""
    if not _POCKET_RUN.get("active"):
        return None
    return _POCKET_RUN.get("p")


def _spendable_base(ib, balances=None):
    """BASE_CCY cash an order may actually spend. `balances`: a cash_by_ccy
    answer the caller already read, so one decision costs one ledger read.

    What IB holds, less what a conversion or an unfilled order has already
    claimed, less the operator's earmark. net_liq() keeps the earmark out of
    the NetLiq that sizes positions, so spending it here would contradict that
    - and the earmark IS the money waiting to be transferred out. Capped at the
    balance held, exactly as net_liq() caps it, so a stale marker cannot make
    the spendable figure negative.

    When the bot's stamped pocket P is known, spending is bounded by P as well:
    max(0, min(P, cash - earmark) - committed). The bot spends its own HKD and
    nothing else - not an unmarked pot, not HKD it cannot identify - and the
    earmark no longer hides the bot's own funding, which is what used to make
    the next run convert ANOTHER ~14,500 of USD for money it already held.
    committed is still subtracted exactly once (the double subtraction of
    aabd4a5 is the failure t11 in test_hkd_funding pins).

    INSIDE A RUN WHOSE POCKET IS UNKNOWN this is 0 (owner decision 2026-10-26,
    the reviewer's extra safety): an HK buy spends no HKD that is not provably
    the bot's, and ensure_ccy funds the whole order by conversion instead. The
    old fallback, cash - earmark - committed, handed over every unmarked dollar
    - the operator's pot whenever the marker was 0 or not yet set, HK dividends
    - and bigger slots made that reach further. Outside a run (tests, one-off
    tools: no pocket was swept at all) the old formula still applies.
    """
    cash = (balances if balances is not None else cash_by_ccy(ib)).get(BASE_CCY, 0.0)
    committed = _FX_COMMITTED.get(BASE_CCY, 0.0)
    exc = _EARMARK_RUN.get("base")
    pocket = _run_pocket()
    if pocket is not None:
        if exc is None:
            exc = min(earmark.exclusion(cash, pocket), max(0.0, cash - committed))
        return max(0.0, min(pocket, cash - exc) - committed)
    if _POCKET_RUN.get("active"):
        return 0.0                 # a run, pocket unknown: nothing provably the bot's
    if exc is None:
        # Outside a run (tests, one-off tools): derive it live.
        exc = min(earmark.effective(cash, persist=False),
                  max(0.0, cash - committed))
    # The cap is taken against cash that is NOT already claimed, and the result
    # is clamped. Capping against the RAW balance double-counted every dollar a
    # working order had claimed - once inside the cap, once as the reservation -
    # so with a marker above the HKD held this returned a NEGATIVE number, and
    # ensure_ccy's `short = need - have` then asked to convert need PLUS the
    # committed amount. On the live balances that was ~3,652 of 4,558 USD turned
    # into HKD the mandate does not allow selling back.
    return max(0.0, cash - exc - committed)


def _own_base_spendable(ib, balances=None):
    """The bot's OWN BASE_CCY that may be SOLD to fund a non-HKD order, or 0.0.

    The narrow source of the owner's HKD decision (2026-10-26) - never
    FX_CONVERT=1, never _spendable_base's fallback. 0.0 unless ALL of these hold:
      - OWN_BASE_FUND is on;
      - the in-run pocket P is known (_run_pocket: anchor set, stamping
        confirmed, no canary, no coverage gap, executions read complete);
      - the earmark is frozen for this run (_EARMARK_RUN), so the operator's
        marked money is out of H before anything is sold;
      - reserve_working_cash read the order book and priced every working order
        (_RESERVE_OK), so no unreserved HKD claim can be sold from under it.
    Then: min(P, H - frozen earmark) - committed x BASE_FUND_BUFFER - margin.
    committed is the HKD working HK buys (and unfilled own sales) already claim;
    the buffer is their stamp duty and fee headroom, which the limit x qty
    reservation does not include. OWN_BASE_MARGIN covers fees, levies and
    interest booked outside executions, which lower H but not P (earmark.py's
    one unsafe residue) - selling all of P would take that much of the pot.
    """
    if not OWN_BASE_FUND:
        return 0.0
    pocket = _run_pocket()
    exc = _EARMARK_RUN.get("base")
    if pocket is None or exc is None or _RESERVE_OK.get("ok") is not True:
        return 0.0
    bal = balances if balances is not None else cash_by_ccy(ib)
    cash = float(bal.get(BASE_CCY, 0.0) or 0.0)
    committed = _FX_COMMITTED.get(BASE_CCY, 0.0)
    own = (min(float(pocket), cash - exc) - committed * BASE_FUND_BUFFER
           - OWN_BASE_MARGIN)
    return max(0.0, own) if own == own else 0.0


def _free_cash_base(ib):
    """(free cash in BASE_CCY, parts for the log): what this run divides over its
    free slots - the entry sizing since 2026-10-26 (owner decision; it was
    NetLiq / TARGET_POSITIONS). run() calls it ONCE, after the exits, after
    reserve_working_cash and the earmark freeze, and only when free > 0.

    Every currency's balance net of _FX_COMMITTED - working orders' and this
    run's reservations, so VLO's claimed USD is not counted twice - valued at
    fx_rate (a remembered weekend rate is fine: this only sizes). Left out:
      - SIZING_SKIP_CCY (GBP, the owner's pass-through money);
      - BASE_CCY, except the bot's own HKD (_own_base_spendable: 0 whenever
        the pocket is unknown, so the earmark and an unmarked pot never count)
        and a net HKD DEBIT, which is owed and counts against;
      - a positive balance with no rate - never valued at 1.0.
    A NEGATIVE balance with no rate RAISES: a debt of unknown size cannot be
    left out, and the caller then places no entries this run. A parked SGOV
    holding is not counted: parking is shadow-only, so none is the bot's.
    """
    bal = cash_by_ccy(ib)
    total, parts = 0.0, []
    # Every currency held OR claimed: a working order in a currency the account
    # holds none of is still money it will owe when it fills.
    for c in sorted({str(k).upper() for k in bal} | set(_FX_COMMITTED)):
        if c in SIZING_SKIP_CCY:
            continue
        net = float(bal.get(c, 0.0) or 0.0) - _FX_COMMITTED.get(c, 0.0)
        if c == BASE_CCY:
            if net < 0:
                total += net
                parts.append(f"{c} owed {net:,.0f}")
            continue
        if abs(net) < 0.5:
            continue
        r = fx_rate(ib, c, BASE_CCY)
        if not r or r != r or r <= 0:
            if net < 0:
                raise ValueError(f"{c} {net:,.0f} is owed and there is no "
                                 f"{c}/{BASE_CCY} rate to value it")
            log(f"  sizing: {c} {net:,.0f} left out - no {c}/{BASE_CCY} rate")
            continue
        total += net * r
        parts.append(f"{c} {net * r:,.0f}")
    own = _own_base_spendable(ib, bal)
    if own > 0:
        total += own
        parts.append(f"own {BASE_CCY} {own:,.0f}")
    if total != total:
        raise ValueError("free cash is not a number")
    return max(0.0, total), parts


def _fundable_base(ib, ccy, rate):
    """What ONE funding pass could pay for an order in <ccy>, in BASE_CCY.

    The cash already in <ccy> (net of reservations), plus the single largest
    other source fund_from_nonbase could convert, divided by its buffer (and a
    hair for the live rate differing from the sizing rate). One source, because
    fund_from_nonbase never pools two: free cash split over USD, JPY and EUR can
    size a slot that no single conversion pays for, and that order would then
    fail funding on every run (live_sim.py ensure() clamps the same way). No
    source at all while a conversion into <ccy> is already working - that run
    gets no second one. Raises on a read error; the caller skips the candidate.
    """
    bal = cash_by_ccy(ib)
    if ccy == BASE_CCY:
        own, buf = _spendable_base(ib, bal), BASE_FUND_BUFFER
    else:
        own = (float(bal.get(ccy, 0.0) or 0.0) - _FX_COMMITTED.get(ccy, 0.0)) * rate
        buf = 1.02
    best = 0.0
    if ccy not in _FX_PENDING_CCY:
        for c, amt in bal.items():
            c = str(c).upper()
            if c in (ccy, BASE_CCY):
                continue
            have = float(amt) - _FX_COMMITTED.get(c, 0.0)
            if have <= 0:
                continue
            r = fx_rate(ib, c, BASE_CCY)
            if r and r == r and r > 0:
                best = max(best, have * r)
        if ccy != BASE_CCY:
            best = max(best, _own_base_spendable(ib, bal))
    return own + best / (buf * 1.0002)


def ensure_ccy(ib, ccy, need_base, dry):
    """Make sure enough <ccy> cash exists for a purchase worth ~need_base (BASE_CCY).

    Returns True when the buy may proceed - the cash is there, or a conversion
    FILLED, or funding is IB's job because the bot's own FX is switched off.
    Returns False when the money is not in the account, and the caller MUST skip
    the order.

    That return value used to not exist: every path fell out as None and the
    caller placed the order regardless, on the docstring's claim that "an
    under-funded stock order is rejected by IB". That is not true of a margin
    account - IB fills it and settles the deficit itself, which on this account
    means reaching the HKD the mandate forbids selling. Board review caught the
    whole filled-only chain dead-ending here, one call short of the leg that
    actually spends money.
    """
    if ccy == BASE_CCY:
        # A Hong Kong stock settles in HKD - the base currency - and this line
        # used to return True without looking at the balance at all: the one
        # currency with no funding check, written when the HKD balance was
        # large. It is 7. IB does not convert other balances to cover the
        # shortfall; it books a NEGATIVE HKD balance and charges HKD margin
        # interest (IBKR's own worked example carries long EUR and short USD
        # side by side). So fund the order first.
        #
        # Nothing on this path sells HKD: fund_from_nonbase offers the bot's
        # own HKD only to a NON-HKD target, and without that source's cap
        # _fx_order_pair and _fx_order each refuse any order that sells it.
        # The mandate - never out of HKD that is not provably the bot's; into
        # it is fine, and cheaper than borrowing it. With the pocket unknown,
        # _spendable_base is 0 here, so the whole order is bought by conversion
        # rather than spending HKD that may be the operator's.
        #
        # Guarded like the two branches below, which this one was written
        # without. Every read here is a fresh Web API call - the ledger behind
        # _spendable_base and fund_from_nonbase, iserver/currency/pairs behind
        # _fx_order_pair - and each raises on a transient 500 or timeout.
        # Unguarded, that exception left run() after an earlier entry had
        # already been SENT and before state.json was saved: the filled
        # position had no state['map'] entry, so the exit loop skipped it
        # silently on every later run - no trailing, regime or time stop.
        # Skipping this one candidate is the whole cost. Nothing is left half
        # converted: the calls that send and poll an FX order swallow their own
        # errors, and fund_from_nonbase stops at the first conversion that is
        # accepted, so an exception can only follow a read or a refused order.
        try:
            have = _spendable_base(ib)
            if have >= need_base:
                return True
            short = need_base - have
            log(f"  {BASE_CCY} short {short:,.0f} for this order (have "
                f"{have:,.0f}, need {need_base:,.0f}) — buying it from "
                f"non-{BASE_CCY} cash")
            return fund_from_nonbase(ib, BASE_CCY, short, dry,
                                     buffer=BASE_FUND_BUFFER)
        except Exception as e:
            log(f"  ! {BASE_CCY} funding skipped ({str(e)[:120]}); skipping "
                f"rather than under-funding")
            return False
    if not FX_CONVERT:
        if not FX_FUND_NONBASE:
            log(f"  bot FX off — no {BASE_CCY} conversion; {ccy} buy uses existing "
                f"cash / IB auto-funding from non-{BASE_CCY} balances")
            return True          # deliberate: IB funds it, as configured
        try:
            rate = fx_rate(ib, ccy, BASE_CCY)
            if not rate:
                log(f"  ! no {ccy}/{BASE_CCY} rate; cannot size {ccy} funding")
                return False
            need_ccy = need_base / rate
            have = _spendable(ib, ccy)
            if have >= need_ccy:
                return True                  # already funded, nothing to do
            return fund_from_nonbase(ib, ccy, (need_ccy - have), dry)
        except Exception as e:
            log(f"  ! {ccy} funding skipped ({e})")
            return False
    try:
        rate = fx_rate(ib, ccy, BASE_CCY)         # BASE per 1 ccy
        if not rate:
            log(f"  ! no {ccy}/{BASE_CCY} rate; cannot fund {ccy}")
            return False
        need_ccy = need_base / rate
        have = _spendable(ib, ccy)
        if have >= need_ccy:
            return True
        if (ccy, BASE_CCY) in _STALE_RATES:
            # Review 2026-09-16: convert_into's USD-cross leg asks
            # fx_rate_live(USD, USD), which is always 1.0, so a shut market
            # sailed past it and sent a market order sized on memory.
            log(f"  no live {ccy}/{BASE_CCY} rate (FX market shut?); not "
                f"converting on a remembered rate")
            return False
        short = (need_ccy - have) * 1.02          # small buffer for slippage/fees
        if not convert_into(ib, ccy, short, dry):
            log(f"  ! no FX path {BASE_CCY}->{ccy}; skipping rather than under-funding")
            return False
        return True
    except Exception as e:
        log(f"  ! FX funding skipped ({e}); skipping rather than under-funding")
        return False


# ---------------- dashboard state publishing ----------------
PARTS_FRESH_S = 2 * 3600     # one missed hourly publish still counts as the close

# The trading rules the last LIVE trading run actually ran with, published into
# data/bot_state.json as "config" so they can be checked from the phone without
# VM access (owner's reviewer, 2026-10-26). Set by run(); a publish without a
# trading run (publish_only, and publish_web.py every hour) carries the
# previous "config" over unchanged - their processes may well have a different
# environment from the trading line, so their own view would prove nothing.
_RUN_CONFIG = {}
# The environment variables a cron line could set to change a trading rule.
# Their RAW values are published when set, and only these names - so a leftover
# MAX_ORDER_BASE or DAILY_LOSS_KILL on the VM shows up instead of silently
# overriding the code. Nothing secret is in this list; keep it that way.
CONFIG_ENV_VARS = ("TARGET_POSITIONS", "MAX_ORDER_BASE", "DAILY_LOSS_KILL",
                   "LIMIT_BUFFER", "FX_CONVERT", "FX_FUND_NONBASE",
                   "BASE_FUND_BUFFER", "HK_ENABLED", "MAX_HOLD_BARS",
                   "IB_BASE_CCY", "IB_BACKEND", "CONFIRM_FIRST", "SIGNALS_URL",
                   "EXCLUDED_CASH")
_COMMIT = {}


def _code_commit():
    """The checked-out commit of this code (12 hex chars), or None. Asked once
    per process; a failure - no git, not a checkout (the test export) - is None."""
    if "c" not in _COMMIT:
        _COMMIT["c"] = None
        try:
            import subprocess
            r = subprocess.run(["git", "-C", str(Path(__file__).resolve().parent),
                                "rev-parse", "--short=12", "HEAD"],
                               capture_output=True, text=True, timeout=10)
            out = (r.stdout or "").strip()
            if r.returncode == 0 and re.fullmatch(r"[0-9a-f]{7,40}", out):
                _COMMIT["c"] = out
        except Exception:
            pass
    return _COMMIT["c"]


def effective_config(dry=False):
    """The trading rules as they are IN EFFECT in this process - code defaults
    with any environment override applied, read at call time. Never raises."""
    try:
        from datetime import datetime, timezone
        return {
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "dry": bool(dry),
            "commit": _code_commit(),
            "target_positions": TARGET_POSITIONS,
            "sizing": "free_cash/free_slots",
            "max_order_base": MAX_ORDER_BASE,
            "entry_move_max": ENTRY_MOVE_MAX,
            "limit_buffer": LIMIT_BUFFER,
            "kill_switch": "on" if KILL_SWITCH else "off",
            "daily_loss_kill": DAILY_LOSS_KILL,
            "fx_convert": bool(FX_CONVERT),
            "fx_fund_nonbase": bool(FX_FUND_NONBASE),
            "own_hkd_funding": bool(OWN_BASE_FUND),
            "base_fund_buffer": BASE_FUND_BUFFER,
            "hk_enabled": bool(HK_ENABLED),
            "max_hold_bars": MAX_HOLD_BARS,
            "crypto_entries": "blocked",
            "entry_order": "tier, then score",
            "park_mode": PARK_MODE,
            "park_symbol": PARK_SYMBOL,
            "market_filter": market_filter_mode(),
            "base_ccy": BASE_CCY,
            "env": {k: str(os.environ[k])[:80] for k in CONFIG_ENV_VARS
                    if k in os.environ},
        }
    except Exception as e:                      # a report must never cost a run
        return {"error": str(e)[:120]}


def _publisher_row_stands(series, parts_path, today, exc=0, now=None):
    """True when today's NetLiq row is the hourly publisher's own, with a recent
    breakdown behind it - so this run must not overwrite it.

    The P&L Calendar breaks each day down from data/day_parts.json, which only
    publish_web writes (the day's last at 23:25 UTC), beside the same NetLiq it
    puts in netliq_history. This run publishes at 23:35 and used to overwrite
    that NetLiq with its own reading ten minutes later, so a day's total and its
    breakdown came from two moments and whatever the account moved in between
    showed as "unexplained" - -567 HKD on 2026-10-08 and +549 the next day.
    The row must also have been netted by the same earmark as this run's
    reading (`exc`): a publisher working from a lagging pocket file can
    over-exclude, and this run's fresher figure must then replace it, as before.
    Anything else - no row today, no breakdown, one that does not match or is
    more than PARTS_FRESH_S old, a file that cannot be read - is False: write
    ours exactly as before. Never raises.
    """
    from datetime import datetime, timezone
    try:
        mine = [e for e in series if isinstance(e, dict) and e.get("d") == today]
        if len(mine) != 1:
            return False
        row = json.loads(Path(parts_path).read_text()).get("days", {}).get(today)
        if not isinstance(row, dict):
            return False
        if round(float(row["nl"])) != round(float(mine[0]["nl"])):
            return False              # not the reading that row was taken with
        if not (round(float(row.get("exc") or 0)) == round(float(mine[0].get("exc") or 0))
                == round(float(exc or 0))):
            return False              # netted by a different earmark than ours
        taken = datetime.strptime(str(row["ts"]), "%Y-%m-%dT%H:%M:%SZ") \
                        .replace(tzinfo=timezone.utc)
        age = ((now or datetime.now(timezone.utc)) - taken).total_seconds()
        return 0 <= age <= PARTS_FRESH_S
    except Exception:
        return False


def publish_state(ib, state, nl):
    """Write data/bot_state.json into the repo clone and push it (best-effort),
    so the phone dashboard shows live bot positions/history automatically."""
    try:
        import subprocess
        from datetime import datetime, timezone
        repo = Path(__file__).resolve().parent.parent      # .../multi-product-signals
        out = repo / "data" / "bot_state.json"
        prev = {}
        if out.exists():
            try:
                prev = json.loads(out.read_text())
            except Exception:
                prev = {}
        smap = state.get("map", {})
        poss = []
        for p in ib.positions():
            if not p.position:
                continue
            if getattr(p.contract, "secType", "") == "CASH":
                continue                      # FX pairs are cash, not investments
            ysym = smap.get(p.contract.symbol, p.contract.symbol)
            st = state.get("pos", {}).get(ysym, {})
            poss.append({"symbol": ysym, "ib_symbol": p.contract.symbol,
                         "qty": p.position, "avg_cost": round(p.avgCost, 4),
                         "ccy": p.contract.currency,
                         "entry": st.get("entry"), "stop": st.get("stop")})
        cash_raw = cash_by_ccy(ib)
        cash = {k: round(v) for k, v in cash_raw.items() if abs(v) >= 1}
        # Scrubbed on EVERY write, old rows included. This file is public, and
        # 9 rows from 2026-09-01..03 carry the live account number inside
        # "POST iserver/account/<acct>/orders failed" errors. ib_orders now
        # redacts at the source; rewriting the carried-over rows is the only
        # thing that cleans the ones already published (git history is left
        # alone on purpose).
        import ib_web
        act = ib_web.scrub((prev.get("activity") or []) + PLACED)
        # Must reproduce net_liq()'s cap EXACTLY. The field's contract is "how
        # much of `cash` is already netted out of `netliq`", and net_liq() nets
        # out min(marker, base-ccy cash held) - not the raw marker. Publishing
        # the raw marker made the same JSON object carry a capped netliq beside
        # an uncapped exclusion, so the dashboard captioned "excl. 18,559 HKD
        # cash" on a day the money had been withdrawn and only 4 was excluded,
        # and the caption flipped every time the other publisher ran.
        # The figure net_liq() actually applied to THIS netliq. Recomputing it
        # here re-read the marker and the balance at the END of the run - after
        # the bot may have bought HKD to fund an entry - so the file carried a
        # netliq and an excluded_cash describing different moments, and the
        # dashboard (which now treats netliq as "everything but the earmark")
        # captioned one against the other.
        exc_pub = _EXC_APPLIED.get("base")
        bot_pub = _EXC_APPLIED.get("bot")
        if exc_pub is None:
            exc_pub, bot_pub = earmark.publisher_exclusion(
                float(cash_raw.get(BASE_CCY, 0) or 0))
        # NOTE: an earlier revision published base_for_orders here so the page
        # could keep order-funding HKD inside net worth. It is gone on purpose:
        # publish_web.py writes this same file every hour and never emitted the
        # field, so it vanished within 50 minutes - inert for exactly the
        # overnight window it existed for. The page now applies ONE exclusion,
        # the earmark, which both publishers already net out of `netliq`, so
        # funding HKD is inside net worth by construction and no field is needed.
        snap = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "netliq": round(nl), "base_ccy": BASE_CCY, "cash": cash,
                "excluded_cash": round(exc_pub),
                # The operator's RAW number, for display only. Never read as the
                # exclusion - publishing the raw marker AS excluded_cash is what
                # once made the caption flip between publishers. Without it a
                # marker above the HKD held is invisible: the card shows
                # "Earmarked = HKD balance" and gives no prompt to clear it.
                "earmark_marker": round(earmark.marker()),
                # The bot's own HKD that netliq's exclusion left in the pool
                # (earmark.bot_share), or null when the stamped pocket is not
                # known and the plain cap applied. The dashboard shows it and
                # stops claiming a stale marker also excludes bot funding. A
                # MISSING key (an older publisher) must keep today's page.
                "earmark_bot_hkd": (None if bot_pub is None else round(bot_pub)),
                "positions": poss, "activity": act[-100:]}
        # The rules the last live trading run ran with (see _RUN_CONFIG): this
        # run's own when it is one, else the previous file's, carried over.
        # publish_web.py carries it the same way, so it survives the hourly
        # publish. Additive: a page that does not read it is unaffected.
        cfg = dict(_RUN_CONFIG) if _RUN_CONFIG else prev.get("config")
        if isinstance(cfg, dict):
            snap["config"] = cfg
        # The market filter's shadow record (see MARKET_FILTER): this run's
        # mode and the index rows it read, plus the rolling would-have-skipped
        # list state.json keeps across runs - or, with no trading run here, the
        # previous file's block carried over, as "config" is. Additive.
        mf = _market_filter_block(state, prev)
        if isinstance(mf, dict):
            snap["market_filter"] = mf
        out.write_text(json.dumps(snap, indent=1))
        # Where each cut-loss has been, so the holding's chart shows WHEN the bot
        # raised it rather than one flat line. Changes only. Guarded on its own,
        # like the fills/dividend/tax steps below: a chart must never cost a
        # publish. Imported here, not at module level: a suite that imports
        # ib_bot without testenv must not pull in more paths.
        try:
            import stop_history
            stop_history.update(out.parent / "stop_history.json", poss, log)
        except Exception as e:
            log(f"  note: cut-loss history skipped ({e})")
        # daily NetLiq history for the dashboard's P&L Calendar: upsert TODAY's
        # (UTC) entry with the latest netliq - unless the hourly publisher's row
        # for today should stand (see _publisher_row_stands). Deposits and
        # withdrawals must be added to "flows" by hand (see execution/README.md)
        # so the calendar shows TRADING P&L, not cash movements.
        try:
            hist_p = out.parent / "netliq_history.json"
            hist = {"series": [], "flows": []}
            if hist_p.exists():
                # an UNREADABLE file must be left in place for human repair —
                # falling through to the rewrite would silently destroy the
                # backfilled series and the hand-entered flows, then push the
                # wipe (board finding 2026-08-06). Raise into the outer handler.
                hist = json.loads(hist_p.read_text())
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            ser = list(hist.get("series", []))
            if _publisher_row_stands(ser, out.parent / "day_parts.json", today,
                                     exc=exc_pub):
                log("  netliq history: today's row is the hourly publisher's, with "
                    "its breakdown behind it - left as it is")
            else:
                ser = [e for e in ser if e.get("d") != today]
                # "exc": the earmark THIS netliq was netted by (see exc_pub
                # above), so a reader chaining the series can tell an earmark
                # step from a trading gain. Additive - every older row reads 0.
                ser.append({"d": today, "nl": round(nl), "exc": round(exc_pub or 0)})
            hist["series"] = sorted(ser, key=lambda e: e["d"])
            tmp = hist_p.with_suffix(".json.tmp")     # atomic: no torn writes
            tmp.write_text(json.dumps(hist, indent=1))
            os.replace(tmp, hist_p)
        except Exception as e:
            log(f"  !! netliq history NOT updated ({e}) — fix data/"
                f"netliq_history.json by hand (validate with python -m "
                f"json.tool); existing file left untouched")
            if not hist_p.exists():
                hist_p.touch()             # git add must not break the publish
        # tax pipeline: sweep today's executions into the fills ledger, then
        # regenerate the UK CGT report the dashboard's Tax mode reads.
        # three independent stages: a fills-sweep failure must not stop the
        # dividend sweep, and neither may stop the report rebuild
        try:
            import fills_capture
            # live only: a remembered rate is days old, and a missing one is
            # flagged rate_missing for uk_cgt instead of valuing the fill wrongly
            fills_capture.capture(ib, lambda ccy: fx_rate_live(ib, ccy, "GBP"))
        except Exception as e:
            log(f"  note: fills sweep skipped ({e})")
        try:
            import flex_dividends
            flex_dividends.capture_if_configured()   # no-op until /root/flex.conf
        except Exception as e:
            log(f"  note: dividend sweep skipped ({e})")
        try:
            import uk_cgt
            uk_cgt.build_report()
        except Exception as e:
            log(f"  note: tax report build skipped ({e})")
        div_ledger = out.parent / "dividends_ledger.jsonl"
        if not div_ledger.exists():
            div_ledger.touch()        # git add fails on a missing pathspec
        stops_file = out.parent / "stop_history.json"
        if not stops_file.exists():
            stops_file.write_text('{"updated": null, "stops": {}}')   # same trap
        for cmd in (["add", "data/bot_state.json", "data/fills_ledger.jsonl",
                     "data/tax_report.json", "data/dividends_ledger.jsonl",
                     "data/netliq_history.json", "data/stop_history.json"],
                    ["-c", "user.email=bot@vm", "-c", "user.name=ib-bot",
                     "commit", "-m", "bot: state update [skip ci]"]):
            r = subprocess.run(["git", "-C", str(repo)] + cmd,
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                log(f"  note: state publish '{cmd[0] if cmd[0] != '-c' else 'commit'}'"
                    f" skipped ({(r.stderr or r.stdout).strip()[:90]})")
                break
        else:
            # Not a bare push: a push refused because origin moved (an operator
            # deploy since the :25 reset) left this run's PLACED/REJECTED/HALT
            # rows in a local commit the next reset threw away for good.
            # gitpush replays it onto origin and retries, and never leaves the
            # checkout mid-rebase (board review 2026-09-21). It logs why it fails.
            import gitpush
            if gitpush.push_with_retry(repo, log, attempts=3, timeout=60):
                log("  bot state published to dashboard")
    except Exception as e:
        log(f"  note: state publish skipped ({e})")


# ---------------- zombie-gateway self-heal ----------------
_ZOMBIE_MARKER = Path("/tmp/mps_zombie_kill")
try:
    ZOMBIE_COOLDOWN_H = float(os.environ.get("ZOMBIE_COOLDOWN_H", "3"))
except ValueError:                       # malformed env must not kill every run
    ZOMBIE_COOLDOWN_H = 3.0


def connect_or_heal(ib, client_id, timeout):
    """ib.connect with zombie-gateway detection. The failure seen 2026-08-01
    17:20Z and again 2026-08-02 11:20Z: the gateway's port keeps accepting TCP
    while its API session is dead, so ensure_gateway's port check passes and
    the outage is silent for hours until a human intervenes.
    Signature = connect TIMES OUT while the port still accepts TCP (a downed
    gateway raises ConnectionRefused instead, which the watchdog already
    handles). On the signature: kill java so ensure_gateway's 15-min cron
    relaunches with a fresh login — 2FA push in waking hours; its existing
    night hold (23:30-07:00 London) defers overnight, unchanged. Rate-limited
    to one kill per ZOMBIE_COOLDOWN_H so an IB-side outage cannot cause
    kill/2FA spam. Always re-raises: a failed connect NEVER trades/publishes."""
    try:
        ib.connect(HOST, PORT, clientId=client_id, timeout=timeout)
        return
    except Exception as e:
        if not isinstance(e, TimeoutError):        # asyncio.TimeoutError == TimeoutError (3.11+)
            raise                                  # (live tracebacks 08-01/08-02 were TimeoutError)
        import socket, subprocess, time
        try:
            with socket.create_connection((HOST, PORT), timeout=3):
                port_accepts = True
        except OSError:
            port_accepts = False
        if not port_accepts:
            raise                                  # plain down — watchdog's job
        # SIBLING GUARD (board 2026-08-02): on BST Mondays the 09:00 UTC run
        # and the 10:00-London catch-up fire simultaneously with the same
        # clientId; the loser's duplicate-clientId timeout is indistinguishable
        # from a zombie while the gateway healthily serves the winner. Never
        # kill when another ib_bot is alive; if we cannot tell, do not kill.
        sibling = True                             # fail-safe default: no kill
        try:
            out = subprocess.run(["pgrep", "-fc", "ib_bot.py"],
                                 capture_output=True, text=True, timeout=10)
            sibling = int((out.stdout or "0").strip() or 0) > 1
        except Exception:
            pass
        if sibling:
            log("!! connect timeout but a sibling ib_bot.py is running — "
                "likely clientId collision, NOT a zombie; not killing")
            raise
        # NIGHT GUARD: 23:00-07:00 London a kill buys nothing — the relaunch
        # is night-held to 07:00 anyway — while IB's nightly server resets
        # (~03:45-05:45 UTC) can stall healthy connects. Defer to daytime.
        night = False
        try:
            from zoneinfo import ZoneInfo
            from datetime import datetime
            h = datetime.now(ZoneInfo("Europe/London")).hour
            night = h >= 23 or h < 7
        except Exception:
            pass                                   # tz unavailable -> treat as day
        if night:
            log("!! ZOMBIE signature in the night window — deferring to "
                "daytime detection (relaunch would be night-held anyway)")
            raise
        try:
            recent = (time.time() - _ZOMBIE_MARKER.stat().st_mtime) \
                < ZOMBIE_COOLDOWN_H * 3600
        except OSError:
            recent = False
        if recent:
            log("!! ZOMBIE GATEWAY again within cooldown — not re-killing")
            raise
        log(f"!! ZOMBIE GATEWAY: port {PORT} accepts TCP but the API connect "
            f"timed out — killing java; ensure_gateway will relaunch with a "
            f"fresh login (2FA push follows in waking hours)")
        try:
            subprocess.run(["pkill", "-9", "java"], timeout=10)
            _ZOMBIE_MARKER.touch()                 # cooldown only on a real kill
        except Exception as ke:
            log(f"!! zombie heal failed ({ke}) — manual restart needed")
        raise


# ---------------- unfinished-exit alerts (ALERT-ONLY) ----------------
# Nothing in this section may influence whether, when or how an order is placed.
# The exit loop hands it facts it has already decided on; it hands nothing back.
#
# Why a second file next to state.json rather than a field in it: state.json is
# read by the trading logic, and "an exit was owed" is exactly the kind of fact a
# later edit would start acting on - re-sending a lapsed exit is an operator
# decision (it changes what trades), not an alerting one. This file is read by
# nothing but _exit_alerts_*.
#
# What it catches that a REJECTED row cannot: the verdict is read ONCE, ~3 s after
# sending. An exit IB accepted and then let expire or cancelled stays 'sent'
# forever, and a refused exit whose rule stops firing is never re-sent and never
# mentioned again - XYZ, refused at 09-01 23:35 and 09-02 09:00, still held 22
# shares with a dead stop on 09-16.
EXIT_ATTEMPTS = Path(os.environ.get("MPS_EXIT_ATTEMPTS", "/root/exit_attempts.json"))


def _utc_minute():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _alert(fn, *a, **k):
    """Run alert-side bookkeeping. An alert that fails costs an alert, never a
    run: an exception here would skip the remaining exits, every entry, and
    save_state/publish_state with them."""
    try:
        return fn(*a, **k)
    except Exception as e:
        try:
            log(f"  note: alert bookkeeping skipped ({str(e)[:100]})")
        except Exception:
            pass
        return None


def _load_exit_attempts():
    try:
        book = json.loads(EXIT_ATTEMPTS.read_text(encoding="utf-8"))
        return book if isinstance(book, dict) else {}
    except Exception:
        return {}


def _save_exit_attempts(book):
    try:
        tmp = EXIT_ATTEMPTS.with_name(EXIT_ATTEMPTS.name + ".tmp")
        tmp.write_text(json.dumps(book, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, EXIT_ATTEMPTS)
    except Exception as e:
        log(f"  note: exit-attempts memo not saved ({str(e)[:80]})")


def _exit_alerts_open(held, state):
    """Before the exit loop: forget what is no longer held, return the memo.

    A symbol that is gone was sold - by the bot's exit, by hand, or by a fill -
    so its record and its refusal episode are closed quietly."""
    smap = state.get("map", {}) or {}
    held_ysyms = set(held) | {smap.get(s, s) for s in held}
    alerts.clear_episodes(held_ysyms)
    memo = _load_exit_attempts()
    gone = [y for y in memo if y not in held_ysyms]
    for y in gone:
        del memo[y]
    if gone:
        _save_exit_attempts(memo)
    return memo


def _exit_alerts_sent(memo, ysym, held_qty, reason, status, run_stamp):
    """After a live exit place(): alert on a refusal or an earlier exit that
    did not finish, then record this attempt for the next run to check."""
    if status is None:
        return                         # nothing reached IB (declined at confirm)
    prev = memo.get(ysym) if isinstance(memo.get(ysym), dict) else None
    # place() appended this order's row just before returning; it carries IB's
    # error text, which the return value does not.
    row = PLACED[-1] if PLACED else {}
    if row.get("action") != "SELL" or row.get("reason") != reason:
        row = {}                       # not this order's row; use what we know
    if status == "REJECTED":
        # A refusal this run speaks for itself - no "did not complete" on top.
        alerts.exit_refused(ysym, row.get("qty", held_qty), reason,
                            row.get("error", ""), run_stamp)
    elif prev and prev.get("status") == "sent":
        # Not in open_syms (the loop skips a symbol with a working order), still
        # held, and re-sent: the previous accepted exit expired, was cancelled,
        # or filled only in part.
        alerts.enqueue(
            f"exit-unfinished-{ysym}-{run_stamp}",
            f"⚠️ {ysym}: the exit IB accepted at {prev.get('time')} "
            f"({prev.get('reason')}) did not complete - {held_qty} still held. "
            f"It expired or was cancelled unfilled, or filled only in part. "
            f"This run sent a new exit ({status}).")
    memo[ysym] = {"time": row.get("time") or _utc_minute(),
                  "status": status, "qty": held_qty, "reason": reason}
    _save_exit_attempts(memo)


def _exit_alerts_not_firing(memo, ysym, held_qty, run_stamp):
    """The exit rule did not fire this run for a symbol with an exit on record.

    Still held and no working order, so that exit never completed - and nothing
    will re-send it now that its condition has cleared. The bot keeps the
    position; whether to sell anyway is the operator's call."""
    prev = memo.get(ysym)
    if not isinstance(prev, dict):
        return
    queued = alerts.enqueue(
        f"exit-lapsed-{ysym}-{run_stamp}",
        f"⚠️ {ysym}: the exit from {prev.get('time')} ({prev.get('status')}, "
        f"{prev.get('reason')}) never completed, and its condition has cleared. "
        f"The bot is keeping the position ({held_qty} held) and will not "
        f"re-send it. Sell by hand if you still want out.")
    if queued:
        # Dropped only once the alert is safely queued, so a failing spool
        # tries again next run instead of losing the XYZ case silently.
        del memo[ysym]
        _save_exit_attempts(memo)
        # The refusal episode ends with the story the lapsed alert just closed,
        # or a later refusal of this still-held symbol is reported as "still
        # refused" with no rule and no IB text (review 2026-09-17). Never raises.
        alerts.close_episode(ysym)


# ---------------- the bot's own HKD pocket ----------------
def _now_utc():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _read_fills_ledger():
    rows = []
    try:
        with open(FILLS_LEDGER, encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue              # one bad line must not mask good fills
    except FileNotFoundError:
        pass
    return rows


# A bot order IB accepted this long ago is, but for a rare multi-day exchange
# holiday, no longer working (DAY orders wait at most for the next session, a
# long weekend away), so no execution for it means it expired unfilled - or
# IB's read left its fill out. Only logged, so an imprecise edge costs a line:
# see _sweep_pocket.
SUBMIT_SETTLED_DAYS = 4.0


def _sweep_pocket(ib, dry):
    """The bot's own BASE_CCY pocket at the START of this run (earmark.bot_pocket).

    Read-only against IB. Executions are read into MEMORY and merged by execId
    with the fills ledger and the VM's executions cache. Nothing is captured
    into the fills ledger here - publish_state's end-of-run sweep stays exactly
    as it was. Under --dry nothing at all is written: no cache row, no coverage
    stamp, no anchor moved or deleted, no pocket file.

    Returns the dict run() installs as _POCKET_RUN. p is None, and every
    consumer falls back to the pre-pocket rule, whenever anything here cannot be
    established: the executions read failed, came back missing fills the
    ledger or cache prove exist, or came back empty while IB had accepted a bot
    order in the coverage window; coverage since the anchor has a gap; there is
    no anchor; stamping is not confirmed; or the canary fired. Never raises.

    COVERAGE (board review 2026-09-17, "The pocket never checks for gaps in
    coverage"): IB's read reaches back 7 days and the git fills ledger loses
    unpushed rows to publish_web's hourly reset, so an SEHK buy that filled
    during a 7-day outage was in neither while the conversion that paid for it
    was - P too high by the buy. A LIVE run therefore keeps every row IB
    returns in earmark.EXECS_FILE, and stamps earmark.COVERED_FILE once the read
    passed every check below. A gap (earmark.coverage_gap) deletes the anchor.

    THE ANCHOR moves only on a live run that sees BASE_CCY cash < 1 right now,
    before anything in this run can move it - the one moment no pot and no
    pocket can exist. It is not moved if an execution already carries this very
    minute: the ledger's ts has minute resolution, so such a fill cannot be put
    on either side of the balance read. It is not set in a run that found a gap
    either: that run's P is None whatever the balance.
    """
    out = {"active": True, "p": None, "confirmed": False, "anchor": None}
    try:
        import fills_capture
        from datetime import timedelta
        from broker import ExecutionFilter
        held = float(cash_by_ccy(ib).get(BASE_CCY, 0.0) or 0.0)
        try:
            fresh = fills_capture.fill_rows(
                ib.reqExecutions(ExecutionFilter(), strict=True))
        except Exception as e:
            log(f"  ! bot {BASE_CCY} pocket unknown - executions unreadable "
                f"({str(e)[:80]}); the earmark falls back to min(marker, "
                f"{BASE_CCY} held)")
            return out
        now = _now_utc()
        anchor = earmark.read_anchor()
        covered = earmark.read_covered()          # the PREVIOUS complete read
        cached, cache_intact = earmark.read_exec_cache()
        cache_saved = False
        if not dry:
            # Kept before any check below can bail out: these rows are real
            # executions whatever the verdict on this read, and once IB's window
            # has moved past them this file is where the pocket still sees them.
            try:
                earmark.update_exec_cache(cached, fresh, anchor, now)
                cache_saved = True
            except Exception as e:
                log(f"  ! executions cache not written ({str(e)[:80]}) - no "
                    f"coverage stamp this run")
        rows, missing = earmark.merge_executions(_read_fills_ledger(), fresh, now,
                                                 cached_rows=cached)
        if missing:
            log(f"  !! bot {BASE_CCY} pocket unknown - IB's executions read is "
                f"missing {len(missing)} recent fill(s) the ledger or cache holds "
                f"({', '.join(missing[:2])}); a partial read would overstate the "
                f"pocket, so the earmark falls back to min(marker, {BASE_CCY} held)")
            return out
        try:
            import ib_orders
            orders_ledger = ib_orders.ORDERS_LEDGER
        except Exception:
            orders_ledger = None
        recent = earmark.bot_submissions(
            orders_ledger, now - timedelta(days=earmark.COVERAGE_MAX_DAYS))
        if not fresh and recent:
            # IB answers a session's first trades call with [] (ib_orders.trades)
            # and three retries do not always get past it. An empty 7-day window
            # is only real if nothing filled all week; an order IB accepted from
            # the bot inside it says something probably did - the 01:30 SEHK buy
            # a 09:00 read must show. Every accepted order counts, however
            # recent: one can fill seconds after it is accepted. The price is a
            # fallback run in a week whose every bot order went unfilled.
            log(f"  !! bot {BASE_CCY} pocket unknown - IB's executions read came "
                f"back EMPTY although IB accepted {len(recent)} bot order(s) in the "
                f"last {earmark.COVERAGE_MAX_DAYS:g} days ({', '.join(sorted(recent)[:3])}); "
                f"an empty read would overstate the pocket, so the earmark falls "
                f"back to min(marker, {BASE_CCY} held)")
            return out
        unseen = earmark.unmatched_submissions(
            rows, recent, now - timedelta(days=SUBMIT_SETTLED_DAYS))
        if unseen:
            # Log only. A bot order with no execution anywhere is far more often
            # a DAY order that expired unfilled (a limit that never traded, a
            # PreSubmitted order in a shut venue) than a fill IB left out of a
            # non-empty read, and IB's order status only covers the current
            # session, so the two cannot be told apart. Refusing the pocket on it
            # would fall back for days after every unfilled order, converting
            # USD into HKD the bot then cannot sell back.
            log(f"  note: {len(unseen)} bot order(s) IB accepted "
                f"{SUBMIT_SETTLED_DAYS:g}-{earmark.COVERAGE_MAX_DAYS:g} days ago have no "
                f"execution in IB's read, the ledger or the cache "
                f"({', '.join(unseen[:3])}) - expired unfilled, or a fill IB did not "
                f"return; check IB's trade history if the pocket looks high")
        gap = earmark.coverage_gap(anchor, covered, cache_intact, now)
        if gap and dry:
            log(f"  !! --dry: bot {BASE_CCY} pocket COVERAGE GAP - {gap}; a live run "
                f"would delete the pocket anchor {anchor} (not deleted)")
        elif gap:
            try:
                earmark.clear_anchor()
                log(f"  !! bot {BASE_CCY} pocket COVERAGE GAP - {gap}. Pocket anchor "
                    f"{anchor} DELETED: a fill in the gap may never have been seen, "
                    f"so the pocket re-anchors only at the next live run that sees "
                    f"{BASE_CCY} < 1; until then the earmark is min(marker, "
                    f"{BASE_CCY} held)")
            except Exception as e:
                # A stamp over an anchor that survived would vouch for the gap.
                cache_saved = False
                log(f"  !! bot {BASE_CCY} pocket COVERAGE GAP - {gap}, and the anchor "
                    f"could NOT be deleted ({str(e)[:80]}); no coverage stamp "
                    f"written, the earmark is min(marker, {BASE_CCY} held)")
        if cache_saved:                               # live only: see above
            try:
                earmark.write_covered(now)
            except Exception as e:
                log(f"  ! coverage stamp not written ({str(e)[:80]})")
        if gap:
            return out
        if held < 1:
            stamp = earmark.utc_minute(now)
            if any(str(r.get("ts") or "")[:16] == stamp for r in rows):
                log(f"  {BASE_CCY} held {held:,.2f} but an execution shares this "
                    f"minute ({stamp}); pocket anchor left at {anchor}")
            elif dry:
                log(f"  --dry: {BASE_CCY} held {held:,.2f} - a live run would move "
                    f"the pocket anchor to {stamp} (not written)")
                anchor = stamp
            else:
                earmark.write_anchor(stamp)
                if anchor != stamp:
                    log(f"  {BASE_CCY} held {held:,.2f}: pocket anchor {anchor} -> {stamp}")
                anchor = stamp
        out["anchor"] = anchor
        p, detail = earmark.bot_pocket(rows, anchor, BASE_CCY,
                                       earmark.bot_submitted_order_ids(orders_ledger))
        out["confirmed"] = bool(detail.get("confirmed"))
        if p is None:
            loud = "!! " if detail.get("broken") else ""
            log(f"  {loud}bot {BASE_CCY} pocket not used - {detail.get('reason')}; "
                f"the earmark is min(marker, {BASE_CCY} held)")
            return out
        out["p"] = p
        log(f"  bot's own {BASE_CCY} pocket {p:,.0f} since {anchor} "
            f"({detail['stamped']} stamped fill(s), {detail['unstamped_out']} "
            f"unstamped debit(s); {detail['unstamped_in_ignored']} unstamped "
            f"credit(s) left as the operator's)")
    except Exception as e:
        log(f"  ! bot {BASE_CCY} pocket skipped ({str(e)[:100]}); the earmark is "
            f"min(marker, {BASE_CCY} held)")
        out.update(p=None, confirmed=False)
    return out


def _write_pocket_file():
    """LIVE runs only. The last pocket, for every process that does not sweep
    executions (publish_web hourly, the digest, ib_commands, publish_only).

    pending is the BASE_CCY claimed by HK buys still working from an earlier run
    or placed in this one (_FX_COMMITTED - reserve_working_cash plus in-run
    reservations). Those readers subtract it, so between runs a working bot buy
    is taken out of the pocket BEFORE it fills: an over-exclusion while it works,
    which is the safe direction, never an under-exclusion after it fills.
    """
    try:
        body = earmark.write_pocket(_POCKET_RUN.get("p"),
                                    _FX_COMMITTED.get(BASE_CCY, 0.0),
                                    _POCKET_RUN.get("confirmed"),
                                    _POCKET_RUN.get("anchor"), _now_utc())
        if body["confirmed"]:
            log(f"  pocket file: {BASE_CCY} {body['p']:,.0f}, pending {body['pending']:,.0f}")
    except Exception as e:
        # A previous run's file would stay "fresh" for up to 36h without the HK
        # buys this run placed in its pending - remove it so the readers fall
        # back to the plain cap now rather than trust it.
        try:
            earmark.POCKET_FILE.unlink()
        except Exception:
            pass
        log(f"  note: pocket file not written ({str(e)[:80]}) - publishers fall "
            f"back to min(marker, {BASE_CCY} held)")


def _drop_pocket_file():
    """LIVE runs only: right after the sweep, before any order can be sent.

    Board review 2026-09-17 ("A run that aborts leaves the previous run's pocket
    file in place..."): only a run that FINISHES rewrote the file. A run that
    spent the pocket on an HK BUY and then died - an exception, Ctrl-C at a
    CONFIRM, SIGKILL, a reboot - left the previous run's file (pending 0) fresh
    for up to 36h, and once the buy filled, publish_web, the digest and
    ib_commands counted the earmarked cash as trading money. Removing it here
    covers every way a run can die, which an except branch cannot: the readers
    fall back to min(marker, HKD held), over-excluding, until the end of a
    finished run writes the file again. Never raises.
    """
    try:
        earmark.POCKET_FILE.unlink()
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"  ! pocket file not removed ({str(e)[:80]}) - if this run dies, the "
            f"previous run's pocket stays in use by the publishers")


def _save_state_on_abort(state, dry, err):
    """run() is dying on an exception: keep what it has already recorded.

    state.json used to be written once, at the very end of run(). An exception
    after an order was sent - a transient 500 on the ledger read while funding
    the NEXT candidate - skipped that write, so the order that had gone out
    never got its state['map'] / state['pos'] entry. It filled at the open, and
    the exit loop skips a holding with no map entry without a word: no trailing
    stop, no regime exit, no time stop, until someone edited state.json by hand.

    Why one handler around run() rather than a save after every entry - the
    smallest design that closes it:
      * the happy path is unchanged: state.json is still written ONCE per run.
        (save_state is an atomic replace since board review 2026-09-21, so a
        mid-run write would no longer risk a torn file - but it would still buy
        nothing this handler does not already give);
      * it also keeps the exit loop's ratchets and entry-date backfills, which a
        per-entry save would still lose to an abort before the first entry;
      * the whole in-memory dict is safe to write: everything in it is what a
        run that finished would have saved - ratchets and backfills decided on
        the cards, map/pos written only for orders place() actually sent - so an
        aborted run persists a subset of a full run's decisions, nothing else.
    Not written when state is None: load_state() itself failed (or was never
    reached), and a state.json that cannot be parsed must stay on disk for
    repair, not be replaced. Nothing is published - publish_state commits and
    pushes, and the abort may well be the network; the next run publishes. The
    pocket file needs nothing here: a live run already removed it right after
    its sweep (_drop_pocket_file). The exit-attempts memo keeps its own rules.

    "The next run publishes" used to be true of the positions only. PLACED - the
    rows of every order this run sent, and IB's refusal text for those it
    refused - lives in memory, so it died with the process, and the next run
    started from an empty list: a BUY that filled at the open showed up in
    Positions with no History row, and the fill could not surface one either,
    since the page only upgrades rows that exist (board review 2026-09-21). So
    the rows are carried in state["_unpublished_activity"], which the next
    LIVE run puts back at the front of PLACED once, before it sends anything
    (state.json is gitignored, so the hourly reset cannot wipe them the way it
    would a local data/bot_state.json). The HALT notice row is left out: the
    next run adds that row itself (see _kill_noted), and two would show. The
    ADOPT notice is kept: the adoption is saved with this state, so no later run
    adds that row again (review 2026-10-23).
    Never raises: the caller re-raises the ORIGINAL exception.
    """
    if state is None:
        return
    if dry:
        log(f"--dry: run aborted ({type(err).__name__}) - state.json NOT written")
        return
    try:
        rows = [dict(r) for r in PLACED if r.get("action") != "HALT"]
        if rows:
            state["_unpublished_activity"] = rows
        save_state(state)
        log(f"!! run aborted ({type(err).__name__}: {str(err)[:120]}) - state.json "
            f"saved first, so any order already sent keeps its map entry and stops"
            + (f"; {len(rows)} activity row(s) kept for the next live run to "
               f"publish" if rows else ""))
    except Exception as e:
        log(f"!! run aborted AND state.json could not be saved ({str(e)[:80]}) - "
            f"check state['map'] against the IB positions by hand")


def _conid_cache_writes(on):
    """Switch ib_orders' conid-cache WRITES on or off; returns the previous
    setting, or None when ib_orders cannot be imported (nothing to switch).

    run() turns writes off for --dry and restores the old value when it ends
    (review 2026-09-17: a preview rewrote /root/conid_cache.json). Reads stay on.
    """
    try:
        import ib_orders
    except Exception:
        return None
    prev = bool(getattr(ib_orders, "CACHE_WRITES", True))
    ib_orders.CACHE_WRITES = bool(on)
    return prev


def _generated_at(doc):
    """(UTC datetime or None, raw value) of a published file's "generated_at":
    the UTC time the engine build that wrote it STARTED its price download."""
    raw = doc.get("generated_at") if isinstance(doc, dict) else None
    return parse_generated_at(raw), raw


def _build_behind_close(ysym, built, now_utc):
    """None when a build that started at `built` can hold ysym's newest finished
    bar at now_utc - or there is nothing to check (no build time, crypto, an
    unlisted suffix) - else (why, settle_utc) for the log and the alert.

    Review 2026-09-17, "The settle check uses the bot's own clock, not the time
    the card was built": market_decidable passes JP at 09:00 UTC, but the newest
    published build often started at ~04:45Z, 13:45 JST, so the card's last bar
    was an in-session print. A build is good for a market only if it began at or
    after that market's last close + SESSION_SETTLE_MIN."""
    settle = last_settled_close(ysym, now_utc)
    if built is None or settle is None or built >= settle:
        return None
    return (f"the newest build started {built:%Y-%m-%d %H:%M}Z, before its last "
            f"close settled at {settle:%Y-%m-%d %H:%M}Z"), settle


# A deferral on its own is routine and only logged: the weekday 09:00 UTC run
# often reads a build that started before Tokyo's close had settled, and JP is
# then simply decided at 23:35, still before Tokyo's next open. What the
# operator needs to hear about is a signal build that has STOPPED - no build for
# longer than a day means every market is being deferred run after run.
STALE_SIGNALS_ALERT_H = 26


def _stale_signals_alert(day, ysym, built, settle):
    """ONE alert per UTC day (once=True, keyed by the date) - but only when the
    newest build is more than STALE_SIGNALS_ALERT_H old. Live runs only; never
    raises (alerts.enqueue)."""
    if (_now_utc() - built).total_seconds() < STALE_SIGNALS_ALERT_H * 3600:
        return False
    return alerts.enqueue(
        f"signals-stale-{day}",
        f"⚠️ Signals are stale: the newest build started {built:%Y-%m-%d %H:%M} "
        f"UTC, over {STALE_SIGNALS_ALERT_H}h ago and before the close it needs "
        f"had settled ({ysym}: "
        f"{settle:%Y-%m-%d %H:%M} UTC). The bot is deferring decisions on every "
        f"market its data does not cover yet - no exits, no stop ratchets, no "
        f"entries there - until a fresh build is published. Check the hourly "
        f"signal build (GitHub Actions) if this repeats.",
        once=True)


def _sells_by_conid():
    """True when an order for a held position's own contract routes by its
    conId. The web shim places every order by conId; the socket backend sends
    the raw position contract direct-routed, which this account refuses (Error
    10311), so there only the qualified SMART contract may be sold."""
    try:
        import broker
        return str(getattr(broker, "BACKEND", "")).strip().lower() == "web"
    except Exception:
        return False


def _exit_contract_mismatch_alert(ysym, sym_local, qty, reason, held_cid, card_cid,
                                  sold_held, run_stamp):
    """Alert that an exit's card symbol and the held position are different
    instruments under one IB symbol. Live runs only; never raises."""
    what = (f"The bot sold the HELD contract (conId {held_cid}) instead."
            if sold_held else
            "The bot sent NO order: this backend cannot sell the held contract "
            "safely. Sell by hand if you still want out.")
    return alerts.enqueue(
        f"exit-conid-mismatch-{ysym}-{run_stamp}",
        f"⚠️ {ysym} exit ({reason}, {qty} held under IB symbol {sym_local}): "
        f"{ysym} resolves to conId {card_cid}, but the position held under "
        f"{sym_local} is conId {held_cid} - two instruments share one IB "
        f"symbol. {what} Check state['map'][{sym_local!r}] against the IB "
        f"positions.")


def _card_missing_alert(day, ysym, sym_local, qty):
    """A HELD position whose product card the build does not have (a 404): ONE
    alert per symbol per UTC day (once=True, keyed by the date). Live runs
    only; never raises (alerts.enqueue).

    The exit loop skipped such a symbol with no log line and no alert (board
    review 2026-09-21). Pages publishes a fresh docs/ each build, so a card the
    engine did not build that time is simply gone - Yahoo stops returning data
    after a rename (SQ became XYZ), or analyze() raised on it - and the bot
    quietly stops managing the position: no trailing stop, no regime break, not
    even the 60-bar time stop, which needs only a price. The stale-build case
    was already alerted after STALE_SIGNALS_ALERT_H; a missing card was not."""
    return alerts.enqueue(
        f"card-missing-{day}-{ysym}",
        f"⚠️ {ysym}: the signal build has no product card for it (404). The bot "
        f"is NOT managing this position ({qty} held under IB symbol {sym_local}): "
        f"no trailing stop, regime break or time stop until a card is published "
        f"again. Likely a Yahoo rename or an analyze() failure - fix the symbol "
        f"in state['map'][{sym_local!r}] or exit by hand. Repeated once a day "
        f"while it lasts.",
        once=True)


def _run_died_alert(err):
    """A LIVE trading run that raised: ONE alert per UTC hour (once=True, keyed
    by the hour). Never raises (alerts.enqueue); main() calls it through _alert.

    Before this a run that died - ssodh/init failing four times at 23:35, or
    Pages erroring on data.json - left a traceback in /root/bot.log and nothing
    else, and bot.log is read only over SSH. The publisher and the 23:40 digest
    use other endpoints, so they stayed green: the dashboard looked fresh and
    the digest still listed 'SELL XYZ @ MKT' for an exit that was never sent
    (board review 2026-09-21). EU and HK get one decision a day, so a breached
    stop there waited a day at least, and a persistent cause stopped every exit
    with no notice at all. Plain text: drain() escapes it."""
    now = _now_utc()
    return alerts.enqueue(
        f"run-died-{now:%Y-%m-%dT%H}",
        f"⚠️ Trading run DIED at {now:%H:%M} UTC ({type(err).__name__}: "
        f"{str(err)[:200]}). Exits, stop ratchets and entries did not all "
        f"complete, so a SELL listed in the digest may NOT have been sent - check "
        f"the dashboard's History. Nothing retries before the next scheduled run. "
        f"Traceback: /root/bot.log.",
        once=True)


# How long a LIVE run waits for the run lock (runlock.py) before giving up. The
# poller holds it for the seconds a phone SELL takes, a sibling trading run
# (the Monday catch-up beside the 09:00 run) for a few minutes; ten minutes
# covers both with room to spare, and is still well inside every market's
# pre-open window. A run that cannot get it exits with RUN_LOCK_EXIT.
RUN_LOCK_WAIT_S = 600
RUN_LOCK_EXIT = 3


def _run_lock_alert(waited_s, lock_file):
    """A live run that could not take the run lock: ONE alert per UTC hour.
    Never raises (alerts.enqueue)."""
    holder = "unknown"
    try:
        holder = Path(lock_file).read_text(encoding="utf-8").strip()[:120] or holder
    except Exception:
        pass                                   # the note is optional; the alert is not
    now = _now_utc()
    return alerts.enqueue(
        f"run-lock-{now:%Y-%m-%dT%H}",
        f"⚠️ Trading run SKIPPED at {now:%H:%M} UTC: the run lock was still held "
        f"after {waited_s:.0f}s (holder: {holder}). Nothing was read or sent - no "
        f"exits, stop ratchets or entries this run. Another process that sends "
        f"orders (the phone-command poller, or a second trading run) is stuck; "
        f"check it on the VM. The lock is released when that process exits.",
        once=True)


# ---------------- cash parking: SGOV, SHADOW mode only ----------------
def park_plan(usd, buffer_usd, px, parked_qty):
    """('BUY' | 'SELL', shares) that parking would send, or None. Pure.

    Park what is left above `buffer_usd` once it reaches PARK_MIN_USD; unpark,
    up to the shares held, only once spendable USD has fallen under HALF the
    buffer - the gap between the two keeps a run from buying back what the last
    one sold. A BUY is sized at the limit (px x (1 + LIMIT_BUFFER)), so it never
    spends more than the excess it was computed from."""
    try:
        px, usd, buffer_usd = float(px), float(usd), float(buffer_usd)
        parked_qty = float(parked_qty or 0)
    except (TypeError, ValueError):
        return None
    if not (px > 0 and usd == usd and buffer_usd == buffer_usd):
        return None
    excess = usd - buffer_usd
    if excess >= PARK_MIN_USD:
        n = int(math.floor(excess / (px * (1.0 + LIMIT_BUFFER))))
        return ("BUY", n) if n > 0 else None
    if parked_qty > 0 and usd < buffer_usd / 2.0:
        n = min(int(math.ceil((buffer_usd - usd) / px)), int(parked_qty))
        return ("SELL", n) if n > 0 else None
    return None


def park_idle_usd(ib, held, open_syms, per_slot, nl, usd_short):
    """Cash parking of idle USD in PARK_SYMBOL - SHADOW mode: one log line on
    what a live mode WOULD park or unpark this run. Places nothing, writes
    nothing, reads no quote (owner decision 2026-10-26: shadow first, live only
    after about a week of these lines and a check that the account may hold a
    US-domiciled ETF).

    Measured where a live mode would act: AFTER the entries loop, from
    _spendable(USD), so the USD this run's entries and every working order
    (VLO's) have claimed is already netted out - parking from the raw balance
    would spend it. The buffer is PARK_BUFFER_SLOTS slots, at the larger of
    this run's per-slot size and NetLiq/TARGET_POSITIONS. Also logs why a live
    mode would hold off: a parking order already working, working orders whose
    cash went unreserved, or a USD entry that was short of cash this run (the
    next run's entries need that USD more). Returns the plan, or None."""
    if PARK_MODE == "off":
        return None
    if PARK_MODE != "shadow":
        log(f"  !! park: PARK_MODE {PARK_MODE!r} is not built - only 'shadow' "
            f"(log only) exists, so nothing is placed")
    rate = fx_rate(ib, "USD", BASE_CCY)
    if not rate or rate != rate or rate <= 0:
        log(f"park (SHADOW): no USD/{BASE_CCY} rate - nothing to report this run")
        return None
    slot = max(float(per_slot or 0.0),
               (float(nl) / TARGET_POSITIONS) if TARGET_POSITIONS > 0 else 0.0)
    buffer_usd = PARK_BUFFER_SLOTS * slot / rate
    usd = _spendable(ib, "USD")
    pos, parked = held.get(PARK_SYMBOL) or (None, 0)
    px = PARK_REF_PX
    try:
        avg = float(getattr(pos, "avgCost", 0) or 0)
        if avg == avg and avg > 0:
            px = avg
    except (TypeError, ValueError):
        pass
    plan = park_plan(usd, buffer_usd, px, parked)
    hold = []
    if PARK_SYMBOL in open_syms:
        hold.append(f"a {PARK_SYMBOL} order is already working")
    if _RESERVE_OK.get("ok") is not True:
        hold.append("not every working order's cash is reserved")
    if usd_short:
        hold.append("a USD entry was short of cash this run")
    head = (f"park (SHADOW - nothing placed): spendable USD {usd:,.0f} after "
            f"entries, keep {buffer_usd:,.0f} ({PARK_BUFFER_SLOTS} slots of "
            f"{slot:,.0f} {BASE_CCY}), {PARK_SYMBOL} held {float(parked or 0):g}")
    if plan is None:
        log(head + " -> nothing to park or unpark")
    else:
        side, n = plan
        log(head + f" -> would {side} {n} {PARK_SYMBOL} (~US${n * px:,.0f} at "
            f"~{px:g})" + (f"; a live mode would hold off: {'; '.join(hold)}"
                          if hold else ""))
    return plan


# ---------------- market trend filter: SHADOW mode only ----------------
# The tag a would-have-skipped BUY carries on its History row. The page shows
# a "filter would have skipped" label for any row that has it.
MARKET_FILTER_TAG = "market filter would have skipped"
# This live run's view of the filter, for publish_state - set by run(), carried
# over by every publish without a trading run, exactly like _RUN_CONFIG.
_RUN_MARKET_FILTER = {}
# The BUYs this run sent that the filter would have skipped (the run list; the
# rolling record across runs is state["_market_filter_skips"]).
_FILTER_SKIPPED_RUN = []


def market_filter_mode():
    """"off" or "shadow". Anything else - "live" included - is NOT built and
    runs as shadow: an unknown value must never start gating orders."""
    return "off" if MARKET_FILTER == "off" else "shadow"


def market_trend_of(data):
    """data.json's "market_trend", checked row by row: {market: row} for each of
    US/HK/JP/EU whose row is usable, holding only the fields the log, the
    published block and the page read. A missing or malformed key (an older
    build), or a market the engine could not judge, gives fewer rows or {}:
    that market is UNKNOWN, which tags nothing. A row whose `below` contradicts
    its own numbers is not trusted either. Never raises."""
    out = {}
    try:
        raw = data.get("market_trend") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            return {}
        for m in MARKET_FILTER_MARKETS:
            r = raw.get(m)
            if not isinstance(r, dict) or not isinstance(r.get("below"), bool):
                continue
            try:
                close, sma = float(r["close"]), float(r["sma100"])
                pct = float(r["pct_vs_sma100"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (all(math.isfinite(x) for x in (close, sma, pct))
                    and close > 0 and sma > 0):
                continue
            # rounded to 2dp by the engine, so equal-looking numbers are allowed
            if r["below"] != (close < sma) and abs(close - sma) >= 0.01:
                continue
            bar = str(r.get("bar") or "")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", bar):
                continue
            out[m] = {"index": str(r.get("index") or "")[:16],
                      "name": str(r.get("name") or "")[:40],
                      "close": close, "sma100": sma, "pct_vs_sma100": pct,
                      "below": r["below"], "bar": bar}
    except Exception:
        return {}
    return out


def market_filter_summary(trend):
    """One line on every market, for the run log: "US above +0.26% (bar
    2026-10-23) · HK below -6.60% ..."; a market with no usable row is
    "unknown"."""
    parts = []
    for m in MARKET_FILTER_MARKETS:
        r = trend.get(m)
        parts.append(f"{m} unknown" if not r else
                     f"{m} {'below' if r['below'] else 'above'} "
                     f"{r['pct_vs_sma100']:+.2f}% (bar {r['bar']})")
    return " · ".join(parts)


def entry_market(a):
    """A BUY row's home market for the filter - US, HK, JP or EU - else None
    (crypto and anything else has no index, as in the backtest's index map).
    The row's own "market" first: it is the engine's market_of, the key the
    backtest filtered on, so CHF/DKK/SEK/NOK names count as EU (keying on the
    currency would leave them out). Only a row without one falls back to the
    symbol's suffix: .HK, .T, a European session in market_clock, none = US."""
    m = str(a.get("market") or "").upper()
    if m:
        return m if m in MARKET_FILTER_MARKETS else None
    sym = str(a.get("symbol") or "").strip().upper()
    if not sym or sym.endswith("-USD"):
        return None
    if "." not in sym:
        return "US"
    suffix = "." + sym.rsplit(".", 1)[1]
    if suffix == ".HK":
        return "HK"
    if suffix == ".T":
        return "JP"
    return "EU" if suffix in MARKET_SESSIONS else None


def shadow_filter_hit(a, trend):
    """(market, row) when the filter WOULD have skipped this BUY row - its
    market's index closed below its 100-day average - else None: above,
    unknown, crypto, or any error. Pure; decides nothing about the order."""
    try:
        m = entry_market(a)
        r = trend.get(m) if m else None
        return (m, r) if isinstance(r, dict) and r.get("below") is True else None
    except Exception:
        return None


def shadow_filter_note(state, ysym, hit, rows, dry):
    """SHADOW mode, for one BUY the bot has just sent (or, under --dry, would
    send) in a market that is below: the log line, the tag on its History
    row(s), the run list, and - live only - a row in state's rolling record,
    which state.json keeps across runs and publish_state publishes. The order
    itself is already out, as it would be without the filter. Never raises."""
    try:
        m, r = hit
        log(f"  shadow market filter: would have skipped {ysym} ({m} index "
            f"{r['close']:,.2f} is {abs(r['pct_vs_sma100']):.2f}% below its 100-day "
            f"average, bar {r['bar']})")
        for row in rows:
            row["shadow"] = MARKET_FILTER_TAG
        from datetime import datetime, timezone
        when = (rows[-1].get("time") if rows else None) or \
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        rec = {"time": str(when), "symbol": str(ysym), "market": m,
               "pct": r["pct_vs_sma100"]}
        _FILTER_SKIPPED_RUN.append(rec)
        if dry:
            return                       # --dry writes nothing, state included
        kept = state.get("_market_filter_skips")
        kept = [x for x in kept if isinstance(x, dict)] if isinstance(kept, list) else []
        kept.append(rec)
        state["_market_filter_skips"] = kept[-MARKET_FILTER_KEEP:]
    except Exception as e:
        log(f"  note: shadow market filter note skipped ({str(e)[:100]})")


def _market_filter_block(state, prev):
    """bot_state.json "market_filter": this live run's (mode, the markets it
    read, the rolling would-have-skipped record from state), or the previous
    file's, carried over by a publish without a trading run - publish_only
    here, and publish_web.py every hour - exactly as "config" is. None when
    there is nothing to publish. Never raises."""
    try:
        if _RUN_MARKET_FILTER:
            kept = state.get("_market_filter_skips") if isinstance(state, dict) else None
            kept = [dict(x) for x in kept if isinstance(x, dict)] \
                if isinstance(kept, list) else []
            return {"mode": _RUN_MARKET_FILTER.get("mode"),
                    "at": _RUN_MARKET_FILTER.get("at"),
                    "markets": _RUN_MARKET_FILTER.get("markets") or {},
                    "would_have_skipped": kept[-MARKET_FILTER_KEEP:]}
        pm = prev.get("market_filter") if isinstance(prev, dict) else None
        return pm if isinstance(pm, dict) else None
    except Exception:
        return None


# ---------------- main reconcile ----------------
def run(dry=False):
    """One trading run. A live run from the command line comes through
    run_locked(), which holds the run lock around all of it; nothing in here
    may take that lock itself."""
    global _FX_REMEMBER
    _FX_REMEMBER = not dry        # --dry must not write the rate memory either
    _RATE_CACHE.clear()
    _STALE_RATES.clear()
    _FX_PENDING.clear()
    _FX_PENDING_CCY.clear()
    _FX_COMMITTED.clear()
    _RESERVE_OK.clear()
    _EARMARK_RUN.clear()
    _EXC_APPLIED.clear()
    _POCKET_RUN.clear()
    # The rules this run trades with, as they are in effect (env included), for
    # publish_state. A --dry run publishes nothing, so it never replaces them.
    _RUN_CONFIG.clear()
    if not dry:
        _RUN_CONFIG.update(effective_config(dry))
    _RUN_MARKET_FILTER.clear()
    del _FILTER_SKIPPED_RUN[:]
    data = get_json(SIGNALS_URL)
    actions = [a for a in data.get("actions", []) if a.get("action") in ("BUY", "BUY/HOLD")]
    log(f"signals {data.get('generated')}: {len(actions)} BUY candidates")
    log(f"rules: {TARGET_POSITIONS} slots sized free cash / free slots, cap "
        f"{MAX_ORDER_BASE:,.0f} {BASE_CCY}, kill switch "
        f"{'ON' if KILL_SWITCH else 'off'}, crypto entries blocked, cash parking "
        f"{PARK_MODE}, market filter {market_filter_mode()}, HK entries "
        f"{'on' if HK_ENABLED else 'off'}, FX_CONVERT {'ON' if FX_CONVERT else 'off'}")
    # The market filter, SHADOW only: read from the same data.json, once. It is
    # consulted after every existing check, only to annotate a BUY already
    # decided - see MARKET_FILTER. A missing or malformed key is "unknown" and
    # tags nothing. Guarded on its own: this runs before the exit loop, and a
    # log-only report must never cost the run its exits.
    mf_mode, trend = market_filter_mode(), {}
    try:
        trend = market_trend_of(data) if mf_mode == "shadow" else {}
        if mf_mode == "off":
            log("market filter: off (code flag MARKET_FILTER) - nothing logged or tagged")
        else:
            if MARKET_FILTER != "shadow":
                log(f"  !! market filter: MARKET_FILTER {MARKET_FILTER!r} is not built "
                    f"- only 'shadow' (log only) exists, so it runs as shadow")
            log("market filter (SHADOW - log only, every order is sent as without "
                "it): " + (market_filter_summary(trend) if trend else
                           "data.json carries no usable market_trend - every market "
                           "unknown, nothing will be tagged"))
        if not dry:
            from datetime import datetime as _mf_dt, timezone as _mf_tz
            _RUN_MARKET_FILTER.update({
                "mode": mf_mode,
                "at": _mf_dt.now(_mf_tz.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "markets": json.loads(json.dumps(trend))})
    except Exception as e:
        trend = {}
        log(f"  note: market filter (shadow) report skipped ({str(e)[:100]}) - "
            f"every market unknown, nothing will be tagged")

    ib = IB()
    connect_or_heal(ib, CLIENT_ID, 30)
    # HOST/PORT describe the SOCKET transport only. Under IB_BACKEND=web the
    # shim ignores them entirely and talks to the live account over OAuth, so
    # printing "127.0.0.1:4002 (PAPER)" there would be actively misleading.
    if os.environ.get("IB_BACKEND", "socket").strip().lower() == "web":
        log("connected via IBKR Web API (OAuth) — LIVE account")
    else:
        log(f"connected {HOST}:{PORT} ({'PAPER' if PORT == 4002 else 'LIVE'})")
    state = None                  # until load_state() succeeds: see _save_state_on_abort
    # --dry writes nothing, the conid cache included (review 2026-09-17). Set
    # here, where every conid lookup of the run is still ahead - the signals
    # fetch and the connect above resolve none - and put back in the finally.
    cache_writes_before = _conid_cache_writes(not dry)
    try:
        # The bot's own HKD, from IB's executions, BEFORE net_liq: the exclusion
        # that sizes this run needs it. In memory only - --dry included.
        _POCKET_RUN.update(_sweep_pocket(ib, dry))
        if not dry:
            _drop_pocket_file()   # before any order: a run that dies leaves none
        nl = net_liq(ib)
        warm_fx_memory(ib, actions)
        # (the earmark is frozen below, once working-order reservations are known)
        state = load_state()
        if not dry:
            # Rows an aborted live run sent but could not publish (see
            # _save_state_on_abort), put back at the FRONT of this run's
            # activity, before anything is sent, so they keep their order and
            # their original times (the page matches a fill to a row sent within
            # a few days of it). Popped here, so the save below drops the key:
            # published exactly once by this run's publish_state, or carried
            # again if this run aborts too. --dry leaves them on disk for the
            # next live run.
            carried = state.pop("_unpublished_activity", None) or []
            if not isinstance(carried, list):
                carried = []
            if carried:
                PLACED[:0] = [dict(r) for r in carried if isinstance(r, dict)]
                log(f"  {len(carried)} activity row(s) from an aborted run will be "
                    f"published with this run's")
        # --- kill-switch: gates NEW ENTRIES ONLY (checked before the entries
        # loop below). It previously returned HERE, before the exit loop —
        # freezing regime/trailing/time exits exactly when a drawdown is
        # deepest. That fired for real 2026-08-03..06: a Sunday cash
        # WITHDRAWAL left NetLiq 11% under the stale pre-withdrawal peak and
        # every run froze silently for four days (board-predicted 2026-08-01:
        # "the withdrawal case is the dangerous asymmetry"). Exits must never
        # depend on this gate. Since 2026-10-23 the peak also accounts for
        # deposits and withdrawals - see kill_switch_check. Since 2026-10-26 it
        # is OFF (KILL_SWITCH): no check, no HALT row, entries never gated on
        # the peak. The code below is kept whole for the day it comes back.
        if KILL_SWITCH:
            killed, peak, kill_basis = kill_switch_check(state, nl)
        else:
            killed, peak, kill_basis = False, None, "off"
            log("kill switch: OFF (code flag KILL_SWITCH, owner decision "
                "2026-10-26) - entries are not gated on the NetLiq peak")
        # The day the HALT row below was added, stamped into state only where
        # that row is published (review 2026-09-17, "A crashed live run saves
        # _kill_noted but throws away the HALT row"): an abort saves state.json
        # but publishes nothing, so a stamp set here reached disk without its
        # row and the same UTC day's next run skipped the notice for good.
        kill_note_day = None
        if killed:
            log(f"KILL-SWITCH: NetLiq {nl:.0f} < {(1-DAILY_LOSS_KILL)*100:.0f}% of "
                f"peak {peak:.0f} ({kill_basis}) — ENTRIES BLOCKED; exits still "
                f"run. A deposit or withdrawal counts once it is in "
                f"data/netliq_history.json 'flows', dated the day the money "
                f"moved. To re-risk after a genuine fall, set _kill_peak_since "
                f"in state.json (see execution/README.md).")
            from datetime import datetime, timezone
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if state.get("_kill_noted") != today:   # one dashboard row per day
                kill_note_day = today
                PLACED.append({"time": datetime.now(timezone.utc)
                               .strftime("%Y-%m-%d %H:%M UTC"),
                               "action": "HALT", "qty": 0, "symbol": "ENTRIES",
                               "limit": "", "ccy": BASE_CCY,
                               "reason": f"kill-switch: NetLiq {nl:.0f} vs "
                                         f"peak {peak:.0f} ({kill_basis})",
                               "status": "notice", "error": ""})
        # No per-position budget here any more: entries are sized from FREE CASH
        # over FREE SLOTS (_free_cash_base), worked out after the exits and the
        # reservations below - computing it here, before `free` exists or
        # while the book is full, would raise ahead of every exit.
        held = held_positions(ib)
        log(f"NetLiq {nl:.0f} {BASE_CCY} | {len(held)} positions")
        # Before the exits: a holding the owner bought by hand and listed in
        # adopt.json is managed from this run on (see adopt_holdings).
        adopt_holdings(state, held, dry)

        # ---- OPEN ORDERS: never double-place against a working order ----
        ib.reqAllOpenOrders()
        ib.sleep(2)
        open_syms = {t.contract.symbol for t in ib.openTrades()
                     if t.orderStatus.status in _WORKING_STATUS}
        if open_syms:
            log(f"open orders already working: {sorted(open_syms)} — will not duplicate")
        # Same book, read for a different purpose: cash that working orders have
        # already claimed must not look spendable to this run.
        reserve_working_cash(ib)
        # Freeze the earmark for this run, AFTER the reservations above are
        # known. Two reasons for each half. Frozen, because the cap rises the
        # moment the bot buys HKD, so re-deriving it per call would re-earmark
        # the very HKD just converted and make the next candidate convert again.
        # After reserve_working_cash, because the cap must be taken against cash
        # that is not already claimed by a working order - capping against the
        # raw balance double-counts it against _FX_COMMITTED and drives
        # _spendable_base negative. Still before any conversion can move the
        # balance, which is what "frozen" is for. The exclusion inside is the
        # run's pocket-aware one - with no pocket, exactly min(marker, cash).
        _EARMARK_RUN["base"] = min(
            earmark.exclusion(cash_by_ccy(ib).get(BASE_CCY, 0.0), _run_pocket()),
            max(0.0, cash_by_ccy(ib).get(BASE_CCY, 0.0)
                - _FX_COMMITTED.get(BASE_CCY, 0.0)))

        # ---- alert-only memo of earlier exits (see _exit_alerts_open) ----
        # Read here and never consulted by a decision below. Under --dry it is
        # neither read nor written, and nothing is queued: a preview writes nothing.
        from datetime import datetime as _dt, timezone as _tz
        run_stamp = _dt.now(_tz.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        exit_memo = {} if dry else (_alert(_exit_alerts_open, held, state) or {})

        # ONE instant for every market decision in this run, exits and entries
        # alike, so a run straddling a settle boundary cannot judge a market
        # both ways (see market_decidable).
        decide_now = _now_utc()
        # ...and the build the decisions read must have started after the close
        # it is judged on (see _build_behind_close). Entries read data.json's
        # generated_at; an exit reads its card's, else data.json's. Without one
        # at all a market is decided on the clock alone, exactly as before -
        # said once per run, since every decision then rests on the clock.
        sig_built, sig_raw = _generated_at(data)
        if sig_built is None:
            log(f"!! signals carry no usable generated_at "
                f"({'missing' if sig_raw is None else repr(str(sig_raw)[:40])}) - "
                f"entries are judged on the clock alone this run, and so are exits "
                f"whose card has none")
        stale_day = decide_now.strftime("%Y-%m-%d")

        # ---- EXITS first (free up cash + capital) ----
        for sym_local, (pos, qty) in list(held.items()):
            ysym = state.get("map", {}).get(sym_local)
            if not ysym:
                continue
            if sym_local in open_syms:
                continue                     # an order for it is already working
            decidable, why = market_decidable(ysym, decide_now)
            if not decidable:
                # The WHOLE close-based evaluation waits for a finished bar: no
                # hw/stop ratchet on an intraday print, no entry_date backfill,
                # no regime, trailing or time stop. `continue` also keeps this
                # symbol away from _exit_alerts_not_firing - a rule that was not
                # evaluated did not "not fire", so its memo record stays put.
                log(f"  {ysym}: exit rules deferred to a finished bar - {why}")
                continue
            if why.startswith("!!"):
                log(f"  {ysym}: {why}")
            try:
                product = get_json(PRODUCTS_URL + safe_name(ysym) + ".json")
                card = product["card"]
            except Exception as e:
                # Still skipped - nothing can be judged without the card - but
                # no longer in silence (board review 2026-09-21). `continue`
                # keeps it away from _exit_alerts_not_firing, as before. Only a
                # 404 (the build has no card for it) is alerted: a timeout is
                # usually a passing blip and the next run fetches again.
                log(f"  !! {ysym}: product card unavailable ({str(e)[:80]}) - exit "
                    f"rules NOT evaluated this run ({abs(qty)} held under {sym_local})")
                if not dry and getattr(e, "code", None) == 404:
                    _alert(_card_missing_alert, stale_day, ysym, sym_local, abs(qty))
                continue
            built = _generated_at(product)[0]
            if built is None:
                built = sig_built
            behind = _build_behind_close(ysym, built, decide_now)
            if behind:
                # Deferred exactly like the clock deferral above, and for the
                # same reason: the card's newest bar is not that close. No
                # ratchet, no rule, no _exit_alerts_not_firing.
                log(f"  {ysym}: exit rules deferred to a fresh build - {behind[0]}")
                if not dry:
                    _alert(_stale_signals_alert, stale_day, ysym, built, behind[1])
                continue
            price = card.get("price")
            sma200 = card.get("sma200")
            atr = card.get("atr") or 0
            st = state.setdefault("pos", {}).get(ysym, {})
            # exact trailing stop maintained here (server-side high-water)
            hw = max(st.get("hw", price or 0), price or 0)
            k = 2.0 if (price and st.get("entry") and price >= st["entry"] + 1.5 * atr) else 3.5
            trail = max(st.get("stop", 0), hw - k * atr) if atr else st.get("stop", 0)
            st.update(hw=hw, stop=trail)
            if not st.get("entry_date"):
                # hand-over: positions opened before the time stop existed get
                # their TRUE entry date from the fills ledger (never today's,
                # which would grant them a fresh 60 bars only as last resort)
                from datetime import date
                bf = ledger_entry_date(sym_local)
                st["entry_date"] = bf or date.today().isoformat()
                log(f"  {ysym}: entry_date backfilled -> {st['entry_date']}"
                    f" ({'ledger' if bf else 'TODAY — no ledger fill found'})")
            state["pos"][ysym] = st
            bars = bars_held(st["entry_date"])
            sell = None
            if sma200 and price and price < sma200:
                sell = "regime break (close < SMA200)"
            elif trail and price and price <= trail:
                sell = f"trailing stop {trail:.2f}"
            elif MAX_HOLD_BARS and bars >= MAX_HOLD_BARS and price:
                # `and price` guard: unlike regime/trail, the time stop needs no
                # price to DECIDE — but place() prices its sanity check off it,
                # so a null-price card here would abort the whole run (and with
                # it every later exit + all entries). Skip loudly instead.
                sell = f"time stop ({bars} bars >= {MAX_HOLD_BARS})"
            elif MAX_HOLD_BARS and bars >= MAX_HOLD_BARS:
                log(f"  !! {ysym}: time stop due ({bars} bars) but card price is "
                    f"null — deferred to next run")
            if sell and qty > 0:
                log(f"EXIT {ysym}: {sell}")
                # route through a clean SMART contract — the raw position
                # contract requests direct routing (Error 10311 rejections)
                xc = to_ib(ysym)
                sold = None
                if xc is not None:
                    qx = ib.qualifyContracts(xc)
                    if qx:
                        sold = qx[0]
                # held and state['map'] are keyed by IB symbol, and two listings
                # can share one: SAN.MC (Santander) and SAN.PA (Sanofi) are both
                # "SAN". The map can then name the OTHER instrument, and selling
                # its contract for this position's quantity leaves a short in a
                # stock never held (review 2026-09-17, reproduced: SELL 91 of
                # conId 12003 against 14 held). The quantity belongs to the
                # position, so never sell a contract that is not the position's.
                held_cid = getattr(pos.contract, "conId", 0) or 0
                card_cid = (getattr(sold, "conId", 0) or 0) if sold is not None else 0
                if held_cid and card_cid and held_cid != card_cid:
                    sold_held = _sells_by_conid()
                    log(f"  !! {ysym}: CONTRACT MISMATCH - {ysym} resolves to conId "
                        f"{card_cid} but the {abs(qty)} held under {sym_local} is "
                        f"conId {held_cid}; state['map'] names the wrong instrument. "
                        + ("Selling the HELD contract by its conId, not the card's."
                           if sold_held else
                           "This backend cannot route the held contract - NO order."))
                    if not dry:
                        _alert(_exit_contract_mismatch_alert, ysym, sym_local,
                               abs(qty), sell, held_cid, card_cid, sold_held, run_stamp)
                    if not sold_held:
                        continue
                    sold = pos.contract
                xst = place(ib, sold if sold is not None else pos.contract,
                            "SELL", abs(qty), price, dry, reason=sell, mkt=True)
                # The verdict used to be thrown away here, so a refused exit was
                # a dashboard row nobody saw (BEN: refused 5 times over ~35 h).
                # Alert only - the exit is not retried or re-decided on it.
                if not dry:
                    _alert(_exit_alerts_sent, exit_memo, ysym, abs(qty), sell,
                           xst, run_stamp)
            elif not sell and price and not dry:
                # Evaluated with a real price and the rule did not fire. A null
                # price proves nothing about the rule, so that record is left
                # for a run that can judge it, as are symbols skipped above.
                _alert(_exit_alerts_not_firing, exit_memo, ysym, abs(qty), run_stamp)

        # ---- ENTRIES (tier, then top score first, up to free slots) ----
        # working BUY orders consume slots too: with two trading runs a day, a
        # 23:35 order still unfilled at the 09:00 run would otherwise let the
        # bot open a 16th position (attempted live on 2026-07-31; only IB's
        # rejection stopped it). The validated engine counts pending the same
        # way: free = slots - positions - pending. PARK_SYMBOL is cash, not a
        # stock: neither a holding of it nor a working order for it takes one
        # of the 15 slots, or parking would shrink every entry it exists to fund.
        pending_buys = {t.contract.symbol for t in ib.openTrades()
                        if t.orderStatus.status in _WORKING_STATUS
                        and t.order.action == "BUY"
                        and getattr(t.contract, "secType", "") != "CASH"
                        and t.contract.symbol not in held
                        and t.contract.symbol != PARK_SYMBOL}
        if pending_buys:
            log(f"working BUY orders hold {len(pending_buys)} slot(s): "
                f"{sorted(pending_buys)}")
        free = (TARGET_POSITIONS
                - len([s for s, (_, q) in held.items() if q > 0 and s != PARK_SYMBOL])
                - len(pending_buys))
        if killed:
            free = 0                     # kill-switch: no new entries, exits ran
        # ---- SIZING: free cash / free slots, ONCE per run (2026-10-26) ----
        # Owner decision, replacing NetLiq / TARGET_POSITIONS. Worked out here -
        # after the exits, after reserve_working_cash and the earmark freeze -
        # and never again this run: `free -= 1` below also runs for a REJECTED
        # order, so re-dividing per candidate would pour the cash of every
        # refused name into fewer, larger ones. Any failure reading the cash
        # places NO entries this run, and only that: the exits have already
        # run, and state is saved and published below as usual.
        per_slot = 0.0
        if free > 0 and _RESERVE_OK.get("ok") is not True:
            # A working order whose cash could not be reserved - a hand MARKET
            # buy has no lmtPrice, or the order book itself was unreadable -
            # still owns money that free cash would count as spare. NetLiq/15
            # usually left that money alone by accident; free cash / free slots
            # spends every free dollar, so it would turn the old reservation
            # gap into a real overdraft (review 2026-10-26: a working MKT BUY
            # 100 7272 had its yen converted for US entries, then filled into
            # about JPY -140k). The same rule as an owed balance with no rate:
            # a claim of unknown size is not guessed at - no entries this run.
            log("  !! a working order's cash could not be reserved (unpriced or "
                "unreadable order book) - NO entries this run; the exits ran "
                "and state is saved as usual")
            free = 0
        if free > 0:
            try:
                free_cash, parts = _free_cash_base(ib)
                per_slot = free_cash / free
                log(f"sizing: free cash {free_cash:,.0f} {BASE_CCY} "
                    f"({', '.join(parts) or 'none'}) / {free} free slot(s) = "
                    f"{per_slot:,.0f} per slot, cap {MAX_ORDER_BASE:,.0f}")
            except Exception as e:
                log(f"  !! free cash unreadable ({str(e)[:120]}) - NO entries this "
                    f"run; the exits ran and state is saved as usual")
                free, per_slot = 0, 0.0
            if free > 0 and not per_slot > 0:
                log("  no free cash to size an entry - no entries this run")
                free = 0
        usd_short = False                # a USD entry was short of cash (parking)
        # IB symbols this run has already sent (or, under --dry, would send) a
        # BUY for -> the Yahoo symbol. held and open_syms were read before the
        # loop, so on their own they let two listings that share one IB symbol
        # both be bought in one run: SAN.MC (Santander) and SAN.PA (Sanofi) are
        # both "SAN", state['map']['SAN'] kept only the second, and the next
        # run's exit sold Sanofi for Santander's quantity (review 2026-09-17).
        # One IB symbol, one instrument, one map key.
        entered_syms = {}
        for a in sorted(actions, key=entry_rank):
            if free <= 0:
                break
            ysym = a["symbol"]
            if is_crypto_entry(a):
                # Before to_ib/qualify: no slot, no Web API call, no state write.
                log(f"  skip {ysym}: crypto is not traded by this bot (PAXOS is "
                    f"not offered by IBKR HK; the crypto sleeve is on hold)")
                continue
            if str(ysym).upper() == PARK_SYMBOL:
                log(f"  skip {ysym}: the cash-parking ETF is never a stock entry")
                continue
            c = to_ib(ysym)
            if c is None:
                continue
            q = ib.qualifyContracts(c)
            if not q:
                log(f"  skip {ysym}: IB could not qualify"); continue
            c = q[0]
            if str(getattr(c, "secType", "") or "").upper() == "CRYPTO":
                log(f"  skip {ysym}: qualified as crypto - not traded by this bot")
                continue
            if c.symbol in held or c.symbol in open_syms or c.symbol in entered_syms:
                # held, an order is already working, or entered this run. Said
                # only when the key belongs to ANOTHER listing: a held name's own
                # BUY/HOLD signal is routine and stays as quiet as it always was.
                owner = entered_syms.get(c.symbol) or state.get("map", {}).get(c.symbol)
                if c.symbol in entered_syms or (owner and owner != ysym):
                    log(f"  skip {ysym}: IB symbol {c.symbol} is already taken by "
                        f"{owner or 'a working order'} - two instruments may not "
                        f"share one state['map'] key")
                continue
            price = a.get("price") or 0
            if price <= 0:
                continue
            decidable, why = market_decidable(ysym, decide_now)
            if not decidable:
                # The BUY was signalled on a bar still in session. Skipped
                # without a slot: a later run re-reads the signal on the close.
                log(f"  skip {ysym}: entry deferred to a finished bar - {why}")
                continue
            if why.startswith("!!"):
                log(f"  {ysym}: {why}")
            behind = _build_behind_close(ysym, sig_built, decide_now)
            if behind:
                # The signal came from a build older than that close: deferred
                # like the clock deferral above, without a slot.
                log(f"  skip {ysym}: entry deferred to a fresh build - {behind[0]}")
                if not dry:
                    _alert(_stale_signals_alert, stale_day, ysym, sig_built, behind[1])
                continue
            notional = min(per_slot, MAX_ORDER_BASE)         # in BASE_CCY
            ccy = currency_of(ysym)
            blocked = entry_blocked_reason(ysym, ccy)
            if blocked:
                log(f"  skip {ysym}: {blocked}")
                continue
            # rate = BASE_CCY per 1 <ccy> (via direct pair or USD cross)
            rate = fx_rate(ib, ccy, BASE_CCY) if ccy != BASE_CCY else 1.0
            if not rate or rate != rate or rate <= 0:
                log(f"  skip {ysym}: no {ccy}/{BASE_CCY} rate to size order")
                continue
            # LSE quotes in PENCE while the GBP rate is per POUND, so
            # notional/rate/price lands 100x too small: BP.L at 539.7 (= GBP
            # 5.40) sizes to 2 shares, ~HK$114 against a HK$14,274 budget, and
            # AZN.L shows 12,006 for a GBP 120 share. Nothing scales GBX
            # anywhere in engine/ or execution/. This was invisible while
            # lot_size wrongly returned 100 for every market - 2 // 100 * 100 = 0
            # silently dropped it - and correcting lot_size unmasks it: the stub
            # order WOULD be placed and would consume a full position slot for
            # 0.8% of its budget, with the trailing- and time-stop machinery
            # running on it. Four LSE names are WATCH right now, one dip from a
            # BUY. Skip the market until the pence scaling is fixed at the price
            # boundary; that is a data-layer change and a separate decision.
            if str(ccy).upper() == "GBP":
                log(f"  skip {ysym}: LSE prices are in pence and not scaled yet "
                    f"— sizing would be 100x too small")
                continue
            # ORDER VALUE GUARD (2026-10-26, with the HK$50k cap). Re-price at
            # IB's own quote BEFORE sizing. place() re-bases the limit to that
            # quote but keeps the share count, and IB's order-value warning is
            # auto-confirmed (ib_orders.py), so a signal price 10x too low would
            # otherwise go out as an order 10x the cap, funded at the wrong
            # price - a margin loan. More than ENTRY_MOVE_MAX apart, one of the
            # two prices is wrong and the entry is skipped (no slot: the next
            # run reads both again). Within it, size and fund at the HIGHER of
            # the two, and hand place() this same quote.
            live = live_base_price(ib, c, price)
            try:
                live = float(live)
            except (TypeError, ValueError):
                live = price
            if not (live == live and live > 0):
                live = price
            if abs(live / price - 1.0) > ENTRY_MOVE_MAX:
                log(f"  skip {ysym}: IB's quote {live:g} is {live / price - 1:+.1%} "
                    f"from the signal price {price:g} - beyond the "
                    f"{ENTRY_MOVE_MAX:.0%} guard, so one of them is wrong; not "
                    f"sized on either")
                continue
            px = max(price, live)
            # ONE FUNDING PASS: clamp the order to what ensure_ccy can actually
            # pay - the cash already in its currency plus the single largest
            # other balance (fund_from_nonbase never pools two). Free cash split
            # across USD, JPY and EUR can size a slot no one conversion pays
            # for, and that entry would fail funding on every run. A read error
            # here skips this candidate only.
            try:
                fundable = _fundable_base(ib, ccy, rate)
            except Exception as e:
                log(f"  skip {ysym}: cannot read the cash that would fund it "
                    f"({str(e)[:100]})")
                if ccy == "USD":
                    usd_short = True
                continue
            if fundable < notional * (1.0 + LIMIT_BUFFER):
                log(f"  {ysym}: one funding pass can pay ~{max(0.0, fundable):,.0f} "
                    f"{BASE_CCY} - order cut from {notional:,.0f}")
                notional = max(0.0, fundable / (1.0 + LIMIT_BUFFER))
                if ccy == "USD":
                    usd_short = True
            shares = int(notional / rate / px)
            lot = lot_size(ib, c)
            if lot <= 0:
                # A board-lot venue whose lot we could not establish. Skipping
                # is the only safe move: sizing on a guess sends an odd lot,
                # which SEHK's continuous market will not auto-match. lot_size
                # has already logged the reason - for HKD that the code is not
                # in HKEX's table, which IB has no say in - so do not restate a
                # cause here.
                log(f"  skip {ysym}: {ccy} board lot unknown "
                    f"— will not risk an odd lot")
                continue
            if lot > 1:
                shares = (shares // lot) * lot      # exchange board-lot multiple
                if shares <= 0:
                    log(f"  skip {ysym}: 1 board lot ({lot} sh ~"
                        f"{int(lot*px*rate):,} {BASE_CCY}) exceeds the position size")
                    continue
            if shares <= 0:
                continue
            # Backstop of the guard: by construction shares x px <= notional <=
            # the cap, and the limit adds LIMIT_BUFFER at most. Anything above
            # cap x 1.01 means a sizing input was wrong - never send it.
            value = shares * px * rate * (1.0 + LIMIT_BUFFER)
            if value > MAX_ORDER_BASE * 1.01:
                log(f"  !! skip {ysym}: {shares} x {px:g} would be worth ~{value:,.0f} "
                    f"{BASE_CCY}, above the {MAX_ORDER_BASE:,.0f} cap - not sent")
                continue
            # Fund only once the order is known to be placeable. Converting
            # ABOVE this point bought currency for candidates that then hit
            # `continue`: on 2026-09-04 three JP names each converted ~1,847 USD
            # and only 7733 ever became an order. Sizing on the ROUNDED share
            # count also stops us converting for the fraction that board-lot
            # rounding just discarded.
            if not ensure_ccy(ib, ccy, shares * px * rate, dry):
                # The cash is not in the account. Skip WITHOUT consuming a slot
                # or writing state - the signal is re-evaluated next run, by
                # which time a working conversion may have filled.
                log(f"  skip {ysym}: {ccy} funding did not complete")
                if ccy == "USD":
                    usd_short = True
                continue
            # The engine's signal kind ("E" or "D3", D3+E1 model) rides in the
            # History row, so a D3 entry can be told from an E one afterwards.
            kind = re.sub(r"[^A-Za-z0-9]", "", str(a.get("signal") or ""))[:8]
            # Market filter, SHADOW: asked only here, after every check above
            # has passed and the order is about to go out exactly as it would
            # without the filter - so it can annotate, never decide. A live
            # filter would skip before the slot and the funding; that is not
            # built (see MARKET_FILTER).
            hit = shadow_filter_hit(a, trend) if mf_mode == "shadow" else None
            n_rows = len(PLACED)
            st = place(ib, c, "BUY", shares, price, dry,
                       reason=f"entry signal{' ' + kind if kind else ''}, score "
                              f"{a.get('score')}", quote=live)
            if hit and (dry or len(PLACED) > n_rows):
                # sent (or, under --dry, would be): log it, tag its History
                # row, record it. A BUY declined at the confirm prompt was not
                # sent, so there is nothing the filter would have stopped.
                shadow_filter_note(state, ysym, hit, PLACED[n_rows:], dry)
            if st != "REJECTED":
                # The IB symbol is taken for the rest of this run (see
                # entered_syms). A refusal leaves it free: nothing was bought.
                entered_syms[c.symbol] = ysym
                # Reserve what this order will spend. CashBalance is not debited
                # until settlement, so without this the next same-currency
                # candidate reads the SAME cash as free and is funded from it
                # too - both fill at the open and the currency goes negative,
                # which IB settles as a margin loan against the HKD balance.
                # This is the stock-side twin of _FX_COMMITTED, and the reason
                # _spendable exists at all.
                # Reserve at the LIMIT, not the signal price: place() sends
                # price * (1 + LIMIT_BUFFER), so reserving the bare price
                # under-counts every order by the buffer - and at the higher of
                # the signal price and IB's quote, which is where place() may
                # have re-based it. Commission is still not modelled anywhere,
                # so this remains a slight under-estimate of the true cost -
                # erring small, but knowingly.
                _FX_COMMITTED[ccy] = (_FX_COMMITTED.get(ccy, 0.0)
                                      + shares * px * (1.0 + LIMIT_BUFFER))
            # NEVER in dry: place() returned before transmitting, so recording
            # an entry price, a high-water mark and a stop here would seed a
            # position the account does not hold - and the trailing- and
            # time-stop machinery would then run against it on the next live
            # run. The slot (`free`) is still consumed either way: a preview
            # that re-used the same slot for every candidate would report 15
            # orders where a live run places one.
            if not dry:
                state.setdefault("map", {})[c.symbol] = ysym
                from datetime import date
                state.setdefault("pos", {})[ysym] = {"entry": price, "hw": price,
                                                     "stop": a.get("stop") or 0,
                                                     "entry_date": date.today().isoformat()}
            free -= 1
        if mf_mode == "shadow" and _FILTER_SKIPPED_RUN:
            log(f"shadow market filter: {len(_FILTER_SKIPPED_RUN)} BUY(s) this run "
                f"would have been skipped - "
                + ", ".join(f"{r['symbol']} ({r['market']})" for r in _FILTER_SKIPPED_RUN)
                + "; all were sent (log only)")
        # ---- cash parking: SHADOW only - one log line, nothing placed ----
        # After the entries, so it sees the USD they claimed. Guarded on its own:
        # a report must never cost the run its save and publish below.
        try:
            park_idle_usd(ib, held, open_syms, per_slot, nl, usd_short)
        except Exception as e:
            log(f"  note: cash-parking (shadow) report skipped ({str(e)[:100]})")
        if dry:
            # --dry is READ-ONLY, all the way out. save_state would persist this
            # run's in-memory bookkeeping (peak NetLiq, ratcheted trailing
            # stops, backfilled entry dates) for orders that were never sent,
            # and publish_state does far more than its name suggests: it
            # rewrites data/bot_state.json and netliq_history.json, sweeps the
            # fills and dividend ledgers, rebuilds the tax report, then commits
            # and pushes - so a preview would land on the live dashboard.
            # --publish-only remains the way to refresh the dashboard and is
            # unaffected by this branch.
            log("--dry: state.json NOT written, nothing published — this run "
                "changed no file and pushed no commit")
        else:
            _write_pocket_file()
            if kill_note_day:
                # With the row it stamps: publish_state below carries PLACED,
                # HALT row included. An aborted run never gets here, so it
                # saves the previous _kill_noted and the next run adds the row.
                state["_kill_noted"] = kill_note_day
            save_state(state)
            publish_state(ib, state, nl)
        log("done.")
    except BaseException as e:
        # BaseException, not Exception: a Ctrl-C at a CONFIRM prompt after an
        # earlier order went out loses the same map entries as a 500 does.
        _save_state_on_abort(state, dry, e)
        raise
    finally:
        # The pocket belongs to this run. A later net_liq in the same process
        # must read the pocket FILE like every other out-of-run caller.
        _POCKET_RUN["active"] = False
        if cache_writes_before is not None:
            _conid_cache_writes(cache_writes_before)
        ib.disconnect()


def publish_only():
    """Connect, read the account, publish state for the dashboard — trade nothing."""
    ib = IB()
    connect_or_heal(ib, CLIENT_ID + 3, 25)
    try:
        publish_state(ib, load_state(), net_liq(ib))
    finally:
        ib.disconnect()


def run_locked():
    """A LIVE trading run, under the run lock (runlock.py) - what main() runs.

    The lock is taken before the signals are read or IB is connected, and held
    until run() returns: exits, entries, save_state and publish_state all inside
    it. So the phone-command poller cannot send a SELL between this run's
    order-book read and its own exit for the same holding (board review
    2026-09-21: both could be sent, both fill at the open, and the account is
    left short a position no exit path ever closes). The poller does not queue
    behind a run: it skips its turn and its commands wait, un-done, for the next
    poll, which then reads a book that already holds this run's exits.

    Nothing inside run() may take the lock again: it is an OS lock on an open
    file, and a second hold() in this same process would fail against the
    first. --dry takes no lock at all (main() calls run(dry=True) directly): it
    sends and writes nothing, and a preview must not hold up the poller or a
    real run. A run that cannot get the lock logs, alerts and exits
    RUN_LOCK_EXIT, having read and sent nothing.

    runlock is imported here, where it is used: a process that only imports
    this module (the publisher, the tests' own harnesses) never needs the lock
    path."""
    import time
    import runlock
    t0 = time.monotonic()
    with runlock.hold("ib_bot", wait_s=RUN_LOCK_WAIT_S) as got:
        waited = time.monotonic() - t0
        if not got:
            log(f"!! could not take the run lock ({runlock.LOCK_FILE}) within "
                f"{waited:.0f}s - another process that sends orders still holds "
                f"it. NOTHING was read or sent this run")
            _alert(_run_lock_alert, waited, runlock.LOCK_FILE)
            raise SystemExit(RUN_LOCK_EXIT)
        if waited >= 5:
            log(f"run lock taken after {waited:.0f}s (another order-sending "
                f"process held it)")
        return run(dry=False)


def main(argv=None):
    """CLI dispatch. A function rather than bare __main__ body so the flag
    handling below is reachable from test_dry_run.py - an untested dispatch is
    how `--publish-only --dry` came to ignore --dry in the first place."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true",
                    help="compute + print; place nothing and write nothing")
    ap.add_argument("--publish-only", action="store_true",
                    help="just refresh the dashboard state (hourly cron)")
    args = ap.parse_args(argv)          # None -> sys.argv, exactly as before
    if args.publish_only:
        # --dry must never write, whatever else is on the command line. This
        # branch used to ignore it outright, so `--publish-only --dry` ran the
        # full publish: bot_state.json, the NetLiq series, the fills and
        # dividend sweeps, the tax report, a commit and a push. There is nothing
        # to preview here - publish_only() places no orders - so honour the flag
        # by doing nothing and saying so.
        if args.dry:
            log("--publish-only with --dry: nothing done. There are no orders "
                "to preview, and --dry may not write or publish. Drop --dry to "
                "refresh the dashboard.")
        else:
            publish_only()
    else:
        if PORT == 4001 and CONFIRM_FIRST is False and not args.dry:
            log("*** LIVE + UNATTENDED mode ***")
        # Here, not inside run(): this also covers what fails BEFORE run()'s
        # own try - the signals fetch, the connect. Exception, not
        # BaseException: a Ctrl-C at a CONFIRM prompt means the operator is at
        # the terminal already, and a run that could not take the run lock
        # (SystemExit) has sent its own alert. Live only - --dry writes
        # nothing, the alert spool included. The ORIGINAL exception is
        # re-raised, so the traceback and the exit code are what they were.
        try:
            if args.dry:
                run(dry=True)             # no lock: see run_locked
            else:
                run_locked()
        except Exception as e:
            if not args.dry:
                _alert(_run_died_alert, e)
            raise


if __name__ == "__main__":
    main()
