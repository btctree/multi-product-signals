"""Monitoring universe: initial list per user spec + daily update mechanism.

Initial universe (user spec item 4):
  - top 50 US stocks by market cap
  - top 30 HK stocks by market cap
  - top 50 Japan stocks by market cap
  - main board indices: US (S&P 500), HK (Hang Seng), Japan (Nikkei 225)
  - gold, silver, oil, BTC, ETH

update_universe() re-ranks a wider candidate pool by live market cap daily and
adds newcomers that enter the top-N of their market (spec item 3). Removed names
are kept while still held, then dropped.
"""
import json
import datetime as dt
from config import UNIVERSE_FILE, DATA_DIR

# Snapshot lists (mid-2026 market-cap order; refreshed by update_universe)
US_TOP50 = [
    "NVDA", "MSFT", "AAPL", "GOOGL", "AMZN", "META", "AVGO", "TSLA", "BRK-B", "LLY",
    "JPM", "WMT", "V", "ORCL", "XOM", "MA", "NFLX", "COST", "UNH", "PG",
    "JNJ", "HD", "ABBV", "BAC", "CRM", "KO", "AMD", "CVX", "MRK", "TMUS",
    "PLTR", "CSCO", "WFC", "PEP", "ADBE", "LIN", "IBM", "GE", "MCD", "ACN",
    "NOW", "ISRG", "TMO", "ABT", "QCOM", "AXP", "GS", "CAT", "INTU", "DIS",
]
HK_TOP30 = [
    "0700.HK", "9988.HK", "1299.HK", "0941.HK", "3690.HK", "0939.HK", "0005.HK",
    "1398.HK", "1810.HK", "3988.HK", "2318.HK", "0883.HK", "1211.HK", "0388.HK",
    "9999.HK", "0857.HK", "2628.HK", "1024.HK", "9618.HK", "9888.HK", "0016.HK",
    "0001.HK", "0175.HK", "0002.HK", "0027.HK", "0066.HK", "2020.HK", "0386.HK",
    "0669.HK", "1113.HK",
]
JP_TOP50 = [
    "7203.T", "6758.T", "9984.T", "6861.T", "8306.T", "6501.T", "9432.T", "8035.T",
    "9433.T", "7974.T", "4063.T", "8058.T", "8001.T", "9983.T", "6098.T", "8316.T",
    "4568.T", "6902.T", "6367.T", "7741.T", "6273.T", "4519.T", "6954.T", "8031.T",
    "8411.T", "2914.T", "6981.T", "4502.T", "7267.T", "6752.T", "8766.T", "9022.T",
    "3382.T", "4661.T", "6503.T", "7751.T", "8053.T", "8002.T", "9020.T", "4543.T",
    "6857.T", "6146.T", "7011.T", "8591.T", "5108.T", "4901.T", "9101.T", "6301.T",
    "8801.T", "7733.T",
]
# EU large caps (added 2026-07-04 per user): liquid Euro Stoxx names, Yahoo suffixes
# .AS Amsterdam · .PA Paris · .DE Xetra · .MC Madrid · .MI Milan · .BR Brussels
EU_TOP30 = [
    "ASML.AS", "MC.PA", "OR.PA", "SAP.DE", "SIE.DE", "TTE.PA", "ALV.DE",
    "AIR.PA", "SAN.PA", "IBE.MC", "ENEL.MI", "ISP.MI", "DTE.DE", "BAS.DE",
    "BNP.PA", "AI.PA", "SU.PA", "CS.PA", "BAYN.DE", "BBVA.MC", "SAN.MC",
    "ENI.MI", "DG.PA", "EL.PA", "RMS.PA", "ADS.DE", "MBG.DE", "BMW.DE",
    "MUV2.DE", "PRX.AS",
]
INDICES = ["^GSPC", "^HSI", "^N225", "^STOXX50E"]   # + Euro Stoxx 50
# gold, silver, WTI oil, + copper (futures = reference, monitor-only).
# USD index via UUP ETF only (DX=F is dead on Yahoo).
COMMODITIES = ["GC=F", "SI=F", "CL=F", "HG=F"]
# tradeable ETF proxies for copper (CPER) and the US dollar index (UUP) — spot cash
MACRO_ETFS = ["CPER", "UUP"]
CRYPTO = ["BTC-USD", "ETH-USD"]
# FX majors (added 2026-07-05 per user): long the pair = long base / short USD.
# EUR/USD, USD/JPY, GBP/USD, USD/CAD (Yahoo "=X" spot rates).
FX = ["EURUSD=X", "USDJPY=X", "GBPUSD=X", "USDCAD=X"]
# Leveraged index/sector ETFs: long-only CASH instruments (no margin account,
# no liquidation, loss capped at stake) — static leg, never rotated by market cap
LEV_ETFS = ["TQQQ", "SOXL", "UPRO", "SPXL", "QLD", "SSO", "TECL", "FAS", "TNA",
            "UDOW", "TMF", "UGL", "NVDL", "BITX",
            "SQQQ"]   # inverse (user-requested; monitor-only, decay product)
# common broad/sector/commodity ETFs (pool expansion 2026-07-09): analyzed and
# searchable; Actions stay driven by the validated sleeves
ETF_SET = {"GLD", "SLV", "USO", "UNG", "DBC", "DBA", "PPLT", "PALL",
           "SPY", "QQQ", "VTI", "VOO", "IWM", "DIA", "EFA", "EEM", "FXI",
           "KWEB", "EWJ", "VGK", "SMH", "XLK", "XLE", "XLF", "XLV", "XLI",
           "XLU", "XLY", "XLP", "XLB", "GDX", "ARKK", "IBIT", "ETHA"}
# Bond ETFs (added 2026-07-04 per user): treasuries, IG/HY credit, EM, TIPS
BONDS = ["TLT", "IEF", "SHY", "LQD", "HYG", "AGG", "EMB", "TIP", "ZROZ"]
_EU_SUFFIXES = (".AS", ".PA", ".DE", ".MC", ".MI", ".BR", ".LS", ".HE",
                ".VI", ".SW", ".ST", ".OL", ".CO", ".L")

NAMES = {  # human labels for reports (partial; yfinance fills the rest)
    "^GSPC": "S&P 500", "^HSI": "Hang Seng Index", "^N225": "Nikkei 225",
    "^STOXX50E": "Euro Stoxx 50",
    "GC=F": "Gold", "SI=F": "Silver", "CL=F": "WTI Crude Oil",
    "HG=F": "Copper", "DX=F": "US Dollar Index",
    "CPER": "Copper ETF (CPER)", "UUP": "US Dollar Index ETF (UUP)",
    "BTC-USD": "Bitcoin", "ETH-USD": "Ethereum",
    "EURUSD=X": "EUR/USD", "USDJPY=X": "USD/JPY",
    "GBPUSD=X": "GBP/USD", "USDCAD=X": "USD/CAD",
}


def market_of(ticker: str) -> str:
    if ticker.endswith(".HK"):
        return "HK"
    if ticker.endswith(".T"):
        return "JP"
    if ticker.endswith(_EU_SUFFIXES):
        return "EU"
    if ticker.startswith("^"):
        return "INDEX"
    if ticker.endswith("=X"):
        return "FX"
    if ticker.endswith("=F"):
        return "COMMODITY"
    if ticker.endswith("-USD"):
        return "CRYPTO"
    if ticker in LEV_ETFS:
        return "LEV"
    if ticker in BONDS:
        return "BOND"
    if ticker in MACRO_ETFS:
        return "MACRO"
    if ticker in ETF_SET:
        return "ETF"
    if ticker.endswith((".SS", ".SZ")):
        return "INDEX"          # Shanghai/Shenzhen composites (monitor-only)
    return "US"


def initial_universe() -> dict:
    return {
        "updated": dt.date.today().isoformat(),
        "tickers": US_TOP50 + HK_TOP30 + JP_TOP50 + EU_TOP30 + INDICES
        + COMMODITIES + MACRO_ETFS + CRYPTO + FX + LEV_ETFS + BONDS,
        "added_log": [],
        "removed_log": [],
    }


def load_universe() -> dict:
    if UNIVERSE_FILE.exists():
        return json.loads(UNIVERSE_FILE.read_text())
    uni = initial_universe()
    save_universe(uni)
    return uni


def save_universe(uni: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    UNIVERSE_FILE.write_text(json.dumps(uni, indent=2))


# Financial-Analyst rules for the monitoring list (policy set 2026-07-08):
# ADD  - Rule A: product enters its market's top-N by market cap (grow-only;
#          the initial top-cap list was only the SEED - cap drops never remove)
#      - Rule B: momentum leader — ~90-day return >= +30% with healthy liquidity
#      - Rule C: within 3% of its 52-week high with healthy liquidity
# REMOVE - ONLY persistent illiquidity: 60-day median dollar-volume in the
#          bottom 10% of its market AND below the absolute floor, for 5
#          consecutive daily checks (strike counter persisted in universe.json).
#          Held positions and static legs are never removed.
VOL_FLOOR = {"US": 10e6, "HK": 50e6, "JP": 1e9, "EU": 10e6}  # local ccy $vol/day


def update_universe(held: set[str] | None = None) -> dict:
    """Daily refresh (spec item 3) under the analyst policy above."""
    import yfinance as yf

    held = held or set()
    uni = load_universe()
    current = set(uni["tickers"])

    # candidate pool = current list + a wider next-tier watch pool per market
    EXTRA_CANDIDATES = {
        "US": ["TXN", "VZ", "PM", "AMGN", "PFE", "UBER", "SPGI", "ANET", "BKNG",
               "HON", "NEE", "UNP", "RTX", "LOW", "BLK", "SYK", "ETN", "PANW",
               "MU", "APP", "LRCX", "KLAC", "SNOW", "COIN", "MSTR"],
        "HK": ["0175.HK", "2382.HK", "1088.HK", "0288.HK", "6862.HK", "0968.HK",
               "2269.HK", "1177.HK", "0981.HK", "9961.HK", "2015.HK", "6690.HK"],
        "JP": ["6702.T", "7735.T", "4578.T", "6723.T", "9434.T", "8604.T",
               "7269.T", "6594.T", "4523.T", "6178.T", "5401.T", "8750.T"],
        "EU": ["SAF.PA", "DTE.DE", "IFX.DE", "ABI.BR", "NDA-FI.HE", "STLAM.MI",
               "UCG.MI", "BN.PA", "KER.PA", "VOW3.DE"],
    }
    tops = {"US": 50, "HK": 30, "JP": 50, "EU": 30}
    pools = {m: [t for t in current if market_of(t) == m] + EXTRA_CANDIDATES[m]
             for m in tops}

    import time
    import pandas as pd
    from data_fetch import cache_path

    new_tickers = list(uni["tickers"])   # grow-only base: nobody leaves on cap
    added, removed, add_via = [], [], {}

    # ---- Rule A: market-cap newcomers (grow-only) ----
    for mkt, pool in pools.items():
        caps, failed = {}, []
        for t in dict.fromkeys(pool):
            try:
                caps[t] = yf.Ticker(t).fast_info.get("marketCap") or 0
            except Exception:
                caps[t] = 0
            if caps[t] <= 0:
                failed.append(t)
            time.sleep(0.15)             # be gentle: ~200 lookups/run
        known = {t: c for t, c in caps.items() if c > 0}
        for t in sorted(known, key=known.get, reverse=True)[: tops[mkt]]:
            if t not in current and t not in added:
                added.append(t)
                add_via[t] = "market-cap top rank"
        print(f"[universe] {mkt}: cap scan ok ({len(known)} known, "
              f"{len(failed)} failed lookups)")

    # ---- Rules B/C: analyst additions beyond cap (momentum / 52w-high) ----
    for mkt in tops:
        for t in dict.fromkeys(EXTRA_CANDIDATES[mkt]):
            if t in current or t in added:
                continue
            try:
                df = yf.download(t, period="1y", interval="1d", auto_adjust=True,
                                 progress=False, threads=False)
                if df is None or df.empty:
                    continue
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                c, v = df["Close"].dropna(), df["Volume"].fillna(0)
                if len(c) < 130:
                    continue
                dvol = float((c * v).tail(60).median())
                mom = float(c.iloc[-1] / c.iloc[-63] - 1)
                near_hi = bool(c.iloc[-1] >= 0.97 * float(c.max()))
                if dvol >= VOL_FLOOR.get(mkt, 0) and (mom >= 0.30 or near_hi):
                    added.append(t)
                    add_via[t] = (f"analyst: +{mom*100:.0f}% momentum" if mom >= 0.30
                                  else "analyst: near 52w high")
            except Exception:
                pass
            time.sleep(0.15)

    new_tickers += added

    # ---- Removal: ONLY persistent bottom-decile liquidity (5 strikes) ----
    strikes = uni.get("lowvol_strikes", {})
    for mkt in tops:
        members = [t for t in new_tickers if market_of(t) == mkt]
        dvols = {}
        for t in members:
            p = cache_path(t)
            if p.exists():
                try:
                    df = pd.read_csv(p, index_col=0, parse_dates=True)
                    dvols[t] = float((df["Close"] * df["Volume"]).tail(60).median())
                except Exception:
                    pass
        if len(dvols) < 10:
            continue
        q10 = float(pd.Series(dvols).quantile(0.10))
        floor = VOL_FLOOR.get(mkt, 0)
        for t, dv in dvols.items():
            if dv < q10 and dv < floor:
                strikes[t] = strikes.get(t, 0) + 1
                if strikes[t] >= 5 and t not in held:
                    removed.append(t)
                    print(f"[universe] {mkt}: REMOVE {t} — illiquid (60d median "
                          f"$vol {dv:,.0f}; bottom decile {strikes[t]} days running)")
            else:
                strikes.pop(t, None)
    uni["lowvol_strikes"] = {t: s for t, s in strikes.items() if t not in removed}
    new_tickers = [t for t in new_tickers if t not in removed]
    uni["add_reasons"] = {**uni.get("add_reasons", {}), **add_via}

    today = dt.date.today().isoformat()
    uni["tickers"] = list(dict.fromkeys(new_tickers))
    uni["updated"] = today
    if added:
        uni["added_log"].append({"date": today, "tickers": added})
    if removed:
        uni["removed_log"].append({"date": today, "tickers": removed})
    save_universe(uni)
    print(f"[universe] refreshed {today}: {len(uni['tickers'])} tickers | "
          f"added {added or 'none'} | removed {removed or 'none'}")
    return uni


if __name__ == "__main__":
    u = load_universe()
    print(f"Universe: {len(u['tickers'])} tickers (updated {u['updated']})")
