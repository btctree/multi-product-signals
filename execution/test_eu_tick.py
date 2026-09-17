#!/usr/bin/env python3
"""Golden tests for European limit prices: RTS 11 floor, IB's bands, smarter retry.

Run from this directory:  python test_eu_tick.py

2026-09-13/14: BAYN went out at 48.4108 and DBK at 34.3207, both snapped to
IB's minTick 0.0001 - the LOWEST band of a price-banded ladder, and never a
legal tick for a share priced 20 or more. Each was refused (Error 110) before a
legal price went through. The retry ladder then walked fixed rungs from 0.0001,
so a name ticking 0.5 (ASML or RMS at ~1,600 if band 5) could burn all six
attempts, and it resent identical prices along the way.

Now, for EUR/CHF/DKK/SEK/NOK only, the first tick is the coarsest of IB's
minTick, the MiFID II RTS 11 band-6 floor and IB's own band at the price; a
refusal retries at the increment IB's error text names; and a price IB already
refused is never resent. USD, HKD and JPY first prices must not move a byte.
"""
import os

os.environ.setdefault("IB_BACKEND", "web")
# Every /root default at a temp path before import (review 2026-09-17, test
# isolation: none was repointed here). See testenv.py.
import testenv                                      # noqa: E402
testenv.isolate("mps-eutick-")
import broker                                       # noqa: E402
import ib_bot                                       # noqa: E402
testenv.assert_isolated()

ib_bot.log = lambda *a, **k: None                   # keep the output to tN lines

# The band-6 column (ADNT >= 9,000) of Commission Delegated Regulation (EU)
# 2017/588, Annex, restated independently of ib_bot's table: (lower edge, tick),
# lower-inclusive. Checked against EUR-Lex CELEX:32017R0588 on 2026-09-17.
ANNEX_BAND6 = [(0.0, 0.0001), (0.1, 0.0001), (0.2, 0.0001), (0.5, 0.0001),
               (1.0, 0.0002), (2.0, 0.0005), (5.0, 0.001), (10.0, 0.002),
               (20.0, 0.005), (50.0, 0.01), (100.0, 0.02), (200.0, 0.05),
               (500.0, 0.1), (1000.0, 0.2), (2000.0, 0.5), (5000.0, 1),
               (10000.0, 2), (20000.0, 5), (50000.0, 10)]


class Patch:
    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(ib_bot, k)
            setattr(ib_bot, k, v)
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(ib_bot, k, v)


def on_grid(p, t):
    return abs(round(p / t) - p / t) < 1e-6


class CD:
    """A contract-details answer; no priceBands/isFallback = ib_async's shape."""

    def __init__(self, minTick=0.0001, bands=None, fallback=None):
        self.minTick = minTick
        if bands is not None:
            self.priceBands = bands
        if fallback is not None:
            self.isFallback = fallback


class Entry:
    def __init__(self, m):
        self.message = m


def web_refusal(tick):
    """IB's own Web API text, as ib_orders raises it and broker translates it."""
    return lambda p: broker._translate_error(
        "order not accepted: The price %s does not conform to the minimum price "
        "variation of %s for this instrument." % (p, tick))


def socket_refusal(p):
    """ib_async's Error 110 names no increment: the rung ladder must still work."""
    return "Error 110, reqId 5: The price does not conform to the minimum price variation for this contract."


class VenueIB:
    """Accepts only prices on the venue's real grid; refuses the rest like IB."""

    def __init__(self, accept, refusal, details=None):
        self.accept, self.refusal = accept, refusal
        self.details = details if details is not None else [CD()]
        self.sent, self.detail_calls = [], 0

    def reqContractDetails(self, c):
        self.detail_calls += 1
        return self.details

    def placeOrder(self, contract, order):
        t = broker.Trade(contract, order)
        p = order.lmtPrice
        self.sent.append(p)
        if self.accept(p):
            t.orderStatus.status = "PreSubmitted"
        else:
            t.orderStatus.status = "Inactive"
            t.log.append(Entry(self.refusal(p)))
        return t

    def sleep(self, n):
        pass


class C:
    def __init__(self, symbol, currency, conId):
        self.symbol, self.currency, self.conId, self.secType = symbol, currency, conId, "STK"


def place(ib, contract, price, action="BUY"):
    ib_bot._TICK_CACHE.clear()
    before = len(ib_bot.PLACED)
    with Patch(live_base_price=lambda ib, c, fallback: fallback, CONFIRM_FIRST=False):
        status = ib_bot.place(ib, contract, action, 10, price, False)
    row = ib_bot.PLACED[before] if len(ib_bot.PLACED) > before else None
    return status, row


def t1_rts11_floor_every_annex_boundary():
    assert [t for _, t in ANNEX_BAND6] == [ib_bot.eu_floor_tick(e) for e, _ in ANNEX_BAND6]
    for i, (edge, tick) in enumerate(ANNEX_BAND6):
        # lower-inclusive: the edge itself belongs to the range it opens
        assert ib_bot.eu_floor_tick(edge) == tick, (edge, ib_bot.eu_floor_tick(edge))
        if i:
            below = ANNEX_BAND6[i - 1][1]
            assert ib_bot.eu_floor_tick(edge - 1e-6) == below, (edge, below)
        upper = ANNEX_BAND6[i + 1][0] if i + 1 < len(ANNEX_BAND6) else edge * 3
        assert ib_bot.eu_floor_tick((edge + upper) / 2) == tick, edge
    assert ib_bot.eu_floor_tick(1e9) == 10
    # the live cases: BAYN ~48 and DBK ~34 tick 0.005 at best, ASML ~1,577 0.2
    assert ib_bot.eu_floor_tick(48.4108) == 0.005
    assert ib_bot.eu_floor_tick(34.3207) == 0.005
    assert ib_bot.eu_floor_tick(1576.845) == 0.2
    assert ib_bot.EU_TICK_CCY == frozenset(("EUR", "CHF", "DKK", "SEK", "NOK"))
    print("t1 RTS 11 band-6 floor at every Annex boundary OK")


def t2_band_edge_resnap():
    # On the Annex's own grid a snap can only reach the edge itself, which is a
    # multiple of the ticks on BOTH sides - so every landing is legal.
    tick, lim = ib_bot.eu_limit(49.998, 0.0001, [])
    assert (tick, lim) == (0.005, 50.0), (tick, lim)
    assert ib_bot.eu_floor_tick(lim) == 0.01 and on_grid(lim, 0.01)
    for i, (edge, _) in enumerate(ANNEX_BAND6[1:], 1):
        below = ANNEX_BAND6[i - 1][1]
        for k in range(-7, 8):
            raw = edge + k * below * 0.37
            tick, lim = ib_bot.eu_limit(raw, 0.0001, [])
            landed = ib_bot.eu_floor_tick(lim)
            assert on_grid(lim, landed), (raw, lim, landed)
            assert abs(lim - raw) <= tick / 2 + 1e-9, (raw, lim, tick)

    # An IB band edge need not be a multiple of the band below it. Snapping
    # 49.999 on 0.03 lands on 50.01, inside the 0.1 band and OFF its grid: the
    # re-snap must put it back on a legal price for wherever it settles.
    bands = [(0.0, 0.03), (50.0, 0.1)]
    assert ib_bot.snap_to_tick(49.999, 0.03) == 50.01          # the naive landing
    tick, lim = ib_bot.eu_limit(49.999, 0.0001, bands)
    assert lim >= 50.0 and on_grid(lim, 0.1), (tick, lim)
    # ...and when the re-snap falls back BELOW the edge, it is legal there too
    bands = [(0.0, 0.03), (50.0, 0.07)]
    assert ib_bot.snap_to_tick(49.9955, 0.03) == 50.01
    tick, lim = ib_bot.eu_limit(49.9955, 0.0001, bands)
    assert lim < 50.0 and on_grid(lim, 0.03), (tick, lim)
    for raw in [49.9 + i * 0.0037 for i in range(60)]:
        for bands in ([(0.0, 0.03), (50.0, 0.1)], [(0.0, 0.03), (50.0, 0.07)]):
            tick, lim = ib_bot.eu_limit(raw, 0.0001, bands)
            assert on_grid(lim, max(ib_bot.eu_floor_tick(lim), ib_bot.band_tick(bands, lim))), \
                (raw, bands, lim)

    # and place() is the one using it: the FIRST price sent is already legal
    ib = VenueIB(lambda p: on_grid(p, 0.1 if p >= 50 else 0.03), web_refusal(0.1),
                 [CD(0.03, bands=[(0.0, 0.03), (50.0, 0.1)])])
    status, row = place(ib, C("EDGE", "EUR", 501), 49.999 / 1.005)
    assert len(ib.sent) == 1 and status == "sent", (ib.sent, status)
    print("t2 band-edge re-snap lands on the grid of the range it lands in OK")


class FakeClient:
    def __init__(self, payload):
        self.payload = payload

    def get(self, path):
        if isinstance(self.payload, Exception):
            raise self.payload

        class R:
            pass
        r = R()
        r.data = self.payload
        return r


def shim_details(payload):
    real = broker.ib_web.client
    try:
        broker.ib_web.client = lambda: FakeClient(payload)
        [cd] = broker.IB().reqContractDetails(broker.Contract("BAYN", "STK", "EUR",
                                                              conId=68598660))
    finally:
        broker.ib_web.client = real
    return cd


def t3_ib_bands_parsed_selected_and_failures_not_cached():
    # The shim: minTick stays incrementRules[0] exactly as before; the full
    # ladder comes along sorted, with malformed rows dropped.
    cd = shim_details({"rules": {"increment": 0.0001, "incrementRules": [
        {"lowerEdge": 0.0, "increment": 0.0001},
        {"lowerEdge": 50.0, "increment": 0.01},
        {"lowerEdge": "20", "increment": "0.005"},
        {"lowerEdge": None, "increment": 0.5},
        {"lowerEdge": 100.0, "increment": 0}]}})
    assert cd.minTick == 0.0001 and cd.isFallback is False, vars(cd)
    assert cd.priceBands == [(0.0, 0.0001), (20.0, 0.005), (50.0, 0.01)], cd.priceBands
    # minTick is incrementRules[0] AS SENT, exactly as before this change - even
    # if IB ever lists the bands out of order; only priceBands is sorted.
    cd = shim_details({"rules": {"incrementRules": [
        {"lowerEdge": 50.0, "increment": 0.01}, {"lowerEdge": 0.0, "increment": 0.0001}]}})
    assert cd.minTick == 0.01 and cd.priceBands == [(0.0, 0.0001), (50.0, 0.01)], vars(cd)
    # single-band payload, the only shape IB's docs show
    cd = shim_details({"rules": {"incrementRules": [{"lowerEdge": 0.0, "increment": 0.01}]}})
    assert (cd.minTick, cd.priceBands, cd.isFallback) == (0.01, [(0.0, 0.01)], False), vars(cd)
    # a flat increment with no ladder is still IB's answer...
    cd = shim_details({"rules": {"increment": 0.05}})
    assert (cd.minTick, cd.priceBands, cd.isFallback) == (0.05, [], False), vars(cd)
    # ...but an empty payload or a failed request is the shim's default
    for payload in ({}, RuntimeError("session")):
        cd = shim_details(payload)
        assert (cd.minTick, cd.priceBands, cd.isFallback) == (0.01, [], True), vars(cd)

    # Selection: the increment of the largest lowerEdge at or below the price.
    bands = [(0.0, 0.0001), (20.0, 0.005), (50.0, 0.01)]
    for price, tick in ((0.0, 0.0001), (19.999, 0.0001), (20.0, 0.005),
                        (49.99, 0.005), (50.0, 0.01), (1e6, 0.01)):
        assert ib_bot.band_tick(bands, price) == tick, (price, tick)
    assert ib_bot.band_tick([(10.0, 0.5)], 9.99) == 0.0        # below every edge
    assert ib_bot.band_tick([], 48.0) == 0.0
    assert ib_bot.band_tick([(0.0, 0.01)], 48.0) == 0.01       # single band

    # Caching: a failure is never kept - raised, empty, or the shim's fallback.
    class FlakyIB:
        def __init__(self, answers):
            self.answers, self.calls = list(answers), 0

        def reqContractDetails(self, c):
            self.calls += 1
            a = self.answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a

    c = C("BAYN", "EUR", 68598660)
    ib_bot._TICK_CACHE.clear()
    ib = FlakyIB([RuntimeError("x"), [], [CD(fallback=True)],
                  [CD(bands=[(20.0, 0.01), (0.0, 0.0001)], fallback=False)]])
    for _ in range(3):
        assert ib_bot.ib_band_tick(ib, c, 48.4) == 0.0
        assert ("bands", 68598660) not in ib_bot._TICK_CACHE
    assert ib_bot.ib_band_tick(ib, c, 48.4) == 0.01 and ib.calls == 4
    assert ib_bot.ib_band_tick(ib, c, 10.0) == 0.0001 and ib.calls == 4   # cached
    # ib_async's ContractDetails has no priceBands: a success, cached as []
    ib_bot._TICK_CACHE.clear()
    ib = FlakyIB([[CD()]])
    assert ib_bot.ib_price_bands(ib, c) == [] and ib_bot.ib_price_bands(ib, c) == []
    assert ib.calls == 1

    # min_tick's one request feeds the bands too - no second call - but its
    # cached 0.01 fallback must not stand in for bands
    ib_bot._TICK_CACHE.clear()
    ib = FlakyIB([[CD(0.0001, bands=[(0.0, 0.0001), (20.0, 0.01)], fallback=False)]])
    assert ib_bot.min_tick(ib, c) == 0.0001
    assert ib_bot.ib_band_tick(ib, c, 48.0) == 0.01 and ib.calls == 1
    ib_bot._TICK_CACHE.clear()
    ib = FlakyIB([[CD(0.01, fallback=True)], [CD(0.0001, bands=[(20.0, 0.01)])]])
    assert ib_bot.min_tick(ib, c) == 0.01
    assert ib_bot.ib_band_tick(ib, c, 48.0) == 0.01 and ib.calls == 2

    # and place() makes exactly ONE details request for a European order
    ib = VenueIB(lambda p: on_grid(p, 0.01), web_refusal(0.01),
                 [CD(0.0001, bands=[(0.0, 0.0001), (20.0, 0.01)], fallback=False)])
    status, row = place(ib, C("BAYN", "EUR", 68598660), 48.1741)
    # IB's band (0.01) beats the RTS 11 floor (0.005): 48.41, not 48.415
    assert ib.sent == [48.41] and ib.detail_calls == 1, (ib.sent, ib.detail_calls)
    print("t3 IB bands parsed, selected, and failures never cached OK")


def t4_bayn_replay_first_attempt_is_legal():
    # Live 2026-09-13/14: BUY 33 BAYN off 48.17, raw 48.41085, sent at 48.4108 on
    # IB's 0.0001 first band and refused "minimum price variation of 0.01".
    raw = 48.17 * (1 + ib_bot.LIMIT_BUFFER)
    assert ib_bot.snap_to_tick(raw, 0.0001) == 48.4108          # what went out live
    for details in ([CD(0.0001)],                                 # no bands (as live)
                    [CD(0.0001, bands=[(0.0, 0.0001)], fallback=False)]):  # single band
        ib = VenueIB(lambda p: on_grid(p, 0.01), web_refusal(0.01), details)
        status, row = place(ib, C("BAYN", "EUR", 68598660), 48.17)
        assert ib.sent == [48.41], ib.sent                        # first attempt, legal
        assert status == "sent" and row["limit"] == 48.41, (status, row)
    # DBK the same night: 34.3207 refused, 34.32 filled
    ib = VenueIB(lambda p: on_grid(p, 0.005), web_refusal(0.005))
    place(ib, C("DBK", "EUR", 14121), 34.3207 / 1.005)
    assert len(ib.sent) == 1 and on_grid(ib.sent[0], 0.005), ib.sent
    print("t4 BAYN replay: 48.4108 now goes out as a legal 48.41 first time OK")


def t5_band4_refusal_retries_at_the_stated_tick():
    # A band-4 name at 20-50 ticks 0.02. The band-6 floor gives 30.25; IB refuses
    # and names 0.02, so attempt 2 is 30.26 - not the rung walk's 30.3, which
    # would pay 4 cents more for the same share.
    ib = VenueIB(lambda p: on_grid(p, 0.02), web_refusal(0.02))
    status, row = place(ib, C("B4", "EUR", 44), 30.1)
    assert ib.sent == [30.25, 30.26], ib.sent
    assert status == "sent" and row["limit"] == 30.26, (status, row)
    # without a number in the text (socket), the rungs still recover it
    ib = VenueIB(lambda p: on_grid(p, 0.02), socket_refusal)
    status, row = place(ib, C("B4", "EUR", 44), 30.1)
    assert ib.sent == [30.25, 30.3] and status == "sent", (ib.sent, status)
    # the parser itself
    assert ib_bot.ib_stated_tick(web_refusal(0.02)(30.25)) == 0.02
    assert ib_bot.ib_stated_tick("... minimum price variation of 0.5.") == 0.5
    assert ib_bot.ib_stated_tick(socket_refusal(1.0)) == 0.0
    assert ib_bot.ib_stated_tick("minimum price variation of .") == 0.0
    assert ib_bot.ib_stated_tick(None) == 0.0
    print("t5 band-4 refusal lands on IB's stated 0.02 tick at attempt 2 OK")


def t6_a_refused_price_is_never_resent():
    ladder = [0.0001, 0.001, 0.01, 0.05, 0.1, 0.2, 0.5, 1, 5, 10, 50, 100, 500, 1000]
    # 1577.0 refused: the 0.5 and 1 rungs would both resend 1577, so the walk
    # goes straight on to 5 -> 1575, and that is attempt 2, not attempt 4.
    ib = VenueIB(lambda p: on_grid(p, 5), socket_refusal)
    status, row = place(ib, C("B2", "EUR", 22), 1576.94 / 1.005)
    assert ib.sent == [1577.0, 1575], ib.sent
    assert status == "sent" and row["limit"] == 1575, (status, row)
    # a stated tick whose price was already refused moves on up the rungs too
    assert ib_bot._retry_price(1576.94, 0.2, web_refusal(0.5)(1577.0), [1577.0],
                               ladder) == (5, 1575)
    # nothing new left to try: stop rather than resend
    assert ib_bot._retry_price(1576.94, 500, socket_refusal(1), [1577.0, 2000],
                               ladder) == (1000, None)
    # a limit of 0 is never a legal answer (a SELL at 0 sells at any price)
    assert ib_bot._retry_price(0.3, 0.5, socket_refusal(1), [0.5], ladder)[1] is None
    # refused everywhere: the walk runs out of NEW prices before six, stops, and
    # records the last price IB actually saw
    ib = VenueIB(lambda p: False, web_refusal(5))
    status, row = place(ib, C("XYZ", "EUR", 1), 846.6)
    assert ib.sent == [850.8, 850, 900, 1000], ib.sent
    assert len(set(ib.sent)) == len(ib.sent)
    assert status == "REJECTED" and row["limit"] == ib.sent[-1], (status, row)
    print("t6 refused prices are never resent, and cost no attempt OK")


def t7_half_tick_name_succeeds_within_six():
    # ASML ~1,569 if band 5 ticks 0.5 at 1,000-2,000. From 0.0001 it failed ~70%
    # of the time; now it is attempt 2 at worst, with or without IB's number.
    ib = VenueIB(lambda p: on_grid(p, 0.5), web_refusal(0.5))
    status, row = place(ib, C("ASML", "EUR", 117902840), 1569.0)
    assert ib.sent == [1576.8, 1577.0] and status == "sent", (ib.sent, status)
    for refusal in (web_refusal(0.5), socket_refusal):
        for i in range(400):
            price = (1000.0 + i * 2.4987) / 1.005
            for action in ("BUY", "SELL"):
                ib = VenueIB(lambda p: on_grid(p, 0.5 if p < 2000 else 1.0), refusal)
                status, row = place(ib, C("ASML", "EUR", 117902840), price, action)
                assert status == "sent", (price, action, ib.sent)
                assert len(ib.sent) <= 6 and len(set(ib.sent)) == len(ib.sent), ib.sent
    print("t7 a 0.5-tick name succeeds within six attempts OK")


def old_first_price(ccy, mt, price, action):
    """place()'s first limit BEFORE this change, verbatim."""
    raw = price * (1 + ib_bot.LIMIT_BUFFER) if action == "BUY" else price * (1 - ib_bot.LIMIT_BUFFER)
    tick = mt
    if ccy == "JPY":
        tick = max(tick, ib_bot.jp_tick(raw))
    elif ccy == "HKD":
        tick = max(tick, ib_bot.hk_tick(raw))
    return ib_bot.snap_to_tick(raw, tick)


def t8_usd_hkd_jpy_first_prices_unchanged():
    # Pinned live values: DXCM 83.03 -> 83.45, and test_hk_market's t8 limits.
    for ccy, mt, price, expect in (("USD", 0.01, 83.03, 83.45),
                                   ("HKD", 0.01, 188.8, 189.7),
                                   ("HKD", 0.001, 47.0, 47.24),
                                   ("HKD", 0.001, 8.0, 8.04),
                                   ("JPY", 1.0, 2345.0, 2357),
                                   ("JPY", 0.1, 24700.0, 24820)):
        ib = VenueIB(lambda p: True, socket_refusal, [CD(mt)])
        place(ib, C("X", ccy, 900), price)
        assert repr(ib.sent[0]) == repr(expect), (ccy, price, ib.sent, expect)
    # and byte-for-byte against the old formula across a price sweep, both sides
    for ccy, ticks in (("USD", (0.01, 0.0001)), ("HKD", (0.001, 0.01)),
                       ("JPY", (1.0, 0.1)), ("GBP", (0.01, 0.0001))):
        for mt in ticks:
            for i in range(300):
                price = 0.37 + i * i * 0.731
                for action in ("BUY", "SELL"):
                    ib = VenueIB(lambda p: True, socket_refusal, [CD(mt, bands=[(0.0, 5.0)])])
                    place(ib, C("X", ccy, 901), price, action)
                    old = old_first_price(ccy, mt, price, action)
                    assert repr(ib.sent[0]) == repr(old), (ccy, mt, price, action, ib.sent, old)
                    assert ib.detail_calls == 1, (ccy, ib.detail_calls)   # no band lookup
    print("t8 USD, HKD and JPY first prices unchanged byte-for-byte OK")


if __name__ == "__main__":
    t1_rts11_floor_every_annex_boundary()
    t2_band_edge_resnap()
    t3_ib_bands_parsed_selected_and_failures_not_cached()
    t4_bayn_replay_first_attempt_is_legal()
    t5_band4_refusal_retries_at_the_stated_tick()
    t6_a_refused_price_is_never_resent()
    t7_half_tick_name_succeeds_within_six()
    t8_usd_hkd_jpy_first_prices_unchanged()
    print("ALL EU TICK TESTS PASS")
