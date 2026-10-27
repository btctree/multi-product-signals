"""Bot BUY lots, and what the market filter would have left out of the account.

    import filter_lots as FL
    orders = FL.bot_orders(activity_rows)           # the bot's own BUY orders
    lots, hand = FL.match_lots(orders, ledger)      # each order's fills
    FL.attach_sales(lots, ledger)                   # first in, first out
    rows, stop = FL.filter_series(days, skipped_lots, day_parts["days"])

Shared by build_filter_backfill.py (the one-off record of every past bot BUY
with the filter's verdict, data/filter_backfill.json) and build_sim_lines.py
(the hourly CI build of the dashboard's Sim lines), so the two can never pair a
fill with a different order or price a lot two ways. Standard library only.

DISPLAY ONLY. Nothing here trades and the bot reads none of it: the dashboard's
"Sim: you with market filter (shadow)" line is the account's own NetLiq with
the profit of the lots the filter would have skipped taken out (owner's
decision 2026-10-27).

WHAT A BOT LOT IS. A BUY row the bot wrote in its own History
(bot_state.json "activity"): action BUY, reason "entry signal ...", status
ok / sent / filled - or no status at all, which is how the first day's rows
(20 Jul 2026) were written, before the field existed. Never a REJECTED order,
and never a row marked "manual": those record a trade the owner entered by
hand in IB (URI on 26 Aug, during the gateway outage) or a fill booked by hand
for an order the bot had placed (NTAP's 21 Aug order, filled at Monday's open
while the gateway was down - the bot's own order is the lot). MSFT, bought by
hand on 23 Oct and handed to the bot through execution/adopt.json, has no bot
BUY row at all. Hand trades are never skipped: the filter gates the bot's own
entries only.

An order becomes a lot through its fills in data/fills_ledger.jsonl: BOT
stock fills of the same IB symbol and currency, at or after the order (a fill
stamped 00:00 carries its date only, so only its date is compared - Tokyo
fills and the first week's estimates are booked that way) and at most
MATCH_DAYS calendar days later. Each fill goes to the NEWEST such order that
still has quantity left: an order the bot placed again later (MS and DAL on
20 Jul, placed at 09:00 and again at 18:35) supersedes the earlier one. An
order with no fill is not a lot - it bought nothing. A fill no order claims is
a hand trade, or a holding from before the bot (the 2024 seed rows).

THE ORDER'S OWN REF. Since 17 Sep 2026 every bot order carries the bot's
client order id, and IB echoes it on the order's fills as the ledger's
"order_ref": "mps-<conid>-B-<YYYYMMDDHHMMSS>" for a BUY
(execution/ib_orders.make_coid), stamped the second the order goes out -
7272's fill of 26 Oct carries mps-14017331-B-20261025233535, its History row
says 2026-10-25 23:35 UTC. A fill with a ref goes ONLY to the order it names:
that symbol's order whose History minute is the stamp's or at most
REF_LAG_MIN later (the row is written once IB has answered - a 3-second wait,
longer over a price retry - so a stamp at 23:35:58 has its row at 23:36),
and the fill may be timed from the stamp on. A ref that is not a bot BUY's
names no order. A fill with no ref - every fill before 17 Sep, and the
owner's hand trades since (MSFT, 23 Oct) - goes as above, but never to an
order known only from the shadow record (qty None: its History row has
rolled off). Such an order has no size to stop it, and only its ref ties a
fill to it, however long ago its History row went: a BUY IB refused has no
fill carrying its ref, so it never claims the owner's own buy of that symbol.
Rows written before the ledger had the key, and fills read through the socket
backend (which has no order_ref), count as having none.

SALES, first in first out per symbol over every BOT fill, the hand ones
included, so a sale closes the oldest shares first whoever bought them.

THE FIGURE. c(d), in HKD, for day d of the account's history: for each
skipped lot, what it was worth that day less what it cost -
    held part:  (shares still held / the position's shares) x the position's
                HKD value in data/day_parts.json days[d].pos
    sold part:  the sale proceeds (price x shares - commission) in HKD
    cost:       price x shares + commission, in HKD
each converted at the day_parts rate of the day the fill or sale happened.
Before the fill c is 0; after the sale the realised profit stays as it is. The
page subtracts c from the account's NetLiq: the money is left as idle cash,
which IBKR HK pays this account nothing on, at its HKD value on the fill day -
so a lot's currency move counts as part of what it made or lost. Dividends a
skipped lot paid are not taken out (none so far).

NEVER GUESSED. A day on which a skipped lot is held but day_parts has no row,
no position, fewer shares than the lot, or no rate for a fill or sale on or
before it has no figure: it is left out of the series rather than given an
invented price. The page steps the line across such a day on levels (NetLiq
less c on the days either side, the money in or out between them added), so
one missing breakdown does not end the line for good; only when the days it
cannot price run to the end does the series stop early, and `stop` says why.
The newest day_parts row counts only once it is the publisher's day-end
snapshot (taken at or after DAY_END_UTC on its own day) - an intraday row
would set a morning price against an evening NetLiq. That is the routine
reason the newest day has no figure yet (INTRADAY), and build_sim_lines does
not warn about it.
"""
import datetime as dt
import json
import re

TIME_FMT = "%Y-%m-%d %H:%M UTC"     # ib_bot's History "time"
MATCH_DAYS = 7                      # an order's fill: within a week (a long weekend + a holiday)
# A bot BUY's client order id as IB echoes it on a fill (ib_orders.make_coid:
# "-r<n>" on a re-sent price), and how many minutes its History row may be
# written after the stamp (see THE ORDER'S OWN REF).
_BOT_BUY_REF = re.compile(r"mps-[A-Za-z0-9]+-B-(\d{12})\d{2}(?:-r\d+)?")
REF_LAG_MIN = 5
BOT_STATUSES = ("ok", "sent", "filled")
DAY_END_UTC = 23                    # the publisher's day-end snapshot is 23:25 UTC
# Why the newest day has no figure while its breakdown is still an intraday
# one - part of lot_value's reason text, so build_sim_lines can tell this
# routine case from a real hole.
INTRADAY = "that day's breakdown is not the day-end one yet"
_EPS = 1e-6

# Yahoo suffix -> currency, as execution/contracts.py maps them (copied, not
# imported: nothing under execution/ is imported by the engine).
_SUFFIX_CCY = {".HK": "HKD", ".T": "JPY", ".AS": "EUR", ".PA": "EUR", ".DE": "EUR",
               ".MC": "EUR", ".MI": "EUR", ".BR": "EUR", ".L": "GBP", ".SW": "CHF",
               ".CO": "DKK", ".HE": "EUR", ".ST": "SEK", ".OL": "NOK", ".VI": "EUR",
               ".LS": "EUR"}


def parse_time(s) -> dt.datetime:
    """A History "time" ("2026-10-25 23:35 UTC") as an aware UTC datetime."""
    return dt.datetime.strptime(str(s), TIME_FMT).replace(tzinfo=dt.timezone.utc)


def ib_symbol(ysym) -> str:
    """The IB symbol a Yahoo symbol trades under, as contracts.to_ib maps it:
    0700.HK -> 700, 7272.T -> 7272, DBK.DE -> DBK, MSFT -> MSFT."""
    s = str(ysym or "").strip().upper()
    if s.endswith(".HK"):
        code = s[:-3]
        return str(int(code)) if code.isdigit() else code
    for suffix in _SUFFIX_CCY:
        if s.endswith(suffix):
            return s[:-len(suffix)]
    return s


def ccy_of(ysym) -> str:
    s = str(ysym or "").strip().upper()
    for suffix, ccy in _SUFFIX_CCY.items():
        if s.endswith(suffix):
            return ccy
    return "USD"


def is_bot_buy(row) -> bool:
    """One of the bot's own entry orders (see the module docstring)."""
    if not isinstance(row, dict) or row.get("action") != "BUY" or row.get("manual"):
        return False
    if not str(row.get("reason") or "").startswith("entry signal"):
        return False
    st = row.get("status")
    if not (st in (None, "") or st in BOT_STATUSES):
        return False
    try:
        parse_time(row.get("time"))
        return float(row.get("qty")) > 0 and bool(str(row.get("symbol") or "").strip())
    except (TypeError, ValueError):
        return False


def order_key(time, symbol) -> str:
    """One order, whichever record it came from: its History time and IB symbol."""
    return f"{time}|{str(symbol).strip().upper()}"


def bot_orders(rows) -> list:
    """The bot's BUY orders among History rows, once each, oldest first:
    [{time, symbol, ccy, qty, limit, reason, status}]. A row seen in several
    snapshots of the file is one order."""
    out = {}
    for r in rows or []:
        if not is_bot_buy(r):
            continue
        o = {"time": str(r["time"]), "symbol": str(r["symbol"]).strip().upper(),
             "ccy": str(r.get("ccy") or "").upper(), "qty": float(r["qty"]),
             "limit": r.get("limit"), "reason": str(r.get("reason") or "")[:80],
             "status": r.get("status")}
        out.setdefault(order_key(o["time"], o["symbol"]), o)
    return sorted(out.values(), key=lambda o: (parse_time(o["time"]), o["symbol"]))


def read_ledger(text) -> list:
    """data/fills_ledger.jsonl's stock fills, oldest first. Keyed by execId -
    a later line with the same id is a correction and replaces the earlier
    one, as fills_capture writes it. Lines that cannot be read, cash (FX)
    rows, and rows without a positive quantity and price are left out. Each
    keeps its "order_ref" (None when the row has none)."""
    by_id, order = {}, []
    for i, line in enumerate(str(text or "").splitlines()):
        try:
            r = json.loads(line)
            if not isinstance(r, dict) or r.get("sec_type", "STK") != "STK":
                continue
            side = r.get("side")
            q, px = float(r.get("qty")), float(r.get("price"))
            day = dt.date.fromisoformat(str(r.get("date"))[:10]).isoformat()
            if side not in ("BOT", "SLD") or not (q > 0 and px > 0):
                continue
            ts = str(r.get("ts") or "")
            f = {"id": str(r.get("execId") or f"line{i}"), "date": day, "ts": ts,
                 "symbol": str(r.get("symbol") or "").strip().upper(), "side": side,
                 "qty": q, "price": px, "ccy": str(r.get("ccy") or "").upper(),
                 "com": float(r.get("commission") or 0.0),
                 "com_ccy": str(r.get("commission_ccy") or r.get("ccy") or "").upper(),
                 "source": str(r.get("source") or ""),
                 "order_ref": str(r.get("order_ref") or "").strip() or None}
        except (TypeError, ValueError, AttributeError):
            continue
        if f["id"] not in by_id:
            order.append(f["id"])
        by_id[f["id"]] = f
    rows = [by_id[k] for k in order]
    return sorted(rows, key=lambda f: (_fill_sort(f), 0 if f["side"] == "BOT" else 1))


def _fill_clock(f):
    """The fill's UTC time when the ledger has one, None when it holds the date
    only (stamped 00:00)."""
    ts = f.get("ts") or ""
    if len(ts) >= 16 and ts[11:16] != "00:00":
        try:
            return dt.datetime.strptime(ts[:16], "%Y-%m-%d %H:%M").replace(
                tzinfo=dt.timezone.utc)
        except ValueError:
            return None
    return None


def _fill_sort(f):
    return f["date"] + " " + (f["ts"][11:16] if _fill_clock(f) else "00:00")


def ref_minute(ref):
    """The UTC minute a bot BUY's client order id was stamped, from a fill's
    order_ref ("mps-14017331-B-20261025233535" -> 2026-10-25 23:35); None
    when the ref is not a bot BUY's."""
    m = _BOT_BUY_REF.fullmatch(str(ref or "").strip())
    try:
        return dt.datetime.strptime(m.group(1), "%Y%m%d%H%M").replace(
            tzinfo=dt.timezone.utc) if m else None
    except ValueError:
        return None


def _fits(order, f, sent=None):
    """Could this fill be this order's? Same symbol and currency, at or after
    the order, within MATCH_DAYS. `sent`: for a fill that carries the order's
    ref, the ref's minute, when the order went out - its History row can be
    written a minute later than a fill that came at once."""
    if f["symbol"] != order["symbol"] or (order["ccy"] and f["ccy"] != order["ccy"]):
        return False
    t = sent or parse_time(order["time"])
    fd = dt.date.fromisoformat(f["date"])
    if fd < t.date() or (fd - t.date()).days > MATCH_DAYS:
        return False
    clock = _fill_clock(f)
    return clock is None or clock >= t


def _named(order, sent) -> bool:
    """Is this the order a fill's ref names? Its History minute is the ref's
    or at most REF_LAG_MIN later."""
    lag = (parse_time(order["time"]) - sent).total_seconds() / 60
    return 0 <= lag <= REF_LAG_MIN


def match_lots(orders, ledger):
    """(lots, unclaimed). lots: one per order that bought anything, oldest
    order first - {"key", "order", "fills": [[id, date, qty, price, com,
    com_ccy, ccy]]}, a fill split between two orders carrying its share of
    the commission. unclaimed: the BOT fills (or parts) no bot order took -
    [id, date, symbol, qty]: hand trades and the holdings from before the bot.
    A fill with an order_ref goes only to the order the ref names (none when
    it is not a bot BUY's ref); one without goes to the newest order that
    fits. An order with qty None (known only from the shadow record, its
    History row gone) takes only the fills its ref names, never one with no
    ref (THE ORDER'S OWN REF in the module docstring)."""
    left = [o["qty"] if o.get("qty") is not None else float("inf") for o in orders]
    got = [[] for _ in orders]
    unclaimed = []
    for f in ledger:
        if f["side"] != "BOT":
            continue
        ref = f.get("order_ref")
        sent = ref_minute(ref) if ref else None
        rest = f["qty"]
        while rest > _EPS:
            if ref:
                fit = [i for i, o in enumerate(orders) if sent is not None and left[i] > _EPS
                       and _named(o, sent) and _fits(o, f, sent)]
            else:
                fit = [i for i, o in enumerate(orders) if left[i] > _EPS
                       and o.get("qty") is not None and _fits(o, f)]
            if not fit:
                break
            if ref:                       # the order written soonest after the stamp
                i = min(fit, key=lambda k: parse_time(orders[k]["time"]))
            else:                         # the newest order
                i = max(fit, key=lambda k: parse_time(orders[k]["time"]))
            q = min(rest, left[i])
            got[i].append([f["id"], f["date"], q, f["price"], f["com"] * q / f["qty"],
                           f["com_ccy"], f["ccy"]])
            left[i] -= q
            rest -= q
        if rest > _EPS:
            unclaimed.append([f["id"], f["date"], f["symbol"], round(rest, 6)])
    lots = [{"key": order_key(o["time"], o["symbol"]), "order": o, "fills": got[i]}
            for i, o in enumerate(orders) if got[i]]
    return lots, unclaimed


def attach_sales(lots, ledger) -> None:
    """Each lot's "sales" - [[id, date, qty, price, com, com_ccy, ccy]] - first
    in, first out per symbol over EVERY BOT fill in the ledger, hand ones
    included. A sale with no shares left before it (a short, or a holding the
    ledger never saw bought) closes nothing here."""
    owner = {}                                   # fill id -> [(lot, its share)]
    for lot in lots:
        lot["sales"] = []
        for part in lot["fills"]:
            owner.setdefault(part[0], []).append((lot, part[2]))
    queues = {}                                  # (symbol, ccy) -> [[qty left, lot or None]]
    for f in ledger:
        q = queues.setdefault((f["symbol"], f["ccy"]), [])
        if f["side"] == "BOT":
            parts = owner.get(f["id"], [])
            mine = sum(p[1] for p in parts)
            for lot, pq in parts:
                q.append([pq, lot])
            if f["qty"] - mine > _EPS:
                q.append([f["qty"] - mine, None])
            continue
        rest = f["qty"]
        while rest > _EPS and q:
            head = q[0]
            take = min(rest, head[0])
            if head[1] is not None:
                head[1]["sales"].append([f["id"], f["date"], take, f["price"],
                                         f["com"] * take / f["qty"], f["com_ccy"], f["ccy"]])
            head[0] -= take
            rest -= take
            if head[0] <= _EPS:
                q.pop(0)


def _fx(parts, day, ccy):
    """HKD per unit of ccy on that day, from day_parts; None when not there."""
    if ccy == "HKD":
        return 1.0
    row = parts.get(day)
    try:
        v = float(row["fx"][ccy])
        return v if v > 0 else None
    except (TypeError, KeyError, ValueError):
        return None


def _taken(row):
    ts = str(row.get("ts") or "") if isinstance(row, dict) else ""
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M UTC"):
        try:
            return dt.datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return None


def _final(parts, day, newest):
    """Is day_parts' row for `day` that day's record? Any older day's is (no
    later row replaced it); the newest only once it is the day-end snapshot."""
    if day < newest:
        return True
    t = _taken(parts.get(day))
    return t is not None and t.date().isoformat() == day and t.hour >= DAY_END_UTC


def lot_value(lot, day, parts, newest):
    """What one lot was worth on `day` less what it cost, in HKD - or
    (None, why) when that cannot be told without guessing."""
    cash = 0.0
    held = 0.0
    sym = lot["order"]["symbol"]
    for _id, fday, q, px, com, com_ccy, ccy in lot["fills"]:
        if fday > day:
            continue
        r, rc = _fx(parts, fday, ccy), _fx(parts, fday, com_ccy)
        if r is None or rc is None:
            return None, f"no {ccy} rate in day_parts for {sym}'s fill on {fday}"
        cash -= q * px * r + com * rc
        held += q
    for _id, sday, q, px, com, com_ccy, ccy in lot.get("sales", []):
        if sday > day:
            continue
        r, rc = _fx(parts, sday, ccy), _fx(parts, sday, com_ccy)
        if r is None or rc is None:
            return None, f"no {ccy} rate in day_parts for {sym}'s sale on {sday}"
        cash += q * px * r - com * rc
        held -= q
    if held <= _EPS:
        return cash, None
    row = parts.get(day)
    if not isinstance(row, dict):
        return None, f"{sym} held on {day} and day_parts has no row for that day"
    if not _final(parts, day, newest):
        return None, f"{sym} held on {day} and {INTRADAY}"
    pos = (row.get("pos") or {})
    p = pos.get((row.get("ib") or {}).get(sym, sym))
    try:
        pq, pv = float(p[0]), float(p[1])
    except (TypeError, IndexError, ValueError):
        return None, f"{sym} held on {day} but not in that day's positions"
    if not (pq >= held - _EPS and pq > 0 and pv == pv):
        return None, f"{sym} held on {day}: the position has {pq:g} shares, the lot {held:g}"
    return cash + held / pq * pv, None


def filter_series(days, lots, parts, holes=None):
    """([[day, c_hkd], ...], stop) over the account's days, oldest first: c is
    the skipped lots' summed value less cost (lot_value). A day that cannot be
    told without guessing is left out - never filled in - and the series goes
    on; `holes`, when a list is passed, gets [day, why] for each such day.
    `stop` is None unless those days run to the end: then it is why the first
    of them has no figure, and the series stops early there. No lots: every
    day is 0, and the line is the account."""
    parts = parts if isinstance(parts, dict) else {}
    newest = max(parts) if parts else ""
    out, missed = [], []
    for d in sorted(set(days)):
        c, why = 0.0, None
        for lot in lots:
            v, why = lot_value(lot, d, parts, newest)
            if v is None:
                break
            c += v
        if why is not None:
            missed.append([d, why])
            continue
        out.append([d, round(c, 2)])
    if holes is not None:
        holes.extend(missed)
    tail = [m for m in missed if not out or m[0] > out[-1][0]]
    return out, (tail[0][1] if tail else None)
