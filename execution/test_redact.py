#!/usr/bin/env python3
"""Golden tests: the IBKR account number never reaches a published file.

Run from this directory:  python test_redact.py

Review 2026-09-17: every order goes to iserver/account/<acct>/orders, and a
failed POST raised OrderError("POST iserver/account/<acct>/orders failed: ...").
broker put that text in trade.log, ib_bot and ib_commands copied it into
activity rows, and both publishers pushed data/bot_state.json to the PUBLIC
repo - 9 rows from 2026-09-01..03 carry the live id. What is locked down:

  * error text is redacted at the source (OrderError, IbWebError) and again in
    the shim's trade.log, for the live id and any U+digits token;
  * both publishers scrub every activity row on every write, so the rows
    already published are cleaned on the next publish - other rows unchanged;
  * the dividends ledger's free text (also published) is scrubbed too.

Git history is deliberately NOT rewritten. Nothing here touches /root, the
repo's data/ or the network: every path is a temp dir and git is stubbed.
"""
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

os.environ.setdefault("IB_BACKEND", "web")
# Every /root default at a temp path before import - the same names as before,
# plus the flex conf, spool, memo and publish_web's paths (review 2026-09-17,
# test isolation). See testenv.py.
import testenv                                     # noqa: E402
_TMP = testenv.isolate("mps-redact-")
os.environ.pop("EXCLUDED_CASH", None)

import broker                                      # noqa: E402
import flex_dividends                              # noqa: E402
import ib_bot                                      # noqa: E402
import ib_orders                                   # noqa: E402
import ib_web                                      # noqa: E402
testenv.assert_isolated()

ACCT = "U7123456"                                  # a made-up live-shaped id
LEAK = re.compile(r"\bU\d{5,}\b")
ib_bot.log = lambda *a, **k: None


class Resp:
    def __init__(self, data):
        self.data = data


class FakeClient:
    """ibind's client: gets answer from `answers`, every post raises."""

    def __init__(self, answers=None, post_error="400 Bad Request: rejected"):
        self.answers, self.post_error = answers or {}, post_error

    def get(self, path):
        if path in self.answers:
            return Resp(self.answers[path])
        raise RuntimeError("404 for %s on account %s" % (path, ACCT))

    def post(self, path, params=None):
        raise RuntimeError(self.post_error)


def reset():
    ib_web._client = None
    ib_web._KNOWN_ACCOUNTS.clear()


def t1_order_error_text_is_redacted_at_the_source():
    reset()
    ib_orders._iserver_ready = True                # no session handshake in a test
    ib_web._client = FakeClient(post_error="400 Bad Request: account %s not allowed" % ACCT)
    ib = broker.IB()
    ib._acct = ACCT
    trade = ib.placeOrder(broker.Contract("DELL", "STK", "USD", conId=1001),
                          broker.MarketOrder("SELL", 4))
    status, err = ib_bot._order_verdict(trade)
    assert status == "REJECTED", status
    assert "POST iserver/account/U***/orders failed" in err, err
    assert ACCT not in err and not LEAK.search(err), err
    # the exception classes redact whatever they are built with
    e = ib_orders.OrderError("GET iserver/account/%s/summary failed: U99999 and U123" % ACCT)
    assert str(e) == "GET iserver/account/U***/summary failed: U*** and U123", str(e)
    try:
        ib_web._get("portfolio/%s/positions/0" % ACCT)
        raise AssertionError("the fake client should have refused")
    except ib_web.IbWebError as w:
        assert ACCT not in str(w) and "portfolio/U***/positions/0" in str(w), str(w)
    # an id that is NOT U+digits is redacted once account_id() has read it
    reset()
    ib_web._client = FakeClient(answers={"portfolio/accounts": [{"accountId": "F7654321"}]})
    assert ib_web.account_id() == "F7654321"
    assert ib_web.redact("POST iserver/account/F7654321/orders failed") == \
        "POST iserver/account/U***/orders failed"
    # the shim's trade.log redacts a failure that did not come from ib_orders,
    # and the Error-110 translation still fires on a redacted message
    reset()
    real_place = ib_orders.place

    def boom(*a, **k):
        raise ValueError("price does not conform for account %s" % ACCT)
    ib_orders.place = boom
    try:
        t = broker.IB().placeOrder(broker.Contract("SAP", "STK", "EUR", conId=7),
                                   broker.LimitOrder("BUY", 1, 100.0))
    finally:
        ib_orders.place = real_place
    msg = t.log[-1].message
    assert msg.startswith("Error 110") and ACCT not in msg and "U***" in msg, msg
    # redact never raises and leaves ordinary text alone
    assert ib_web.redact(None) == "" and ib_web.redact(12) == "12"
    for keep in ("SELL 4 U @ MKT", "DU1234567 paper", "U1234", "mps-DELL-S-20260917"):
        assert ib_web.redact(keep) == keep, keep
    reset()
    print("t1 order error text redacted at the source and in trade.log OK")


LEAKED_ROWS = [
    {"time": "2026-09-01 23:35 UTC", "action": "SELL", "qty": 5, "symbol": "SNOW",
     "limit": "MKT-open", "ccy": "USD", "reason": "trailing stop 311.20",
     "status": "REJECTED",
     "error": "POST iserver/account/%s/orders failed: RestClient.post() got an "
              "unexpected keyword argument 'json'" % ACCT},
    {"time": "2026-09-03 10:54 UTC", "action": "SELL", "qty": 54, "symbol": "BEN",
     "limit": "MKT-open", "ccy": "USD", "reason": "trailing stop 44.10",
     "status": "REJECTED",
     "error": "POST iserver/account/%s/orders failed: IBKR returned 400 Bad "
              "Request: no bridge." % ACCT},
]
CLEAN_ROW = {"time": "2026-09-15 07:02 UTC", "action": "BUY", "qty": 46, "symbol": "U",
             "limit": 33.8, "ccy": "USD", "reason": "entry signal, score 100",
             "status": "sent", "error": ""}


def assert_clean(path):
    text = path.read_text(encoding="utf-8")
    assert not LEAK.search(text) and ACCT not in text, LEAK.search(text)
    act = json.loads(text)["activity"]
    assert act[0]["error"] == ("POST iserver/account/U***/orders failed: RestClient.post() "
                               "got an unexpected keyword argument 'json'"), act[0]
    assert act[1]["error"].startswith("POST iserver/account/U***/orders failed: IBKR"), act[1]
    for got, want in zip(act[:2], LEAKED_ROWS):
        assert {k: v for k, v in got.items() if k != "error"} == \
            {k: v for k, v in want.items() if k != "error"}, "only the id may change"
    assert act[2] == CLEAN_ROW, act[2]
    return act


class Done:
    returncode, stdout, stderr = 0, "", ""


def seed_repo():
    repo = Path(tempfile.mkdtemp(prefix="repo-", dir=str(_TMP)))
    (repo / "data").mkdir()
    (repo / "execution").mkdir()
    (repo / "execution" / "state.json").write_text('{"map": {}, "pos": {}}', encoding="utf-8")
    (repo / "data" / "bot_state.json").write_text(
        json.dumps({"activity": LEAKED_ROWS + [CLEAN_ROW]}, indent=1), encoding="utf-8")
    return repo


def t2_publish_web_scrubs_rows_already_published():
    import publish_web
    repo = seed_repo()
    old = (publish_web.STATE, publish_web.DATA, publish_web.REPO, ib_web.snapshot,
           subprocess.run, publish_web.sys.argv)
    try:
        publish_web.STATE = repo / "execution" / "state.json"
        publish_web.DATA, publish_web.REPO = repo / "data", repo
        ib_web.snapshot = lambda: {"positions": [], "netliq": 100000.0, "cash": {"HKD": 10.0}}
        subprocess.run = lambda *a, **k: Done()
        publish_web.sys.argv = ["publish_web.py"]
        assert publish_web.main() == 0
    finally:
        (publish_web.STATE, publish_web.DATA, publish_web.REPO, ib_web.snapshot,
         subprocess.run, publish_web.sys.argv) = old
    act = assert_clean(repo / "data" / "bot_state.json")
    assert len(act) == 3
    print("t2 publish_web scrubs old activity rows on the next publish OK")


class AV:
    def __init__(self, tag, value, currency):
        self.tag, self.value, self.currency = tag, value, currency


class PubIB:
    def positions(self):
        return []

    def accountValues(self):
        return [AV("NetLiquidation", "100000", "HKD"), AV("CashBalance", "10", "HKD")]


def t3_ib_bot_publish_state_scrubs_old_and_new_rows():
    import fills_capture
    import flex_dividends as fd
    import uk_cgt
    repo = seed_repo()
    new_row = dict(CLEAN_ROW, symbol="DBK", error="order not accepted: %s is restricted" % ACCT)
    ib_bot.PLACED.append(new_row)
    old = (ib_bot.__file__, subprocess.run, fills_capture.capture,
           fd.capture_if_configured, uk_cgt.build_report)
    try:
        # publish_state finds the repo from its own file: point that at the temp
        # repo, so neither the real data/ nor a real git push can be touched
        ib_bot.__file__ = str(repo / "execution" / "ib_bot.py")
        subprocess.run = lambda *a, **k: Done()
        fills_capture.capture = lambda *a, **k: 0
        fd.capture_if_configured = lambda *a, **k: 0
        uk_cgt.build_report = lambda *a, **k: None
        ib_bot.publish_state(PubIB(), {"map": {}, "pos": {}}, 100000.0)
    finally:
        (ib_bot.__file__, subprocess.run, fills_capture.capture,
         fd.capture_if_configured, uk_cgt.build_report) = old
        del ib_bot.PLACED[:]
    act = assert_clean(repo / "data" / "bot_state.json")
    assert act[3]["error"] == "order not accepted: U*** is restricted", act[3]
    assert act[3]["symbol"] == "DBK"
    print("t3 ib_bot.publish_state scrubs carried-over and new rows OK")


def t4_dividend_free_text_is_scrubbed_at_write_time():
    xml = ('<FlexQueryResponse><FlexStatements><FlexStatement accountId="%s">'
           '<CashTransactions><CashTransaction type="Dividends" symbol="BEN" conid="1" '
           'currency="USD" amount="1.00" settleDate="20260711" transactionID="9" '
           'description="BEN DIV credited to %s" /></CashTransactions>'
           '</FlexStatement></FlexStatements></FlexQueryResponse>' % (ACCT, ACCT))
    rows = flex_dividends.parse_cash_transactions(xml)
    assert rows[0]["description"] == "BEN DIV credited to U***", rows
    assert ACCT not in json.dumps(rows)
    print("t4 dividends ledger free text scrubbed OK")


if __name__ == "__main__":
    try:
        t1_order_error_text_is_redacted_at_the_source()
        t2_publish_web_scrubs_rows_already_published()
        t3_ib_bot_publish_state_scrubs_old_and_new_rows()
        t4_dividend_free_text_is_scrubbed_at_write_time()
    finally:
        reset()
        ib_orders._iserver_ready = False
    print("ALL REDACTION TESTS PASS")
