#!/usr/bin/env python3
"""Golden tests: a symbol resolves to the listing on ITS exchange, or not at all.

Run from this directory:  python test_conid_resolver.py

Review 2026-09-17: broker.qualifyContracts dropped the primaryExchange that
contracts.to_ib set, and the conid cache was keyed "SYM|CCY|STK". A search for
SAN returns Santander's ADR (NYSE), Santander (BM, Madrid) and Sanofi (SBF,
Paris). Madrid was not in the euro venue set, so SAN.MC matched the one euro
row - Sanofi - and cached it under the key SAN.PA uses too (reproduced with a
stubbed search). place() would then re-base the limit onto Sanofi's ~76 quote:
129 shares sized for Santander at 12.14 become a ~EUR 9,900 order against
~EUR 1,566 funded.

What is locked down:
  * SAN.MC resolves to the BM listing and SAN.PA to the SBF one.
  * A venue with no listing, or no map entry, refuses - it never falls back to
    another exchange's listing, however unique that listing looks.
  * Cache keys carry the venue; an old "SAN|EUR|STK" entry is never read by a
    venue lookup. US lookups keep their old key and their old behaviour.
  * Madrid, Brussels, Helsinki, Vienna and Lisbon resolve; CHF, DKK, SEK and
    NOK still refuse, before any search.

Nothing here touches /root or the network: the conid cache is a temp file and
secdef/search is a stub.
"""
import json
import os
from pathlib import Path

os.environ.setdefault("IB_BACKEND", "web")
# Every /root default at a temp path before import (review 2026-09-17, test
# isolation: ib_web's OAuth dir was still /root here). See testenv.py.
import testenv                                      # noqa: E402
_TMP = testenv.isolate("mps-conid-")

import broker                                       # noqa: E402
import ib_orders                                    # noqa: E402
from contracts import to_ib                         # noqa: E402
testenv.assert_isolated()

CACHE = Path(ib_orders.CONID_CACHE)
assert str(CACHE).startswith(str(_TMP)), CACHE     # never /root/conid_cache.json

SAN_ADR, SANTANDER, SANOFI = 12001, 12002, 12003


def row(conid, exch, name, sec="STK"):
    return {"conid": str(conid), "description": exch, "companyName": name,
            "sections": [{"secType": sec}, {"secType": "OPT", "exchange": "CBOE"}]}


BOGUS = {"conid": "2147483647", "description": None,
         "companyName": "Corporate Fixed Income", "sections": [{"secType": "BOND"}]}

# secdef/search answers, by symbol. Shapes as IB returns them: description is
# the listing exchange, every answer carries the fixed-income catch-all row.
SEARCH = {
    "SAN": [row(SAN_ADR, "NYSE", "BANCO SANTANDER SA-SPON ADR"),
            row(SANTANDER, "BM", "BANCO SANTANDER SA"),
            row(SANOFI, "SBF", "SANOFI"), BOGUS],
    "SNOW": [row(444884365, "NYSE", "SNOWFLAKE INC"),
             row(539835325, "VALU", "SNOWBIRD NV"),
             row(382828517, "GETTEX", "SNOW INC"), BOGUS],
    # only ONE euro row, and it is on the wrong exchange for a Madrid lookup
    "ONLYPA": [row(13001, "SBF", "SOMETHING FRENCH")],
    "IBE": [row(14001, "BM", "IBERDROLA SA"), row(14002, "PINK", "IBERDROLA ADR")],
    "ABI": [row(15001, "ENEXT.BE", "ANHEUSER-BUSCH INBEV"), row(15002, "NYSE", "BUD ADR")],
    "NDA": [row(16001, "HEX", "NORDEA BANK ABP")],
    "EBS": [row(17001, "VSE", "ERSTE GROUP BANK")],
    "EDP": [row(18001, "BVL", "EDP")],
    "ALV": [row(19001, "IBIS", "ALLIANZ SE"), row(19002, "IBIS2", "AUTOLIV"),
            row(19003, "NYSE", "AUTOLIV INC")],
    "700": [row(20001, "SEHK", "TENCENT")],
    "7203": [row(21001, "TSEJ", "TOYOTA MOTOR")],
    "NESN": [row(22001, "EBS", "NESTLE SA")],
    "NOVO-B": [row(23001, "CPH", "NOVO NORDISK")],
    "VOLV-B": [row(24001, "SFB", "VOLVO")],
    "EQNR": [row(25001, "OSE", "EQUINOR"), row(25002, "NYSE", "EQUINOR ADR")],
    "DUAL": [row(26001, "NYSE", "DUAL ONE"), row(26002, "NASDAQ", "DUAL TWO")],
}
CALLS = []


def fake_get(path):
    CALLS.append(path)
    assert path.startswith("iserver/secdef/search?symbol="), path
    return SEARCH.get(path.split("=", 1)[1], [])


ib_orders._get = fake_get                           # the only network call made


def fresh(cache=None):
    del CALLS[:]
    if cache is None:
        if CACHE.exists():
            CACHE.unlink()
    else:
        CACHE.write_text(json.dumps(cache), encoding="utf-8")


def cached():
    return json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}


def qualify(ysym):
    """The live path: to_ib, then the web shim's qualifyContracts."""
    c = to_ib(ysym)
    q = broker.IB().qualifyContracts(c)
    return q[0].conId if q else None


def refuses(fn):
    try:
        fn()
    except ib_orders.OrderError as e:
        return str(e)
    raise AssertionError("expected a refusal")


def t1_same_symbol_two_exchanges_two_companies():
    fresh()
    assert qualify("SAN.MC") == SANTANDER, "SAN.MC must be Santander on BM"
    assert qualify("SAN.PA") == SANOFI, "SAN.PA must be Sanofi on SBF"
    # and in the other order, from a warm cache: neither can serve the other
    fresh()
    assert qualify("SAN.PA") == SANOFI
    assert qualify("SAN.MC") == SANTANDER
    book = cached()
    assert book == {"SAN|EUR|STK|SBF": SANOFI, "SAN|EUR|STK|BM": SANTANDER}, book
    del CALLS[:]
    assert qualify("SAN.MC") == SANTANDER and qualify("SAN.PA") == SANOFI
    assert CALLS == [], "a venue lookup must be served from its own cache key"
    print("t1 SAN.MC resolves to Santander on BM, SAN.PA to Sanofi on SBF OK")


def t2_unmatched_exchange_refuses_never_falls_back():
    fresh()
    # BVME (Milan) is mapped, but SAN has no Milan listing
    msg = refuses(lambda: ib_orders.resolve_conid("SAN", "EUR", "STK", primary_exchange="BVME"))
    assert "on BVME" in msg and "refusing" in msg, msg
    # the one euro row is on SBF: a Madrid lookup must not take it
    msg = refuses(lambda: ib_orders.resolve_conid("ONLYPA", "EUR", primary_exchange="BM"))
    assert "on BM" in msg and "SBF" in msg, msg
    assert qualify("ONLYPA.MC") is None, "the shim must omit it, not trade Paris"
    # a venue with no map entry refuses before the cache or the network
    fresh({"SAN|EUR|STK|XYZ": SANOFI})
    msg = refuses(lambda: ib_orders.resolve_conid("SAN", "EUR", primary_exchange="XYZ"))
    assert "no listing map for exchange 'XYZ'" in msg, msg
    assert CALLS == [], CALLS
    # a mapped venue in the wrong currency refuses too
    msg = refuses(lambda: ib_orders.resolve_conid("SAN", "GBP", primary_exchange="BM"))
    assert "does not trade in GBP" in msg, msg
    # nothing refused was cached
    assert cached() == {"SAN|EUR|STK|XYZ": SANOFI}, cached()
    print("t2 no listing on the named exchange refuses, never another venue's OK")


def t3_old_cache_keys_are_never_used_for_a_venue_lookup():
    # What the VM may hold today: Sanofi cached under the shared old key.
    fresh({"SAN|EUR|STK": SANOFI, "IBE|EUR|STK": 99999})
    assert qualify("SAN.MC") == SANTANDER
    assert qualify("IBE.MC") == 14001
    assert len(CALLS) == 2, "each venue lookup searched instead of reading the old key"
    book = cached()
    assert book["SAN|EUR|STK|BM"] == SANTANDER and book["IBE|EUR|STK|BM"] == 14001
    assert book["SAN|EUR|STK"] == SANOFI, "old entries are left alone, just never read"
    print("t3 old SYM|CCY|STK cache entries ignored by venue lookups OK")


def t4_us_symbols_resolve_exactly_as_before():
    # to_ib stamps primaryExchange=NASDAQ on every US name; SNOW is on NYSE.
    assert to_ib("SNOW").primaryExchange == "NASDAQ"
    fresh()
    assert qualify("SNOW") == 444884365
    assert cached() == {"SNOW|USD|STK": 444884365}, "US keeps the old key"
    # a warm old-format entry still serves a US lookup, with no search
    fresh({"SNOW|USD|STK": 444884365})
    assert qualify("SNOW") == 444884365 and CALLS == []
    # same answer as a lookup that names no venue at all
    fresh()
    assert ib_orders.resolve_conid("SNOW", "USD", "STK") == 444884365
    # two US listings are still ambiguous, and still refused
    fresh()
    assert qualify("DUAL") is None
    msg = refuses(lambda: ib_orders.resolve_conid("DUAL", "USD", "STK"))
    assert "ambiguous" in msg, msg
    print("t4 US symbols resolve as before, NASDAQ stamp ignored OK")


def t5_new_euro_venues_resolve_swiss_nordic_still_refuse():
    fresh()
    assert qualify("IBE.MC") == 14001               # Madrid
    assert qualify("ABI.BR") == 15001               # Brussels
    assert ib_orders.resolve_conid("NDA", "EUR", primary_exchange="HEX") == 16001
    assert ib_orders.resolve_conid("EBS", "EUR", primary_exchange="VSE") == 17001
    assert ib_orders.resolve_conid("EDP", "EUR", primary_exchange="BVL") == 18001
    # Xetra's own listing, not a same-symbol foreign line on IBIS2
    assert qualify("ALV.DE") == 19001
    assert qualify("0700.HK") == 20001 and qualify("7203.T") == 21001
    # CHF, DKK, SEK and NOK are not traded: refused before any search
    fresh()
    for ysym in ("NESN.SW", "NOVO-B.CO", "VOLV-B.ST", "EQNR.OL"):
        assert qualify(ysym) is None, ysym
    assert CALLS == [], CALLS
    for sym, ccy, venue in (("NESN", "CHF", "EBS"), ("NOVO-B", "DKK", "CPH"),
                            ("VOLV-B", "SEK", "SFB"), ("EQNR", "NOK", "OSE")):
        refuses(lambda: ib_orders.resolve_conid(sym, ccy, primary_exchange=venue))
        assert venue not in ib_orders._VENUE_LISTINGS, venue
        assert ccy not in ib_orders._CCY_EXCHANGES, ccy
    # SIX Swiss is a CHF venue: gone from the euro set, never an alias
    assert "EBS" not in ib_orders._CCY_EXCHANGES["EUR"]
    assert not any("EBS" in v for v in ib_orders._VENUE_LISTINGS.values())
    assert qualify("NESN.PA") is None               # a Paris lookup cannot land on EBS
    msg = refuses(lambda: ib_orders.resolve_conid("NESN", "EUR", "STK"))
    assert "no STK listing" in msg and "EBS" in msg, msg
    print("t5 BM, ENEXT.BE, HEX, VSE, BVL resolve; CHF/DKK/SEK/NOK refuse OK")


def t6_the_shim_passes_the_venue_and_nothing_else_changed():
    seen = []
    real = ib_orders.resolve_conid

    def spy(*a, **k):
        seen.append((a, k))
        return 1
    ib_orders.resolve_conid = spy
    try:
        broker.IB().qualifyContracts(to_ib("SAN.MC"), to_ib("SNOW"), to_ib("0700.HK"))
    finally:
        ib_orders.resolve_conid = real
    assert [k.get("primary_exchange") for _, k in seen] == ["BM", "", "SEHK"], seen
    assert [a for a, _ in seen] == [("SAN", "EUR", "STK"), ("SNOW", "USD", "STK"),
                                    ("700", "HKD", "STK")], seen
    print("t6 qualifyContracts passes the listing venue (none for US) OK")


if __name__ == "__main__":
    t1_same_symbol_two_exchanges_two_companies()
    t2_unmatched_exchange_refuses_never_falls_back()
    t3_old_cache_keys_are_never_used_for_a_venue_lookup()
    t4_us_symbols_resolve_exactly_as_before()
    t5_new_euro_venues_resolve_swiss_nordic_still_refuse()
    t6_the_shim_passes_the_venue_and_nothing_else_changed()
    print("ALL CONID RESOLVER TESTS PASS")
