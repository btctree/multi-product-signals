"""SP1b — can a stronger ENTRY (higher follow-through) push the trail engine's
win rate up toward 55% WITHOUT killing its 2:1 R:R? Mechanism: only buy dips in
the strongest stocks (near 52w highs) / strongest trends / shallow pullbacks.
Pre-registered; the goal is to move the frontier point, not to mine."""
from engine_rr import run, gate, FULL_YEARS
from data_fetch import fetch_all

VARIANTS = {
    "Q1 K3 near-high.90": dict(K=3.0, rsi_entry=15, near_high=0.90),
    "Q2 K3 near-high.85 mom>10": dict(K=3.0, rsi_entry=15, near_high=0.85, min_mom=0.10),
    "Q3 K3 shallow(rsi25,>0.97fast)": dict(K=3.0, rsi_entry=25, min_price_over_fast=0.97),
    "Q4 K2.5 near-high.90 rsi25": dict(K=2.5, rsi_entry=25, near_high=0.90),
    "Q5 two-stage3.5->2 near-high.90": dict(K=3.5, K_tight=2.0, rsi_entry=15, near_high=0.90),
    "Q6 two-stage3.5->2 rsi25 nh.88": dict(K=3.5, K_tight=2.0, rsi_entry=25, near_high=0.88),
    "Q7 K3 rsi25 nh.90 mom>10 partial": dict(K=3.0, rsi_entry=25, near_high=0.90,
                                             min_mom=0.10, partial=True, partial_at=2.0),
}


def main():
    data = fetch_all()
    for name, kw in VARIANTS.items():
        s = run(data, slots=7, **kw)
        reds = [y for y in FULL_YEARS if s["yearly"].get(y, 0) < 0]
        g = "PASS" if gate(s) else "----"
        pmw = min(s["per_market_win"].values()) * 100 if s["per_market_win"] else 0
        print(f"{name:34s} n={s['trades']:4d} win={s['win']*100:5.1f}% "
              f"[{s['h1']*100:4.1f}/{s['h2']*100:4.1f}] R:R={s['rr']:4.2f} "
              f"(w{s['avg_win']*100:+.1f}/l{s['avg_loss']*100:+.1f}) PF={s['pf']:4.2f} "
              f"CAGR={s['cagr']*100:5.1f}% dd={s['dd']*100:5.1f}% minMkt={pmw:.0f}% [{g}]")


if __name__ == "__main__":
    main()
