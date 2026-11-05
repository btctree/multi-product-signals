"""From daily bars to the arrays the simulator reads: the signal rules and E1. Frozen.

The logic of the holdout's s30_build.py and engine/model_d3e1.py at commit aace4aa7. One difference in how prices arrive: bars
are held AS TRADED, with dividends, splits and Yahoo's own back-adjustment steps as events (data_store.py), and the factors are
worked out here. A name carried from the store therefore goes down the same road as a fresh one, and bars saved months apart
join with no false jump.
"""
import numpy as np
import pandas as pd

from . import model_d3e1_frozen as MD
from .frozen import FROZEN_PARAMS
from .indicators_frozen import add_features

P = FROZEN_PARAMS["signal"]
SHARED = {"PA": "EURONEXT", "AS": "EURONEXT", "BR": "EURONEXT", "LS": "EURONEXT"}


def group_of(t):
    t = t.upper()
    if t.endswith("-USD"):
        return "CRYPTO"
    if "." in t:
        s = t.rsplit(".", 1)[1]
        return SHARED.get(s, s)
    return "US"


def ccy_of(t):
    if t.endswith(".HK"):
        return "HKD"
    if t.endswith(".T"):
        return "JPY"
    suf = t.rsplit(".", 1)[1] if "." in t else ""
    return {"L": "GBP", "SW": "CHF", "CO": "DKK", "ST": "SEK", "OL": "NOK"}.get(suf, "EUR" if suf else "USD")


def enterable(t, m, lot):
    if m == "CRYPTO":
        return True
    if m == "US":
        return not any(c in t for c in ".=^-")
    if m == "EU":
        return t.rsplit(".", 1)[1] not in ("L", "SW", "CO", "ST", "OL")
    if m == "HK":
        return lot is not None
    return m == "JP"


def momentum_score(mom):
    return int(round(max(0.0, min(1.0, mom / 0.50)) * 100))


def later_product(x):
    """out[i] = the product of x[k] for every k after i."""
    out = np.ones(len(x))
    acc = 1.0
    for i in range(len(x) - 1, -1, -1):
        out[i] = acc
        acc *= x[i]
    return out


def split_factors(splits):
    """F[i] = the product of the split ratios after bar i: as traded = split-adjusted x F."""
    return later_product(np.where(splits > 0, splits, 1.0))


def split_adjusted(b):
    """As-traded bars -> (split-adjusted frame with its dividend back-adjustment ratio, F), rows with no usable close dropped."""
    b = b[b["Close"] > 0].dropna(subset=["Close"])
    F = split_factors(b["Splits"].fillna(0.0).to_numpy(dtype=float))
    r = pd.DataFrame({c: b[c].to_numpy(dtype=float) / F for c in ("Open", "High", "Low", "Close")}, index=b.index)
    r["Dividends"] = b["Dividends"].fillna(0.0).to_numpy(dtype=float) / F
    r["ratio"] = later_product(b["AdjStep"].fillna(1.0).to_numpy(dtype=float))
    return r, F


def total_return(b):
    """Close x back-adjustment: a total-return series with its ratios right. For the benchmarks."""
    r, _ = split_adjusted(b)
    return r["Close"] * r["ratio"]


def build_record(t, b, fz):
    """One name's arrays, as s30_build.py builds them, or None under 100 bars."""
    m = fz.market_of[t]
    r, F = split_adjusted(b)
    if len(r) < 100:
        return None
    div, ratio = r["Dividends"].to_numpy(), r["ratio"]
    a = pd.DataFrame({"Open": r["Open"] * ratio, "High": r["High"] * ratio, "Low": r["Low"] * ratio, "Close": r["Close"] * ratio, "Volume": 0.0})
    f = add_features(a, P)
    n = len(a)
    raw_c = r["Close"].ffill().to_numpy()
    raw_o = r["Open"].ffill().to_numpy()
    raw_l = r["Low"].ffill().to_numpy()
    adj_c = a["Close"].to_numpy()
    act_c, act_o, act_l = raw_c * F, raw_o * F, raw_l * F
    k = act_c / adj_c
    mom0 = f["mom_90"].to_numpy()
    mom = np.where(np.isfinite(mom0), mom0, 0.0)
    st, sf = f["sma_trend"].to_numpy(), f["sma_fast"].to_numpy()
    atr, rsi, hi = f["atr"].to_numpy(), f["rsi"].to_numpy(), f["hi_52w"].to_numpy()
    hi_eff = np.where(np.isfinite(hi), hi, adj_c)
    atr_eff = np.where(np.isfinite(atr), atr, adj_c * 0.02)
    long_enough = np.arange(n) >= P["min_bars"] - 1
    with np.errstate(invalid="ignore"):
        up = np.isfinite(st) & (adj_c > st) & (sf > st)
        if m == "CRYPTO":
            E = np.zeros(n, bool)
            C = up & long_enough
        else:
            E = (up & (rsi < P["rsi_entry"]) & (mom > P["min_mom"]) & (adj_c >= P["near_high"] * hi_eff)
                 & (atr_eff / adj_c > FROZEN_PARAMS["MIN_ATR_BY_MARKET"].get(m, P["min_atr_pct"])) & long_enough)
            C = np.zeros(n, bool)
    lot = fz.hk_lots.get(t) if m == "HK" else 100 if m == "JP" else 1
    rec = {"t": t, "m": m, "ccy": ccy_of(t), "src": "yahoo", "dates": a.index.values.astype("datetime64[D]"),
           "o": act_o, "l": act_l, "c": act_c, "raw_c": raw_c, "F": F, "div": div, "adj_c": adj_c,
           "sma200_act": st * k, "atr_act": atr_eff * k, "E": E, "CRY": C,
           "score": np.array([momentum_score(x) for x in mom], dtype=np.int16), "mom": mom,
           "group": group_of(t), "order": fz.order[t], "enterable": enterable(t, m, lot), "lot": lot,
           "removed": None, "added": None,            # the list is frozen: no name joins or leaves it
           "ELIG": long_enough & np.isfinite(st), "UP": long_enough & up}
    if m != "CRYPTO":
        rec["D3"] = MD.d3_series(f, m) & long_enough
    return rec


def e1_verdicts(tickers, first_day, facts_for):
    """E1 on every US E card from day number first_day on, strictly as of the signal day (filings dated <= it).

    facts_for(ticker) -> a facts frame, or None when SEC has none (unscored, so the dip is kept, as in the bot); it raises when
    SEC cannot be reached. Five failures running stop the fetching: the rest are counted as failed, not as unscored.
    Returns ({ticker: {bar: keep?}}, status)."""
    e1, stat = {}, {"cards": 0, "scored": 0, "blocked": 0, "unscored_kept": 0, "names_failed": []}
    streak, asked = 0, 0
    for t, r in tickers.items():
        if r["m"] != "US" or "E" not in r:
            continue
        idx = np.flatnonzero(r["E"] & (r["dates"].astype(np.int64) >= first_day))
        if not len(idx):
            continue
        stat["cards"] += len(idx)
        asked += 1
        if streak >= 5:
            stat["names_failed"].append(t)
            continue
        try:
            df = facts_for(t)
            streak = 0
        except Exception:
            streak += 1
            stat["names_failed"].append(t)
            continue
        p_ = MD.prep_facts(df) if df is not None else None
        res, cache = {}, {}
        for i in idx:
            d = pd.Timestamp(r["dates"][i])
            if p_ is None:
                stat["unscored_kept"] += 1
                continue
            sub = p_[p_.filed <= d]
            key = len(sub)
            if key not in cache:
                cache[key] = MD.events5(sub)
            v = MD.e1_decision(sub, cache[key], d)
            if v["decision"] == "unscored":
                stat["unscored_kept"] += 1
                continue
            stat["scored"] += 1
            ok = v["decision"] == "keep"
            stat["blocked"] += (not ok)
            res[int(i)] = bool(ok)
        e1[t] = res
    failed = len(stat["names_failed"])
    stat["ran"] = "no US dips in the window" if not asked else "yes" if not failed else "PARTLY" if failed < asked else "NO"
    return e1, stat
