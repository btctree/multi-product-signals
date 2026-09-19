#!/usr/bin/env python3
"""Golden tests for the daily NetLiq history and the growth-vs-S&P card.

Run from this directory:  python test_netliq_history.py

data/netliq_history.json is the ONLY record of what the account was worth each
day. Two things read it and both present the result as performance: the P&L
Calendar and the "Growth vs S&P 500" card on the Positions tab. Neither can tell
a trading gain from money moving unless the file says so, and both publishers
write a NetLiq that is already NET of earmarked cash (ib_bot.net_liq and
publish_web both do `nl -= exc`).

So a day whose EARMARK moved - the owner's documented monthly routine: mark the
GBP deposit, convert, withdraw, clear - steps the series with no flows row
behind it, and a reader that chains the series books that step as performance.
Board review 2026-09-19 reproduced it: a 23,746 earmark step turns the card's
"+8.7%, AHEAD 6.1%" into "-2.8%, BEHIND 5.5%", and leaves a permanent -11.2%
"Worst dip". The fix is one field, and this file is what keeps it there:

  * BOTH publishers must write "exc" beside "nl" - one of them dropping it is
    enough, because they take turns writing the same file all day.
  * The dashboard must net the day-over-day change in "exc" out of the base it
    measures each day's return against, exactly as it does for a cash flow.
  * Rows written before the field existed must keep reading as zero, which is
    true of the whole backfilled history: no earmark step sits inside it.
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


def t2_the_card_nets_the_earmark_out_of_the_base():
    """The step must leave the return, not just be recorded.

    Recording "exc" is useless on its own: the card has to subtract the
    day-over-day change from the base it divides by, the same treatment a
    deposit gets.
    """
    page = _src("docs", "index.html").replace(" ", "")
    assert "constdExc=pts[i].exc-pts[i-1].exc" in page, \
        "the vs-S&P card no longer measures the earmark step"
    assert "constbase=pts[i-1].nl+f-dExc" in page, \
        "the earmark step is no longer removed from the base"
    assert "exc:r.exc||0" in page, \
        "rows written before the field existed must still read as zero"
    # profit must lose the same amount it lost from the percentage, or the two
    # numbers on the card contradict each other
    assert "-out.flows+out.earmark" in page, \
        "the profit row no longer removes earmarked money"
    print("t2 the card removes the earmark step from the return OK")


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
