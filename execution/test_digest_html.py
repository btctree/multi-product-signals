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

Nothing here touches /root or the network: every path is a temp file, every
fetch and every Telegram call is a stub.
"""
import io
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
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
os.environ.pop("EXCLUDED_CASH", None)

import alerts                                      # noqa: E402
import daily_signal as ds                          # noqa: E402
import ib_web                                      # noqa: E402
import telegram_poll                               # noqa: E402
testenv.assert_isolated()

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
    with Patch(ds, get_json=fake_get_json), Patch(ib_web, snapshot=no_ib):
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
    with Patch(ds, get_json=fake_get_json), Patch(ib_web, snapshot=no_ib), \
            Patch(urllib.request, urlopen=tg):
        assert ds.main() == 0
    assert len(tg.calls) == 1, "the escaped digest needed no resend"
    assert "&lt;urlopen error timed out&gt;" in tg.calls[0]["text"]
    assert prev.exists(), "the P&L baseline must be saved"
    print("t3 digest with a feed timeout is sent and the baseline saved OK")


if __name__ == "__main__":
    t1_problem_lines_and_names_are_escaped()
    t2_entity_parse_error_resends_once_as_plain_text()
    t3_digest_with_a_network_error_is_delivered()
    print("ALL DIGEST HTML TESTS PASS")
