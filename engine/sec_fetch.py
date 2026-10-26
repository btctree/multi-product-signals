"""Fetch the SEC EDGAR EPS facts the E1 filter needs - for today's US dips only.

E1 (MODEL "D3E1", owner decision 2026-10-26) keeps a US dip only when its
trailing-12-month EPS grew (engine/model_d3e1.py). build_dashboard.py reads
the facts from data/sec/ and never touches the network itself; this script
is the only thing that talks to SEC, in its own CI step BEFORE "Build
dashboard" (.github/workflows/daily.yml), continue-on-error with a 3-minute
timeout. It ALWAYS exits 0 and every failure is soft: a dip without facts is
unscored, and an unscored dip is KEPT - today's plain E rule. A failed build
would leave data.json stale, and the bot defers every exit on a stale build,
so nothing here may be able to stop the build.

Which names: the US tickers whose card (production.analyze, e1=None - the
card itself, so this can never disagree with the build) is an E BUY on the
newest cached bar. About 3 a day over the last year (p95 8, max 18), so
2-6 requests and 1-3 s per hourly build. --all-us fetches every US name (for
research); it has no time budget.

SEC asks every client to name itself (fair-access policy). The User-Agent is
the repo Actions variable SEC_USER_AGENT, e.g. "Multi Product signals
contact@example.org" - whatever contact the owner chooses; none is written
in this code. When it is empty NO request is made at all and the status says
so (every US dip is then unscored, i.e. plain E).

For each name: GET companyconcept us-gaap/EarningsPerShareDiluted, and
EarningsPerShareBasic only when diluted has fewer than 8 usable rows (the
research's choice). One request per SEC["spacing_s"], each timed out at
SEC["timeout_s"] and retried once; a 404 is saved as {} ("no such facts");
the run stops at SEC["budget_s"]. CIKs come from the committed
data/sec_cik.json; a name missing there is looked up in SEC's own
company_tickers.json (fetched at most once per run).

Writes (tmp + os.replace, so a killed step leaves no half file):
  data/sec/<ticker>_<concept>.json   SEC's response, as sent (gitignored)
  data/sec/_status.json              what this run did, read by the build
Nothing has to persist between runs: E1 reads only the latest filings.
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request

import pandas as pd

from config import DATA_DIR, MODEL, SEC, UNIVERSE_FILE
from model_d3e1 import CONCEPTS, facts_from_json, sec_file
from production import analyze
from universe import market_of

SEC_DIR = DATA_DIR / "sec"
CIK_FILE = DATA_DIR / "sec_cik.json"
STATUS_NAME = "_status.json"
FACT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{concept}.json"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


def utc_now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_atomic(path, body: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(body)
    os.replace(tmp, path)


def us_tickers():
    """The universe's US names, read-only (load_universe would CREATE a
    universe file when there is none - not this step's business)."""
    uni = json.loads(UNIVERSE_FILE.read_text(encoding="utf-8"))
    return [t for t in uni.get("tickers", []) if market_of(t) == "US"]


def _cached_prices(ticker):
    """The price file data_fetch.py wrote this run, as fetch_all reads it
    (more than 260 bars, or the build will not analyse it either)."""
    from data_fetch import cache_path         # only here: it imports yfinance
    p = cache_path(ticker)
    if not p.exists():
        return None
    df = pd.read_csv(p, index_col=0, parse_dates=True)
    return df if len(df) > 260 else None


def us_dips(tickers, load=_cached_prices):
    """(dips, read): the tickers whose card is an E BUY on the newest bar, and
    how many price files could be read at all. A name that fails to load or
    analyse is skipped - it cannot be a dip in the build either."""
    dips, read = [], 0
    for t in tickers:
        try:
            df = load(t)
            if df is None:
                continue
            read += 1
            c = analyze(t, df)
            if c.get("action") == "BUY" and c.get("signal") == "E":
                dips.append(t)
        except Exception:
            continue
    return dips, read


class Sec:
    """Polite, bounded HTTP for SEC: spaced requests, one retry, a budget."""

    def __init__(self, ua, spacing=SEC["spacing_s"], timeout=SEC["timeout_s"],
                 budget=SEC["budget_s"], opener=urllib.request.urlopen,
                 sleep=time.sleep, clock=time.monotonic):
        self.ua, self.spacing, self.timeout, self.budget = ua, spacing, timeout, budget
        self.open, self.sleep, self.clock = opener, sleep, clock
        self.t0 = clock()
        self.last = None
        self.requests = 0

    def out_of_time(self):
        return self.budget is not None and self.clock() - self.t0 > self.budget

    def get(self, url):
        """The body, or None for a 404. Anything else is retried once, then
        raised. The User-Agent is never printed: it may hold an address."""
        err = None
        for attempt in (0, 1):
            if self.last is not None:
                wait = self.spacing - (self.clock() - self.last)
                if wait > 0:
                    self.sleep(wait)
            self.last = self.clock()
            self.requests += 1
            req = urllib.request.Request(url, headers={"User-Agent": self.ua,
                                                       "Accept-Encoding": "identity"})
            try:
                with self.open(req, timeout=self.timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                err = e
            except Exception as e:                # timeout, reset, DNS
                err = e
            if attempt == 0 and not self.out_of_time():
                self.sleep(1.0)
            else:
                break
        raise err


def cik_map(sec, tickers, status):
    """{ticker: CIK} for `tickers`: the committed map first, then - only for
    names it lacks - SEC's company_tickers.json, with the research's lookup
    (exact ticker, else '-' read as '.')."""
    try:
        known = json.loads(CIK_FILE.read_text(encoding="utf-8")).get("cik", {})
    except Exception:
        known = {}
    out = {t: int(known[t]) for t in tickers if t in known}
    missing = [t for t in tickers if t not in out]
    if missing and not sec.out_of_time():
        try:
            body = sec.get(TICKERS_URL)
            rows = json.loads(body).values() if body else []
            live = {str(v["ticker"]).upper(): int(v["cik_str"]) for v in rows}
            for t in missing:
                c = live.get(t.upper()) or live.get(t.upper().replace("-", "."))
                if c is not None:
                    out[t] = c
        except Exception as e:
            status["errors"].append(f"company_tickers.json: {type(e).__name__}")
    return out


def fetch(sec, tickers, status):
    """Save each ticker's facts; record what happened in `status`."""
    ciks = cik_map(sec, tickers, status)
    for t in tickers:
        if sec.out_of_time():
            status["budget_hit"] = True
            status["skipped"] = tickers[tickers.index(t):]
            break
        if t not in ciks:
            status["no_cik"].append(t)
            continue
        # this run's answer or none: an older file must not stand in for a
        # fetch that fails now
        for concept in CONCEPTS:
            sec_file(SEC_DIR, t, concept).unlink(missing_ok=True)
        for concept in CONCEPTS:
            try:
                body = sec.get(FACT_URL.format(cik=ciks[t], concept=concept))
                data = {} if body is None else json.loads(body)
            except Exception as e:
                status["errors"].append(f"{t} {concept}: {type(e).__name__}")
                break
            write_atomic(sec_file(SEC_DIR, t, concept), b"{}" if body is None else body)
            status["fetched"] += 1
            if body is None:
                status["not_found"] += 1
            if facts_from_json(data) is not None:
                break                     # diluted is usable - basic is not needed


def main(argv=None, load=_cached_prices, sec_factory=Sec, env=None):
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    all_us = "--all-us" in argv
    ua = str(env.get("SEC_USER_AGENT", "")).strip()
    status = {"ran_at": utc_now(), "model": MODEL, "user_agent_set": bool(ua),
              "mode": "all-us" if all_us else "dips", "state": "", "message": "",
              "us_read": 0, "dips": [], "fetched": 0, "not_found": 0, "no_cik": [],
              "errors": [], "budget_hit": False, "skipped": [], "requests": 0}
    try:
        if MODEL != "D3E1" and not all_us:
            status.update(state="model_e", message='MODEL is "E": E1 is not used, '
                                                   'no SEC request made')
            return 0
        if not ua or any(ch in ua for ch in "\r\n"):
            status.update(state="no_user_agent",
                          message="SEC_USER_AGENT is not set (or not one line): no SEC "
                                  "request made - every US dip is unscored and kept (plain E)")
            return 0
        tickers = us_tickers()
        if all_us:
            names = sorted(tickers)
        else:
            names, status["us_read"] = us_dips(tickers, load)
            if not status["us_read"]:
                status.update(state="failed", message="no US price file could be read - "
                                                      "nothing to check")
                return 0
        status["dips"] = names
        if not names:
            status.update(state="no_dips", message="no US dip on the newest bar - "
                                                   "nothing to fetch")
            return 0
        sec = sec_factory(ua, budget=None if all_us else SEC["budget_s"])
        try:
            fetch(sec, names, status)
        finally:
            status["requests"] = sec.requests
        short = status["errors"] or status["no_cik"] or status["budget_hit"]
        status.update(state="partial" if short else "ok",
                      message=(f"{status['fetched']} file(s) for {len(names)} name(s)"
                               + (f"; errors: {len(status['errors'])}" if status["errors"] else "")
                               + (f"; no CIK: {', '.join(status['no_cik'])}"
                                  if status["no_cik"] else "")
                               + ("; stopped at the time budget" if status["budget_hit"] else "")
                               + (" - names without facts are unscored and kept (plain E)"
                                  if short else "")))
        return 0
    except Exception as e:
        status.update(state="failed", message=f"{type(e).__name__}: {str(e)[:200]} - every "
                                              f"US dip without facts is unscored and kept")
        return 0
    finally:
        try:
            write_atomic(SEC_DIR / STATUS_NAME,
                         json.dumps(status, indent=1).encode("utf-8"))
        except Exception as e:
            print(f"::warning::SEC step could not write its status ({type(e).__name__})")
        line = (f"SEC step: {status['state']} - {status['message']} "
                f"(dips {status['dips']}, requests {status['requests']})")
        print(line if status["state"] in ("ok", "no_dips", "model_e")
              else "::warning::" + line)


if __name__ == "__main__":
    try:
        main()
    except BaseException as e:          # nothing - not even an interrupt - fails the step
        print(f"::warning::SEC step aborted ({type(e).__name__})")
    sys.exit(0)
