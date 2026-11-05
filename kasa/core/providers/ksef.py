"""KSeF provider: invoices (FA(3) XML) of a company and the list of invoice numbers.

Two implementations, chosen by ``KASA_KSEF_PROVIDER``:

* ``mock`` (default) - :class:`MockKsef` reads ``<fixtures>/<company slug>/ksef/``
  (``KASA_FIXTURES_DIR``, default ``fixtures/`` in the repository);
* ``ksef`` - :class:`Ksef`, a client of the KSeF 2.0 API
  (``https://ksef.mf.gov.pl/api/v2``) authenticated with a KSeF token
  (``KASA_KSEF_TOKEN``) or a KSeF certificate (``KASA_KSEF_CERT_PATH``).

Missing configuration never raises at startup: the factory logs a warning and
falls back to the mock. Kasa only reads from KSeF - it never issues invoices.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time as time_module
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from kasa.core.normalize import to_minor
from kasa.core.providers import provider_name

try:  # shared exception, if the providers package defines one
    from kasa.core.providers import ProviderNotConfigured  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - depends on the other providers

    class ProviderNotConfigured(RuntimeError):
        """The real provider has no credentials; the caller falls back to the mock."""


log = logging.getLogger(__name__)

WARSAW = ZoneInfo("Europe/Warsaw")
FIXTURES_DIR_ENV = "KASA_FIXTURES_DIR"
DEFAULT_FIXTURES_DIR = Path(__file__).resolve().parents[3] / "fixtures"

Direction = Literal["sales", "purchase"]
# KSeF subject types: Subject1 = we are the seller, Subject2 = we are the buyer.
SUBJECT_TYPE = {"sales": "Subject1", "purchase": "Subject2"}


class CompanyLike(Protocol):
    slug: str


@dataclass(frozen=True)
class RawInvoice:
    """One invoice as KSeF serves it: the XML exactly as fetched."""

    ksef_number: str
    xml: bytes
    accepted_at: datetime
    direction: Direction
    meta: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class KsefInvoiceRef:
    """One row of the KSeF invoice list (metadata, no XML) - input of the completeness check."""

    ksef_number: str
    direction: Direction
    accepted_at: datetime
    invoice_number: str | None
    issue_date: date | None
    currency: str
    gross_minor: int


@runtime_checkable
class KsefProvider(Protocol):
    name: str

    def subject_nip(self, company: CompanyLike) -> str | None:
        """NIP of the KSeF context (our company), when the provider knows it."""

    def fetch_invoices(self, company: CompanyLike, date_from: date, date_to: date,
                       direction: Direction | None = None) -> list[RawInvoice]:
        """Invoices accepted by KSeF between the dates (inclusive, Polish time)."""

    def list_invoice_numbers(self, company: CompanyLike, date_from: date, date_to: date) -> list[KsefInvoiceRef]:
        """The list KSeF has for the window, both directions (for the completeness check)."""


def _in_window(moment: datetime, date_from: date, date_to: date) -> bool:
    return date_from <= moment.astimezone(WARSAW).date() <= date_to


def fixtures_dir() -> Path:
    return Path(os.environ.get(FIXTURES_DIR_ENV) or DEFAULT_FIXTURES_DIR).expanduser().resolve()


class MockKsef:
    """KSeF made of fixture files: ``<fixtures>/<slug>/ksef/index.json`` + the XML files it lists."""

    name = "mock"

    def __init__(self, root: Path | None = None):
        self.root = root

    def _dir(self, company: CompanyLike) -> Path:
        return (self.root or fixtures_dir()) / company.slug / "ksef"

    def _index(self, company: CompanyLike) -> dict[str, Any]:
        path = self._dir(company) / "index.json"
        if not path.is_file():
            log.info("Mock KSeF: brak %s - firma nie ma faktur w danych testowych", path)
            return {"invoices": []}
        return json.loads(path.read_text(encoding="utf-8"))

    def subject_nip(self, company: CompanyLike) -> str | None:
        return self._index(company).get("subject_nip")

    def _entries(self, company: CompanyLike, date_from: date, date_to: date,
                 direction: Direction | None) -> list[dict[str, Any]]:
        entries = []
        for entry in self._index(company)["invoices"]:
            accepted = datetime.fromisoformat(entry["acceptance_timestamp"])
            if direction and entry["direction"] != direction:
                continue
            if _in_window(accepted, date_from, date_to):
                entries.append(entry)
        return sorted(entries, key=lambda e: (e["acceptance_timestamp"], e["ksef_number"]))

    def fetch_invoices(self, company: CompanyLike, date_from: date, date_to: date,
                       direction: Direction | None = None) -> list[RawInvoice]:
        result = []
        for entry in self._entries(company, date_from, date_to, direction):
            xml = (self._dir(company) / entry["file"]).read_bytes()
            digest = hashlib.sha256(xml).hexdigest()
            if entry.get("sha256") and digest != entry["sha256"]:
                raise ValueError(f"Mock KSeF: plik {entry['file']} ma inny SHA-256 niż w index.json")
            result.append(RawInvoice(
                ksef_number=entry["ksef_number"],
                xml=xml,
                accepted_at=datetime.fromisoformat(entry["acceptance_timestamp"]),
                direction=entry["direction"],
                meta={"offline": entry.get("offline", False), "file": entry["file"]},
            ))
        return result

    def list_invoice_numbers(self, company: CompanyLike, date_from: date, date_to: date) -> list[KsefInvoiceRef]:
        return [
            KsefInvoiceRef(
                ksef_number=entry["ksef_number"],
                direction=entry["direction"],
                accepted_at=datetime.fromisoformat(entry["acceptance_timestamp"]),
                invoice_number=entry.get("invoice_number"),
                issue_date=date.fromisoformat(entry["issue_date"]) if entry.get("issue_date") else None,
                currency=entry["currency"],
                gross_minor=to_minor(Decimal(entry["gross"]), entry["currency"]),
            )
            for entry in self._entries(company, date_from, date_to, None)
        ]


class Ksef:
    """Client of the KSeF 2.0 API (read only). Skeleton - not exercised by tests.

    Flow (KSeF 2.0): ``POST /auth/challenge`` -> authentication with a KSeF token
    (``POST /auth/ksef-token``, token encrypted with the MF public key) or with a
    signed XAdES request (``POST /auth/xades-signature``, KSeF certificate) ->
    ``GET /auth/{referenceNumber}`` until done -> ``POST /auth/token/redeem`` gives
    the access token. Invoices: ``POST /invoices/query/metadata`` (list, paged)
    and ``GET /invoices/ksef/{ksefNumber}`` (XML), or a package
    ``POST /invoices/exports`` + ``GET /invoices/exports/{referenceNumber}``
    (encrypted ZIP parts) for large windows.
    """

    name = "ksef"
    BASE_URLS: ClassVar[dict[str, str]] = {
        "prod": "https://ksef.mf.gov.pl/api/v2",
        "test": "https://ksef-test.mf.gov.pl/api/v2",
        "demo": "https://ksef-demo.mf.gov.pl/api/v2",
    }
    PAGE_SIZE = 100

    def __init__(self, *, nip: str, token: str | None = None, cert_path: str | None = None,
                 key_path: str | None = None, key_password: str | None = None,
                 base_url: str = BASE_URLS["prod"], timeout: float = 60.0):
        if not nip:
            raise ProviderNotConfigured("brak KASA_KSEF_NIP (NIP firmy, w której kontekście czytamy KSeF)")
        if not token and not cert_path:
            raise ProviderNotConfigured("brak KASA_KSEF_TOKEN ani KASA_KSEF_CERT_PATH")
        if cert_path and not Path(cert_path).expanduser().is_file():
            raise ProviderNotConfigured(f"plik certyfikatu KSeF nie istnieje: {cert_path}")
        self.nip = nip
        self.token = token
        self.cert_path = cert_path
        self.key_path = key_path
        self.key_password = key_password
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._access_token: str | None = None
        self._access_valid_until: datetime | None = None

    @classmethod
    def from_env(cls) -> Ksef:
        env = os.environ
        base_url = env.get("KASA_KSEF_BASE_URL") or cls.BASE_URLS.get(env.get("KASA_KSEF_ENV", "prod"))
        if not base_url:
            raise ProviderNotConfigured(f"nieznane KASA_KSEF_ENV={env.get('KASA_KSEF_ENV')!r} (prod|test|demo)")
        return cls(
            nip=env.get("KASA_KSEF_NIP", ""),
            token=env.get("KASA_KSEF_TOKEN") or None,
            cert_path=env.get("KASA_KSEF_CERT_PATH") or None,
            key_path=env.get("KASA_KSEF_KEY_PATH") or None,
            key_password=env.get("KASA_KSEF_KEY_PASSWORD") or None,
            base_url=base_url,
        )

    # -- HTTP -----------------------------------------------------------------

    def _request(self, method: str, path: str, *, body: Any = None, bearer: str | None = None,
                 accept: str = "application/json", content_type: str = "application/json",
                 raw_body: bytes | None = None, absolute: bool = False) -> bytes:
        import urllib.error
        import urllib.request

        url = path if absolute else f"{self.base_url}{path}"
        data = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", accept)
        if data is not None:
            request.add_header("Content-Type", content_type)
        if bearer:
            request.add_header("Authorization", f"Bearer {bearer}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:2000]
            raise RuntimeError(f"KSeF {method} {path}: HTTP {exc.code} {detail}") from exc

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        return json.loads(self._request(method, path, **kwargs) or b"{}")

    # -- authentication ---------------------------------------------------------

    def _session(self) -> str:
        """Access token, authenticating when there is none or it expired."""
        now = datetime.now().astimezone()
        if self._access_token and self._access_valid_until and self._access_valid_until > now + timedelta(minutes=1):
            return self._access_token
        challenge = self._json("POST", "/auth/challenge")
        context = {"type": "Nip", "value": self.nip}
        if self.token:
            started = self._json("POST", "/auth/ksef-token", body={
                "challenge": challenge["challenge"],
                "contextIdentifier": context,
                "encryptedToken": self._encrypt_token(challenge),
            })
        else:
            started = json.loads(self._request(
                "POST", "/auth/xades-signature", raw_body=self._signed_auth_request(challenge),
                content_type="application/xml",
            ))
        auth_token = started["authenticationToken"]["token"]
        reference = started["referenceNumber"]
        for _ in range(60):
            status = self._json("GET", f"/auth/{reference}", bearer=auth_token)
            code = status.get("status", {}).get("code")
            if code == 200:
                break
            if code and code >= 400:
                raise RuntimeError(f"KSeF odrzucił uwierzytelnienie: {status}")
            time_module.sleep(1)
        else:
            raise RuntimeError("KSeF: uwierzytelnienie nie zakończyło się w 60 s")
        tokens = self._json("POST", "/auth/token/redeem", bearer=auth_token)
        self._access_token = tokens["accessToken"]["token"]
        self._access_valid_until = datetime.fromisoformat(tokens["accessToken"]["validUntil"])
        return self._access_token

    def _encrypt_token(self, challenge: dict[str, Any]) -> str:
        """``token|timestampMs`` encrypted with RSA-OAEP (SHA-256) and the MF public key."""
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError as exc:
            raise ProviderNotConfigured("brak biblioteki cryptography (uv pip install cryptography)") from exc
        certificates = json.loads(self._request("GET", "/security/public-key-certificates"))
        cert = next(c for c in certificates if "KsefTokenEncryption" in c.get("usage", []))
        public_key = x509.load_der_x509_certificate(base64.b64decode(cert["certificate"])).public_key()
        timestamp_ms = challenge.get("timestampMs") or int(
            datetime.fromisoformat(challenge["timestamp"]).timestamp() * 1000
        )
        payload = f"{self.token}|{timestamp_ms}".encode()
        encrypted = public_key.encrypt(
            payload, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
        )
        return base64.b64encode(encrypted).decode()

    def _signed_auth_request(self, challenge: dict[str, Any]) -> bytes:
        """AuthTokenRequest signed with XAdES (KSeF certificate). Needs signxml + the private key."""
        try:
            from signxml.xades import XAdESSigner  # noqa: F401
        except ImportError as exc:
            raise ProviderNotConfigured(
                "logowanie certyfikatem wymaga biblioteki signxml; użyj tokenu KSeF (KASA_KSEF_TOKEN)"
            ) from exc
        raise NotImplementedError("podpis XAdES żądania AuthTokenRequest - do uzupełnienia z certyfikatem")

    # -- invoices ---------------------------------------------------------------

    def _query(self, direction: Direction, date_from: date, date_to: date) -> list[dict[str, Any]]:
        start = datetime.combine(date_from, time.min, WARSAW)
        end = datetime.combine(date_to, time.max, WARSAW)
        found: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._json(
                "POST", f"/invoices/query/metadata?pageOffset={offset}&pageSize={self.PAGE_SIZE}",
                bearer=self._session(),
                body={
                    "subjectType": SUBJECT_TYPE[direction],
                    "dateRange": {"dateType": "PermanentStorage", "from": start.isoformat(), "to": end.isoformat()},
                },
            )
            found.extend(page.get("invoices", []))
            if not page.get("hasMore"):
                if page.get("isTruncated"):
                    log.warning("KSeF obciął wynik zapytania (%s %s-%s) - zawęź okno", direction, date_from, date_to)
                return found
            offset += 1

    @staticmethod
    def _accepted(meta: dict[str, Any]) -> datetime:
        stamp = meta.get("permanentStorageDate") or meta.get("acquisitionDate") or meta.get("invoicingDate")
        return datetime.fromisoformat(stamp)

    def list_invoice_numbers(self, company: CompanyLike, date_from: date, date_to: date) -> list[KsefInvoiceRef]:
        refs = []
        for direction in ("sales", "purchase"):
            for meta in self._query(direction, date_from, date_to):
                currency = meta.get("currency") or "PLN"
                refs.append(KsefInvoiceRef(
                    ksef_number=meta["ksefNumber"],
                    direction=direction,
                    accepted_at=self._accepted(meta),
                    invoice_number=meta.get("invoiceNumber"),
                    issue_date=date.fromisoformat(meta["issueDate"][:10]) if meta.get("issueDate") else None,
                    currency=currency,
                    gross_minor=to_minor(Decimal(str(meta["grossAmount"])), currency),
                ))
        return refs

    def fetch_invoices(self, company: CompanyLike, date_from: date, date_to: date,
                       direction: Direction | None = None) -> list[RawInvoice]:
        invoices = []
        for side in ([direction] if direction else ["sales", "purchase"]):
            for meta in self._query(side, date_from, date_to):
                xml = self._request("GET", f"/invoices/ksef/{meta['ksefNumber']}", bearer=self._session(),
                                    accept="application/xml")
                invoices.append(RawInvoice(ksef_number=meta["ksefNumber"], xml=xml,
                                           accepted_at=self._accepted(meta), direction=side, meta=meta))
        return invoices

    def subject_nip(self, company: CompanyLike) -> str | None:
        return self.nip


def get_ksef_provider(name: str | None = None) -> KsefProvider:
    """Provider configured by KASA_KSEF_PROVIDER; never raises for missing keys."""
    name = (name or provider_name("ksef")).lower()
    if name == "ksef":
        try:
            return Ksef.from_env()
        except ProviderNotConfigured as exc:
            log.warning("KSeF nie jest skonfigurowany (%s) - używam danych testowych (mock).", exc)
            return MockKsef()
    if name != "mock":
        log.warning("Nieznany dostawca KSeF %r - używam danych testowych (mock).", name)
    return MockKsef()


REGISTRY = {"mock": MockKsef, "ksef": Ksef}
