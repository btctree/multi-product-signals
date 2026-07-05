"""Full performance scorecard for the candidate engines, so the user can decide.
Overall: CAGR, total return, win%, R:R, PF, Sharpe, Calmar, maxDD.
Per year: return% and win%. Writes PERFORMANCE.md + prints.
"""
from engine_rr import run, FULL_YEARS
from options_model import run_calls
from data_fetch import fetch_all

YEARS = list(range(2016, 2027))

CANDIDATES = [
    ("C1 Balanced (Q6 spot)", lambda d: run(d, K=3.5, K_tight=2.0, rsi_entry=25,
                                            near_high=0.88, slots=7)),
    ("C2 Max-R:R (A1 spot)", lambda d: run(d, K=3.0, rsi_entry=15, slots=7)),
    ("C3 Max-R:R in DD (A5 spot)", lambda d: run(d, K=3.0, rsi_entry=15,
                                                min_mom=0.05, slots=7)),
    ("C4 Full deep-ITM CALLS", lambda d: run_calls(d, K=3.0, rsi_entry=15, slots=7,
                                                   use_options=True, opt_frac=1.0,
                                                   itm=0.10)),
]


def fmt_overall(name, s):
    return (f"| {name} | {s['win']*100:.1f}% | {s['rr']:.2f} | {s['pf']:.2f} | "
            f"{s['cagr']*100:.1f}% | {s['total_ret']*100:,.0f}% | "
            f"{s['final']:,.0f} | {s['dd']*100:.1f}% | {s['sharpe']:.2f} | "
            f"{s['calmar']:.2f} |")


def main():
    data = fetch_all()
    results = [(name, fn(data)) for name, fn in CANDIDATES]

    lines = ["# Candidate Engine Scorecard (11.2y, net of costs, honest fills)",
             "",
             "*C4 (calls) is a MODELED deep-ITM overlay, not a chain backtest — "
             "see options_model.py banner. maxDD is the peak-to-trough equity fall.*",
             "",
             "## Overall",
             "",
             "| Engine | Win | R:R | PF | CAGR | Total ret | 150k → | maxDD | Sharpe | Calmar |",
             "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|"]
    for name, s in results:
        lines.append(fmt_overall(name, s))

    # per-year return table
    lines += ["", "## Return per year (%)", "",
              "| Year | " + " | ".join(n.split()[0] for n, _ in results) + " |",
              "|---|" + "|".join("--:" for _ in results) + "|"]
    for y in YEARS:
        row = [f"{results[i][1]['yearly'].get(y, float('nan'))*100:+.0f}"
               if y in results[i][1]['yearly'] else "—" for i in range(len(results))]
        lines.append(f"| {y} | " + " | ".join(row) + " |")

    # per-year win-rate table
    lines += ["", "## Win rate per year (%)", "",
              "| Year | " + " | ".join(n.split()[0] for n, _ in results) + " |",
              "|---|" + "|".join("--:" for _ in results) + "|"]
    for y in YEARS:
        row = []
        for i in range(len(results)):
            yw = results[i][1].get("yearly_win", {})
            row.append(f"{yw[y]*100:.0f}" if y in yw else "—")
        lines.append(f"| {y} | " + " | ".join(row) + " |")

    txt = "\n".join(lines)
    with open("../PERFORMANCE.md", "w", encoding="utf-8") as f:
        f.write(txt + "\n")

    # console
    for name, s in results:
        print(f"\n=== {name} ===")
        print(f"  win {s['win']*100:.1f}%  R:R {s['rr']:.2f}  PF {s['pf']:.2f}  "
              f"CAGR {s['cagr']*100:.1f}%  150k->{s['final']:,.0f}  "
              f"maxDD {s['dd']*100:.1f}%  Sharpe {s['sharpe']:.2f}  Calmar {s['calmar']:.2f}")
        yw = s.get("yearly_win", {})
        print("  yr:  " + "  ".join(f"{y}:{s['yearly'].get(y,0)*100:+.0f}%/"
                                    f"{yw.get(y,0)*100:.0f}w" for y in YEARS))
    print("\nWritten: PERFORMANCE.md")


if __name__ == "__main__":
    main()
