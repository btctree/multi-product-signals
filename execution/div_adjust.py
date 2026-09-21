#!/usr/bin/env python3
"""Following dividends and splits - the one rule, shared.

ib_bot.py (the trading run) and daily_signal.py (the digest, python 3.9, which
must never import ib_bot) both replay the trailing stop. They must follow a
dividend or split the same way or the digest lists a SELL the bot will not
make, so the rule lives here, stdlib only, the way market_clock.py is shared.

WHY. The engine prices everything DIVIDEND- and SPLIT-ADJUSTED (data_fetch uses
yfinance auto_adjust=True): when a stock goes ex-dividend, Yahoo scales every
EARLIER bar down instead of showing a drop. The validated backtest runs on that
series, so its trailing stop never sees an ex-dividend day.
The bot does not re-derive its stop from that series each run. It keeps a
high-water mark and a stop in the state file, ratcheted up run after run - and
until 2026-09-21 nothing ever scaled them down. So on an ex-dividend day the
price fell by the dividend while the stop stayed on pre-dividend prices, and a
position sitting near its stop could be sold on a drop that was not a loss.
Backtested before changing anything (operator-approved 2026-09-21): 61 of 2,089
exits changed; the effect on RETURNS is noise and its sign flips between
models, but single trades moved up to 15.8 points either way, and rescaling
reproduces the validated run exactly - 2,087 of 2,087 exits identical.
A split does the same thing far harder (a 2-for-1 halves the price), and this
covers it with the same code.

HOW THE STEP IS READ. Not from a dividend feed but from the card's own adjusted
series: remember a few closes as this run saw them, and next run look the SAME
dates up again. If the history was rescaled in between, every remembered date
moved by the same factor.

WHICH closes. Not the newest. Yahoo sometimes rescales a day or more after the
ex-date, and the newest close can be filled from a live quote. A remembered
close ON or AFTER the ex-date would not move when the rescale lands, the dates
would disagree ([f, f, 1]), the event would be rejected as a glitch - and the
memory overwritten, so it was lost for good (board review 2026-09-21; 8 US
names went ex that day and were still unadjusted after the close). So the
remembered closes are older than the newest PX_REF_SKIP bars: a rescale up to
about that many bars late still moves all of them together, and is followed.
Each rescale is counted once, because every run re-records what it now sees.
"""

PX_REF_BARS = 3
PX_REF_SKIP = 5            # must be >= 1
# Prices are published to 4 dp, so a ratio carries rounding noise of up to
# ~1e-4 on a $1 stock. Agreement is judged well above that, and anything closer
# to 1 than the floor is noise, not an event (a 0.02% dividend moves nothing).
ADJ_AGREE_TOL = 5e-4
ADJ_NOISE_FLOOR = 2e-4
# A reverse split can push the factor above 1 and a 50-for-1 split below 0.02,
# but neither is plausible here; outside this band it is far likelier to be a
# damaged card than an event, and moving a live stop on it would be worse. The
# band edges carry the same rounding tolerance as the agreement test, or an
# exact 50-for-1 split is rejected about half the time on 4 dp closes.
ADJ_MIN, ADJ_MAX = 0.02, 50.0


def px_ref(prices, n=PX_REF_BARS, skip=PX_REF_SKIP):
    """n [date, close] pairs of a card's adjusted series, as seen NOW - the n
    closes just before the newest `skip` bars (see the module note)."""
    try:
        rows = [[str(d), float(v)] for d, v in (prices or []) if v]
    except (TypeError, ValueError):
        return []
    return rows[-(n + skip):-skip]


def adjustment_since(ref, prices):
    """The factor the card's history was rescaled by since `ref` was taken.

    Returns None when there was no event, when it cannot be told, or when the
    evidence disagrees. A real dividend or split rescales EVERY earlier bar by
    the same factor, so the remembered dates must all agree; a single bad bar
    moves one of them and is rejected rather than acted on.
    """
    if not ref:
        return None
    try:
        now = {str(d): float(v) for d, v in (prices or []) if v}
    except (TypeError, ValueError):
        return None
    fs = []
    for d, old in ref:
        try:
            old = float(old)
        except (TypeError, ValueError):
            continue
        if old > 0 and d in now and now[d] > 0:
            fs.append(now[d] / old)
    if len(fs) < 2:
        return None                          # not enough evidence to move a stop
    fs.sort()
    f = fs[len(fs) // 2]
    if any(abs(x / f - 1.0) > ADJ_AGREE_TOL for x in fs):
        return None                          # the dates disagree: a glitch, not an event
    if abs(f - 1.0) < ADJ_NOISE_FLOOR:
        return None                          # rounding, not a dividend
    if not (ADJ_MIN * (1 - ADJ_AGREE_TOL) <= f <= ADJ_MAX * (1 + ADJ_AGREE_TOL)):
        return None                          # implausible: a damaged card, not a split
    return f
