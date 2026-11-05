"""The gate on the way OUT to the MCP client (a model): every text that may carry a person goes through
``kasa.core.pseudonym.pseudonymize_text`` (CLAUDE.md, rule 7; briefing, step 9).

* EVERY string of a result (at any depth, under any key - review of 4C/5, point 10) is pseudonymized;
  companies stay as named, persons become codes (``P-7f3a``), IBANs / phones / e-mails / PESEL / addresses too;
  only strings that cannot name a person by their form (ids, digests, dates, numbers, amounts, pseudonyms)
  skip the gate - an optimization, never a list of "text keys";
* free texts of the data are wrapped as ``{"untrusted_text": ...}`` by the views and stay wrapped;
* no gate (module missing, function missing, an error) -> ``GateUnavailable``: the tool refuses and
  returns no data at all - never the raw text.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import psycopg

from kasa.core.documents import privacy

# Every string of a result goes through the gate (review of 4C/5, point 10: a list of "text keys" missed
# whatever a tool returned under another key). What is skipped is only an optimization for strings that
# cannot name a person by their FORM - identifiers, digests, dates and moments, numbers and formatted amounts
# or percentages - or that sit under a key whose values are such forms by construction (ids, enums of the
# schema). A string that does not have such a form is pseudonymized even under a "safe" key.
SAFE_KEYS = frozenset({
    "id", "ref", "key", "kind", "status", "unit", "grain", "currency", "direction", "op_type", "final_status",
    "subtype", "intent", "badge", "method", "source", "channel", "check", "card_kind", "subject_type",
    "account_kind", "counterparty_kind", "record_type", "difference_kind", "line_kind", "status_method",
    "linked_by", "content_type", "implementation", "view", "tool", "bucket", "mpp_role", "period_mode",
})
SAFE_SUFFIXES = ("_id", "_ids", "_minor", "_at", "_date", "_sha256", "_share")
_SAFE_FORM = re.compile(
    r"^(?:"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"        # uuid
    r"|[0-9a-f]{32,64}"                                                     # digest
    r"|\d{4}-\d{2}-\d{2}(?:[T ][0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?)?"          # date / moment
    r"|\d{4}-\d{2}"                                                        # month
    r"|-?\d{1,8}(?:[.,]\d+)?"                                               # a number (never 9+ digits: a phone)
    r"|-?\d{1,3}(?:[ \u00a0]\d{3})*,\d{2}[ \u00a0]?(?:zł|PLN|EUR|USD)"       # a formatted amount
    r"|-?\d{1,3}(?:[.,]\d+)?[ \u00a0]?%"                                    # a percentage
    r"|\d{1,5} dni"                                                         # days
    r"|[A-Z]-[0-9a-f]{4,16}"                                                # a pseudonym
    r"|[a-z][a-z0-9_]*@\d+"                                                 # a metric ref
    r")$")
_SAFE_ENUM = re.compile(r"^[a-z][a-z0-9_]{0,40}$")                          # a lower-case enum / key of the schema


class GateUnavailable(RuntimeError):
    """The pseudonymization gate does not work - no data leaves kasa."""


class Outbound:
    """One tool call: a connection of the gate (owner role, never given to the agent) and the context."""

    def __init__(self, conn: psycopg.Connection, company_id: UUID):
        module = privacy.load_pseudonym_module()
        function = getattr(module, "pseudonymize_text", None) if module is not None else None
        loader = getattr(module, "load_context", None) if module is not None else None
        if function is None:
            raise GateUnavailable(f"bramka pseudonimizacji niedostępna ({privacy.PSEUDONYM_MODULE}.pseudonymize_text)")
        self.conn = conn
        self.company_id = company_id
        self._function = function
        try:
            self._context = loader(conn, company_id) if loader is not None else None
        except Exception as exc:  # noqa: BLE001 - any failure of the gate means: no data
            raise GateUnavailable(f"bramka pseudonimizacji nie działa ({type(exc).__name__})") from exc
        self.texts = 0
        self._cache: dict[str, str | None] = {}
        self._company_values = self._load_company_values()

    def _load_company_values(self) -> frozenset[str]:
        """Account numbers that are the company's own or of a firm (not a person): data of the company, kept as
        they are (the agent names the accounts by them). Nothing loaded = everything goes through the gate."""
        try:
            with self.conn.transaction():
                rows = self.conn.execute(
                    """
                    SELECT iban FROM l1_bank_account WHERE company_id = %(c)s AND iban IS NOT NULL
                    UNION
                    SELECT unnest(ibans) FROM l1_counterparty
                     WHERE company_id = %(c)s AND kind <> 'person' AND superseded_by IS NULL
                    """,
                    {"c": self.company_id},
                ).fetchall()
        except Exception:  # noqa: BLE001 - conservative: without the list every IBAN is coded
            return frozenset()
        return frozenset(str(r[0]).replace(" ", "") for r in rows if r[0])

    def text(self, value: str | None) -> str | None:
        if value is None or value == "":
            return value
        try:
            with self.conn.transaction():
                if self._context is not None:
                    result = self._function(self.conn, self.company_id, value, context=self._context)
                else:
                    result = self._function(self.conn, self.company_id, value)
        except Exception as exc:  # noqa: BLE001
            raise GateUnavailable(f"bramka pseudonimizacji zgłosiła błąd ({type(exc).__name__})") from exc
        self.texts += 1
        return privacy.as_text(result)

    @staticmethod
    def safe(value: str, key: str | None = None) -> bool:
        """A string that cannot name a person by its form (only an optimization of ``clean``)."""
        if _SAFE_FORM.match(value.strip()):
            return True
        if key is not None and (key in SAFE_KEYS or key.endswith(SAFE_SUFFIXES)) and _SAFE_ENUM.match(value):
            return True
        return False

    def clean(self, obj: Any, *, key: str | None = None, inside: bool = True) -> Any:
        """Pseudonymize EVERY string of a tool result, at any depth and under any key (keys of objects too).

        ``inside`` is kept for the callers of phase 5; it no longer narrows anything."""
        if isinstance(obj, dict):
            return {(self.clean(k) if isinstance(k, str) and not _SAFE_ENUM.match(k) else k): self.clean(v, key=k)
                    for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.clean(v, key=key) for v in obj]
        if isinstance(obj, str) and obj and not self.safe(obj, key) \
                and obj.replace(" ", "") not in self._company_values:
            if obj not in self._cache:
                self._cache[obj] = self.text(obj)
            return self._cache[obj]
        return obj


@contextmanager
def outbound(gate_factory, company_id: UUID) -> Iterator[Outbound]:
    """The gate of one tool call (its connection commits new pseudonym keys at the end)."""
    try:
        context = gate_factory()
        conn = context.__enter__()
    except Exception as exc:  # noqa: BLE001 - no connection of the gate = no gate
        raise GateUnavailable(f"bramka pseudonimizacji niedostępna ({type(exc).__name__})") from exc
    with _closing(context):
        yield Outbound(conn, company_id)


@contextmanager
def _closing(context) -> Iterator[None]:
    try:
        yield
    except BaseException as exc:
        if not context.__exit__(type(exc), exc, exc.__traceback__):
            raise
    else:
        context.__exit__(None, None, None)
