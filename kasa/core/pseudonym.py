"""Pseudonymization gate: personal data is replaced by stable codes before any text
may reach a model (briefing, step 9; CLAUDE.md rule 7).

* ``pseudonymize_text(conn, company_id, text)`` finds IBANs (mod-97), Polish phone
  numbers, e-mails, PESEL numbers (checksum), street addresses and names of
  persons, and replaces each with a code such as ``P-7f3a``.
* The same value always gets the same code: the key is HMAC-SHA256 of the
  normalized value (``pseudonym_key.value_hash``), the value itself is stored
  only encrypted (pgcrypto ``pgp_sym_encrypt``). Both keys derive from the secret
  in ``KASA_PSEUDONYM_SECRET``; without it a development key is used and a
  warning is logged.
* ``depseudonymize`` reverses the codes, for internal roles only.
* ``sensitive_hits`` tells whether a text contains a sensitive word (matched by
  word stem, not by substring); such a text bypasses the model entirely.

Names of persons are found by code, without a model:
1. known persons (every ``person`` row in ``pseudonym_key``: bank counterparties,
   rows of internal sheets) in any word order, and their surnames alone;
2. a Polish first name next to a capitalized word that is neither a safe word
   nor a word of a company name from the counterparty registry;
3. two capitalized words (``Xxxx Yyyy``) that are neither safe words, nor words of
   a company name, nor first names.
Over-pseudonymizing is the safe direction: a missed surname is a leak, an extra
code only costs the model some context.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from uuid import UUID

import psycopg

from kasa.core.normalize import is_valid_iban, normalize_iban

log = logging.getLogger(__name__)

SECRET_ENV = "KASA_PSEUDONYM_SECRET"
_DEV_SECRET = "kasa-dev-pseudonym-secret-not-for-production"
_warned_dev_secret = False

KINDS = ("person", "iban", "phone", "email", "pesel", "address")
PREFIX = {"person": "P", "iban": "I", "phone": "T", "email": "E", "pesel": "N", "address": "A"}
PSEUDONYM_RE = re.compile(r"\b([PITENA])-([0-9a-f]{4,16})\b")
# Roles that may see personal data again (the company, the operator, the
# accounting office on the company's order). The agent and models never may.
INTERNAL_ROLES = frozenset({"owner", "operator", "accountant"})


class PseudonymizationError(RuntimeError):
    """The gate cannot work (no key, database error): no model call may happen."""


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def _secret() -> bytes:
    global _warned_dev_secret
    value = os.environ.get(SECRET_ENV, "").strip()
    if not value:
        if not _warned_dev_secret:
            log.warning(
                "%s nie jest ustawiony - używam klucza deweloperskiego pseudonimizacji. "
                "Na produkcji ustaw własny sekret.", SECRET_ENV,
            )
            _warned_dev_secret = True
        value = _DEV_SECRET
    return value.encode("utf-8")


def _subkey(purpose: str) -> bytes:
    return hmac.new(_secret(), f"kasa-pseudonym:{purpose}".encode(), hashlib.sha256).digest()


def value_hash(kind: str, normalized: str) -> str:
    """HMAC-SHA256 (hex) of a normalized value; the lookup key of pseudonym_key."""
    if kind not in KINDS:
        raise ValueError(f"unknown pseudonym kind: {kind!r}")
    return hmac.new(_subkey("hash"), f"{kind}:{normalized}".encode(), hashlib.sha256).hexdigest()


def _encryption_key() -> str:
    return _subkey("encrypt").hex()


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def fold(text: str) -> str:
    """Case- and diacritics-insensitive form ("Wójcicka" -> "wojcicka")."""
    text = text.replace("ł", "l").replace("Ł", "L")
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W\d_]+", text)


def normalize_value(kind: str, value: str) -> str:
    """Canonical form of a personal value: what is hashed."""
    match kind:
        case "person":
            # word order does not matter: "Lewandowicz Maja" = "MAJA LEWANDOWICZ"
            return " ".join(sorted(fold(w) for w in _words(value)))
        case "iban":
            return normalize_iban(value, validate=False)
        case "phone":
            digits = re.sub(r"\D", "", value)
            if digits.startswith("0048") and len(digits) == 13:
                digits = digits[4:]
            elif digits.startswith("48") and len(digits) == 11:
                digits = digits[2:]
            return digits
        case "email":
            return value.strip().casefold()
        case "pesel":
            return re.sub(r"\D", "", value)
        case "address":
            return " ".join(fold(value).replace(".", " ").split())
    raise ValueError(f"unknown pseudonym kind: {kind!r}")


# ---------------------------------------------------------------------------
# Dictionaries
# ---------------------------------------------------------------------------

# Common Polish first names (folded). Used to recognise "Imię Nazwisko" and
# "Nazwisko Imię" in titles and counterparty names.
FIRST_NAMES = frozenset(fold(n) for n in """
Ada Adam Adrian Adrianna Agata Agnieszka Aleksander Aleksandra Aleksy Alicja Alina Amelia Anastazja Andrzej
Aneta Angelika Aniela Anita Anna Antoni Antonina Arkadiusz Artur Barbara Bartosz Bartłomiej Beata Bernadeta
Błażej Bogdan Bogumiła Bogusław Bożena Bronisław Cezary Celina Czesław Damian Daniel Daniela Danuta Dariusz
Dawid Dominik Dominika Dorota Edward Edyta Elżbieta Emil Emilia Ewa Ewelina Fabian Filip Franciszek Gabriel
Gabriela Grażyna Grzegorz Halina Hanna Henryk Helena Hubert Ignacy Igor Ilona Irena Iwona Izabela Jacek Jadwiga
Jakub Jan Janina Janusz Jarosław Jerzy Joanna Jolanta Józef Julia Julian Julita Justyna Kacper Kalina Kamil
Kamila Karol Karolina Kasia Katarzyna Kazimierz Kinga Klara Klaudia Konrad Kornelia Krystian Krystyna Krzysztof
Ksawery Laura Lena Leon Leszek Liliana Lucyna Ludwik Łucja Łukasz Maciej Magdalena Maja Maksymilian Malwina
Małgorzata Marcel Marcelina Marcin Marek Maria Marian Marianna Mariusz Marta Martyna Marzena Mateusz Michał
Michalina Mieczysław Mikołaj Milena Mirosław Mirosława Monika Nadia Natalia Natasza Nikola Nikodem Norbert Olaf
Oliwer Oliwia Patrycja Patryk Paula Paulina Paweł Piotr Przemysław Radosław Rafał Renata Robert Roksana Roman
Róża Ryszard Sabina Sandra Sebastian Szymon Sławomir Stanisław Stefan Sylwia Szczepan Tadeusz Teresa Tomasz
Tymon Tymoteusz Urszula Wacław Waldemar Wanda Weronika Wiesław Wiktor Wiktoria Wincenty Witold Władysław Wojciech
Zbigniew Zdzisław Zenon Zofia Zuzanna Zygmunt Żaneta
""".split())

# Words that look like names when capitalized but are not persons (folded):
# months, days, cities, institutions and the usual vocabulary of transfer titles
# and company names. Exact forms only: a stem would also swallow surnames.
SAFE_WORDS = frozenset(fold(w) for w in """
styczen stycznia styczniu luty lutego lutym marzec marca marcu kwiecien kwietnia kwietniu maj maja maju
czerwiec czerwca czerwcu lipiec lipca lipcu sierpien sierpnia sierpniu wrzesien wrzesnia wrzesniu
pazdziernik pazdziernika pazdzierniku listopad listopada listopadzie grudzien grudnia grudniu
poniedzialek wtorek sroda czwartek piatek sobota niedziela
warszawa warszawy warszawie warszawe krakow krakowa krakowie gdansk gdanska gdansku gdynia gdyni sopot sopotu
wroclaw wroclawia wroclawiu lodz lodzi poznan poznania poznaniu katowice katowic katowicach szczecin szczecina
szczecinie lublin lublina lublinie bydgoszcz bydgoszczy bialystok bialegostoku torun torunia toruniu kielce kielc
rzeszow rzeszowa rzeszowie olsztyn olsztyna opole opola gliwice zabrze bytom czestochowa radom sosnowiec
zakopane zakopanem sopocie mokotow mokotowa mokotowie praga pragi pradze wola woli ursynow wilanow
polska polski polsce poland europe europa berlin londyn london paris dublin tallinn wilno vilnius amsterdam
san francisco new york
faktura faktury fakture fakturze fakt zaplata zaplaty przelew przelewu wlasny wlasna czynsz czynszu zwrot
zwrotu zaliczka zaliczki rachunek rachunku umowa umowy wynagrodzenie wynagrodzenia prowizja prowizji oplata
oplaty kapitalizacja odsetki odsetek polecenie rata raty skladki skladka korekta korekty nota noty zamowienie
zamowienia oferta oferty projekt projektu dostawa uslugi usluga sprzedaz zakup najem wynajem kaucja depozyt
rozliczenie zlecenie dzielo premia delegacja paragon
gala summit roadshow partner partners konferencja targi kongres festiwal piknik warsztaty szkolenie gala
express elixir sorbnet blik sepa swift
hotel studio biuro centrum agencja grupa group dom dwor palac zamek sala loft przestrzen drukarnia kancelaria
catering leasing media software development retail international team labs holding invest capital consulting
solutions systems services serwis technika produkcja fundacja stowarzyszenie spoldzielnia apteka sklep market
restauracja bistro kawiarnia pizzeria
bank banku urzad urzedu skarbowy skarbowego zaklad zakladu ubezpieczen spolecznych komornik komornika sadowy
sadowego sad sadu sadzie rejonowy rejonowym rejonowego okregowy okregowego gmina gminy miasto miasta powiat
wojewodztwo starostwo ministerstwo narodowy narodowego fundusz funduszu zdrowia
rachunkowe rachunkowy ksiegowe ksiegowy bilans
""".split())

# Words that make a name a company (folded, without dots).
COMPANY_MARKERS = frozenset(
    "sp spolka zoo z o oo sa s a sj sk ska sc ltd llc inc gmbh ag bv nv uab sas sarl plc oy ab as se spa kg co".split()
)

_CAPITAL_COMPANY_RE = re.compile(
    r"\bsp\.?\s*z\s*o\.?\s*o\b|\bs\.?\s?k\.?\s?a\b|\bs\.\s?a\b\.?|\bs\.?a\.?$|\bp\.?s\.?a\b"
    r"|\b(?:gmbh|ltd|limited|inc|llc|uab|ag|plc|bv|nv|sarl|sas|spa|oy|ab|se|corp|corporation)\b",
    re.IGNORECASE,
)


def is_capital_company(name: str | None) -> bool:
    """Name of a legal entity (sp. z o.o., S.A., GmbH, Ltd ...), not of a natural person."""
    return bool(name) and bool(_CAPITAL_COMPANY_RE.search(fold(name)))


# Sensitive words added by the core to the extension's list (stems are matched,
# so "komornik" also finds "komornicze", "komornika").
EXTRA_SENSITIVE_WORDS: tuple[str, ...] = (
    "zajęcie wynagrodzenia",
    "egzekucja",
    "dłużnik",
    "choroba",
    "leczenie",
    "szpital",
    "lekarz",
    "terapia",
    "rehabilitacja",
    "niepełnosprawność",
    "ciąża",
    "składka związkowa",
    "wyznanie",
)

_SUFFIXES = tuple(sorted(
    {"owego", "owemu", "owych", "owymi", "ami", "ach", "owi", "owa", "owe", "owy", "ego", "emu", "ich", "ymi",
     "imi", "om", "ow", "ie", "ia", "ii", "iu", "ek", "ki", "y", "i", "a", "e", "o", "u"},
    key=len, reverse=True,
))


def stem(word: str) -> str:
    """Crude Polish stem: folded word without its inflection ending (min. 4 letters)."""
    w = fold(word)
    for suffix in _SUFFIXES:
        if w.endswith(suffix) and len(w) - len(suffix) >= 4:
            return w[: -len(suffix)]
    return w


def _stems_match(word_stem: str, token_stem: str) -> bool:
    if len(word_stem) < 4 or len(token_stem) < 4:
        return word_stem == token_stem
    common = 0
    for a, b in zip(word_stem, token_stem, strict=False):
        if a != b:
            break
        common += 1
    # "komornik" ~ "komornicze": alternation in the last letter of the stem
    return common >= max(4, len(word_stem) - 1)


def sensitive_hits(text: str | None, words: Iterable[str]) -> list[str]:
    """Sensitive words (or phrases) found in ``text``, matched by stem."""
    if not text:
        return []
    tokens = [stem(t) for t in _words(text)]
    hits = []
    for phrase in dict.fromkeys(words):
        parts = [stem(p) for p in _words(phrase)]
        if not parts:
            continue
        n = len(parts)
        for i in range(len(tokens) - n + 1):
            if all(_stems_match(parts[j], tokens[i + j]) for j in range(n)):
                hits.append(phrase)
                break
    return hits


def is_sensitive(text: str | None, words: Iterable[str]) -> bool:
    return bool(sensitive_hits(text, words))


def sensitive_words_for(extension_words: Sequence[str] | None) -> list[str]:
    """The extension's list (which already includes the core baseline) plus core extras."""
    from kasa.core.extension import BASELINE_SENSITIVE_WORDS

    return list(dict.fromkeys([*BASELINE_SENSITIVE_WORDS, *(extension_words or ()), *EXTRA_SENSITIVE_WORDS]))


# ---------------------------------------------------------------------------
# Detection (pure, no database)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Detection:
    kind: str
    start: int
    end: int
    value: str          # text as found
    normalized: str     # what is hashed
    method: str         # checksum / regex / known_person / first_name / capitalized_pair


@dataclass(frozen=True)
class PseudonymHit:
    kind: str
    start: int          # position in the original text
    end: int
    value: str
    pseudonym: str
    method: str


@dataclass
class KnownPerson:
    normalized: str                 # sorted folded tokens
    tokens: frozenset[str]
    surnames: frozenset[str]


@dataclass
class PseudonymContext:
    """What the detector knows about a company: company-name words and known persons."""

    company_tokens: set[str] = field(default_factory=set)
    persons: dict[str, KnownPerson] = field(default_factory=dict)
    cache: dict[tuple[str, str], str] = field(default_factory=dict)   # (kind, normalized) -> pseudonym

    def add_person(self, name: str) -> KnownPerson | None:
        tokens = [fold(w) for w in _words(name)]
        if len(tokens) < 2:
            return None
        normalized = " ".join(sorted(tokens))
        surnames = frozenset(t for t in tokens if t not in FIRST_NAMES and len(t) >= 3)
        person = KnownPerson(normalized, frozenset(tokens), surnames)
        self.persons[normalized] = person
        return person

    def add_company_name(self, name: str) -> None:
        """Register the words of a company name, except a person's name inside it.

        A capital company ("sp. z o.o.", "S.A.", "GmbH") is a legal entity: its name
        is taken whole. Any other business name (sole trader "Kadr Studio Paweł
        Wierzbicki", a bailiff's office) may carry a person, who is left out.
        """
        if not name:
            return
        person_spans = [] if is_capital_company(name) else _detect_persons(name, self, company_mode=True)
        masked = list(name)
        for d in person_spans:
            masked[d.start:d.end] = " " * (d.end - d.start)
        for word in _words("".join(masked)):
            folded = fold(word)
            if len(folded) >= 3 and folded not in COMPANY_MARKERS and folded not in FIRST_NAMES:
                self.company_tokens.add(folded)


_IBAN_START_RE = re.compile(r"(?<![A-Z0-9])([A-Z]{2})(\d{2})(?=[ ]?[A-Z0-9])")
_NRB_RE = re.compile(r"(?<![\dA-Z])(\d{2}(?:[ ]?\d{4}){6})(?!\d)")
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PESEL_RE = re.compile(r"(?<!\d)(\d{11})(?!\d)")
_PHONE_RES = (
    # +48 500 085 853 / 0048-500-085-853 / (+48) 500085853
    re.compile(r"(?<![\d+])(?:\(?(?:\+|00)48\)?[ -]?)(\d{3})([ -]?)(\d{3})\2(\d{3})(?![\d/])"),
    # 500 085 853 / 500-085-853
    re.compile(r"(?<![\d/.,])([1-9]\d{2})([ -])(\d{3})\2(\d{3})(?![\d/,]|\.\d)"),
    # 500085853 (mobile prefixes only, to spare invoice numbers)
    re.compile(r"(?<![\d/.,A-Za-z-])([4-8]\d{8})(?![\d/,A-Za-z]|\.\d|-\d)"),
    # landline 22 123 45 67
    re.compile(r"(?<![\d/.,])(\d{2}) (\d{3}) (\d{2}) (\d{2})(?![\d/,]|\.\d)"),
)
_ADDRESS_RE = re.compile(
    r"\b(?:ul\.|ulica|ulicy|al\.|aleja|alei|aleje|pl\.|plac|placu|os\.|osiedle)\s*"
    r"[A-ZĄĆĘŁŃÓŚŹŻ0-9][\w\-]*(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ][\w\-]*){0,3}"
    r"\s+\d+[a-zA-Z]?(?:\s*/\s*\d+[a-zA-Z]?|\s+(?:m\.|lok\.)\s*\d+)?"
    r"(?:,?\s+\d{2}-\d{3}(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ][\w\-]+)?)?"
)
_WORD_RE = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*")


def _pesel_valid(digits: str) -> bool:
    weights = (1, 3, 7, 9, 1, 3, 7, 9, 1, 3)
    if len(digits) != 11 or not digits.isdigit():
        return False
    check = (10 - sum(int(d) * w for d, w in zip(digits[:10], weights, strict=True)) % 10) % 10
    if check != int(digits[10]):
        return False
    month = int(digits[2:4])
    day = int(digits[4:6])
    return month % 20 in range(1, 13) and 1 <= day <= 31


def _detect_ibans(text: str) -> list[Detection]:
    found = []
    for m in _IBAN_START_RE.finditer(text):
        start = m.start()
        chars: list[str] = []
        positions: list[int] = []
        i = start
        while i < len(text) and len(chars) < 34:
            ch = text[i]
            if ch.isascii() and (ch.isdigit() or ch.isupper()):
                chars.append(ch)
                positions.append(i)
            elif ch == " " and i + 1 < len(text) and text[i + 1].isascii() and text[i + 1].isalnum() and chars:
                pass
            else:
                break
            i += 1
        for length in range(len(chars), 14, -1):
            candidate = "".join(chars[:length])
            if is_valid_iban(candidate):
                end = positions[length - 1] + 1
                found.append(Detection("iban", start, end, text[start:end], candidate, "checksum"))
                break
    for m in _NRB_RE.finditer(text):
        digits = re.sub(r"\s", "", m.group(1))
        if is_valid_iban("PL" + digits):
            found.append(Detection("iban", m.start(1), m.end(1), m.group(1), "PL" + digits, "checksum"))
    return found


def _detect_regex(text: str) -> list[Detection]:
    found = []
    for m in _EMAIL_RE.finditer(text):
        found.append(Detection("email", m.start(), m.end(), m.group(0), normalize_value("email", m.group(0)), "regex"))
    for m in _PESEL_RE.finditer(text):
        if _pesel_valid(m.group(1)):
            found.append(Detection("pesel", m.start(1), m.end(1), m.group(1), m.group(1), "checksum"))
    for pattern in _PHONE_RES:
        for m in pattern.finditer(text):
            value = m.group(0)
            digits = normalize_value("phone", value)
            if len(digits) == 9 and digits[0] != "0":
                found.append(Detection("phone", m.start(), m.end(), value, digits, "regex"))
    for m in _ADDRESS_RE.finditer(text):
        found.append(Detection("address", m.start(), m.end(), m.group(0), normalize_value("address", m.group(0)),
                               "regex"))
    return found


def _word_class(word: str) -> str | None:
    letters = word.replace("-", "")
    if len(letters) >= 2 and letters.isupper():
        return "caps"
    if word[0].isupper() and any(ch.islower() for ch in word[1:]):
        return "title"
    return None


def _is_safe(folded: str, context: PseudonymContext) -> bool:
    parts = folded.split("-")
    return any(p in SAFE_WORDS or p in context.company_tokens or p in COMPANY_MARKERS for p in parts)


def _detect_persons(text: str, context: PseudonymContext, *, company_mode: bool = False) -> list[Detection]:
    """Names of persons; ``company_mode`` (reading a company name) uses only first names and known persons."""
    words = [(m.start(), m.end(), m.group(0)) for m in _WORD_RE.finditer(text)]
    # runs of capitalized words separated only by spaces
    runs: list[list[tuple[int, int, str, str]]] = []
    current: list[tuple[int, int, str, str]] = []
    prev_end = None
    for start, end, word in words:
        cls = _word_class(word)
        joined = prev_end is not None and text[prev_end:start].strip(" ") == "" and "\n" not in text[prev_end:start]
        if cls is None:
            if current:
                runs.append(current)
            current = []
        else:
            if current and not joined:
                runs.append(current)
                current = []
            current.append((start, end, word, cls))
        prev_end = end
    if current:
        runs.append(current)

    found: list[Detection] = []
    for run in runs:
        used: set[int] = set()
        i = 0
        while i < len(run):
            hit = None
            for n in (3, 2):
                window = run[i:i + n]
                if len(window) < n:
                    continue
                folded = [fold(w[2]) for w in window]
                key = " ".join(sorted(folded))
                if key in context.persons:
                    hit = (n, "known_person", key)
                    break
                firsts = [f for f in folded if f in FIRST_NAMES]
                others = [f for f in folded if f not in FIRST_NAMES]
                if firsts and others and all(not _is_safe(f, context) for f in others) \
                        and not any(f in COMPANY_MARKERS for f in folded):
                    hit = (n, "first_name", key)
                    break
                if (not company_mode and n == 2 and all(w[3] == "title" for w in window)
                        and not firsts and all(not _is_safe(f, context) for f in folded)):
                    hit = (n, "capitalized_pair", key)
                    break
            if hit is not None:
                n, method, key = hit
                start, end = run[i][0], run[i + n - 1][1]
                found.append(Detection("person", start, end, text[start:end], key, method))
                used.update(range(i, i + n))
                i += n
                continue
            i += 1
        # a known person's surname alone ("Kowalska", "Kowalskiej")
        for idx, (start, end, word, _cls) in enumerate(run):
            if idx in used:
                continue
            folded = fold(word)
            for person in context.persons.values():
                if any(_surname_matches(s, folded) for s in person.surnames):
                    found.append(Detection("person", start, end, word, person.normalized, "known_person"))
                    break
    return found


def _surname_matches(surname: str, token: str) -> bool:
    if token == surname:
        return True
    return len(surname) >= 6 and token.startswith(surname[:-1]) and len(token) <= len(surname) + 3


def detect(text: str, context: PseudonymContext | None = None) -> list[Detection]:
    """All personal values in ``text``, without overlaps, in text order."""
    context = context or PseudonymContext()
    ordered = [*_detect_ibans(text), *_detect_regex(text)]
    accepted: list[Detection] = []
    taken: list[tuple[int, int]] = []

    def free(d: Detection) -> bool:
        return all(d.end <= s or d.start >= e for s, e in taken)

    for d in ordered:
        if free(d):
            accepted.append(d)
            taken.append((d.start, d.end))
    # persons last, on the text with the other values masked out
    masked = list(text)
    for s, e in taken:
        masked[s:e] = " " * (e - s)
    for d in _detect_persons("".join(masked), context):
        if free(d):
            accepted.append(Detection(d.kind, d.start, d.end, text[d.start:d.end], d.normalized, d.method))
            taken.append((d.start, d.end))
    return sorted(accepted, key=lambda d: d.start)


# ---------------------------------------------------------------------------
# Database: keys, context, pseudonymize / depseudonymize
# ---------------------------------------------------------------------------


def pseudonym_for(conn: psycopg.Connection, company_id: UUID, kind: str, value: str, *,
                  normalized: str | None = None, context: PseudonymContext | None = None) -> str:
    """The pseudonym of a value, created on first sight (stable: same value -> same code)."""
    if kind not in KINDS:
        raise ValueError(f"unknown pseudonym kind: {kind!r}")
    normalized = normalized if normalized is not None else normalize_value(kind, value)
    if not normalized:
        raise ValueError(f"empty {kind} value")
    cache_key = (kind, normalized)
    if context is not None and cache_key in context.cache:
        return context.cache[cache_key]
    digest = value_hash(kind, normalized)
    try:
        row = conn.execute(
            "SELECT pseudonym FROM pseudonym_key WHERE company_id = %s AND kind = %s AND value_hash = %s",
            (company_id, kind, digest),
        ).fetchone()
        if row is None:
            for length in range(4, 17):
                candidate = f"{PREFIX[kind]}-{digest[:length]}"
                conn.execute(
                    """
                    INSERT INTO pseudonym_key (company_id, kind, pseudonym, value_hash, value_encrypted)
                    VALUES (%s, %s, %s, %s, pgp_sym_encrypt(%s, %s))
                    ON CONFLICT DO NOTHING
                    """,
                    (company_id, kind, candidate, digest, value.strip(), _encryption_key()),
                )
                row = conn.execute(
                    "SELECT pseudonym FROM pseudonym_key WHERE company_id = %s AND kind = %s AND value_hash = %s",
                    (company_id, kind, digest),
                ).fetchone()
                if row is not None:
                    break
            else:  # pragma: no cover - 16 hex characters of HMAC never collide in one company
                raise PseudonymizationError("cannot allocate a unique pseudonym")
    except psycopg.Error as exc:
        raise PseudonymizationError(f"bramka pseudonimizacji nie działa: {exc}") from exc
    pseudonym = row[0]
    if context is not None:
        context.cache[cache_key] = pseudonym
        if kind == "person":
            context.add_person(value)
    return pseudonym


def register_person(conn: psycopg.Connection, company_id: UUID, name: str,
                    context: PseudonymContext | None = None) -> str:
    """Pseudonym of a person (e.g. for ``l1_internal_record.person_pseudonym``); the person becomes known."""
    return pseudonym_for(conn, company_id, "person", name, context=context)


def load_context(conn: psycopg.Connection, company_id: UUID) -> PseudonymContext:
    """Company-name words from the counterparty registry and every known person."""
    context = PseudonymContext()
    try:
        rows = conn.execute(
            """
            SELECT name FROM l1_counterparty
             WHERE company_id = %s AND superseded_by IS NULL AND kind <> 'person' AND name IS NOT NULL
            """,
            (company_id,),
        ).fetchall()
        persons = conn.execute(
            """
            SELECT pgp_sym_decrypt(value_encrypted, %s) FROM pseudonym_key
             WHERE company_id = %s AND kind = 'person' AND value_encrypted IS NOT NULL
            """,
            (_encryption_key(), company_id),
        ).fetchall()
    except psycopg.Error as exc:
        raise PseudonymizationError(f"bramka pseudonimizacji nie działa: {exc}") from exc
    for (name,) in persons:
        context.add_person(name)
    for (name,) in rows:
        context.add_company_name(name)
    return context


def pseudonymize_text(conn: psycopg.Connection, company_id: UUID, text: str | None, *,
                      context: PseudonymContext | None = None) -> tuple[str | None, list[PseudonymHit]]:
    """Replace personal data in ``text`` by pseudonyms. Raises PseudonymizationError when the gate fails."""
    if text is None:
        return None, []
    context = context if context is not None else load_context(conn, company_id)
    hits: list[PseudonymHit] = []
    out: list[str] = []
    pos = 0
    for d in detect(text, context):
        pseudonym = pseudonym_for(conn, company_id, d.kind, d.value, normalized=d.normalized, context=context)
        out.append(text[pos:d.start])
        out.append(pseudonym)
        pos = d.end
        hits.append(PseudonymHit(d.kind, d.start, d.end, d.value, pseudonym, d.method))
    out.append(text[pos:])
    return "".join(out), hits


def depseudonymize(conn: psycopg.Connection, company_id: UUID, text: str | None, *, role: str) -> str | None:
    """Put the original values back. Only for internal roles (never the agent or a model)."""
    if role not in INTERNAL_ROLES:
        raise PermissionError(f"rola {role!r} nie może odwracać pseudonimów")
    if text is None:
        return None
    codes = sorted({m.group(0) for m in PSEUDONYM_RE.finditer(text)})
    if not codes:
        return text
    rows = conn.execute(
        """
        SELECT pseudonym, CASE WHEN value_encrypted IS NULL THEN NULL
                               ELSE pgp_sym_decrypt(value_encrypted, %s) END
          FROM pseudonym_key WHERE company_id = %s AND pseudonym = ANY(%s)
        """,
        (_encryption_key(), company_id, codes),
    ).fetchall()
    values = {code: value for code, value in rows if value is not None}
    log.info("depseudonymize: company=%s role=%s codes=%d", company_id, role, len(values))
    return PSEUDONYM_RE.sub(lambda m: values.get(m.group(0), m.group(0)), text)


def looks_like_person(name: str | None, context: PseudonymContext | None = None) -> bool:
    """A bank counterparty name that is a person ("Anna Mazur", "MAJA LEWANDOWICZ")."""
    if not name:
        return False
    words = _words(name)
    if not 2 <= len(words) <= 4:
        return False
    folded = [fold(w) for w in words]
    if is_capital_company(name) or any(
        f in COMPANY_MARKERS or (f in SAFE_WORDS and f not in FIRST_NAMES) for f in folded
    ):
        return False
    context = context or PseudonymContext()
    if " ".join(sorted(folded)) in context.persons:
        return True
    return any(f in FIRST_NAMES for f in folded) and any(f not in FIRST_NAMES for f in folded)
