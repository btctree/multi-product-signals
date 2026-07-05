"""Daily orchestrator — run this once per day (after HK close / before US open,
or any time; each market's signal uses its own latest daily close).

  python run_daily.py              # normal daily run
  python run_daily.py --update     # also refresh the universe by market cap
  python run_daily.py --backtest   # rerun the full 10y validation
"""
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from data_fetch import fetch_all
from report import generate_report, load_positions


def main():
    args = set(sys.argv[1:])
    if "--update" in args:
        from universe import update_universe
        held = set(load_positions().keys())
        uni = update_universe(held)
        print(f"Universe refreshed: {len(uni['tickers'])} tickers")
    data = fetch_all(force="--force" in args)
    if "--backtest" in args:
        from backtest import run_backtest
        run_backtest(data)
    print(generate_report(data)[:3000])


if __name__ == "__main__":
    main()
