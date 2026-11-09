"""Which currency a fill on a given IB venue is denominated in.

WHY THIS EXISTS
    IBKR's /iserver/account/trades sends `currency: null`, so the Web API shim
    (broker.py, IB.reqExecutions) has to work the currency out from where the
    trade EXECUTED. That table used to live inside broker.py's IB_BACKEND=web
    branch and listed only the listing exchanges. A SMART-routed order can
    fill on an alternative venue instead: on Mon 9 Nov 2026 the bot's 7272
    (Yamaha Motor) sale filled on JPNNEXT (Japannext), which was not listed,
    so the row reached data/fills_ledger.jsonl with no currency. fills_capture
    then guessed USD, and the dashboard valued 100 x 1,711 JPY as 171,100 USD -
    a HK$1.34M "gain" on the calendar.

    It is a separate module, with no imports, because fills_capture needs the
    same answer when it repairs a row already written, and broker._EXCH_CCY
    only exists when IB_BACKEND=web.

THE RULE
    An unknown venue returns "" - never a guess. fills_capture then flags the
    row ccy_guessed / rate_missing, which is the safe outcome: the bot's HKD
    pocket treats a numeric (possibly SEHK) ticker with an unknown currency as
    "unknown" and refuses to trust itself. Add a venue here only when the
    currency of EVERY trade on it is certain. SMART is a router, not a venue,
    and must never be listed (the ledger has SMART fills in USD, EUR and JPY);
    so are the pan-European MTFs that trade several currencies.
"""

EXCH_CCY = {
    # --- US listing exchanges (the original table) ---
    "NYSE": "USD", "NASDAQ": "USD", "ARCA": "USD", "AMEX": "USD",
    "BATS": "USD", "IEX": "USD", "PSE": "USD",
    # --- US-only execution venues a SMART order can fill on. Every one of
    # them trades US equities in USD only. ---
    "ISLAND": "USD", "IBKRATS": "USD", "EDGX": "USD", "EDGEA": "USD",
    "BYX": "USD", "BEX": "USD", "MEMX": "USD", "LTSE": "USD", "PEARL": "USD",
    "NYSENAT": "USD", "DRCTEDGE": "USD", "IBEOS": "USD", "CHX": "USD",
    "PSX": "USD", "OVERNIGHT": "USD",
    # --- Hong Kong ---
    "SEHK": "HKD",
    # --- Japan: the Tokyo listing plus the two alternative venues (PTS) a
    # SMART order on a Tokyo name can fill on ---
    "TSEJ": "JPY", "JPNNEXT": "JPY", "CHIXJ": "JPY",
    # --- euro venues ---
    "IBIS": "EUR", "IBIS2": "EUR", "XETRA": "EUR",
    "AEB": "EUR", "SBF": "EUR", "BVME": "EUR",
    # The euro venues ib_orders resolves (Madrid, Brussels, Helsinki, Vienna,
    # Lisbon). Without them a fill there reached the tax ledger with no
    # currency, guessed as USD and flagged, instead of booked in EUR.
    "BM": "EUR", "ENEXT.BE": "EUR", "HEX": "EUR", "VSE": "EUR", "BVL": "EUR",
    # --- London ---
    "LSE": "GBP", "LSEETF": "GBP",
    # --- Switzerland and the Nordics. EBS is SIX Swiss, which trades in CHF
    # (contracts.py maps .SW -> ("EBS", "CHF")); it used to be listed here as
    # EUR, so a Swiss fill would have been booked in euros with no flag. ---
    "EBS": "CHF", "CPH": "DKK", "SFB": "SEK", "OSE": "NOK",
    # Osaka, spelled out so a listing of "OSE.JPN" can never be shortened to
    # Oslo's "OSE" by the suffix rule in ccy_for.
    "OSE.JPN": "JPY",
}


def ccy_for(exchange, listing=None):
    """The currency of a fill that executed on `exchange`, or "" when unknown.

    `listing` is IB's listing_exchange for the contract, when the payload has
    one ("NASDAQ.NMS", "TSEJ"). It is consulted only when the execution venue
    is not mapped: first exactly, then with a ".XXX" suffix removed. Only the
    listing is ever shortened - "ENEXT.BE" is a venue code in its own right.
    """
    ex = str(exchange or "").strip().upper()
    if ex in EXCH_CCY:
        return EXCH_CCY[ex]
    li = str(listing or "").strip().upper()
    if li in EXCH_CCY:
        return EXCH_CCY[li]
    if "." in li:
        head = li.split(".", 1)[0]
        if head in EXCH_CCY:
            return EXCH_CCY[head]
    return ""
