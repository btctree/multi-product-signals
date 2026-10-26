#!/usr/bin/env python3
"""Golden tests for entry sizing since 2026-10-26: free cash / free slots.

Run from this directory:  python test_sizing.py

The owner's decisions (2026-10-26) this locks down, through the REAL run(),
cash_by_ccy, reserve_working_cash, _free_cash_base, _fundable_base, ensure_ccy
and place() - only the broker, the rates and the clock are stubbed:

  * an entry is sized at FREE CASH / FREE SLOTS (15 slots kept), worked out
    ONCE per run, after the exits, the working-order reservations and the
    earmark freeze. Free cash is every balance net of what working orders and
    this run's entries claim, valued at fx_rate - but not GBP (the owner's
    month-end pass-through), not HKD (the operator's pot; only the bot's own
    HKD counts, and only when its pocket is known), and never a balance with no
    rate at 1.0. The golden is the live book on 2026-10-26: HK$19,588 a slot
    at 4 free slots, VLO's working buy netted out;
  * no free slot means no division, and the exits still run; a free-cash read
    error, or a debt in a currency with no rate, means no entries this run -
    and only that: state is saved and published;
  * free -= 1 runs for a REJECTED order too, and the size is not recomputed,
    so a refusal never pours its cash into the next name;
  * each order is clamped to what ONE funding pass can pay (own cash plus the
    single largest other balance): the last slots no longer fail funding on
    every run;
  * the per-order cap is HK$50,000 (code default) and binds at one free slot;
  * the order value guard: re-priced at IB's quote, skipped more than 10% away,
    sized and funded at the higher price, and place() limits on that same quote;
  * crypto (-USD, market CRYPTO) is refused before to_ib/qualify: no slot, no
    Web API call, no state write;
  * entries are tried by (tier, -score): an E row (tier 0) before a D3 row.

Nothing here touches /root: every path is repointed before ib_bot is imported.
"""
import io
import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

os.environ.setdefault("IB_BACKEND", "web")
import testenv                                     # noqa: E402
_TMP = testenv.isolate("mps-sizing-")
os.environ.pop("EXCLUDED_CASH", None)
# The code defaults are what is under test: a shell exporting one of these
# must not change what this suite measures.
for _v in ("MAX_ORDER_BASE", "TARGET_POSITIONS", "DAILY_LOSS_KILL", "LIMIT_BUFFER",
           "FX_CONVERT", "FX_FUND_NONBASE", "BASE_FUND_BUFFER", "HK_ENABLED"):
    os.environ.pop(_v, None)

import alerts                                      # noqa: E402
import broker                                      # noqa: E402
import ib_bot                                      # noqa: E402
import ib_web                                      # noqa: E402
testenv.assert_isolated()

BASE = ib_bot.BASE_CCY
HERE = Path(__file__).resolve().parent
# Wed 2026-09-16 23:35 UTC, the evening run: every market's bar is final.
RUN_AT = datetime(2026, 9, 16, 23, 35, 20, tzinfo=timezone.utc)
TODAY = date.today().isoformat()
# HKD per unit, the rates of the 2026-10-26 first-run estimate
HKD_PER = {"HKD": 1.0, "USD": 7.8413, "JPY": 0.04974, "EUR": 8.7932, "GBP": 10.5}


def rate(ib_, a, b):
    if a == b:
        return 1.0
    if a in HKD_PER and b in HKD_PER:
        return HKD_PER[a] / HKD_PER[b]
    return 0.0


class Patch:
    def __init__(self, mod, **kw):
        self.mod, self.kw, self.old = mod, kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(self.mod, k)
            setattr(self.mod, k, v)
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(self.mod, k, v)
        for reg in (ib_bot._FX_COMMITTED, ib_bot._FX_PENDING_CCY, ib_bot._FX_PENDING,
                    ib_bot._EARMARK_RUN, ib_bot._EXC_APPLIED, ib_bot._POCKET_RUN,
                    ib_bot._RESERVE_OK, ib_bot._RUN_CONFIG, ib_bot._TICK_CACHE):
            reg.clear()
        del ib_bot.PLACED[:]


def contract(sym, ccy="USD", sec="STK"):
    c = broker.Contract(sym, sec, ccy)
    c.conId = abs(hash((sym, ccy, sec))) % 10 ** 7 + 1
    return c


class Working:
    """One openTrades row: an order IB has accepted and not yet filled."""

    def __init__(self, sym, ccy, action, qty, lmt, status="PreSubmitted"):
        self.contract = contract(sym, ccy)
        self.order = broker.Order(action, qty, "LMT", lmt)
        self.orderStatus = broker._OrderStatus(status)
        self.log = []


class BookIB:
    """Real-shaped account values, positions and working orders behind the REAL
    cash_by_ccy, held_positions, reserve_working_cash and place(). quotes: IB's
    last price per IB symbol (none = no quote, the signal price stands);
    refuse: IB symbols whose BUY IB refuses."""

    def __init__(self, cash, held=(), working=(), netliq=222002.0, quotes=None,
                 refuse=()):
        self.cash, self.held, self.working = dict(cash), list(held), list(working)
        self.netliq, self.quotes, self.refuse = netliq, dict(quotes or {}), set(refuse)
        self.placed, self.qualified, self.tickers = [], [], []
        self.disconnected = False

    def accountValues(self):
        out = [broker.AccountValue("NetLiquidation", str(self.netliq), BASE)]
        return out + [broker.AccountValue("CashBalance", str(v), c)
                      for c, v in self.cash.items()]

    def positions(self):
        return [broker.Position(contract(s, ccy), q, avg) for s, ccy, q, avg in self.held]

    def reqAllOpenOrders(self):
        pass

    def openTrades(self):
        return list(self.working)

    def sleep(self, *a):
        pass

    def qualifyContracts(self, *cs):
        self.qualified.extend(c.symbol for c in cs)
        return list(cs)

    def reqTickers(self, c):
        self.tickers.append(c.symbol)
        return [broker.Ticker(last=self.quotes.get(c.symbol))]

    def placeOrder(self, c, order):
        self.placed.append((order.action, order.totalQuantity, c.symbol, order.lmtPrice))
        t = broker.Trade(c, order)
        if c.symbol in self.refuse:
            t.orderStatus = broker._OrderStatus("Inactive")
            t.log.append(broker._LogEntry("order refused by IB"))
        else:
            t.orderStatus = broker._OrderStatus("Submitted")
        return t

    def disconnect(self):
        self.disconnected = True


def buy(sym, price, score, market="US", action="BUY", **kw):
    row = {"symbol": sym, "action": action, "price": price, "score": score,
           "stop": round(price * 0.88, 2), "market": market}
    row.update(kw)
    return row


LAST_ROWS = []                                     # the last live run's PLACED rows


def bot_run(ib, actions, state=None, cards=None, dry=False, **extra):
    """One ib_bot.run(). Returns (exception or None, state on disk, log lines,
    published states)."""
    d = Path(tempfile.mkdtemp(prefix="run-", dir=str(_TMP)))
    path = d / "state.json"
    path.write_text(json.dumps(state or {"map": {}, "pos": {}}), encoding="utf-8")
    alerts.DIR = d / "outbox"
    signals = {"generated": "2026-09-16", "actions": list(actions)}
    cards = cards or {}

    def get_json(url):
        if url == ib_bot.SIGNALS_URL:
            return signals
        for ysym, card in cards.items():
            if url.endswith("/" + ib_bot.safe_name(ysym) + ".json"):
                return {"card": card}
        raise AssertionError("unexpected fetch " + url)

    lines, published = [], []

    def publish(ib_, st, nl):
        published.append(json.loads(json.dumps(st)))
        LAST_ROWS[:] = [dict(r) for r in ib_bot.PLACED]     # the History rows sent

    stubs = dict(
        STATE=path, IB=lambda: ib, connect_or_heal=lambda *a, **k: None,
        get_json=get_json, _now_utc=lambda: RUN_AT,
        publish_state=publish,
        fx_rate=rate, lot_size=lambda ib_, c: 100 if c.currency == "JPY" else 1,
        min_tick=lambda ib_, c: 0.01, confirm=lambda msg: True,
        EXIT_ATTEMPTS=d / "exit_attempts.json", FILLS_LEDGER=d / "fills.jsonl",
        ADOPT=d / "adopt.json",            # none: the repo's real one adopts MSFT
        log=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    stubs.update(extra)
    err = None
    with Patch(ib_bot, **stubs):
        try:
            ib_bot.run(dry=dry)
        except BaseException as e:
            err = e
    return err, json.loads(path.read_text(encoding="utf-8")), lines, published


def sizing_line(lines):
    got = [l for l in lines if l.startswith("sizing: ")]
    assert len(got) <= 1, got
    return got[0] if got else None


# The live book, 2026-10-26: ten US holdings, VLO's BUY 4 @ 421.38 working,
# USD 8,066, JPY 462,392, EUR 605 - plus the HKD 21 that is the operator's pot
# and a month-end GBP deposit not yet converted, neither of which may size a slot.
HOLDINGS = [(s, "USD", q, 100.0) for s, q in (
    ("CRWD", 4), ("HPQ", 60), ("PANW", 10), ("ZBRA", 5), ("MPC", 10), ("LLY", 1),
    ("MSFT", 2), ("PSX", 12), ("CRM", 7), ("URI", 1))]
LIVE_CASH = {"USD": 8066.0, "JPY": 462392.0, "EUR": 605.0, "HKD": 21.0, "GBP": 2160.0}
VLO = Working("VLO", "USD", "BUY", 4, 421.38)


def t1_first_run_golden_19588_per_slot_at_four_free():
    ib = BookIB(LIVE_CASH, held=HOLDINGS, working=[VLO])
    actions = [buy("VLO", 419.28, 100),                    # its own order is working
               buy("ETH-USD", 2352.52, 99, market="CRYPTO", action="BUY/HOLD"),
               buy("NEWA", 250.0, 90),
               buy("7272.T", 1767.0, 80, market="JP")]
    err, st, lines, published = bot_run(ib, actions)
    assert err is None, repr(err)
    line = sizing_line(lines)
    # (8,066 - 4 x 421.38) x 7.8413 + 462,392 x 0.04974 + 605 x 8.7932 = 78,351
    assert line == ("sizing: free cash 78,351 HKD (EUR 5,320, JPY 22,999, USD 50,031) "
                    "/ 4 free slot(s) = 19,588 per slot, cap 50,000"), line
    assert "GBP" not in line and "HKD 21" not in line and "own" not in line, line
    assert any("working orders reserve: USD 1,686" in l for l in lines), lines
    # 19,588 / 7.8413 / 250 = 9.99 -> 9; 19,588 / 0.04974 / 1,767 = 222.9 -> 2 lots
    assert [(a, q, s) for a, q, s, _ in ib.placed] == [("BUY", 9, "NEWA"),
                                                      ("BUY", 200, "7272")], ib.placed
    # crypto: refused before to_ib/qualify, no slot, no state
    assert "ETH" not in ib.qualified and "ETH-USD" not in st["pos"], (ib.qualified, st)
    assert any("skip ETH-USD: crypto is not traded" in l for l in lines), lines
    assert set(st["map"]) == {"NEWA", "7272"}, st["map"]
    assert st["pos"]["NEWA"]["entry"] == 250.0 and st["pos"]["NEWA"]["stop"] == 220.0
    assert len(published) == 1
    # the same number straight from the helper, with VLO reserved
    with Patch(ib_bot, fx_rate=rate):
        ib_bot._FX_COMMITTED["USD"] = 4 * 421.38
        fc, parts = ib_bot._free_cash_base(BookIB(LIVE_CASH))
    assert abs(fc - 78350.52) < 0.01 and abs(fc / 4 - 19587.63) < 0.01, fc
    print("t1 first run on the live book: HK$19,588 a slot at 4 free; GBP, the HKD "
          "pot and crypto left out OK")


def t2_no_free_slot_no_division_and_the_exits_still_run():
    held = HOLDINGS + [(s, "USD", 1, 50.0) for s in ("AA", "BB", "CC", "DD")]   # 14 held
    ib = BookIB(LIVE_CASH, held=held, working=[VLO])                             # +VLO = 15
    state = {"map": {"CRWD": "CRWD"},
             "pos": {"CRWD": {"entry": 300, "hw": 330, "stop": 290, "entry_date": TODAY}}}
    cards = {"CRWD": {"price": 250.0, "sma200": 280.0, "atr": 10.0}}   # regime break

    def never(ib_):
        raise AssertionError("free cash read with no free slot")
    err, st, lines, published = bot_run(ib, [buy("NEWA", 250.0, 90)], state=state,
                                        cards=cards, _free_cash_base=never)
    assert err is None, repr(err)
    assert [(a, q, s) for a, q, s, _ in ib.placed] == [("SELL", 4, "CRWD")], ib.placed
    assert sizing_line(lines) is None, lines
    assert len(published) == 1 and st["map"] == {"CRWD": "CRWD"}
    print("t2 no free slot: no division, no cash read, the exit still runs OK")


def t3_a_free_cash_read_error_places_no_entries_but_saves_and_publishes():
    state = {"map": {"CRWD": "CRWD"},
             "pos": {"CRWD": {"entry": 300, "hw": 330, "stop": 290, "entry_date": TODAY}}}
    cards = {"CRWD": {"price": 250.0, "sma200": 280.0, "atr": 10.0}}

    def boom(ib_):
        raise ib_web.IbWebError("GET portfolio/U1/ledger failed: 500 Internal Server Error")
    ib = BookIB(LIVE_CASH, held=HOLDINGS, working=[VLO])
    err, st, lines, published = bot_run(ib, [buy("NEWA", 250.0, 90)], state=state,
                                        cards=cards, _free_cash_base=boom)
    assert err is None, repr(err)
    assert [(a, s) for a, q, s, _ in ib.placed] == [("SELL", "CRWD")], ib.placed
    assert any("free cash unreadable" in l and "500" in l and "NO entries" in l
               for l in lines), lines
    assert len(published) == 1, "the run must still publish"
    assert st["pos"]["CRWD"]["hw"] == 330, "the exit loop's state must still be saved"

    # a DEBT in a currency with no rate cannot be left out: no entries either
    cash = dict(LIVE_CASH, CHF=-500.0)
    ib = BookIB(cash, held=HOLDINGS, working=[VLO])
    err, st, lines, published = bot_run(ib, [buy("NEWA", 250.0, 90)])
    assert err is None and ib.placed == [], (err, ib.placed)
    assert any("free cash unreadable" in l and "CHF" in l and "owed" in l for l in lines), lines
    assert len(published) == 1
    # ...while a POSITIVE balance with no rate is left out, never valued at 1.0
    cash = dict(LIVE_CASH, CHF=5000.0)
    ib = BookIB(cash, held=HOLDINGS, working=[VLO])
    err, st, lines, published = bot_run(ib, [buy("NEWA", 250.0, 90)])
    assert err is None and [q for a, q, s, _ in ib.placed] == [9], ib.placed
    assert "= 19,588 per slot" in sizing_line(lines), sizing_line(lines)
    assert any("CHF 5,000 left out - no CHF/HKD rate" in l for l in lines), lines
    print("t3 a free-cash read error or an unvalued debt: no entries, state saved "
          "and published OK")


def t4_a_rejected_order_does_not_inflate_the_next():
    # 3 free slots, HK$60,000 free (USD 7,651.8) -> HK$20,000 each. IB refuses
    # AAA: its slot is spent (free -= 1) and BBB and CCC keep HK$20,000 each -
    # re-dividing after the refusal would have given BBB HK$30,000.
    usd = 60000.0 / HKD_PER["USD"]
    held = [(s, "USD", 1, 10.0) for s in ("H%02d" % k for k in range(12))]
    ib = BookIB({"USD": usd}, held=held, refuse={"AAA"})
    actions = [buy("AAA", 100.0, 90), buy("BBB", 100.0, 80), buy("CCC", 100.0, 70)]
    err, st, lines, published = bot_run(ib, actions)
    assert err is None, repr(err)
    assert "/ 3 free slot(s) = 20,000 per slot" in sizing_line(lines), sizing_line(lines)
    per = int(20000.0 / HKD_PER["USD"] / 100.0)                         # 25
    assert [(s, q) for a, q, s, _ in ib.placed] == [("AAA", per), ("BBB", per),
                                                    ("CCC", per)], ib.placed
    assert {"BBB", "CCC"} <= set(st["map"]), st["map"]
    # a fourth candidate finds no slot: the refusal spent AAA's
    ib = BookIB({"USD": usd}, held=held, refuse={"AAA"})
    err, st, lines, published = bot_run(ib, actions + [buy("DDD", 100.0, 60)])
    assert [s for a, q, s, _ in ib.placed] == ["AAA", "BBB", "CCC"], ib.placed
    print("t4 a REJECTED order spends its slot and no cash moves to the next name OK")


def t5_each_order_is_clamped_to_one_funding_pass():
    # One free slot, free cash HK$31,800 split three ways: USD 1,000 (HK$7,800),
    # JPY 300,000 (HK$15,000) and EUR 1,000 (HK$9,000). No single conversion
    # pays a HK$31,800 USD order, and fund_from_nonbase never pools two - so
    # unclamped it would fail funding on every run. Clamped to own USD + JPY/1.02.
    global HKD_PER
    old = HKD_PER
    HKD_PER = {"HKD": 1.0, "USD": 7.8, "JPY": 0.05, "EUR": 9.0, "GBP": 10.5}
    try:
        held = [(s, "USD", 1, 10.0) for s in ("H%02d" % k for k in range(14))]
        ib = BookIB({"USD": 1000.0, "JPY": 300000.0, "EUR": 1000.0}, held=held)
        funded = []

        def fx_pair(ib_, src, dst, qty_src, qty_dst, dry, **kw):
            funded.append((src, dst, round(qty_src), round(qty_dst)))
            return True                                   # the conversion filled
        err, st, lines, published = bot_run(ib, [buy("NEWA", 100.0, 90)],
                                            _fx_order_pair=fx_pair)
    finally:
        HKD_PER = old
    assert err is None, repr(err)
    assert "free cash 31,800 HKD" in sizing_line(lines), sizing_line(lines)
    # fundable = 7,800 + 15,000 / (1.02 x 1.0002) = 22,503 -> notional 22,391
    assert any("one funding pass can pay ~22,503 HKD - order cut from 31,800" in l
               for l in lines), lines
    assert [(s, q) for a, q, s, _ in ib.placed] == [("NEWA", 28)], ib.placed
    # 28 x USD 100 = 2,800, 1,000 held: 1,800 short, bought with JPY at 156 x 1.02
    assert funded == [("JPY", "USD", 286416, 1836)], funded
    assert funded[0][2] <= 300000, "the clamp must keep the conversion payable"
    print("t5 an order is clamped to what one conversion can pay, and is funded OK")


def t6_the_hk50k_cap_binds_at_one_free_slot():
    src = io.open(HERE / "ib_bot.py", encoding="utf-8").read()
    assert 'MAX_ORDER_BASE = float(os.environ.get("MAX_ORDER_BASE", "50000"))' in src
    assert ib_bot.MAX_ORDER_BASE == 50000.0
    ds = io.open(HERE / "daily_signal.py", encoding="utf-8").read()
    assert 'MAX_ORDER_BASE = float(os.environ.get("MAX_ORDER_BASE", "50000"))' in ds
    # one free slot and HK$120,000 of USD: the slot would be 120k, the cap is 50k
    usd = 120000.0 / HKD_PER["USD"]
    held = [(s, "USD", 1, 10.0) for s in ("H%02d" % k for k in range(14))]
    ib = BookIB({"USD": usd}, held=held)
    err, st, lines, published = bot_run(ib, [buy("NEWA", 100.0, 90)])
    assert err is None, repr(err)
    assert "= 120,000 per slot, cap 50,000" in sizing_line(lines), sizing_line(lines)
    shares = int(50000.0 / HKD_PER["USD"] / 100.0)                      # 63
    assert [(s, q) for a, q, s, _ in ib.placed] == [("NEWA", shares)], ib.placed
    lim = ib.placed[0][3]
    assert shares * lim * HKD_PER["USD"] <= 50000.0 * 1.01, (shares, lim)
    print("t6 the HK$50,000 code-default cap binds at one free slot OK")


def t7_the_order_value_guard():
    held = [(s, "USD", 1, 10.0) for s in ("H%02d" % k for k in range(12))]
    usd = 60000.0 / HKD_PER["USD"]                       # 3 free: HK$20,000 a slot
    # IB's quote 12% above the signal: skipped without a slot; a bad price 10x
    # too LOW in the signal is the same case seen from the other side
    ib = BookIB({"USD": usd}, held=held, quotes={"AAA": 112.0, "BAD": 1000.0,
                                                 "BBB": 105.0})
    actions = [buy("AAA", 100.0, 90), buy("BAD", 100.0, 85), buy("BBB", 100.0, 80),
               buy("CCC", 100.0, 70)]
    err, st, lines, published = bot_run(ib, actions)
    assert err is None, repr(err)
    assert any("skip AAA: IB's quote 112 is +12.0% from the signal price 100" in l
               for l in lines), lines
    assert any("skip BAD: IB's quote 1000 is +900.0%" in l for l in lines), lines
    got = {s: (q, lim) for a, q, s, lim in ib.placed}
    assert set(got) == {"BBB", "CCC"}, ib.placed            # both kept their slots
    # BBB: sized at the HIGHER price (105), limited at 105 x 1.005 from the same
    # quote - place() was handed it and did not ask IB again
    assert got["BBB"] == (int(20000.0 / HKD_PER["USD"] / 105.0),
                          ib_bot.snap_to_tick(105.0 * 1.005, 0.01)), got["BBB"]
    assert ib.tickers.count("BBB") == 1, ib.tickers
    # no quote at all: the signal price stands
    assert got["CCC"] == (int(20000.0 / HKD_PER["USD"] / 100.0), 100.5), got["CCC"]
    # reserved at the higher price too, so nothing after it can spend that USD
    left = usd - got["BBB"][0] * 105.0 * 1.005 - got["CCC"][0] * 100.0 * 1.005
    assert any("park (SHADOW" in l and "spendable USD %s after entries" % format(left, ",.0f")
               in l for l in lines), [l for l in lines if "park" in l]
    print("t7 the value guard: >10% skipped, sized and limited at the higher quote OK")


def t8_crypto_is_refused_before_any_ib_call():
    held = [(s, "USD", 1, 10.0) for s in ("H%02d" % k for k in range(14))]
    ib = BookIB({"USD": 3000.0}, held=held)                 # ONE free slot
    actions = [buy("ETH-USD", 2352.52, 99, market="CRYPTO", action="BUY/HOLD"),
               buy("BTC-USD", 80554.0, 98, market="CRYPTO", action="BUY/HOLD"),
               buy("SOL", 150.0, 97, market="CRYPTO"),    # no suffix: the market says so
               buy("NEWA", 100.0, 50)]
    err, st, lines, published = bot_run(ib, actions)
    assert err is None, repr(err)
    for sym in ("ETH-USD", "BTC-USD", "SOL"):
        assert any("skip %s: crypto is not traded by this bot" % sym in l for l in lines), sym
    assert ib.qualified == ["NEWA"], "crypto reached qualifyContracts: %s" % ib.qualified
    assert [s for a, q, s, _ in ib.placed] == ["NEWA"], "crypto used the only slot"
    assert set(st["map"]) == {"NEWA"} and set(st["pos"]) == {"NEWA"}, st
    # and a contract that QUALIFIES as crypto is refused too (belt and braces)
    ib = BookIB({"USD": 3000.0}, held=held)
    err, st, lines, published = bot_run(
        ib, [buy("WEIRD", 10.0, 90), buy("NEWA", 100.0, 50)],
        to_ib=lambda ysym: contract(ysym, "USD", "CRYPTO" if ysym == "WEIRD" else "STK"))
    assert [s for a, q, s, _ in ib.placed] == ["NEWA"], ib.placed
    assert any("skip WEIRD: qualified as crypto" in l for l in lines), lines
    assert all(ib_bot.is_crypto_entry(a) for a in actions[:3])
    assert not ib_bot.is_crypto_entry(actions[3])
    print("t8 crypto rows are refused before to_ib/qualify: no slot, no state OK")


def t9_entries_are_tried_by_tier_then_score():
    held = [(s, "USD", 1, 10.0) for s in ("H%02d" % k for k in range(14))]
    ib = BookIB({"USD": 3000.0}, held=held)                 # ONE free slot
    d3 = buy("DDD", 100.0, 100, tier=1, signal="D3")
    e = buy("EEE", 100.0, 60, tier=0, signal="E")
    err, st, lines, published = bot_run(ib, [d3, e])
    assert [s for a, q, s, _ in ib.placed] == ["EEE"], ib.placed      # E before D3
    # the signal kind rides in the History row; without one, the old text
    assert [r["reason"] for r in LAST_ROWS] == ["entry signal E, score 60"], LAST_ROWS
    ib = BookIB({"USD": 3000.0}, held=held)
    bot_run(ib, [buy("PPP", 100.0, 70)])
    assert [r["reason"] for r in LAST_ROWS] == ["entry signal, score 70"], LAST_ROWS
    rows = [buy("A", 1, 50), buy("B", 1, 90, tier=1), buy("C", 1, 70),
            buy("D", 1, 95, tier="1"), buy("E", 1, None), buy("F", 1, "x", tier="junk")]
    order = [r["symbol"] for r in sorted(rows, key=ib_bot.entry_rank)]
    assert order == ["C", "A", "E", "F", "D", "B"], order
    # no tier field at all: exactly the old score-only order
    plain = [buy("P", 1, 10), buy("Q", 1, 30), buy("R", 1, 20)]
    assert ([r["symbol"] for r in sorted(plain, key=ib_bot.entry_rank)]
            == [r["symbol"] for r in sorted(plain, key=lambda x: -(x.get("score") or 0))])
    print("t9 entries are tried by tier, then score: E before D3 OK")


def t10_sizing_runs_after_the_exits_and_the_reservations():
    # The order IS part of the fix: a per-slot size worked out before the exit
    # loop or before `free` exists would raise ahead of every exit (NameError,
    # or ZeroDivisionError with the book full).
    src = io.open(HERE / "ib_bot.py", encoding="utf-8").read()
    body = src[src.index("def run(dry=False):"):]
    exits = body.index("# ---- EXITS first")
    reserve = body.index("reserve_working_cash(ib)\n")
    freeze = body.index('_EARMARK_RUN["base"] =')
    free = body.index("free = (TARGET_POSITIONS")
    sizing = body.index("free_cash, parts = _free_cash_base(ib)")
    loop = body.index("for a in sorted(actions, key=entry_rank):")
    assert reserve < freeze < exits < free < sizing < loop, (reserve, freeze, exits,
                                                              free, sizing, loop)
    assert "if free > 0:" in body[free:sizing], "sizing must be guarded by free > 0"
    assert "per_pos" not in src, "the NetLiq/15 budget is gone"
    assert body.count("_free_cash_base(ib)") == 1, "free cash is read ONCE per run"
    print("t10 sizing runs once, after the exits, reservations and freeze OK")


if __name__ == "__main__":
    t1_first_run_golden_19588_per_slot_at_four_free()
    t2_no_free_slot_no_division_and_the_exits_still_run()
    t3_a_free_cash_read_error_places_no_entries_but_saves_and_publishes()
    t4_a_rejected_order_does_not_inflate_the_next()
    t5_each_order_is_clamped_to_one_funding_pass()
    t6_the_hk50k_cap_binds_at_one_free_slot()
    t7_the_order_value_guard()
    t8_crypto_is_refused_before_any_ib_call()
    t9_entries_are_tried_by_tier_then_score()
    t10_sizing_runs_after_the_exits_and_the_reservations()
    print("ALL SIZING TESTS PASS")
