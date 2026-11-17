#!/usr/bin/env python3
"""Golden tests: the after-close update alarm (owner decision 2026-11-17).

Run from this directory:  python test_post_close_alarm.py

Since 1 Nov 2026 only 3 of 11 nights had a signal build that started between
the US settle (22:30 UTC in winter) and the 23:35 run, so on most nights the
23:35 run deferred every US exit and entry to the 10:00 London catch-up - and
only bot.log said so. execution/post_close_build.py now asks GitHub for a build
at 22:40 UTC Monday to Friday, and ib_bot raises an alarm when the build the
run reads still started before a market's settled close. It is MESSAGE ONLY.

Locked down here, through ib_bot.run() itself (the stubbed-IB harness of
test_market_clock.py, with a US holding and a US BUY candidate):
  * 23:35 on a pre-close build (20:17Z): ONE plain alert for the US, naming
    the holding, the candidate, or both, and "the next run (Tue 10:00 UTC)";
  * a fresh build (22:41Z): no alert, and the SELL and the BUY are placed;
  * the 10:00 catch-up still on the pre-close build: ONE louder alert, "is
    being MISSED", and the plain one is never repeated - not by a 23:50 rerun,
    not by the 12:00 retry;
  * routine waits stay quiet: a run before the 22:40 request has had its 30
    minutes, and a catch-up whose deferral is a close that settled after the
    last 22:40 request (Tokyo at 08:00 UTC);
  * US summer (settle 21:30 UTC, catch-up 09:00 UTC), and a Friday close whose
    next run is Saturday's catch-up;
  * NO ORDER CHANGES: every scenario places the same orders and saves the same
    state with the alarm switched off; a broken alarm costs the alarm, never
    the run; --dry queues nothing.
FIXED 2026-11-17 (review of the first build):
  * a BUY candidate the loop would skip on a fresh build too - London (GBP,
    pence not scaled) or one entry_blocked_reason refuses - is no missed buy
    and raises no alarm; a London HOLDING still does (exits stay open);
  * Europe is ONE market: SAP.DE, MC.PA, ASML.AS, ENI.MI and EQNR.OL behind
    the same evening give one alert listing them all, not one per suffix.
Nothing here touches /root: every path is repointed before ib_bot is imported.
"""
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("IB_BACKEND", "web")
import testenv                                     # noqa: E402
_TMP = testenv.isolate("mps-pcalarm-")
os.environ.pop("EXCLUDED_CASH", None)

import alerts                                      # noqa: E402
import ib_bot                                      # noqa: E402
from contracts import currency_of                  # noqa: E402
testenv.assert_isolated()

UTC = timezone.utc


def at(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


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


TODAY = datetime.now(UTC).date().isoformat()
US_HELD = {"AAPL": ("AAPL", 10)}
JP_HELD = {"7733": ("7733.T", 100)}
UK_HELD = {"BP": ("BP.L", 200)}
SEED_POS = {"AAPL": {"entry": 190, "hw": 205, "stop": 170, "entry_date": TODAY},
            "7733.T": {"entry": 2800, "hw": 2900, "stop": 2700, "entry_date": TODAY},
            "BP.L": {"entry": 520, "hw": 560, "stop": 500, "entry_date": TODAY}}
CARDS = {"AAPL": {"price": 180, "sma200": 200, "atr": 3},        # regime break
         "7733.T": {"price": 3000, "sma200": 3100, "atr": 50},   # regime break
         "BP.L": {"price": 480, "sma200": 510, "atr": 9}}        # regime break
UK_BUY = [{"symbol": "BP.L", "action": "BUY", "price": 540, "score": 9, "stop": 500}]
HK_BUY = [{"symbol": "0700.HK", "action": "BUY", "price": 400, "score": 4, "stop": 360}]
EU_BUY = [{"symbol": s, "action": "BUY", "price": p, "score": 8, "stop": p * 0.9}
          for s, p in (("SAP.DE", 200), ("MC.PA", 600), ("ASML.AS", 700),
                       ("ENI.MI", 14), ("EQNR.OL", 260))]
US_BUY = [{"symbol": "NVDA", "action": "BUY", "price": 150, "score": 9, "stop": 135}]
JP_BUY = [{"symbol": "6758.T", "action": "BUY", "price": 3000, "score": 5, "stop": 2700}]
HKD_PER = {"HKD": 1.0, "USD": 7.8, "EUR": 9.047, "JPY": 0.053, "GBP": 10.6,
           "NOK": 0.72}

# Winter: US on EST since 1 Nov 2026, the UK on GMT since 25 Oct.
MON_EVENING = at(2026, 11, 16, 23, 35, 20)      # Mon: US close settled 22:30 UTC
TUE_CATCHUP = at(2026, 11, 17, 10, 0, 20)       # 10:00 London = 10:00 UTC
PRE_CLOSE = "2026-11-16T20:17:00Z"              # an afternoon hourly build
POST_CLOSE = "2026-11-16T22:41:30Z"             # the 22:40 request's build


def bot_run(now, held, actions, built, card_built=None, outbox=None, dry=False,
            alarm=True, broken=None, blocked=None):
    """One ib_bot.run() at `now`. held: {IB sym: (ysym, qty)}; built: data.json's
    generated_at; card_built: {ysym: generated_at} for the product cards (else
    the cards carry `built` too); outbox: a spool kept across runs; alarm=False
    switches the after-close alarm off (the control); broken: a name in ib_bot
    to replace with a function that raises; blocked: entry_blocked_reason's
    stand-in (default: nothing is blocked)."""
    d = Path(tempfile.mkdtemp(prefix="run-", dir=str(_TMP)))
    state_path = d / "state.json"
    state_path.write_text(json.dumps({"map": {k: v[0] for k, v in held.items()},
                                      "pos": {v[0]: SEED_POS[v[0]] for v in held.values()},
                                      "_peak_netliq": 300000}), encoding="utf-8")
    alerts.DIR = Path(outbox) if outbox else d / "outbox"
    signals = {"generated": str(built)[:10], "actions": actions, "generated_at": built}
    card_built = card_built or {}

    def get_json(url):
        if url == ib_bot.SIGNALS_URL:
            return signals
        for ysym, card in CARDS.items():
            if url.endswith("/" + ib_bot.safe_name(ysym) + ".json"):
                return {"card": card, "generated_at": card_built.get(ysym, built)}
        raise AssertionError("unexpected fetch " + url)

    def boom(*a, **k):
        raise RuntimeError("alarm helper exploded")

    ib, lines, published = FakeIB(), [], []
    stubs = dict(
        STATE=state_path, IB=lambda: ib, connect_or_heal=lambda *a, **k: None,
        get_json=get_json, _now_utc=lambda: now,
        publish_state=lambda ib_, st, nl: published.append(nl),
        net_liq=lambda ib_: 300000.0,
        cash_by_ccy=lambda ib_: {"HKD": 7.0, "USD": 33500.0},
        held_positions=lambda ib_: {s: (Pos(C(s, currency_of(y)), q), q)
                                    for s, (y, q) in held.items()},
        reserve_working_cash=lambda ib_: ib_bot._RESERVE_OK.__setitem__("ok", True),
        warm_fx_memory=lambda ib_, a: None,
        to_ib=lambda ysym: C(ysym.split(".")[0], currency_of(ysym)),
        entry_blocked_reason=blocked or (lambda ysym, ccy: None),
        fx_rate=lambda ib_, a, b: HKD_PER[a] / HKD_PER[b],
        ensure_ccy=lambda ib_, ccy, need, dry_: True,
        lot_size=lambda ib_, c: {"JPY": 100, "HKD": 10}.get(c.currency, 1),
        min_tick=lambda ib_, c: 0.01, live_base_price=lambda ib_, c, fallback: fallback,
        confirm=lambda msg: True, EXIT_ATTEMPTS=d / "exit_attempts.json",
        FILLS_LEDGER=d / "fills.jsonl",
        log=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    if not alarm:
        stubs["_post_close_alarm"] = lambda book, now_utc: 0
    if broken:
        stubs[broken] = boom
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
    return (ib.placed, json.loads(state_path.read_text(encoding="utf-8")), lines,
            queued, published)


def alarms(queued):
    return [q for q in queued if "After-close update missing" in q or "is being MISSED" in q]


def same_orders_without_alarm(now, held, actions, built, **kw):
    """The alarm is message-only: run without it and with it, compare. The run
    with it goes last, so alerts.DIR is left on its spool (`outbox`)."""
    off = bot_run(now, held, actions, built, alarm=False,
                  **{k: v for k, v in kw.items() if k != "outbox"})
    on = bot_run(now, held, actions, built, **kw)
    assert on[0] == off[0], ("orders differ with the alarm on", on[0], off[0])
    assert on[1] == off[1], ("state differs with the alarm on", on[1], off[1])
    assert on[4] == off[4], ("publish differs with the alarm on", on[4], off[4])
    assert alarms(off[3]) == [], off[3]
    return on


def t1_pre_close_build_at_2335_one_plain_alert_for_a_holding():
    placed, st, lines, queued, published = same_orders_without_alarm(
        MON_EVENING, US_HELD, [], PRE_CLOSE)
    assert placed == [], "the deferred holding is not sold: %s" % placed
    assert st["pos"]["AAPL"] == SEED_POS["AAPL"], "no ratchet on a pre-close card"
    assert [l for l in lines if "AAPL: exit rules deferred to a fresh build" in l], lines
    assert len(published) == 1, "an alarm is not an abort"
    assert len(alarms(queued)) == 1 == len(queued), queued
    q = queued[0]
    assert q.startswith("⚠️ After-close update missing: the newest signals build "
                        "started Mon 16 Nov 20:17 UTC, before the US close of Mon 16 Nov "
                        "settled (22:30 UTC). US buys and sells wait for the next run "
                        "(Tue 10:00 UTC)"), q
    assert "1 holding(s) not checked for exits (AAPL)" in q and "candidate" not in q, q
    assert [l for l in lines if "after-close update alarm: 1 alert(s) queued" in l], lines
    print("t1 23:35 on a 20:17Z build: the US holding waits, ONE plain alert names it OK")


def t2_a_buy_candidate_alone_and_both_in_one_alert():
    placed, st, lines, queued, published = same_orders_without_alarm(
        MON_EVENING, {}, US_BUY, PRE_CLOSE)
    assert placed == [] and "NVDA" not in st["pos"], (placed, st["pos"])
    assert [l for l in lines if "skip NVDA: entry deferred to a fresh build" in l], lines
    assert len(alarms(queued)) == 1 == len(queued), queued
    assert "1 buy candidate(s) waiting (NVDA)" in queued[0], queued[0]
    assert "holding" not in queued[0], queued[0]
    # a holding and a candidate in the same market: still ONE alert, both named
    placed, st, lines, queued, published = same_orders_without_alarm(
        MON_EVENING, US_HELD, US_BUY, PRE_CLOSE)
    assert placed == [], placed
    assert len(alarms(queued)) == 1 == len(queued), queued
    assert ("1 holding(s) not checked for exits (AAPL); 1 buy candidate(s) "
            "waiting (NVDA)") in queued[0], queued[0]
    print("t2 a candidate alone is named; holding + candidate give ONE alert per market OK")


def t3_fresh_build_no_alert_and_the_decisions_are_made():
    placed, st, lines, queued, published = same_orders_without_alarm(
        MON_EVENING, US_HELD, US_BUY, POST_CLOSE)
    assert ("SELL", 10, "AAPL") in placed, placed
    assert [p for p in placed if p[0] == "BUY" and p[2] == "NVDA"], placed
    assert not [l for l in lines if "fresh build" in l], lines
    assert queued == [], queued
    # a build that started exactly at the settle holds the bar
    placed, st, lines, queued, published = bot_run(
        MON_EVENING, US_HELD, US_BUY, "2026-11-16T22:30:00Z")
    assert ("SELL", 10, "AAPL") in placed and queued == [], (placed, queued)
    print("t3 a 22:41Z build: SELL AAPL and BUY NVDA go out, no alarm OK")


def t4_catchup_still_behind_the_louder_one_once():
    box = _TMP / "catchup-outbox"
    bot_run(MON_EVENING, US_HELD, US_BUY, PRE_CLOSE, outbox=box)
    first = alarms([q["text"] for _, q in alerts._queued()])
    assert len(first) == 1 and "After-close update missing" in first[0], first
    # a rerun the same evening: nothing new (once per close per market)
    placed, st, lines, queued, published = bot_run(
        at(2026, 11, 16, 23, 50), US_HELD, US_BUY, PRE_CLOSE, outbox=box)
    assert alarms(queued) == first, queued
    assert alerts.drain(lambda text: None) == 1           # delivered; still remembered
    # the 10:00 catch-up on the SAME build: the close is still undecidable, and
    # the next run (23:35) judges Tuesday's close instead - the louder alert
    placed, st, lines, queued, published = same_orders_without_alarm(
        TUE_CATCHUP, US_HELD, US_BUY, PRE_CLOSE, outbox=box)
    assert placed == [] and st["pos"]["AAPL"] == SEED_POS["AAPL"], (placed, st["pos"])
    assert len(alarms(queued)) == 1 == len(queued), queued
    q = queued[0]
    assert q.startswith("🚨 US close of Mon 16 Nov is being MISSED: no signals build "
                        "started after it settled (22:30 UTC) - the newest started "
                        "Mon 16 Nov 20:17 UTC."), q
    assert ("By the next run (Tue 23:35 UTC) a later close replaces it or the "
            "market is open, so buys signalled on this close are skipped and these "
            "holdings are next checked on a later close: 1 holding(s) not checked "
            "for exits (AAPL); 1 buy candidate(s) waiting (NVDA).") in q, q
    assert "/root/post_close.log" in q, q
    assert "After-close update missing" not in " ".join(queued), "the plain one is not repeated"
    # the 12:00 London retry adds nothing, delivered or not
    assert alerts.drain(lambda text: None) == 1
    placed, st, lines, queued, published = bot_run(
        at(2026, 11, 17, 12, 0, 20), US_HELD, US_BUY, PRE_CLOSE, outbox=box)
    assert alarms(queued) == [], queued
    print("t4 the catch-up still on the 20:17Z build: ONE louder 'MISSED' alert, never repeated OK")


def t5_routine_waits_stay_quiet():
    # before the 22:40 request has had its 30 minutes: a hand run at 22:50 and
    # one at 23:09 say nothing; 23:10 is the first that may
    for when, want in ((at(2026, 11, 16, 22, 50), 0), (at(2026, 11, 16, 23, 9, 59), 0),
                       (at(2026, 11, 16, 23, 10), 1)):
        placed, st, lines, queued, published = bot_run(when, US_HELD, [], PRE_CLOSE)
        assert placed == [], (when, placed)
        assert len(alarms(queued)) == want, (when, queued)
    # the catch-up on a build after Monday's US settle but before Tuesday's
    # Tokyo settle (08:00 UTC): US decided, Tokyo waits for 23:35 - routine,
    # because no 22:40 request has come since that close
    placed, st, lines, queued, published = same_orders_without_alarm(
        TUE_CATCHUP, dict(US_HELD, **JP_HELD), US_BUY + JP_BUY, "2026-11-17T05:05:00Z")
    assert ("SELL", 10, "AAPL") in placed, placed
    assert not [p for p in placed if p[2] in ("7733", "6758")], placed
    assert [l for l in lines if "7733.T: exit rules deferred to a fresh build" in l], lines
    assert queued == [], queued
    # ... but at 23:35 that Tokyo close, still without a build, is alarmed -
    # MISSED, as the next run (Wed 10:00 UTC) has Wednesday's close
    placed, st, lines, queued, published = bot_run(
        at(2026, 11, 17, 23, 35, 20), JP_HELD, JP_BUY, "2026-11-17T05:05:00Z")
    assert len(alarms(queued)) == 1, queued
    assert queued[0].startswith("🚨 Tokyo close of Tue 17 Nov is being MISSED"), queued[0]
    assert ("so buys signalled on this close are skipped and these holdings are "
            "next checked on a later close: 1 holding(s)") in queued[0], queued[0]
    print("t5 before 23:10 and a morning Tokyo wait: no alarm; Tokyo still behind at 23:35 is OK")


def t6_us_summer_and_the_friday_close():
    # Wed 16 Sep 2026: US on EDT, settle 21:30 UTC; the catch-up is 09:00 UTC
    evening = at(2026, 9, 16, 23, 35, 20)
    placed, st, lines, queued, published = bot_run(
        evening, US_HELD, US_BUY, "2026-09-16T21:45:00Z")
    assert ("SELL", 10, "AAPL") in placed and queued == [], (placed, queued)
    placed, st, lines, queued, published = same_orders_without_alarm(
        evening, US_HELD, US_BUY, "2026-09-16T21:05:00Z")
    assert placed == [] and len(alarms(queued)) == 1, (placed, queued)
    assert ("before the US close of Wed 16 Sep settled (21:30 UTC). US buys and "
            "sells wait for the next run (Thu 09:00 UTC)") in queued[0], queued[0]
    # Friday 20 Nov 2026: the next run is Saturday's catch-up, which can still
    # decide Friday's close - plain; Saturday's catch-up on the same build adds
    # nothing (its next run, Sat 23:35, still has Friday's close)
    box = _TMP / "friday-outbox"
    placed, st, lines, queued, published = bot_run(
        at(2026, 11, 20, 23, 35, 20), US_HELD, [], "2026-11-20T19:05:00Z", outbox=box)
    assert len(alarms(queued)) == 1 and "next run (Sat 10:00 UTC)" in queued[0], queued
    placed, st, lines, queued, published = bot_run(
        at(2026, 11, 21, 10, 0, 20), US_HELD, [], "2026-11-20T19:05:00Z", outbox=box)
    assert len(alarms(queued)) == 1, "nothing new on Saturday: %s" % queued
    print("t6 US summer (21:30 settle, 09:00 catch-up) and a Friday close OK")


def t7_a_broken_alarm_costs_the_alarm_not_the_run_and_dry_queues_nothing():
    for name in ("_post_close_alarm", "_note_build_deferral", "_next_trading_run"):
        placed, st, lines, queued, published = bot_run(
            MON_EVENING, US_HELD, US_BUY, PRE_CLOSE, broken=name)
        assert placed == [] and len(published) == 1, (name, placed, published)
        assert alarms(queued) == [], (name, queued)
        assert [l for l in lines if "alert bookkeeping skipped" in l], (name, lines)
    # and on a fresh build the orders still go out with the helper broken
    placed, st, lines, queued, published = bot_run(
        MON_EVENING, US_HELD, US_BUY, POST_CLOSE, broken="_post_close_alarm")
    assert ("SELL", 10, "AAPL") in placed, placed
    box = _TMP / "dry-outbox"
    placed, st, lines, queued, published = bot_run(
        MON_EVENING, US_HELD, US_BUY, PRE_CLOSE, outbox=box, dry=True)
    assert placed == [] and published == [], (placed, published)
    assert [l for l in lines if "AAPL: exit rules deferred to a fresh build" in l], lines
    assert not box.exists() or not list(box.iterdir()), list(box.iterdir())
    print("t7 a raising alarm helper leaves the run whole; --dry queues nothing OK")


def t8_helpers():
    nxt = ib_bot._next_trading_run
    assert nxt(at(2026, 11, 16, 23, 36)) == at(2026, 11, 17, 10, 0)
    assert nxt(at(2026, 11, 17, 10, 1)) == at(2026, 11, 17, 23, 35)
    assert nxt(at(2026, 9, 16, 23, 36)) == at(2026, 9, 17, 9, 0)        # BST
    assert nxt(at(2026, 9, 17, 0, 10)) == at(2026, 9, 17, 9, 0)
    due = ib_bot._post_close_due
    assert due(at(2026, 11, 16, 22, 30)) == at(2026, 11, 16, 23, 10)
    assert due(at(2026, 11, 20, 22, 30)) == at(2026, 11, 20, 23, 10)    # Friday
    assert due(at(2026, 11, 17, 8, 0)) == at(2026, 11, 17, 23, 10)      # Tokyo
    assert due(at(2026, 11, 16, 22, 45)) == at(2026, 11, 17, 23, 10)    # after 22:40
    assert [ib_bot._market_name(ib_bot._market_suffix(s)) for s in
            ("AAPL", "0700.HK", "7733.T", "BP.L", "SAP.DE", "EQNR.OL")] == \
        ["US", "Hong Kong", "Tokyo", "Europe", "Europe", "Europe"]
    # every MARKET_SESSIONS venue but the US, Hong Kong and Tokyo is Europe
    europe = [s for s in ib_bot.MARKET_SESSIONS if s not in ("", ".HK", ".T")]
    assert europe and all(ib_bot._market_name(s) == "Europe" for s in europe), europe
    assert all(ib_bot.MARKET_SESSIONS[s][0].startswith("Europe/") for s in europe), europe
    # the two pure entry gates the alarm mirrors
    could = ib_bot._entry_could_follow
    old = ib_bot.entry_blocked_reason
    try:
        ib_bot.entry_blocked_reason = lambda ysym, ccy: None
        assert could("NVDA") and could("SAP.DE") and could("0700.HK")
        assert not could("BP.L"), "London is never bought while pence are unscaled"
        ib_bot.entry_blocked_reason = lambda ysym, ccy: "off" if ccy == "HKD" else None
        assert not could("0700.HK") and could("NVDA")
    finally:
        ib_bot.entry_blocked_reason = old
    # the real gate: Hong Kong follows HK_ENABLED (=1 on both VM trading runs)
    assert could("0700.HK") == ib_bot.HK_ENABLED and could("NVDA")
    print("t8 next run, the 22:40 due time, market names and the entry gates OK")


def t9_a_candidate_the_loop_would_skip_anyway_is_no_missed_buy():
    # The reviewer's probe: BP.L alone at Mon 23:35. On a fresh build the loop
    # places nothing - London is skipped for its unscaled pence - ...
    placed, st, lines, queued, published = bot_run(MON_EVENING, {}, UK_BUY, POST_CLOSE)
    assert placed == [] and queued == [], (placed, queued)
    assert [l for l in lines if "skip BP.L: LSE prices are in pence" in l], lines
    # ... so on a 16:05Z build its deferral is no missed buy: no alarm, and
    # the orders and state are those of the run with the alarm off
    placed, st, lines, queued, published = same_orders_without_alarm(
        MON_EVENING, {}, UK_BUY, "2026-11-16T16:05:00Z")
    assert placed == [] and "BP.L" not in st["pos"], (placed, st["pos"])
    assert [l for l in lines if "skip BP.L: entry deferred to a fresh build" in l], lines
    assert alarms(queued) == [] and queued == [], queued
    # the same for a candidate entry_blocked_reason refuses (HK switched off)
    hk_off = lambda ysym, ccy: "HK entries are switched off" if ccy == "HKD" else None
    placed, st, lines, queued, published = bot_run(
        MON_EVENING, {}, HK_BUY, POST_CLOSE, blocked=hk_off)
    assert placed == [] and [l for l in lines if "skip 0700.HK: HK entries" in l], lines
    placed, st, lines, queued, published = same_orders_without_alarm(
        MON_EVENING, {}, HK_BUY, "2026-11-16T05:05:00Z", blocked=hk_off)
    assert placed == [], placed
    assert [l for l in lines if "skip 0700.HK: entry deferred to a fresh build" in l], lines
    assert queued == [], queued
    # ... while HK switched ON does alarm (its Monday close is missed by 23:35)
    placed, st, lines, queued, published = same_orders_without_alarm(
        MON_EVENING, {}, HK_BUY, "2026-11-16T05:05:00Z")
    assert len(alarms(queued)) == 1 == len(queued), queued
    assert queued[0].startswith("🚨 Hong Kong close of Mon 16 Nov is being MISSED"), queued
    assert "so buys signalled on this close are skipped: 1 buy candidate(s) waiting " \
        "(0700.HK)." in queued[0], queued[0]
    # a London HOLDING is another matter: its exits stay open, so a pre-close
    # build that defers them is alarmed (and nothing is sold either way)
    placed, st, lines, queued, published = same_orders_without_alarm(
        MON_EVENING, UK_HELD, UK_BUY, "2026-11-16T16:05:00Z")
    assert placed == [] and st["pos"]["BP.L"] == SEED_POS["BP.L"], (placed, st["pos"])
    assert len(alarms(queued)) == 1 == len(queued), queued
    assert queued[0].startswith("🚨 Europe close of Mon 16 Nov is being MISSED"), queued
    assert "these holdings are next checked on a later close: 1 holding(s) not " \
        "checked for exits (BP.L)." in queued[0], queued[0]
    assert "candidate" not in queued[0] and "buys signalled" not in queued[0], queued[0]
    placed, st, lines, queued, published = bot_run(MON_EVENING, UK_HELD, [], POST_CLOSE)
    assert ("SELL", 200, "BP") in placed and queued == [], (placed, queued)
    print("t9 London and blocked candidates raise no alarm; a London holding does OK")


def t10_europe_is_one_market():
    # The reviewer's second probe: Mon 23:35 on a 16:05Z build with SAP.DE,
    # MC.PA, ASML.AS, ENI.MI and EQNR.OL (Oslo settles 16:50 UTC, the rest
    # 18:00), NVDA, 6758.T and 0700.HK. Tokyo and Hong Kong settled before
    # 16:05 and are decided; the US gets its plain alert; Europe ONE louder one.
    box = _TMP / "europe-outbox"
    acts = EU_BUY + US_BUY + JP_BUY + HK_BUY
    placed, st, lines, queued, published = same_orders_without_alarm(
        MON_EVENING, {}, acts, "2026-11-16T16:05:00Z", outbox=box)
    assert sorted(p[2] for p in placed) == ["0700", "6758"], placed
    for s in ("SAP.DE", "MC.PA", "ASML.AS", "ENI.MI", "EQNR.OL", "NVDA"):
        assert [l for l in lines if "skip " + s + ": entry deferred to a fresh build" in l], s
    assert len(alarms(queued)) == 2 == len(queued), queued
    eu = [q for q in queued if "Europe" in q]
    us = [q for q in queued if "After-close update missing" in q]
    assert len(eu) == 1 and len(us) == 1, queued
    q = eu[0]
    assert q.startswith("🚨 Europe close of Mon 16 Nov is being MISSED: no signals "
                        "build started after it settled (16:50-18:00 UTC) - the "
                        "newest started Mon 16 Nov 16:05 UTC."), q
    assert "5 buy candidate(s) waiting (" in q, q
    for s in ("SAP.DE", "MC.PA", "ASML.AS", "ENI.MI", "EQNR.OL"):
        assert s in q, (s, q)
    assert "(.DE)" not in q and "NVDA" not in q, q
    assert "before the US close of Mon 16 Nov settled (22:30 UTC)" in us[0], us[0]
    assert "1 buy candidate(s) waiting (NVDA)" in us[0], us[0]
    # a rerun the same evening adds nothing
    placed, st, lines, queued, published = bot_run(
        at(2026, 11, 16, 23, 50), {}, acts, "2026-11-16T16:05:00Z", outbox=box)
    assert len(alarms(queued)) == 2, queued
    # two venues alone, same settle: one alert, one settle time
    placed, st, lines, queued, published = bot_run(
        MON_EVENING, {}, EU_BUY[:2], "2026-11-16T16:05:00Z")
    assert len(queued) == 1 and "settled (18:00 UTC)" in queued[0], queued
    assert "2 buy candidate(s) waiting (" in queued[0], queued[0]
    print("t10 five European venues behind one evening give ONE Europe alert OK")


if __name__ == "__main__":
    t1_pre_close_build_at_2335_one_plain_alert_for_a_holding()
    t2_a_buy_candidate_alone_and_both_in_one_alert()
    t3_fresh_build_no_alert_and_the_decisions_are_made()
    t4_catchup_still_behind_the_louder_one_once()
    t5_routine_waits_stay_quiet()
    t6_us_summer_and_the_friday_close()
    t7_a_broken_alarm_costs_the_alarm_not_the_run_and_dry_queues_nothing()
    t8_helpers()
    t9_a_candidate_the_loop_would_skip_anyway_is_no_missed_buy()
    t10_europe_is_one_market()
    print("ALL POST-CLOSE ALARM TESTS PASS")
