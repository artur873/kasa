"""Bank provider: account list, transactions, balances and consent (PSD2 via EnableBanking).

Two implementations of :class:`BankProvider`:

* :class:`MockEnableBanking` - reads ``<fixtures>/bank/enablebanking/`` (directory from
  ``KASA_FIXTURES_DIR``, default ``fixtures/odm``); the files have the shape of the
  EnableBanking API responses, so the same parser reads both.
* :class:`EnableBanking` - the real API at https://api.enablebanking.com. It needs
  ``KASA_EB_APP_ID`` (application id = ``kid`` of the JWT), ``KASA_EB_PRIVATE_KEY_PATH``
  (PEM private key of the application, RS256) and ``KASA_EB_SESSION_ID`` (session
  created after the company authorised its accounts in the bank). Without them the
  constructor raises :class:`ProviderNotConfigured` and :func:`get_bank_provider`
  falls back to the mock (a missing key never breaks the start, CLAUDE.md).

A provider only fetches bytes: every response is returned as a :class:`RawBatch`
(exact bytes + endpoint and parameters) to be stored in L0 before parsing.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from kasa.core.providers import provider_name

log = logging.getLogger(__name__)

FIXTURES_DIR_ENV = "KASA_FIXTURES_DIR"
DEFAULT_FIXTURES_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "odm"
EB_API_URL = "https://api.enablebanking.com"
EB_APP_ID_ENV = "KASA_EB_APP_ID"
EB_PRIVATE_KEY_ENV = "KASA_EB_PRIVATE_KEY_PATH"
EB_SESSION_ENV = "KASA_EB_SESSION_ID"
EB_API_URL_ENV = "KASA_EB_API_URL"
CONSENT_ALERT_DAYS = 14      # briefing, step 9: alert 14 days before the consent expires
CONSENT_MAX_DAYS = 180


def fixtures_dir_path(explicit: str | Path | None = None) -> Path:
    """Directory of the test data: ``explicit``, else ``KASA_FIXTURES_DIR``, else ``fixtures/odm``.

    A relative path is taken from the current directory when it exists there, otherwise from the repository.
    """
    path = Path(explicit or os.environ.get(FIXTURES_DIR_ENV) or DEFAULT_FIXTURES_DIR)
    if path.is_absolute() or path.exists():
        return path
    return DEFAULT_FIXTURES_DIR.parents[1] / path


from kasa.core.providers import ProviderError, ProviderNotConfigured  # noqa: E402  (shared exceptions)


# ---------------------------------------------------------------------------
# Data passed between a provider and the ingestion
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountInfo:
    uid: str                    # account id at the provider
    iban: str
    currency: str
    kind: str                   # current / vat / savings
    provider: str
    owner_name: str | None = None
    details: str | None = None  # product description shown by the bank
    product: str | None = None
    bank_name: str | None = None
    bic: str | None = None
    has_balance_after_transaction: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)


@dataclass(frozen=True)
class RawBatch:
    """One response exactly as received, with what was asked for."""

    payload: bytes
    endpoint: str
    account_uid: str
    content_type: str = "application/json"
    params: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)
    provider: str = "enablebanking"

    def meta(self) -> dict[str, Any]:
        return {"provider": self.provider, "endpoint": self.endpoint, "account_uid": self.account_uid,
                "params": self.params}


@dataclass(frozen=True)
class ConsentInfo:
    valid_until: datetime | None
    days_left: int | None
    status: str
    accounts: tuple[str, ...] = ()

    @property
    def alert(self) -> bool:
        """True when the consent ends within 14 days (or already ended)."""
        return self.days_left is not None and self.days_left <= CONSENT_ALERT_DAYS


@runtime_checkable
class BankProvider(Protocol):
    name: str

    def list_accounts(self, company: Any = None) -> list[AccountInfo]: ...

    def fetch_transactions(self, account: AccountInfo, date_from: date, date_to: date) -> list[RawBatch]: ...

    def fetch_balances(self, account: AccountInfo) -> RawBatch: ...

    def consent_status(self) -> ConsentInfo: ...


def infer_account_kind(raw: dict[str, Any]) -> str:
    """current / vat / savings from what the bank says about the account."""
    hint = (raw.get("fixture") or {}).get("kind")
    if hint in ("current", "vat", "savings"):
        return hint
    text = " ".join(str(raw.get(k) or "") for k in ("details", "product", "name")).upper()
    if "VAT" in text:
        return "vat"
    if raw.get("cash_account_type") == "SVGS" or "OSZCZ" in text:
        return "savings"
    return "current"


def _account_from_eb(raw: dict[str, Any], provider: str) -> AccountInfo:
    account_id = raw.get("account_id") or {}
    servicer = raw.get("account_servicer") or {}
    fixture = raw.get("fixture") or {}
    iban = account_id.get("iban") or (account_id.get("other") or {}).get("identification")
    if not iban:
        raise ProviderError(f"rachunek {raw.get('uid')} nie ma IBAN-u")
    return AccountInfo(
        uid=str(raw.get("uid") or raw.get("account_uid") or iban),
        iban=iban,
        currency=raw.get("currency") or "PLN",
        kind=infer_account_kind(raw),
        provider=provider,
        owner_name=raw.get("name"),
        details=raw.get("details"),
        product=raw.get("product"),
        bank_name=servicer.get("name"),
        bic=servicer.get("bic_fi"),
        has_balance_after_transaction=fixture.get("has_balance_after_transaction"),
        raw=raw,
    )


def _consent(valid_until: datetime | None, status: str, accounts: tuple[str, ...], today: date | None) -> ConsentInfo:
    today = today or date.today()
    days_left = None if valid_until is None else (valid_until.date() - today).days
    return ConsentInfo(valid_until=valid_until, days_left=days_left, status=status, accounts=accounts)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Mock: fixtures in the shape of EnableBanking responses
# ---------------------------------------------------------------------------


class MockEnableBanking:
    """EnableBanking without network: ``<fixtures>/bank/enablebanking/<account>/*.json``."""

    name = "mock"
    provider = "enablebanking"

    def __init__(self, fixtures_dir: str | Path | None = None, *, consent_valid_until: datetime | None = None,
                 today: date | None = None):
        self.base = fixtures_dir_path(fixtures_dir) / "bank" / "enablebanking"
        if not (self.base / "accounts.json").is_file():
            raise ProviderError(f"brak danych testowych banku: {self.base / 'accounts.json'}")
        self._today = today
        # A consent granted on 1.09.2026 for 180 days, unless the test sets another.
        self._valid_until = consent_valid_until or datetime(2026, 9, 1, tzinfo=UTC) + timedelta(days=CONSENT_MAX_DAYS)

    def _accounts_raw(self) -> list[dict[str, Any]]:
        return json.loads((self.base / "accounts.json").read_bytes())["accounts"]

    def _dir(self, account: AccountInfo) -> Path:
        slug = (account.raw.get("fixture") or {}).get("slug")
        if slug and (self.base / slug).is_dir():
            return self.base / slug
        for raw in self._accounts_raw():
            if raw.get("uid") == account.uid:
                return self.base / raw["fixture"]["slug"]
        raise ProviderError(f"mock nie zna rachunku {account.uid}")

    def list_accounts(self, company: Any = None) -> list[AccountInfo]:
        return [_account_from_eb(raw, self.provider) for raw in self._accounts_raw()]

    def fetch_transactions(self, account: AccountInfo, date_from: date, date_to: date) -> list[RawBatch]:
        directory = self._dir(account)
        batches = []
        for status, filename, date_key in (("BOOK", "transactions.json", "booking_date"),
                                           ("PDNG", "transactions_pending.json", "transaction_date")):
            path = directory / filename
            if not path.is_file():
                continue
            payload = path.read_bytes()
            data = json.loads(payload)
            selected = [
                t for t in data.get("transactions", [])
                if t.get(date_key) is None or date_from.isoformat() <= t[date_key] <= date_to.isoformat()
            ]
            if len(selected) != len(data.get("transactions", [])):
                # The API answers only for the asked range: re-serialize deterministically.
                payload = json.dumps({"transactions": selected, "continuation_key": None},
                                     ensure_ascii=False, indent=2).encode("utf-8")
            batches.append(RawBatch(
                payload=payload,
                endpoint=f"/accounts/{account.uid}/transactions",
                account_uid=account.uid,
                params={"date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
                        "transaction_status": status, "continuation_key": None},
                provider=self.provider,
            ))
        return batches

    def fetch_balances(self, account: AccountInfo) -> RawBatch:
        return RawBatch(
            payload=(self._dir(account) / "balances.json").read_bytes(),
            endpoint=f"/accounts/{account.uid}/balances",
            account_uid=account.uid,
            provider=self.provider,
        )

    def consent_status(self) -> ConsentInfo:
        uids = tuple(raw["uid"] for raw in self._accounts_raw())
        return _consent(self._valid_until, "AUTHORIZED", uids, self._today)


# ---------------------------------------------------------------------------
# Real EnableBanking API
# ---------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class EnableBanking:
    """https://api.enablebanking.com, application JWT signed with RS256.

    Endpoints used: ``GET /sessions/{id}`` (accounts, consent validity),
    ``GET /accounts/{uid}/details``, ``GET /accounts/{uid}/balances`` and
    ``GET /accounts/{uid}/transactions`` (paged by ``continuation_key``).
    Starting an authorisation (``POST /auth``) and creating the session
    (``POST /sessions``) are one-time steps described in docs/BANK.md.
    """

    name = "enablebanking"
    provider = "enablebanking"

    def __init__(self, *, app_id: str | None = None, private_key_path: str | None = None,
                 session_id: str | None = None, api_url: str | None = None, timeout: float = 30.0,
                 today: date | None = None):
        self.app_id = app_id or os.environ.get(EB_APP_ID_ENV, "").strip()
        key_path = private_key_path or os.environ.get(EB_PRIVATE_KEY_ENV, "").strip()
        self.session_id = session_id or os.environ.get(EB_SESSION_ENV, "").strip()
        missing = [env for env, value in ((EB_APP_ID_ENV, self.app_id), (EB_PRIVATE_KEY_ENV, key_path),
                                          (EB_SESSION_ENV, self.session_id)) if not value]
        if missing:
            raise ProviderNotConfigured("EnableBanking: brak zmiennych " + ", ".join(missing))
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise ProviderNotConfigured("EnableBanking: zainstaluj pakiet 'cryptography' (podpis JWT RS256)") from exc
        try:
            pem = Path(key_path).expanduser().read_bytes()
            self._key = serialization.load_pem_private_key(pem, password=None)
        except (OSError, ValueError, TypeError) as exc:
            raise ProviderNotConfigured(f"EnableBanking: nie można wczytać klucza {key_path}: {exc}") from exc
        self._hashes, self._padding = hashes, padding
        self.api_url = (api_url or os.environ.get(EB_API_URL_ENV) or EB_API_URL).rstrip("/")
        self.timeout = timeout
        self._today = today
        self._token: tuple[str, float] | None = None

    # -- transport ----------------------------------------------------------

    def _jwt(self) -> str:
        now = int(time.time())
        if self._token and self._token[1] > now + 60:
            return self._token[0]
        header = {"typ": "JWT", "alg": "RS256", "kid": self.app_id}
        claims = {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": now, "exp": now + 3600}
        signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(claims).encode())}"
        signature = self._key.sign(signing_input.encode("ascii"), self._padding.PKCS1v15(), self._hashes.SHA256())
        token = f"{signing_input}.{_b64url(signature)}"
        self._token = (token, now + 3600)
        return token

    def _get(self, path: str, params: dict[str, Any] | None = None) -> bytes:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self.api_url}{path}" + (f"?{query}" if query else "")
        request = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self._jwt()}",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - fixed https host
                return response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500]
            raise ProviderError(f"EnableBanking {exc.code} dla {path}: {body}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"EnableBanking niedostępny ({path}): {exc.reason}") from exc

    def _session(self) -> dict[str, Any]:
        return json.loads(self._get(f"/sessions/{self.session_id}"))

    # -- BankProvider ------------------------------------------------------

    def list_accounts(self, company: Any = None) -> list[AccountInfo]:
        session = self._session()
        accounts = []
        for entry in session.get("accounts", []):
            uid = entry if isinstance(entry, str) else entry.get("uid")
            details = json.loads(self._get(f"/accounts/{uid}/details"))
            details.setdefault("uid", uid)
            accounts.append(_account_from_eb(details, self.provider))
        return accounts

    def fetch_transactions(self, account: AccountInfo, date_from: date, date_to: date) -> list[RawBatch]:
        batches = []
        endpoint = f"/accounts/{account.uid}/transactions"
        for status in ("BOOK", "PDNG"):
            continuation: str | None = None
            while True:
                params = {"date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
                          "transaction_status": status, "continuation_key": continuation}
                payload = self._get(endpoint, params)
                batches.append(RawBatch(payload=payload, endpoint=endpoint, account_uid=account.uid,
                                        params=params, provider=self.provider))
                continuation = json.loads(payload).get("continuation_key")
                if not continuation:
                    break
        return batches

    def fetch_balances(self, account: AccountInfo) -> RawBatch:
        endpoint = f"/accounts/{account.uid}/balances"
        return RawBatch(payload=self._get(endpoint), endpoint=endpoint, account_uid=account.uid,
                        provider=self.provider)

    def consent_status(self) -> ConsentInfo:
        session = self._session()
        access = session.get("access") or {}
        uids = tuple(a if isinstance(a, str) else a.get("uid") for a in session.get("accounts", []))
        return _consent(_parse_datetime(access.get("valid_until")), str(session.get("status", "UNKNOWN")),
                        uids, self._today)


REGISTRY: dict[str, type] = {"mock": MockEnableBanking, "enablebanking": EnableBanking}


def get_bank_provider(name: str | None = None) -> BankProvider:
    """Provider from ``KASA_BANK_PROVIDER`` (or ``name``); without keys -> the mock, with a log entry."""
    chosen = (name or provider_name("bank")).strip().lower()
    if chosen == "enablebanking":
        try:
            return EnableBanking()
        except ProviderNotConfigured as exc:
            log.warning("%s - używam mock (dane testowe z fixtures)", exc)
            return MockEnableBanking()
    if chosen != "mock":
        log.warning("Nieznany dostawca banku %r - używam mock", chosen)
    return MockEnableBanking()
