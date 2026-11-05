"""Exchange rates (NBP, "the day before") and the bank provider (mock, real EnableBanking without network)."""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from bank_paths import FIXTURES, OCTOBER
from kasa.core.bank.fx import FixtureNbpSource, NbpApiSource, load_rates, rate_before, store_rates
from kasa.core.providers.bank import (
    EB_APP_ID_ENV,
    EB_PRIVATE_KEY_ENV,
    EB_SESSION_ENV,
    BankProvider,
    EnableBanking,
    MockEnableBanking,
    ProviderNotConfigured,
    get_bank_provider,
)

NBP = FIXTURES / "reference" / "nbp_rates.json"


def test_fixture_rates_and_the_day_before(conn):
    source = FixtureNbpSource(NBP)
    assert source.currencies == ["EUR", "USD"]
    inserted = load_rates(conn, source, ["EUR", "USD"], date(2026, 9, 1), date(2026, 10, 31))
    assert inserted > 80
    assert load_rates(conn, source, ["EUR", "USD"], date(2026, 9, 1), date(2026, 10, 31)) == 0   # idempotent
    data = json.loads(NBP.read_text(encoding="utf-8"))["rates"]["EUR"]
    by_day = {item["date"]: item for item in data}

    # card payment booked Friday 16.10 -> rate of Thursday 15.10
    rate = rate_before(conn, "EUR", date(2026, 10, 16))
    assert rate.rate_date == date(2026, 10, 15) and rate.rate == Decimal(by_day["2026-10-15"]["mid"])
    assert rate.table_ref == by_day["2026-10-15"]["table"]
    # Monday -> Friday (no table on the weekend)
    monday = date(2026, 10, 19)
    assert rate_before(conn, "EUR", monday).rate_date == date(2026, 10, 16)
    assert rate_before(conn, "PLN", monday) is None
    assert rate_before(conn, "EUR", date(2025, 1, 1)) is None


def test_store_rates_is_shared_reference_data(conn):
    rates = FixtureNbpSource(NBP).rates_between("USD", date(2026, 10, 1), date(2026, 10, 2))
    assert store_rates(conn, rates) == 2
    rows = conn.execute(
        "SELECT company_id, source, method::text FROM l1_fx_rate WHERE currency = 'USD' AND rate_date <= '2026-10-02'"
        " AND rate_date >= '2026-10-01'").fetchall()
    assert rows == [(None, "nbp_a", "code")] * 2


def test_nbp_api_source_builds_the_documented_url(monkeypatch):
    calls = []

    def fake(self, url):
        calls.append(url)
        return {"rates": [{"no": "201/A/NBP/2026", "effectiveDate": "2026-10-15", "mid": 4.2468}]}

    monkeypatch.setattr(NbpApiSource, "_fetch", fake)
    rate = NbpApiSource().rate_on("EUR", date(2026, 10, 15))
    assert calls == ["https://api.nbp.pl/api/exchangerates/rates/a/eur/2026-10-15/?format=json"]
    assert rate.rate == Decimal("4.2468") and rate.table_ref == "201/A/NBP/2026"


def test_card_payment_carries_bank_rate_and_nbp_rate(conn, ingested, rows_of):
    company, _ = ingested
    [card] = rows_of(company.id, "t.entry_reference = %s", ("202610166224040945",))
    assert (card["original_amount_minor"], card["original_currency"], card["fx_rate"]) == (
        -9900, "EUR", Decimal("4.4154"))
    fx = card["extracted"]["fx"]
    assert fx["bank_rate"] == "4.4154" and fx["nbp_date"] == "2026-10-15"
    expected = (Decimal(-99) * Decimal(fx["nbp_rate"])).quantize(Decimal("0.01"))
    assert fx["pln_at_nbp_minor"] == int(expected * 100)


def test_mock_provider_shape_and_ranges():
    provider = MockEnableBanking(FIXTURES)
    assert isinstance(provider, BankProvider)
    accounts = provider.list_accounts()
    assert [(a.iban, a.kind) for a in accounts] == [
        ("PL40114020040000350277812345", "current"), ("PL67114020040000310277812346", "vat"),
        ("PL80116022020000000412906733", "current")]
    assert accounts[0].bank_name == "mBank S.A." and accounts[0].owner_name == "ODM Partners sp. z o.o."
    full = provider.fetch_transactions(accounts[0], *OCTOBER)
    assert [b.params["transaction_status"] for b in full] == ["BOOK", "PDNG"]
    assert full[0].payload == (FIXTURES / "bank/enablebanking/mbank_biezacy/transactions.json").read_bytes()
    assert full[0].meta()["endpoint"] == f"/accounts/{accounts[0].uid}/transactions"
    part = provider.fetch_transactions(accounts[0], date(2026, 10, 1), date(2026, 10, 5))
    assert len(json.loads(part[0].payload)["transactions"]) == 4
    assert provider.fetch_transactions(accounts[0], date(2026, 10, 1), date(2026, 10, 5))[0].payload == \
        part[0].payload                                        # deterministic bytes for the same range
    assert json.loads(provider.fetch_balances(accounts[0]).payload)["balances"][0]["balance_type"] == "CLBD"


def test_consent_and_the_14_day_alert():
    consent = MockEnableBanking(FIXTURES, today=date(2026, 11, 5)).consent_status()
    assert consent.valid_until.date() == date(2027, 2, 28) and consent.days_left == 115 and not consent.alert
    late = MockEnableBanking(FIXTURES, today=date(2027, 2, 15)).consent_status()
    assert late.days_left == 13 and late.alert


def test_factory_falls_back_to_mock_without_keys(monkeypatch, caplog):
    for env in (EB_APP_ID_ENV, EB_PRIVATE_KEY_ENV, EB_SESSION_ENV):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("KASA_BANK_PROVIDER", "enablebanking")
    with pytest.raises(ProviderNotConfigured):
        EnableBanking()
    with caplog.at_level(logging.WARNING):
        provider = get_bank_provider()
    assert isinstance(provider, MockEnableBanking)
    assert "KASA_EB_APP_ID" in caplog.text
    monkeypatch.setenv("KASA_BANK_PROVIDER", "mock")
    assert isinstance(get_bank_provider(), MockEnableBanking)


def test_real_provider_signs_a_jwt_and_reads_a_session(tmp_path, monkeypatch):
    rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")
    from cryptography.hazmat.primitives import serialization

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "eb.pem"
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    monkeypatch.setenv(EB_APP_ID_ENV, "app-123")
    monkeypatch.setenv(EB_PRIVATE_KEY_ENV, str(path))
    monkeypatch.setenv(EB_SESSION_ENV, "sess-1")
    provider = EnableBanking(today=date(2027, 2, 20))
    header, claims, _signature = provider._jwt().split(".")
    pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
    assert json.loads(pad(header)) == {"typ": "JWT", "alg": "RS256", "kid": "app-123"}
    payload = json.loads(pad(claims))
    assert payload["iss"] == "enablebanking.com" and payload["aud"] == "api.enablebanking.com"
    assert payload["exp"] - payload["iat"] == 3600

    valid_until = (datetime(2027, 3, 1, tzinfo=UTC)).isoformat()
    responses = {
        "/sessions/sess-1": {"status": "AUTHORIZED", "accounts": ["uid-1"], "access": {"valid_until": valid_until}},
        "/accounts/uid-1/details": {"account_id": {"iban": "PL40114020040000350277812345"}, "currency": "PLN",
                                    "product": "Rachunek VAT", "account_servicer": {"bic_fi": "BREXPLPWMBK"}},
    }
    seen = []

    def fake_get(path, params=None):
        seen.append((path, params))
        if path.endswith("/transactions"):
            key = params["continuation_key"]
            return json.dumps({"transactions": [], "continuation_key": None if key else "next"}).encode()
        return json.dumps(responses[path]).encode()

    monkeypatch.setattr(provider, "_get", fake_get)
    [account] = provider.list_accounts()
    assert (account.uid, account.kind, account.bic) == ("uid-1", "vat", "BREXPLPWMBK")
    batches = provider.fetch_transactions(account, date(2026, 10, 1), date(2026, 10, 31))
    assert [(b.params["transaction_status"], b.params["continuation_key"]) for b in batches] == [
        ("BOOK", None), ("BOOK", "next"), ("PDNG", None), ("PDNG", "next")]
    consent = provider.consent_status()
    assert consent.days_left == 9 and consent.alert and consent.valid_until - timedelta(days=9) == \
        datetime(2027, 2, 20, tzinfo=UTC)
