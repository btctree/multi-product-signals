#!/usr/bin/env python3
"""IBKR Client Portal Web API access over self-service OAuth 1.0a.

Why this exists: IBKR forced passkey 2FA on 2026-08-24 and IB Gateway cannot
complete a headless login, so the TWS socket API (ib_bot.py's transport) has
been dead since. OAuth 1.0a performs NO interactive login at all - the VM signs
each request with a local RSA key - so it is immune to any future 2FA policy
change. Consumer key MULTIPROD activated 2026-08-28.

THIS MODULE IS READ-ONLY. It exposes account, position, ledger and price reads
only. There is deliberately no order-placement function here: the order path
belongs behind the broker adapter, which has to pass its verification gates
before it is allowed near a live account. Keeping reads in a separate module
means the reporting path can never accidentally place anything.

Credentials: /root/oauth/oauth.env (mode 600) + the RSA keys in /root/oauth/.
Never in the repo.
"""
import json
import os
import re
import subprocess
import threading

OAUTH_DIR = os.environ.get("MPS_OAUTH_DIR", "/root/oauth")
ENVF = os.path.join(OAUTH_DIR, "oauth.env")

_client = None
_lock = threading.Lock()


class IbWebError(RuntimeError):
    pass


def _dh_prime_hex():
    """IBKR wants the Diffie-Hellman prime as hex; openssl only prints it as a
    colon-separated dump inside -text output, so parse it out."""
    out = subprocess.run(
        ["openssl", "dhparam", "-in", os.path.join(OAUTH_DIR, "dhparam.pem"),
         "-text", "-noout"], capture_output=True, text=True).stdout
    m = re.search(r"(?:prime|P):(.*?)(?:generator|G):", out, re.S | re.I)
    if not m:
        raise IbWebError("could not parse DH prime from dhparam.pem")
    h = re.sub(r"[^0-9a-fA-F]", "", m.group(1))
    return h[2:] if h.startswith("00") else h


def _load_env():
    cfg = {}
    with open(ENVF, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    for k in ("IB_CONSUMER_KEY", "IB_ACCESS_TOKEN", "IB_ACCESS_TOKEN_SECRET"):
        if not cfg.get(k):
            raise IbWebError("%s missing from %s" % (k, ENVF))
    return cfg


def client():
    """Cached IbkrClient. Establishing the live session token is expensive, so
    a short-lived process should reuse one client for all its reads."""
    global _client
    with _lock:
        if _client is not None:
            return _client
        cfg = _load_env()
        os.environ.update({
            "IBIND_USE_OAUTH": "True",
            "IBIND_OAUTH1A_CONSUMER_KEY": cfg["IB_CONSUMER_KEY"],
            "IBIND_OAUTH1A_ACCESS_TOKEN": cfg["IB_ACCESS_TOKEN"],
            "IBIND_OAUTH1A_ACCESS_TOKEN_SECRET": cfg["IB_ACCESS_TOKEN_SECRET"],
            "IBIND_OAUTH1A_DH_PRIME": _dh_prime_hex(),
            "IBIND_OAUTH1A_ENCRYPTION_KEY_FP": os.path.join(OAUTH_DIR, "private_encryption.pem"),
            "IBIND_OAUTH1A_SIGNATURE_KEY_FP": os.path.join(OAUTH_DIR, "private_signature.pem"),
        })
        try:
            from ibind import IbkrClient
        except Exception as e:
            raise IbWebError("ibind not installed: %s" % e)
        try:
            _client = IbkrClient(use_oauth=True)
        except Exception as e:
            raise IbWebError("OAuth handshake failed: %s" % str(e)[:300])
        return _client


def _get(path):
    try:
        return client().get(path).data
    except Exception as e:
        raise IbWebError("GET %s failed: %s" % (path, str(e)[:300]))


def account_id():
    accts = _get("portfolio/accounts")
    if not accts:
        raise IbWebError("no accounts returned")
    return accts[0]["accountId"]


def positions(acct=None):
    """Live positions. Returns rows shaped like the bot's own state, so the
    caller does not have to know this came from the Web API:
        {symbol, ib_symbol, conid, qty, avg_cost, ccy, mkt_price, mkt_value,
         unrealised, sec_type}
    NOTE on symbol identity: the Web API returns `ticker` (e.g. '5301') where
    the engine keys on the Yahoo symbol (e.g. '5301.T'). Mapping back is the
    single most dangerous part of the migration - a key that drifts by one
    character detaches a position from its trailing stop silently - so callers
    MUST map via state['map'] rather than trusting `ticker` directly.
    """
    acct = acct or account_id()
    rows = []
    for p in (_get("portfolio/%s/positions/0" % acct) or []):
        if not p.get("position"):
            continue
        rows.append({
            "ib_symbol": p.get("ticker") or p.get("contractDesc"),
            "conid": p.get("conid"),
            "qty": p.get("position"),
            "avg_cost": p.get("avgCost") if p.get("avgCost") is not None else p.get("avgPrice"),
            "ccy": p.get("currency"),
            "mkt_price": p.get("mktPrice"),
            "mkt_value": p.get("mktValue"),
            "unrealised": p.get("unrealizedPnl"),
            "sec_type": p.get("assetClass"),
            "desc": p.get("contractDesc"),
        })
    return rows


def ledger(acct=None):
    """Per-currency cash + the BASE row carrying NetLiq."""
    return _get("portfolio/%s/ledger" % (acct or account_id())) or {}


def netliq_and_cash(acct=None):
    """(netliq_base, {ccy: cash}) in the account's base currency (HKD).

    Reads BASE.netliquidationvalue - NOT a per-currency slice. Wiring the wrong
    one is the failure the review board flagged as gate G2: ledger['HKD'] is a
    few HKD of local cash while ledger['BASE'] is the whole account, and the
    difference silently turns position sizing into nonsense and trips the
    kill switch on every run.
    """
    led = ledger(acct)
    netliq, cash = None, {}
    for ccy, v in led.items():
        if not isinstance(v, dict):
            continue
        if str(ccy).upper() == "BASE":
            netliq = v.get("netliquidationvalue")
        else:
            cb = v.get("cashbalance")
            if cb:
                cash[ccy] = cb
    if netliq is None:
        raise IbWebError("no BASE row in ledger - refusing to guess NetLiq")
    return netliq, cash


def summary(acct=None):
    return _get("portfolio/%s/summary" % (acct or account_id())) or {}


def auth_status():
    """Brokerage-session state. connected/authenticated/competing."""
    try:
        return client().tickle().data
    except Exception as e:
        raise IbWebError("tickle failed: %s" % str(e)[:200])


def snapshot():
    """One call for everything the reporting path needs."""
    acct = account_id()
    netliq, cash = netliq_and_cash(acct)
    return {"account": acct, "netliq": netliq, "cash": cash,
            "positions": positions(acct)}


if __name__ == "__main__":
    s = snapshot()
    print("account", s["account"], "netliq", s["netliq"])
    print("cash", s["cash"])
    for p in sorted(s["positions"], key=lambda x: str(x["ib_symbol"])):
        print("  %-8s %8g @ %-11s %s" % (p["ib_symbol"], p["qty"], p["avg_cost"], p["ccy"]))
