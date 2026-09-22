#!/usr/bin/env python3
"""Golden tests for the daily NetLiq history and the growth-vs-S&P card.

Run from this directory:  python test_netliq_history.py

data/netliq_history.json is the ONLY record of what the account was worth each
day. Two things read it and both present the result as performance: the P&L
Calendar and the "Growth vs S&P 500" card on the Positions tab. Neither can tell
a trading gain from money moving unless the file says so, and both publishers
write a NetLiq that is already NET of earmarked cash (ib_bot.net_liq and
publish_web both do `nl -= exc`).

The EARMARK is how the owner's monthly routine stays out of that performance:
the GBP deposit is converted to HKD and earmarked the SAME day, withdrawn early
next month, and neither leg is entered in flows. Published "nl" is already net
of the earmark, so on both days it does not move - and must not be touched.
Board review 2026-09-19 added the day-over-day change in "exc" back into the
base; board review 2026-09-21 found that turns every such pass-through into a
fake gain on the deposit day and a fake loss on the withdrawal day, and took it
out again (every day in the history has exc 0 or none, so no past day changed).
What this file keeps in place:

  * BOTH publishers still write "exc" beside "nl" - it is shown, and one of
    them dropping it silently blinds the other's rows.
  * The dashboard measures each day's return against the previous nl plus the
    cash flows only; the earmark never enters the base or the profit.
"""
import io
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _src(*parts):
    return io.open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def t1_both_publishers_record_the_exclusion():
    """ib_bot and publish_web take turns writing the same file, all day.

    If either one omits "exc", the next publish from the other silently
    overwrites a correct row with a blind one - and the blind row reads as 0,
    which claims "no earmark today" rather than "unknown".
    """
    bot = _src("execution", "ib_bot.py").replace(" ", "")
    web = _src("execution", "publish_web.py").replace(" ", "")
    assert '"nl":round(nl),"exc":round(exc_pub or 0)'.replace(" ", "") in bot, \
        "ib_bot stopped publishing the earmark beside netliq"
    assert '"nl":round(nl),"exc":round(exc)'.replace(" ", "") in web, \
        "publish_web stopped publishing the earmark beside netliq"
    print("t1 both publishers record the earmark beside netliq OK")


def t2_the_earmark_stays_out_of_the_return():
    """A same-day earmarked pass-through must leave the return untouched.

    nl is already net of the earmark, so the deposit day and the withdrawal day
    both show no step. Adding the earmark change back (the 2026-09-19 rule)
    books the deposit as a gain and the withdrawal as a loss.
    """
    page = _src("docs", "index.html").replace(" ", "")
    assert "dExc" not in page, \
        "the earmark change is back in a P&L figure - a pass-through becomes a fake gain/loss"
    assert "+out.earmark" not in page, \
        "the profit row adds earmarked money back"
    assert "constbase=pts[i-1].nl+f;" in page, \
        "the vs-S&P card's base is no longer previous nl plus cash flows"
    # profit and percentage must agree: both are nl moves net of flows only
    assert "out.profit=Math.round(pts[pts.length-1].nl-pts[0].nl-out.flows);" in page, \
        "the profit row no longer nets exactly the cash flows"
    print("t2 the earmark stays out of the return and the profit OK")


def t3_the_page_states_what_the_benchmark_is():
    """engine/data_fetch downloads with auto_adjust=True.

    SPY's published closes are therefore dividend-adjusted, and the card must
    not tell the owner the opposite: a caveat claiming the index's dividends
    are missing invites him to mentally add ~1.2%/yr to the line he is losing
    to, which is exactly backwards.
    """
    fetch = _src("engine", "data_fetch.py").replace(" ", "")
    assert "auto_adjust=True" in fetch, \
        "prices are no longer dividend-adjusted - the card's caveat is now wrong"
    page = _src("docs", "index.html")
    assert "dividend-adjusted" in page, "the card stopped saying what SPY's line includes"
    assert "leaves out its" not in page.split("dividend-adjusted")[0][-400:], \
        "the card claims dividends are excluded while the download adjusts for them"
    print("t3 the benchmark caveat matches how prices are downloaded OK")


def t4_flows_are_named_not_netted():
    """+23,746 in and -32,000 out is not "-8,254 taken out".

    The owner reconciles this against his bank. Printing the net hides the
    deposit entirely, and on a window where they cancel it printed "no money
    went in or out" over a month that saw both.
    """
    page = _src("docs", "index.html").replace(" ", "")
    assert "r.moves===0" in page, "the no-flows sentence is back to testing the net"
    assert "out.inAmt+=f" in page and "out.outAmt+=f" in page, \
        "money in and money out are no longer tracked separately"
    print("t4 money in and money out are reported separately OK")


def t5_history_rows_stay_readable():
    """The live file must still parse and still be chainable by the calendar."""
    import json
    p = os.path.join(ROOT, "data", "netliq_history.json")
    if not os.path.exists(p):          # a clean checkout has no data/ payload
        print("t5 no local history file - skipped")
        return
    h = json.loads(io.open(p, encoding="utf-8").read())
    ser = h.get("series", [])
    assert ser and all("d" in e and "nl" in e for e in ser), "series row shape changed"
    assert all(isinstance(e.get("exc", 0), (int, float)) for e in ser), \
        "exc must be a number when present"
    ds = [e["d"] for e in ser]
    assert ds == sorted(ds), "series must stay sorted - the card chains it in order"
    assert len(set(ds)) == len(ds), "one row per day, upserted"
    for f in h.get("flows", []):
        assert "d" in f and "amt" in f, "flow row shape changed"
    print("t5 published history parses and stays chainable OK (%d days)" % len(ser))


if __name__ == "__main__":
    t1_both_publishers_record_the_exclusion()
    t2_the_card_nets_the_earmark_out_of_the_base()
    t3_the_page_states_what_the_benchmark_is()
    t4_flows_are_named_not_netted()
    t5_history_rows_stay_readable()
    print("ALL NETLIQ HISTORY TESTS PASS")
