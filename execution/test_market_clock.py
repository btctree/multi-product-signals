#!/usr/bin/env python3
"""Golden tests: each market is decided only on a finished trading day.

Run from this directory:  python test_market_clock.py

Every rule in the bot is close-evaluated, but Yahoo fills today's daily bar with
the live price while a market trades, and the product cards carry that bar. The
weekday 09:00 UTC run read cards built at 07:35Z on 2026-09-15 and 09-16: EU was
~35 minutes into its session and HK before its closing auction, so a regime
break or trailing stop could be decided - and an EU market sell filled - on an
opening dip the close then undid, with the hw/stop ratchet moving on intraday
prints as well.

market_decidable(ysym, now_utc) is the operator-approved rule: a market is NOT
decidable only on a local weekday with local time in [open, close + 90 min).
What is locked down:
  * the table over real UTC instants - summer, winter, weekend, the DST shift
    of the US close, HK's 16:10 close, crypto and unknown suffixes;
  * every row of MARKET_SESSIONS against the operator's approved numbers;
  * run() at 09:00 UTC: a held EU name below its SMA200 sends NO sell and its
    stops do NOT move, a held JP name is evaluated as before, EU/HK entries are
    skipped, and a deferred symbol is not "a rule that did not fire" - no
    lapsed-exit alert, memo untouched. The 23:35 UTC control does all of it.

EXTENDED 2026-09-17 (review: "The settle check uses the bot's own clock, not
the time the card was built"). The clock now lives in market_clock.py, shared
with the 3.9 digest, and a decision also needs a build that STARTED at or after
the close it rests on. Locked down on top of the above:
  * market_clock parses as Python 3.9 and ib_bot re-exports the very same names;
  * last_settled_close across weekends and both DST shifts, checked against an
    independent enumeration of every settle instant;
  * a 06:35Z build read at 09:00Z for a .T name is deferred - no SELL, no
    ratchet, no BUY - with ONE stale-signals alert per UTC day, none under --dry;
  * a fresh build is decided; an exit reads its card's generated_at before
    data.json's; a missing or unreadable field keeps the clock-only rule and
    says so once per run.
Nothing here touches /root: every path is repointed before ib_bot is imported.
"""
import ast
import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ.setdefault("IB_BACKEND", "web")
_TMP = Path(tempfile.mkdtemp(prefix="mps-clock-"))
os.environ["MPS_EARMARK_DIR"] = str(_TMP / "earmark")
os.environ["MPS_ORDERS_LEDGER"] = str(_TMP / "orders_ledger.jsonl")
os.environ["MPS_ALERT_DIR"] = str(_TMP / "outbox")
os.environ["MPS_EXIT_ATTEMPTS"] = str(_TMP / "exit_attempts.json")
os.environ["MPS_FX_LAST_GOOD"] = str(_TMP / "fx_last_good.json")
os.environ["MPS_CONID_CACHE"] = str(_TMP / "conid_cache.json")
os.environ.pop("EXCLUDED_CASH", None)
(_TMP / "earmark").mkdir()

import alerts                                      # noqa: E402
import ib_bot                                      # noqa: E402
import market_clock                                # noqa: E402
from contracts import currency_of                  # noqa: E402

UTC = timezone.utc
decidable = ib_bot.market_decidable


def at(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


US, DE, PA, JP, HK, UK, CRYPTO = "AAPL", "DBK.DE", "MC.PA", "7733.T", "0700.HK", "BP.L", "BTC-USD"


def check(rows):
    for sym, when, want in rows:
        got, why = decidable(sym, when)
        assert got is want, (sym, when.isoformat(), want, why)
        assert isinstance(why, str) and why, (sym, when, why)


def t1_the_two_daily_runs_summer_and_winter():
    for day in (at(2026, 9, 16, 0, 0), at(2026, 1, 14, 0, 0)):      # Wednesdays
        evening = day.replace(hour=23, minute=35)
        morning = day.replace(hour=9, minute=0)
        check([(s, evening, True) for s in (US, DE, PA, JP, HK, UK, CRYPTO)])
        check([(US, morning, True), (JP, morning, True), (CRYPTO, morning, True),
               (DE, morning, False), (PA, morning, False), (HK, morning, False),
               (UK, morning, False)])
    ok, why = decidable(DE, at(2026, 9, 16, 9, 0))
    assert "Europe/Berlin" in why and "17:30" in why and "19:00" in why, why
    print("t1 23:35 UTC decides US/EU/JP/HK; 09:00 UTC decides US+JP, defers EU+HK OK")


def t2_weekends_always_decide():
    sat = at(2026, 9, 19, 0, 0)
    for h, m in ((0, 0), (1, 30), (9, 0), (14, 0), (20, 0), (23, 35)):
        check([(s, sat.replace(hour=h, minute=m), True)
               for s in (US, DE, PA, JP, HK, UK, CRYPTO)])
    # local weekday decides, not the UTC one: Friday 23:35 UTC is Saturday in Asia,
    # Sunday 23:35 UTC is Monday morning there (before the open), and Monday
    # 01:30 UTC is HK's Monday open
    check([(JP, at(2026, 9, 18, 23, 35), True), (HK, at(2026, 9, 18, 23, 35), True),
           (JP, at(2026, 9, 20, 23, 35), True), (HK, at(2026, 9, 20, 23, 35), True),
           (HK, at(2026, 9, 21, 1, 30), False), (JP, at(2026, 9, 21, 0, 0), False),
           (US, at(2026, 9, 18, 20, 30), False), (US, at(2026, 9, 18, 21, 30), True)])
    print("t2 weekends always decide; the LOCAL weekday counts OK")


def t3_us_close_plus_90_follows_dst():
    # Winter: 16:00 EST = 21:00 UTC, so the bar is final from 22:30 UTC.
    check([(US, at(2026, 1, 14, 14, 29, 59), True),     # 09:29:59 EST, before the open
           (US, at(2026, 1, 14, 14, 30), False),        # the open
           (US, at(2026, 1, 14, 21, 0), False),         # the close itself
           (US, at(2026, 1, 14, 22, 29, 59), False),
           (US, at(2026, 1, 14, 22, 30), True)])
    # Summer: 16:00 EDT = 20:00 UTC, final from 21:30 UTC.
    check([(US, at(2026, 9, 16, 13, 29), True), (US, at(2026, 9, 16, 13, 30), False),
           (US, at(2026, 9, 16, 21, 29, 59), False), (US, at(2026, 9, 16, 21, 30), True)])
    print("t3 US close + 90 min is 22:30 UTC in winter, 21:30 in summer OK")


def t4_hk_closing_auction_and_crypto():
    for day in ((2026, 9, 16), (2026, 1, 14)):              # HK keeps no DST
        check([(HK, at(*day, 9, 39), False), (HK, at(*day, 9, 41), True),
               (HK, at(*day, 9, 40), True),                  # 16:10 + 90 = 17:40 HKT
               (HK, at(*day, 1, 29), True), (HK, at(*day, 1, 30), False)])
    t = at(2026, 9, 16, 0, 0)
    for minute in range(0, 7 * 24 * 60, 37):                 # a week, every 37 minutes
        assert decidable(CRYPTO, t + timedelta(minutes=minute))[0] is True
        assert decidable("ETH-USD", t + timedelta(minutes=minute))[0] is True
    # unknown suffixes (and a naive clock, read as UTC) keep today's behaviour
    check([("600519.SS", at(2026, 9, 16, 3, 0), True), ("RY.TO", at(2026, 9, 16, 15, 0), True)])
    assert decidable(DE, datetime(2026, 9, 16, 9, 0))[0] is False
    print("t4 HK 09:39 UTC defers, 09:41 decides; crypto and unknowns always decide OK")


# The operator-approved table (2026-09-17), copied here on purpose: a later
# edit to MARKET_SESSIONS has to be made in both places to pass.
APPROVED = {
    "": ("America/New_York", "09:30", "16:00"), ".HK": ("Asia/Hong_Kong", "09:30", "16:10"),
    ".T": ("Asia/Tokyo", "09:00", "15:30"), ".DE": ("Europe/Berlin", "09:00", "17:30"),
    ".PA": ("Europe/Paris", "09:00", "17:30"), ".AS": ("Europe/Paris", "09:00", "17:30"),
    ".BR": ("Europe/Paris", "09:00", "17:30"), ".LS": ("Europe/Lisbon", "08:00", "16:30"),
    ".MC": ("Europe/Madrid", "09:00", "17:30"), ".MI": ("Europe/Rome", "09:00", "17:30"),
    ".SW": ("Europe/Zurich", "09:00", "17:30"), ".CO": ("Europe/Copenhagen", "09:00", "17:00"),
    ".ST": ("Europe/Stockholm", "09:00", "17:30"), ".OL": ("Europe/Oslo", "09:00", "16:20"),
    ".HE": ("Europe/Helsinki", "10:00", "18:30"), ".VI": ("Europe/Vienna", "09:00", "17:30"),
    ".L": ("Europe/London", "08:00", "16:30"),
}


def t5_every_market_row_matches_the_approved_table():
    assert ib_bot.SESSION_SETTLE_MIN == 90
    assert sorted(ib_bot.MARKET_SESSIONS) == sorted(APPROVED), sorted(ib_bot.MARKET_SESSIONS)
    for suffix, (zone, opens, closes) in APPROVED.items():
        sym = "X" + suffix
        for day in (date(2026, 9, 16), date(2026, 1, 14)):  # summer and winter
            tz = ZoneInfo(zone)
            oh, om = map(int, opens.split(":"))
            ch, cm = map(int, closes.split(":"))
            o = datetime(day.year, day.month, day.day, oh, om, tzinfo=tz).astimezone(UTC)
            c = datetime(day.year, day.month, day.day, ch, cm, tzinfo=tz).astimezone(UTC)
            final = c + timedelta(minutes=90)
            check([(sym, o - timedelta(seconds=1), True), (sym, o, False),
                   (sym, c, False), (sym, final - timedelta(seconds=1), False),
                   (sym, final, True)])
            sat = datetime(2026, 9, 19, oh, om, tzinfo=tz).astimezone(UTC)
            check([(sym, sat, True), (sym, sat + timedelta(hours=3), True)])
    print("t5 all 17 session rows match the operator's table, both seasons OK")


# ------------------------------------------------------------------ run() --
class C:                                            # a contract
    def __init__(self, symbol, currency="USD", secType="STK"):
        self.symbol, self.currency, self.secType = symbol, currency, secType
        self.conId, self.exchange = abs(hash(symbol)) % 10 ** 6, "SMART"


class Pos:
    def __init__(self, contract, qty):
        self.contract, self.position, self.avgCost = contract, qty, 1.0


class Status:
    def __init__(self, status):
        self.status = status


class Trade:
    def __init__(self):
        self.orderStatus, self.log = Status("PreSubmitted"), []


class FakeIB:
    def __init__(self):
        self.placed = []

    def reqAllOpenOrders(self):
        pass

    def openTrades(self):
        return []

    def sleep(self, *a):
        pass

    def qualifyContracts(self, *c):
        return list(c)

    def placeOrder(self, contract, order):
        self.placed.append((order.action, order.totalQuantity, contract.symbol))
        return Trade()

    def disconnect(self):
        pass


TODAY = date.today().isoformat()
HELD = {"DBK": ("DBK.DE", 40), "7733": ("7733.T", 100)}
SEED_POS = {"DBK.DE": {"entry": 28, "hw": 29, "stop": 25, "entry_date": TODAY},
            "7733.T": {"entry": 2800, "hw": 2900, "stop": 2700, "entry_date": TODAY}}
BELOW = {"DBK.DE": {"price": 30, "sma200": 32, "atr": 1},        # regime break
         "7733.T": {"price": 3000, "sma200": 3100, "atr": 50}}   # regime break
CALM_EU = {"DBK.DE": {"price": 35, "sma200": 32, "atr": 1},      # no rule fires
           "7733.T": {"price": 3000, "sma200": 3100, "atr": 50}}
ACTIONS = [{"symbol": "SAP.DE", "action": "BUY", "price": 200, "score": 9, "stop": 180},
           {"symbol": "6758.T", "action": "BUY", "price": 3000, "score": 5, "stop": 2700},
           {"symbol": "0700.HK", "action": "BUY", "price": 400, "score": 4, "stop": 360}]
HKD_PER = {"HKD": 1.0, "USD": 7.8, "EUR": 9.047, "JPY": 0.053, "GBP": 10.6}
REJECTED_DBK = {"DBK.DE": {"time": "2026-09-15 23:35 UTC", "status": "REJECTED",
                           "qty": 40, "reason": "trailing stop 33.00"}}


def bot_run(now, cards, memo=None, built=None, card_built=None, outbox=None, dry=False):
    """built: data.json's generated_at (None = no such field); card_built:
    {ysym: generated_at} for the product files; outbox: a spool kept across runs."""
    d = Path(tempfile.mkdtemp(prefix="run-", dir=str(_TMP)))
    state_path = d / "state.json"
    state_path.write_text(json.dumps({"map": {k: v[0] for k, v in HELD.items()},
                                      "pos": SEED_POS, "_peak_netliq": 300000}),
                          encoding="utf-8")
    exit_memo = d / "exit_attempts.json"
    if memo is not None:
        exit_memo.write_text(json.dumps(memo), encoding="utf-8")
    alerts.DIR = Path(outbox) if outbox else d / "outbox"
    signals = {"generated": "2026-09-16", "actions": ACTIONS}
    if built is not None:
        signals["generated_at"] = built

    def get_json(url):
        if url == ib_bot.SIGNALS_URL:
            return signals
        for ysym, card in cards.items():
            if url.endswith("/" + ib_bot.safe_name(ysym) + ".json"):
                doc = {"card": card}
                if card_built and ysym in card_built:
                    doc["generated_at"] = card_built[ysym]
                return doc
        raise AssertionError("unexpected fetch " + url)

    ib, lines, published = FakeIB(), [], []
    stubs = dict(
        STATE=state_path, IB=lambda: ib, connect_or_heal=lambda *a, **k: None,
        get_json=get_json, _now_utc=lambda: now,
        publish_state=lambda ib_, st, nl: published.append(nl),
        net_liq=lambda ib_: 300000.0,
        cash_by_ccy=lambda ib_: {"HKD": 50000.0, "USD": 10000.0},
        held_positions=lambda ib_: {s: (Pos(C(s, currency_of(y)), q), q)
                                    for s, (y, q) in HELD.items()},
        reserve_working_cash=lambda ib_: None, warm_fx_memory=lambda ib_, a: None,
        to_ib=lambda ysym: C(ysym.split(".")[0], currency_of(ysym)),
        entry_blocked_reason=lambda ysym, ccy: None,
        fx_rate=lambda ib_, a, b: HKD_PER[a] / HKD_PER[b],
        ensure_ccy=lambda ib_, ccy, need, dry_: True,
        lot_size=lambda ib_, c: {"JPY": 100, "HKD": 10}.get(c.currency, 1),
        min_tick=lambda ib_, c: 0.01, live_base_price=lambda ib_, c, fallback: fallback,
        confirm=lambda msg: True, EXIT_ATTEMPTS=exit_memo, FILLS_LEDGER=d / "fills.jsonl",
        log=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    old = {k: getattr(ib_bot, k) for k in stubs}
    try:
        for k, v in stubs.items():
            setattr(ib_bot, k, v)
        ib_bot.run(dry=dry)
    finally:
        for k, v in old.items():
            setattr(ib_bot, k, v)
        del ib_bot.PLACED[:]
        ib_bot._FX_COMMITTED.clear()
        ib_bot._EARMARK_RUN.clear()
        ib_bot._POCKET_RUN.clear()
    queued = [q["text"] for _, q in alerts._queued()] if alerts.DIR.exists() else []
    memo_after = (json.loads(exit_memo.read_text(encoding="utf-8"))
                  if exit_memo.exists() else {})
    return (ib.placed, json.loads(state_path.read_text(encoding="utf-8")), lines,
            queued, memo_after, published)


MORNING = at(2026, 9, 16, 9, 0, 20)          # Wed: EU 11:00 CEST, HK 17:00 HKT, JP 18:00 JST
EVENING = at(2026, 9, 16, 23, 35, 20)        # Wed: every market's bar is final


def t6_run_at_0900_defers_eu_and_hk_and_evaluates_jp():
    placed, st, lines, queued, memo, published = bot_run(MORNING, BELOW)
    # the EU holding below its SMA200: no sell, and its stops did not move
    assert not [p for p in placed if p[2] == "DBK"], placed
    assert st["pos"]["DBK.DE"] == SEED_POS["DBK.DE"], st["pos"]["DBK.DE"]
    assert len([l for l in lines if "DBK.DE: exit rules deferred" in l]) == 1, lines
    # the JP holding is evaluated exactly as before: ratchet, then regime break
    assert ("SELL", 100, "7733") in placed, placed
    assert st["pos"]["7733.T"]["hw"] == 3000 and st["pos"]["7733.T"]["stop"] == 2900, st["pos"]
    # entries: EU and HK skipped with a log line, JP placed
    assert ("BUY", 100, "6758") in placed, placed
    assert not [p for p in placed if p[2] in ("SAP", "0700")], placed
    assert any("skip SAP.DE: entry deferred" in l for l in lines), lines
    assert any("skip 0700.HK: entry deferred" in l for l in lines), lines
    assert "SAP.DE" not in st["pos"] and "0700.HK" not in st["pos"], st["pos"]
    assert len(published) == 1, "publishing is unaffected"

    # CONTROL - the same cards at 23:35: DBK is sold and ratcheted, EU/HK enter
    placed, st, lines, queued, memo, published = bot_run(EVENING, BELOW)
    assert ("SELL", 40, "DBK") in placed and ("SELL", 100, "7733") in placed, placed
    assert st["pos"]["DBK.DE"]["hw"] == 30 and st["pos"]["DBK.DE"]["stop"] == 28, st["pos"]
    assert ("BUY", 11, "SAP") in placed and ("BUY", 50, "0700") in placed, placed
    assert not [l for l in lines if "deferred" in l], lines
    print("t6 run() at 09:00 UTC: EU exit and stops untouched, JP evaluated, EU/HK entries skipped OK")


def t7_deferred_is_not_a_lapsed_exit():
    # A refused DBK exit is on record and its condition has cleared. At 09:00 the
    # rule was not evaluated at all: no lapsed-exit alert, the record stays.
    placed, st, lines, queued, memo, _ = bot_run(MORNING, CALM_EU, memo=REJECTED_DBK)
    assert not [q for q in queued if "DBK" in q], queued
    assert memo.get("DBK.DE") == REJECTED_DBK["DBK.DE"], memo
    assert not [p for p in placed if p[2] == "DBK"], placed
    # CONTROL - at 23:35 the rule IS evaluated, does not fire, and the detector
    # reports the lapsed exit and drops the record
    placed, st, lines, queued, memo, _ = bot_run(EVENING, CALM_EU, memo=REJECTED_DBK)
    lapsed = [q for q in queued if "DBK.DE" in q and "never completed" in q]
    assert len(lapsed) == 1, queued
    assert "DBK.DE" not in memo, memo
    assert not [p for p in placed if p[2] == "DBK"], placed
    print("t7 a deferred symbol raises no lapsed-exit alert and keeps its memo OK")


def t8_run_reads_the_clock_once_through_now_utc():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ib_bot.py"),
               encoding="utf-8").read()
    body = src[src.index("def run(dry=False):"):src.index("def publish_only():")]
    assert body.count("decide_now = _now_utc()") == 1, "one clock read per run"
    assert body.count("market_decidable(ysym, decide_now)") == 2, "exits and entries"
    # the exit check sits before the card fetch and the ratchet
    assert body.index("market_decidable(ysym, decide_now)") < body.index("st.update(hw=hw, stop=trail)")
    print("t8 run() decides every market on one _now_utc() reading OK")


# ------------------------------------------------ market_clock + build time --
HERE = os.path.dirname(os.path.abspath(__file__))


def _src(name):
    return open(os.path.join(HERE, name), encoding="utf-8").read()


def t9_market_clock_is_python39_stdlib_and_shared():
    # daily_signal and telegram_poll run under /usr/bin/python3 = 3.9 and import
    # market_clock (and alerts, which gained close_episode).
    for name in ("market_clock.py", "daily_signal.py", "alerts.py", "telegram_poll.py"):
        ast.parse(_src(name), filename=name, feature_version=(3, 9))
    tree = ast.parse(_src("market_clock.py"), feature_version=(3, 9))
    imported = set()
    for node in ast.walk(tree):
        assert type(node).__name__ != "Match", "a match statement needs 3.10"
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(str(node.module).split(".")[0])
        elif isinstance(node, ast.Attribute):
            assert node.attr != "UTC", "datetime.UTC is 3.11+"
        # X | Y in an annotation is evaluated at runtime on 3.9 and raises
        anns = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            anns = [a.annotation for a in node.args.args + node.args.kwonlyargs] + [node.returns]
        elif isinstance(node, ast.AnnAssign):
            anns = [node.annotation]
        for ann in anns:
            if ann is not None:
                assert not [n for n in ast.walk(ann)
                            if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr)], ast.dump(ann)
    assert imported <= {"datetime", "zoneinfo"}, imported
    # one table: ib_bot re-exports the shared objects and keeps no copy
    for name in ("MARKET_SESSIONS", "SESSION_SETTLE_MIN", "market_decidable",
                 "last_settled_close", "parse_generated_at"):
        assert getattr(ib_bot, name) is getattr(market_clock, name), name
    assert "def market_decidable" not in _src("ib_bot.py")
    assert "MARKET_SESSIONS = {" not in _src("ib_bot.py")
    ds_imports = set()
    for node in ast.walk(ast.parse(_src("daily_signal.py"))):
        if isinstance(node, ast.Import):
            ds_imports |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            ds_imports.add(str(node.module))
    assert "market_clock" in ds_imports and "ib_bot" not in ds_imports, ds_imports
    print("t9 market_clock parses as 3.9, stdlib only, and ib_bot re-exports it OK")


def t10_last_settled_close_weekends_and_dst():
    L = market_clock.last_settled_close
    rows = [
        # JP: 15:30 JST + 90 min = 17:00 JST = 08:00 UTC, no DST
        (JP, at(2026, 9, 16, 9, 0, 20), at(2026, 9, 16, 8, 0)),
        (JP, at(2026, 9, 16, 8, 0), at(2026, 9, 16, 8, 0)),           # AT counts
        (JP, at(2026, 9, 16, 7, 59, 59), at(2026, 9, 15, 8, 0)),
        (JP, at(2026, 9, 19, 12, 0), at(2026, 9, 18, 8, 0)),          # Saturday -> Friday
        (JP, at(2026, 9, 20, 23, 35), at(2026, 9, 18, 8, 0)),         # Monday 08:35 JST
        (JP, datetime(2026, 9, 16, 9, 0), at(2026, 9, 16, 8, 0)),     # naive = UTC
        # HK: 16:10 + 90 = 17:40 HKT = 09:40 UTC
        (HK, at(2026, 9, 16, 9, 0, 20), at(2026, 9, 15, 9, 40)),
        (HK, at(2026, 9, 16, 9, 40), at(2026, 9, 16, 9, 40)),
        # US: 17:30 New York. EDT 21:30 UTC, EST 22:30 UTC
        (US, at(2026, 9, 16, 23, 35), at(2026, 9, 16, 21, 30)),
        (US, at(2026, 1, 14, 23, 35), at(2026, 1, 14, 22, 30)),
        (US, at(2026, 1, 14, 22, 29, 59), at(2026, 1, 13, 22, 30)),
        (US, at(2026, 1, 19, 9, 0), at(2026, 1, 16, 22, 30)),         # Monday -> Friday
        # US DST starts Sun 2026-03-08: Friday settles EST, Monday EDT
        (US, at(2026, 3, 9, 21, 29), at(2026, 3, 6, 22, 30)),
        (US, at(2026, 3, 9, 21, 30), at(2026, 3, 9, 21, 30)),
        # US DST ends Sun 2026-11-01: Friday settles EDT, Monday EST
        (US, at(2026, 11, 2, 13, 0), at(2026, 10, 30, 21, 30)),
        (US, at(2026, 11, 2, 22, 29), at(2026, 10, 30, 21, 30)),
        (US, at(2026, 11, 2, 22, 30), at(2026, 11, 2, 22, 30)),
        # EU: 19:00 Berlin. DST starts Sun 2026-03-29, ends Sun 2026-10-25
        (DE, at(2026, 3, 30, 16, 59), at(2026, 3, 27, 18, 0)),
        (DE, at(2026, 3, 30, 17, 0), at(2026, 3, 30, 17, 0)),
        (DE, at(2026, 10, 26, 17, 30), at(2026, 10, 23, 17, 0)),
        (DE, at(2026, 10, 26, 18, 0), at(2026, 10, 26, 18, 0)),
    ]
    for sym, t, want in rows:
        got = L(sym, t)
        assert got == want and got.utcoffset() == timedelta(0), (sym, t.isoformat(), got, want)
    for sym in (CRYPTO, "ETH-USD", "600519.SS", "RY.TO"):
        assert L(sym, at(2026, 9, 16, 9, 0)) is None, sym

    # Independent of the implementation: a settle instant is exactly where
    # market_decidable flips from "settling" to decidable, and every approved
    # close + 90 min falls on a 10-minute mark. The newest such flip at or before
    # t must be what last_settled_close returns - across both DST weeks.
    step = timedelta(minutes=10)
    windows = (at(2026, 3, 5, 0, 0), at(2026, 3, 26, 0, 0),
               at(2026, 10, 22, 0, 0), at(2026, 10, 29, 0, 0))
    checked = 0
    for sym in (US, DE, JP, HK, "X.HE", "X.L"):
        for start in windows:
            for k in range(0, 6 * 24 * 60, 181):
                t = start + timedelta(minutes=k, seconds=7)
                u = t.replace(second=0) - timedelta(minutes=t.minute % 10)
                while not (decidable(sym, u)[0] and not decidable(sym, u - step)[0]):
                    u -= step
                assert L(sym, t) == u, (sym, t.isoformat(), L(sym, t), u)
                checked += 1
    assert checked > 700, checked
    print("t10 last_settled_close across weekends and both DST shifts (%d cross-checks) OK"
          % checked)


def t11_parse_generated_at():
    P = market_clock.parse_generated_at
    assert P("2026-09-16T06:35:00Z") == at(2026, 9, 16, 6, 35)
    assert P(" 2026-09-16T06:35:00Z ") == at(2026, 9, 16, 6, 35)
    assert P("2026-09-16T06:35:00+00:00") == at(2026, 9, 16, 6, 35)
    assert P("2026-09-16T14:35:00+08:00") == at(2026, 9, 16, 6, 35)
    for bad in (None, "", "yesterday", "2026-09-16", "2026-09-16T06:35:00", 1758004500, {"t": 1}):
        assert P(bad) is None, bad
    print("t11 generated_at parses as UTC; missing, naive or garbage is None OK")


STALE = "2026-09-16T06:35:00Z"     # 15:35 JST: Tokyo's close had not settled
FRESH = "2026-09-16T08:35:00Z"     # 17:35 JST: after 15:30 + 90 min
CALM_JP = {"DBK.DE": {"price": 35, "sma200": 32, "atr": 1},
           "7733.T": {"price": 3200, "sma200": 3100, "atr": 50}}     # no rule fires
REJECTED_7733 = {"7733.T": {"time": "2026-09-15 09:00 UTC", "status": "REJECTED",
                            "qty": 100, "reason": "regime break (close < SMA200)"}}


def stale_alerts(texts):
    return [q for q in texts if "Signals are stale" in q]


def t12_stale_build_defers_the_market_like_the_clock():
    # The reviewer's case: the newest build started 06:35Z and the run is 09:00Z.
    # market_decidable passes JP (18:00 JST), but the card's bar is pre-auction.
    outbox = _TMP / "stale-outbox"
    placed, st, lines, queued, memo, published = bot_run(
        MORNING, BELOW, built=STALE, card_built={"7733.T": STALE}, outbox=outbox)
    assert not [p for p in placed if p[2] in ("7733", "6758")], placed
    assert placed == [], placed                       # EU and HK wait on the clock
    assert st["pos"]["7733.T"] == SEED_POS["7733.T"], "no ratchet on a stale card"
    assert [l for l in lines if "7733.T: exit rules deferred to a fresh build" in l
            and "06:35" in l and "08:00" in l], lines
    assert [l for l in lines if "skip 6758.T: entry deferred to a fresh build" in l], lines
    assert "6758.T" not in st["pos"], st["pos"]
    # two stale markets in one run: ONE alert
    assert len(stale_alerts(queued)) == 1 and len(queued) == 1, queued
    assert "06:35" in queued[0] and "7733.T" in queued[0], queued[0]
    assert len(published) == 1, "a deferral is not an abort"
    # the clock deferrals are untouched
    assert len([l for l in lines if "DBK.DE: exit rules deferred to a finished bar" in l]) == 1

    # Delivered, then the 23:35 run on the SAME stale build, same UTC day: now
    # every market is behind its close, and still no second alert that day.
    assert alerts.drain(lambda text: None) == 1
    placed, st, lines, queued, memo, published = bot_run(
        EVENING, BELOW, built=STALE, outbox=outbox)
    assert placed == [], placed
    assert st["pos"] == SEED_POS, st["pos"]
    for sym in ("DBK.DE", "7733.T"):
        assert [l for l in lines if sym + ": exit rules deferred to a fresh build" in l], lines
    for sym in ("SAP.DE", "6758.T", "0700.HK"):
        assert [l for l in lines if "skip " + sym + ": entry deferred to a fresh build" in l], lines
    assert queued == [], "one stale-signals alert per UTC day: %s" % queued
    # the next UTC day it is news again
    placed, st, lines, queued, memo, published = bot_run(
        at(2026, 9, 17, 9, 0, 20), BELOW, built=STALE, outbox=outbox)
    assert len(stale_alerts(queued)) == 1, queued

    # a stale market is not "a rule that did not fire": no lapsed alert, memo kept
    placed, st, lines, queued, memo, _ = bot_run(MORNING, CALM_JP, memo=REJECTED_7733,
                                                 built=STALE)
    assert not [q for q in queued if "never completed" in q], queued
    assert memo.get("7733.T") == REJECTED_7733["7733.T"], memo
    # CONTROL - the same memo on a fresh build: evaluated, lapsed, reported
    placed, st, lines, queued, memo, _ = bot_run(MORNING, CALM_JP, memo=REJECTED_7733,
                                                 built=FRESH)
    assert [q for q in queued if "7733.T" in q and "never completed" in q], queued
    assert "7733.T" not in memo, memo

    # --dry: deferred the same way, and no alert written
    dry_box = _TMP / "stale-dry-outbox"
    placed, st, lines, queued, memo, published = bot_run(
        MORNING, BELOW, built=STALE, outbox=dry_box, dry=True)
    assert placed == [] and published == [], (placed, published)
    assert [l for l in lines if "7733.T: exit rules deferred to a fresh build" in l], lines
    assert not dry_box.exists() or not list(dry_box.iterdir()), list(dry_box.iterdir())
    print("t12 a 06:35Z build read at 09:00Z defers JP like the clock, one alert a day OK")


def t13_fresh_build_is_decided_and_the_card_time_comes_first():
    placed, st, lines, queued, memo, published = bot_run(
        MORNING, BELOW, built=FRESH, card_built={"7733.T": FRESH})
    assert ("SELL", 100, "7733") in placed and ("BUY", 100, "6758") in placed, placed
    assert st["pos"]["7733.T"]["hw"] == 3000 and st["pos"]["7733.T"]["stop"] == 2900
    assert not [l for l in lines if "fresh build" in l or "generated_at" in l], lines
    assert stale_alerts(queued) == [], queued
    # a build that started AT the settle instant holds the bar; the card has no
    # time of its own here, so the exit falls back to data.json's
    placed, st, lines, queued, memo, published = bot_run(
        MORNING, BELOW, built="2026-09-16T08:00:00Z")
    assert ("SELL", 100, "7733") in placed and ("BUY", 100, "6758") in placed, placed
    placed, st, lines, queued, memo, published = bot_run(
        MORNING, BELOW, built="2026-09-16T07:59:59Z")
    assert not [p for p in placed if p[2] in ("7733", "6758")], placed
    # the card's own time wins over data.json's, both ways
    placed, st, lines, queued, memo, published = bot_run(
        MORNING, BELOW, built=STALE, card_built={"7733.T": FRESH})
    assert ("SELL", 100, "7733") in placed, placed                  # exit: its card
    assert not [p for p in placed if p[2] == "6758"], placed        # entry: data.json
    placed, st, lines, queued, memo, published = bot_run(
        MORNING, BELOW, built=FRESH, card_built={"7733.T": STALE})
    assert not [p for p in placed if p[2] == "7733"], placed
    assert st["pos"]["7733.T"] == SEED_POS["7733.T"], st["pos"]
    assert ("BUY", 100, "6758") in placed, placed
    # an unreadable card time falls back to data.json's too
    placed, st, lines, queued, memo, published = bot_run(
        MORNING, BELOW, built=FRESH, card_built={"7733.T": "soon"})
    assert ("SELL", 100, "7733") in placed, placed
    print("t13 a fresh build is decided; an exit reads its card's time first OK")


def t14_missing_generated_at_keeps_the_clock_rule_and_says_so_once():
    for built in (None, "", "yesterday evening", "2026-09-16T08:35:00"):
        placed, st, lines, queued, memo, published = bot_run(MORNING, BELOW, built=built)
        # exactly t6's outcome
        assert ("SELL", 100, "7733") in placed and ("BUY", 100, "6758") in placed, (built, placed)
        assert not [p for p in placed if p[2] in ("DBK", "SAP", "0700")], placed
        notes = [l for l in lines if "no usable generated_at" in l]
        assert len(notes) == 1, (built, lines)
        assert not [l for l in lines if "fresh build" in l], lines
        assert stale_alerts(queued) == [], queued
    # a card with a time of its own is still checked when data.json has none
    placed, st, lines, queued, memo, published = bot_run(
        MORNING, BELOW, card_built={"7733.T": STALE})
    assert not [p for p in placed if p[2] == "7733"], placed
    assert ("BUY", 100, "6758") in placed, placed          # entries: the clock alone
    # CONTROL for the log: a usable field says nothing
    placed, st, lines, queued, memo, published = bot_run(EVENING, BELOW, built=FRESH)
    assert not [l for l in lines if "generated_at" in l], lines
    print("t14 a missing or unreadable generated_at keeps the clock rule, logged once OK")


if __name__ == "__main__":
    t1_the_two_daily_runs_summer_and_winter()
    t2_weekends_always_decide()
    t3_us_close_plus_90_follows_dst()
    t4_hk_closing_auction_and_crypto()
    t5_every_market_row_matches_the_approved_table()
    t6_run_at_0900_defers_eu_and_hk_and_evaluates_jp()
    t7_deferred_is_not_a_lapsed_exit()
    t8_run_reads_the_clock_once_through_now_utc()
    t9_market_clock_is_python39_stdlib_and_shared()
    t10_last_settled_close_weekends_and_dst()
    t11_parse_generated_at()
    t12_stale_build_defers_the_market_like_the_clock()
    t13_fresh_build_is_decided_and_the_card_time_comes_first()
    t14_missing_generated_at_keeps_the_clock_rule_and_says_so_once()
    print("ALL MARKET-CLOCK TESTS PASS")
