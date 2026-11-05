"""Pseudonymization gate in front of every model call made by the documents channel.

The gate itself lives in ``kasa.core.pseudonym`` (bank/pseudonymization work,
migration 0006). This module only *uses* it, loaded dynamically:

* the module exists -> text goes through ``pseudonymize_text(conn, company_id, text)``;
* the module is missing or raises -> **no call** to a model that leaves the machine
  (briefing, step 9: "gdy bramka nie działa, wywołania nie ma"). The mock model
  never leaves the machine, so it may still read the text (with a warning).

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
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Protocol
from uuid import UUID

import psycopg

log = logging.getLogger(__name__)

PSEUDONYM_MODULE = "kasa.core.pseudonym"
PSEUDONYM_SECRET_ENV = "KASA_PSEUDONYM_SECRET"


class PseudonymizationUnavailable(RuntimeError):
    """The gate does not work, so the model is not called."""


class ModelLike(Protocol):
    name: str
    leaves_machine: bool


@dataclass(frozen=True)
class GateResult:
    text: str
    pseudonymized: bool
    note: str = ""


def load_pseudonym_module() -> ModuleType | None:
    """The pseudonymization module, or None when it does not exist (yet)."""
    try:
        return importlib.import_module(PSEUDONYM_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name in (PSEUDONYM_MODULE, PSEUDONYM_MODULE.rsplit(".", 1)[0]):
            return None
        raise


def _as_text(result: Any) -> str:
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


def gate_for_model(conn: psycopg.Connection, company_id: UUID, text: str, model: ModelLike) -> GateResult:
    """Text the model may see. Raises PseudonymizationUnavailable when the call must not happen."""
    module = load_pseudonym_module()
    function = getattr(module, "pseudonymize_text", None) if module is not None else None
    if function is None:
        if model.leaves_machine:
            raise PseudonymizationUnavailable(
                f"bramka pseudonimizacji niedostępna ({PSEUDONYM_MODULE}.pseudonymize_text) - "
                f"wywołania modelu {model.name} nie ma"
            )
        log.warning("Brak bramki pseudonimizacji - model %s jest lokalny (mock), tekst nie opuszcza maszyny.",
                    model.name)
        return GateResult(text, False, "brak bramki; model lokalny (mock) - tekst nie opuścił maszyny")
    try:
        with conn.transaction():
            pseudonymized = _as_text(function(conn, company_id, text))
    except Exception as exc:  # any failure of the gate means: no call
        if model.leaves_machine:
            raise PseudonymizationUnavailable(
                f"bramka pseudonimizacji zgłosiła błąd ({exc}) - wywołania modelu {model.name} nie ma"
            ) from exc
        log.warning("Bramka pseudonimizacji zgłosiła błąd (%s) - model lokalny (mock) czyta tekst.", exc)
        return GateResult(text, False, f"bramka zgłosiła błąd: {exc}; model lokalny (mock)")
    return GateResult(pseudonymized, True)


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


def sensitive_hits(text: str, extension_words: list[str]) -> list[str]:
    """Sensitive words in the text (word stems when the pseudonym module offers it, else substrings)."""
    module = load_pseudonym_module()
    hits = getattr(module, "sensitive_hits", None) if module is not None else None
    words_for = getattr(module, "sensitive_words_for", None) if module is not None else None
    if hits is not None and words_for is not None:
        return list(hits(text, words_for(extension_words)))
    folded = text.casefold()
    return [word for word in extension_words if word.casefold() in folded]


_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(message)
