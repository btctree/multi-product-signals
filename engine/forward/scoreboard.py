"""The forward scoreboard: fetch, merge, simulate every arm from the frozen start, measure, write one reading, print the table.

Nothing under execution/ imports or reads any of this, and this reads only prices, SEC facts, its own folder and
data/netliq_history.json. No result here reaches the bot.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from . import data_store as DS
from . import live_sim as LS
from . import rules as RU
from . import twins as TW
from .fp_sim import ForwardSim

YEAR_SESSIONS = 261        # the simulator marks NetLiq every weekday
MIN_SESSIONS_ANNUAL = 20   # a year's rate from less than a month is noise on noise: not shown
MAX_FETCH_FAIL = 0.05      # more names than this failing to download stops the reading: that is an outage, not delisting
LABELS = {"LIVE": "LIVE: D3+E1 with tie-break A",
          "D3E1_FILE": "D3+E1, file order (no A)",
          "E_A": "E with A: the OLD model (see note)"}


class ReadingStopped(Exception):
    """A fetch failed badly enough that no honest reading can be made. Nothing is written."""


def last_finished_close(now):
    """The last weekday every market had closed by now. 21:00 UTC is past the US close in daylight and standard time; 22:00 leaves Yahoo an hour."""
    d = now.date() if now.hour >= 22 else now.date() - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d.isoformat()


def _round(x, n=6):
    return None if x is None else round(float(x), n)


def curve_metrics(values, base_value, base_day, end):
    """Growth since the start, growth a year (from 20 sessions), worst fall, for a value series that starts after base_day."""
    v = np.r_[float(base_value), np.asarray(values, dtype=float)]
    years = (pd.Timestamp(end) - pd.Timestamp(base_day)).days / 365.25
    growth = v[-1] / v[0] - 1
    annual = (1 + growth) ** (1 / years) - 1 if len(v) - 1 >= MIN_SESSIONS_ANNUAL and years > 0 else None
    return {"end_value": round(float(v[-1]), 2), "growth": _round(growth), "annual": _round(annual),
            "worst_fall": _round((v / np.maximum.accumulate(v) - 1).min()), "sessions": len(v) - 1}


def returns(values, base_value):
    v = np.r_[float(base_value), np.asarray(values, dtype=float)]
    return v[1:] / v[:-1] - 1


def power(a, b, name):
    """t = gap / tracking error x sqrt(years), from daily returns. None under 2 sessions."""
    d = np.asarray(a) - np.asarray(b)
    if len(d) < 2:
        return {"against": name, "sessions": len(d), "t": None}
    gap, te = d.mean() * YEAR_SESSIONS, d.std(ddof=1) * YEAR_SESSIONS ** 0.5
    years = len(d) / YEAR_SESSIONS
    return {"against": name, "sessions": len(d), "gap": _round(gap), "tracking_error": _round(te), "years": _round(years, 3),
            "t": _round(gap / te * years ** 0.5, 3) if te > 0 else None,
            "years_for_t2_at_this_gap": _round((2 * te / abs(gap)) ** 2, 1) if gap and te > 0 else None,
            "years_for_t2_at_3_5_points": _round((2 * te / 0.035) ** 2, 1) if te > 0 else None}


def install_data(bars, fz):
    """Build the simulator's module-level tables from merged bars."""
    s = fz.settings
    tickers = {t: r for t, r in ((t, RU.build_record(t, bars[t], fz)) for t in fz.sim_tickers if t in bars) if r is not None}
    fx = {c: bars[tk]["Close"].dropna() for c, tk in s["fx_tickers"].items()}
    LS.DATA = {"tickers": tickers, "fx": fx, "order": fz.order}
    LS.prep_indexes()
    rate = bars[s["rate_ticker"]]["Close"].dropna() / 100.0     # FRED is not reachable: the 13-week bill stands in, as in the review
    LS._DFF = (np.array([LS.dnum(d) for d in rate.index], dtype=np.int64), rate.to_numpy(dtype=float))
    return tickers


def run_sim(extra, start, end, fz, cards=None, fp=True):
    cfg = dict(fz.settings["sim"], capital=fz.settings["capital_hkd"], start=start, end=end, **extra)
    return ForwardSim(cfg, fp=fp, cards=cards).go().result()


def twin_tables(fz, tickers, e1, first):
    """[(cards, fpct, short draws)] for every seed, and the LIVE counts they copy."""
    real = LS.DATA["cards"]
    counts = TW.live_counts(real, e1, tickers, first)
    elig = TW.eligible(tickers, list(counts), fz.order)
    tw = fz.settings["twins"]
    out = []
    for k in range(tw["n"]):
        cards, fpct, short = TW.twin_table(k, counts, elig, fz.tickers, tw["entropy"])
        for key, lst in real.items():       # crypto cards are unchanged: never bought, they only take list room
            if key[1] >= first.get(key[0], 10 ** 9):
                x = [c for c in lst if c[0] == "CRY"]
                if x:
                    cards.setdefault(key, []).extend(x)
        out.append((cards, fpct, short))
    return out, counts


def arm_metrics(res, capital, base, end):
    t = res["trades"]
    m = curve_metrics(res["curve"].to_numpy(dtype=float), capital, base, end)
    m.update(closed_trades=int(len(t)), open_positions=int(len(res["open"])), win_rate=_round((t["ret"].to_numpy() > 0).mean()) if len(t) else None)
    return m


def flat_metrics(capital):
    return {"end_value": round(float(capital), 2), "growth": 0.0, "annual": None, "worst_fall": 0.0, "sessions": 0,
            "closed_trades": 0, "open_positions": 0, "win_rate": None}


def vanish_flags(res, stale):
    """(sold because the data ended, still held in a name with no recent bars)."""
    t, o = res["trades"], res["open"]
    sold = []
    if len(t):
        for _, x in t[t["reason"].str.startswith("delisted")].iterrows():
            sold.append({"ticker": x["ticker"], "sold_on": str(x["exit_date"].date()), "last_seen_price": _round(x["exit_px"])})
    held = sorted(set(o["ticker"]) & set(stale)) if len(o) else []
    return sold, held


def benchmark(bars, usd, base_day, end, capital):
    """HK$ value of the fund's total return from the last close on or before base_day, no costs. (metrics, series), or None."""
    if bars is None:
        return None
    tr = RU.total_return(bars)
    hkd = (tr * usd.reindex(tr.index, method="ffill")).dropna()
    before = hkd[hkd.index <= pd.Timestamp(base_day)]
    if not len(before):
        return None
    cur = hkd[(hkd.index > pd.Timestamp(base_day)) & (hkd.index <= pd.Timestamp(end))]
    val = capital * cur / before.iloc[-1]
    return curve_metrics(val.to_numpy(), capital, base_day, end), val


def real_account(path, base_day, end):
    """The real account from data/netliq_history.json, net of flows, as the dashboard chains it: nl / (previous nl + money in or out).
    Earmarked cash takes no part: NetLiq is already net of it. It did not start in cash, so it compares only roughly."""
    try:
        h = json.loads(Path(path).read_text(encoding="utf-8"))
        s = sorted((r["d"], float(r["nl"])) for r in h["series"] if float(r.get("nl") or 0) > 0)
        flows = [(f["d"], float(f.get("amt") or 0)) for f in h.get("flows", [])]
    except Exception as e:
        return {"available": False, "why": repr(e)[:120]}
    s = [x for x in s if x[0] <= end]
    points = [x for x in s if x[0] <= base_day][-1:] + [x for x in s if x[0] > base_day]
    if not points:
        return {"available": False, "why": "no NetLiq rows in the window"}
    idx, vals = 1.0, []
    for (p, a), (c, b) in zip(points, points[1:]):
        idx *= b / (a + sum(x for d, x in flows if p < d <= c))
        vals.append(idx)
    m = curve_metrics(vals, 1.0, points[0][0], points[-1][0])
    m.pop("end_value")
    m.update(available=True, base_day=points[0][0], last_day=points[-1][0], base_netliq=points[0][1],
             flows_in_window=round(sum(x for d, x in flows if points[0][0] < d <= points[-1][0]), 2))
    return m


def _canon(x):
    return json.dumps(x, sort_keys=True, separators=(",", ":"))


def seal(reading, previous):
    reading["prev_sha256"] = hashlib.sha256(_canon(previous).encode()).hexdigest() if previous is not None else None
    reading["sha256"] = hashlib.sha256(_canon({k: v for k, v in reading.items() if k != "sha256"}).encode()).hexdigest()
    return reading


def verify_chain(readings):
    """Raise when any earlier reading was edited or removed."""
    for i, r in enumerate(readings):
        if r.get("sha256") != hashlib.sha256(_canon({k: v for k, v in r.items() if k != "sha256"}).encode()).hexdigest():
            raise ValueError("reading %d was edited" % (i + 1))
        want = hashlib.sha256(_canon(readings[i - 1]).encode()).hexdigest() if i else None
        if r.get("prev_sha256") != want:
            raise ValueError("reading %d does not follow reading %d" % (i + 1, i))


def load_readings(path):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def append_reading(path, reading):
    """One more reading at the end of the file. Earlier readings are checked, then kept byte for byte."""
    p = Path(path)
    body = json.dumps(reading, indent=1)
    if p.exists():
        old = p.read_bytes().decode("utf-8").replace("\r\n", "\n")
        verify_chain(json.loads(old))
        if not old.endswith("\n]\n"):
            raise ValueError("readings file does not end as written")
        text = old[:-3] + ",\n" + body + "\n]\n"
    else:
        text = "[\n" + body + "\n]\n"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, p)


def run(fz, now, readings_path, store_dir, netliq_path, bars_fn=DS.yahoo_bars, facts_fn=None, start=None, write=True, commit=None, manifest=None):
    """One reading. start= is the pipeline test: any past start, and nothing is ever written. Returns {"reading", "text", "written"}."""
    s = fz.settings
    if start is not None and write:
        raise ValueError("a past start is a pipeline test and is never written")
    first = start or s["first_session"]
    end = last_finished_close(now)
    capital = float(s["capital_hkd"])
    earlier = load_readings(readings_path)
    verify_chain(earlier)
    if write and any(r["last_close"] == end for r in earlier):
        return {"reading": None, "written": False, "text": "A reading for the close of %s already exists. Nothing written." % end}

    # ---- prices
    extra = list(s["fx_tickers"].values()) + [s["rate_ticker"]] + s["benchmarks"]
    wanted = list(dict.fromkeys(fz.sim_tickers + extra))
    fresh, why = DS.fetch_all(wanted, s["warmup_from"], now, bars_fn)
    failed = {t: w for t, w in why.items() if w.startswith("fetch failed")}
    if len(failed) > MAX_FETCH_FAIL * len(wanted):
        raise ReadingStopped("%d of %d downloads failed, for example %s: %s. Nothing written." % (len(failed), len(wanted), *next(iter(failed.items()))))
    no_fx = [t for t in s["fx_tickers"].values() if t not in fresh]
    if no_fx:
        raise ReadingStopped("no fresh exchange rates for %s (%s). Nothing written." % (no_fx, [why.get(t) for t in no_fx]))
    stored = DS.load_store(store_dir)
    bars, carried, missing = DS.merge(fresh, stored, wanted, why, end)
    if s["rate_ticker"] not in bars:
        raise ReadingStopped("no interest-rate series (%s) fresh or stored. Nothing written." % s["rate_ticker"])
    in_list = set(fz.sim_tickers)
    stale = sorted(t for t, b in bars.items() if t in in_list and t not in carried and (pd.Timestamp(end) - b.index[-1]).days > 7)
    carried_in_list = {t: w for t, w in carried.items() if t in in_list}

    # ---- arms
    base_day = (pd.Timestamp(first) - pd.Timedelta(days=1)).date().isoformat()
    sessions = int(np.busday_count(first, (pd.Timestamp(end) + pd.Timedelta(days=1)).date().isoformat())) if end >= first else 0
    arms, twins, flags, ret, e1_stat, short, picks, n_days = {}, [], {}, {}, None, 0, 0, 0
    if sessions:
        tickers = install_data(bars, fz)
        firsts = TW.window_keys(LS.DATA["gdates"], LS.dnum(first))
        facts = facts_fn or (lambda t: DS.sec_facts(t, fz.cik))
        e1, e1_stat = RU.e1_verdicts(tickers, firsts.get("US", LS.dnum(first)), facts)
        LS.E1 = {"fixed": e1}
        tables, counts = twin_tables(fz, tickers, e1, firsts)
        picks, n_days = int(sum(a + b for a, b in counts.values())), len(counts)
        for name, extra_cfg in s["arms"].items():
            res = run_sim(extra_cfg, first, end, fz)
            arms[name] = arm_metrics(res, capital, base_day, end)
            ret[name] = returns(res["curve"].to_numpy(dtype=float), capital)
            sold, held = vanish_flags(res, set(carried_in_list) | set(stale))
            flags[name] = {"sold_at_last_seen_price": sold, "held_no_recent_bars": held}
            curve_days = res["curve"].index
        twin_ret = []
        tw_sold, tw_held = 0, 0
        for cards, fpct, sh in tables:
            res = run_sim({"variant": s["twins"]["variant"], "rank": s["twins"]["rank"], "fpct": fpct}, first, end, fz, cards=cards)
            twins.append(arm_metrics(res, capital, base_day, end))
            twin_ret.append(returns(res["curve"].to_numpy(dtype=float), capital))
            sold, held = vanish_flags(res, set(carried_in_list) | set(stale))
            tw_sold, tw_held = tw_sold + len(sold), tw_held + len(held)
            short += sh
        ret["TWIN_AVERAGE"] = np.mean(np.vstack(twin_ret), axis=0)      # a day-by-day median would be biased low: daily returns are skewed
        flags["TWINS"] = {"sold_at_last_seen_price_count": tw_sold, "held_no_recent_bars_count": tw_held}
    else:
        e1_stat = {"ran": "no sessions yet"}
        arms = {name: flat_metrics(capital) for name in s["arms"]}
        twins = [flat_metrics(capital) for _ in range(s["twins"]["n"])]
        curve_days = None

    # ---- scale: the funds and the real account
    usd = bars[s["fx_tickers"]["USD"]]["Close"].dropna()
    funds = {}
    for b in s["benchmarks"]:
        got = benchmark(bars.get(b), usd, base_day, end, capital)
        funds[b] = got[0] if got else None
        if got and b == "SPY" and curve_days is not None:
            ret["SPY"] = returns(got[1].reindex(curve_days, method="ffill").fillna(capital).to_numpy(), capital)
    real = real_account(netliq_path, base_day, end)

    # ---- how little this can show
    power_lines = []
    if "LIVE" in ret:
        for key, name in (("TWIN_AVERAGE", "the average of the twins"), ("D3E1_FILE", "D3+E1, file order"), ("E_A", "E with A (old model)"), ("SPY", "SPY in HKD")):
            if key in ret:
                power_lines.append(power(ret["LIVE"], ret[key], name))
    beat = sum(arms["LIVE"]["growth"] > t["growth"] for t in twins)
    med = {k: _round(np.median([t[k] for t in twins if t[k] is not None])) if any(t[k] is not None for t in twins) else None
           for k in ("end_value", "growth", "annual", "worst_fall", "closed_trades", "open_positions", "win_rate")}
    fx_jumps = []
    for c, tk in s["fx_tickers"].items():
        x = bars[tk]["Close"].dropna()
        x = x[(x.index >= pd.Timestamp(first))]
        if len(x) > 1:
            fx_jumps += ["%s %s %+.1f%%" % (c, d.date(), 100 * v) for d, v in x.pct_change().dropna().items() if abs(v) > 0.04]

    reading = {
        "reading": len(earlier) + 1, "kind": "baseline" if not sessions else "reading",
        "run_utc": now.strftime("%Y-%m-%d %H:%M"), "code_commit": commit, "frozen_manifest_sha256": manifest,
        "freeze_commit": s["freeze_commit"], "first_session": first, "last_close": end, "sessions": sessions,
        "years": _round((pd.Timestamp(end) - pd.Timestamp(base_day)).days / 365.25, 3) if sessions else 0.0,
        "capital_hkd": capital, "arms": arms,
        "twins": {"n": len(twins), "median": med, "live_beat": int(beat) if sessions else None, "days_with_picks": n_days, "picks": picks,
                  "short_draws": short, "growth_by_seed": [t["growth"] for t in twins], "worst_fall_by_seed": [t["worst_fall"] for t in twins]},
        "benchmarks": funds, "real_account": real, "power": power_lines, "e1": e1_stat,
        "data": {"names_in_list": len(fz.sim_tickers), "names_with_bars": sum(t in bars for t in fz.sim_tickers),
                 "carried_count": len(carried_in_list), "carried": carried_in_list,
                 "other_carried": {t: w for t, w in carried.items() if t not in in_list},
                 "no_recent_bars": stale, "missing": [t for t in missing if t in in_list],
                 "download_failures": len(failed), "fx_jumps_over_4pct": fx_jumps, "flags": flags},
    }
    written = False
    if write:
        close_only = set(extra)
        info = DS.write_store(store_dir, bars, stored, end, close_only)
        info["store_total_bytes"] = DS.store_bytes(store_dir)
        reading["data"]["store"] = info
        seal(reading, earlier[-1] if earlier else None)
        append_reading(readings_path, reading)
        written = True
    return {"reading": reading, "written": written, "text": render(reading, pipeline=start is not None)}


# ------------------------------------------------------------------ the table
def _pct(x, n=1):
    return "n/a" if x is None else "%+.*f%%" % (n, 100 * x)


def _row(label, m, width=46):
    trades = "" if m.get("closed_trades") is None else "%d / %d" % (m["closed_trades"], m.get("open_positions") or 0)
    win = "" if "win_rate" not in m else ("n/a" if m["win_rate"] is None else "%.0f%%" % (100 * m["win_rate"]))
    return "%-*s %11s %8s %8s %8s %9s %6s" % (width, label[:width], "{:,.0f}".format(m["end_value"]) if m.get("end_value") is not None else "",
                                              _pct(m["growth"]), _pct(m["annual"]), _pct(m["worst_fall"]), trades, win)


def render(r, pipeline=False):
    L = []
    if pipeline:
        L += ["*** PIPELINE TEST. NOT A READING. Past prices the rules were tuned on; nothing here was written. ***", ""]
    L += ["FORWARD SCOREBOARD, pipeline test" if pipeline else "FORWARD SCOREBOARD, reading %d (%s)" % (r["reading"], r["kind"]),
          "Frozen at %s on 2026-11-05. First session %s, last finished close %s: %d sessions, %.2f years."
          % (r["freeze_commit"], r["first_session"], r["last_close"], r["sessions"], r["years"]),
          "Every arm starts in cash at HK$%s with the same mechanics. All simulated, except the real account." % "{:,.0f}".format(r["capital_hkd"]), ""]
    L.append("%-46s %11s %8s %8s %8s %9s %6s" % ("Arm", "End HK$", "Growth", "A year", "Worst", "Trades", "Win"))
    L.append("%-46s %11s %8s %8s %8s %9s %6s" % ("", "", "", "", "fall", "closed/open", "rate"))
    for k, v in r["arms"].items():
        L.append(_row(LABELS.get(k, k), v))
    tw = r["twins"]
    L.append(_row("Random twins, median of %d seeds" % tw["n"], tw["median"]))
    if tw["live_beat"] is not None:
        g = tw["growth_by_seed"]
        L.append("    LIVE beat %d of %d seeds on growth. Seeds ranged %s to %s. Twins took the same %d picks on the same %d days as LIVE%s."
                 % (tw["live_beat"], tw["n"], _pct(min(g)), _pct(max(g)), tw["picks"], tw["days_with_picks"],
                    "" if not tw["short_draws"] else " (%d draws short: too few uptrend names)" % tw["short_draws"]))
    L.append("    Note: E with A is the dips alone, the model before D3+E1. It appears only here, under the owner's exception.")
    L += ["", "For scale, HKD total return from the same start, no costs:"]
    for k, v in r["benchmarks"].items():
        L.append(_row(k, v) if v else "%-46s not available" % k)
    ra = r["real_account"]
    if ra.get("available"):
        L.append(_row("REAL account, net of flows", dict(ra, end_value=None)))
        L.append("    From %s to %s. It did not start in cash, so this compares only roughly. Flows in the window HK$%s."
                 % (ra["base_day"], ra["last_day"], "{:,.0f}".format(ra["flows_in_window"])))
    else:
        L.append("REAL account: not available (%s)" % ra.get("why"))

    L += ["", "HOW LITTLE THIS CAN SHOW. t = gap / tracking error x sqrt(years). About 2 is the usual bar for one test, about 3.3 after 100 tries."]
    lines = [p for p in r["power"] if p.get("t") is not None]
    if lines:
        for p in lines:
            L.append("  LIVE against %-32s gap %+6.1f points a year, tracking error %5.1f, %.2f years: t = %+.2f. t=2 needs about %s years at this gap, %s at a 3.5-point gap."
                     % (p["against"], 100 * p["gap"], 100 * p["tracking_error"], p["years"], p["t"],
                        "n/a" if p["years_for_t2_at_this_gap"] is None else "%.0f" % p["years_for_t2_at_this_gap"], "%.0f" % p["years_for_t2_at_3_5_points"]))
    else:
        L.append("  Too few sessions to measure a tracking error, so no t can be computed from this data yet.")
    L += ["  The review's own scale: a 3.5-point gap takes about 18 years to reach t=2, about 72 if the true gap is half.",
          "  So this is a check that the rules still behave, not evidence for or against them. A good or bad table at this length proves nothing."]

    L += ["", "E1: " + _e1_line(r["e1"])]
    d = r["data"]
    L.append("Dead names: %d of %d names had to be carried from the store%s." % (d["carried_count"], d["names_in_list"], ": " + ", ".join(sorted(d["carried"])) if d["carried"] else ""))
    if d["other_carried"]:
        L.append("  Carried besides the list (rates, funds): " + ", ".join(sorted(d["other_carried"])))
    if d["no_recent_bars"]:
        L.append("  No bars in the last 7 days: " + ", ".join(d["no_recent_bars"]))
    if d["missing"]:
        L.append("  No bars at all, fresh or stored: " + ", ".join(d["missing"]))
    for k, fl in d["flags"].items():
        if k == "TWINS":
            if fl["sold_at_last_seen_price_count"] or fl["held_no_recent_bars_count"]:
                L.append("  Twins: %d sales at a last seen price, %d holdings with no recent bars, across all seeds." % (fl["sold_at_last_seen_price_count"], fl["held_no_recent_bars_count"]))
            continue
        for x in fl["sold_at_last_seen_price"]:
            L.append("  FLAG %s sold %s on %s at its last seen price %s: its data ended." % (k, x["ticker"], x["sold_on"], x["last_seen_price"]))
        if fl["held_no_recent_bars"]:
            L.append("  FLAG %s still holds names with no recent bars, marked at their last price: %s" % (k, ", ".join(fl["held_no_recent_bars"])))
    if d["fx_jumps_over_4pct"]:
        L.append("  Exchange rate jumps over 4% in a day, worth a look (not repaired): " + ", ".join(d["fx_jumps_over_4pct"]))
    if d.get("store"):
        st = d["store"]
        L.append("Store: %s, %d rows, %.2f MB (whole store %.2f MB)." % (st["file"], st["rows"], st["bytes"] / 1e6, st["store_total_bytes"] / 1e6))
    if pipeline:
        L += ["", "*** PIPELINE TEST. NOT A READING. These figures are on the tuned history and say nothing about the rules. ***"]
    return "\n".join(L)


def _e1_line(e):
    ran = e.get("ran")
    if ran in ("no sessions yet", "no US dips in the window"):
        return ran + "; E1 had nothing to judge."
    head = "ran on %d US dips: %d kept, %d blocked, %d unscored and kept." % (e["cards"], e["scored"] - e["blocked"], e["blocked"], e["unscored_kept"])
    if ran == "yes":
        return head
    if ran == "NO":
        return ("*** E1 DID NOT RUN: SEC could not be reached for any of the %d US names with dips. LIVE and D3+E1 above are D3+E (no earnings filter), "
                "not the live model, and the twins copy those picks. Read this reading as that, or read again later. ***" % len(e["names_failed"]))
    return ("*** E1 RAN ONLY IN PART: SEC failed for %d names (%s), whose dips were kept unjudged. LIVE and D3+E1 above are not exactly the live model. *** "
            % (len(e["names_failed"]), ", ".join(e["names_failed"][:10]))) + head
