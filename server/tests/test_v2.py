"""Sources et calculs ajoutés en v2 (extraits de réponses réelles du 13-14 novembre 2026)."""
import json
from datetime import date
from pathlib import Path

from sillage import model, signals
from sillage.ingest import bafin, buybacks, earnings, euronext, fundamentals, senate, usaspending

FIX = Path(__file__).parent / "fixtures"


def test_fundamentals():
    assert fundamentals.recent_frames(date(2026, 11, 14)) == ["CY2026Q3I", "CY2026Q2I", "CY2026Q1I"]
    assert fundamentals.recent_frames(date(2026, 2, 1))[0] == "CY2025Q4I"
    rows = fundamentals.parse_frame({"data": [{"accn": "x", "cik": 1750, "entityName": "AAR CORP", "loc": "US-IL",
                                               "end": "2026-06-30", "val": 39892472}, {"cik": "bad"}]})
    assert rows == [(1750, 39892472.0, date(2026, 6, 30))]
    assert fundamentals.cap_bucket(1.5e9) == "300 M$-2 Md$" and fundamentals.cap_bucket(None) == "inconnue"


def test_buybacks_hits():
    data = {"hits": {"hits": [{"_id": "0001493152-26-049714:form8-k.htm", "_source": {
        "ciks": ["0001473334"], "display_names": ["XMax Inc.  (XMAX)  (CIK 0001473334)"], "file_date": "2026-11-03",
        "form": "8-K", "adsh": "0001493152-26-049714"}}]}}
    h = buybacks.parse_hits(data)[0]
    assert h == {"accession": "0001493152-26-049714", "cik": 1473334, "company": "XMax Inc.", "filed": date(2026, 11, 3)}


def test_earnings_rows():
    rows = earnings.parse({"data": {"rows": [{"symbol": "NVDA", "time": "time-after-hours"},
                                             {"symbol": "HD", "time": "time-pre-market"},
                                             {"symbol": "X", "time": "time-not-supplied"}]}})
    assert rows == [("NVDA", "après clôture"), ("HD", "avant ouverture"), ("X", None)]
    assert earnings.parse({"data": None}) == []


def test_euronext_and_ecb():
    rows = euronext.parse_csv((FIX / "euronext.csv").read_text(encoding="utf-8-sig"))
    assert rows and rows[-1] == (date(2026, 11, 13), 1341.5)
    assert euronext.mic_for("Euronext Growth Paris")[0] == "ALXP" and euronext.mic_for(None)[0] == "XPAR"
    day, rates = euronext.parse_ecb("<Cube time='2026-11-13'><Cube currency='USD' rate='1.1052'/>"
                                    "<Cube currency='GBP' rate='0.83559'/></Cube>")
    assert day == date(2026, 11, 13) and rates == {"USD": 1.1052, "GBP": 0.83559}


def test_bafin_csv():
    rows = bafin.parse_csv((FIX / "bafin.csv").read_bytes().decode("utf-8"))
    r = rows[0]
    assert r["company"] == "UmweltBank Aktiengesellschaft" and r["isin"] == "DE0005570808"
    assert r["side"] == "buy" and r["price"] == 3.55 and r["value"] == 20945.0 and r["currency"] == "EUR"
    assert r["trade_date"] == date(2026, 11, 12) and r["published"] == date(2026, 11, 13)
    assert bafin.de_number("1.234.567,89 EUR") == (1234567.89, "EUR")


def test_bafin_roles_in_score():
    base = {"id": 1, "company": "X AG", "person": "A", "side": "buy", "isin": "DE0005570808", "price": 10,
            "currency": "EUR", "volume": 30000, "value": 300000, "trade_date": date(2026, 11, 10),
            "published": date(2026, 11, 11), "venue": "XETRA", "url": None}
    board = signals.europe_signal({**base, "role": "Vorstand"}, 0, "bafin", None)
    relative = signals.europe_signal({**base, "role": "in enger Beziehung"}, 0, "bafin", None)
    assert board["score"] == relative["score"] + 15 and board["role"] == "Membre du directoire"
    assert any("Membre du directoire" in x for x in board["reasons"]) and relative["role"] == "Proche d'un dirigeant"
    assert board["id"] == "b1" and board["currency"] == "EUR"


def test_senate_parsers():
    lst = senate.parse_report_list({"data": [
        ["JOHN", "DOE", "Doe, John (Senator)", '<a href="/search/view/ptr/1a2b-3c/" target="_blank">PTR</a>', "11/10/2026"],
        ["JANE", "ROE", "Roe, Jane", '<a href="/search/view/paper/9f9f-00/">Paper</a>', "11/09/2026"]]})
    assert lst[0]["senator"] == "John Doe" and lst[0]["filed"] == date(2026, 11, 10) and not lst[0]["paper"]
    assert lst[1]["paper"] and lst[0]["url"].endswith("/search/view/ptr/1a2b-3c/")
    page = """<table><tbody>
      <tr><td>1</td><td>11/03/2026</td><td>Spouse</td><td><a href="#">NVDA</a></td><td>NVIDIA Corporation</td>
          <td>Stock</td><td>Purchase</td><td>$1,001 - $15,000</td><td>--</td></tr>
      <tr><td>2</td><td>11/04/2026</td><td>Self</td><td>--</td><td>US Treasury Bill</td>
          <td>Municipal Security</td><td>Purchase</td><td>$15,001 - $50,000</td><td>--</td></tr>
      <tr><td>3</td><td>11/05/2026</td><td>Joint</td><td>AAPL</td><td>Apple Inc.</td>
          <td>Stock</td><td>Sale (Partial)</td><td>$50,001 - $100,000</td><td></td></tr></tbody></table>"""
    tx = senate.parse_ptr(page)
    assert [(t["ticker"], t["side"]) for t in tx] == [("NVDA", "buy"), ("AAPL", "sell")]
    assert tx[1]["amount_min"] == 50001 and tx[0]["trade_date"] == date(2026, 11, 3)


def test_usaspending():
    assert usaspending.search_name("Lockheed Martin Corp") == "LOCKHEED MARTIN"
    assert usaspending.search_name("The Boeing Company") == "BOEING"
    total, rec = usaspending.parse({"results": [
        {"name": "LOCKHEED MARTIN CORPORATION", "amount": 17850000128.91},
        {"name": "SIKORSKY AIRCRAFT CORPORATION", "amount": 3130025631.89},
        {"name": "LOCKHEED MARTIN CORPORATION", "amount": 3121071185.2}]}, "LOCKHEED MARTIN")
    assert round(total) == round(17850000128.91 + 3121071185.2) and len(rec) == 2


def test_score_model():
    assert model.sample_check() > 0.4  # le modèle sépare un critère favorable d'un défavorable
    assert model.auc([0.9, 0.8, 0.2, 0.1], [1, 1, 0, 0]) == 1.0
    assert model.auc([0.1, 0.2, 0.8, 0.9], [1, 1, 0, 0]) == 0.0
    m = {"bias": 0.0, "weights": {"plan": -1.0, "cluster=3+": 0.8}, "quantiles": [i / 100 for i in range(101)]}
    fs = model.features(role="Autre", value=60000, delay=1, cluster=3, plan=False, position_pct=20, first_line=False,
                        price=12, market_cap=5e8, buyback=True, earnings_soon=False, track_wins=0.7)
    assert {"cluster=3+", "buyback", "track=good", "cap=300 M$-2 Md$", "pos=10-50", "value=50-250K"} <= set(fs)
    p = model.predict(m, fs)
    assert 0.6 < p < 0.7 and model.percentile(m, p) == 69
    assert model.explain(m, fs, p)[0].startswith("Cluster de 3 initiés ou plus (+")


def test_sectors_and_v2_features():
    from sillage.ingest import company
    assert company.sector_for("2834") == "Santé" and company.sector_for("7372") == "Technologie"
    assert company.sector_for("6798") == "Immobilier" and company.sector_for("6022") == "Finance"
    assert company.sector_for("1311") == "Énergie" and company.sector_for(None) is None
    fs = model.features_congress(amount_min=60000, delay=40, cluster=2, chamber="Senate", track_wins=0.3)
    assert fs == ["amount=50-250K", "delay=>30", "cluster=2", "chamber=Sénat", "track=bad"]
    fe = model.features_europe(role_class="top", value=10_000, delay=1, cluster=0, track_wins=None)
    assert fe == ["role=top", "value=<50K", "delay=<=2"]
    from sillage import backtest
    assert backtest.europe_role_class("bafin", "Vorstand") == "top"
    assert backtest.europe_role_class("amf", "Directeur Général") == "top"
    assert backtest.europe_role_class("amf", None) == "close"
    small = model.fit_samples([(date(2026, 1, 1), ["a"], 1, 0.1)] * 10, 300)
    assert small["model"] is None and "pas assez" in small["metrics"]["reason"]
