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
Nothing here touches /root: every path is repointed before ib_bot is imported.
"""
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


def bot_run(now, cards, memo=None):
    d = Path(tempfile.mkdtemp(prefix="run-", dir=str(_TMP)))
    state_path = d / "state.json"
    state_path.write_text(json.dumps({"map": {k: v[0] for k, v in HELD.items()},
                                      "pos": SEED_POS, "_peak_netliq": 300000}),
                          encoding="utf-8")
    exit_memo = d / "exit_attempts.json"
    if memo is not None:
        exit_memo.write_text(json.dumps(memo), encoding="utf-8")
    alerts.DIR = d / "outbox"
    signals = {"generated": "2026-09-16", "actions": ACTIONS}

    def get_json(url):
        if url == ib_bot.SIGNALS_URL:
            return signals
        for ysym, card in cards.items():
            if url.endswith("/" + ib_bot.safe_name(ysym) + ".json"):
                return {"card": card}
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
        ib_bot.run(dry=False)
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


if __name__ == "__main__":
    t1_the_two_daily_runs_summer_and_winter()
    t2_weekends_always_decide()
    t3_us_close_plus_90_follows_dst()
    t4_hk_closing_auction_and_crypto()
    t5_every_market_row_matches_the_approved_table()
    t6_run_at_0900_defers_eu_and_hk_and_evaluates_jp()
    t7_deferred_is_not_a_lapsed_exit()
    t8_run_reads_the_clock_once_through_now_utc()
    print("ALL MARKET-CLOCK TESTS PASS")
