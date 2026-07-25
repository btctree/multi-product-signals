"""Golden tests for dividend capture + UK dividend section
(run: python test_dividends.py)."""
from datetime import date
import flex_dividends
import uk_cgt

FIXTURE_XML = """<FlexQueryResponse queryName="divs" type="AF">
 <FlexStatements count="1">
  <FlexStatement accountId="U1234567" fromDate="2026-06-01" toDate="2026-07-25">
   <CashTransactions>
    <CashTransaction type="Dividends" symbol="BEN" conid="270662" currency="USD"
      amount="17.28" settleDate="2026-07-11" transactionID="9001"
      description="BEN (FRANKLIN RESOURCES) CASH DIVIDEND USD 0.32 PER SHARE" />
    <CashTransaction type="Withholding Tax" symbol="BEN" conid="270662" currency="USD"
      amount="-2.59" settleDate="2026-07-11" transactionID="9002"
      description="BEN - US TAX" />
    <CashTransaction type="Dividends" symbol="MS" conid="10963" currency="USD"
      amount="7.40" settleDate="2026-07-15" transactionID="9003"
      description="MS CASH DIVIDEND USD 0.925 PER SHARE" />
    <CashTransaction type="Withholding Tax" symbol="MS" conid="10963" currency="USD"
      amount="-1.11" settleDate="2026-07-15" transactionID="9004"
      description="MS - US TAX" />
    <CashTransaction type="Payment In Lieu Of Dividends" symbol="WEN" conid="7"
      currency="USD" amount="2.50" settleDate="2026-07-15" transactionID="9005"
      description="PIL WEN" />
    <CashTransaction type="Dividends" symbol="MC" conid="9" currency="EUR"
      amount="13.00" settleDate="2026-04-28" transactionID="9006"
      description="MC (LVMH) CASH DIVIDEND" />
    <CashTransaction type="Withholding Tax" symbol="MC" conid="9" currency="EUR"
      amount="-3.32" settleDate="2026-04-28" transactionID="9007"
      description="MC - FR TAX" />
    <CashTransaction type="BrokerInterest" symbol="" conid="0" currency="USD"
      amount="0.12" settleDate="2026-07-03" transactionID="9008"
      description="INTEREST - ignored by dividend capture" />
   </CashTransactions>
  </FlexStatement>
 </FlexStatements>
</FlexQueryResponse>"""


def test_parse():
    rows = flex_dividends.parse_cash_transactions(FIXTURE_XML)
    assert len(rows) == 7, [r["type"] for r in rows]    # interest row excluded
    kinds = sorted(r["type"] for r in rows)
    assert kinds.count("dividend") == 3 and kinds.count("wht") == 3
    assert kinds.count("pil") == 1
    ben = next(r for r in rows if r["symbol"] == "BEN" and r["type"] == "dividend")
    assert ben["amount"] == 17.28 and ben["date"] == "2026-07-11"
    assert ben["id"] == "9001"
    print("t1 flex parse OK (7 rows, interest ignored)")
    return rows


def test_section(rows):
    # stamp deterministic rates: USD .74, EUR .85; MS row rate missing
    for r in rows:
        r["gbp_rate"] = None if r["symbol"] == "MS" else (
            0.85 if r["ccy"] == "EUR" else 0.74)
    sec = uk_cgt.dividends_section(rows, today=date(2026, 7, 25))
    pays = {p["symbol"]: p for p in sec["payments"]}
    assert set(pays) == {"BEN", "MS", "WEN", "MC"}, set(pays)

    ben = pays["BEN"]      # gross 17.28*0.74=12.79; wht 2.59*0.74=1.92
    assert abs(ben["gross_gbp"] - 12.79) < 0.01 and abs(ben["wht_gbp"] - 1.92) < 0.01
    assert abs(ben["net_gbp"] - 10.87) < 0.01 and ben["wht_pct"] == 15.0
    assert not ben["estimated"]

    assert pays["MS"]["estimated"] and pays["MS"]["gross_gbp"] is None
    assert pays["WEN"]["wht_local"] == 0                      # PIL, no WHT row
    assert pays["MC"]["tax_year"] == "2026/27"                # 28 Apr -> 26/27
    assert abs(pays["MC"]["wht_pct"] - 25.5) < 0.1            # 3.32/13.00

    y = sec["years"]["2026/27"]
    assert y["payments"] == 4 and y["excluded_estimated"] == 1
    # captured gross: BEN 12.79 + WEN 1.85 + MC 11.05 = 25.69
    assert abs(y["gross_gbp"] - 25.69) < 0.02, y["gross_gbp"]
    assert y["above_allowance_gbp"] == 0.0 and y["allowance_gbp"] == 500.0
    print("t2 UK section OK: gross", y["gross_gbp"], "| wht", y["wht_gbp"],
          "| above allowance", y["above_allowance_gbp"])


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
         "amount": -1.5, "ccy": "USD", "gbp_rate": 0.75}])
    d = rep["dividends"]
    assert d["payments"][0]["net_gbp"] == 6.38 and d["allowance_gbp"] == 500.0
    print("t4 report integration OK")


if __name__ == "__main__":
    rows = test_parse()
    test_section(rows)
    test_allowance_breach()
    test_full_report_integration()
    print("ALL DIVIDEND TESTS PASS")
