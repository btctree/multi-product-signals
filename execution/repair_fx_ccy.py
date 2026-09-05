"""One-off repair: re-currency the FX rows that were booked in the wrong currency.

WHY THIS EXISTS
    /iserver/account/trades reports IDEALPRO fills with no `currency` field.
    IDEALPRO is (correctly) absent from broker._EXCH_CCY, so the fill's currency
    landed empty and fills_capture's `or "USD"` guessed USD. An FX fill's price
    and commission are denominated in the pair's QUOTE currency, so:

        USD 10.0 @ 156.215  ->  booked ccy USD, not JPY
        USD 1.45 @ 7.83983  ->  booked ccy USD, not HKD
        GBP 1750 @ 10.61405 ->  booked ccy USD, not HKD

    1,562 JPY valued as 1,562 USD is how a ten-dollar conversion reached the CGT
    report as gbp_value 1,155.37. broker.py now resolves the pair from the conid,
    so this cannot recur; those already-written rows still need correcting.

WHAT IT DOES NOT DO
    It does NOT invent a GBP rate. The stored rate was fetched for the WRONG
    currency, and today's rate is not the rate that applied on the fill date, so
    the row is marked rate_missing - a state uk_cgt.py already understands and
    surfaces - rather than quietly revalued. Sourcing historical rates is a
    separate decision.

Idempotent: rows already carrying ccy_repaired are left alone. Run on the VM,
where an IB session exists:  python execution/repair_fx_ccy.py [--apply]
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(os.path.dirname(HERE), "data", "fills_ledger.jsonl")


def main(apply_changes):
    sys.path.insert(0, HERE)
    import ib_orders

    rows, changed, cash_rows, resolved = [], 0, 0, 0
    with open(LEDGER, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if (str(r.get("sec_type") or "").upper() == "CASH"
                    and "ccy_repaired" not in (r.get("flags") or [])):
                cash_rows += 1
                quote = ib_orders.fx_quote_ccy(r.get("symbol"), r.get("con_id"))
                if quote:
                    resolved += 1
                if quote and quote != r.get("ccy"):
                    print("  %s %s %s @ %s : ccy %s -> %s"
                          % (r.get("date"), r.get("side"), r.get("symbol"),
                             r.get("price"), r.get("ccy"), quote))
                    r["ccy"] = quote
                    r["commission_ccy"] = quote
                    # The stored rate was fetched for the wrong currency. Drop
                    # it rather than revalue at a rate that did not apply then.
                    r["gbp_rate"] = None
                    r["gbp_rate_commission"] = None
                    flags = [f for f in (r.get("flags") or []) if f != "rate_missing"]
                    r["flags"] = flags + ["rate_missing", "ccy_repaired"]
                    changed += 1
            rows.append(r)

    if cash_rows and not resolved:
        # fx_quote_ccy swallows every error and returns "", so a dead IB
        # session looks identical to a clean ledger: nothing resolves,
        # nothing changes, exit 0. Refuse to report success we cannot
        # stand behind.
        raise SystemExit(
            "could not resolve ANY of the %d FX row(s) - the IB session is "
            "probably down. Refusing to report a clean ledger; fix the "
            "session and re-run." % cash_rows)
    print("\n%d of %d FX row(s) resolved; %d need repair out of %d total"
          % (resolved, cash_rows, changed, len(rows)))
    if not changed:
        return
    if not apply_changes:
        print("dry run - re-run with --apply to write")
        return
    tmp = LEDGER + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    os.replace(tmp, LEDGER)          # atomic: never leave a half-written ledger
    print("written to %s" % LEDGER)


if __name__ == "__main__":
    main("--apply" in sys.argv)
