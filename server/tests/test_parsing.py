from datetime import date

import pytest

from sillage.config import ConfigError, load_settings
from sillage.ingest import congress, sec
from sillage import scoring

ATOM = """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Latest Filings</title>
<entry>
<title>4 - Doe John (0001111111) (Reporting)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/1111111/000111111125000001/0001111111-25-000001-index.htm"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2025-03-04 &lt;b&gt;AccNo:&lt;/b&gt; 0001111111-25-000001 &lt;b&gt;Size:&lt;/b&gt; 5 KB</summary>
<updated>2025-03-04T16:30:00-05:00</updated>
<category scheme="https://www.sec.gov/" label="form type" term="4"/>
<id>urn:tag:sec.gov,2008:accession-number=0001111111-25-000001</id>
</entry>
<entry>
<title>4 - ACME CORP (0000222222) (Issuer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/222222/000111111125000001/0001111111-25-000001-index.htm"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2025-03-04 &lt;b&gt;AccNo:&lt;/b&gt; 0001111111-25-000001</summary>
<category scheme="https://www.sec.gov/" label="form type" term="4"/>
<id>urn:tag:sec.gov,2008:accession-number=0001111111-25-000001</id>
</entry>
<entry>
<title>424B2 - BIG BANK (0000333333) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/333333/000033333325000009/0000333333-25-000009-index.htm"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2025-03-04</summary>
<category scheme="https://www.sec.gov/" label="form type" term="424B2"/>
<id>urn:tag:sec.gov,2008:accession-number=0000333333-25-000009</id>
</entry>
<entry>
<title>SCHEDULE 13D/A - Target Inc (0000444444) (Subject)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/444444/000555555525000002/0000555555-25-000002-index.htm"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2025-03-05</summary>
<category scheme="https://www.sec.gov/" label="form type" term="SCHEDULE 13D/A"/>
<id>urn:tag:sec.gov,2008:accession-number=0000555555-25-000002</id>
</entry>
<entry>
<title>SCHEDULE 13D/A - Activist Fund LP (0000555555) (Filed by)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/555555/000555555525000002/0000555555-25-000002-index.htm"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2025-03-05</summary>
<category scheme="https://www.sec.gov/" label="form type" term="SCHEDULE 13D/A"/>
<id>urn:tag:sec.gov,2008:accession-number=0000555555-25-000002</id>
</entry>
</feed>"""


def test_parse_atom_filters_and_pairs():
    entries = sec.parse_atom(ATOM)
    assert len(entries) == 5
    form4 = [e for e in entries if e.form == "4"]
    assert {e.accession for e in form4} == {"0001111111-25-000001"}  # doublon Reporting/Issuer
    first = form4[0]
    assert first.name == "Doe John" and first.cik == 1111111 and first.role == "Reporting"
    assert first.filed == date(2025, 3, 4)
    assert sec.txt_url_from_index(first.link).endswith("/000111111125000001/0001111111-25-000001.txt")
    assert not any(e.form == "4" for e in entries if e.accession.startswith("0000333333"))

    pairs = sec.pair_ownership_entries(entries)
    assert len(pairs) == 1
    p = pairs[0]
    assert p["form_type"] == "13D/A"
    assert p["subject_name"] == "Target Inc" and p["subject_cik"] == 444444
    assert p["filer_name"] == "Activist Fund LP"


IDX = """Description:           Daily Index of EDGAR Dissemination Feed by Form Type
Last Data Received:    March 4, 2025

Form Type   Company Name                                                  CIK         Date Filed  File Name
---------------------------------------------------------------------------------------------------------------------------------------------
3           SOME CO                                                       1234567     20250304    edgar/data/1234567/0001234567-25-000010.txt
4           ACME CORP                                                     222222      20250304    edgar/data/222222/0001111111-25-000001.txt
4           Doe John                                                      1111111     20250304    edgar/data/1111111/0001111111-25-000001.txt
4/A         OTHER CO                                                      7654321     20250304    edgar/data/7654321/0007654321-25-000003.txt
"""


def test_parse_daily_index():
    rows = sec.parse_daily_index(IDX)
    assert len(rows) == 4
    f4 = [r for r in rows if r.form == "4"]
    assert len(f4) == 2
    assert f4[0].accession == "0001111111-25-000001"
    assert f4[0].filed == date(2025, 3, 4) and f4[0].cik == 222222
    assert [r.form for r in rows] == ["3", "4", "4", "4/A"]
    days = sec.business_days_back(date(2025, 3, 10), 3)  # lundi
    assert days == [date(2025, 3, 7), date(2025, 3, 6), date(2025, 3, 5)]


FORM4_TXT = """<SEC-DOCUMENT>0001111111-25-000001.txt : 20250304
<SEC-HEADER>
FILED AS OF DATE:		20250304
</SEC-HEADER>
<DOCUMENT>
<TYPE>4
<TEXT>
<XML>
<?xml version="1.0"?>
<ownershipDocument>
  <schemaVersion>X0508</schemaVersion>
  <documentType>4</documentType>
  <issuer>
    <issuerCik>0000222222</issuerCik>
    <issuerName>ACME CORP</issuerName>
    <issuerTradingSymbol>acme</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerCik>0001111111</rptOwnerCik><rptOwnerName>Doe John</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>1</isDirector><isOfficer>true</isOfficer><isTenPercentOwner>0</isTenPercentOwner>
      <officerTitle>Chief Executive Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <reportingOwner>
    <reportingOwnerId><rptOwnerCik>0001111112</rptOwnerCik><rptOwnerName>Doe Trust</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isTenPercentOwner>1</isTenPercentOwner></reportingOwnerRelationship>
  </reportingOwner>
  <aff10b5One>0</aff10b5One>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2025-03-03</value></transactionDate>
      <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10000</value></transactionShares>
        <transactionPricePerShare><value>25.50</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>50000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2025-03-03</value></transactionDate>
      <transactionCoding><transactionCode>G</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>5</value></transactionShares></transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2025-03-03</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>100</value></transactionShares>
        <transactionPricePerShare><value>26</value><footnoteId id="F1"/></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
  <derivativeTable>
    <derivativeTransaction>
      <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
    </derivativeTransaction>
  </derivativeTable>
  <footnotes><footnote id="F1">Sold pursuant to a Rule 10b5-1 trading plan adopted on May 1.</footnote></footnotes>
</ownershipDocument>
</XML>
</TEXT>
</DOCUMENT>
"""


def test_parse_form4():
    xml = sec.extract_ownership_xml(FORM4_TXT)
    assert xml is not None
    assert sec.filed_date_from_txt(FORM4_TXT) == date(2025, 3, 4)
    f = sec.parse_form4(xml)
    assert f.ticker == "ACME" and f.issuer_cik == 222222 and f.issuer_name == "ACME CORP"
    assert f.owner_name == "Doe John et al."
    assert f.is_director and f.is_officer and f.is_ten_percent
    assert f.officer_title == "Chief Executive Officer"
    assert f.plan_10b5_1  # via la note de bas de page
    assert [t["code"] for t in f.trades] == ["P", "S"]  # G ignoré, table dérivée ignorée
    p = f.trades[0]
    assert p["line_no"] == 1 and p["value"] == 255000.0 and p["acquired"] is True
    assert p["trade_date"] == date(2025, 3, 3) and p["shares_after"] == 50000
    assert f.trades[1]["line_no"] == 3


def test_insider_score():
    score, reasons = scoring.insider_score(
        code="P", officer_title="CEO", is_officer=True, is_director=False, is_ten_percent=False,
        value=1_500_000, delay_days=1, cluster=3, plan_10b5_1=False, price_change=0.02,
    )
    assert score == 40 + 15 + 15 + 10 + 15 == 95
    assert any("premier plan" in r for r in reasons)
    score, _ = scoring.insider_score(
        code="S", officer_title=None, is_officer=False, is_director=True, is_ten_percent=False,
        value=60_000, delay_days=1, cluster=0, plan_10b5_1=True, price_change=0.2,
    )
    assert score == max(0, 12 + 8 + 5 - 20 - 10)  # pas de bonus de délai pour une vente


def test_congress_and_convergence_scores():
    score, _ = scoring.congress_score(side="buy", amount_min=50_001, delay_days=10, cluster=2)
    assert score == 35 + 10 + 15 + 8
    score, reasons = scoring.congress_score(side="sell", amount_min=1_001, delay_days=40, cluster=0)
    assert score == 5 and any("tard" in r for r in reasons)
    assert scoring.convergence_score(insiders=2, congress=1, ownership=0) == 40 + 12 + 6
    assert scoring.convergence_score(insiders=6, congress=6, ownership=3) == 100


def test_quiver_normalization():
    row = {"Representative": "Jane Roe", "Transaction": "Purchase", "Ticker": "nvda", "Range": "$15,001 - $50,000",
           "TransactionDate": "2025-02-01", "ReportDate": "2025-02-20", "Party": "D", "House": "Representatives"}
    n = congress.normalize(row)
    assert n["side"] == "buy" and n["ticker"] == "NVDA"
    assert n["amount_min"] == 15001 and n["amount_max"] == 50000
    assert n["trade_date"] == date(2025, 2, 1) and n["report_date"] == date(2025, 2, 20)
    assert n["source_key"] == congress.normalize(dict(row))["source_key"]
    assert congress.normalize({**row, "Transaction": "Sale (Partial)"})["side"] == "sell"
    assert congress.normalize({**row, "Transaction": "Exchange"}) is None
    assert congress.normalize({"Name": "X", "Transaction": "Sale", "Ticker": "A"})["politician"] == "X"


def test_missing_variable_is_named():
    with pytest.raises(ConfigError) as exc:
        load_settings({"DATABASE_URL": "postgresql://x", "ADMIN_EMAIL": "a@b.test"})
    assert "JWT_SECRET" in str(exc.value)
    s = load_settings({"DATABASE_URL": "postgresql://x", "ADMIN_EMAIL": "A@B.test", "JWT_SECRET": "x" * 32})
    assert s.admin_email == "a@b.test" and s.sec_user_agent == "Sillage/1.0 a@b.test"
    assert s.paper_start_cash == 25000 and s.congress_enabled and not s.quiver_api_key


def test_price_parsers():
    from sillage.ingest import prices
    assert prices.parse_chart({"chart": {"result": [{"meta": {"regularMarketPrice": 12.5, "currency": "USD"}}]}}) == (12.5, "USD")
    assert prices.parse_chart({"chart": {"result": None, "error": {"code": "Not Found"}}}) is None
    assert prices.parse_nasdaq({"data": {"primaryData": {"lastSalePrice": "$1,356.30"}}}) == (1356.3, "USD")
    assert prices.parse_nasdaq({"data": None}) is None
    assert prices.clean_ticker(" brk.b ") == "BRK.B" and prices.clean_ticker("A;DROP") is None
