"""Can SMA200 also DEFINE MARKET TYPE for entry (not just exit)? Test using the
SMA200 slope (rising long-term trend) and structure to sharpen the entry regime.
Base = C1 Balanced + SMA200 exit (the adopted config). All variants keep that.
"""
from engine_rr import run, FULL_YEARS
from data_fetch import fetch_all

YEARS = list(range(2016, 2027))


def show(tag, s):
    print(f"{tag:34s} n={s['trades']:4d} win={s['win']*100:4.1f}% R:R={s['rr']:4.2f} "
          f"CAGR={s['cagr']*100:5.1f}% dd={s['dd']*100:6.1f}% Calmar={s['calmar']:.2f} "
          f"| 2018 {s['yearly'].get(2018,0)*100:+.0f}% 2022 {s['yearly'].get(2022,0)*100:+.0f}%")


def main():
    data = fetch_all()
    base = dict(K=3.5, K_tight=2.0, rsi_entry=25, near_high=0.88, slots=7, regime_exit=200)
    variants = {
        "C1+SMA200 exit (adopted base)": {},
        "+ SMA200 rising (entry regime)": dict(sma200_rising=True),
        "+ SMA200 & SMA50 both rising": dict(sma200_rising=True, sma50_rising=True),
        "+ SMA200 rising + not extended(<6ATR)": dict(sma200_rising=True, max_ext_atr=6.0),
        "+ SMA200 rising + not extended(<4ATR)": dict(sma200_rising=True, max_ext_atr=4.0),
    }
    for name, kw in variants.items():
        s = run(data, **base, **kw)
        show(name, s)


if __name__ == "__main__":
    main()
