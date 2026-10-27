#!/usr/bin/env python3
"""Golden tests: the per-country market filter runs in SHADOW mode only.

Run from this directory:  python test_market_filter.py

The owner's request (2026-10-26): "test the country filter only, starting in
log-only mode". The rule as backtested - no new buy in a market while its home
index closes below its 100-day average, on the last finished bar; exits never
gated - is published by the engine as data.json "market_trend", and ib_bot
reads it with MARKET_FILTER = "shadow". Locked down here, through the REAL
run(), place(), ensure_ccy and publish_state - only the broker, the rates and
the clock are stubbed:

  * SHADOW NEVER CHANGES AN ORDER: on a book where JP and EU are below, the
    orders sent, their sizes, the state map/pos, the slots and the exits are
    identical with MARKET_FILTER "off" and "shadow"; only the log, a "shadow"
    tag on the History rows of BUYs in a below market and the rolling record
    differ;
  * the log line, the tag and the would-have-skipped rows (last 50, kept in
    state.json across runs) and the bot_state.json "market_filter" block,
    which publish_only and publish_web.py carry over like "config";
  * a missing or malformed market_trend is "unknown": same orders, no tag, no
    exception - and it can never cost a run its exits;
  * "off" logs and tags nothing; any other value ("live" included) is NOT
    built and runs as shadow; --dry logs but records nothing;
  * markets map by the row's own "market" (the engine's market_of, as the
    backtest keyed it), crypto never, the suffix only without one.

Nothing here touches /root: every path is repointed before ib_bot is imported.
"""
import json
import os
import subprocess
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

os.environ.setdefault("IB_BACKEND", "web")
import testenv                                     # noqa: E402
_TMP = testenv.isolate("mps-mfilter-")
os.environ.pop("EXCLUDED_CASH", None)
for _v in ("MAX_ORDER_BASE", "TARGET_POSITIONS", "DAILY_LOSS_KILL", "LIMIT_BUFFER",
           "FX_CONVERT", "FX_FUND_NONBASE", "BASE_FUND_BUFFER", "HK_ENABLED"):
    os.environ.pop(_v, None)

import alerts                                      # noqa: E402
import broker                                      # noqa: E402
import ib_bot                                      # noqa: E402
import ib_web                                      # noqa: E402
testenv.assert_isolated()

BASE = ib_bot.BASE_CCY
# Wed 2026-09-16 23:35 UTC, the evening run: every market's bar is final.
RUN_AT = datetime(2026, 9, 16, 23, 35, 20, tzinfo=timezone.utc)
TODAY = date.today().isoformat()
HKD_PER = {"HKD": 1.0, "USD": 7.8413, "JPY": 0.04974, "EUR": 8.7932, "GBP": 10.5,
           "CHF": 9.7}
TAG = "market filter would have skipped"


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
                    ib_bot._RESERVE_OK, ib_bot._RUN_CONFIG, ib_bot._TICK_CACHE,
                    ib_bot._RUN_MARKET_FILTER):
            reg.clear()
        del ib_bot._FILTER_SKIPPED_RUN[:]
        del ib_bot.PLACED[:]


def contract(sym, ccy="USD", sec="STK"):
    c = broker.Contract(sym, sec, ccy)
    c.conId = abs(hash((sym, ccy, sec))) % 10 ** 7 + 1
    return c


class BookIB:
    """Account values, positions and orders behind the REAL cash_by_ccy,
    held_positions, reserve_working_cash and place(). refuse: IB symbols whose
    BUY IB refuses; boom: IB symbols whose qualify raises (a dying run)."""

    def __init__(self, cash, held=(), netliq=222002.0, refuse=(), boom=()):
        self.cash, self.held = dict(cash), list(held)
        self.netliq, self.refuse, self.boom = netliq, set(refuse), set(boom)
        self.placed = []

    def accountValues(self):
        out = [broker.AccountValue("NetLiquidation", str(self.netliq), BASE)]
        return out + [broker.AccountValue("CashBalance", str(v), c)
                      for c, v in self.cash.items()]

    def positions(self):
        return [broker.Position(contract(s, ccy), q, avg) for s, ccy, q, avg in self.held]

    def reqAllOpenOrders(self):
        pass

    def openTrades(self):
        return []

    def sleep(self, *a):
        pass

    def qualifyContracts(self, *cs):
        for c in cs:
            if c.symbol in self.boom:
                raise ib_web.IbWebError("GET secdef failed: 500 Internal Server Error")
        return list(cs)

    def reqTickers(self, c):
        return [broker.Ticker(last=None)]

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
        pass


def buy(sym, price, score, market="US", **kw):
    row = {"symbol": sym, "action": "BUY", "price": price, "score": score,
           "stop": round(price * 0.88, 2), "market": market}
    row.update(kw)
    return row


def trend_row(index, close, sma, bar="2026-09-16"):
    return {"index": index, "name": index.strip("^"), "close": close, "sma100": sma,
            "pct_vs_sma100": round((close / sma - 1) * 100, 2), "below": close < sma,
            "bar": bar}


# US and HK above, JP and EU below - the shape of the first live run, when the
# S&P sat +0.26% over its average and the other three were 3.7-6.6% under.
TREND = {"US": trend_row("^GSPC", 7623.68, 7603.69),
         "HK": trend_row("^HSI", 26000.0, 25000.0),
         "JP": trend_row("^N225", 62693.16, 66669.08),
         "EU": trend_row("^STOXX50E", 6068.22, 6303.68)}
# One E candidate per market, plus crypto (blocked before any IB call) and an
# HK name (HK entries are off) - neither may ever be logged by the filter.
CANDS = [buy("NEWA", 250.0, 95), buy("7203.T", 2900.0, 90, market="JP"),
         buy("SAP.DE", 200.0, 85, market="EU"), buy("NOVN.SW", 80.0, 84, market="EU"),
         buy("0700.HK", 500.0, 83, market="HK"),
         buy("ETH-USD", 2352.0, 99, market="CRYPTO"), buy("NEWB", 120.0, 70)]
CASH = {"USD": 20000.0, "JPY": 2000000.0, "EUR": 9000.0, "CHF": 6000.0}
HELD = [("H%02d" % k, "USD", 1, 10.0) for k in range(8)]


def bot_run(ib, actions, signals_extra=None, state=None, cards=None, dry=False,
            mode="shadow", **extra):
    """One ib_bot.run() with MARKET_FILTER = mode. Returns (exception or None,
    state on disk, log lines, PLACED rows as published, publish count)."""
    d = Path(tempfile.mkdtemp(prefix="run-", dir=str(_TMP)))
    path = d / "state.json"
    path.write_text(json.dumps(state or {"map": {}, "pos": {}}), encoding="utf-8")
    alerts.DIR = d / "outbox"
    signals = {"generated": "2026-09-16", "generated_at": "2026-09-16T23:05:00Z",
               "actions": list(actions)}
    signals.update(signals_extra or {})
    cards = cards or {}

    def get_json(url):
        if url == ib_bot.SIGNALS_URL:
            return json.loads(json.dumps(signals))
        for ysym, card in cards.items():
            if url.endswith("/" + ib_bot.safe_name(ysym) + ".json"):
                return {"card": card, "generated_at": "2026-09-16T23:05:00Z"}
        raise AssertionError("unexpected fetch " + url)

    lines, rows, published = [], [], []

    def publish(ib_, st, nl):
        published.append(json.loads(json.dumps(st)))
        rows[:] = [dict(r) for r in ib_bot.PLACED]

    stubs = dict(
        STATE=path, IB=lambda: ib, connect_or_heal=lambda *a, **k: None,
        get_json=get_json, _now_utc=lambda: RUN_AT, publish_state=publish,
        fx_rate=rate, lot_size=lambda ib_, c: 100 if c.currency == "JPY" else 1,
        min_tick=lambda ib_, c: 0.01, confirm=lambda msg: True,
        EXIT_ATTEMPTS=d / "exit_attempts.json", FILLS_LEDGER=d / "fills.jsonl",
        ADOPT=d / "adopt.json", MARKET_FILTER=mode,
        log=lambda *a: lines.append(" ".join(str(x) for x in a)),
    )
    stubs.update(extra)
    err = None
    with Patch(ib_bot, **stubs):
        try:
            ib_bot.run(dry=dry)
        except BaseException as e:
            err = e
        if not rows:
            rows[:] = [dict(r) for r in ib_bot.PLACED]
    return err, json.loads(path.read_text(encoding="utf-8")), lines, rows, len(published)


def no_shadow(rows):
    return [{k: v for k, v in r.items() if k not in ("shadow", "time")} for r in rows]


def t1_shadow_never_changes_an_order():
    runs = {}
    for mode in ("off", "shadow"):
        ib = BookIB(CASH, held=HELD)
        runs[mode] = (ib,) + bot_run(ib, CANDS, {"market_trend": TREND}, mode=mode)
    (ib_off, e0, st_off, l_off, r_off, p_off) = runs["off"]
    (ib_sh, e1, st_sh, l_sh, r_sh, p_sh) = runs["shadow"]
    assert e0 is None and e1 is None, (e0, e1)
    # the orders IB received - side, size, symbol, limit - are the same
    assert ib_off.placed == ib_sh.placed, (ib_off.placed, ib_sh.placed)
    sent = [s for a, q, s, _ in ib_sh.placed]
    assert sent == ["NEWA", "7203", "SAP", "NOVN", "NEWB"], sent       # crypto, HK skipped
    # the bot's memory is the same: map, entry, stop, entry date
    assert st_off["map"] == st_sh["map"] and st_off["pos"] == st_sh["pos"], (st_off, st_sh)
    assert p_off == p_sh == 1
    # the History rows differ only by the tag, which is on JP and EU buys only
    assert no_shadow(r_off) == no_shadow(r_sh), (r_off, r_sh)
    assert not any("shadow" in r for r in r_off), r_off
    tagged = {r["symbol"]: r["shadow"] for r in r_sh if "shadow" in r}
    assert tagged == {"7203": TAG, "SAP": TAG, "NOVN": TAG}, tagged
    # the sizing and slot lines, every skip line, are word for word the same
    strip = lambda ls: [l for l in ls if "market filter" not in l]
    assert strip(l_off) == strip(l_sh), (strip(l_off), strip(l_sh))
    assert "_market_filter_skips" not in st_off, st_off
    print("t1 shadow mode sends the same orders, sizes, state and slots as off; it "
          "only tags the JP/EU buys OK")


def t2_the_log_line_the_tag_and_the_rolling_record():
    old = [{"time": "2026-09-%02d 23:35 UTC" % (k % 28 + 1), "symbol": "OLD%02d" % k,
            "market": "JP", "pct": -1.0} for k in range(49)]
    ib = BookIB(CASH, held=HELD)
    err, st, lines, rows, pubs = bot_run(ib, CANDS, {"market_trend": TREND},
                                         state={"map": {}, "pos": {},
                                                "_market_filter_skips": old})
    assert err is None, repr(err)
    want = ("  shadow market filter: would have skipped 7203.T (JP index 62,693.16 is "
            "5.96% below its 100-day average, bar 2026-09-16)")
    assert want in lines, [l for l in lines if "shadow" in l]
    assert any(l.startswith("  shadow market filter: would have skipped SAP.DE (EU index "
                            "6,068.22 is 3.74% below") for l in lines), lines
    assert not any("would have skipped NEWA" in l or "NEWB" in l and "shadow" in l
                   for l in lines), "an above-market buy was logged"
    assert not any("0700.HK" in l and "shadow" in l for l in lines), "a skipped HK name was logged"
    assert any(l.startswith("market filter (SHADOW - log only, every order is sent as "
                            "without it): US above +0.26% (bar 2026-09-16) · HK above "
                            "+4.00% (bar 2026-09-16) · JP below -5.96%") for l in lines), lines
    assert any("shadow market filter: 3 BUY(s) this run would have been skipped - "
               "7203.T (JP), SAP.DE (EU), NOVN.SW (EU); all were sent (log only)" in l
               for l in lines), lines
    # the order follows its own line, the note annotates it
    i = next(k for k, l in enumerate(lines) if l.startswith("BUY") and " 7203 " in l)
    assert "would have skipped 7203.T" in lines[i + 1], lines[i:i + 2]
    # the rolling record: 49 old + 3 new, the last 50 kept, newest last
    kept = st["_market_filter_skips"]
    assert len(kept) == 50 and kept[0]["symbol"] == "OLD02", (len(kept), kept[:2])
    assert [r["symbol"] for r in kept[-3:]] == ["7203.T", "SAP.DE", "NOVN.SW"], kept[-3:]
    assert kept[-3] == {"time": kept[-3]["time"], "symbol": "7203.T", "market": "JP",
                        "pct": -5.96}, kept[-3]
    row7203 = next(r for r in rows if r["symbol"] == "7203")
    assert kept[-3]["time"] == row7203["time"], "the record carries its order's time"
    assert ib_bot._RUN_MARKET_FILTER == {}, "cleared between runs by the harness"
    print("t2 the log line, the History tag and the rolling 50-row record OK")


def t3_malformed_or_missing_trend_is_unknown_and_changes_nothing():
    base_ib = BookIB(CASH, held=HELD)
    bot_run(base_ib, CANDS, {}, mode="off")
    bad = [None, {}, "garbage", ["JP"], {"JP": "below"},
           {"JP": dict(TREND["JP"], below="yes")},                  # not a boolean
           {"JP": dict(TREND["JP"], pct_vs_sma100=float("nan"))},
           {"JP": dict(TREND["JP"], close="x")},
           {"JP": dict(TREND["JP"], below=False)},                  # contradicts its numbers
           {"JP": dict(TREND["JP"], bar="Friday")},
           {"JP": dict(TREND["JP"], sma100=0)}]
    for mt in bad:
        ib = BookIB(CASH, held=HELD)
        extra = {} if mt is None else {"market_trend": mt}
        err, st, lines, rows, pubs = bot_run(ib, CANDS, extra)
        assert err is None, (mt, repr(err))
        assert ib.placed == base_ib.placed, (mt, ib.placed)
        assert not any("shadow" in r for r in rows), (mt, rows)
        assert "_market_filter_skips" not in st, (mt, st)
        assert not any("would have skipped" in l for l in lines), mt
        assert any("JP unknown" in l or "every market unknown" in l for l in lines), (mt, lines)
        assert pubs == 1
    # market_trend_of itself, on things json can carry
    assert ib_bot.market_trend_of(None) == {} and ib_bot.market_trend_of([]) == {}
    assert set(ib_bot.market_trend_of({"market_trend": TREND})) == {"US", "HK", "JP", "EU"}
    assert ib_bot.market_trend_of({"market_trend": {"XX": TREND["JP"]}}) == {}
    # a number too big for a float drops that market only (review 2026-10-26)
    huge = dict(TREND, JP=dict(TREND["JP"], close=10 ** 400))
    assert set(ib_bot.market_trend_of({"market_trend": huge})) == {"US", "HK", "EU"}
    # a row rounded to equal numbers keeps the engine's own verdict
    eq = dict(TREND["JP"], close=100.0, sma100=100.0, below=True, pct_vs_sma100=0.0)
    assert ib_bot.market_trend_of({"market_trend": {"JP": eq}})["JP"]["below"] is True
    print("t3 a missing or malformed market_trend is unknown: same orders, no tag, no "
          "exception OK")


def t4_the_exits_and_the_slots_are_untouched():
    # a JP holding with a regime break: JP is below, and the exit still goes
    held = HELD + [("7974", "JPY", 100, 9000.0)]
    state = {"map": {"7974": "7974.T"},
             "pos": {"7974.T": {"entry": 9000, "hw": 9500, "stop": 8000,
                                "entry_date": TODAY}}}
    cards = {"7974.T": {"price": 8500.0, "sma200": 8800.0, "atr": 100.0}}
    out = {}
    for mode in ("off", "shadow"):
        ib = BookIB(CASH, held=held)
        err, st, lines, rows, pubs = bot_run(ib, CANDS, {"market_trend": TREND},
                                             state=json.loads(json.dumps(state)),
                                             cards=cards, mode=mode)
        assert err is None, repr(err)
        out[mode] = (ib.placed, st["pos"]["7974.T"],
                     [l for l in lines if l.startswith("sizing: ")])
        sell = [r for r in rows if r["action"] == "SELL"]
        assert len(sell) == 1 and "shadow" not in sell[0], sell
    assert out["off"] == out["shadow"], out
    assert out["shadow"][0][0][:3] == ("SELL", 100, "7974"), out["shadow"][0]
    # 15 - 9 held = 6 free slots, the same split of the same cash either way
    assert "/ 6 free slot(s)" in out["shadow"][2][0], out["shadow"][2]
    print("t4 exits, stops and the slot count are the same with the filter in "
          "shadow OK")


def t5_off_logs_and_tags_nothing_and_an_unbuilt_mode_runs_as_shadow():
    ib = BookIB(CASH, held=HELD)
    err, st, lines, rows, pubs = bot_run(ib, CANDS, {"market_trend": TREND}, mode="off")
    assert err is None and not any("shadow" in r for r in rows)
    assert any(l.startswith("market filter: off") for l in lines), lines
    assert not any("would have skipped" in l for l in lines)
    assert any("market filter off," in l for l in lines if l.startswith("rules: ")), lines
    with Patch(ib_bot, MARKET_FILTER="off"):
        assert ib_bot.effective_config()["market_filter"] == "off"
    assert ib_bot.effective_config()["market_filter"] == "shadow"
    # "live" is not built: it must annotate exactly like shadow and gate nothing
    ref = BookIB(CASH, held=HELD)
    bot_run(ref, CANDS, {"market_trend": TREND}, mode="shadow")
    ib = BookIB(CASH, held=HELD)
    err, st, lines, rows, pubs = bot_run(ib, CANDS, {"market_trend": TREND}, mode="live")
    assert err is None and ib.placed == ref.placed, (ib.placed, ref.placed)
    assert any("MARKET_FILTER 'live' is not built" in l for l in lines), lines
    assert {r["symbol"] for r in rows if r.get("shadow")} == {"7203", "SAP", "NOVN"}
    print("t5 off logs and tags nothing; an unbuilt 'live' runs as shadow and gates "
          "nothing OK")


def t6_dry_logs_but_records_nothing():
    ib = BookIB(CASH, held=HELD)
    err, st, lines, rows, pubs = bot_run(ib, CANDS, {"market_trend": TREND},
                                         state={"map": {}, "pos": {}}, dry=True)
    assert err is None and pubs == 0 and ib.placed == [], (err, pubs, ib.placed)
    assert any("would have skipped 7203.T" in l for l in lines), lines
    assert st == {"map": {}, "pos": {}}, st                      # --dry writes nothing
    assert ib_bot._RUN_MARKET_FILTER == {}
    print("t6 --dry logs what the filter would have skipped and records nothing OK")


def t7_a_refused_buy_is_still_tagged_a_declined_one_is_not():
    ib = BookIB(CASH, held=HELD, refuse={"7203"})
    err, st, lines, rows, pubs = bot_run(ib, CANDS, {"market_trend": TREND})
    r = next(x for x in rows if x["symbol"] == "7203")
    assert r["status"] == "REJECTED" and r["shadow"] == TAG, r
    assert [x["symbol"] for x in st["_market_filter_skips"]] == ["7203.T", "SAP.DE", "NOVN.SW"]
    # a BUY declined at the confirm prompt was never sent: nothing to note
    ib = BookIB(CASH, held=HELD)
    err, st, lines, rows, pubs = bot_run(
        ib, CANDS, {"market_trend": TREND},
        confirm=lambda msg: "7203" not in msg)
    assert err is None and "7203" not in [s for a, q, s, _ in ib.placed]
    assert not any("would have skipped 7203.T" in l for l in lines), lines
    assert [x["symbol"] for x in st["_market_filter_skips"]] == ["SAP.DE", "NOVN.SW"]
    # a below-market candidate the bot skips on its own (here: no CHF, and the
    # conversion cannot be made) was never sent either - not logged, not kept
    ib = BookIB(dict(CASH, CHF=0.0), held=HELD)
    err, st, lines, rows, pubs = bot_run(ib, CANDS, {"market_trend": TREND})
    assert err is None and "NOVN" not in [s for a, q, s, _ in ib.placed], ib.placed
    assert any("skip NOVN.SW: CHF funding did not complete" in l for l in lines), lines
    assert not any("would have skipped NOVN.SW" in l for l in lines), lines
    assert [x["symbol"] for x in st["_market_filter_skips"]] == ["7203.T", "SAP.DE"]
    print("t7 a refused BUY was sent, so it is tagged; a declined or unfunded one "
          "is not OK")


def t8_an_aborted_run_keeps_the_tag_and_the_record():
    ib = BookIB(CASH, held=HELD, boom={"NEWB"})
    err, st, lines, rows, pubs = bot_run(ib, CANDS, {"market_trend": TREND})
    assert isinstance(err, ib_web.IbWebError), repr(err)
    assert pubs == 0
    carried = {r["symbol"]: r.get("shadow") for r in st["_unpublished_activity"]}
    assert carried == {"NEWA": None, "7203": TAG, "SAP": TAG, "NOVN": TAG}, carried
    assert [x["symbol"] for x in st["_market_filter_skips"]] == ["7203.T", "SAP.DE", "NOVN.SW"]
    assert set(st["map"]) == {"NEWA", "7203", "SAP", "NOVN"}, st["map"]
    print("t8 a run that dies after tagged orders keeps their tags and record for "
          "the next publish OK")


def t9_markets_map_like_the_backtest():
    em = ib_bot.entry_market
    assert em({"symbol": "NEWA", "market": "US"}) == "US"
    assert em({"symbol": "NOVN.SW", "market": "EU"}) == "EU"         # CHF, still EU
    assert em({"symbol": "ETH-USD", "market": "CRYPTO"}) is None
    assert em({"symbol": "GLD", "market": "ETF"}) is None
    # no market field: the suffix
    assert em({"symbol": "7203.T"}) == "JP" and em({"symbol": "0700.HK"}) == "HK"
    assert em({"symbol": "SAP.DE"}) == "EU" and em({"symbol": "NOVO-B.CO"}) == "EU"
    assert em({"symbol": "MSFT"}) == "US" and em({"symbol": "BTC-USD"}) is None
    assert em({"symbol": "X.XX"}) is None and em({}) is None
    hit = ib_bot.shadow_filter_hit
    tr = ib_bot.market_trend_of({"market_trend": TREND})
    assert hit({"symbol": "7203.T", "market": "JP"}, tr)[0] == "JP"
    assert hit({"symbol": "NEWA", "market": "US"}, tr) is None
    assert hit({"symbol": "ETH-USD", "market": "CRYPTO"}, tr) is None
    assert hit({"symbol": "7203.T", "market": "JP"}, {}) is None      # unknown
    assert hit(None, tr) is None                                       # never raises
    print("t9 candidates map to US/HK/JP/EU by the row's market, crypto never OK")


# --------------------------------------------------------------- publishing --
class Done:
    returncode, stdout, stderr = 0, "", ""


class PubIB:
    def positions(self):
        return []

    def accountValues(self):
        return [broker.AccountValue("NetLiquidation", "100000", BASE),
                broker.AccountValue("CashBalance", "10", BASE)]


def seed_repo(prev):
    repo = Path(tempfile.mkdtemp(prefix="repo-", dir=str(_TMP)))
    (repo / "data").mkdir()
    (repo / "execution").mkdir()
    (repo / "execution" / "state.json").write_text('{"map": {}, "pos": {}}', encoding="utf-8")
    (repo / "data" / "bot_state.json").write_text(json.dumps(prev, indent=1), encoding="utf-8")
    return repo


def bot_publish(repo, state):
    import fills_capture
    import flex_dividends as fd
    import uk_cgt
    old = (ib_bot.__file__, subprocess.run, fills_capture.capture,
           fd.capture_if_configured, uk_cgt.build_report)
    try:
        ib_bot.__file__ = str(repo / "execution" / "ib_bot.py")
        subprocess.run = lambda *a, **k: Done()
        fills_capture.capture = lambda *a, **k: 0
        fd.capture_if_configured = lambda *a, **k: 0
        uk_cgt.build_report = lambda *a, **k: None
        ib_bot.publish_state(PubIB(), state, 100000.0)
    finally:
        (ib_bot.__file__, subprocess.run, fills_capture.capture,
         fd.capture_if_configured, uk_cgt.build_report) = old
    return json.loads((repo / "data" / "bot_state.json").read_text(encoding="utf-8"))


OLD_MF = {"mode": "shadow", "at": "2026-10-26 23:35 UTC", "markets": {"JP": TREND["JP"]},
          "would_have_skipped": [{"time": "2026-10-26 23:35 UTC", "symbol": "7203.T",
                                  "market": "JP", "pct": -5.96}]}


def t10_publish_state_writes_or_carries_the_block():
    skips = [{"time": "t%d" % k, "symbol": "S%d" % k, "market": "EU", "pct": -3.7}
             for k in range(60)]
    state = {"map": {}, "pos": {}, "_market_filter_skips": skips}
    # (a) a trading run: its mode, the markets it read, the last 50 rows
    ib_bot._RUN_MARKET_FILTER.update({"mode": "shadow", "at": "2026-10-27 23:35 UTC",
                                      "markets": ib_bot.market_trend_of(
                                          {"market_trend": TREND})})
    try:
        snap = bot_publish(seed_repo({"activity": [], "market_filter": OLD_MF}), state)
    finally:
        ib_bot._RUN_MARKET_FILTER.clear()
    mf = snap["market_filter"]
    assert mf["mode"] == "shadow" and mf["at"] == "2026-10-27 23:35 UTC", mf
    assert set(mf["markets"]) == {"US", "HK", "JP", "EU"}
    assert mf["markets"]["JP"]["below"] is True and mf["markets"]["US"]["below"] is False
    assert len(mf["would_have_skipped"]) == 50 and mf["would_have_skipped"][0]["symbol"] == "S10"
    # (b) publish_only - no trading run in this process: carried as it is
    snap = bot_publish(seed_repo({"activity": [], "market_filter": OLD_MF}), state)
    assert snap["market_filter"] == OLD_MF, snap.get("market_filter")
    # (c) nothing to carry: no key, never an invented one
    snap = bot_publish(seed_repo({"activity": []}), state)
    assert "market_filter" not in snap and snap["netliq"] == 100000
    # (d) a damaged record in state never costs the publish
    ib_bot._RUN_MARKET_FILTER.update({"mode": "shadow", "at": "x", "markets": {}})
    try:
        snap = bot_publish(seed_repo({"activity": []}),
                           {"map": {}, "pos": {}, "_market_filter_skips": "garbage"})
    finally:
        ib_bot._RUN_MARKET_FILTER.clear()
    assert snap["market_filter"]["would_have_skipped"] == [] and "positions" in snap
    print("t10 publish_state writes the run's block, or carries the last one OK")


def t11_publish_web_carries_the_block_untouched():
    import publish_web
    for prev, want in (({"activity": [], "market_filter": OLD_MF}, OLD_MF),
                       ({"activity": []}, None)):
        repo = seed_repo(prev)
        old = (publish_web.STATE, publish_web.DATA, publish_web.REPO, ib_web.snapshot,
               ib_web.ledger, subprocess.run, publish_web.sys.argv,
               publish_web.gitpush.push_with_retry)
        try:
            publish_web.STATE = repo / "execution" / "state.json"
            publish_web.DATA, publish_web.REPO = repo / "data", repo
            ib_web.snapshot = lambda: {"positions": [], "netliq": 100000.0,
                                       "cash": {BASE: 10.0}}
            ib_web.ledger = lambda *a, **k: {"BASE": {"netliquidationvalue": 100000.0,
                                                      "exchangerate": 1}}
            subprocess.run = lambda *a, **k: Done()
            publish_web.gitpush.push_with_retry = lambda *a, **k: True
            publish_web.sys.argv = ["publish_web.py"]
            assert publish_web.main() == 0
        finally:
            (publish_web.STATE, publish_web.DATA, publish_web.REPO, ib_web.snapshot,
             ib_web.ledger, subprocess.run, publish_web.sys.argv,
             publish_web.gitpush.push_with_retry) = old
        snap = json.loads((repo / "data" / "bot_state.json").read_text(encoding="utf-8"))
        assert snap.get("market_filter") == want, snap.get("market_filter")
    print("t11 publish_web.py carries the market_filter block untouched, never "
          "writes its own OK")


if __name__ == "__main__":
    t1_shadow_never_changes_an_order()
    t2_the_log_line_the_tag_and_the_rolling_record()
    t3_malformed_or_missing_trend_is_unknown_and_changes_nothing()
    t4_the_exits_and_the_slots_are_untouched()
    t5_off_logs_and_tags_nothing_and_an_unbuilt_mode_runs_as_shadow()
    t6_dry_logs_but_records_nothing()
    t7_a_refused_buy_is_still_tagged_a_declined_one_is_not()
    t8_an_aborted_run_keeps_the_tag_and_the_record()
    t9_markets_map_like_the_backtest()
    t10_publish_state_writes_or_carries_the_block()
    t11_publish_web_carries_the_block_untouched()
    print("ALL MARKET FILTER TESTS PASS")
