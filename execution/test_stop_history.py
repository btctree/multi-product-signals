#!/usr/bin/env python3
"""Golden tests for the published cut-loss history (execution/stop_history.py).

Run from this directory:  python test_stop_history.py

The dashboard draws a holding's cut-loss as it actually moved from this file, so
what it must never do is invent a step: a value repeated hour after hour is not
a change, two changes on one day are one row (the day's last value, which is
what a daily chart can show), and a torn write must not cost a publish or leave
a file the page cannot read.
"""
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import stop_history                                     # noqa: E402

TMP = tempfile.mkdtemp(prefix="mps-stophist-")


def pos(sym, stop):
    return {"symbol": sym, "qty": 10, "stop": stop}


def t1_only_changes_are_recorded():
    doc = {"stops": {}}
    assert stop_history.record(doc, [pos("FFIV", 352.9)], day="2026-07-23") == 1
    for d in ("2026-07-24", "2026-07-25"):               # hourly republishes
        assert stop_history.record(doc, [pos("FFIV", 352.9)], day=d) == 0
    assert stop_history.record(doc, [pos("FFIV", 361.2)], day="2026-07-28") == 1
    assert doc["stops"]["FFIV"] == [["2026-07-23", 352.9], ["2026-07-28", 361.2]]
    print("t1 a stop that did not move adds no row OK")


def t2_two_moves_in_one_day_collapse_to_the_last():
    doc = {"stops": {}}
    stop_history.record(doc, [pos("MS", 185.86)], day="2026-07-20")
    # the 23:35 run and the 09:00 run both ratchet on the same UTC day
    stop_history.record(doc, [pos("MS", 190.10)], day="2026-07-21")
    stop_history.record(doc, [pos("MS", 191.44)], day="2026-07-21")
    assert doc["stops"]["MS"] == [["2026-07-20", 185.86], ["2026-07-21", 191.44]], \
        doc["stops"]["MS"]
    print("t2 two moves on one day leave one row, the day's last OK")


def t3_a_missing_or_odd_stop_is_skipped_not_guessed():
    doc = {"stops": {}}
    n = stop_history.record(doc, [
        {"symbol": "AAA", "stop": None},                 # no stop yet
        {"symbol": "", "stop": 10},                      # no symbol
        {"symbol": "BBB", "stop": "not a number"},
        {"symbol": "CCC", "stop": "44.5"},               # a numeric string is fine
    ], day="2026-08-01")
    assert n == 1 and doc["stops"] == {"CCC": [["2026-08-01", 44.5]]}, doc["stops"]
    print("t3 rows without a usable stop are skipped, never invented OK")


def t4_history_survives_a_sale_and_a_re_buy():
    """The page cuts the line at the current lot's entry date, so the old rows
    are harmless - but losing them would lose the previous lot's exit too."""
    doc = {"stops": {}}
    stop_history.record(doc, [pos("URI", 900.0)], day="2026-07-30")
    stop_history.record(doc, [], day="2026-08-01")       # sold: not in positions
    stop_history.record(doc, [pos("URI", 1000.0)], day="2026-08-26")   # re-bought
    assert doc["stops"]["URI"] == [["2026-07-30", 900.0], ["2026-08-26", 1000.0]]
    print("t4 a sale does not erase what the previous lot recorded OK")


def t5_written_atomically_and_readable():
    p = os.path.join(TMP, "stop_history.json")
    assert stop_history.update(p, [pos("DELL", 385.04)], day="2026-08-20") is True
    assert not os.path.exists(p + ".tmp"), "a temp file was left behind"
    d = json.loads(io.open(p, encoding="utf-8-sig").read())
    assert d["stops"]["DELL"] == [["2026-08-20", 385.04]]
    assert d["updated"] and d["updated"].endswith("UTC")
    # a second, unchanged publish rewrites nothing
    assert stop_history.update(p, [pos("DELL", 385.04)], day="2026-08-21") is False
    print("t5 written through a temp file and read back OK")


def t6_the_file_is_created_even_with_nothing_to_record():
    """Both publishers name it in `git add`, and git fails the whole add on a
    pathspec matching no file - which stopped a trading run's publish."""
    p = os.path.join(TMP, "fresh.json")
    assert stop_history.update(p, [], day="2026-08-20") is True
    assert os.path.exists(p)
    assert json.loads(io.open(p, encoding="utf-8-sig").read())["stops"] == {}
    print("t6 a first run with no positions still creates the file OK")


def t7_a_broken_file_never_costs_a_publish():
    p = os.path.join(TMP, "torn.json")
    io.open(p, "w", encoding="utf-8").write('{"stops": {"X": [["2026-0')
    assert stop_history.load(p)["stops"] == {}, "a torn file must read as empty"
    logged = []
    assert stop_history.update(p, [pos("X", 5.0)], logged.append,
                               day="2026-09-01") is True
    assert json.loads(io.open(p, encoding="utf-8-sig").read())["stops"]["X"]
    # an unwritable path is logged, not raised
    bad = os.path.join(TMP, "nope", "deep", "x.json")
    assert stop_history.update(bad, [pos("X", 5.0)], logged.append) is False
    assert any("NOT updated" in m for m in logged), logged
    print("t7 a torn or unwritable file is logged, never raised OK")


def t8_the_published_file_matches_these_rules():
    """The real file, if this checkout has one: sorted, deduped, parseable."""
    p = os.path.join(os.path.dirname(HERE), "data", "stop_history.json")
    if not os.path.exists(p):
        print("t8 no local stop_history.json - skipped")
        return
    d = json.loads(io.open(p, encoding="utf-8-sig").read())
    for sym, rows in d["stops"].items():
        ds = [r[0] for r in rows]
        assert ds == sorted(ds), sym + ": rows out of order"
        assert len(set(ds)) == len(ds), sym + ": two rows on one day"
        assert all(isinstance(r[1], (int, float)) for r in rows), sym + ": non-numeric stop"
        assert all(rows[i][1] != rows[i - 1][1] for i in range(1, len(rows))), \
            sym + ": a repeated value was stored as a change"
    print("t8 the published file is sorted, deduped and numeric OK (%d symbols)"
          % len(d["stops"]))


if __name__ == "__main__":
    t1_only_changes_are_recorded()
    t2_two_moves_in_one_day_collapse_to_the_last()
    t3_a_missing_or_odd_stop_is_skipped_not_guessed()
    t4_history_survives_a_sale_and_a_re_buy()
    t5_written_atomically_and_readable()
    t6_the_file_is_created_even_with_nothing_to_record()
    t7_a_broken_file_never_costs_a_publish()
    t8_the_published_file_matches_these_rules()
    print("ALL STOP-HISTORY TESTS PASS")
