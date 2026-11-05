"""Sensitive words: a text with one of them never reaches a model (briefing, step 9).

Words are matched by stem with Polish inflection, never as substrings
(docs/PRZEGLADY.md, point 15): "alimentów", "komornicze", "związkowej",
"chorobowego", "zdrowotna" are found, "zapłata" and "faktura" are not.

* ``stem(word)`` - folded word (case and diacritics ignored, also when the text
  comes without Polish letters) without one inflection ending
  (-a, -u, -owi, -em, -ie, -y, -ów, -om, -ami, -ach, -ego, -emu, -ym, -ą, -ej, -ych
  and the adjective chain -ow-/-ow-ej...), minimum 4 letters left;
* a token matches a term stem when its stem starts with the term stem, or with the
  term stem after a regular Polish alternation of its last letter
  (komornik -> komornic-ze, związek -> związk-u);
* a phrase ("zajęcie wynagrodzenia") matches consecutive tokens only.

The list lives in the database (``sensitive_term``, migration 0008), seeded by
``sync_sensitive_terms`` (``python -m kasa init-db``) from the core list below and
from the extensions; terms added by a person (source ``human``) are read from there
too. Code that only has the code lists (no connection) uses ``code_terms``.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

import psycopg

log = logging.getLogger(__name__)

# Core list (privacy by default; any extension adds to it, never removes).
# category: health, union, enforcement, family, religion
CORE_TERMS: tuple[tuple[str, str], ...] = (
    ("zdrowie", "health"),
    ("związki", "union"),
    ("komornik", "enforcement"),
    ("alimenty", "family"),
    ("zajęcie wynagrodzenia", "enforcement"),
    ("egzekucja", "enforcement"),
    ("dłużnik", "enforcement"),
    ("choroba", "health"),
    ("chorobowe", "health"),
    ("leczenie", "health"),
    ("szpital", "health"),
    ("lekarz", "health"),
    ("terapia", "health"),
    ("rehabilitacja", "health"),
    ("niepełnosprawność", "health"),
    ("ciąża", "health"),
    ("składka związkowa", "union"),
    ("wyznanie", "religion"),
)

# Idioms that contain a sensitive stem but are not about it ("w związku z" = "in connection with").
SAFE_PHRASES: tuple[str, ...] = ("w związku z", "w zwiazku z", "w związku ze", "w zwiazku ze")

_SUFFIXES = tuple(sorted(
    {"a", "u", "owi", "em", "ie", "y", "ow", "om", "ami", "ach", "ego", "emu", "ym", "ej", "ych",
     # plural and neuter endings ("związki", "chorobowe")
     "i", "e",
     # adjective endings formed on -ow- / -n- ("związkowej", "chorobowego", "zdrowotna")
     "owa", "owe", "owy", "owej", "owego", "owemu", "owym", "owych", "owymi"},
    key=len, reverse=True,
))
# Regular alternations of the last stem letter in Polish inflection (term -> text).
_ALTERNATIONS: dict[str, tuple[str, ...]] = {
    "k": ("c", "cz"), "g": ("dz", "z"), "ch": ("sz",), "r": ("rz",), "rz": ("r",), "t": ("c",), "d": ("dz",),
    "ek": ("k",), "ec": ("c",),   # fleeting e: związek -> związku
}
_WORD_RE = re.compile(r"[^\W\d_]+")
_MIN_STEM = 4


def fold(text: str) -> str:
    """Lower case without diacritics ("Związkowej" -> "zwiazkowej")."""
    text = text.replace("ł", "l").replace("Ł", "L")
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def stem(word: str) -> str:
    """Crude Polish stem: folded word without one inflection ending (at least 4 letters stay)."""
    w = fold(word)
    for suffix in _SUFFIXES:
        if w.endswith(suffix) and len(w) - len(suffix) >= _MIN_STEM:
            return w[: -len(suffix)]
    return w


def phrase_stem(phrase: str) -> str:
    """Stems of a term's words joined by spaces (the ``sensitive_term.stem`` column)."""
    return " ".join(stem(w) for w in _WORD_RE.findall(phrase))


def _variants(term_stem: str) -> tuple[str, ...]:
    variants = [term_stem]
    for ending, replacements in _ALTERNATIONS.items():
        if term_stem.endswith(ending) and len(term_stem) - len(ending) >= _MIN_STEM - 1:
            variants += [term_stem[: -len(ending)] + r for r in replacements]
    return tuple(dict.fromkeys(variants))


def _token_variants(token_stem: str) -> tuple[str, ...]:
    if token_stem.endswith(("ek", "ec")) and len(token_stem) > _MIN_STEM + 1:
        return token_stem, token_stem[:-2] + token_stem[-1]       # fleeting e: związek ~ związk-
    return (token_stem,)


def token_matches(term_stem: str, token_stem: str) -> bool:
    """Does a text token (its stem) inflect the term stem?"""
    if len(term_stem) < _MIN_STEM:
        return token_stem == term_stem
    return any(t.startswith(v) for v in _variants(term_stem) for t in _token_variants(token_stem))


@dataclass(frozen=True)
class Term:
    term: str            # as written
    stem: str            # stems joined by spaces
    category: str
    source: str = "core"

    @classmethod
    def of(cls, term: str, category: str = "other", source: str = "core") -> Term:
        return cls(term=term.strip(), stem=phrase_stem(term), category=category, source=source)


def _strip_safe_phrases(folded: str) -> str:
    for phrase in SAFE_PHRASES:
        folded = re.sub(rf"\b{re.escape(fold(phrase))}\b", " ", folded)
    return folded


def find_sensitive(text: str | None, terms: Iterable[Term | str]) -> list[str]:
    """Terms (as written) found in ``text``, matched by stem; empty list when none."""
    if not text:
        return []
    folded = _strip_safe_phrases(fold(text))
    tokens = [stem(t) for t in _WORD_RE.findall(folded)]
    hits: list[str] = []
    for item in terms:
        term = item if isinstance(item, Term) else Term.of(item)
        parts = term.stem.split()
        if not parts:
            continue
        n = len(parts)
        for i in range(len(tokens) - n + 1):
            if all(token_matches(parts[j], tokens[i + j]) for j in range(n)):
                if term.term not in hits:
                    hits.append(term.term)
                break
    return hits


def categorize(term: str) -> str:
    """Category of an extension term: the category of the core term it inflects, else 'other'."""
    hits = find_sensitive(term, [Term.of(t, c) for t, c in CORE_TERMS])
    if hits:
        return dict(CORE_TERMS)[hits[0]]
    return "other"


def code_terms(extension_words: Sequence[str] | None = None) -> list[Term]:
    """Core terms plus the extension's words (no database)."""
    terms = [Term.of(t, c, "core") for t, c in CORE_TERMS]
    seen = {t.stem for t in terms}
    for word in extension_words or ():
        term = Term.of(word, categorize(word), "extension")
        if term.stem and term.stem not in seen:
            terms.append(term)
            seen.add(term.stem)
    return terms


# ---------------------------------------------------------------------------
# Database (sensitive_term, migration 0008)
# ---------------------------------------------------------------------------


def _table_exists(conn: psycopg.Connection) -> bool:
    return conn.execute("SELECT to_regclass('sensitive_term') IS NOT NULL").fetchone()[0]


def sync_sensitive_terms(conn: psycopg.Connection, companies: Iterable[tuple[UUID, Sequence[str]]] = (),
                         *, created_by: str = "kasa.init") -> int:
    """Seed ``sensitive_term``: the core list (global) and each company's extension words.

    ``companies`` = (company_id, extension words). Existing rows are kept as they are
    (a retired term stays retired). Returns the number of rows added.
    """
    added = 0
    for term in code_terms():
        added += conn.execute(
            """
            INSERT INTO sensitive_term (company_id, term, stem, category, source, created_by)
            VALUES (NULL, %s, %s, %s, 'core', %s) ON CONFLICT (company_id, stem) DO NOTHING
            """,
            (term.term, term.stem, term.category, created_by),
        ).rowcount
    core_stems = {t.stem for t in code_terms()}
    for company_id, words in companies:
        for term in code_terms(words):
            if term.source != "extension" or term.stem in core_stems:
                continue
            added += conn.execute(
                """
                INSERT INTO sensitive_term (company_id, term, stem, category, source, created_by)
                VALUES (%s, %s, %s, %s, 'extension', %s) ON CONFLICT (company_id, stem) DO NOTHING
                """,
                (company_id, term.term, term.stem, term.category, created_by),
            ).rowcount
    return added


def add_human_term(conn: psycopg.Connection, company_id: UUID | None, term: str, *, category: str | None = None,
                   created_by: str) -> Term:
    """A word a person added (e.g. from the review queue); it bypasses the model from now on."""
    item = Term.of(term, category or categorize(term), "human")
    conn.execute(
        """
        INSERT INTO sensitive_term (company_id, term, stem, category, source, created_by)
        VALUES (%s, %s, %s, %s, 'human', %s) ON CONFLICT (company_id, stem) DO NOTHING
        """,
        (company_id, item.term, item.stem, item.category, created_by),
    )
    return item


def load_terms(conn: psycopg.Connection, company_id: UUID | None,
               extension_words: Sequence[str] | None = None) -> list[Term]:
    """Every active term for a company: database rows (global + company) and the code lists.

    The code lists are always included, so a database that was never seeded still
    protects: the union is the safe direction.
    """
    terms = code_terms(extension_words)
    seen = {t.stem for t in terms}
    if _table_exists(conn):
        rows = conn.execute(
            """
            SELECT term, stem, category, source::text FROM sensitive_term
             WHERE retired_at IS NULL AND (company_id IS NULL OR company_id = %s)
             ORDER BY company_id NULLS FIRST, created_at, id
            """,
            (company_id,),
        ).fetchall()
        for term, stem_text, category, source in rows:
            if stem_text not in seen:
                terms.append(Term(term, stem_text, category, source))
                seen.add(stem_text)
    return terms
