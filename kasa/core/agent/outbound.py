"""The gate on the way OUT to the MCP client (a model): every text that may carry a person goes through
``kasa.core.pseudonym.pseudonymize_text`` (CLAUDE.md, rule 7; briefing, step 9).

* texts of the data (titles, names, labels, rank reasons, facts of suggestions, drafts) are pseudonymized;
  companies stay as named, persons become codes (``P-7f3a``), IBANs / phones / e-mails / PESEL / addresses too;
* free texts of the data are wrapped as ``{"untrusted_text": ...}`` by the views and stay wrapped;
* no gate (module missing, function missing, an error) -> ``GateUnavailable``: the tool refuses and
  returns no data at all - never the raw text.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import psycopg

from kasa.core.documents import privacy

# keys whose string values are data texts (may name a person) - everything under them goes through the gate
TEXT_KEYS = frozenset({
    "untrusted_text", "title", "counterparty", "label", "subject_label", "rank_reason", "facts", "question",
    "text", "draft", "reason", "summary", "name", "line_name", "lines", "party", "seller_name", "buyer_name",
    "query", "rationale", "examples", "exceptions", "notes", "description", "account", "cost_object_ref",
})
# keys that are never data texts (ids, enums, dates, amounts already formatted, definitions of the layer)
SAFE_SUFFIXES = ("_id", "_ids", "_pl", "_minor", "_at", "_date", "_sha256", "_share")


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

    def clean(self, obj: Any, *, key: str | None = None, inside: bool = False) -> Any:
        """Pseudonymize every data text of a tool result (recursively)."""
        if isinstance(obj, dict):
            return {k: self.clean(v, key=k, inside=inside or k in TEXT_KEYS) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.clean(v, key=key, inside=inside) for v in obj]
        if isinstance(obj, str) and inside and not (key and key.endswith(SAFE_SUFFIXES)):
            return self.text(obj)
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
