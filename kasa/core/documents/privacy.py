"""Access to the pseudonymization module for the documents channel and the gate.

The gate itself lives in ``kasa.core.pseudonym`` (migration 0006) and the only way
to a model is ``kasa.core.semantics.gate.guarded_complete``, which loads the module
through :func:`load_pseudonym_module`:

* the module exists -> text goes through ``pseudonymize_text(conn, company_id, text)``;
* the module is missing or raises -> **no call** to any model, the local mock
  included (briefing, step 9: "gdy bramka nie działa, wywołania nie ma").

``person_pseudonym`` gives the stable code of a person named in an internal file
(PM sheet): the pseudonym module's code when it offers one, otherwise an HMAC of
the normalized name (temporary, documented in docs/DOKUMENTY.md).
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import logging
import os
import re
import unicodedata
from types import ModuleType
from typing import Any
from uuid import UUID

import psycopg

log = logging.getLogger(__name__)

PSEUDONYM_MODULE = "kasa.core.pseudonym"
PSEUDONYM_SECRET_ENV = "KASA_PSEUDONYM_SECRET"


class PseudonymizationUnavailable(RuntimeError):
    """The gate does not work, so the model is not called."""


def load_pseudonym_module() -> ModuleType | None:
    """The pseudonymization module, or None when it does not exist."""
    try:
        return importlib.import_module(PSEUDONYM_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name in (PSEUDONYM_MODULE, PSEUDONYM_MODULE.rsplit(".", 1)[0]):
            return None
        raise


def as_text(result: Any) -> str:
    """The text part of what ``pseudonymize_text`` returned ((text, hits), dict or object)."""
    if isinstance(result, str):
        return result
    if isinstance(result, (tuple, list)) and result and isinstance(result[0], str):
        return result[0]
    if isinstance(result, dict) and isinstance(result.get("text"), str):
        return result["text"]
    text = getattr(result, "text", None)
    if isinstance(text, str):
        return text
    raise TypeError(f"pseudonymize_text zwróciło {type(result).__name__}, oczekiwano tekstu")


def normalize_person(name: str) -> str:
    text = unicodedata.normalize("NFKC", name).casefold()
    return re.sub(r"\s+", " ", text).strip()


def hmac_pseudonym(company_id: UUID, value: str, kind: str = "person") -> str:
    """Stable code ``P-xxxxxxxx`` of a value (HMAC-SHA256; key from env or the company id)."""
    secret = os.environ.get(PSEUDONYM_SECRET_ENV) or f"kasa:{company_id}"
    digest = hmac.new(secret.encode(), f"{kind}:{normalize_person(value)}".encode(), hashlib.sha256).hexdigest()
    return f"P-{digest[:8]}"


def person_pseudonym(conn: psycopg.Connection, company_id: UUID, name: str | None) -> tuple[str | None, str]:
    """(pseudonym, method) of a person's name. Method: 'pseudonym_module' or 'hmac'."""
    if not name or not name.strip():
        return None, "none"
    module = load_pseudonym_module()
    register = getattr(module, "register_person", None) if module is not None else None
    if register is not None:
        try:
            with conn.transaction():
                code = register(conn, company_id, name)
        except Exception as exc:  # noqa: BLE001 - no pseudonym_key (0006) or a failing gate: HMAC code
            _warn_once("person", f"Moduł pseudonimizacji nie nadał kodu osobie ({exc}) - używam HMAC.")
        else:
            if isinstance(code, str) and code and code != name:
                return code, "pseudonym_module"
    return hmac_pseudonym(company_id, name), "hmac"


_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(message)
