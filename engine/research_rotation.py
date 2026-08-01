"""Best-practice engine for ETF/sector/commodity/bond/FX universes:
CROSS-SECTIONAL MOMENTUM ROTATION (dual momentum) — genuinely different from the
per-product trend engine already tested.

Monthly: rank a class by 6-month momentum; hold the top-K that are ALSO above
their 200-day SMA (absolute filter -> cash otherwise); equal weight; rebalance
monthly; costs on turnover. This is the documented way to extract return from
asset-class baskets. Reported honestly vs the user's targets
(CAGR>50, win>60, DD<30, Calmar>1) — no mining, pre-registered configs.
"""
import json
import numpy as np
import pandas as pd

from config import COST_BP
from data_fetch import fetch_all
from universe import market_of

YEARS = list(range(2016, 2027))

GROUPS = {
    "SECTOR_ETF": ["XLK", "XLE", "XLF", "XLV", "XLI", "XLU", "XLY", "XLP", "XLB",
                   "SMH", "GDX", "ARKK", "KWEB"],
    "BROAD_ETF": ["SPY", "QQQ", "VTI", "VOO", "IWM", "DIA", "EFA", "EEM", "FXI",
                  "EWJ", "VGK"],
    "COMMODITY_ETF": ["GLD", "SLV", "USO", "UNG", "DBC", "DBA", "PPLT", "PALL"],
    "BOND_ETF": ["TLT", "IEF", "SHY", "LQD", "HYG", "AGG", "EMB", "TIP", "ZROZ"],
}


def panel(data, tickers):
    cols = {t: data[t]["Close"] for t in tickers if t in data}
    df = pd.DataFrame(cols).sort_index().ffill()
    return df.dropna(how="all")


def rotation(data, tickers, top_k=3, mom_d=126, cost_bp=10):
    px = panel(data, tickers)
    if px.shape[1] < top_k + 1 or len(px) < 300:
        return None
    rets = px.pct_change().fillna(0)
    sma = px.rolling(200).mean()
    mom = px / px.shift(mom_d) - 1
    month_end = px.resample("ME").last().index
    held, eq, cur = [], [], set()
    mrets = []
    idx = px.index
    # map each day to the most recent completed month-end signal
    sig_dates = [d for d in month_end if d in idx or True]
    weights = pd.Series(0.0, index=px.columns)
    daily = pd.Series(0.0, index=idx)
    turnover_cost = pd.Series(0.0, index=idx)
    last_sel = set()
    me_iter = list(month_end)
    for i, d in enumerate(idx):
        # find latest month-end strictly before d -> its signal governs today
        past_me = [m for m in me_iter if m < d]
        if past_me:
            m = past_me[-1]
            row_mom = mom.loc[:m].iloc[-1]
            row_sma_ok = (px.loc[:m].iloc[-1] > sma.loc[:m].iloc[-1])
            elig = [t for t in px.columns
                    if np.isfinite(row_mom.get(t, np.nan)) and row_mom[t] > 0
                    and bool(row_sma_ok.get(t, False))]
            elig.sort(key=lambda t: row_mom[t], reverse=True)
            sel = set(elig[:top_k])
        else:
            sel = set()
        if sel != last_sel:
            # rebalance cost on the changed fraction
            changed = len(sel.symmetric_difference(last_sel))
            turnover_cost.loc[d] = (changed / max(top_k, 1)) * (cost_bp / 10000) / 2
            last_sel = sel
        if sel:
            daily.loc[d] = rets.loc[d, list(sel)].mean() - turnover_cost.loc[d]
        else:
            daily.loc[d] = -turnover_cost.loc[d]
    eqc = 150000 * (1 + daily).cumprod()
    yrs = (idx[-1] - idx[0]).days / 365.25
    cagr = (eqc.iloc[-1] / eqc.iloc[0]) ** (1 / yrs) - 1
    dd = (eqc / eqc.cummax() - 1).min()
    monthly = eqc.resample("ME").last().pct_change().dropna()
    win = (monthly > 0).mean()
    sharpe = daily.mean() / daily.std() * np.sqrt(252) if daily.std() > 0 else 0
    ye = eqc.groupby(eqc.index.year).agg(["first", "last"])
    yearly = {int(y): r["last"] / r["first"] - 1 for y, r in ye.iterrows()}
    return {"cagr": cagr, "dd": dd, "win": win, "sharpe": sharpe,
            "calmar": cagr / -dd if dd < 0 else 0, "final": eqc.iloc[-1],
            "yearly": yearly, "eq": eqc}


def show(tag, s):
    if s is None:
        print(f"{tag:30s} (insufficient data)"); return None
    reds = [y for y in YEARS if s["yearly"].get(y, 0) < 0]
    hit = ("CAGR>50 " if s["cagr"] > .5 else "") + ("win>60 " if s["win"] > .6 else "") \
        + ("DD<30 " if s["dd"] > -.3 else "") + ("Calmar>1" if s["calmar"] > 1 else "")
    print(f"{tag:30s} win={s['win']*100:4.1f}% CAGR={s['cagr']*100:6.1f}% "
          f"dd={s['dd']*100:6.1f}% Calmar={s['calmar']:.2f} Sharpe={s['sharpe']:.2f} "
          f"| targets met: [{hit.strip() or 'none'}]")
    return s


def main():
    data = fetch_all()
    print("=== cross-sectional momentum rotation, per class (top-3, 6m mom) ===")
    curves = {}
    for name, tk in GROUPS.items():
        s = show(name, rotation(data, tk, top_k=3, cost_bp=10))
        if s:
            curves[name] = s
    # all monitor-only ETFs combined (the real asset-class rotation), top 5
    allt = sum(GROUPS.values(), [])
    s_all = show("ALL_ETF (top5)", rotation(data, allt, top_k=5, cost_bp=10))
    if s_all:
        curves["ALL_ETF"] = s_all
    # sector rotation with top-2 (more concentrated) and 3m momentum variant
    show("SECTOR top2", rotation(data, GROUPS["SECTOR_ETF"], top_k=2, cost_bp=10))
    show("SECTOR 3m-mom top3", rotation(data, GROUPS["SECTOR_ETF"], top_k=3, mom_d=63, cost_bp=10))

    out = {k: {kk: v[kk] for kk in ("cagr", "dd", "win", "calmar", "sharpe")}
           for k, v in curves.items()}
    with open("../data/rotation.json", "w") as f:
        json.dump(out, f, indent=1, default=float)
    print("\nBest achievable is reported above; targets (CAGR>50/win>60/DD<30/Calmar>1)")
    print("are shown per row. saved ../data/rotation.json")


if __name__ == "__main__":
    main()
