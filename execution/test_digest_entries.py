#!/usr/bin/env python3
"""Golden tests: the Telegram digest lists the entries the bot would make.

Run from this directory:  python test_digest_entries.py

The digest (daily_signal.py) is the action list the operator places by hand
when the gateway is down, so it must replay the bot's entry rules, which
changed on 2026-10-26 (owner decisions). Locked down here:

  * only BUY and BUY/HOLD rows, in the bot's order - tier (0 = E, 1 = D3), then
    score, then (since 2026-10-27) raw 90-day momentum - where it used to take
    data.json's rows unfiltered, in file order; the two rank functions agree
    row for row, and the tie-break's golden rows are the build's and the bot's;
  * crypto is never listed (the bot does not trade it), nor the parking ETF;
  * size = free cash / free slots, capped at HK$50,000: every balance but HKD
    and GBP at the digest's rates (an unknown rate leaves a balance out, never
    at 1.0), plus the bot's own HKD from the pocket file - labelled an estimate,
    since the digest sees no working orders; a candidate in a currency with no
    rate is reported, never sized at 1.0;
  * the 8% kill switch is OFF here as in the bot (KILL_SWITCH) - and still
    works if switched back on;
  * SGOV held is parked cash: valued, never judged by a stock exit rule, never
    a SELL line, never one of the 15 slots;
  * the module stays python 3.9-safe and free of any ib_bot import.

Nothing here touches /root or the network: every path is a temp file, every
fetch a stub.
"""
import ast
import io
import json
import os
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path

import testenv                                     # noqa: E402
_TMP = testenv.isolate("mps-digest-entries-")
REPO = _TMP / "repo"
(REPO / "execution").mkdir(parents=True)
(REPO / "data").mkdir()
os.environ.pop("EXCLUDED_CASH", None)
for _v in ("MAX_ORDER_BASE", "TARGET_POSITIONS", "HK_ENABLED", "DAILY_LOSS_KILL"):
    os.environ.pop(_v, None)
os.environ.setdefault("IB_BACKEND", "web")

import daily_signal as ds                          # noqa: E402
import earmark                                     # noqa: E402
import ib_web                                      # noqa: E402
testenv.assert_isolated()
ds.log = lambda *a, **k: None

HERE = Path(__file__).resolve().parent
AT = datetime(2026, 9, 16, 23, 40, tzinfo=timezone.utc)          # every market decided
BUILT = "2026-09-16T22:05:00Z"
FX = {"HKD=X": 7.8, "EURUSD=X": 1.1, "JPY=X": 150.0, "GBPUSD=X": 1.3}
USDHKD, JPYHKD, EURHKD, GBPHKD = 7.8, 7.8 / 150.0, 7.8 * 1.1, 7.8 * 1.3
TIMEOUT = urllib.error.URLError("timed out")


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


def row(sym, price, score, action="BUY", market="US", **kw):
    r = {"symbol": sym, "action": action, "price": price, "score": score, "market": market}
    r.update(kw)
    return r


def report(n_held, actions, cash, extra_pos=(), peak=None):
    """build_report on n_held calm US holdings (+ extra_pos rows from IB),
    LIVE from IB, with `cash` as IB's balances. Returns the message."""
    held = ["H%02d" % k for k in range(n_held)]
    state = {"map": {s: s for s in held}, "_peak_netliq": peak or 1,
             "pos": {s: {"entry": 90, "hw": 100, "stop": 80,
                         "entry_date": date.today().isoformat()} for s in held}}
    Path(ds.STATE).write_text(json.dumps(state), encoding="utf-8")
    Path(ds.BOT_STATE).write_text(json.dumps({"updated": "x", "netliq": 220000,
                                              "cash": cash, "positions": []}),
                                  encoding="utf-8")
    Path(ds.NETLIQ_HIST).write_text(json.dumps({"series": [{"d": "2026-07-20", "nl": 200000}],
                                                "flows": []}), encoding="utf-8")
    snap = {"netliq": 220000.0, "cash": dict(cash),
            "positions": [{"ib_symbol": s, "qty": 10, "avg_cost": 90.0, "ccy": "USD",
                           "sec_type": "STK"} for s in held] + list(extra_pos)}
    fetched = []

    def get_json(url, timeout=30):
        fetched.append(url)
        for s in held:
            if url == ds.PRODUCTS + s + ".json":
                return {"card": {"price": 100.0, "sma200": 80.0, "atr": 2.0},
                        "generated_at": BUILT}
        if url.startswith(ds.PRODUCTS):
            raise AssertionError("a card was asked for " + url)
        if "interval=1m" in url:                     # live marks: quiet problems list
            sym = url.split("/chart/")[1].split("?")[0]
            px = 100.45 if sym == "SGOV" else 100.0
            return {"chart": {"result": [{"timestamp": [1789570800],
                                          "indicators": {"quote": [{"close": [px]}]}}]}}
        for pair, v in FX.items():
            if "/chart/" + pair + "?" in url:
                return {"chart": {"result": [{"meta": {"regularMarketPrice": v}}]}}
        if "range=1y" in url:
            raise AssertionError("Yahoo was asked to judge " + url)
        if url == ds.BASE + "data.json":
            return {"generated_at": BUILT, "actions": list(actions)}
        raise AssertionError("unexpected fetch " + url)

    with Patch(ds, get_json=get_json, _now_utc=lambda: AT), \
            Patch(ib_web, snapshot=lambda: snap):
        msg = ds.build_report(on_demand=True)[0]
    return msg, fetched


def buy_lines(msg):
    return [l for l in msg.splitlines() if l.startswith("<code>BUY ")]


def t1_only_buy_rows_crypto_and_parking_never():
    actions = [row("WWW", 100.0, 99, action="WATCH"), row("AAA", 100.0, 98, action="AVOID"),
               row("ETH-USD", 2352.0, 97, action="BUY/HOLD", market="CRYPTO"),
               row("BTC-USD", 80554.0, 96, action="BUY", market="CRYPTO"),
               row("SOL", 150.0, 95, action="BUY", market="CRYPTO"),
               row("SGOV", 100.5, 94),
               row("HHH", 100.0, 90, action="BUY/HOLD"), row("BBB", 100.0, 80),
               row("NOLABEL", 100.0, 85, action=None)]
    msg, _ = report(10, actions, {"USD": 20000.0})
    got = [l.split()[1] for l in buy_lines(msg)]
    assert got == ["HHH", "BBB"], (got, msg)
    for sym in ("WWW", "AAA", "ETH-USD", "BTC-USD", "SOL", "SGOV", "NOLABEL"):
        assert sym not in msg, (sym, msg)
    assert "1 lot exceeds" not in msg, msg
    print("t1 the digest keeps BUY and BUY/HOLD rows only; crypto and SGOV never listed OK")


def t2_the_bots_order_tier_then_score():
    d3 = row("DDD", 100.0, 100, tier=1, signal="D3")
    e = row("EEE", 100.0, 60, tier=0, signal="E")
    msg, _ = report(14, [d3, e], {"USD": 20000.0})                  # ONE free slot
    assert [l.split()[1] for l in buy_lines(msg)] == ["EEE"], msg   # E before D3
    # the digest's rank IS the bot's, row for row (daily_signal may not import
    # ib_bot; this suite may)
    import ib_bot
    rows = [row("A", 1, 50), row("B", 1, 90, tier=1), row("C", 1, 70),
            row("D", 1, 95, tier="1"), row("E", 1, None), row("F", 1, "<9>", tier="junk"),
            row("G", 1, 80, tier=None), row("H", 1, float("nan"))]
    assert [ds.entry_rank(r) for r in rows] == [ib_bot.entry_rank(r) for r in rows]
    crypto = rows + [row("X-USD", 1, 1), row("Y", 1, 1, market="CRYPTO"),
                     row("Z", 1, 1, market="crypto"), {"symbol": None}]
    assert [ds.is_crypto_entry(r) for r in crypto] == [ib_bot.is_crypto_entry(r) for r in crypto]
    print("t2 the digest tries rows in the bot's order (tier, then score) OK")


# The tie-break golden (owner decision 2026-10-27): (symbol, tier, score, mom).
# engine/test_model_d3e1.py (the build) and test_sizing.py (the bot) pin these
# same rows to this same order. None = no "mom" key.
TIE_ROWS = [("LOW", 0, 100, 0.55), ("D3HI", 1, 100, 1.4), ("TOP", 0, 100, 0.91),
            ("NOM", 0, 100, None), ("E90", 0, 90, 0.45), ("MID", 0, 100, 0.72),
            ("D3LO", 1, 100, 0.6), ("NOM2", 0, 100, None), ("D3NO", 1, 100, None)]
TIE_ORDER = ["TOP", "MID", "LOW", "NOM", "NOM2", "E90", "D3HI", "D3LO", "D3NO"]
OLD_ORDER = ["LOW", "TOP", "NOM", "MID", "NOM2", "E90", "D3HI", "D3LO", "D3NO"]


def t2b_ties_at_100_go_to_the_higher_momentum():
    import ib_bot
    rows = []
    for s, t, sc, m in TIE_ROWS:
        r = row(s, 1, sc, tier=t)
        if m is not None:
            r["mom"] = m
        rows.append(r)
    assert [r["symbol"] for r in sorted(rows, key=ds.entry_rank)] == TIE_ORDER
    bare = [{k: v for k, v in r.items() if k != "mom"} for r in rows]
    assert [r["symbol"] for r in sorted(bare, key=ds.entry_rank)] == OLD_ORDER
    # the digest's key IS the bot's, term for term, garbage included
    odd = rows + bare + [row("M%d" % i, 1, 100, mom=m) for i, m in enumerate(
        (None, "0.9", float("nan"), float("inf"), float("-inf"), True, False, [1],
         10 ** 400, 0, -0.2, 3))]
    assert [ds.entry_rank(r) for r in odd] == [ib_bot.entry_rank(r) for r in odd]
    # ONE free slot, two dips tie at 100 with the weaker listed first: the BUY
    # line is the stronger one, as the bot buys it
    weak = row("WEAK", 100.0, 100, tier=0, signal="E", mom=0.58)
    strong = row("STRONG", 100.0, 100, tier=0, signal="E", mom=0.93)
    msg, _ = report(14, [weak, strong], {"USD": 20000.0})
    assert [l.split()[1] for l in buy_lines(msg)] == ["STRONG"], msg
    # an older data.json without "mom": the old file order
    msg, _ = report(14, [dict(weak, mom=None), dict(strong, mom=None)], {"USD": 20000.0})
    assert [l.split()[1] for l in buy_lines(msg)] == ["WEAK"], msg
    print("t2b ties at score 100 go to the higher 90-day momentum, the bot's key term "
          "for term; no mom = the old order OK")


def t3_size_is_free_cash_over_free_slots_with_the_bots_exclusions():
    earmark.write_pocket(14500.0, 0.0, True, "2026-09-01 00:00", datetime.now(timezone.utc))
    try:
        cash = {"USD": 5000.0, "JPY": 100000.0, "EUR": 1000.0, "GBP": 2000.0,
                "HKD": 22000.0, "CHF": 500.0}
        msg, _ = report(13, [row("BBB", 100.0, 80), row("7203.T", 2800.0, 70, market="JP")],
                        cash)
    finally:
        earmark.POCKET_FILE.unlink()
    # USD + JPY + EUR at the digest's rates, + the bot's own HKD (14,500 - 100):
    # never GBP (pass-through), never the HKD pot, never CHF at 1.0
    free_cash = 5000.0 * USDHKD + 100000.0 * JPYHKD + 1000.0 * EURHKD + 14400.0
    per = free_cash / 2
    assert per < 50000.0, per                       # the cap is t3's next case
    assert ("sized: free cash ~HK$%s / 2 free slot(s) = HK$%s each, cap HK$50,000 "
            "(est. - working orders not netted)" % (ds.money(free_cash), ds.money(per))) in msg, msg
    assert "sizing: CHF 500 left out - no rate" in msg, msg
    assert "<code>BUY BBB %d @ LMT" % int(per / USDHKD / 100.0) in msg, msg
    assert "<code>BUY 7203.T %d @ LMT" % (int(per / JPYHKD / 2800.0) // 100 * 100) in msg, msg

    # one free slot and HK$150k free: the cap binds
    msg, _ = report(14, [row("BBB", 100.0, 80)], {"USD": 150000.0 / USDHKD})
    assert "<code>BUY BBB %d @ LMT" % int(50000.0 / USDHKD / 100.0) in msg, msg
    # a candidate whose currency has no rate is reported, never sized at 1.0
    with Patch(ds, fx_rates=lambda problems: {"HKD": 1.0, "USD": 7.8}):
        msg, _ = report(13, [row("SAP.DE", 200.0, 90, market="EU")], {"USD": 20000.0})
    assert not buy_lines(msg) and "SAP.DE: no EUR rate to size it" in msg, msg
    # a balance OWED with no rate stops entries, as in the bot
    msg, _ = report(13, [row("BBB", 100.0, 80)], {"USD": 20000.0, "CHF": -500.0})
    assert not buy_lines(msg) and "CHF -500 owed and no rate" in msg, msg
    print("t3 size = free cash / free slots (no GBP, no HKD pot, no 1.0 rates), "
          "capped at HK$50k, labelled an estimate OK")


def t4_the_kill_switch_is_off_here_too():
    assert ds.KILL_SWITCH is False
    src = io.open(HERE / "ib_bot.py", encoding="utf-8").read()
    assert "\nKILL_SWITCH = False\n" in src, "the bot and the digest must agree"
    # NetLiq 220,000 against a stored peak of 1,000,000: 78% down
    msg, _ = report(10, [row("BBB", 100.0, 80)], {"USD": 20000.0}, peak=1000000)
    assert buy_lines(msg) and "Kill switch: off" in msg and "ACTIVE" not in msg, msg
    # switched back on, the old gate still works
    with Patch(ds, KILL_SWITCH=True):
        msg, _ = report(10, [row("BBB", 100.0, 80)], {"USD": 20000.0}, peak=1000000)
    assert not buy_lines(msg) and "kill switch active" in msg and "<b>ACTIVE</b>" in msg, msg
    print("t4 the digest's kill switch is off, as the bot's; switched on it still gates OK")


def t5_sgov_is_parked_cash_never_a_sell_or_a_slot():
    sgov = {"ib_symbol": "SGOV", "qty": 300, "avg_cost": 100.4, "ccy": "USD",
            "sec_type": "STK"}
    msg, fetched = report(14, [row("BBB", 100.0, 80)], {"USD": 20000.0}, extra_pos=[sgov])
    assert "SELL SGOV" not in msg and "<code>SELL" not in msg, msg
    assert not [u for u in fetched if "SGOV" in u and ds.PRODUCTS in u], fetched
    assert "SGOV: no state['map'] entry" not in msg, msg
    assert "POSITIONS (15)" in msg and "<code>SGOV " in msg, msg
    assert "SGOV is parked cash, not a stock slot" in msg, msg
    # 14 stocks + SGOV: one stock slot is still free
    assert [l.split()[1] for l in buy_lines(msg)] == ["BBB"], msg
    print("t5 SGOV held: valued, no exit rule, no SELL line, no slot OK")


def t6_still_python_39_safe_and_free_of_ib_bot():
    src = io.open(HERE / "daily_signal.py", encoding="utf-8").read()
    ast.parse(src, "daily_signal.py", feature_version=(3, 9))
    tree = ast.parse(src)
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert "ib_bot" not in imported, imported
    print("t6 daily_signal stays 3.9-safe and imports nothing from ib_bot OK")


if __name__ == "__main__":
    t1_only_buy_rows_crypto_and_parking_never()
    t2_the_bots_order_tier_then_score()
    t2b_ties_at_100_go_to_the_higher_momentum()
    t3_size_is_free_cash_over_free_slots_with_the_bots_exclusions()
    t4_the_kill_switch_is_off_here_too()
    t5_sgov_is_parked_cash_never_a_sell_or_a_slot()
    t6_still_python_39_safe_and_free_of_ib_bot()
    print("ALL DIGEST ENTRY TESTS PASS")
