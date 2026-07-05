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
# gold, silver, WTI oil, + copper & USD-index (futures = reference, monitor-only)
COMMODITIES = ["GC=F", "SI=F", "CL=F", "HG=F", "DX=F"]
# tradeable ETF proxies for copper (CPER) and the US dollar index (UUP) — spot cash
MACRO_ETFS = ["CPER", "UUP"]
CRYPTO = ["BTC-USD", "ETH-USD"]
# FX majors (added 2026-07-05 per user): long the pair = long base / short USD.
# EUR/USD, USD/JPY, GBP/USD, USD/CAD (Yahoo "=X" spot rates).
FX = ["EURUSD=X", "USDJPY=X", "GBPUSD=X", "USDCAD=X"]
# Leveraged index/sector ETFs: long-only CASH instruments (no margin account,
# no liquidation, loss capped at stake) — static leg, never rotated by market cap
LEV_ETFS = ["TQQQ", "SOXL", "UPRO", "SPXL", "QLD", "SSO", "TECL", "FAS", "TNA"]
# Bond ETFs (added 2026-07-04 per user): treasuries, IG/HY credit, EM, TIPS
BONDS = ["TLT", "IEF", "SHY", "LQD", "HYG", "AGG", "EMB", "TIP"]
_EU_SUFFIXES = (".AS", ".PA", ".DE", ".MC", ".MI", ".BR", ".LS", ".HE",
                ".VI", ".SW", ".ST", ".OL", ".CO")

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


def update_universe(held: set[str] | None = None) -> dict:
    """Daily refresh (spec item 3): re-rank each equity market by live market cap;
    add newcomers entering the top-N, retire names that fall out (unless held)."""
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

    new_tickers, added, removed = [], [], []
    for mkt, pool in pools.items():
        caps = {}
        for t in dict.fromkeys(pool):  # dedupe, keep order
            try:
                fi = yf.Ticker(t).fast_info
                caps[t] = fi.get("marketCap") or 0
            except Exception:
                caps[t] = 0
        ranked = sorted(caps, key=caps.get, reverse=True)
        keep = ranked[: tops[mkt]]
        for t in keep:
            if t not in current:
                added.append(t)
        for t in [x for x in current if market_of(x) == mkt]:
            if t not in keep and t not in held:
                removed.append(t)
        new_tickers += [t for t in keep if t not in held] + \
                       [t for t in current if market_of(t) == mkt and t in held and t not in keep]

    # static legs never rotate out
    new_tickers += [t for t in current
                    if market_of(t) in ("INDEX", "COMMODITY", "MACRO",
                                        "CRYPTO", "FX", "LEV", "BOND")]

    today = dt.date.today().isoformat()
    uni["tickers"] = list(dict.fromkeys(new_tickers))
    uni["updated"] = today
    if added:
        uni["added_log"].append({"date": today, "tickers": added})
    if removed:
        uni["removed_log"].append({"date": today, "tickers": removed})
    save_universe(uni)
    return uni


if __name__ == "__main__":
    u = load_universe()
    print(f"Universe: {len(u['tickers'])} tickers (updated {u['updated']})")
