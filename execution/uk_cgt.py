"""UK Capital Gains computation from the fills ledger.

Implements HMRC share identification for individuals (TCGA 1992 s105/s106A;
CG51550+; HS284): disposals match acquisitions in strict order
  1) same-day, 2) 30 days AFTER the disposal (earliest first, "bed & breakfast"),
  3) Section 104 pool at weighted-average cost — maintained in GBP, with each
acquisition entering at (cost + buy commission) converted at its OWN date's rate.
Gains are computed in GBP per-leg (never net-local-then-convert).

FX cash conversions (secType CASH) are outside CGT for individuals since
6 Apr 2012 (TCGA 1992 s252) — listed for reference only.

Ledger rows (data/fills_ledger.jsonl), one JSON object per line:
  {execId, ts:"YYYY-MM-DD HH:MM", date:"YYYY-MM-DD", symbol, con_id, sec_type,
   side:"BOT"|"SLD", qty, price, ccy, commission, commission_ccy,
   gbp_rate (GBP per 1 unit of ccy), gbp_rate_commission, source:"api"|"estimate",
   name, exchange, flags:[...]}
Rows with source=="estimate" or missing gbp_rate taint everything they touch:
those disposals are excluded from headline totals (basis_quality ESTIMATED).
"""
import json
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LEDGER = REPO / "data" / "fills_ledger.jsonl"
DIV_LEDGER = REPO / "data" / "dividends_ledger.jsonl"
REPORT = REPO / "data" / "tax_report.json"
PROVISIONAL_DAYS = 31
# Dividend income is Income Tax, not CGT: £500 allowance; above it, 2026/27
# rates are 10.75% basic / 35.75% higher / 39.35% additional (ordinary+upper
# rose 2pp at 6 Apr 2026, Autumn Budget 2025). Foreign withholding is
# creditable via FTCR capped at the LOWER of the treaty rate (US 15% with
# W-8BEN) and the UK tax actually due on that dividend — dividends inside the
# £500 allowance bear 0% UK tax, so their withholding earns no credit.
DIVIDEND_ALLOWANCE_GBP = 500.0
WHT_MATCH_DAYS = 10          # withholding true-ups book days after the payment


def tax_year_of(d):
    y = d.year if (d.month, d.day) >= (4, 6) else d.year - 1
    return f"{y}/{str(y + 1)[2:]}"


def _load_ledger():
    rows = {}
    if LEDGER.exists():
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                rows[r["execId"]] = r          # later lines win (corrections)
            except Exception:
                continue
    return list(rows.values())


def _gbp(row, field="price"):
    """GBP value of qty*price plus nothing else; None if no usable rate."""
    rate = row.get("gbp_rate")
    if not rate or rate <= 0:
        return None
    return row["qty"] * row[field] * rate


def _fee_gbp(row):
    rate = row.get("gbp_rate_commission") or row.get("gbp_rate") or 0
    com = row.get("commission") or 0
    return abs(com) * rate if rate > 0 else None


def _load_dividends():
    rows = {}
    if DIV_LEDGER.exists():
        for line in DIV_LEDGER.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                rows[r["id"]] = r              # later lines win (corrections)
            except Exception:
                continue
    return list(rows.values())


def dividends_section(div_rows, today=None):
    """Cash-transaction rows -> per-payment records + per-tax-year totals.

    UK treatment: the GROSS dividend is the taxable income (GBP at payment
    date); foreign withholding is shown for the FTCR claim, NEVER netted off
    the taxable amount. Rows missing a GBP rate are flagged estimated and
    excluded from headline totals (same policy as the CGT side).
    Robustness (from adversarial review): withholding matches its dividend
    within WHT_MATCH_DAYS, not exact-date; reversals (negative dividends) stay
    visible and net off the year; orphaned withholding still counts; PIL rows
    keep their type (substitute payments are NOT treaty dividends); undated
    rows are counted, never silently dropped."""
    undated = 0
    groups = {}                       # (symbol, date) -> dividend/pil bucket
    whts = []
    for r in div_rows:
        if not r.get("date"):
            undated += 1
            continue
        if r.get("type") == "wht":
            whts.append(r)
            continue
        if r.get("type") not in ("dividend", "pil"):
            continue
        key = (r.get("symbol"), r["date"])
        p = groups.setdefault(key, {"symbol": r.get("symbol"), "date": r["date"],
                                    "ccy": r.get("ccy"), "gross": 0.0, "wht": 0.0,
                                    "rate": None, "est": False, "types": set(),
                                    "desc": ""})
        p["gross"] += r.get("amount") or 0
        p["types"].add(r.get("type"))
        p["desc"] = p["desc"] or r.get("description", "")
        if not r.get("gbp_rate"):
            p["est"] = True
        elif not p["rate"]:
            p["rate"] = r["gbp_rate"]

    # attach each withholding row to the nearest same-symbol payment
    for w in whts:
        wd = date.fromisoformat(w["date"])
        cands = [(abs((date.fromisoformat(p["date"]) - wd).days), p)
                 for (s, _), p in groups.items() if s == w.get("symbol")]
        cands = [(gap, p) for gap, p in cands if gap <= WHT_MATCH_DAYS]
        if cands:
            p = min(cands, key=lambda c: c[0])[1]
        else:                          # orphan true-up: keep it visible
            p = groups.setdefault((w.get("symbol"), w["date"]),
                                  {"symbol": w.get("symbol"), "date": w["date"],
                                   "ccy": w.get("ccy"), "gross": 0.0, "wht": 0.0,
                                   "rate": None, "est": False,
                                   "types": {"wht_adjustment"},
                                   "desc": w.get("description", "")})
        p["wht"] += -(w.get("amount") or 0)      # IB books WHT negative
        if not w.get("gbp_rate"):
            p["est"] = True
        elif not p["rate"]:
            p["rate"] = w["gbp_rate"]

    payments, years = [], {}
    for (_, d), p in sorted(groups.items(), key=lambda kv: kv[0][1]):
        if p["gross"] == 0 and p["wht"] == 0:
            continue
        dd = date.fromisoformat(d)
        ty = tax_year_of(dd)
        rate = p["rate"] or 0
        est = p["est"] or rate <= 0
        # estimated payments carry NO GBP figures at all — a half-stamped
        # payment must not look half-usable in the UI or CSV
        gross_gbp = round(p["gross"] * rate, 2) if not est else None
        wht_gbp = round(p["wht"] * rate, 2) if not est else None
        types = sorted(p["types"])
        ptype = types[0] if len(types) == 1 else "mixed"
        payments.append({
            "date": d, "tax_year": ty, "symbol": p["symbol"], "ccy": p["ccy"],
            "type": ptype,
            "gross_local": round(p["gross"], 2), "wht_local": round(p["wht"], 2),
            "net_local": round(p["gross"] - p["wht"], 2),
            "gbp_rate": (rate or None) if not est else None,
            "gross_gbp": gross_gbp, "wht_gbp": wht_gbp,
            "net_gbp": round(gross_gbp - wht_gbp, 2) if gross_gbp is not None else None,
            "wht_pct": round(100 * p["wht"] / p["gross"], 1) if p["gross"] else 0,
            "estimated": est, "description": p["desc"],
        })
        y = years.setdefault(ty, {"payments": 0, "gross_gbp": 0.0, "wht_gbp": 0.0,
                                  "net_gbp": 0.0, "excluded_estimated": 0,
                                  "has_pil": False})
        y["payments"] += 1
        y["has_pil"] = y["has_pil"] or ptype in ("pil", "mixed")
        if est:
            y["excluded_estimated"] += 1
        else:
            y["gross_gbp"] += gross_gbp          # reversals net off (negative)
            y["wht_gbp"] += wht_gbp or 0
            y["net_gbp"] += gross_gbp - (wht_gbp or 0)
    for y in years.values():
        for k in ("gross_gbp", "wht_gbp", "net_gbp"):
            y[k] = round(y[k], 2)
        y["allowance_gbp"] = DIVIDEND_ALLOWANCE_GBP
        y["above_allowance_gbp"] = round(
            max(0.0, y["gross_gbp"] - DIVIDEND_ALLOWANCE_GBP), 2)
    return {"payments": payments[::-1], "years": years,
            "allowance_gbp": DIVIDEND_ALLOWANCE_GBP, "undated_rows": undated}


def _merge_same_day(disps):
    """One disposal per security per day - TCGA 1992 s105(1)(a), CG51560.

    All shares of one class disposed of by the same person on the same day are
    a single disposal. IB routinely splits one order into several fills - CRL's
    8-share sell on 2026-09-11 arrived as 7 + 1 - and reporting each fill as
    its own disposal overstated the SA108 disposal count.

    Merging the finished rows is exactly equivalent to matching the aggregate:
    same-day and 30-day matches consume acquisitions in the same order either
    way, and consecutive Section 104 draws on one date leave the pool's average
    cost unchanged, so the summed cost is the aggregate's cost.
    """
    merged, order = {}, []
    for d in disps:
        k = (d["symbol"], d["date"])
        if k not in merged:
            merged[k] = dict(d, slices=list(d["slices"]), fills=1)
            order.append(k)
            continue
        m = merged[k]
        m["fills"] += 1
        m["qty"] += d["qty"]
        m["proceeds_local"] = round(m["proceeds_local"] + d["proceeds_local"], 2)
        for f in ("proceeds_gbp", "sell_fee_gbp", "gain_gbp"):
            m[f] = (round(m[f] + d[f], 2)
                    if m[f] is not None and d[f] is not None else None)
        m["cost_gbp"] = round(m["cost_gbp"] + d["cost_gbp"], 2)
        m["rules"] = sorted(set(m["rules"]) | set(d["rules"]))
        m["slices"] += d["slices"]
        if d["basis_quality"] == "ESTIMATED":
            m["basis_quality"] = "ESTIMATED"
        m["proceeds_estimated"] = m["proceeds_estimated"] or d["proceeds_estimated"]
        m["proceeds_unvalued"] = m["proceeds_unvalued"] or d["proceeds_unvalued"]
        m["cost_unknown"] = m["cost_unknown"] or d["cost_unknown"]
        if d.get("gbp_rate") != m.get("gbp_rate"):
            m["_rate_mixed"] = True
    out = []
    for k in order:
        m = merged[k]
        mixed = m.pop("_rate_mixed", False)
        # An unvalued disposal has no GBP rate. Leaving the first fill's rate on
        # it put 'GBP @ 0.75' beside 'NO GBP RATE', and which rate survived
        # depended on the order the fills arrived in.
        if m["proceeds_unvalued"]:
            m["gbp_rate"] = None
        if m["fills"] > 1:
            if m["qty"]:
                m["price_local"] = round(m["proceeds_local"] / m["qty"], 4)
            if mixed and m["proceeds_gbp"] and m["proceeds_local"]:
                m["gbp_rate"] = m["proceeds_gbp"] / m["proceeds_local"]
        out.append(m)
    return out


def compute(rows=None, today=None, div_rows=None):
    """Pure computation: ledger rows -> report dict."""
    rows = _load_ledger() if rows is None else rows
    div_rows = _load_dividends() if div_rows is None else div_rows
    today = today or date.today()
    stocks = [r for r in rows if r.get("sec_type") != "CASH"]
    fx = [r for r in rows if r.get("sec_type") == "CASH"]

    by_sym = {}
    for r in stocks:
        by_sym.setdefault(r.get("symbol"), []).append(r)

    disposals_out, open_out = [], []

    for sym, rs in sorted(by_sym.items()):
        acqs = sorted([dict(r, remaining=r["qty"]) for r in rs if r["side"] == "BOT"],
                      key=lambda r: (r["date"], r.get("ts", "")))
        disps = sorted([dict(r, remaining=r["qty"]) for r in rs if r["side"] == "SLD"],
                       key=lambda r: (r["date"], r.get("ts", "")))
        matches = {id(d): [] for d in disps}

        def take(d, a, rule):
            q = min(d["remaining"], a["remaining"])
            if q <= 0:
                return
            d["remaining"] -= q
            a["remaining"] -= q
            rate = a.get("gbp_rate") or 0
            fee = _fee_gbp(a)
            cost = (q * a["price"] * rate + (fee or 0) * q / a["qty"]) if rate > 0 else None
            est = a.get("source") == "estimate" or cost is None
            matches[id(d)].append({"rule": rule, "qty": q, "acq_date": a["date"],
                                   "cost_gbp": cost, "estimated": est, "cost_unknown": cost is None,
                                   "acq_price": a["price"], "acq_rate": rate or None})

        # 1) same-day for EVERY disposal, then 2) 30-day after (earliest first).
        # Two passes, not one. s106A(9) makes the 30-day rule "subject to" the
        # same-day rule in s105(1), so an acquisition on the same day as a LATER
        # disposal belongs to that disposal first. The single pass let an earlier
        # disposal's 30-day search take it: sell on 07-01, buy + sell on 07-05
        # gave 07-01 the 30-day match and pushed 07-05 onto the pool - the right
        # year total within one tax year, but across 5 April it moves gain into
        # the wrong year. Board review 2026-09-11; pre-existing.
        for d in disps:
            for a in acqs:
                if a["date"] == d["date"]:
                    take(d, a, "same_day")
        for d in disps:
            dd = date.fromisoformat(d["date"])
            for a in acqs:
                ad = date.fromisoformat(a["date"])
                if dd < ad <= dd + timedelta(days=30):
                    take(d, a, "30_day")

        # 3) chronological S104 pool over remaining quantities
        events = ([("A", date.fromisoformat(a["date"]), a) for a in acqs] +
                  [("D", date.fromisoformat(d["date"]), d) for d in disps])
        events.sort(key=lambda e: (e[1], 0 if e[0] == "A" else 1))
        pool_qty, pool_cost, pool_est, pool_unknown = 0.0, 0.0, False, False
        for kind, dt_, r in events:
            if kind == "A" and r["remaining"] > 0:
                rate = r.get("gbp_rate") or 0
                fee = _fee_gbp(r)
                if rate > 0 and fee is not None:
                    add_cost = r["remaining"] * r["price"] * rate + fee * r["remaining"] / r["qty"]
                else:
                    add_cost = 0.0
                    pool_est = True
                    pool_unknown = True      # no GBP rate: this lot's cost is UNKNOWN, not estimated
                if r.get("source") == "estimate":
                    pool_est = True
                pool_qty += r["remaining"]
                pool_cost += add_cost
                r["remaining"] = 0
            elif kind == "D" and r["remaining"] > 0:
                q = min(r["remaining"], pool_qty)
                if q > 0:
                    avg = pool_cost / pool_qty if pool_qty else 0.0
                    matches[id(r)].append({"rule": "s104_pool", "qty": q,
                                           "acq_date": "S104 pool",
                                           "cost_gbp": q * avg,
                                           "estimated": pool_est, "cost_unknown": pool_unknown,
                                           "acq_price": None, "acq_rate": None})
                    pool_qty -= q
                    pool_cost -= q * avg
                    if pool_qty < 1e-9:
                        # The pool is empty, so whatever tainted it has been sold. A later lot
                        # must start a clean pool: otherwise one rate-less or seeded purchase
                        # marks every future sale of the symbol, and fully valued gains fall
                        # out of the SA108 test. (pool_est never reset either - pre-existing.)
                        pool_qty, pool_cost, pool_est, pool_unknown = 0.0, 0.0, False, False
                    r["remaining"] -= q
                if r["remaining"] > 0:          # nothing left to match against
                    matches[id(r)].append({"rule": "unmatched", "qty": r["remaining"],
                                           "acq_date": "unknown", "cost_gbp": None, "cost_unknown": True,
                                           "estimated": True,
                                           "acq_price": None, "acq_rate": None})
                    r["remaining"] = 0

        for d in disps:
            dd = date.fromisoformat(d["date"])
            slices = matches[id(d)]
            rate = d.get("gbp_rate") or 0
            proceeds = d["qty"] * d["price"] * rate if rate > 0 else None
            sell_fee = _fee_gbp(d)
            est = (d.get("source") == "estimate" or proceeds is None or sell_fee is None
                   or any(s["estimated"] for s in slices))
            cost = sum(s["cost_gbp"] or 0 for s in slices)
            cost_unknown = any(sl.get("cost_unknown") for sl in slices)
            # An unknown cost is not a zero cost. Proceeds minus a £0 cost printed the
            # whole sale as gain on the disposal's own row (SNOW with a rate-less buy:
            # '+£1,683.55') even while the year totals kept it out. The gain is unknown.
            gain = ((proceeds - (sell_fee or 0) - cost)
                    if (proceeds is not None and not cost_unknown) else None)
            disposals_out.append({
                "symbol": sym, "name": d.get("name", ""), "ccy": d.get("ccy"),
                "date": d["date"], "tax_year": tax_year_of(dd), "qty": d["qty"],
                "price_local": d["price"], "proceeds_local": round(d["qty"] * d["price"], 2),
                "gbp_rate": rate or None,
                "proceeds_gbp": round(proceeds, 2) if proceeds is not None else None,
                "sell_fee_gbp": round(sell_fee, 2) if sell_fee is not None else None,
                "cost_gbp": round(cost, 2),
                "gain_gbp": round(gain, 2) if gain is not None else None,
                "rules": sorted({s["rule"] for s in slices}),
                "slices": [{k: (round(v, 2) if isinstance(v, float) else v)
                            for k, v in s.items()} for s in slices],
                "basis_quality": "ESTIMATED" if est else "captured",
                # Distinct from basis_quality: most estimated disposals are real sales
                # whose COST was seeded. Only a pre-coverage seed SELL row has estimated
                # proceeds too - 3 of the 11 estimated 2026/27 disposals.
                "proceeds_estimated": d.get("source") == "estimate",
                # No GBP rate at all: the sale's GBP value is UNKNOWN, not estimated,
                # and it cannot be in any GBP total. Kept separate so the page can say
                # the SA108 figures exclude it rather than imply it is in them.
                "proceeds_unvalued": proceeds is None,
                # Some slice's cost is UNKNOWN - a purchase with no GBP rate, or more
                # sold than the ledger holds. Its cost would otherwise sum as £0 and the
                # whole proceeds would read as gain, which must not reach the SA108 test.
                "cost_unknown": cost_unknown,
                "provisional_until": (dd + timedelta(days=PROVISIONAL_DAYS)).isoformat()
                    if (today - dd).days < PROVISIONAL_DAYS else None,
            })

        if pool_qty > 0.0001:
            open_out.append({"symbol": sym, "qty": round(pool_qty, 4),
                             "cost_gbp": round(pool_cost, 2),
                             "estimated": pool_est})

    disposals_out = _merge_same_day(disposals_out)

    # ---- per-tax-year summaries ----
    # Two sets of totals. The original fields stay captured-basis only,
    # exactly as before. The all_* fields count every disposal, because HMRC's
    # SA108 triggers do: disposals WORTH more than £50,000, or chargeable gains
    # BEFORE losses above the annual exempt amount (SA108 notes 2025-26). On
    # 2026/27 the captured-only figures showed proceeds £11,195 against a true
    # £26,116, and a net GAIN of £262 against a best-estimate net LOSS of
    # £1,048 - the seed-basis disposals carry the year's largest losses.
    years = {}
    for d in disposals_out:
        y = years.setdefault(d["tax_year"], {"disposals": 0, "proceeds_gbp": 0.0,
                                             "gains_gbp": 0.0, "losses_gbp": 0.0,
                                             "excluded_estimated": 0,
                                             "excluded_est_net_gbp": 0.0,
                                             "all_proceeds_gbp": 0.0,
                                             "all_gains_gbp": 0.0,
                                             "all_losses_gbp": 0.0,
                                             "proceeds_estimated": 0,
                                             "proceeds_unvalued": 0,
                                             "cost_unknown": 0,
                                             "in_gains": 0,
                                             "est_in_gains": 0,
                                             "est_in_gains_net_gbp": 0.0})
        y["disposals"] += 1
        if d["proceeds_gbp"] is not None:
            y["all_proceeds_gbp"] += d["proceeds_gbp"]
        if d.get("proceeds_estimated"):
            y["proceeds_estimated"] += 1
        if d.get("proceeds_unvalued"):
            y["proceeds_unvalued"] += 1
        # Only sales whose proceeds ARE valued: the page's note says 'the proceeds
        # still count', which is false for a sale that also has no GBP rate - and
        # that one is already reported by the unvalued note.
        if d.get("cost_unknown") and not d.get("proceeds_unvalued"):
            y["cost_unknown"] += 1
        # the page must never derive these by subtraction: an unvalued or
        # unknown-cost sale is in NO gains figure, and saying so needs the
        # exact set that IS in them
        if d["gain_gbp"] is not None and not d.get("cost_unknown"):
            y["in_gains"] += 1
            if d["basis_quality"] == "ESTIMATED":
                y["est_in_gains"] += 1
                y["est_in_gains_net_gbp"] += d["gain_gbp"]
            if d["gain_gbp"] >= 0:
                y["all_gains_gbp"] += d["gain_gbp"]
            else:
                y["all_losses_gbp"] += -d["gain_gbp"]
        if d["basis_quality"] == "captured" and d["gain_gbp"] is not None:
            y["proceeds_gbp"] += d["proceeds_gbp"]
            if d["gain_gbp"] >= 0:
                y["gains_gbp"] += d["gain_gbp"]
            else:
                y["losses_gbp"] += -d["gain_gbp"]
        else:
            y["excluded_estimated"] += 1
            if d["gain_gbp"] is not None:
                y["excluded_est_net_gbp"] += d["gain_gbp"]
    for y in years.values():
        for k in ("proceeds_gbp", "gains_gbp", "losses_gbp", "excluded_est_net_gbp",
                  "all_proceeds_gbp", "all_gains_gbp", "all_losses_gbp",
                  "est_in_gains_net_gbp"):
            y[k] = round(y[k], 2)
        y["net_gain_gbp"] = round(y["gains_gbp"] - y["losses_gbp"], 2)
        y["all_net_gain_gbp"] = round(y["all_gains_gbp"] - y["all_losses_gbp"], 2)

    fx_out = [{"date": r["date"], "symbol": r.get("symbol"), "qty": r["qty"],
               "price": r["price"], "ccy": r.get("ccy"),
               "gbp_value": round(_gbp(r), 2) if _gbp(r) else None,
               "side": r["side"]} for r in sorted(fx, key=lambda r: r["date"])]

    cov = min((r["date"] for r in rows if r.get("source") != "estimate"), default=None)
    return {"generated": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
            "coverage_start": cov, "years": years, "disposals": disposals_out,
            "open_positions": open_out, "fx_conversions": fx_out,
            "dividends": dividends_section(div_rows, today)}


def build_report():
    rep = compute()
    REPORT.write_text(json.dumps(rep, indent=1), encoding="utf-8")
    return rep


if __name__ == "__main__":
    r = build_report()
    print("tax_report.json:", len(r["disposals"]), "disposals,",
          len(r["open_positions"]), "open,", len(r["fx_conversions"]), "fx rows")
