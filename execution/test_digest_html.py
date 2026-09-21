#!/usr/bin/env python3
"""Golden tests: the Telegram digest is valid HTML, and a parse error still delivers.

Run from this directory:  python test_digest_html.py

Review 2026-09-17: send_message posts with parse_mode=HTML, but build_report
added problem lines raw. A Yahoo connect timeout is a URLError whose text is
"<urlopen error timed out>"; Telegram answered 400 "can't parse entities:
Unsupported start tag", send_message raised, and main() returned 1 - no digest
and no P&L baseline, on exactly the nights the feeds were failing. The regime
break reason "(50.00 < SMA200 60.00)" broke it the same way. What is locked down:

  * every dynamic string in the digest is escaped; only <b>, <i> and <code>
    written by build_report itself are markup;
  * a Telegram entity-parse refusal resends the SAME text once as plain text
    (own tags stripped, entities unescaped); any other refusal does not resend;
  * send_message keeps its signature and return value for telegram_poll.

EXTENDED 2026-09-17 (review: "The /update digest still replays exits and
entries without market_decidable"). A held or candidate symbol whose market is
still in session or settling is listed under "decided after the close", never
as a SELL or BUY line; the 23:40 UTC digest is line-for-line what it was. t1
and t3 hold a US name, so they now pin the digest's clock to 23:40 UTC - on the
wall clock they would fail in US hours for the wrong reason.

Final review 2026-09-17:
  * t5: a text over ~3900 UTF-16 code units is sent as sequential messages cut
    at newlines, each valid HTML and under Telegram's 4096 cap, each with its
    own plain-text resend; the LAST message_id comes back.
  * t6: that section listed a ~120-character line per symbol, and an /update
    in US hours with the live book and nine or so open US buy signals went past
    Telegram's 4096 characters ("message is too long", not a parse error), so
    the reply was lost. It is grouped per market now: the live-sized book with
    15 open US candidates at Wed 15:00 UTC stays under the cap.
  * t7: the digest mirrors the bot's build-stamp gate as a label. A held card,
    or a candidate, read from a build that started before its market's last
    close + 90 min gets no SELL/BUY line and is listed as decided after the
    close; with no usable generated_at the clock alone decides, as before.

Board review 2026-09-21:
  * t8: the digest asked for a dotted holding's card as DBK.DE.json, but the
    dashboard writes DBK_DE.json (dots become underscores). Every .T and .DE
    holding 404'd and was priced from Yahoo, with a "used Yahoo" problem line
    every night. A dotted holding is now priced from its own card, and the
    digest's copy of the file-name rule is pinned to the dashboard's and the
    bot's. t4 and t6 mocked the wrong URL and now mock the real one. (The
    dividend replay of the stop has its own suite, test_digest_divadj.py.)

Nothing here touches /root or the network: every path is a temp file, every
fetch and every Telegram call is a stub.
"""
import ast
import io
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

# Every /root default at a temp path before import (review 2026-09-17, test
# isolation: ib_web's OAuth dir was still /root here). The same names the
# suite always used: repo, daily_signal_prev.json, earmark, outbox...
import testenv                                     # noqa: E402
_TMP = testenv.isolate("mps-digest-")
REPO = _TMP / "repo"
(REPO / "execution").mkdir(parents=True)
(REPO / "data").mkdir()
assert os.environ["MPS_REPO"] == str(REPO)
(_TMP / "earmark").mkdir(exist_ok=True)
os.environ.pop("EXCLUDED_CASH", None)

import alerts                                      # noqa: E402
import daily_signal as ds                          # noqa: E402
import ib_web                                      # noqa: E402
import market_clock                                # noqa: E402
import telegram_poll                               # noqa: E402
testenv.assert_isolated()

# Wed 2026-09-16: the scheduled digest's time, when every market is decidable,
# and a mid-morning /update, when Europe is in session and New York is not open.
AT_2340 = datetime(2026, 9, 16, 23, 40, tzinfo=timezone.utc)
AT_1015 = datetime(2026, 9, 16, 10, 15, tzinfo=timezone.utc)

assert ds.PREV.startswith(str(_TMP)) and ds.STATE.startswith(str(REPO)), (ds.PREV, ds.STATE)
alerts.DIR = _TMP / "outbox"
telegram_poll.OFFSET_FILE = str(_TMP / "telegram_offset.json")
telegram_poll.LOCK = str(_TMP / "tg_poll.lock")
ds.log = lambda *a, **k: None

TIMEOUT = urllib.error.URLError(socket.timeout("timed out"))
assert str(TIMEOUT) == "<urlopen error timed out>", str(TIMEOUT)


def write(path, obj):
    Path(path).write_text(json.dumps(obj), encoding="utf-8")


write(ds.STATE, {"map": {"DXCM": "DXCM"}, "_peak_netliq": 1,
                 "pos": {"DXCM": {"entry": 55, "hw": 58, "stop": 40,
                                  "entry_date": "2026-09-01"}}})
write(ds.BOT_STATE, {"updated": "2026-09-17 23:20 UTC <stale> & old", "netliq": 20000,
                     "cash": {"USD": 1000},
                     "positions": [{"symbol": "DXCM", "qty": 20, "avg_cost": 55.0,
                                    "ccy": "USD"}]})
write(ds.NETLIQ_HIST, {"series": [{"d": "2026-07-01", "nl": 15000}], "flows": []})
Path(ds.ENVF).write_text("TELEGRAM_TOKEN=tok\nTELEGRAM_CHAT_ID=42\n", encoding="utf-8")


def fake_get_json(url, timeout=30):
    if url == ds.PRODUCTS + "DXCM.json":
        return {"card": {"price": 50.0, "sma200": 60.0, "atr": 2.0}}   # regime break
    if "interval=1m" in url or "HKD=X" in url:
        raise TIMEOUT                              # live quote and USDHKD time out
    if "=X" in url:
        return {"chart": {"result": [{"meta": {"regularMarketPrice": 150.0}}]}}
    if url == ds.BASE + "data.json":
        return {"actions": [{"symbol": "A&B", "price": 10.0, "score": "<9>",
                             "market": "US"}]}
    raise AssertionError("unexpected fetch " + url)


def no_ib():
    raise TIMEOUT


class Patch:
    def __init__(self, mod, **kw):
        self.mod, self.kw, self.old = mod, kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(self.mod, k)
            setattr(self.mod, k, v)
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(self.mod, k, v)


_ENTITY = re.compile(r"&(?:lt|gt|amp|quot|#\d+|#x[0-9a-fA-F]+);")


def telegram_html_ok(text):
    """Telegram's parse_mode=HTML rules, as far as this digest can break them:
    tags must be supported and balanced, and every other <, > and & must be an
    entity. Returns the problem, or None."""
    stack = []
    for part in re.split(r"(</?(?:b|i|code)>)", text):
        m = re.fullmatch(r"<(/?)(b|i|code)>", part)
        if m:
            if m.group(1):
                if not stack or stack.pop() != m.group(2):
                    return "unbalanced </%s>" % m.group(2)
            else:
                stack.append(m.group(2))
            continue
        if "<" in part or ">" in part:
            return "raw angle bracket in %r" % part[:60]
        if "&" in _ENTITY.sub("", part):
            return "raw & in %r" % part[:60]
    return "unclosed %s" % stack if stack else None


def t1_problem_lines_and_names_are_escaped():
    with Patch(ds, get_json=fake_get_json, _now_utc=lambda: AT_2340), \
            Patch(ib_web, snapshot=no_ib):
        msg, snap = ds.build_report()
    assert telegram_html_ok(msg) is None, telegram_html_ok(msg)
    for want in ("live DXCM: &lt;urlopen error timed out&gt;",
                 "fx USDHKD: &lt;urlopen error timed out&gt;",
                 "IB read failed (&lt;urlopen error timed out&gt;)",
                 "regime break (50.00 &lt; SMA200 60.00)",
                 "<code>BUY A&amp;B ", "score &lt;9&gt;",
                 "<code>2026-09-17 23:20 UTC &lt;stale&gt; &amp; old</code>",
                 "<b>⚠️ Problems</b>", "<code>SELL DXCM 20 @ MKT</code>"):
        assert want in msg, (want, msg)
    assert "<urlopen" not in msg and "< SMA200" not in msg
    # the old, unescaped rendering of the same lines is what Telegram refused
    assert telegram_html_ok("• live DXCM: <urlopen error timed out>") is not None
    assert snap["est_netliq"] > 0
    print("t1 problem lines, reasons, symbols and names are escaped OK")


class Answer:
    def __init__(self, obj):
        self.body = json.dumps(obj).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a):
        return self.body


def refusal(req, description, code=400):
    body = json.dumps({"ok": False, "error_code": code, "description": description})
    return urllib.error.HTTPError(req.full_url, code, "Bad Request", {},
                                  io.BytesIO(body.encode("utf-8")))


class Telegram:
    """sendMessage stub. script: one entry per call - "ok", or a description
    to refuse with. Records each call's form fields."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []

    def __call__(self, req, timeout=30):
        assert "/sendMessage" in req.full_url, req.full_url
        self.calls.append({k: v[0] for k, v in urllib.parse.parse_qs(
            req.data.decode("utf-8"), keep_blank_values=True).items()})
        step = self.script.pop(0)
        if step == "ok":
            return Answer({"ok": True, "result": {"message_id": 70 + len(self.calls)}})
        if step == "html":                          # behave like Telegram's parser
            f = self.calls[-1]
            if f.get("parse_mode") == "HTML" and telegram_html_ok(f["text"]):
                raise refusal(req, "Bad Request: can't parse entities: Unsupported "
                                   "start tag \"urlopen\" at byte offset 12")
            return Answer({"ok": True, "result": {"message_id": 99}})
        if step == "strict":                        # Telegram's parser AND its length cap
            f = self.calls[-1]
            html_mode = f.get("parse_mode") == "HTML"
            if html_mode and telegram_html_ok(f["text"]):
                raise refusal(req, PARSE_ERR)
            if units(ds.plain_text(f["text"]) if html_mode else f["text"]) > 4096:
                raise refusal(req, "Bad Request: message is too long")
            return Answer({"ok": True, "result": {"message_id": 70 + len(self.calls)}})
        if step == "not-json":
            raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", {},
                                         io.BytesIO(b"<html>bad gateway</html>"))
        raise refusal(req, step)


def units(s):
    """Telegram's length: UTF-16 code units."""
    return len(s.encode("utf-16-le")) // 2


PARSE_ERR = "Bad Request: can't parse entities: Unsupported start tag \"urlopen\" at byte offset 42"


def t2_entity_parse_error_resends_once_as_plain_text():
    text = ("<b>⚠️ Problems</b>\n• live DXCM: <urlopen error timed out>\n"
            "• <code>A&amp;B</code> &lt;9&gt; <i>note</i><a href=\"https://x\"></a>")
    tg = Telegram(PARSE_ERR, "ok")
    with Patch(urllib.request, urlopen=tg):
        assert ds.send_message("tok", "42", text) == 72      # the resend's id
    assert len(tg.calls) == 2, tg.calls
    assert tg.calls[0]["parse_mode"] == "HTML" and tg.calls[0]["text"] == text
    assert "parse_mode" not in tg.calls[1], tg.calls[1]
    assert tg.calls[1]["text"] == ("⚠️ Problems\n• live DXCM: <urlopen error timed out>\n"
                                   "• A&B <9> note"), tg.calls[1]["text"]
    assert tg.calls[1]["chat_id"] == "42"
    # a refusal that is not a parse error is not resent
    tg = Telegram("Bad Request: chat not found")
    with Patch(urllib.request, urlopen=tg):
        try:
            ds.send_message("tok", "42", text)
            raise AssertionError("must raise")
        except RuntimeError as e:
            assert "chat not found" in str(e), e
    assert len(tg.calls) == 1, tg.calls
    # a resend that fails too raises after exactly two calls - never a loop
    tg = Telegram(PARSE_ERR, PARSE_ERR)
    with Patch(urllib.request, urlopen=tg):
        try:
            ds.send_message("tok", "42", text)
            raise AssertionError("must raise")
        except RuntimeError as e:
            assert "parse entities" in str(e), e
    assert len(tg.calls) == 2, tg.calls
    # an error page that is not Telegram's JSON still raises, unsent twice
    tg = Telegram("not-json")
    with Patch(urllib.request, urlopen=tg):
        try:
            ds.send_message("tok", "42", "hello")
            raise AssertionError("must raise")
        except urllib.error.HTTPError:
            pass
    assert len(tg.calls) == 1
    # a clean send is one call, as before
    tg = Telegram("ok")
    with Patch(urllib.request, urlopen=tg):
        assert ds.send_message("tok", "42", "<b>fine</b>") == 71
    assert len(tg.calls) == 1 and tg.calls[0]["parse_mode"] == "HTML"
    # telegram_poll's alert drain still delivers through it
    alerts.enqueue("exit-refused-BEN-run1", "EXIT REFUSED <h4>x</h4>")
    tg = Telegram(PARSE_ERR, "ok")
    with Patch(urllib.request, urlopen=tg):
        telegram_poll.drain_alerts("tok", "42")
    assert len(tg.calls) == 2 and alerts._queued() == [], (tg.calls, alerts._queued())
    print("t2 a Telegram parse refusal resends exactly once as plain text OK")


def t3_digest_with_a_network_error_is_delivered():
    prev = Path(ds.PREV)
    if prev.exists():
        prev.unlink()
    tg = Telegram("html")
    with Patch(ds, get_json=fake_get_json, _now_utc=lambda: AT_2340), \
            Patch(ib_web, snapshot=no_ib), Patch(urllib.request, urlopen=tg):
        assert ds.main() == 0
    assert len(tg.calls) == 1, "the escaped digest needed no resend"
    assert "&lt;urlopen error timed out&gt;" in tg.calls[0]["text"]
    assert prev.exists(), "the P&L baseline must be saved"
    print("t3 digest with a feed timeout is sent and the baseline saved OK")


def t4_digest_lists_markets_in_session_as_decided_after_the_close():
    # Held: DXCM (New York) and DBK.DE (Xetra), both below their SMA200 on the
    # card. Candidates: SAP.DE and MSFT. At 10:15 UTC on a Wednesday Xetra is
    # trading and New York has not opened.
    state_p, bot_p = _TMP / "t4_state.json", _TMP / "t4_bot_state.json"
    write(state_p, {"map": {"DXCM": "DXCM", "DBK": "DBK.DE"}, "_peak_netliq": 1,
                    "pos": {"DXCM": {"entry": 55, "hw": 58, "stop": 40, "entry_date": "2026-09-01"},
                            "DBK.DE": {"entry": 28, "hw": 31, "stop": 20, "entry_date": "2026-09-01"}}})
    write(bot_p, {"updated": "2026-09-16 09:25 UTC", "netliq": 250000, "cash": {"USD": 30000},
                  "positions": [{"symbol": "DXCM", "qty": 20, "avg_cost": 55.0, "ccy": "USD"},
                                {"symbol": "DBK.DE", "qty": 40, "avg_cost": 28.0, "ccy": "EUR"}]})
    fx = {"HKD=X": 7.8, "EURUSD=X": 1.1, "JPY=X": 150.0, "GBPUSD=X": 1.3}

    def get_json(url, timeout=30):
        if url == ds.PRODUCTS + "DXCM.json":
            return {"card": {"price": 50.0, "sma200": 60.0, "atr": 2.0}}
        if url == ds.PRODUCTS + "DBK_DE.json":         # the dashboard's file name
            return {"card": {"price": 30.0, "sma200": 32.0, "atr": 1.0}}
        if "interval=1m" in url:
            raise TIMEOUT
        for pair, v in fx.items():
            if "/chart/" + pair + "?" in url:
                return {"chart": {"result": [{"meta": {"regularMarketPrice": v}}]}}
        if url == ds.BASE + "data.json":
            return {"actions": [
                {"symbol": "SAP.DE", "price": 200.0, "score": 9, "market": "EU"},
                {"symbol": "MSFT", "price": 100.0, "score": 8, "market": "US"}]}
        raise AssertionError("unexpected fetch " + url)

    def report(now, gate=True):
        extra = {} if gate else {"market_decidable": lambda ysym, t: (True, "stub")}
        with Patch(ds, get_json=get_json, _now_utc=lambda: now, STATE=str(state_p),
                   BOT_STATE=str(bot_p)), Patch(ib_web, snapshot=no_ib), \
                Patch(market_clock, **extra):
            return ds.build_report(on_demand=True)[0]

    msg = report(AT_1015)
    assert telegram_html_ok(msg) is None, telegram_html_ok(msg)
    assert "<code>SELL DXCM 20 @ MKT</code>" in msg, msg          # New York: decided
    assert "<code>BUY MSFT " in msg, msg
    assert "SELL DBK.DE" not in msg and "BUY SAP.DE" not in msg, msg
    assert "<b>⏳ DECIDED AFTER THE CLOSE</b>" in msg, msg
    # one reason line for the market, then its names (see t6 for why grouped)
    assert ("• Europe/Berlin session 09:00-17:30 is still open or settling "
            "(local Wed 12:15; its bar is final from 19:00)\n"
            "   held: <code>DBK.DE</code>\n"
            "   buy signals: <code>SAP.DE</code>\n") in msg, msg
    assert msg.count("Europe/Berlin session") == 1, msg
    # still valued and listed among the positions, without the exit flag
    assert "POSITIONS (2)" in msg, msg
    dbk_rows = [l for l in msg.splitlines() if l.startswith("<code>DBK.DE ")]
    assert len(dbk_rows) == 1 and "⚠️" not in dbk_rows[0], dbk_rows
    # the section sits between BUY and POSITIONS
    assert msg.index("BUY</b>") < msg.index("DECIDED AFTER THE CLOSE") < msg.index("POSITIONS ("), msg

    # CONTROL at 23:40 UTC: every market decided, no section, and the digest is
    # line for line what it was before the gate existed (header aside - it
    # carries the wall clock).
    msg = report(AT_2340)
    assert "<code>SELL DBK.DE 40 @ MKT</code>" in msg and "<code>BUY SAP.DE " in msg, msg
    assert "DECIDED AFTER THE CLOSE" not in msg, msg
    assert msg.splitlines()[1:] == report(AT_2340, gate=False).splitlines()[1:]
    # ...and without the gate, 10:15 was exactly the bug: a SELL off the intraday card
    assert "<code>SELL DBK.DE 40 @ MKT</code>" in report(AT_1015, gate=False)
    print("t4 an in-session market is listed as decided after the close, 23:40 unchanged OK")


def t5_text_over_the_cap_is_sent_in_pieces_cut_at_newlines():
    lines = ["<b>\U0001F504 UPDATE — 16 Sep 15:00 UTC</b>", ""]
    for k in range(70):
        lines.append("<code>SELL SYM%02d 10 @ MKT</code>" % k)
        lines.append("   regime break (50.00 &lt; SMA200 60.00) · \U0001F534 A&amp;B %s" % ("x" * 20))
        if k % 9 == 0:
            lines.append("")
    text = "\n".join(lines)
    assert units(ds.plain_text(text)) > 4096, units(ds.plain_text(text))
    # premise: Telegram refuses it whole, and that refusal is no parse error
    tg = Telegram("strict")
    with Patch(urllib.request, urlopen=tg):
        try:
            ds._send_piece("tok", "42", text)
            raise AssertionError("the stub must refuse an over-long message")
        except RuntimeError as e:
            assert "message is too long" in str(e), e
    assert len(tg.calls) == 1

    pieces = ds.split_message(text)
    assert len(pieces) >= 2, len(pieces)
    tg = Telegram(*["strict"] * len(pieces))
    with Patch(urllib.request, urlopen=tg):
        mid = ds.send_message("tok", "42", text)
    assert len(tg.calls) == len(pieces), (len(tg.calls), len(pieces))
    assert mid == 70 + len(pieces), "the LAST piece's message_id: %s" % mid
    for c in tg.calls:
        assert c["parse_mode"] == "HTML" and c["chat_id"] == "42", c
        assert telegram_html_ok(c["text"]) is None, telegram_html_ok(c["text"])
        assert 0 < units(ds.plain_text(c["text"])) <= ds.TG_PIECE_UNITS, units(c["text"])
        assert c["text"].strip() and not c["text"].startswith("\n") \
            and not c["text"].endswith("\n"), repr(c["text"][:40])
    # nothing lost or reordered: only blank lines at a cut are dropped
    sent = [l for c in tg.calls for l in c["text"].split("\n")]
    assert [l for l in sent if l] == [l for l in lines if l], "lines lost or reordered"
    # the tags count for nothing: a long RAW text that shows short is one message
    tagged = "\n".join("<b></b><i></i><code>%d</code>" % k for k in range(250))
    assert len(tagged) > 4096 and units(ds.plain_text(tagged)) < ds.TG_PIECE_UNITS
    assert ds.split_message(tagged) == [tagged]
    # blank lines at a cut are dropped, and a piece of nothing but blank lines
    # is never sent - Telegram refuses an empty message
    edge = "\n".join(["a" * 3899, "", "", "", "b" * 3899])
    assert ds.split_message(edge) == ["a" * 3899, "b" * 3899], \
        [repr(p[:3] + "..." + p[-3:]) for p in ds.split_message(edge)]
    tg = Telegram("strict", "strict")
    with Patch(urllib.request, urlopen=tg):
        assert ds.send_message("tok", "42", edge) == 72
    assert [c["text"] for c in tg.calls] == ["a" * 3899, "b" * 3899]
    # a text at the limit is untouched; one unit over is cut
    at = "\n".join(["y" * 99] * 39)                  # 39 * 99 + 38 = 3899
    assert ds.split_message(at + "z") == [at + "z"]
    assert len(ds.split_message(at + "zz")) == 2
    # a single line over the limit (no caller writes one) is cut by characters,
    # an astral emoji never split in two
    one = ("\U0001F534" + "w" * 9) * 450            # 4,950 units, no newline
    cut = ds.split_message(one)
    assert "".join(cut) == one and all(units(p) <= ds.TG_PIECE_UNITS for p in cut), \
        [units(p) for p in cut]
    # a parse refusal of ONE piece resends that piece alone as plain text
    tg = Telegram("strict", PARSE_ERR, *["strict"] * len(pieces))
    with Patch(urllib.request, urlopen=tg):
        assert ds.send_message("tok", "42", text) == 70 + len(pieces) + 1
    assert len(tg.calls) == len(pieces) + 1, len(tg.calls)
    assert "parse_mode" not in tg.calls[2] and tg.calls[2]["text"] == ds.plain_text(pieces[1])
    assert [c["text"] for i, c in enumerate(tg.calls) if i != 2] == pieces
    # any other refusal raises there: the pieces before it went, none after
    tg = Telegram("strict", "Bad Request: chat not found")
    with Patch(urllib.request, urlopen=tg):
        try:
            ds.send_message("tok", "42", text)
            raise AssertionError("must raise")
        except RuntimeError as e:
            assert "chat not found" in str(e), e
    assert len(tg.calls) == 2, len(tg.calls)
    # telegram_poll's /update reply goes through it unchanged in signature
    got = []
    tg = Telegram(*["strict"] * len(pieces))
    with Patch(telegram_poll, get_updates=lambda tok, off: [
                {"update_id": 5, "message": {"chat": {"id": 42}, "text": "/update"}}],
               drain_alerts=lambda tok, owner: None,
               log=lambda *a: got.append(" ".join(str(x) for x in a))), \
            Patch(ds, build_report=lambda on_demand=False: (text, {})), \
            Patch(urllib.request, urlopen=tg):
        assert telegram_poll.main() == 0
    assert len(tg.calls) == len(pieces), len(tg.calls)
    assert got == ["answered /update -> message_id %d" % (70 + len(pieces))], got
    print("t5 a text over Telegram's cap goes as pieces cut at newlines, last id returned OK")


# The live book on 2026-09-16 (data/bot_state.json): 11 US names, DBK.DE, 5301.T
# and 7733.T - 14 of 15 held, NetLiq about HK$215k.
LIVE_BOOK = [("DXCM", "DXCM", 20, 86.55, "USD", 88.1), ("DELL", "DELL", 4, 437.25, "USD", 512.0),
             ("LLY", "LLY", 1, 1129.66, "USD", 1150.2), ("DBK.DE", "DBK", 46, 33.7052, "EUR", 33.63),
             ("URI", "URI", 1, 1058.05, "USD", 1040.0), ("HPE", "HPE", 33, 53.1803, "USD", 57.9),
             ("MS", "MS", 8, 211.2845, "USD", 214.3), ("XYZ", "XYZ", 22, 78.5155, "USD", 80.1),
             ("NTAP", "NTAP", 9, 191.0511, "USD", 190.4), ("5301.T", "5301", 100, 1690.3512, "JPY", 1811.0),
             ("CNC", "CNC", 28, 63.6857, "USD", 66.2), ("FFIV", "FFIV", 4, 398.76, "USD", 420.5),
             ("7733.T", "7733", 100, 2066.1516, "JPY", 2101.0), ("HUM", "HUM", 4, 378.93, "USD", 395.0)]
OPEN_US = ["ZBRA", "CPAY", "ADBE", "ORCL", "INTU", "AMAT", "KLAC", "LRCX", "MCHP", "TXN",
           "NXPI", "SWKS", "TER", "QRVO", "EPAM"]


def live_sized_report(now, actions, generated_at):
    state_p, bot_p = _TMP / "t6_state.json", _TMP / "t6_bot_state.json"
    write(state_p, {"map": {ib: y for y, ib, *_ in LIVE_BOOK}, "_peak_netliq": 221000,
                    "pos": {y: {"entry": avg, "hw": px * 1.02, "stop": px * 0.9,
                                "entry_date": date.today().isoformat()}
                            for y, ib, q, avg, ccy, px in LIVE_BOOK}})
    write(bot_p, {"updated": "2026-09-16 14:35 UTC", "netliq": 214987,
                  "cash": {"JPY": 83346, "EUR": 39, "USD": 4297}, "positions": []})
    Path(ds.PREV).write_text(json.dumps({"est_netliq": 213500.0}), encoding="utf-8")
    snap = {"netliq": 214987.0, "cash": {"JPY": 83346, "EUR": 39, "USD": 4297},
            "positions": [{"ib_symbol": ib, "qty": q, "avg_cost": avg, "ccy": ccy,
                           "sec_type": "STK"} for y, ib, q, avg, ccy, px in LIVE_BOOK]}
    px_of = {y: px for y, ib, q, avg, ccy, px in LIVE_BOOK}
    fx = {"HKD=X": 7.79, "EURUSD=X": 1.17, "JPY=X": 147.0, "GBPUSD=X": 1.35}

    def get_json(url, timeout=30):
        for y, px in px_of.items():
            if url == ds.PRODUCTS + ds.safe_name(y) + ".json":
                return {"card": {"price": px, "sma200": px * 0.85, "atr": px * 0.03},
                        "generated_at": generated_at}
        for pair, v in fx.items():
            if "/chart/" + pair + "?" in url:
                return {"chart": {"result": [{"meta": {"regularMarketPrice": v}}]}}
        if "interval=1m" in url:
            sym = urllib.parse.unquote(url.split("/chart/")[1].split("?")[0])
            return {"chart": {"result": [{"timestamp": [1789570800],
                                          "indicators": {"quote": [{"close": [px_of[sym]]}]}}]}}
        if url == ds.BASE + "data.json":
            return {"generated_at": generated_at, "actions": actions}
        raise AssertionError("unexpected fetch " + url)

    with Patch(ds, get_json=get_json, _now_utc=lambda: now, STATE=str(state_p),
               BOT_STATE=str(bot_p)), Patch(ib_web, snapshot=lambda: snap):
        return ds.build_report(on_demand=True)[0]


def t6_live_sized_update_in_us_hours_stays_under_telegrams_cap():
    at_1500 = datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc)   # Wed, 11:00 in New York
    actions = ([{"symbol": "HUM", "price": 395.0, "score": 91, "market": "US"}]
               + [{"symbol": s, "price": 150.0 + 7 * k, "score": 88 - k, "market": "US"}
                  for k, s in enumerate(OPEN_US)]
               + [{"symbol": "ETH-USD", "price": 4480.0, "score": 70, "market": "CRYPTO"},
                  {"symbol": "9843.T", "price": 3120.0, "score": 66, "market": "JP"},
                  {"symbol": "BTC-USD", "price": 116000.0, "score": 64, "market": "CRYPTO"}])
    msg = live_sized_report(at_1500, actions, "2026-09-16T14:05:00Z")
    shown = units(ds.plain_text(msg))
    assert shown < 4096, "visible length %d: Telegram refuses the /update reply" % shown
    assert telegram_html_ok(msg) is None, telegram_html_ok(msg)
    assert "Book: <b>LIVE from IB</b>" in msg and "POSITIONS (14)" in msg, msg
    # one reason line per market, all names listed once
    us_held = [y for y, ib, q, avg, ccy, px in LIVE_BOOK if "." not in y]
    assert msg.count("America/New_York session") == 1, msg
    assert msg.count("Europe/Berlin session") == 1, msg
    assert "   held: <code>%s</code>\n" % ", ".join(us_held) in msg, msg
    assert "   held: <code>DBK.DE</code>\n" in msg, msg
    # at most 8 buy signals named, the rest counted
    assert "   buy signals: <code>%s</code> +7 more\n" % ", ".join(OPEN_US[:8]) in msg, msg
    assert re.findall(r"\b(%s)\b" % "|".join(OPEN_US[8:]), msg) == [], msg
    assert "<code>SELL" not in msg and "<code>BUY" not in msg, msg
    # the three that fit no slot are still reported, and nothing went missing
    assert msg.count("1 lot exceeds position size") == 3, msg
    # the backstop is not needed for it: one message
    assert ds.split_message(msg) == [msg]
    print("t6 live-sized /update at Wed 15:00 UTC with 15 open US candidates: "
          "%d visible units, under 4096 OK" % shown)


def t7_digest_mirrors_the_build_stamp_gate():
    # Fri 2026-08-28: the newest build before 23:35/23:40 started at 18:32:56Z
    # (14:32 in New York), and New York's close settled at 21:30Z.
    at = datetime(2026, 8, 28, 23, 40, tzinfo=timezone.utc)
    stale, fresh = "2026-08-28T18:32:56Z", "2026-08-28T22:05:00Z"
    assert market_clock.market_decidable("DXCM", at)[0], "premise: the clock decides it"
    state_p, bot_p = _TMP / "t7_state.json", _TMP / "t7_bot_state.json"
    write(state_p, {"map": {"DXCM": "DXCM"}, "_peak_netliq": 1,
                    "pos": {"DXCM": {"entry": 55, "hw": 58, "stop": 40, "entry_date": "2026-08-20"}}})
    write(bot_p, {"updated": "2026-08-28 23:20 UTC", "netliq": 250000, "cash": {"USD": 30000},
                  "positions": [{"symbol": "DXCM", "qty": 20, "avg_cost": 55.0, "ccy": "USD"}]})
    fx = {"HKD=X": 7.8, "EURUSD=X": 1.1, "JPY=X": 150.0, "GBPUSD=X": 1.3}
    STALE_REASON = "newest build started 18:32Z, before its close settled"

    def report(card_stamp, data_stamp, card=True):
        def get_json(url, timeout=30):
            if url == ds.PRODUCTS + "DXCM.json":
                if not card:
                    raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
                doc = {"card": {"price": 50.0, "sma200": 60.0, "atr": 2.0}}   # regime break
                if card_stamp is not None:
                    doc["generated_at"] = card_stamp
                return doc
            if "range=1y" in url:                    # Yahoo fallback: also below its SMA200
                return {"chart": {"result": [{"meta": {"regularMarketPrice": 50.0},
                                              "indicators": {"quote": [{"close": [60.0] * 200}]}}]}}
            if "interval=1m" in url:
                raise TIMEOUT
            for pair, v in fx.items():
                if "/chart/" + pair + "?" in url:
                    return {"chart": {"result": [{"meta": {"regularMarketPrice": v}}]}}
            if url == ds.BASE + "data.json":
                doc = {"actions": [{"symbol": "MSFT", "price": 100.0, "score": 9, "market": "US"},
                                   {"symbol": "7203.T", "price": 2800.0, "score": 8, "market": "JP"}]}
                if data_stamp is not None:
                    doc["generated_at"] = data_stamp
                return doc
            raise AssertionError("unexpected fetch " + url)

        # one free slot, so a deferred candidate that took one would starve 7203.T
        with Patch(ds, get_json=get_json, _now_utc=lambda: at, STATE=str(state_p),
                   BOT_STATE=str(bot_p), TARGET_POSITIONS=2), Patch(ib_web, snapshot=no_ib):
            msg = ds.build_report(on_demand=False)[0]
        assert telegram_html_ok(msg) is None, telegram_html_ok(msg)
        return msg

    # both from the 18:32Z build: no SELL, no BUY for New York, and the slot
    # MSFT did not use goes to Tokyo, whose close that build did follow
    msg = report(stale, stale)
    assert "SELL DXCM" not in msg and "BUY MSFT" not in msg, msg
    assert ("<b>⏳ DECIDED AFTER THE CLOSE</b>\n• %s\n   held: <code>DXCM</code>\n"
            "   buy signals: <code>MSFT</code>\n" % STALE_REASON) in msg, msg
    assert "<code>BUY 7203.T 100 @ LMT" in msg, msg
    dxcm = [l for l in msg.splitlines() if l.startswith("<code>DXCM ")]
    assert len(dxcm) == 1 and "⚠️" not in dxcm[0], dxcm              # still valued
    # CONTROL: builds that started after 21:30Z decide both, as the bot does
    msg = report(fresh, fresh)
    assert "<code>SELL DXCM 20 @ MKT</code>" in msg and "<code>BUY MSFT " in msg, msg
    assert "DECIDED AFTER THE CLOSE" not in msg and "BUY 7203.T" not in msg, msg
    # the boundary is ib_bot's: a build that started AT the settle instant holds
    # the finished bar; one second earlier does not
    msg = report("2026-08-28T21:30:00Z", "2026-08-28T21:30:00Z")
    assert "<code>SELL DXCM 20 @ MKT</code>" in msg and "DECIDED AFTER" not in msg, msg
    msg = report("2026-08-28T21:29:59Z", "2026-08-28T21:29:59Z")
    assert "SELL DXCM" not in msg and "• newest build started 21:29Z, before its close " \
        "settled\n   held: <code>DXCM</code>\n   buy signals: <code>MSFT</code>\n" in msg, msg
    # a card with no stamp of its own is judged by data.json's
    msg = report(None, stale)
    assert "SELL DXCM" not in msg and "   held: <code>DXCM</code>" in msg, msg
    # the card's own stamp wins for the exit; entries always read data.json's
    msg = report(fresh, stale)
    assert "<code>SELL DXCM 20 @ MKT</code>" in msg and "BUY MSFT" not in msg, msg
    assert "   buy signals: <code>MSFT</code>" in msg and "held: <code>DXCM" not in msg, msg
    msg = report(stale, fresh)
    assert "SELL DXCM" not in msg and "<code>BUY MSFT " in msg, msg
    # no usable generated_at anywhere: the clock alone decides - line for line
    # what a digest without the build check gives
    plain = report(None, None)
    assert "<code>SELL DXCM 20 @ MKT</code>" in plain and "<code>BUY MSFT " in plain, plain
    assert "DECIDED AFTER THE CLOSE" not in plain, plain
    for unusable in ("2026-08-28 18:32", "2026-08-28T18:32:56", "yesterday", 1788460376):
        assert report(unusable, unusable).splitlines()[1:] == plain.splitlines()[1:], unusable
    with Patch(ds, _build_behind_close=lambda *a: None):
        assert report(stale, stale).splitlines()[1:] == plain.splitlines()[1:]
    # a row priced from Yahoo has no build to judge: its exit is shown
    msg = report(stale, stale, card=False)
    assert "<code>SELL DXCM 20 @ MKT</code>" in msg and "DXCM: no dashboard card" in msg, msg
    assert "held: <code>DXCM" not in msg and "   buy signals: <code>MSFT</code>" in msg, msg
    # an /update in Tokyo's evening on the ~04:45Z build: JP is deferred by
    # the build, New York (not yet open) is not
    at = datetime(2026, 9, 16, 10, 15, tzinfo=timezone.utc)
    assert market_clock.market_decidable("7203.T", at)[0]
    assert ds._build_behind_close("7203.T", market_clock.parse_generated_at(
        "2026-09-16T04:45:00Z"), at) == "newest build started 04:45Z, before its close settled"
    # a build from an earlier UTC day carries its date, or "22:05Z" on a
    # two-day-old card would read as tonight's, after the 21:30Z US settle
    late = datetime(2026, 9, 16, 23, 40, tzinfo=timezone.utc)
    assert ds._build_behind_close("DXCM", market_clock.parse_generated_at(
        "2026-09-14T22:05:00Z"), late) == (
        "newest build started 2026-09-14 22:05Z, before its close settled")
    assert ds._build_behind_close("MSFT", market_clock.parse_generated_at(
        "2026-09-16T04:45:00Z"), at) is None
    assert ds._build_behind_close("BTC-USD", market_clock.parse_generated_at(
        "2026-09-16T04:45:00Z"), at) is None
    assert ds._build_behind_close("7203.T", None, at) is None
    print("t7 a card or signal from a build older than its close is decided after the close OK")


def _safe_name_from(relpath):
    """The safe_name defined in a repo file, compiled on its own - read from the
    source rather than imported, because data_fetch needs pandas and yfinance
    and ib_bot a broker library, and neither import is this suite's business."""
    path = Path(__file__).resolve().parent.parent / relpath
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "safe_name"]
    assert len(fn) == 1, "%s: expected one safe_name, found %d" % (relpath, len(fn))
    ns = {}
    exec(compile(ast.Module(body=fn, type_ignores=[]), str(path), "exec"), ns)
    return ns["safe_name"]


def t8_a_dotted_holding_is_priced_from_its_own_card():
    # The rule itself: the digest's copy names files exactly as the dashboard
    # writes them (engine/data_fetch.py) and the bot reads them (ib_bot.py).
    assert ds.safe_name("DBK.DE") == "DBK_DE" and ds.safe_name("5301.T") == "5301_T"
    for relpath in ("engine/data_fetch.py", "execution/ib_bot.py"):
        other = _safe_name_from(relpath)
        for sym in ("DBK.DE", "5301.T", "0700.HK", "BRK.B", "DXCM", "BTC-USD",
                    "^GSPC", "EURUSD=X", "SAN.MC"):
            assert ds.safe_name(sym) == other(sym), (relpath, sym, ds.safe_name(sym))

    # Held: 5301.T (Tokyo) and DBK.DE (Xetra), both comfortably above stop and
    # SMA200. Only the dashboard's real file names are served; the old dotted
    # names 404 as they do live, and a Yahoo fallback would be recorded.
    state_p, bot_p = _TMP / "t8_state.json", _TMP / "t8_bot_state.json"
    write(state_p, {"map": {"5301": "5301.T", "DBK": "DBK.DE"}, "_peak_netliq": 1,
                    "pos": {"5301.T": {"entry": 1690, "hw": 1811, "stop": 1600,
                                       "entry_date": date.today().isoformat()},
                            "DBK.DE": {"entry": 33.7, "hw": 33.63, "stop": 30,
                                       "entry_date": date.today().isoformat()}}})
    write(bot_p, {"updated": "2026-09-16 23:20 UTC", "netliq": 250000,
                  "cash": {"USD": 30000},
                  "positions": [{"symbol": "5301.T", "qty": 100, "avg_cost": 1690.3512, "ccy": "JPY"},
                                {"symbol": "DBK.DE", "qty": 46, "avg_cost": 33.7052, "ccy": "EUR"}]})
    fx = {"HKD=X": 7.8, "EURUSD=X": 1.1, "JPY=X": 150.0, "GBPUSD=X": 1.3}
    cards = {"5301_T.json": 1811.0, "DBK_DE.json": 33.63}
    fetched = []

    def get_json(url, timeout=30):
        fetched.append(url)
        if url.startswith(ds.PRODUCTS):
            name = url[len(ds.PRODUCTS):]
            if name not in cards:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            px = cards[name]
            return {"card": {"price": px, "sma200": px * 0.85, "atr": px * 0.03},
                    "generated_at": "2026-09-16T22:05:00Z"}
        if "range=1y" in url:                        # the Yahoo fallback
            return {"chart": {"result": [{"meta": {"regularMarketPrice": 1.0},
                                          "indicators": {"quote": [{"close": [1.0] * 200}]}}]}}
        if "interval=1m" in url:
            raise TIMEOUT                            # marks fall back to the card price
        for pair, v in fx.items():
            if "/chart/" + pair + "?" in url:
                return {"chart": {"result": [{"meta": {"regularMarketPrice": v}}]}}
        if url == ds.BASE + "data.json":
            return {"generated_at": "2026-09-16T22:05:00Z", "actions": []}
        raise AssertionError("unexpected fetch " + url)

    with Patch(ds, get_json=get_json, _now_utc=lambda: AT_2340, STATE=str(state_p),
               BOT_STATE=str(bot_p)), Patch(ib_web, snapshot=no_ib):
        msg = ds.build_report(on_demand=True)[0]
    assert telegram_html_ok(msg) is None, telegram_html_ok(msg)
    for y, name in (("5301.T", "5301_T.json"), ("DBK.DE", "DBK_DE.json")):
        assert ds.PRODUCTS + name in fetched, (name, fetched)
        assert ds.PRODUCTS + y + ".json" not in fetched, (y, fetched)
        assert "%s: no dashboard card" % y not in msg, msg
    assert "used Yahoo" not in msg and "yahoo " not in msg, msg
    assert not [u for u in fetched if "range=1y" in u], fetched
    # priced from the card: the close on the card, not Yahoo's 1.0
    assert "<code>5301.T   1811.00 " in msg, msg
    assert "<code>DBK.DE     33.63 " in msg, msg
    assert "POSITIONS (2)" in msg and "<code>SELL" not in msg, msg
    print("t8 a dotted holding is priced from its own card (DBK_DE.json), no Yahoo line OK")


if __name__ == "__main__":
    t1_problem_lines_and_names_are_escaped()
    t2_entity_parse_error_resends_once_as_plain_text()
    t3_digest_with_a_network_error_is_delivered()
    t4_digest_lists_markets_in_session_as_decided_after_the_close()
    t5_text_over_the_cap_is_sent_in_pieces_cut_at_newlines()
    t6_live_sized_update_in_us_hours_stays_under_telegrams_cap()
    t7_digest_mirrors_the_build_stamp_gate()
    t8_a_dotted_holding_is_priced_from_its_own_card()
    print("ALL DIGEST HTML TESTS PASS")
