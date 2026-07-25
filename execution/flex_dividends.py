"""Dividend capture via IB Flex Web Service (stdlib only — runs on the VM).

Trade executions never show dividends (they arrive as cash credits), so this
pulls IB's Activity Flex statement instead: CashTransaction rows of type
Dividends / Payment In Lieu Of Dividends / Withholding Tax.

Ledger: data/dividends_ledger.jsonl — append-only, keyed by IB transactionID
(or a content hash when absent). GBP value is stamped at the PAYMENT date via
the ECB reference-rate API (frankfurter.dev, keyless) — HMRC accepts a
consistent published daily rate; rows fall back to flag "rate_missing".

Config (never in the repo): /root/flex.conf with two lines
    FLEX_TOKEN=...      (Client Portal > Settings > FlexWeb Service)
    FLEX_QUERY_ID=...   (Performance & Reports > Flex Queries — an Activity
                         query with the Cash Transactions section ticked)
Env vars FLEX_TOKEN / FLEX_QUERY_ID override the file. If neither exists the
capture is a silent no-op, so the publish pipeline never breaks.
"""
import hashlib
import json
import os
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LEDGER = REPO / "data" / "dividends_ledger.jsonl"
CONF = Path("/root/flex.conf")
FLEX_BASE = ("https://gdcdyn.interactivebrokers.com/Universal/servlet/"
             "FlexStatementService")
DIV_TYPES = {"Dividends": "dividend",
             "Payment In Lieu Of Dividends": "pil",
             "Withholding Tax": "wht"}


def _config():
    cfg = {}
    if CONF.exists():
        for line in CONF.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    tok = os.environ.get("FLEX_TOKEN") or cfg.get("FLEX_TOKEN")
    qid = os.environ.get("FLEX_QUERY_ID") or cfg.get("FLEX_QUERY_ID")
    return (tok, qid) if tok and qid else None


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def fetch_statement(token, query_id):
    """SendRequest -> poll GetStatement until the XML is ready."""
    first = _get(f"{FLEX_BASE}.SendRequest?t={token}&q={query_id}&v=3")
    root = ET.fromstring(first)
    if (root.findtext("Status") or "").strip() != "Success":
        raise RuntimeError(f"Flex SendRequest: {root.findtext('ErrorMessage')}")
    code = (root.findtext("ReferenceCode") or "").strip()
    url = (root.findtext("Url") or f"{FLEX_BASE}.GetStatement").strip()
    for attempt in range(6):
        time.sleep(5 + 5 * attempt)
        body = _get(f"{url}?t={token}&q={code}&v=3")
        if "<FlexQueryResponse" in body:
            return body
        # "statement generation in progress" comes back as an error doc
    raise RuntimeError("Flex statement not ready after 6 polls")


_RATE_CACHE = {}


def gbp_rate_on(day, ccy):
    """GBP per 1 unit of ccy at a given ISO date (ECB reference rates)."""
    if ccy == "GBP":
        return 1.0
    key = (day, ccy)
    if key in _RATE_CACHE:
        return _RATE_CACHE[key]
    rate = None
    for host in ("https://api.frankfurter.dev/v1", "https://api.frankfurter.app"):
        try:
            data = json.loads(_get(f"{host}/{day}?base={ccy}&symbols=GBP"))
            rate = float(data["rates"]["GBP"])
            break
        except Exception:
            continue
    _RATE_CACHE[key] = rate
    return rate


def _iso_date(raw):
    """Normalize IB Flex dates: '2026-07-11', '2026/07/11', '20260711' and
    'yyyyMMdd;HHmmss' all appear depending on the query's date format."""
    if not raw:
        return None
    s = str(raw).split(";")[0].strip().replace("/", "-")
    if len(s) == 8 and s.isdigit():
        s = f"{s[:4]}-{s[4:6]}-{s[6:]}"
    s = s[:10]
    try:
        date.fromisoformat(s)
        return s
    except ValueError:
        return None


def parse_cash_transactions(xml_text):
    """Flex XML -> normalized dividend/withholding rows (no rates yet)."""
    rows, seen_ids = [], {}
    root = ET.fromstring(xml_text)
    for ct in root.iter("CashTransaction"):
        kind = DIV_TYPES.get((ct.get("type") or "").strip())
        if not kind:
            continue
        # statements configured with Summary+Detail repeat every row — keep
        # detail only (rows without the attribute are plain single-level)
        if (ct.get("levelOfDetail") or "").upper() == "SUMMARY":
            continue
        day = (_iso_date(ct.get("settleDate")) or _iso_date(ct.get("reportDate"))
               or _iso_date(ct.get("dateTime")))
        amt = float(ct.get("amount") or 0)
        tid = (ct.get("transactionID") or "").strip()
        if not tid:
            base = "h" + hashlib.sha1(
                f"{ct.get('symbol')}|{day}|{amt}|{kind}|{ct.get('conid')}"
                .encode()).hexdigest()[:16]
            # identical same-day rows (two equal partial credits) must not
            # collapse into one id
            n = seen_ids.get(base, 0) + 1
            seen_ids[base] = n
            tid = base if n == 1 else f"{base}-{n}"
        rows.append({
            "id": tid, "date": day, "symbol": (ct.get("symbol") or "").strip(),
            "con_id": int(ct.get("conid") or 0), "type": kind,
            "amount": amt, "ccy": (ct.get("currency") or "USD").strip(),
            "description": (ct.get("description") or "").strip()[:120],
            "source": "flex",
        })
    return rows


def _append(rows):
    if not rows:
        return 0
    LEDGER.parent.mkdir(exist_ok=True)
    seen, tail_ok = set(), True
    if LEDGER.exists():
        text = LEDGER.read_text(encoding="utf-8")
        tail_ok = (not text) or text.endswith("\n")
        for line in text.splitlines():
            try:
                seen.add(json.loads(line)["id"])
            except Exception:
                pass
    new = [r for r in rows if r["id"] not in seen]
    if new:
        with LEDGER.open("a", encoding="utf-8") as f:
            if not tail_ok:
                f.write("\n")      # never glue onto a truncated tail line
            for r in new:
                f.write(json.dumps(r) + "\n")
    return len(new)


SWEEP_MARK = LEDGER.parent / ".dividends_last_sweep"


def _backfill_missing_rates():
    """Retry GBP rates for rows a transient lookup failure left unstamped.
    Appends corrected copies — the loader keeps the last row per id."""
    if not LEDGER.exists():
        return 0
    latest = {}
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
            latest[r["id"]] = r
        except Exception:
            pass
    fixed = []
    for r in latest.values():
        if r.get("gbp_rate") or not r.get("date"):
            continue
        rate = gbp_rate_on(r["date"], r["ccy"])
        if rate:
            r2 = dict(r, gbp_rate=rate)
            r2.pop("flags", None)
            fixed.append(r2)
    n = _append_corrections(fixed)
    if n:
        print(f"[dividends] backfilled GBP rate on {n} row(s)")
    return n


def _append_corrections(rows):
    if not rows:
        return 0
    with LEDGER.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return len(rows)


def capture_if_configured(force=False):
    """Daily pull; silent no-op without config. Returns rows appended."""
    cfg = _config()
    if not cfg:
        return 0
    today = date.today().isoformat()
    if not force and SWEEP_MARK.exists() and \
            SWEEP_MARK.read_text().strip() == today:
        return 0                                 # already swept today
    xml_text = fetch_statement(*cfg)
    rows = parse_cash_transactions(xml_text)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    for r in rows:
        rate = gbp_rate_on(r["date"], r["ccy"]) if r["date"] else None
        r["gbp_rate"] = rate
        r["captured"] = now
        if not rate:
            r["flags"] = ["rate_missing"]
    n = _append(rows)
    _backfill_missing_rates()
    SWEEP_MARK.write_text(today)
    if n:
        print(f"[dividends] captured {n} new cash transaction row(s)")
    return n


if __name__ == "__main__":
    print("appended:", capture_if_configured(force="--force" in __import__("sys").argv))
