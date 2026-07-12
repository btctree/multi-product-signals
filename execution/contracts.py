"""Map dashboard (Yahoo-format) symbols to Interactive Brokers contracts.

The caller must ib.qualifyContracts() the result; anything IB can't resolve is
skipped (never traded on a guess). Known refinement points are flagged in-line.
"""
from ib_async import Stock, Crypto

# EU/UK/CH/Nordic suffix -> (IB exchange, currency)
_EU = {
    ".AS": ("AEB", "EUR"), ".PA": ("SBF", "EUR"), ".DE": ("IBIS", "EUR"),
    ".MC": ("BM", "EUR"), ".MI": ("BVME", "EUR"), ".BR": ("ENEXT.BE", "EUR"),
    ".L": ("LSE", "GBP"), ".SW": ("EBS", "CHF"), ".CO": ("CPH", "DKK"),
    ".HE": ("HEX", "EUR"), ".ST": ("SFB", "SEK"), ".OL": ("OSE", "NOK"),
    ".VI": ("VSE", "EUR"), ".LS": ("BVL", "EUR"),
}


def currency_of(sym: str) -> str:
    if sym.endswith(".HK"):
        return "HKD"
    if sym.endswith(".T"):
        return "JPY"
    for suf, (_, ccy) in _EU.items():
        if sym.endswith(suf):
            return ccy
    if sym.endswith("-USD"):
        return "USD"
    return "USD"


def to_ib(sym: str):
    """Return an unqualified IB contract for a tradeable symbol, or None if this
    class shouldn't be auto-traded (indices, futures, FX pairs, ETFs, etc.)."""
    if sym.endswith(".HK"):
        code = str(int(sym.split(".")[0]))          # 0700.HK -> 700
        return Stock(code, "SEHK", "HKD")
    if sym.endswith(".T"):
        return Stock(sym.replace(".T", ""), "TSEJ", "JPY")
    for suf, (exch, ccy) in _EU.items():
        if sym.endswith(suf):
            return Stock(sym.replace(suf, ""), exch, ccy)
    if sym.endswith("-USD"):
        base = sym.replace("-USD", "")
        return Crypto(base, "PAXOS", "USD")         # needs IB crypto permission
    if any(c in sym for c in (".", "=", "^", "-")):
        return None                                  # unknown / non-equity form
    return Stock(sym, "SMART", "USD", primaryExchange="NASDAQ")  # US


# NOTE (verify on paper before live):
#  - HK/JP have BOARD-LOT minimums; sizing rounds DOWN to whole shares here and
#    IB may reject a non-lot HK order. Refinement: read contractDetails.minSize
#    / round to lot. Flagged, not yet enforced.
#  - Crypto via IB requires the Paxos/ZeroHash permission on your account.
#  - .SS/.SZ (China A) and most monitor-only classes return None on purpose.
