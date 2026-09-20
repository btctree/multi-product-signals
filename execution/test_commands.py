#!/usr/bin/env python3
"""Golden tests for the phone -> VM command channel (ib_commands.py).

Run from this directory:  python test_commands.py

This channel places MARKET SELLS and moves the earmark that sizes positions, and
its only authentication is "the GitHub issue was opened by the repo owner", on a
PUBLIC repo. So the grammar and the author check are security surfaces, not
conveniences. What is locked down here:

  * CMD_RE must FULLMATCH. A prefix match turns "SELL: NVDA when it hits 200"
    into an immediate full-position sell, because the trailing words leave qty
    unparsed and qty=None means "sell everything".
  * EARMARK requires an amount. An optional one would make a stray "EARMARK"
    title mean zero - silently un-marking money still waiting to be withdrawn,
    and inflating the pool the bot sizes against.
  * Only the repo owner's issues are executed, and only recent ones.
  * ib_commands writes the SAME marker file ib_bot and daily_signal read.
"""
import io
import json
import os
import re
from datetime import datetime, timedelta, timezone

os.environ.setdefault("IB_BACKEND", "web")
# Every /root default (the DONE file, the earmark, the orders ledger...) at a
# temp path before import - review 2026-09-17, test isolation. See testenv.py.
import testenv                                      # noqa: E402
testenv.isolate("mps-commands-")
import ib_commands                                  # noqa: E402
testenv.assert_isolated()


def kind_of(title):
    m = ib_commands.CMD_RE.fullmatch(title)
    if not m:
        return None
    return "sell" if m.group(1) else ("refresh" if m.group(4) else "earmark")


def t1_grammar_accepts_what_the_dashboard_sends():
    assert kind_of("REFRESH") == "refresh"
    assert kind_of("SELL: NVDA") == "sell"
    assert kind_of("SELL: NVDA 5") == "sell"
    assert kind_of("EARMARK: 23746") == "earmark"
    assert kind_of("EARMARK 23746") == "earmark"
    assert kind_of("EARMARK: 0") == "earmark"
    assert kind_of("EARMARK: 23746.5") == "earmark"       # the page may send a decimal
    print("t1 grammar accepts the dashboard's commands OK")


def t2_grammar_rejects_everything_else():
    for bad in ("EARMARK",                    # no amount -> would mean "clear"
                "EARMARK: ",
                "EARMARK: abc",
                "EARMARK: 23,746",            # a comma is what toLocaleString would send
                "EARMARK: -5",
                "EARMARK: 100 extra",
                "SELL: NVDA when it hits 200",
                "sell my house",
                "Bug report: dashboard is slow",
                ""):
        assert kind_of(bad) is None, bad
    print("t2 grammar rejects malformed and hostile titles OK")


def _issue(num, title, login="btctree", assoc="OWNER", age_h=1):
    when = datetime.now(timezone.utc) - timedelta(hours=age_h)
    return {"number": num, "title": title, "user": {"login": login},
            "author_association": assoc,
            "created_at": when.strftime("%Y-%m-%dT%H:%M:%SZ")}


def _with_issues(issues, fn):
    class R:
        def __enter__(self_):
            return self_

        def __exit__(self_, *a):
            return False

        def read(self_):
            return json.dumps(issues).encode()

    real = ib_commands.urllib.request.urlopen
    ib_commands.urllib.request.urlopen = lambda *a, **k: R()
    real_load = json.load
    json.load = lambda fh: json.loads(fh.read().decode())
    try:
        return fn()
    finally:
        ib_commands.urllib.request.urlopen = real
        json.load = real_load


def t3_owner_only_and_recent_only():
    issues = [_issue(1, "EARMARK: 23746"),
              _issue(2, "SELL: NVDA 5", login="stranger", assoc="NONE"),
              _issue(3, "EARMARK: 999", login="btctree", assoc="CONTRIBUTOR"),
              _issue(4, "REFRESH", age_h=ib_commands.MAX_AGE_H + 5)]
    got = _with_issues(issues, ib_commands.fetch_commands)
    assert [c["id"] for c in got] == [1], got        # 2 not owner, 3 not OWNER assoc, 4 stale
    assert got[0]["kind"] == "earmark" and got[0]["amount"] == 23746.0, got
    print("t3 only the owner's recent commands execute OK")


def t4_parsed_fields():
    got = _with_issues([_issue(7, "SELL: DXCM 20"), _issue(8, "EARMARK: 0"),
                        _issue(9, "REFRESH")], ib_commands.fetch_commands)
    by = {c["id"]: c for c in got}
    assert by[7]["kind"] == "sell" and by[7]["symbol"] == "DXCM" and by[7]["qty"] == 20.0
    assert by[8]["kind"] == "earmark" and by[8]["amount"] == 0.0
    assert by[9]["kind"] == "refresh" and by[9]["amount"] is None
    print("t4 commands parse into the right fields OK")


def t5_one_shared_earmark_module():
    """All four programs must go through execution/earmark.py.

    They used to carry four copies of the same read-and-cap, and the copies
    drifted: the cap climbed with the balance in some and not others, so the
    number the bot sized against, the number the phone showed and the number the
    digest reported could all differ.
    """
    import earmark
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("ib_bot.py", "publish_web.py", "daily_signal.py", "ib_commands.py"):
        src = io.open(os.path.join(here, name), encoding="utf-8").read()
        assert "earmark" in src, "%s does not use the shared earmark module" % name
        # ...and nobody re-implements the path or the cap locally
        assert '"/root/excluded_cash"' not in src, "%s still hard-codes the path" % name
        assert "earmark_pocket.json" not in src and "earmark_anchor" not in src, \
            "%s hard-codes a pocket file" % name
    # The production path, checked in the source: the module itself now points
    # at a temp dir (testenv), so its live value can no longer show /root.
    em_src = io.open(os.path.join(here, "earmark.py"), encoding="utf-8").read()
    assert 'DIR = Path(os.environ.get("MPS_EARMARK_DIR", "/root"))' in em_src
    assert 'MARKER_FILE = DIR / "excluded_cash"' in em_src
    assert earmark.MARKER_FILE == earmark.DIR / "excluded_cash"
    # ADDED 2026-09-17 with the stamped pocket: the processes that do not sweep
    # executions must all take the bot's HKD from the SAME pocket reader, or the
    # number the bot sizes against and the number the phone and digest show
    # drift apart again - the exact failure this module was created to end.
    # Calling earmark.effective() directly would silently keep the old
    # limitation in that one reader.
    for name in ("publish_web.py", "daily_signal.py"):
        src = io.open(os.path.join(here, name), encoding="utf-8").read()
        assert "earmark.publisher_exclusion(" in src, name
        assert "earmark.effective(" not in src, "%s bypasses the pocket" % name
        assert not re.search(r"^\s*(import|from)\s+ib_orders\b", src, re.M), \
            "%s must stay read-only" % name
    bot = io.open(os.path.join(here, "ib_bot.py"), encoding="utf-8").read()
    assert "earmark.publisher_exclusion(held)" in bot, "net_liq outside a run"
    print("t5 one shared earmark module OK")


def t6_dashboard_sends_a_title_the_vm_accepts():
    # The page builds the title as 'EARMARK: ' + Number, deliberately NOT
    # toLocaleString - a comma would be silently ignored by the VM.
    page = io.open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "docs", "index.html"), encoding="utf-8").read()
    # space-insensitive compare, so the needle is written without them too
    assert "title:'EARMARK:'+amt" in page.replace(" ", ""), "dashboard title format changed"
    assert "toLocaleString()}" not in page.split("title:'EARMARK:")[1][:40]
    assert kind_of("EARMARK: " + str(23746)) == "earmark"
    assert kind_of("EARMARK: " + str(0)) == "earmark"
    assert kind_of("EARMARK: " + str(23746.5)) == "earmark"
    print("t6 dashboard title format matches the VM grammar OK")


def t7_commands_apply_oldest_first():
    """A correction must not be overwritten by the typo it corrects.

    GitHub is queried newest-first (sort=created&direction=desc) and EARMARK is
    last-write-wins on a single file. Processing in arrival order meant: tap
    200000, notice, tap 20000 - and the file ended at 200000 while the log
    printed the correct value first, reading as if the right one had won.
    """
    issues = [_issue(12, "EARMARK: 20000"), _issue(11, "EARMARK: 200000")]
    got = _with_issues(issues, ib_commands.fetch_commands)
    assert [c["id"] for c in got] == [12, 11], "API order is newest-first"
    todo = sorted(got, key=lambda c: c["id"])          # what main() now does
    assert [c["id"] for c in todo] == [11, 12]
    assert todo[-1]["amount"] == 20000.0, "the correction must land last"
    src = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "ib_commands.py"), encoding="utf-8").read()
    assert 'todo.sort(key=lambda c: c["id"])' in src, "main() must sort oldest-first"
    print("t7 commands apply oldest-first OK")


def t10_a_pending_sell_survives_the_operators_own_refreshes():
    """The operator fills the issue list himself.

    Every tap of Refresh and every pull-to-refresh opens an owner-authored
    REFRESH issue, and the live repo's last 100 owner issues are ALL "REFRESH" -
    38 of them inside a single rolling 48 h window. creator= keeps strangers
    out of the window; it does nothing about this.

    A SELL held pending - the book unreadable, or positions not flushable - has
    to survive MAX_AGE_H in that window. Pushed off the first page it is never
    fetched: never executed, never aged out, never added to DONE, and never
    alerted, because the alerts can only fire for a command that is IN the
    fetch. The poll meanwhile looks perfectly healthy.

    So the fetch has to run until the page is OLDER than MAX_AGE_H.
    """
    pages = {
        1: [_issue(1000 + n, "REFRESH", age_h=1) for n in range(100)],
        2: [_issue(900 + n, "REFRESH", age_h=30) for n in range(99)]
             + [_issue(618, "SELL: NVDA 5", age_h=40)],
        3: [],
    }
    seen = []

    class R:
        def __init__(self, body):
            self.body = body

        def read(self):
            return json.dumps(self.body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, *a, **k):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        n = 1
        if "&page=" in url:
            n = int(url.split("&page=")[1].split("&")[0])
        seen.append(n)
        return R(pages.get(n, []))

    real_open = ib_commands.urllib.request.urlopen
    real_load = json.load
    ib_commands.urllib.request.urlopen = fake_urlopen
    json.load = lambda fh: json.loads(fh.read().decode())
    try:
        got = ib_commands.fetch_commands()
    finally:
        ib_commands.urllib.request.urlopen = real_open
        json.load = real_load

    assert seen[:2] == [1, 2], "the fetch did not page past the first 100 (%r)" % seen
    ids = [c["id"] for c in got]
    assert 618 in ids, \
        "the pending SELL was lost behind the operator's own REFRESH issues"
    sells = [c for c in got if c["kind"] == "sell"]
    assert len(sells) == 1 and sells[0]["symbol"] == "NVDA" and sells[0]["qty"] == 5.0
    # and it stops: page 3 is empty, so it must not keep asking
    assert max(seen) <= ib_commands.MAX_PAGES, seen
    print("t10 a pending SELL survives 100+ of the operator's own REFRESH issues OK")


def t11_the_update_stamp_matches_the_page():
    """The dashboard tells the owner when the page itself is out of date, by
    comparing the build stamp in the document it is RUNNING against the one the
    server serves. That check is only as good as the stamp.

    A stamp a human has to remember to bump is a stamp that goes stale, and a
    stale stamp makes the banner quietly stop working - the exact failure it
    exists to prevent, and one nobody would notice. So it is derived from the
    file's own bytes and checked here: index.html cannot change without it.
    """
    import stamp_app
    text = io.open(stamp_app.PAGE, encoding="utf-8", newline="").read()
    have, want = stamp_app.current(text), stamp_app.compute(text)
    assert have is not None, "the app-build meta is gone - no update check"
    assert have == want, (
        "docs/index.html changed without restamping (page %s, meta %s). "
        "Run: python execution/stamp_app.py --write" % (want, have))
    # and the page must actually READ it, both sides of the comparison
    page = text.replace(" ", "")
    assert 'meta[name="app-build"]' in page, "the page no longer reads its own stamp"
    assert "cache:'no-store'" in page, "the version check could be served from cache"
    print("t11 the update stamp matches the page it stamps OK (%s)" % want)


def t9_a_stranger_cannot_crowd_out_the_owners_sell():
    """The repo is PUBLIC, so anyone can open issues - and this poller reads
    only the 30 NEWEST.

    Filtering by author AFTER the fetch was not enough: thirty issues from a
    stranger push the owner's SELL out of the page entirely, so the poller
    never sees it, logs nothing unusual, and silently does nothing until the
    command ages out 48 hours later. No forged identity needed, and the thing
    denied is the emergency exit.

    So the filter has to be applied by GITHUB, in the query. The author check
    stays as well - correctness must never depend on a query parameter.
    """
    assert "&creator=" + ib_commands.OWNER in ib_commands.ISSUES_URL, \
        "the issue query no longer restricts to the owner - a stranger can " \
        "crowd the owner's commands out of the 30 newest"
    # built from OWNER, so the two cannot drift apart
    assert ib_commands.ISSUES_URL.count(ib_commands.OWNER) >= 2

    # and the belt-and-braces check still rejects a forged author if the
    # parameter were ever dropped or ignored by the API
    issues = [_issue(1, "SELL: NVDA", login="stranger", assoc="NONE"),
              _issue(2, "SELL: NVDA", login=ib_commands.OWNER, assoc="CONTRIBUTOR"),
              _issue(3, "SELL: NVDA", login=ib_commands.OWNER, assoc="OWNER")]
    got = _with_issues(issues, ib_commands.fetch_commands)
    assert [c["id"] for c in got] == [3], got
    print("t9 only the owner's issues are fetched, and only the owner's are run OK")


def t8_raw_marker_is_published_for_display_only():
    """Review finding 2026-09-16: a marker above the HKD held was invisible.

    Only the capped excluded_cash was published, so a stale marker read as a
    normal "Earmarked = HKD balance". Both publishers now emit the raw number as
    earmark_marker - and never AS excluded_cash, which once made the caption
    flip between publishers. The dashboard warns when it exceeds the HKD held.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("ib_bot.py", "publish_web.py"):
        src = io.open(os.path.join(here, name), encoding="utf-8").read()
        assert '"earmark_marker": round(earmark.marker())' in src, name
        assert '"excluded_cash": round(earmark.marker())' not in src, name
        # ADDED 2026-09-17: both publishers emit the bot's own HKD beside the
        # marker, as a number or null - never omitted by a current publisher.
        assert '"earmark_bot_hkd": (None if' in src, name
    page = io.open(os.path.join(os.path.dirname(here), "docs", "index.html"),
                   encoding="utf-8").read()
    assert "BOT.earmark_marker" in page, "dashboard does not read the raw marker"
    assert "earMarker>exCash+1" in page, "dashboard does not warn on a stale marker"
    # REWRITTEN needles, deliberately. With a pocket the stale-marker warning
    # says how much of the HKD is the operator's and how much the bot's, and no
    # longer claims the bot's funding is excluded too. A missing or null field
    # (older publisher, unconfirmed stamping) must keep the old text and the old
    # not-earmarked threshold, so both branches are pinned.
    assert "typeof BOT.earmark_bot_hkd==='number'" in page
    assert "Bot's own HKD" in page
    assert "of the HKD here is yours" in page and "the bot holds" in page
    assert "(hkdCash-(botHkd||0)-exCash)>=1000" in page
    assert "any HKD the bot buys" in page, "the no-pocket text must remain for null"
    newer = page.split("of the HKD here is yours")[1].split("`:`")[0]
    assert "excluded too" not in newer, "the pocket branch still claims bot funding is excluded"
    print("t8 raw marker published for display only OK")


if __name__ == "__main__":
    t1_grammar_accepts_what_the_dashboard_sends()
    t2_grammar_rejects_everything_else()
    t3_owner_only_and_recent_only()
    t4_parsed_fields()
    t5_one_shared_earmark_module()
    t6_dashboard_sends_a_title_the_vm_accepts()
    t7_commands_apply_oldest_first()
    t8_raw_marker_is_published_for_display_only()
    t9_a_stranger_cannot_crowd_out_the_owners_sell()
    t10_a_pending_sell_survives_the_operators_own_refreshes()
    t11_the_update_stamp_matches_the_page()
    print("ALL COMMAND TESTS PASS")
