"""Record real fills so the daily report tracks live positions and cash.

  python position_cli.py buy  0700.HK 512.0 [2026-07-03]   # size auto = cash/(15-held)
  python position_cli.py sell 0700.HK 545.0                # returns proceeds to cash
  python position_cli.py cash 150000                       # set/true-up cash ledger
  python position_cli.py list
"""
import datetime as dt
import sys

from config import STRAT, CRY_STRAT, SLEEVES
from report import load_state, save_state, sleeve_of
from data_fetch import fetch_one
from indicators import add_features
from strategy import stop_price, target_price


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    st = load_state()
    pos = st["positions"]

    if cmd == "list":
        for s, cfg in SLEEVES.items():
            n = sum(1 for t in pos if sleeve_of(t) == s)
            print(f"{s}: cash HKD {st['cash'][s]:,.0f} | {n}/{cfg['slots']} slots")
        for t, v in pos.items():
            tgt = f"target {v['target']:.2f}" if "target" in v else "trend (trail)"
            print(f"{t} [{sleeve_of(t)}]: HKD {v['notional']:,.0f} @ {v['entry_px']} "
                  f"on {v['entry_date']} | {tgt} | stop {v['stop']:.2f}")
        return

    if cmd == "cash":
        total = float(sys.argv[2])
        st["cash"] = {s: total * cfg["weight"] for s, cfg in SLEEVES.items()}
        save_state(st)
        print(" | ".join(f"{s}: HKD {st['cash'][s]:,.0f}" for s in SLEEVES))
        return

    t = sys.argv[2]
    s = sleeve_of(t)
    if cmd == "buy":
        cfg = SLEEVES[s]
        n_held = sum(1 for x in pos if sleeve_of(x) == s)
        if n_held >= cfg["slots"]:
            print(f"{s} sleeve full ({cfg['slots']} slots)"); return
        px = float(sys.argv[3])
        date = sys.argv[4] if len(sys.argv) > 4 else dt.date.today().isoformat()
        notional = st["cash"][s] / (cfg["slots"] - n_held)
        df = add_features(fetch_one(t), STRAT)
        a = float(df["atr"].iloc[-1])
        if s == "CRY":
            trail = px - CRY_STRAT["trail_atr_mult"] * a
            pos[t] = {"entry_px": px, "entry_date": date, "entry_atr": a,
                      "notional": round(notional, 2), "stop": trail, "hw": px}
            order_txt = (f"place GTC SELL STOP {trail:.2f} (chandelier trail — "
                         f"raise it per the daily report, never lower)")
        else:
            pos[t] = {"entry_px": px, "entry_date": date, "entry_atr": a,
                      "notional": round(notional, 2),
                      "stop": stop_price(px, a), "target": target_price(px, a)}
            order_txt = (f"place GTC SELL LIMIT {pos[t]['target']:.2f} "
                         f"and SELL STOP {pos[t]['stop']:.2f}")
        st["cash"][s] -= pos[t]["notional"]
        save_state(st)
        print(f"BUY {t} [{s}]: HKD {pos[t]['notional']:,.0f} @ {px}\n  {order_txt}\n"
              f"  {s} cash left: HKD {st['cash'][s]:,.0f}")
    elif cmd == "sell":
        if t not in pos:
            print(f"{t} not held"); return
        info = pos.pop(t)
        px = float(sys.argv[3])
        proceeds = info["notional"] * (px / info["entry_px"])
        st["cash"][s] += proceeds
        save_state(st)
        print(f"SELL {t} [{s}] @ {px} | P&L {(px / info['entry_px'] - 1) * 100:+.2f}% "
              f"(HKD {proceeds - info['notional']:+,.0f}) | {s} cash: HKD {st['cash'][s]:,.0f}")


if __name__ == "__main__":
    main()
