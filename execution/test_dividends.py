"""Golden tests for dividend capture + UK dividend section
(run: python test_dividends.py). Covers the adversarial-review fixes:
yyyyMMdd dates, summary-row skip, id collisions, date-shifted withholding,
reversal netting, orphaned withholding, PIL typing, estimated=all-null."""
from datetime import date
import flex_dividends
import uk_cgt

FIXTURE_XML = """<FlexQueryResponse queryName="divs" type="AF">
 <FlexStatements count="1">
  <FlexStatement accountId="U1234567" fromDate="20260601" toDate="20260725">
   <CashTransactions>
    <CashTransaction type="Dividends" symbol="BEN" conid="270662" currency="USD"
      amount="17.28" settleDate="20260711" transactionID="9001"
      levelOfDetail="DETAIL" description="BEN CASH DIVIDEND USD 0.32/SH" />
    <CashTransaction type="Dividends" symbol="BEN" conid="270662" currency="USD"
      amount="17.28" settleDate="20260711" transactionID="9001S"
      levelOfDetail="SUMMARY" description="summary duplicate - must be skipped" />
    <CashTransaction type="Withholding Tax" symbol="BEN" conid="270662" currency="USD"
      amount="-2.59" settleDate="20260711" transactionID="9002" description="BEN US TAX" />
    <CashTransaction type="Dividends" symbol="MS" conid="10963" currency="USD"
      amount="7.40" settleDate="2026-07-15" transactionID="9003" description="MS DIV" />
    <CashTransaction type="Withholding Tax" symbol="MS" conid="10963" currency="USD"
      amount="-1.11" settleDate="2026-07-16" transactionID="9004"
      description="MS US TAX (books a day late)" />
    <CashTransaction type="Payment In Lieu Of Dividends" symbol="WEN" conid="7"
      currency="USD" amount="2.50" settleDate="2026/07/15" transactionID="9005"
      description="PIL WEN" />
    <CashTransaction type="Dividends" symbol="MC" conid="9" currency="EUR"
      amount="13.00" settleDate="20260428" transactionID="9006" description="LVMH DIV" />
    <CashTransaction type="Withholding Tax" symbol="MC" conid="9" currency="EUR"
      amount="-3.32" settleDate="20260428" transactionID="9007" description="MC FR TAX" />
    <CashTransaction type="Dividends" symbol="BEN" conid="270662" currency="USD"
      amount="-17.28" settleDate="20260718" transactionID="9008"
      description="BEN REVERSAL (rebooked)" />
    <CashTransaction type="Withholding Tax" symbol="URI" conid="15" currency="USD"
      amount="-0.30" settleDate="20260630" transactionID="9009"
      description="URI WHT TRUE-UP (no dividend nearby)" />
    <CashTransaction type="Dividends" symbol="ZZ" conid="99" currency="USD"
      amount="1.00" dateTime="20260702;093000" description="partial credit A" />
    <CashTransaction type="Dividends" symbol="ZZ" conid="99" currency="USD"
      amount="1.00" dateTime="20260702;093000" description="partial credit B" />
    <CashTransaction type="BrokerInterest" symbol="" conid="0" currency="USD"
      amount="0.12" settleDate="20260703" transactionID="9010" description="ignored" />
   </CashTransactions>
  </FlexStatement>
 </FlexStatements>
</FlexQueryResponse>"""


def test_parse():
    rows = flex_dividends.parse_cash_transactions(FIXTURE_XML)
    assert len(rows) == 11, len(rows)         # summary + interest excluded
    ben = next(r for r in rows if r["id"] == "9001")
    assert ben["date"] == "2026-07-11", ben["date"]          # yyyyMMdd
    zz = [r for r in rows if r["symbol"] == "ZZ"]
    assert zz[0]["date"] == "2026-07-02"                     # dateTime;HHmmss
    assert zz[0]["id"] != zz[1]["id"]                        # collision suffix
    assert not any("summary" in r["description"] for r in rows)
    print("t1 parse OK: dates normalized, summary skipped, ids unique")
    return rows


def test_section(rows):
    for r in rows:                     # deterministic rates; MS legs missing
        r["gbp_rate"] = None if r["symbol"] == "MS" else (
            0.85 if r["ccy"] == "EUR" else 0.74)
    sec = uk_cgt.dividends_section(rows, today=date(2026, 7, 25))
    pays = {(p["symbol"], p["date"]): p for p in sec["payments"]}
    assert len(sec["payments"]) == 7, [p['symbol'] for p in sec['payments']]

    ben = pays[("BEN", "2026-07-11")]
    assert abs(ben["gross_gbp"] - 12.79) < 0.01 and abs(ben["wht_gbp"] - 1.92) < 0.01
    assert ben["wht_pct"] == 15.0 and ben["type"] == "dividend"

    rev = pays[("BEN", "2026-07-18")]                 # reversal stays visible
    assert rev["gross_local"] == -17.28 and rev["gross_gbp"] < 0

    ms = pays[("MS", "2026-07-15")]                   # date-shifted WHT attached
    assert ms["wht_local"] == 1.11
    assert ms["estimated"] and ms["gross_gbp"] is None and ms["wht_gbp"] is None

    assert pays[("WEN", "2026-07-15")]["type"] == "pil"
    uri = pays[("URI", "2026-06-30")]                 # orphan WHT kept
    assert uri["type"] == "wht_adjustment" and uri["wht_local"] == 0.30
    assert pays[("ZZ", "2026-07-02")]["gross_local"] == 2.00   # both partials

    y = sec["years"]["2026/27"]
    # gross: BEN 12.79 - 12.79(rev) + WEN 1.85 + MC 11.05 + ZZ 1.48 = 14.38
    assert abs(y["gross_gbp"] - 14.38) < 0.02, y["gross_gbp"]
    # wht: BEN 1.92 + MC 2.82 + URI 0.22 = 4.96
    assert abs(y["wht_gbp"] - 4.96) < 0.02, y["wht_gbp"]
    assert y["excluded_estimated"] == 1 and y["has_pil"] is True
    assert y["above_allowance_gbp"] == 0.0 and sec["undated_rows"] == 0
    print("t2 UK section OK: gross", y["gross_gbp"], "| wht", y["wht_gbp"],
          "| reversal netted, orphan WHT kept, PIL typed")


def test_allowance_breach():
    rows = [{"id": str(i), "symbol": "BIG", "date": f"2026-05-{10+i:02d}",
             "type": "dividend", "amount": 200.0, "ccy": "GBP", "gbp_rate": 1.0}
            for i in range(4)]
    sec = uk_cgt.dividends_section(rows)
    y = sec["years"]["2026/27"]
    assert y["gross_gbp"] == 800.0 and y["above_allowance_gbp"] == 300.0
    print("t3 allowance breach OK: 800 gross -> 300 UK-taxable above £500")


def test_full_report_integration():
    rep = uk_cgt.compute(rows=[], div_rows=[
        {"id": "x", "symbol": "T", "date": "2026-07-01", "type": "dividend",
         "amount": 10.0, "ccy": "USD", "gbp_rate": 0.75},
        {"id": "y", "symbol": "T", "date": "2026-07-01", "type": "wht",
         "amount": -1.5, "ccy": "USD", "gbp_rate": 0.75},
        {"id": "z", "symbol": "T", "date": None, "type": "dividend",
         "amount": 5.0, "ccy": "USD", "gbp_rate": 0.75}])
    d = rep["dividends"]
    assert d["payments"][0]["net_gbp"] == 6.38 and d["allowance_gbp"] == 500.0
    assert d["undated_rows"] == 1                     # counted, not silent
    print("t4 report integration OK (undated row counted)")


if __name__ == "__main__":
    rows = test_parse()
    test_section(rows)
    test_allowance_breach()
    test_full_report_integration()
    print("ALL DIVIDEND TESTS PASS")
