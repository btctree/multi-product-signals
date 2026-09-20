#!/usr/bin/env python3
"""What each day's P&L was actually MADE of, for the dashboard's calendar.

data/netliq_history.json says what the account was worth each day. It cannot say
why: a -3,147 HKD Tuesday could be one position collapsing or fourteen drifting,
and the owner cannot tell which. This module publishes the missing half - a
snapshot per day of every position's value, the cash in each currency, and the
exchange rate each was valued at - so tapping a day in the P&L Calendar can
break the number down instead of just restating it.

WHO WRITES IT. Only the hourly publisher (publish_web.py). ib_bot deliberately
does NOT: its shim drops IB's mkt_value (broker.Position carries qty and
avg_cost only), so the bot would need extra API calls inside the live trading
run to produce the same row. A breakdown is a nice-to-have; the run is not. The
cost of that choice is that the last row of a day comes from the last HOURLY
publish rather than the bot's 23:20 one, which is minutes apart and after every
venue the account trades has closed. The dashboard reconciles against
netliq_history anyway and shows the leftover as "unexplained" rather than
smearing it into the biggest mover.

WHY THE RATES. mkt_value comes back in the position's own currency and the cash
balances are native, so without the rate a day's move in a EUR holding cannot be
expressed in HKD at all. IB's ledger already carries "exchangerate" per
currency in the response the publisher fetches anyway - no extra call, no
guessing, and no rate that "looks right" but is a day old.

SHAPE (data/day_parts.json):

  {"days": {"2026-09-19": {
      "ts":   "2026-09-19T23:05:11Z",   when this row was taken
      "nl":   216201,                   base ccy, NET of "exc" - same as netliq_history
      "exc":  0,                        earmarked cash netted out of nl
      "fx":   {"USD": 7.7924, ...},     units of base ccy per 1 unit of that ccy
      "pos":  {"NVDA": [20, 34120.5, "USD"]},  qty, value in BASE ccy, its own ccy
      "cash": {"USD": 4297, ...},       native units, not converted
      "src":  "live"                    or "reconstructed" for backfilled days
  }}}

A row is REPLACED by any later row for the same date, exactly like
netliq_history's series: the last write of the day is the day's record. An
unreadable file is left in place for repair and never silently rewritten - the
same rule that protects the NetLiq history, for the same reason.
"""
import datetime
import json
import os

# Keep the file bounded. 400 days is comfortably more than a year of tapping
# back through the calendar, and at ~600 bytes a day that is well under a MB.
KEEP_DAYS = 400


def _today():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def _stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rates_from_ledger(led, base_ccy="HKD"):
    """{ccy: units of base per 1 unit of ccy} out of IB's ledger response.

    The BASE row is the whole account, not a currency - skipping it is the same
    trap ib_web.netliq_and_cash documents. The base currency itself is 1 by
    definition and IB does not always say so.
    """
    out = {}
    for ccy, v in (led or {}).items():
        if not isinstance(v, dict):
            continue
        if str(ccy).upper() == "BASE":
            continue
        try:
            r = float(v.get("exchangerate"))
        except (TypeError, ValueError):
            continue
        if r > 0:
            out[str(ccy)] = round(r, 8)
    out[base_ccy] = 1.0
    return out


def build_row(positions, cash, rates, netliq, exc=0, base_ccy="HKD", src="live"):
    """One day's row from a publisher's snapshot.

    positions: ib_web.positions() rows - qty, ccy, mkt_value in THAT ccy.
    A position whose currency has no rate is kept with a null value rather than
    dropped: the dashboard then shows it as unpriced instead of quietly leaving
    a holding out of a breakdown that claims to be complete.
    """
    pos, ib = {}, {}
    for p in positions or []:
        if str(p.get("sec_type") or "").upper() == "CASH":
            continue                       # FX balances are cash, not holdings
        # Key by the SAME symbol the rest of the site uses (the Yahoo ticker),
        # and keep IB's own ticker beside it when they differ. fills_ledger
        # records IB's - "DBK" for a position published as "DBK.DE" - and a
        # reader that cannot join the two books a 46-share purchase as a
        # 13,543 HKD gain, which is exactly what the first build did.
        sym = str(p.get("symbol") or p.get("ib_symbol") or "").strip()
        if not sym:
            continue
        ibs = str(p.get("ib_symbol") or "").strip()
        if ibs and ibs != sym:
            ib[ibs] = sym
        try:
            qty = float(p.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if not qty:
            continue
        r = rates.get(str(p.get("ccy") or base_ccy))
        try:
            mv = float(p.get("mkt_value"))
        except (TypeError, ValueError):
            mv = None
        val = None if (mv is None or r is None) else round(mv * r, 2)
        # [qty, value in base, CURRENCY]. The currency is what lets a reader
        # re-express one day's values at another day's rates. Without it the
        # dashboard cannot separate "the share moved" from "the rate moved",
        # and it could not bridge the day the rates switched from fitted to
        # IB's real ones at all - that day had no breakdown whatsoever.
        pos[sym] = [qty, val, str(p.get("ccy") or base_ccy)]
    return {"ts": _stamp(), "nl": round(float(netliq)), "exc": round(float(exc or 0)),
            "fx": rates, "pos": pos, "ib": ib,
            "cash": {k: round(float(v)) for k, v in (cash or {}).items()
                     if abs(float(v)) >= 1},
            "src": src}


def upsert(path, row, day=None, keep_days=KEEP_DAYS):
    """Write today's row, atomically, keeping the file bounded.

    Returns the number of days in the file after the write. An unreadable file
    RAISES rather than being replaced - a corrupt file is a repair job, not a
    reason to throw away every day already recorded.
    """
    day = day or _today()
    doc = {"days": {}}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.loads(fh.read())          # raises on a damaged file
    days = doc.get("days") or {}
    days[day] = row
    if keep_days and len(days) > keep_days:
        for d in sorted(days)[:len(days) - keep_days]:
            days.pop(d, None)
    doc["days"] = days
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(doc, separators=(",", ":"), sort_keys=True))
    os.replace(tmp, path)                        # atomic: no torn reads
    return len(days)
