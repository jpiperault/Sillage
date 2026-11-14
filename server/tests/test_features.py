"""Backtest, réglages modifiables, alertes Telegram."""
import io
import zipfile
from datetime import date

import pytest

from sillage import alerts, backtest, settings_store
from sillage.config import load_settings

BASE = {"DATABASE_URL": "postgresql://x", "ADMIN_EMAIL": "admin@b.test", "JWT_SECRET": "x" * 32}


# ---------------------------------------------------------------- backtest

def test_list_quarters():
    html = ('<a href="/files/datastandardsinnovation/data/insider-transactions-data-sets/2026q2_form345.zip">'
            '<a href="/files/structureddata/data/insider-transactions-data-sets/2026q1_form345.zip">'
            '<a href="/files/structureddata/data/insider-transactions-data-sets/2025q4_form345.zip">')
    qs = backtest.list_quarters(html)
    assert [q for q, _ in qs] == ["2026q2", "2026q1", "2025q4"]
    assert qs[0][1].startswith("https://www.sec.gov/files/datastandardsinnovation/")


def _dataset() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SUBMISSION.tsv",
                   "ACCESSION_NUMBER\tFILING_DATE\tDOCUMENT_TYPE\tISSUERCIK\tISSUERTRADINGSYMBOL\tAFF10B5ONE\n"
                   "A1\t03-MAR-2026\t4\t0000000123\tACME\t0\n"
                   "A2\t05-MAR-2026\t4/A\t0000000123\tACME\t0\n"
                   "A3\t06-MAR-2026\t4\t0000000999\tNONE\t1\n")
        z.writestr("REPORTINGOWNER.tsv",
                   "ACCESSION_NUMBER\tRPTOWNERCIK\tRPTOWNERNAME\tRPTOWNER_RELATIONSHIP\tRPTOWNER_TITLE\n"
                   "A1\t0000000001\tDoe John\tOfficer\tCEO\n"
                   "A1\t0000000002\tDoe Trust\tTenPercentOwner\t\n")
        z.writestr("NONDERIV_TRANS.tsv",
                   "ACCESSION_NUMBER\tNONDERIV_TRANS_SK\tTRANS_DATE\tTRANS_CODE\tTRANS_SHARES\tTRANS_PRICEPERSHARE"
                   "\tSHRS_OWND_FOLWNG_TRANS\n"
                   "A1\t10\t02-MAR-2026\tP\t100\t10\t1100\n"
                   "A1\t11\t02-MAR-2026\tP\t300\t12\t1400\n"
                   "A1\t12\t02-MAR-2026\tG\t5\t0\t1395\n"
                   "A2\t13\t02-MAR-2026\tP\t1\t1\t1\n")
    return buf.getvalue()


def test_parse_dataset_groups_lines():
    rows = backtest.parse_dataset(_dataset(), "2026q1")
    assert len(rows) == 1  # 4/A et ticker NONE ignorés, une ligne par (déclaration, code)
    acc, code, q, ticker, cik, owner_cik, name, rel, title, shares, value, price, after, td, fd, plan = rows[0]
    assert (acc, code, ticker, cik) == ("A1", "P", "ACME", 123)
    assert name == "Doe John et al." and "Officer" in rel and "TenPercentOwner" in rel and title == "CEO"
    assert shares == 400 and value == 4600 and price == 11.5 and after == 1400
    assert td == date(2026, 3, 2) and fd == date(2026, 3, 3) and plan is False


def test_position_change_and_history():
    assert backtest.position_change("P", 400, 1400) == 40.0  # 1000 → 1400
    assert backtest.position_change("P", 500, 500) is None  # première ligne
    assert backtest.position_change("S", 250, 750) == 25.0
    data = {"data": {"tradesTable": {"rows": [{"date": "11/12/2026", "close": "$354.88"},
                                              {"date": "11/11/2026", "close": "1,355.63"}]}}}
    assert backtest.parse_history(data) == [(date(2026, 11, 11), 1355.63), (date(2026, 11, 12), 354.88)]
    assert backtest.parse_history({"data": None}) == []


def _row(**kw):
    r = {"accession": "A", "code": "P", "ticker": "X", "relationship": "Officer", "officer_title": "CEO",
         "owner_cik": 1, "owner_name": "Doe", "market_cap": None,
         "value": 300_000, "shares": 100, "shares_after": 200, "trade_date": date(2026, 1, 5),
         "filed_date": date(2026, 1, 6), "plan_10b5_1": False, "cluster": 1, "entry": 10.0,
         "spy_entry": 100.0, "c1": 11.0, "c2": 12.0, "c3": None, "spy1": 101.0, "spy2": 100.0, "spy3": None}
    r.update(kw)
    return r


def test_build_report():
    rep = backtest.build_report([_row(), _row(c1=9.0, c2=8.0, value=10_000, officer_title=None, relationship="Director")])
    assert rep["signals"] == 2
    tous = next(g for g in rep["groups"] if g["dimension"] == "Tous" and g["side"] == "Achats (P)")
    s1 = tous["stats"]["21"]
    assert s1["n"] == 2 and s1["win"] == 50.0
    assert s1["mean"] == pytest.approx(((0.10 - 0.01) + (-0.10 - 0.01)) / 2 * 100)
    assert tous["stats"]["126"] is None  # horizon pas encore atteint
    labels = {(g["dimension"], g["label"]) for g in rep["groups"]}
    assert ("Rôle", "Dirigeant de premier plan") in labels and ("Montant", "< 50 K$") in labels
    assert ("Taille vs position", "+ ≥ 100 %") in labels


# ---------------------------------------------------------------- réglages

def test_settings_overrides():
    base = load_settings(BASE)
    eff = settings_store.apply(base, {"ingest_interval_min": "5", "amf_enabled": "non", "funds_13f": "1067983",
                                      "sec_user_agent": "Sillage/1.0 robot@b.test", "telegram_bot_token": "123:abc",
                                      "public_url": "https://sillage.example/", "unknown": "x",
                                      "backfill_days": "abc"})
    assert eff.ingest_interval_min == 5 and not eff.amf_enabled and list(eff.funds_13f) == [1067983]
    assert eff.sec_user_agent == "Sillage/1.0 robot@b.test" and eff.telegram_bot_token == "123:abc"
    assert eff.public_url == "https://sillage.example" and eff.backfill_days == base.backfill_days  # invalide ignoré
    with pytest.raises(settings_store.SettingError):
        settings_store.convert("sec_user_agent", "Sillage sans contact")
    with pytest.raises(settings_store.SettingError):
        settings_store.convert("backtest_quarters", "40")
    desc = {d["key"]: d for d in settings_store.describe(base, {"telegram_bot_token": "123:abcdef"},
                                                         settings_store.apply(base, {"telegram_bot_token": "123:abcdef"}))}
    assert desc["telegram_bot_token"]["value"] == "••••cdef" and desc["telegram_bot_token"]["source"] == "interface"
    assert desc["funds_13f"]["value"] == ""  # liste par défaut


# ---------------------------------------------------------------- alertes

def test_alert_formatting():
    assert alerts.parse_start("/start AbC123") == "AbC123" and alerts.parse_start("/start") is None
    assert alerts.parse_start("bonjour") is None
    sig = {"score": 85, "ticker": "AAPL", "side": "buy", "value": 1_200_000, "person": "Cook <Tim>", "role": "CEO",
           "amount_range": None}
    text = alerts.format_message([sig], [{"ticker": "AAPL", "person": "X", "market_value": 2_000_000}],
                                 [{"ticker": "MSFT", "price": 400, "pnl": -35.5}], "https://s.example")
    assert "<b>85</b> AAPL — Achat 1,2 M$ — Cook &lt;Tim&gt; (CEO)" in text
    assert "Form 144" in text and "MSFT vendu à 400.00 $ (-35.50 $)" in text
    assert text.endswith('<a href="https://s.example/">Ouvrir Sillage</a>')
    assert alerts.format_message([], [], [], "") is None
    eur = alerts.format_signal({"score": 70, "ticker": None, "company": "HERMES", "side": "sell", "value": 5_000,
                                "currency": "EUR", "person": "X", "role": None})
    assert "HERMES — Vente 5 K€" in eur
