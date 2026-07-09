"""One-time pool expansion (user-approved 2026-07-09):
S&P 500 + Nikkei 225 + Hang Seng members (scraped from Wikipedia with names)
+ Euro Stoxx 50 / DAX / CAC 40 + expanded commodities, FX majors/crosses,
common global indices, common leveraged ETFs, common broad ETFs.
Merges grow-only into data/universe.json and seeds company_names.json.
"""
import json
import re

import pandas as pd

from config import DATA_DIR
from universe import load_universe, save_universe, market_of

WIKI = {
    "SP500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "N225": "https://en.wikipedia.org/wiki/Nikkei_225",
    "HSI": "https://en.wikipedia.org/wiki/Hang_Seng_Index",
}

# hand-curated legs (symbol -> name)
EURO_TOP = {
    "ASML.AS": "ASML", "MC.PA": "LVMH", "OR.PA": "L'Oreal", "SAP.DE": "SAP",
    "SIE.DE": "Siemens", "TTE.PA": "TotalEnergies", "ALV.DE": "Allianz",
    "AIR.PA": "Airbus", "SAN.PA": "Sanofi", "IBE.MC": "Iberdrola",
    "ENEL.MI": "Enel", "ISP.MI": "Intesa Sanpaolo", "DTE.DE": "Deutsche Telekom",
    "BAS.DE": "BASF", "BNP.PA": "BNP Paribas", "AI.PA": "Air Liquide",
    "SU.PA": "Schneider Electric", "CS.PA": "AXA", "BAYN.DE": "Bayer",
    "BBVA.MC": "BBVA", "SAN.MC": "Banco Santander", "ENI.MI": "Eni",
    "DG.PA": "Vinci", "EL.PA": "EssilorLuxottica", "RMS.PA": "Hermes",
    "ADS.DE": "Adidas", "MBG.DE": "Mercedes-Benz", "BMW.DE": "BMW",
    "MUV2.DE": "Munich Re", "PRX.AS": "Prosus", "SAF.PA": "Safran",
    "IFX.DE": "Infineon", "ABI.BR": "AB InBev", "NDA-FI.HE": "Nordea",
    "UCG.MI": "UniCredit", "STLAM.MI": "Stellantis", "BN.PA": "Danone",
    "KER.PA": "Kering", "VOW3.DE": "Volkswagen", "DBK.DE": "Deutsche Bank",
    "DHL.DE": "DHL Group", "RWE.DE": "RWE", "HEI.DE": "Heidelberg Materials",
    "RI.PA": "Pernod Ricard", "CAP.PA": "Capgemini", "ML.PA": "Michelin",
    "ORA.PA": "Orange", "ACA.PA": "Credit Agricole", "GLE.PA": "Societe Generale",
    "ITX.MC": "Inditex (Zara)", "TEF.MC": "Telefonica", "REP.MC": "Repsol",
    "RACE.MI": "Ferrari", "G.MI": "Generali", "AD.AS": "Ahold Delhaize",
    "INGA.AS": "ING", "PHIA.AS": "Philips", "HEIA.AS": "Heineken",
    "WKL.AS": "Wolters Kluwer", "ADYEN.AS": "Adyen", "NOVO-B.CO": "Novo Nordisk",
    "NESN.SW": "Nestle", "ROG.SW": "Roche", "NOVN.SW": "Novartis",
    "UBSG.SW": "UBS", "ZURN.SW": "Zurich Insurance", "AZN.L": "AstraZeneca",
    "SHEL.L": "Shell", "HSBA.L": "HSBC (London)", "ULVR.L": "Unilever",
    "RIO.L": "Rio Tinto", "GSK.L": "GSK", "BP.L": "BP", "BARC.L": "Barclays",
    "LSEG.L": "London Stock Exchange Group", "REL.L": "RELX",
}
COMMODITY_FUTS = {
    "NG=F": "Natural Gas", "BZ=F": "Brent Crude Oil", "PL=F": "Platinum",
    "PA=F": "Palladium", "ZC=F": "Corn", "ZW=F": "Wheat", "ZS=F": "Soybeans",
    "KC=F": "Coffee", "SB=F": "Sugar", "CC=F": "Cocoa", "CT=F": "Cotton",
}
COMMODITY_ETFS = {
    "GLD": "SPDR Gold Shares", "SLV": "iShares Silver Trust",
    "USO": "United States Oil Fund", "UNG": "United States Natural Gas Fund",
    "DBC": "Invesco DB Commodity Index", "DBA": "Invesco DB Agriculture",
    "PPLT": "abrdn Physical Platinum", "PALL": "abrdn Physical Palladium",
}
FX_MORE = {
    "AUDUSD=X": "AUD/USD", "NZDUSD=X": "NZD/USD", "USDCHF=X": "USD/CHF",
    "EURJPY=X": "EUR/JPY", "EURGBP=X": "EUR/GBP", "GBPJPY=X": "GBP/JPY",
    "CNY=X": "USD/CNY", "USDSGD=X": "USD/SGD", "USDKRW=X": "USD/KRW",
    "USDINR=X": "USD/INR", "USDMXN=X": "USD/MXN", "AUDJPY=X": "AUD/JPY",
}
INDICES_MORE = {
    "^NDX": "Nasdaq 100", "^DJI": "Dow Jones Industrial", "^RUT": "Russell 2000",
    "^FTSE": "FTSE 100", "^GDAXI": "DAX", "^FCHI": "CAC 40", "^VIX": "VIX",
    "^TNX": "US 10Y Treasury Yield", "^KS11": "KOSPI", "^TWII": "Taiwan Weighted",
    "^AXJO": "ASX 200", "^BSESN": "BSE Sensex", "^HSCE": "Hang Seng China Ent.",
    "^STI": "Straits Times", "000001.SS": "Shanghai Composite",
}
LEV_MORE = {
    "UDOW": "ProShares UltraPro Dow30 (3x)", "TMF": "Direxion 20+Y Treasury Bull 3x",
    "UGL": "ProShares Ultra Gold (2x)", "NVDL": "GraniteShares 2x Long NVDA",
    "BITX": "Volatility Shares 2x Bitcoin",
}
ETFS = {
    "SPY": "SPDR S&P 500 ETF", "QQQ": "Invesco QQQ (Nasdaq 100)",
    "VTI": "Vanguard Total Stock Market", "VOO": "Vanguard S&P 500",
    "IWM": "iShares Russell 2000", "DIA": "SPDR Dow Jones",
    "EFA": "iShares MSCI EAFE", "EEM": "iShares MSCI Emerging Markets",
    "FXI": "iShares China Large-Cap", "KWEB": "KraneShares China Internet",
    "EWJ": "iShares MSCI Japan", "VGK": "Vanguard FTSE Europe",
    "SMH": "VanEck Semiconductor", "XLK": "Technology Select SPDR",
    "XLE": "Energy Select SPDR", "XLF": "Financial Select SPDR",
    "XLV": "Health Care Select SPDR", "XLI": "Industrial Select SPDR",
    "XLU": "Utilities Select SPDR", "XLY": "Consumer Discretionary SPDR",
    "XLP": "Consumer Staples SPDR", "XLB": "Materials Select SPDR",
    "GDX": "VanEck Gold Miners", "ARKK": "ARK Innovation",
    "IBIT": "iShares Bitcoin Trust", "ETHA": "iShares Ethereum Trust",
}


def _tables(url):
    import urllib.request
    from io import StringIO
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) research script"})
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", errors="replace")
    return pd.read_html(StringIO(html))


def scrape():
    """Return {symbol: name} from the three Wikipedia index-member lists."""
    out = {}
    # S&P 500
    try:
        t = _tables(WIKI["SP500"])[0]
        for _, r in t.iterrows():
            sym = str(r["Symbol"]).strip().replace(".", "-")   # BRK.B -> BRK-B
            out[sym] = str(r["Security"]).strip()
        print(f"S&P 500: {len(t)} members")
    except Exception as e:
        print("WARN S&P500 scrape failed:", e)
    # Nikkei 225 — official Nikkei Indexes component list (sector tables)
    try:
        n = 0
        for t in _tables("https://indexes.nikkei.co.jp/en/nkave/index/component"):
            cols = [str(c).lower() for c in t.columns]
            if any("code" in c for c in cols) and any("name" in c for c in cols):
                code_col = t.columns[[i for i, c in enumerate(cols) if "code" in c][0]]
                name_col = t.columns[[i for i, c in enumerate(cols) if "name" in c][0]]
                for _, r in t.iterrows():
                    m = re.search(r"(\d{4})", str(r[code_col]))
                    if m:
                        out[f"{m.group(1)}.T"] = str(r[name_col]).strip()
                        n += 1
        print(f"Nikkei 225: {n} members")
    except Exception as e:
        print("WARN Nikkei scrape failed:", e)
    # Hang Seng Index
    try:
        n = 0
        for t in _tables(WIKI["HSI"]):
            cols = [str(c).lower() for c in t.columns]
            if any("ticker" in c or "sehk" in c or "code" in c for c in cols) and len(t) > 40:
                code_col = t.columns[[i for i, c in enumerate(cols)
                                      if "ticker" in c or "sehk" in c or "code" in c][0]]
                name_col = t.columns[[i for i, c in enumerate(cols) if "name" in c or "compan" in c][0]]
                for _, r in t.iterrows():
                    m = re.search(r"(\d{1,4})", str(r[code_col]))
                    if m:
                        out[f"{int(m.group(1)):04d}.HK"] = str(r[name_col]).strip()
                        n += 1
        print(f"Hang Seng: {n} members")
    except Exception as e:
        print("WARN HSI scrape failed:", e)
    return out


def main():
    uni = load_universe()
    current = set(uni["tickers"])
    names = {}
    add = {}

    add.update(scrape())
    for grp in (EURO_TOP, COMMODITY_FUTS, COMMODITY_ETFS, FX_MORE,
                INDICES_MORE, LEV_MORE, ETFS):
        add.update(grp)

    new = [t for t in add if t not in current]
    uni["tickers"] = uni["tickers"] + new
    uni["added_log"].append({"date": "2026-07-09", "tickers": new,
                             "via": "pool expansion: index members + common products"})
    uni["add_reasons"] = {**uni.get("add_reasons", {}),
                          **{t: "pool expansion 2026-07-09" for t in new}}
    save_universe(uni)

    # seed company names (avoids thousands of slow get_info calls)
    p = DATA_DIR / "company_names.json"
    cache = json.loads(p.read_text()) if p.exists() else {}
    cache.update({k: v for k, v in add.items() if isinstance(v, str) and v})
    p.write_text(json.dumps(cache, indent=1))

    from collections import Counter
    print(f"\nuniverse: {len(uni['tickers'])} tickers (+{len(new)})")
    print(dict(Counter(market_of(t) for t in uni["tickers"])))


if __name__ == "__main__":
    main()
