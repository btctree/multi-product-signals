#!/usr/bin/env python3
"""Rebuild data/day_parts.json for the days that ran before it existed.

    python execution/backfill_day_parts.py            # dry run, prints the fit
    python execution/backfill_day_parts.py --write    # writes the file

day_parts.py records what each day's P&L was made of, but only from the publish
that follows its deploy. Everything before that is still recoverable, because
the VM commits data/bot_state.json on every publish - a few dozen snapshots a
day, each carrying the positions held, the cash in each currency and the NetLiq
they added up to. The LAST snapshot of each day is that day's record, the same
rule netliq_history uses.

Two things are missing from those snapshots and are dealt with honestly:

  1. PRICES. bot_state carries quantities, not values. Closes come from the
     published product cards (docs/products/<sym>.json on the live site), which
     are the same series the rest of the dashboard draws.

  2. EXCHANGE RATES. Nothing published in that period recorded them. Rather
     than invent a number, this fits ONE rate per currency across every day at
     once: each day is an equation
         netliq + earmark = SUM(qty * close * rate) + SUM(cash * rate)
     over 50-odd days with three or four unknowns, so the rates are recovered
     by least squares from the account's own history. The fit residual is
     printed per day and per currency - if it does not come out small, the
     reconstruction is wrong and the run says so instead of publishing it.

     A fitted constant rate cannot capture a currency DRIFTING over two months.
     That error does not touch the day-over-day price moves the breakdown is
     made of (a rate scales a contribution, it does not invent one); it lands in
     the day's "unexplained" line, which the dashboard shows rather than hides.

Every row written here is stamped "src": "reconstructed" and the dashboard
labels it, so a reconstructed day is never passed off as a measured one.
"""
import datetime
import io
import json
import os
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import day_parts                                            # noqa: E402

SITE = "https://btctree.github.io/multi-product-signals/products/%s.json"
BASE = "HKD"


def log(*a):
    print(*a)


def _git(*args):
    r = subprocess.run(["git", "-C", ROOT] + list(args),
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("git %s: %s" % (args[0], (r.stderr or "").strip()[:200]))
    return r.stdout


def daily_snapshots():
    """{day: parsed bot_state.json} taking the LAST commit of each UTC day.

    Bucketed on the commit's UTC timestamp, NOT --date=format-local: on a BST
    machine the local day ends at 23:00 UTC, which picked the 22:5x publish and
    left 43 of 56 rows disagreeing with netliq_history for the same date by up
    to 497 HKD. netliq_history keys on UTC, and these rows are differenced
    against it, so they have to agree on where a day ends.
    """
    out = {}
    lines = _git("log", "--pretty=%H %ct", "--", "data/bot_state.json").splitlines()
    # git log is newest-first, so the first commit seen for a day is its last
    for ln in lines:
        sha, _, ct = ln.partition(" ")
        try:
            day = datetime.datetime.fromtimestamp(
                int(ct.strip()), datetime.timezone.utc).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            continue
        if day in out:
            continue
        try:
            out[day] = json.loads(_git("show", "%s:data/bot_state.json" % sha))
        except Exception as e:
            log("  skip %s (%s)" % (day, e))
    return out


def closes(symbols, cache_dir=None):
    """{sym: {date: close}} from the published product cards.

    Always from the LIVE site, never from docs/products/: that directory is
    gitignored build output, so a local copy can be any age at all - the first
    run of this script read a DXCM card that stopped on 2026-07-10 and silently
    rejected every day of the period. Downloads are cached under cache_dir for
    reruns, keyed by symbol, and the cache is this script's own, not the site's.
    """
    out = {}
    for s in sorted(symbols):
        safe = s.replace("^", "_IDX_").replace("=", "_EQ_").replace(".", "_")
        cached = os.path.join(cache_dir, "%s.json" % safe) if cache_dir else None
        raw = None
        if cached and os.path.exists(cached):
            raw = io.open(cached, encoding="utf-8").read()
        else:
            try:
                with urllib.request.urlopen(SITE % safe, timeout=30) as fh:
                    raw = fh.read().decode("utf-8")
                if cached:
                    io.open(cached, "w", encoding="utf-8").write(raw)
            except Exception as e:
                log("  no prices for %s (%s)" % (s, e))
                continue
        try:
            out[s] = {d: float(v) for d, v in json.loads(raw).get("prices", [])}
        except Exception as e:
            log("  bad prices for %s (%s)" % (s, e))
    return out


def mark_at(px_sym, day):
    """The close a broker would mark this holding at on `day`.

    A Tokyo holiday or any weekend leaves a symbol with no bar that date. The
    position did not become unpriced - it is still worth its last close, which
    is exactly how the account itself is valued. Only a date BEFORE the symbol's
    first published bar is genuinely unknown, and that returns None.
    """
    if not px_sym:
        return None
    if day in px_sym:
        return px_sym[day]
    prev = [d for d in px_sym if d < day]
    return px_sym[max(prev)] if prev else None


def fx_history(ccys, start, base=BASE):
    """{ccy: {date: rate}} of REAL daily rates, from the same source as the prices.

    The first version of this script fitted ONE average rate per currency across
    the whole period, because nothing the system published had ever recorded a
    rate. That was wrong twice over: Yahoo carries <CCY><BASE>=X daily, the same
    feed engine/data_fetch already uses for every price, and a constant rate
    leaves every day off by the currency's drift - which then surfaced on the
    dashboard as a "reconstruction error" line the owner could not act on.
    Cross-check on 2026-09-19: Yahoo USDHKD 7.8441 against IB's own 7.8451.
    """
    import yfinance as yf                       # local one-off; not on the VM path
    want = [c for c in sorted(set(ccys)) if c != base]
    if not want:
        return {}
    syms = ["%s%s=X" % (c, base) for c in want]
    df = yf.download(syms, start=start, interval="1d", auto_adjust=True,
                     progress=False, threads=False)
    close = df["Close"]
    out = {}
    for c, s in zip(want, syms):
        col = close[s] if s in getattr(close, "columns", []) else close
        out[c] = {d.strftime("%Y-%m-%d"): float(v)
                  for d, v in col.dropna().items()}
        log("  %s: %d daily rates" % (s, len(out[c])))
    out[base] = None                            # 1 by definition, never looked up
    return out


def rate_at(fx, ccy, day, base=BASE):
    """The rate a holding in `ccy` was worth on `day`, carried forward."""
    if ccy == base:
        return 1.0
    return mark_at(fx.get(ccy) or {}, day)


def fit_rates(days, px, ccy_of):
    """Least-squares fit of one rate per currency across every day.

    Returns ({ccy: rate}, [(day, residual_hkd, netliq)]). Uses numpy when it is
    there and falls back to solving the normal equations by hand, because the
    VM's python3.11 has numpy but a bare checkout may not.
    """
    ccys, rows, rhs, used = [], [], [], []
    for day in sorted(days):
        st = days[day]
        try:
            nl = float(st.get("netliq"))
        except (TypeError, ValueError):
            continue
        # netliq is published NET of the earmark; add it back to balance the sum
        target = nl + float(st.get("excluded_cash") or 0)
        weight = {}
        ok = True
        for p in st.get("positions") or []:
            sym, c = str(p.get("symbol")), str(p.get("ccy") or BASE)
            close = mark_at(px.get(sym), day)
            if close is None:
                ok = False                      # never priced at all: this day
                break                           # cannot balance the equation
            weight[c] = weight.get(c, 0.0) + float(p.get("qty") or 0) * close
            ccy_of[sym] = c
        if not ok:
            continue
        for c, v in (st.get("cash") or {}).items():
            weight[str(c)] = weight.get(str(c), 0.0) + float(v)
        # The base currency is 1 by definition, not something to fit. Left as a
        # free parameter it came out 0.9867 and quietly absorbed the model's
        # error into a rate that cannot be anything but 1.
        target -= weight.pop(BASE, 0.0)
        for c in weight:
            if c not in ccys:
                ccys.append(c)
        rows.append(weight)
        rhs.append(target)
        used.append(day)
    if len(rows) < len(ccys) + 2:
        raise RuntimeError("only %d usable days for %d currencies - not enough to fit"
                           % (len(rows), len(ccys)))
    A = [[r.get(c, 0.0) for c in ccys] for r in rows]
    # normal equations: (A'A) x = A'b, solved by Gauss-Jordan. Small and square.
    n = len(ccys)
    M = [[sum(A[k][i] * A[k][j] for k in range(len(A))) for j in range(n)]
         + [sum(A[k][i] * rhs[k] for k in range(len(A)))] for i in range(n)]
    for i in range(n):
        piv = max(range(i, n), key=lambda r: abs(M[r][i]))
        if abs(M[piv][i]) < 1e-9:
            raise RuntimeError("currency %s is not identifiable from the history" % ccys[i])
        M[i], M[piv] = M[piv], M[i]
        d = M[i][i]
        M[i] = [v / d for v in M[i]]
        for r in range(n):
            if r != i and M[r][i]:
                f = M[r][i]
                M[r] = [a - f * b for a, b in zip(M[r], M[i])]
    rates = {c: M[i][n] for i, c in enumerate(ccys)}
    rates[BASE] = 1.0
    resid = []
    for k, day in enumerate(used):
        est = sum(A[k][i] * rates[ccys[i]] for i in range(n))
        # rhs here is net of the base-currency leg, so compare like with like
        resid.append((day, est - rhs[k], float(days[day].get("netliq") or 0)))
    return rates, resid


def main():
    write = "--write" in sys.argv
    log("reading bot_state snapshots from git ...")
    days = daily_snapshots()
    log("  %d days: %s .. %s" % (len(days), min(days), max(days)))

    syms = set()
    for st in days.values():
        for p in st.get("positions") or []:
            syms.add(str(p.get("symbol")))
    log("fetching closes for %d symbols ever held ..." % len(syms))
    cache = os.environ.get("MPS_BACKFILL_CACHE")
    if cache:
        os.makedirs(cache, exist_ok=True)
    px = closes(syms, cache_dir=cache)
    log("  got %d" % len(px))

    ccy_of = {}
    rates, resid = fit_rates(days, px, ccy_of)
    log("fitted rates (units of %s per 1):" % BASE)
    for c, r in sorted(rates.items()):
        log("   %-4s %12.6f" % (c, r))
    worst = sorted(resid, key=lambda t: -abs(t[1]))[:5]
    rel = [abs(r) / nl for _, r, nl in resid if nl]
    log("fit residual: median %.3f%%  worst %.3f%%  (%d days balanced)"
        % (100 * sorted(rel)[len(rel) // 2], 100 * max(rel), len(rel)))
    for d, r, nl in worst:
        log("   %s  %+9.0f %s  (%.2f%%)" % (d, r, BASE, 100 * r / nl))
    if max(rel) > 0.05:
        log("!! a day is off by more than 5%% - NOT writing. The reconstruction "
            "cannot be trusted; check for a holding with no published closes.")
        return 2

    # Each day's own fit error, so the dashboard can NAME it instead of leaving
    # it inside "unexplained". Measured on 2026-09-03: the breakdown's -1,734
    # was -1,797 of rate-fit drift (09-02 -918 -> 09-03 +879) and only +63 of
    # anything real. A number the owner cannot account for is worse than a
    # bigger number that says what it is.
    # REAL daily rates for the valuation. fit_rates above is kept only as an
    # independent cross-check of the reconstruction, not as its source of truth.
    ccys = sorted({str(p.get("ccy") or BASE)
                   for st in days.values() for p in (st.get("positions") or [])}
                  | {str(c) for st in days.values() for c in (st.get("cash") or {})})
    log("fetching real daily rates for %s ..." % ", ".join(c for c in ccys if c != BASE))
    fx = fx_history(ccys, min(days))
    fit = {d: r for d, r, _ in resid}
    # ONLY days the fit could balance may be published. A day skipped in
    # fit_rates (a holding with no published close - one 404 or a renamed
    # ticker is enough) never entered the residual statistics or the 5% guard,
    # so publishing it would ship a row that nothing validated, with the
    # unpriced holding silently missing from a breakdown claiming to be whole.
    dropped = [d for d in sorted(days) if d not in fit]
    if dropped:
        log("NOT writing %d day(s) the fit could not balance: %s"
            % (len(dropped), ", ".join(dropped)))
    rows = {}
    for day in sorted(d for d in days if d in fit):
        st = days[day]
        pos, ib = {}, {}
        day_fx = {BASE: 1.0}
        for p in st.get("positions") or []:
            sym = str(p.get("symbol"))
            ibs = str(p.get("ib_symbol") or "")
            if ibs and ibs != sym:
                ib[ibs] = sym                  # fills_ledger speaks IB's ticker
            c = str(p.get("ccy") or BASE)
            close = mark_at(px.get(sym), day)
            r = rate_at(fx, c, day)
            if r is not None:
                day_fx[c] = round(r, 8)
            qty = float(p.get("qty") or 0)
            pos[sym] = [qty, None if (close is None or r is None)
                        else round(qty * close * r, 2),
                        c]                             # lets a reader rebase it
        for c in (st.get("cash") or {}):
            r = rate_at(fx, str(c), day)
            if r is not None:
                day_fx[str(c)] = round(r, 8)
        # what the day's own rates could not account for, against the real NetLiq
        # A currency with no rate must make the gap UNKNOWN, not zero. Valuing
        # it at 0 books the whole balance as a discrepancy and then hides it
        # inside a number the dashboard presents to the owner as measurement.
        unknown = [str(c) for c in (st.get("cash") or {}) if str(c) not in day_fx]
        unknown += [s for s, v in pos.items() if v[1] is None]
        gap, scale = None, None
        if unknown:
            log("  %s: not anchored (unpriced: %s)"
                % (day, ", ".join(sorted(set(unknown)))))
        else:
            cash_hkd = sum(float(v) * day_fx[str(c)]
                           for c, v in (st.get("cash") or {}).items())
            book = sum(v[1] for v in pos.values())
            gap = (book + cash_hkd) - (float(st.get("netliq") or 0)
                                       + float(st.get("excluded_cash") or 0))
            # ANCHOR THE DAY ON IB'S OWN NUMBER.
            #
            # IB never published a per-position mark for these days, and its
            # historical bars are not reachable from the publisher (the /iserver
            # endpoints need a brokerage session the publisher must not open).
            # But IB DID publish what it valued the whole account at, every day,
            # in netliq_history - and the cash balances beside it are IB's too.
            # So the only part that is ours is the split between holdings.
            #
            # Scale the holdings so the book sums to exactly what IB said the
            # account was worth. The day's TOTAL is then IB's own figure rather
            # than a sum of published closes that misses it by a few hundred
            # HKD, and the leftover the dashboard used to show as "price source"
            # disappears. What stays approximate is each holding's SHARE of the
            # book, apportioned by its published close - and the sheet says so.
            ib_book = (float(st.get("netliq") or 0)
                       + float(st.get("excluded_cash") or 0) - cash_hkd)
            if book > 0 and ib_book > 0:
                scale = ib_book / book
                # A scale far from 1 means something is wrong with the inputs,
                # not that the book really moved 20% away from IB's valuation.
                # Leave the day unanchored and loud rather than rewrite every
                # holding to chase a bad number.
                if abs(scale - 1.0) > 0.05:
                    log("  %s: NOT anchored - would need a %.1f%% rescale"
                        % (day, 100 * (scale - 1.0)))
                    scale = None
                else:
                    for s in pos:
                        pos[s][1] = round(pos[s][1] * scale, 2)
                    gap = 0.0
        rows[day] = {"ts": str(st.get("updated") or ""), "nl": round(float(st.get("netliq") or 0)),
                     "exc": round(float(st.get("excluded_cash") or 0)),
                     "fx": day_fx,
                     "pos": pos, "ib": ib,
                     **({} if scale is None else {"anchor": round(scale, 8)}),
                     "cash": {str(k): round(float(v)) for k, v in (st.get("cash") or {}).items()
                              if abs(float(v)) >= 1},
                     **({} if gap is None else {"fit": round(gap)}),
                     "src": "reconstructed"}
    log("built %d reconstructed days" % len(rows))
    if not write:
        log("dry run - pass --write to save. Sample:")
        d = max(rows)
        log(json.dumps({d: rows[d]}, indent=1)[:700])
        return 0

    path = os.path.join(ROOT, "data", "day_parts.json")
    doc = {"days": {}}
    if os.path.exists(path):
        doc = json.loads(io.open(path, encoding="utf-8").read())
    have = doc.get("days") or {}
    kept = 0
    for d, row in rows.items():
        # never overwrite a LIVE row with a reconstructed one
        if (have.get(d) or {}).get("src") == "live":
            kept += 1
            continue
        have[d] = row
    doc["days"] = have
    tmp = path + ".tmp"
    io.open(tmp, "w", encoding="utf-8").write(
        json.dumps(doc, separators=(",", ":"), sort_keys=True))
    os.replace(tmp, path)
    log("wrote %s: %d days (%d live rows left untouched)" % (path, len(have), kept))
    return 0


if __name__ == "__main__":
    sys.exit(main())
