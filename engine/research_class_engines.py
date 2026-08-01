"""4/5: match engine to PRODUCT TYPE. Test a trend-following engine (the natural
fit) on the currently monitor-only liquid classes, to see if any deserve to
become a tradeable sleeve. Then test whether adding the best as a small
'diversifier' sleeve improves the live D product within the <=30% DD gate.

Pre-registered, gated, no mining. Trend engines are expected to have ~40-50%
win (right less often, win bigger) — judged on CAGR/DD/Calmar of the BLEND,
not win rate in isolation.
"""
import json

import engine_rr
from engine_rr import run
from research_r7_trend import run_trend
from research_r8_combo import combo
from data_fetch import fetch_all
from universe import market_of

engine_rr.run.return_curve = True
YEARS = list(range(2016, 2027))

GROUPS = {
    "BROAD_ETF": ["SPY", "QQQ", "VTI", "VOO", "IWM", "DIA", "EFA", "EEM", "FXI",
                  "KWEB", "EWJ", "VGK"],
    "SECTOR_ETF": ["XLK", "XLE", "XLF", "XLV", "XLI", "XLU", "XLY", "XLP", "XLB",
                   "SMH", "GDX", "ARKK"],
    "COMMODITY_ETF": ["GLD", "SLV", "USO", "UNG", "DBC", "DBA", "PPLT", "PALL"],
    "BOND_ETF": ["TLT", "IEF", "SHY", "LQD", "HYG", "AGG", "EMB", "TIP", "ZROZ"],
    "FX": [t for t in [] ],  # filled below
}


def sub(data, tickers):
    return {t: d for t, d in data.items() if t in tickers}


def line(tag, s):
    dd = s["dd"]
    calmar = s["cagr"] / -dd if dd < 0 else 0
    print(f"  {tag:16s} n={s['trades']:4d} win={s['win']*100:4.1f}% "
          f"CAGR={s['cagr']*100:6.1f}% dd={dd*100:6.1f}% Calmar={calmar:.2f}")
    return s


def blendline(tag, b, base_calmar):
    dd = b["dd"]; calmar = b["cagr"] / -dd if dd < 0 else 0
    reds = [y for y in YEARS if b["yearly"].get(y, 0) < 0]
    ok = "PASS" if dd >= -0.30 else "DD-BREACH"
    mark = "  <-- beats D" if (calmar > base_calmar and dd >= -0.30) else "  (no better)"
    print(f"{tag:30s} win={b['win']*100:4.1f}% CAGR={b['cagr']*100:5.1f}% "
          f"dd={dd*100:6.1f}% Calmar={calmar:.2f} 150k->{b['final']:,.0f} [{ok}]{mark}")
    print("   yr: " + " ".join(f"{y}:{b['yearly'].get(y,0)*100:+.0f}" for y in YEARS)
          + f"  reds={reds}")


def main():
    data = fetch_all()
    GROUPS["FX"] = [t for t in data if market_of(t) == "FX"]

    # ---- Step 1: trend engine per class (regime entry + SMA50 exit / 4ATR trail) ----
    print("=== trend engine per product class (standalone, 3-4 slots) ===")
    curves = {}
    for name, tickers in GROUPS.items():
        d = sub(data, tickers)
        if len(d) < 3:
            print(f"  {name}: too few with data ({len(d)})"); continue
        s, eq, tr = run_trend(d, N=55, K=4.0, slots=min(4, max(2, len(d)//3)),
                              entry_mode="regime", exit_sma="sma_fast")
        curves[name] = (s, eq)
        line(name, s)

    # also a diversified "managed-futures / CTA" basket: commodities+bonds+FX together
    cta_tk = GROUPS["COMMODITY_ETF"] + GROUPS["BOND_ETF"] + GROUPS["FX"]
    s_cta, eq_cta, _ = run_trend(sub(data, cta_tk), N=55, K=4.0, slots=4,
                                 entry_mode="regime", exit_sma="sma_fast")
    curves["CTA_BASKET"] = (s_cta, eq_cta)
    line("CTA_BASKET", s_cta)

    # ---- Step 2: baseline D + best diversifier sleeve ----
    from research_breakout_sleeve import eq_curves
    (s_dip13, eq_dip13), _ = eq_curves(data, 13, 1)
    s_cr, eq_cr, _ = run_trend({t: d for t, d in data.items()
                                if market_of(t) == "CRYPTO"},
                               N=55, K=4.0, slots=2, entry_mode="regime",
                               exit_sma="sma_fast")
    base = combo({"DIP": eq_dip13, "CRY": eq_cr},
                 {"DIP": (s_dip13["win"], s_dip13["trades"]),
                  "CRY": (s_cr["win"], s_cr["trades"])},
                 {"DIP": .70, "CRY": .30})
    base_calmar = base["cagr"] / -base["dd"]
    print("\n=== D baseline vs D + a diversifier sleeve (10% carved from dip) ===")
    blendline("D baseline (70dip/30cry)", base, base_calmar)

    for name in ("BROAD_ETF", "SECTOR_ETF", "CTA_BASKET"):
        if name not in curves:
            continue
        s_x, eq_x = curves[name]
        # 60 dip / 10 diversifier / 30 crypto
        b = combo({"DIP": eq_dip13, "X": eq_x, "CRY": eq_cr},
                  {"DIP": (s_dip13["win"], s_dip13["trades"]),
                   "X": (s_x["win"], s_x["trades"]),
                   "CRY": (s_cr["win"], s_cr["trades"])},
                  {"DIP": .60, "X": .10, "CRY": .30})
        blendline(f"D + 10% {name}", b, base_calmar)

    out = {"per_class": {k: {"win": v[0]["win"], "cagr": v[0]["cagr"],
                             "dd": v[0]["dd"], "trades": v[0]["trades"]}
                         for k, v in curves.items()}}
    with open("../data/class_engines.json", "w") as f:
        json.dump(out, f, indent=1, default=float)
    print("\nsaved ../data/class_engines.json")


if __name__ == "__main__":
    main()
