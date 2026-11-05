#!/usr/bin/env python3
"""Golden tests: the forward scoreboard (engine/forward/).

Run from this directory:  python test_forward_scoreboard.py

The scoreboard compares the frozen rules with random picks on data no decision has used. What must stay true:

  * The freeze holds: an edit to the list, the settings, the rule code or the manifest fails the check, and the runner refuses to go.
  * No look-ahead: changing prices, dividends, splits or filings after an as-of date cannot change anything before it.
  * Twins take the same days and the same pick counts as LIVE, are reproducible from their seed, and a new day or a lost name
    cannot move their past. numpy SeedSequence and default_rng, never crc32.
  * Readings are append-only, with a hash chain over the earlier ones; a second run on the same close writes nothing.
  * A name that vanishes from Yahoo is carried from the store, counted and named; if an arm held it, it is sold at its last seen
    price and flagged. Bars saved months apart join cleanly across a split.
  * If SEC cannot be reached the reading says E1 did not run. If a download fails badly, nothing is written.
  * Nothing under execution/ imports or reads the scoreboard.

Offline: urllib's opener fails the test; prices and filings are small synthetic fixtures; every file goes to a temp dir. It only
READS the committed frozen files and execution/. Identity with the review's saved runs needs temp data and is not checked here.
"""
import contextlib
import copy
import datetime as dt
import io
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

ENGINE = Path(__file__).resolve().parent
REPO = ENGINE.parent
sys.path.insert(0, str(ENGINE))


def _no_urlopen(*a, **k):
    raise AssertionError("test reached the network (urlopen)")


urllib.request.urlopen = _no_urlopen

from forward import data_store as DS            # noqa: E402
from forward import frozen as FZ                # noqa: E402
from forward import fp_sim as FP                # noqa: E402
from forward import live_sim as LS              # noqa: E402
from forward import rules as RU                 # noqa: E402
from forward import scoreboard as SB            # noqa: E402
from forward import twins as TW                 # noqa: E402

FROZEN_AT_FREEZE = "9e3f5558a2e6415adb7f0be02b3ec97575ecce03cb5c21511024b0ea6d6bc193"
UTC = dt.timezone.utc
DAYS = pd.bdate_range("2025-06-02", "2027-06-30")
US = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]
JP = ["1111.T", "2222.T", "3333.T"]
HK = ["0011.HK", "0022.HK"]
EU = ["AAA.PA", "BBB.DE"]
NAMES = US + JP + HK + EU + ["BTC-USD", "^VIX"]
MARKET = dict([(t, "US") for t in US] + [(t, "JP") for t in JP] + [(t, "HK") for t in HK] + [(t, "EU") for t in EU] + [("BTC-USD", "CRYPTO"), ("^VIX", "INDEX")])
PRICE = {"US": 60.0, "JP": 200.0, "HK": 10.0, "EU": 50.0, "CRYPTO": 30000.0}
FX_LEVEL = {"HKD=X": 7.8, "JPYHKD=X": 0.052, "EURHKD=X": 9.0, "GBPHKD=X": 10.5, "CHFHKD=X": 9.5}


def make_fz(n_twins=4):
    s = copy.deepcopy(FZ.load().settings)
    s["twins"]["n"] = n_twins
    return FZ.Frozen(s, list(NAMES), dict(MARKET), {t: 100 for t in HK}, {t: 1000 + k for k, t in enumerate(US)})


def as_traded(c, seed, days=DAYS):
    """As-traded bars around a close series, with one dividend a year or so."""
    rng = np.random.default_rng(seed)
    n = len(c)
    op = np.r_[c[0], c[:-1]] * (1 + rng.normal(0, 0.002, n))
    hi = np.maximum(op, c) * (1 + np.abs(rng.normal(0, 0.006, n)))
    lo = np.minimum(op, c) * (1 - np.abs(rng.normal(0, 0.006, n)))
    b = pd.DataFrame({"Open": op, "High": hi, "Low": lo, "Close": c, "Dividends": 0.0, "Splits": 0.0, "AdjStep": 1.0}, index=days[:n])
    k = 150 + seed % 40
    b.iloc[k, b.columns.get_loc("Dividends")] = c[k - 1] * 0.01
    b.iloc[k, b.columns.get_loc("AdjStep")] = 0.99
    return b


def make_prices():
    """A rising market with sharp 3-day dips, so E and D3 cards keep coming and every arm trades."""
    out = {}
    for k, t in enumerate(NAMES):
        rng = np.random.default_rng(1000 + k)
        r = 0.009 + rng.normal(0, 0.011, len(DAYS))
        for j in range(20 + 3 * k, len(DAYS) - 3, 22 + k % 5):
            r[j:j + 3] = -0.025
        p0 = PRICE.get(MARKET[t], 100.0)
        out[t] = as_traded(p0 * np.exp(np.cumsum(r)) * 0.6, 1000 + k)
    for k, (t, lvl) in enumerate(FX_LEVEL.items()):
        out[t] = as_traded(lvl * (1 + 0.003 * np.sin(np.arange(len(DAYS)) / 40.0 + k)), 2000 + k)
    out["^IRX"] = as_traded(np.full(len(DAYS), 4.0), 2100)
    for k, t in enumerate(("SPY", "RSP", "ACWI")):
        out[t] = as_traded(100 * np.exp(np.cumsum(np.full(len(DAYS), 0.0004 + 0.0001 * k))), 2200 + k)
    return out


PRICES = make_prices()


def fake_bars(prices=None, gone=(), broken=()):
    prices = PRICES if prices is None else prices

    def fn(t, from_day, now):
        if t in broken:
            raise DS.FetchFailed("HTTP 429")
        if t in gone or t not in prices:
            return None
        b = prices[t]
        return b[(b.index >= pd.Timestamp(from_day)) & (b.index.date <= now.date())]        # today's bar, even if unfinished
    return fn


def fake_facts(growing=True):
    """Ten filed quarters of EPS, growing or falling, filed 30 days after each quarter ends."""
    ends = pd.date_range("2024-03-31", "2026-12-31", freq="QE")

    def fn(t):
        rows = []
        for k, e in enumerate(ends):
            v = 1.0 + (0.05 if growing else -0.05) * k
            rows.append({"start": (e - pd.Timedelta(days=90)).date().isoformat(), "end": e.date().isoformat(), "val": v,
                         "filed": (e + pd.Timedelta(days=30)).date().isoformat(), "form": "10-Q"})
        return pd.DataFrame(rows)
    return fn


def run_at(tmp, when, **kw):
    fz = kw.pop("fz", None) or make_fz()
    kw.setdefault("bars_fn", fake_bars())
    kw.setdefault("facts_fn", fake_facts())
    netliq = Path(tmp) / "netliq.json"
    if not netliq.exists():
        netliq.write_text(json.dumps({"series": [{"d": "2026-11-04", "nl": 215000}, {"d": "2026-11-06", "nl": 216000}], "flows": []}))
    return SB.run(fz, when, Path(tmp) / "readings.json", Path(tmp) / "store", netliq, **kw)


def install(prices=None, end="2027-03-01", fz=None):
    """The arrays for a run, as SB.run builds them. Returns (fz, tickers, e1, firsts)."""
    fz = fz or make_fz()
    prices = PRICES if prices is None else prices
    wanted = list(prices)
    bars, _, _ = DS.merge({t: prices[t] for t in wanted}, {}, wanted, {}, end)
    tickers = SB.install_data(bars, fz)
    first = fz.settings["first_session"]
    firsts = TW.window_keys(LS.DATA["gdates"], LS.dnum(first))
    e1, _ = RU.e1_verdicts(tickers, firsts["US"], fake_facts(True))
    LS.E1 = {"fixed": e1}
    return fz, tickers, e1, firsts


def t1_the_freeze_holds():
    assert FZ.MANIFEST_SHA256 == FROZEN_AT_FREEZE
    assert FZ.verify() == FROZEN_AT_FREEZE
    fz = FZ.load()
    s = fz.settings
    assert s["freeze_commit"] == "aace4aa7" and s["freeze_date"] == "2026-11-05" and s["first_session"] == "2026-11-06"
    assert s["capital_hkd"] == 222000.0 and s["twins"]["n"] == 20
    assert s["arms"] == {"LIVE": {"variant": "D3E1", "rank": "rawmom"}, "D3E1_FILE": {"variant": "D3E1", "rank": "score"}, "E_A": {"variant": "E", "rank": "rawmom"}}
    assert len(fz.tickers) == 992 and len(fz.sim_tickers) == 882 and fz.tickers[0] == "TXN"

    with tempfile.TemporaryDirectory(prefix="mps-fwd-") as d:
        root = Path(d)
        for f in FZ.FROZEN_FILES + (FZ.FROZEN_DIR + "/MANIFEST.json",):
            (root / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / f, root / f)
        assert FZ.verify(root) == FROZEN_AT_FREEZE

        # CRLF or LF, the same hash: a Windows worktree and a clean export must agree
        p = root / FZ.FROZEN_DIR / "list.json"
        original = p.read_bytes()
        p.write_bytes(original.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        assert FZ.verify(root) == FROZEN_AT_FREEZE
        p.write_bytes(original)

        def edited(rel, swap):
            q = root / rel
            keep = q.read_bytes()
            q.write_bytes(swap(keep))
            try:
                FZ.verify(root)
            except FZ.FrozenEdited:
                return True
            finally:
                q.write_bytes(keep)
            return False
        assert edited(FZ.FROZEN_DIR + "/list.json", lambda b: b.replace(b'"TXN"', b'"TXNX"'))
        assert edited(FZ.FROZEN_DIR + "/settings.json", lambda b: b.replace(b"20261105", b"20261106"))
        assert edited(FZ.FROZEN_DIR + "/settings.json", lambda b: b.replace(b"222000.0", b"100000.0"))
        assert edited(FZ.FROZEN_DIR + "/MANIFEST.json", lambda b: b + b"\n")
        for rel in ("engine/forward/live_sim.py", "engine/forward/fp_sim.py", "engine/forward/rules.py", "engine/forward/twins.py",
                    "engine/forward/model_d3e1_frozen.py", "engine/forward/indicators_frozen.py"):
            assert edited(rel, lambda b: b + b"\n# touched\n"), rel
        assert FZ.verify(root) == FROZEN_AT_FREEZE
    print("t1 the frozen list, settings, rule code and manifest cannot change without failing, CRLF or LF OK")


def t2_nothing_after_the_as_of_date_reaches_back():
    asof = pd.Timestamp("2027-01-15")
    changed = {t: b.copy() for t, b in PRICES.items()}
    for t, b in changed.items():
        if t in FX_LEVEL or t == "^IRX":
            continue
        later = b.index > asof
        b.loc[later, ["Open", "High", "Low", "Close"]] *= 1.5          # a later jump...
        i = int(np.flatnonzero(later)[10])
        b.iloc[i:, [b.columns.get_loc(c) for c in ("Open", "High", "Low", "Close")]] /= 2.0   # ...a later split...
        b.iloc[i, b.columns.get_loc("Splits")] = 2.0
        b.iloc[i + 5, b.columns.get_loc("Dividends")] = 1.0            # ...and a later dividend
        b.iloc[i + 5, b.columns.get_loc("AdjStep")] = 0.98

    def everything(prices, end):
        fz, tickers, e1, firsts = install(prices, end)
        tables, counts = SB.twin_tables(fz, tickers, e1, firsts)
        out = {name: SB.run_sim(cfg, fz.settings["first_session"], end, fz) for name, cfg in fz.settings["arms"].items()}
        cards, fpct, _ = tables[0]
        out["TWIN"] = SB.run_sim({"variant": "D3", "rank": "feat", "fpct": fpct}, fz.settings["first_session"], end, fz, cards=cards)
        sig = {t: (r["dates"], r["E"].copy(), r["D3"].copy() if "D3" in r else None) for t, r in tickers.items()}
        return out, sig, tables, dict(e1)

    def upto(cards):
        return {k: v for k, v in cards.items() if k[1] <= LS.dnum(asof)}

    a, sig_a, tab_a, e1_a = everything(PRICES, "2027-03-01")
    b, sig_b, tab_b, e1_b = everything(changed, "2027-03-01")
    c, _, tab_c, e1_c = everything({t: x[x.index <= asof] for t, x in PRICES.items()}, asof.date().isoformat())
    assert len(a["LIVE"]["trades"]) >= 5, "the fixture must trade"
    for name in ("LIVE", "D3E1_FILE", "E_A", "TWIN"):
        ca = a[name]["curve"]
        for other in (b[name]["curve"], c[name]["curve"]):
            x, y = ca[ca.index <= asof], other[other.index <= asof]
            assert x.index.equals(y.index)
            assert float((x - y).abs().max()) < 1e-6, (name, float((x - y).abs().max()))
        assert (ca.to_numpy() != b[name]["curve"].to_numpy()).any(), "the later change must matter after the date"
        ta, tb = a[name]["trades"], b[name]["trades"]
        done = lambda t: t[t["exit_date"] <= asof][["ticker", "fill_date", "exit_date", "shares"]].reset_index(drop=True)
        assert done(ta).equals(done(tb)), name
    for t, (d, e, d3) in sig_a.items():
        k = d <= np.datetime64(asof.date())
        d2, e2, d32 = sig_b[t]
        assert (e[k] == e2[d2 <= np.datetime64(asof.date())]).all(), t
        if d3 is not None:
            assert (d3[k] == d32[d2 <= np.datetime64(asof.date())]).all(), t
    for (ca, fa, _), (cb, fb, _) in zip(tab_a, tab_b):
        assert upto(ca) == upto(cb)
    for (ca, fa, _), (cc, fc, _) in zip(tab_a, tab_c):
        assert upto(ca) == upto(cc)

    # a filing after the signal day cannot change that day's E1 verdict, and an earlier one can
    good = fake_facts(True)("AAA")
    tick = {"AAA": dict(m="US", E=np.array([True, True]), dates=np.array(["2026-11-10", "2027-03-10"], dtype="datetime64[D]"))}
    v1, _ = RU.e1_verdicts(tick, 0, lambda t: good)
    bad = good.copy()
    late = pd.to_datetime(bad["filed"]) > pd.Timestamp("2026-11-10")
    bad.loc[late, "val"] = -50.0
    v2, _ = RU.e1_verdicts(tick, 0, lambda t: bad)
    assert v1["AAA"][0] is True and v2["AAA"][0] is True, (v1, v2)
    assert v2["AAA"][1] is False, v2
    print("t2 later prices, splits, dividends and filings change nothing before the as-of date, and do change what follows OK")


def t3_twins_copy_lives_days_and_counts_and_never_move():
    fz, tickers, e1, firsts = install()
    real = LS.DATA["cards"]
    counts = TW.live_counts(real, e1, tickers, firsts)
    assert sum(a + b for a, b in counts.values()) >= 20, "the fixture must make picks"
    tables, counts2 = SB.twin_tables(fz, tickers, e1, firsts)
    assert counts2 == counts
    # by hand: what LIVE's list reads, day by day
    for key, (n_e, n_d) in counts.items():
        lst = real[key]
        es = [c for c in lst if c[0] == "E" and e1.get(c[1], {}).get(c[2], True)]
        ds = [c for c in lst if c[0] == "D3" and c[1] not in {x[1] for x in es}]
        assert (len(es), len(ds)) == (n_e, n_d), key
    for cards, fpct, short in tables:
        assert short == 0, short
        got = {k: v for k, v in cards.items() if any(c[0] != "CRY" for c in v)}
        assert set(got) == set(counts)
        for key, (n_e, n_d) in counts.items():
            kinds = [c[0] for c in cards[key] if c[0] != "CRY"]
            assert kinds.count("E") == n_e and kinds.count("D3") == n_d, key
            names = [c[1] for c in cards[key] if c[0] != "CRY"]
            assert len(names) == len(set(names))
            for k_, t, i in cards[key]:
                if k_ != "CRY":
                    assert tickers[t]["UP"][i] and tickers[t]["group"] == key[0] and tickers[t]["dn"][i] == key[1], (t, key)
                    assert (t, i) in fpct
    # reproducible from the seed, and different seeds differ
    entropy = fz.settings["twins"]["entropy"]
    elig = TW.eligible(tickers, list(counts), fz.order)
    a, fa, _ = TW.twin_table(0, counts, elig, fz.tickers, entropy)
    b, fb, _ = TW.twin_table(0, counts, elig, fz.tickers, entropy)
    c, _, _ = TW.twin_table(1, counts, elig, fz.tickers, entropy)
    assert a == b and fa == fb and a != c
    # a new day appended changes no earlier pick, whichever group it falls in
    keys = sorted(counts, key=lambda k: k[1])
    early = {k: counts[k] for k in keys[:len(keys) // 2]}
    x, _, _ = TW.twin_table(0, early, elig, fz.tickers, entropy)
    assert x == {k: a[k] for k in early}
    # a name that stops existing changes only the picks that involved it
    victim = a[keys[0]][0][1]
    less = {k: (v[0][v[0] != fz.order[victim]], v[1][v[0] != fz.order[victim]]) for k, v in elig.items()}
    y, _, _ = TW.twin_table(0, counts, less, fz.tickers, entropy)
    for k in counts:
        if not any(c[1] == victim for c in a[k]) and len(less[k][0]) >= sum(counts[k]):
            assert y[k] == a[k], k
    # a twin runs through the same simulator, session for session
    res = SB.run_sim({"variant": "D3", "rank": "feat", "fpct": tables[0][1]}, fz.settings["first_session"], "2027-03-01", fz, cards=tables[0][0])
    assert len(res["curve"]) == np.busday_count("2026-11-06", "2027-03-02")
    src = (ENGINE / "forward" / "twins.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in src.splitlines() if "never crc32" not in line)
    assert "SeedSequence" in code and "default_rng" in code and "crc32" not in code and "zlib" not in code
    print("t3 twins: LIVE's days and pick counts, uptrend names only, reproducible by seed, the past fixed OK")


def t4_readings_are_append_only():
    with tempfile.TemporaryDirectory(prefix="mps-fwd-") as d:
        p = Path(d) / "readings.json"
        one = SB.seal({"reading": 1, "last_close": "2026-11-05", "x": 1.5}, None)
        SB.append_reading(p, one)
        first = p.read_bytes()
        two = SB.seal({"reading": 2, "last_close": "2027-05-05", "x": 2.5}, one)
        SB.append_reading(p, two)
        three = SB.seal({"reading": 3, "last_close": "2027-11-05", "x": 3.5}, two)
        SB.append_reading(p, three)
        all3 = p.read_bytes()
        assert all3.startswith(first[:-3])                  # the earlier bytes are still there, untouched
        back = json.loads(all3)
        assert [r["reading"] for r in back] == [1, 2, 3] and back[0] == one and back[1] == two
        SB.verify_chain(back)
        # an edit to an earlier reading, or removing one, stops the next append
        for bad in (all3.replace(b'"x": 1.5', b'"x": 9.5'), json.dumps([back[0], back[2]], indent=1).encode() + b"\n"):
            p.write_bytes(bad)
            try:
                SB.append_reading(p, SB.seal({"reading": 4, "last_close": "2028-05-05"}, three))
            except ValueError:
                pass
            else:
                raise AssertionError("an edited history was accepted")
            assert p.read_bytes() == bad
        # the whole runner: same close, nothing new; a later close, one more; the first stays
        with tempfile.TemporaryDirectory(prefix="mps-fwd-") as e:
            r1 = run_at(e, dt.datetime(2026, 11, 20, 23, 0, tzinfo=UTC))
            before = (Path(e) / "readings.json").read_bytes()
            again = run_at(e, dt.datetime(2026, 11, 21, 9, 0, tzinfo=UTC))
            assert not again["written"] and "already exists" in again["text"]
            assert (Path(e) / "readings.json").read_bytes() == before
            r2 = run_at(e, dt.datetime(2026, 12, 10, 23, 0, tzinfo=UTC))
            assert r1["written"] and r2["written"]
            after = (Path(e) / "readings.json").read_bytes()
            assert after.startswith(before[:-3])
            got = json.loads(after)
            assert [x["reading"] for x in got] == [1, 2] and got[0] == r1["reading"]
            SB.verify_chain(got)
            # a past start is a pipeline test: it can never be written
            try:
                run_at(e, dt.datetime(2026, 12, 11, 23, 0, tzinfo=UTC), start="2026-11-20")
            except ValueError:
                pass
            else:
                raise AssertionError("a past start was written")
            test = run_at(e, dt.datetime(2026, 12, 11, 23, 0, tzinfo=UTC), start="2026-11-20", write=False)
            assert not test["written"] and "PIPELINE TEST" in test["text"] and (Path(e) / "readings.json").read_bytes() == after
    print("t4 readings only ever grow, earlier bytes untouched, an edit is refused, a pipeline test writes nothing OK")


def t5_a_vanished_name_is_carried_sold_and_flagged():
    with tempfile.TemporaryDirectory(prefix="mps-fwd-") as d:
        r1 = run_at(d, dt.datetime(2026, 12, 15, 23, 0, tzinfo=UTC))
        assert r1["reading"]["data"]["carried_count"] == 0 and r1["reading"]["sessions"] == 28
        assert r1["reading"]["data"]["store"]["rows"] > 1000
        fz = make_fz()
        res = SB.run_sim(fz.settings["arms"]["LIVE"], "2026-11-06", "2026-12-15", fz)         # the arrays are still installed
        held = sorted(res["open"]["ticker"])
        assert held, "the fixture must hold something at the first reading"
        gone = held[0]
        last = PRICES[gone][PRICES[gone].index <= "2026-12-15"]
        r2 = run_at(d, dt.datetime(2027, 1, 15, 23, 0, tzinfo=UTC), bars_fn=fake_bars(gone=(gone,)))
        x = r2["reading"]["data"]
        assert x["carried_count"] == 1 and list(x["carried"]) == [gone] and x["carried"][gone] == "no longer on Yahoo", x["carried"]
        sold = x["flags"]["LIVE"]["sold_at_last_seen_price"]
        assert [s["ticker"] for s in sold] == [gone], sold
        assert abs(sold[0]["last_seen_price"] / float(last["Close"].iloc[-1]) - 1) < 1e-5, (sold, last["Close"].iloc[-1])
        assert sold[0]["sold_on"] > "2026-12-15"
        assert gone in r2["text"] and "FLAG LIVE sold" in r2["text"] and "1 of %d names" % len(fz.sim_tickers) in r2["text"]
        # a name that comes back is fresh again, and every reading reports the count
        r3 = run_at(d, dt.datetime(2027, 2, 15, 23, 0, tzinfo=UTC))
        assert r3["reading"]["data"]["carried_count"] == 0 and "0 of %d names" % len(fz.sim_tickers) in r3["text"]
        # each file holds only what no earlier one had
        files = sorted((Path(d) / "store").glob("bars_*.csv.xz"))
        assert len(files) == 3
        assert pd.read_csv(files[1], compression="xz")["date"].min() > "2026-12-15"

    # merge: fresh wins on a shared day, the store fills the rest, a lost dividend stays, an early end is named
    days = pd.bdate_range("2026-01-05", periods=6)
    frame = lambda c, div=0.0: pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Dividends": div, "Splits": 0.0, "AdjStep": 1.0}, index=days)
    s = frame(10.0)
    s.loc[days[2], ["Dividends", "AdjStep"]] = [0.5, 0.95]
    f = frame(11.0).iloc[:4]
    bars, carried, missing = DS.merge({"X": f}, {"X": s}, ["X", "Y"], {}, "2026-01-30")
    assert list(bars["X"]["Close"]) == [11.0, 11.0, 11.0, 11.0, 10.0, 10.0]
    assert bars["X"]["Dividends"].iloc[2] == 0.5 and bars["X"]["AdjStep"].iloc[2] == 0.95
    assert "X" in carried and "store" in carried["X"] and missing == ["Y"]

    # bars saved before a split join bars saved after it, with no false jump
    raw = pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0, "Dividends": 0.0, "Splits": 0.0, "AdjClose": 100.0}, index=pd.bdate_range("2026-01-05", periods=10))
    before = DS.to_actual(raw.iloc[:6])
    assert before["Close"].iloc[0] == 100.0
    after = raw.copy()
    after.loc[after.index[:7], ["Open", "High", "Low", "Close", "AdjClose"]] /= 2.0   # Yahoo restates everything before a 2:1 split
    after.iloc[7, after.columns.get_loc("Splits")] = 2.0
    later = DS.to_actual(after)
    assert later["Close"].iloc[0] == 50.0 * 2 and later["Close"].iloc[8] == 100.0
    with tempfile.TemporaryDirectory(prefix="mps-fwd-") as d:
        DS.write_store(d, {"X": before}, {}, "2026-01-12")
        stored = DS.load_store(d)
        DS.write_store(d, {"X": later}, stored, "2026-01-16")
        again = DS.load_store(d)["X"]
        r, F = RU.split_adjusted(again)
        assert len(again) == 10 and list(F[:8]) == [2.0] * 7 + [1.0] and (r["Close"].to_numpy() == [50.0] * 7 + [100.0] * 3).all(), (F, r["Close"])
    print("t5 a vanished name is carried, counted, named, sold at its last seen price and flagged; bars join across a split OK")


def t6_sec_out_of_reach_is_said_out_loud():
    def down(t):
        raise DS.FetchFailed("timed out")

    def half(t):
        if t in ("AAA", "BBB", "CCC"):
            raise DS.FetchFailed("timed out")
        return fake_facts()(t)

    when = dt.datetime(2027, 1, 15, 23, 0, tzinfo=UTC)
    with tempfile.TemporaryDirectory(prefix="mps-fwd-") as d:
        ok = run_at(d, when, write=False, start="2026-11-06")
        assert ok["reading"]["e1"]["ran"] == "yes" and ok["reading"]["e1"]["cards"] > 0
        assert "DID NOT RUN" not in ok["text"]
        r = run_at(d, when, facts_fn=down)
        assert r["written"] and r["reading"]["e1"]["ran"] == "NO" and r["reading"]["e1"]["scored"] == 0
        assert "E1 DID NOT RUN" in r["text"]
        saved = json.loads((Path(d) / "readings.json").read_text())[0]
        assert saved["e1"]["ran"] == "NO"
        # it really is a different result, which is why it must be said
        assert saved["arms"]["LIVE"]["end_value"] != ok["reading"]["arms"]["LIVE"]["end_value"] or ok["reading"]["e1"]["blocked"] == 0
    with tempfile.TemporaryDirectory(prefix="mps-fwd-") as d:
        r = run_at(d, when, facts_fn=half, write=False, start="2026-11-06")
        assert r["reading"]["e1"]["ran"] == "PARTLY" and set(r["reading"]["e1"]["names_failed"]) <= {"AAA", "BBB", "CCC"}
        assert "ONLY IN PART" in r["text"]
    # blocked in E1 means a falling EPS: E cards drop out, and the twins copy the smaller count
    fz, tickers, e1, firsts = install()
    bad, _ = RU.e1_verdicts(tickers, firsts["US"], fake_facts(False))
    assert sum(not v for x in bad.values() for v in x.values()) > 0
    c_good = TW.live_counts(LS.DATA["cards"], e1, tickers, firsts)
    c_bad = TW.live_counts(LS.DATA["cards"], bad, tickers, firsts)
    assert sum(a for a, _ in c_bad.values()) < sum(a for a, _ in c_good.values())
    print("t6 SEC down: the reading is written and says E1 did not run; in part: says so OK")


def t7_a_baseline_with_no_sessions_yet():
    with tempfile.TemporaryDirectory(prefix="mps-fwd-") as d:
        r = run_at(d, dt.datetime(2026, 11, 5, 9, 0, tzinfo=UTC))
        x = r["reading"]
        assert r["written"] and x["kind"] == "baseline" and x["sessions"] == 0 and x["last_close"] == "2026-11-04"
        assert x["arms"]["LIVE"]["end_value"] == 222000.0 and x["arms"]["LIVE"]["growth"] == 0.0 and x["twins"]["live_beat"] is None
        assert x["benchmarks"]["SPY"]["growth"] == 0.0 and x["data"]["carried_count"] == 0
        assert x["real_account"]["available"] and x["real_account"]["growth"] == 0.0
        t = r["text"]
        assert "HOW LITTLE THIS CAN SHOW" in t and "Too few sessions" in t and "18 years" in t and "owner's exception" in t
        assert x["data"]["store"]["rows"] > 1000
        # a reading a little later has sessions, and the same warning
        r2 = run_at(d, dt.datetime(2026, 11, 13, 23, 0, tzinfo=UTC))
        assert r2["reading"]["sessions"] == 6 and r2["reading"]["kind"] == "reading" and r2["reading"]["prev_sha256"] == SB.hashlib.sha256(SB._canon(x).encode()).hexdigest()
        assert "t = " in r2["text"] and "18 years" in r2["text"] and "LIVE beat" in r2["text"]
        assert r2["reading"]["arms"]["LIVE"]["annual"] is None     # a year's rate from a week is not shown
    print("t7 the first reading has no sessions, stands at HK$222,000, and still says what it cannot show OK")


def t8_a_bad_fetch_stops_the_reading():
    when = dt.datetime(2026, 12, 1, 23, 0, tzinfo=UTC)
    with tempfile.TemporaryDirectory(prefix="mps-fwd-") as d:
        for kw in (dict(broken=tuple(US[:3])), dict(gone=("HKD=X",)), dict(gone=("^IRX",))):
            try:
                run_at(d, when, bars_fn=fake_bars(**kw))
            except SB.ReadingStopped as e:
                assert "Nothing written" in str(e)
            else:
                raise AssertionError("no stop for %s" % kw)
            assert not (Path(d) / "readings.json").exists() and not list((Path(d) / "store").glob("*") if (Path(d) / "store").exists() else [])
        # one fund missing is not a stop: the table says so
        r = run_at(d, when, bars_fn=fake_bars(gone=("ACWI",)))
        assert r["reading"]["benchmarks"]["ACWI"] is None and "ACWI" in r["text"] and "not available" in r["text"]
    # the runner reports a stop and returns 1 rather than a traceback or a guess; and it refuses an edited freeze
    src = (ENGINE / "forward" / "run_scoreboard.py").read_text(encoding="utf-8")
    assert "FZ.verify()" in src and "READING STOPPED" in src
    print("t8 too many failed downloads, or no exchange rates, stop the reading with nothing written OK")


def t9_mode_switches_off_is_the_plain_simulator_on():
    fz, tickers, e1, firsts = install()
    cfg = dict(fz.settings["sim"], capital=222000.0, start="2026-11-06", end="2027-03-01", variant="D3E1", rank="rawmom")
    a = FP.ForwardSim(cfg, fp=False).go().result()
    b = LS.Sim(cfg).go().result()
    assert a["curve"].equals(b["curve"]) and a["skips"] == b["skips"] and len(a["trades"]) == len(b["trades"]) > 0
    c = FP.ForwardSim(cfg, fp=True).go().result()
    assert c["curve"].index.equals(a["curve"].index)
    # pair direction, as _fx_buys_pair_base
    assert FP.buys_base("USD", "EUR") and not FP.buys_base("EUR", "USD") and FP.buys_base("JPY", "USD") and not FP.buys_base("USD", "JPY") and FP.buys_base("USD", "SEK")

    # the hold: cost 1,000 EUR from USD. Enough without the hold, not with it: refused, no cash moves
    def sim_with(usd, fp):
        s = FP.ForwardSim(dict(cfg, start="2027-01-11", end="2027-01-11"), fp=fp)
        s.cash = {k: 0.0 for k in s.cash}
        s.cash["USD"] = usd
        return s
    day = LS.dnum("2027-01-11")
    probe = sim_with(0.0, False)
    usd_cost = 1000.0 * 1.02 * probe.rate("EUR", day) / probe.rate("USD", day)
    tight = usd_cost * 1.01 + 10.0
    s = sim_with(tight, True)
    assert s.ensure("EUR", 1000.0, day, day, "0900") is False and s.cash["USD"] == tight and s.cash["EUR"] == 0.0
    s = sim_with(tight, False)
    assert s.ensure("EUR", 1000.0, day, day, "0900") is True and s.cash["EUR"] > 1000.0
    s = sim_with(usd_cost * 1.06 + 10.0, True)
    assert s.ensure("EUR", 1000.0, day, day, "0900") is True
    # selling the base side needs no hold: 1% over the cost is enough for USD -> JPY
    jpy_cost = 100000.0 * 1.02 * probe.rate("JPY", day) / probe.rate("USD", day)
    s = sim_with(jpy_cost * 1.01 + 10.0, True)
    assert s.ensure("JPY", 100000.0, day, day, "0900") is True
    print("t9 switches off equals the plain simulator; on, the 5% hold refuses a tight BUY-side conversion and only that OK")


def t10_the_figures():
    # the real account: a deposit is not a gain, nor a withdrawal a loss, and earmarks take no part
    with tempfile.TemporaryDirectory(prefix="mps-fwd-") as d:
        p = Path(d) / "h.json"
        p.write_text(json.dumps({"series": [{"d": "2026-11-05", "nl": 100000, "exc": 0}, {"d": "2026-11-06", "nl": 101000, "exc": 5},
                                            {"d": "2026-11-09", "nl": 111000, "exc": 0}, {"d": "2026-11-10", "nl": 101000, "exc": 0}],
                                 "flows": [{"d": "2026-11-09", "amt": 10000}, {"d": "2026-11-10", "amt": -10000}]}))
        r = SB.real_account(p, "2026-11-05", "2026-11-10")
        assert abs(r["growth"] - ((101000 / 100000) * (111000 / 111000) * (101000 / 101000) - 1)) < 1e-9 and r["worst_fall"] == 0.0, r
        assert r["base_day"] == "2026-11-05" and r["last_day"] == "2026-11-10" and r["flows_in_window"] == 0.0
        r = SB.real_account(p, "2026-11-04", "2026-11-04")
        assert r["available"] is False
        r = SB.real_account(Path(d) / "nothing.json", "2026-11-05", "2026-11-10")
        assert r["available"] is False

    # t = gap / tracking error x sqrt(years), from the data
    rng = np.random.default_rng(7)
    a, b = rng.normal(0.001, 0.01, 522), rng.normal(0.0005, 0.01, 522)
    p = SB.power(a, b, "x")
    d = a - b
    gap, te = d.mean() * 261, d.std(ddof=1) * 261 ** 0.5
    assert abs(p["gap"] - gap) < 1e-5 and abs(p["tracking_error"] - te) < 1e-5 and p["years"] == 2.0
    assert abs(p["t"] - gap / te * 2 ** 0.5) < 1e-2 and abs(p["years_for_t2_at_3_5_points"] - (2 * te / 0.035) ** 2) < 0.1
    assert SB.power(a[:1], b[:1], "x")["t"] is None

    # growth a year, worst fall
    m = SB.curve_metrics([110.0] * 19 + [90.0, 121.0], 100.0, "2026-01-01", "2027-01-01")
    assert abs(m["growth"] - 0.21) < 1e-9 and abs(m["worst_fall"] - (90 / 110 - 1)) < 1e-6 and m["sessions"] == 21 and abs(m["annual"] - 0.21) < 1e-3
    assert SB.curve_metrics([101.0], 100.0, "2026-01-01", "2026-01-02")["annual"] is None

    # the last finished close: a weekday every market had closed, cutting today's unfinished bar
    f = SB.last_finished_close
    assert f(dt.datetime(2026, 11, 5, 9, 0, tzinfo=UTC)) == "2026-11-04" and f(dt.datetime(2026, 11, 5, 22, 0, tzinfo=UTC)) == "2026-11-05"
    assert f(dt.datetime(2026, 11, 7, 12, 0, tzinfo=UTC)) == "2026-11-06" and f(dt.datetime(2026, 11, 9, 8, 0, tzinfo=UTC)) == "2026-11-06"
    print("t10 the real account net of flows, t, growth, worst fall and the last finished close OK")


def t11_execution_never_touches_the_scoreboard():
    pat = re.compile(r"scoreboard|engine[/\\.]forward|(from|import)\s+forward\b|[\"'/\\]forward[/\\.]", re.I)
    assert pat.search("from forward import scoreboard") and pat.search('open("data/forward_scoreboard/readings.json")') and pat.search("import forward.rules")
    assert not pat.search("the forward exchange rate") and not pat.search("forwarded to the bot")
    seen, hits = 0, []
    for root, _, files in os.walk(REPO / "execution"):
        for f in files:
            p = Path(root) / f
            if "__pycache__" in str(p):
                continue
            body = p.read_bytes().decode("utf-8", "replace")
            seen += 1
            if pat.search(body):
                hits += ["%s:%d" % (p.name, i + 1) for i, line in enumerate(body.splitlines()) if pat.search(line)]
    assert seen > 50 and not hits, (seen, hits)
    # and the other way: no machine path, personal address or claude.ai link in what this adds
    bad = re.compile(r"[A-Za-z]:[\\/]Users|/Users/|/home/|claude\.ai|[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[a-z]{2,}")
    roots = [ENGINE / "forward", REPO / FZ.FROZEN_DIR]
    for r in roots:
        for p in sorted(r.rglob("*")):
            if p.is_file() and "__pycache__" not in str(p):
                assert not bad.search(p.read_bytes().decode("utf-8", "replace")), p
    # the code never holds a contact: SEC's name comes from the environment or is generic
    src = (ENGINE / "forward" / "data_store.py").read_text(encoding="utf-8")
    assert 'os.environ.get("SEC_USER_AGENT")' in src
    print("t11 nothing under execution/ imports or reads the scoreboard; no machine paths or addresses in what it adds OK")


if __name__ == "__main__":
    with contextlib.redirect_stderr(io.StringIO()):
        t1_the_freeze_holds()
        t2_nothing_after_the_as_of_date_reaches_back()
        t3_twins_copy_lives_days_and_counts_and_never_move()
        t4_readings_are_append_only()
        t5_a_vanished_name_is_carried_sold_and_flagged()
        t6_sec_out_of_reach_is_said_out_loud()
        t7_a_baseline_with_no_sessions_yet()
        t8_a_bad_fetch_stops_the_reading()
        t9_mode_switches_off_is_the_plain_simulator_on()
        t10_the_figures()
        t11_execution_never_touches_the_scoreboard()
    print("ALL FORWARD-SCOREBOARD TESTS PASS")
