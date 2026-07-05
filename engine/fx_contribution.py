"""How much does FX actually contribute? Per-market trade breakdown for the
balanced (C1) and max-R:R (C2) engines with FX in the universe."""
import engine_rr
from engine_rr import run
from data_fetch import fetch_all

engine_rr.run.return_trades = True


def breakdown(name, kw):
    data = fetch_all()
    s, tr = run(data, **kw)
    print(f"\n=== {name} ===  total {s['trades']} trades, "
          f"win {s['win']*100:.1f}%, R:R {s['rr']:.2f}, CAGR {s['cagr']*100:.1f}%")
    if tr.empty:
        return
    g = tr.groupby("market").agg(n=("net_ret", "size"),
                                 win=("net_ret", lambda x: round((x > 0).mean()*100, 1)),
                                 avg=("net_ret", lambda x: round(x.mean()*100, 2)),
                                 pnl=("pnl", lambda x: round(x.sum())))
    print(g.to_string())


if __name__ == "__main__":
    data = fetch_all()  # warm cache once
    breakdown("C1 Balanced", dict(K=3.5, K_tight=2.0, rsi_entry=25,
                                  near_high=0.88, slots=7))
    breakdown("C2 Max-R:R", dict(K=3.0, rsi_entry=15, slots=7))
