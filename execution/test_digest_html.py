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

Nothing here touches /root or the network: every path is a temp file, every
fetch and every Telegram call is a stub.
"""
import io
import json
import os
import re
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="mps-digest-"))
REPO = _TMP / "repo"
(REPO / "execution").mkdir(parents=True)
(REPO / "data").mkdir()
(_TMP / "earmark").mkdir()
os.environ["MPS_REPO"] = str(REPO)
os.environ["MPS_PREV"] = str(_TMP / "daily_signal_prev.json")
os.environ["MPS_MANUAL"] = str(_TMP / "manual_state.json")
os.environ["MPS_ENV"] = str(_TMP / "telegram.env")
os.environ["MPS_EARMARK_DIR"] = str(_TMP / "earmark")
os.environ["MPS_ALERT_DIR"] = str(_TMP / "outbox")
os.environ["MPS_TG_OFFSET"] = str(_TMP / "telegram_offset.json")
os.environ["MPS_TG_LOCK"] = str(_TMP / "tg_poll.lock")
os.environ["MPS_OAUTH_DIR"] = str(_TMP / "oauth")
os.environ.pop("EXCLUDED_CASH", None)

import alerts                                      # noqa: E402
import daily_signal as ds                          # noqa: E402
import ib_web                                      # noqa: E402
import market_clock                                # noqa: E402
import telegram_poll                               # noqa: E402

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
        if step == "not-json":
            raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", {},
                                         io.BytesIO(b"<html>bad gateway</html>"))
        raise refusal(req, step)


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
        if url == ds.PRODUCTS + "DBK.DE.json":
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
    assert ("<code>DBK.DE</code> held · Europe/Berlin session 09:00-17:30 is still "
            "open or settling (local Wed 12:15; its bar is final from 19:00)") in msg, msg
    assert "<code>SAP.DE</code> buy signal · Europe/Berlin session" in msg, msg
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


if __name__ == "__main__":
    t1_problem_lines_and_names_are_escaped()
    t2_entity_parse_error_resends_once_as_plain_text()
    t3_digest_with_a_network_error_is_delivered()
    t4_digest_lists_markets_in_session_as_decided_after_the_close()
    print("ALL DIGEST HTML TESTS PASS")
