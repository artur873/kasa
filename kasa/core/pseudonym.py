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

Names of persons are found by code, without a model. Two capitalized words are a
person only with evidence (phase 2 review: product, street and company names are
not persons and stay readable):
1. known persons - registered persons (rows of internal sheets, bank counterparties
   of kind ``person``) and keys created with evidence (``pseudonym_key.basis``) - in
   any word order, and their surnames alone (also inflected: "Kuczyńskiej");
2. a Polish first name (built-in list of ~330 names with genitive forms) next to a
   capitalized word that is neither a safe word nor a word of a company name;
3. context: an honorific before the name ("Pan", "Pani", "P."), "BLIK" before it, or
   "tel." / "PESEL" right after it.
Anything else ("Google Workspace", "Scena Pro") is left as it is. The window of a person is a
first name with one or two surnames: a masked value (PESEL, IBAN, phone) is a boundary, a
capitalised acronym (PESEL, NIP, FV, BLIK) is never a name, and a word followed by a number
belongs to the address ("Jan Kowalski Kwiatowa 7" -> person + address, the same person code
as "Jan Kowalski" alone).

Addresses (phase 3 review): with a prefix ("ul.", "al.", "pl.", "os."); without one when the
number carries a flat, "m."/"lok." or a postal code ("Kwiatowa 7/3", "Mazowiecka 12 m. 3
Warszawa", "Kwiatowa 7, 05-500 Piaseczno"); a bare "Kwiatowa 7" only next to a person or after
"zam."/"adres"; foreign ones by number and street word ("70 Sir John Rogerson's Quay", "500
Market Street, Suite 900"). Invoice numbers ("FV 7/10/2026", "Rachunek 4/2026") and amounts
are not addresses. A company's seat from structured FA(3) fields never reaches a model (the
semantics of invoice lines sends only the line name), so only addresses inside free text -
transfer titles, a person's bill, PDFs - are coded.
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

# Common Polish first names. Used to recognise "Imię Nazwisko" and "Nazwisko Imię"
# in titles, counterparty names and documents; FIRST_NAMES holds them folded, with
# their genitive forms ("Tomasza", "Anny", "Pawła", "Julii").
_FIRST_NAMES_TEXT = """
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
Adela Albert Alan Alfred Ambroży Aureliusz Benedykt Bianka Blanka Borys Brunon Dagmara Daria Diana Dobromir
Dorian Eliza Emma Eryk Ernest Eugeniusz Felicja Feliks Franciszka Fryderyk Genowefa Gerard Gustaw Hieronim
Hilary Irmina Iwo Jagoda Jeremi Józefa Judyta Juliusz Kajetan Karina Klemens Kornel Leonard Lidia Lucjan
Ludmiła Maryla Matylda Melania Melchior Miłosz Mirella Nataniel Nina Olga Oskar Regina Remigiusz Rozalia Sara
Seweryn Stanisława Stefania Sylwester Tamara Teodor Tobiasz Wiesława Zdzisława Zenobia Zenobiusz Ziemowit
"""

_VOWELS = set("aeiouy")


def _first_name_forms(name: str) -> set[str]:
    """Nominative and genitive of a first name, folded (Tomasz -> tomasz, tomasza)."""
    f = fold(name)
    forms = {f}
    if f.endswith("a"):
        if f.endswith(("sia", "cia", "zia", "nia")):
            forms.add(f[:-1])                               # Kasia -> Kasi
        elif f.endswith("ia"):
            forms.add(f[:-1] + "i")                         # Julia -> Julii
        elif f.endswith("ja"):
            forms.add(f[:-2] + "i" if f[-3] in _VOWELS else f[:-1] + "i")   # Maja -> Mai, Łucja -> Łucji
        elif f.endswith(("ka", "ga")):
            forms.add(f[:-1] + "i")                         # Kinga -> Kingi
        else:
            forms.add(f[:-1] + "y")                         # Anna -> Anny
    elif name.endswith("eł"):
        forms.add(f[:-2] + "la")                            # Paweł -> Pawła
    elif f.endswith("ek"):
        forms.add(f[:-2] + "ka")                            # Marek -> Marka
    elif f.endswith("y"):
        forms.add(f[:-1] + "ego")                           # Ignacy -> Ignacego
    elif f.endswith("i"):
        forms.add(f + "ego")                                # Antoni -> Antoniego
    elif f.endswith("o"):
        forms.add(f[:-1] + "a")                             # Iwo -> Iwa
    else:
        forms.add(f + "a")                                  # Jan -> Jana
    return forms


FIRST_NAME_LIST: tuple[str, ...] = tuple(dict.fromkeys(_FIRST_NAMES_TEXT.split()))
FIRST_NAMES = frozenset(form for name in FIRST_NAME_LIST for form in _first_name_forms(name))

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
pan pani pana panu pania panem panie
""".split())

# Words that make a name a company (folded, without dots).
COMPANY_MARKERS = frozenset(
    "sp spolka zoo z o oo sa s a sj sk ska sc ltd llc inc gmbh ag bv nv uab sas sarl plc oy ab as se spa kg co".split()
)

# Nominative first names (folded): a street is never called "Anna"; a genitive form ("Jana",
# "Marii") may open a street name ("Jana Pawła II", "Marii Konopnickiej").
FIRST_NAME_NOMINATIVES = frozenset(fold(name) for name in FIRST_NAME_LIST)

# Cities (folded) that may close an address after a flat number ("Mazowiecka 12 m. 3 Warszawa").
CITY_WORDS = frozenset(fold(w) for w in """
warszawa warszawie krakow krakowie gdansk gdansku gdynia gdyni sopot sopocie wroclaw wroclawiu lodz lodzi poznan
poznaniu katowice katowicach szczecin szczecinie lublin lublinie bydgoszcz bydgoszczy bialystok bialymstoku torun
toruniu kielce kielcach rzeszow rzeszowie olsztyn olsztynie opole opolu gliwice gliwicach zabrze bytom czestochowa
radom sosnowiec zakopane piaseczno pruszkow legionowo otwock grodzisk zielonka marki zabki
""".split())

# Words before a street's main word that belong to the street ("Aleje Jerozolimskie", "Wybrzeże
# Wyspiańskiego", "gen. Andersa", "św. Marcina"), folded, without dots.
STREET_LEADERS = frozenset(fold(w) for w in """
aleje aleja alei wybrzeze wybrzeza bulwar bulwaru rondo ronda skwer plac placu osiedle osiedla
generala gen ksiedza ks swietego swietej sw marszalka marsz kardynala kard profesora prof doktora dr
majora pulkownika plk krola krolowej hetmana biskupa papieza powstancow obroncow bohaterow
""".split())

# Acronyms written in capitals that are never a first name or a surname ("PESEL 44051401359 Marek Nowak":
# the word before the name must not join it). Bank counterparties in capitals ("MAJA LEWANDOWICZ") stay names.
ACRONYMS = frozenset("""
pesel nip iban regon krs fv fa fvat vat pln eur usd gbp chf blik nr tel ul al pl os zus us pit cit km nus spv
sp zoo sa sk ska sj ltd llc inc gmbh ag bv nv uab plc inv idc txt okr sfp ti mpp ksef jpk bic swift sepa atm pos
ref id nrb rk kp kw wz pz cmr pkd
""".split())

_CAPITAL_COMPANY_RE = re.compile(
    r"\bsp\.?\s*z\s*o\.?\s*o\b|\bs\.?\s?k\.?\s?a\b|\bs\.\s?a\b\.?|\bs\.?a\.?$|\bp\.?s\.?a\b"
    r"|\b(?:gmbh|ltd|limited|inc|llc|uab|ag|plc|bv|nv|sarl|sas|spa|oy|ab|se|corp|corporation)\b",
    re.IGNORECASE,
)


def is_capital_company(name: str | None) -> bool:
    """Name of a legal entity (sp. z o.o., S.A., GmbH, Ltd ...), not of a natural person."""
    return bool(name) and bool(_CAPITAL_COMPANY_RE.search(fold(name)))


# Sensitive words live in kasa.core.sensitive (stems with Polish inflection, table
# sensitive_term); these wrappers keep the phase 2 API.


def sensitive_hits(text: str | None, words: Iterable[str]) -> list[str]:
    """Sensitive words (or phrases) found in ``text``, matched by stem (kasa.core.sensitive)."""
    from kasa.core.sensitive import find_sensitive

    return find_sensitive(text, words)


def is_sensitive(text: str | None, words: Iterable[str]) -> bool:
    return bool(sensitive_hits(text, words))


def sensitive_words_for(extension_words: Sequence[str] | None) -> list[str]:
    """The core list (kasa.core.sensitive.CORE_TERMS) plus the extension's words."""
    from kasa.core.sensitive import code_terms

    return [t.term for t in code_terms(extension_words)]


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
    # 500 085 853 / 500-085-853 (a comma after the number ends it: "tel. 500 085 853, konto ..."; a decimal
    # comma - "500 085 853,00" - is an amount)
    re.compile(r"(?<![\d/.,])([1-9]\d{2})([ -])(\d{3})\2(\d{3})(?![\d/]|[.,]\d)"),
    # 500085853 (mobile prefixes only, to spare invoice numbers)
    re.compile(r"(?<![\d/.,A-Za-z-])([4-8]\d{8})(?![\d/A-Za-z]|[.,]\d|-\d)"),
    # landline 22 123 45 67
    re.compile(r"(?<![\d/.,])(\d{2}) (\d{3}) (\d{2}) (\d{2})(?![\d/]|[.,]\d)"),
)
_ADDRESS_RE = re.compile(
    r"\b(?:ul\.|ulica|ulicy|al\.|aleja|alei|aleje|pl\.|plac|placu|os\.|osiedle)\s*"
    r"[A-ZĄĆĘŁŃÓŚŹŻ0-9][\w\-]*(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ][\w\-]*){0,3}"
    r"\s+\d+[a-zA-Z]?(?:\s*/\s*\d+[a-zA-Z]?|\s+(?:m\.|lok\.)\s*\d+)?"
    r"(?:,?\s+\d{2}-\d{3}(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ][\w\-]+)?)?"
)
_UP = "A-ZĄĆĘŁŃÓŚŹŻ"
_LOW = "a-ząćęłńóśźżäöüß"
# A street without a prefix: "Kwiatowa 7/3", "Mazowiecka 12 m. 3 Warszawa", "Kwiatowa 7, 05-500 Piaseczno".
# Only the word right before the number is matched here; words that belong to the street before it
# ("Jana Pawła II", "Aleje Jerozolimskie") are added by code. A strong match has a flat number, "m."/"lok."
# or a postal code; a weak one (just "Kwiatowa 7") is an address only right after a person.
_PLAIN_ADDRESS_RE = re.compile(
    rf"(?<![\w.\-/])(?P<main>[{_UP}][{_LOW}]+(?:-[{_UP}][{_LOW}]+)?)(?:[ ]+(?P<roman>[IVX]{{1,4}}))?"
    rf"[ ]+(?P<num>[1-9]\d{{0,3}}[a-zA-Z]?)(?![\w]|[.,\-]\d)"
    rf"(?P<flat>[ ]?/[ ]?[1-9]\d{{0,2}}[a-zA-Z]?(?![\w/]|[.,\-]\d))?"
    rf"(?P<lok>[ ]+(?:m\.|lok\.|lokal|mieszk\.)[ ]*[1-9]\d{{0,3}}[a-zA-Z]?(?![\w/]|[.,\-]\d))?"
    rf"(?P<postal>(?:[ ]*,[ ]*|[ ]+|[ ]*\n[ ]*)\d{{2}}-\d{{3}}(?![\d\-])"
    rf"(?P<city>[ ]+[{_UP}][{_LOW}]+(?:-[{_UP}][{_LOW}]+)?(?P<city2>[ ]+[{_UP}][{_LOW}]+)?)?)?"
    rf"(?P<tail>,?[ ]+(?P<tailcity>[{_UP}][{_LOW}]+))?"
)
_STREET_SUFFIX = (r"Street|St\.|Avenue|Ave\.?|Road|Rd\.?|Quay|Parkway|Pkwy\.?|Boulevard|Blvd\.?|Lane|Drive|"
                  r"Way|Place|Square|Court|Terrace|Highway|Straße|Strasse|Platz|Gasse|Allee|Weg")
# A foreign street: number first ("70 Sir John Rogerson's Quay", "500 Market Street, Suite 900").
_FOREIGN_ADDRESS_RE = re.compile(
    rf"(?<![\w/.,\-])[1-9]\d{{0,4}}[A-Za-z]?(?:-\d{{1,4}})?[ \t]+(?:[A-Z][\w'’.\-]*[ \t]+){{0,4}}"
    rf"(?:{_STREET_SUFFIX})(?![\w])"
    rf"(?:,?[ \t]+(?:Suite|Ste\.?|Floor|Fl\.|Unit|Apt\.?|Apartment|Room)[ \t]*#?[\w\-]+)?"
)
# Words after which a bare "Kwiatowa 7" is an address.
_ADDRESS_CONTEXT_RE = re.compile(r"(?:\bzam\.|\bzamieszka\w*|\bzameldowan\w*|\badres\w*)[\s:,-]*$", re.IGNORECASE)
_WORD_RE = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*")
_MASK = "#"


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


# Words of transfer titles and documents that are followed by a number but are never a street
# (only for addresses; persons use SAFE_WORDS).
_NOT_STREETS = frozenset(fold(w) for w in """
okres okresu termin terminu numer pozycja poz czesc etap transza transzy wplata wplaty saldo lista kurs kwota
suma razem strona zalacznik aneks aneksu decyzja postanowienie sprawa sprawy sygn platnosc dokument dokumentu
paczka tydzien tygodnia dzien dnia rok roku miesiac miesiaca kwartal kwartalu edycja edycji wersja wersji
sezon sezonu runda rundy grupa sala sali stoisko stoiska pokoj pokoju lot lotu bilet biletu miejsce miejsca
impreza imprezy klient klienta zadanie zadania punkt punktu wiersz wiersza kasa kasjer karta
invoice order total subtotal amount qty item page
""".split())


def _street_word_ok(word: str, context: PseudonymContext) -> bool:
    for part in fold(word).split("-"):
        if part in _NOT_STREETS or part in COMPANY_MARKERS or part in context.company_tokens \
                or part in FIRST_NAME_NOMINATIVES:
            return False
        # SAFE_WORDS hold city names; their adjectival forms are common streets ("Gdańska 5/3")
        if part in SAFE_WORDS and not part.endswith(("ska", "cka")):
            return False
    return True


def _street_start(text: str, start: int) -> int:
    """Extend a street name backwards over its leading words ("Jana Pawła", "Aleje", "gen.")."""
    for _ in range(2):
        m = re.search(rf"(?:^|(?<=[\s(,;:]))([{_UP}{_LOW}][{_LOW}]*\.?)[ ]+$", text[:start])
        if m is None:
            break
        word = m.group(1)
        folded = fold(word.rstrip("."))
        if folded in STREET_LEADERS or (word[0].isupper() and folded in FIRST_NAMES
                                         and folded not in FIRST_NAME_NOMINATIVES and not word.endswith(".")):
            start = m.start(1)
            continue
        break
    return start


def _detect_foreign_addresses(text: str) -> list[Detection]:
    return [Detection("address", m.start(), m.end(), m.group(0), normalize_value("address", m.group(0)), "regex")
            for m in _FOREIGN_ADDRESS_RE.finditer(text)]


def _detect_plain_addresses(text: str, context: PseudonymContext, *, weak: bool) -> list[Detection]:
    """Streets without a prefix; ``weak`` also returns bare "Name 7" (accepted by the caller only in context)."""
    found = []
    for m in _PLAIN_ADDRESS_RE.finditer(text):
        if not _street_word_ok(m.group("main"), context):
            continue
        if not m.group("flat") and re.match(r"[ ]?/", text[m.end("num"):]):
            continue                    # "Faktura 214/09/2026", "Rachunek 4/2026": a document number
        strong = bool(m.group("flat") or m.group("lok") or m.group("postal"))
        if not strong and (not weak or len(re.sub(r"\D", "", m.group("num"))) > 3):
            continue
        end = m.end()
        if m.group("tail") and not ((m.group("flat") or m.group("lok")) and not m.group("postal")
                                    and fold(m.group("tailcity")) in CITY_WORDS):
            end = m.start("tail")       # a city closes an address only after a flat number
        if m.group("city2") and end >= m.end("city2"):
            second = fold(m.group("city2").strip())
            if second in FIRST_NAME_NOMINATIVES or (second in SAFE_WORDS and second not in CITY_WORDS):
                end = m.start("city2")  # "05-500 Piaseczno Jan Kowalski": the city is one word
        start = _street_start(text, m.start("main"))
        value = text[start:end]
        found.append(Detection("address", start, end, value, normalize_value("address", value),
                               "regex" if strong else "context"))
    return found


def _is_acronym(word: str) -> bool:
    letters = word.replace("-", "")
    if not (letters.isupper() and len(letters) >= 2):
        return False
    folded = fold(letters)
    return folded in ACRONYMS or (len(folded) <= 4 and not any(ch in _VOWELS for ch in folded))


def _word_class(word: str) -> str | None:
    if _is_acronym(word):
        return None
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
            elif current and _NUMBER_AFTER_RE.match(text, end) and _holds_name(current, context):
                # "Jan Kowalski Kwiatowa 7": a word before a number is the street, not a third name
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
                if key in context.persons and not all(f in context.company_tokens for f in folded):
                    hit = (n, "known_person", key)      # (a key made only of company words is a company)
                    break
                firsts = [f for f in folded if f in FIRST_NAMES]
                others = [f for f in folded if f not in FIRST_NAMES]
                if firsts and others and all(not _is_safe(f, context) for f in others) \
                        and not any(f in COMPANY_MARKERS for f in folded):
                    hit = (n, "first_name", key)
                    break
                if (not company_mode and n == 2 and all(not _is_safe(f, context) for f in folded)
                        and _context_evidence(text, window[0][0], window[-1][1])):
                    hit = (n, "context", key)
                    break
            if hit is None and not company_mode and _HONORIFIC_RE.search(_line_before(text, run[i][0])):
                folded_one = fold(run[i][2])
                if not _is_safe(folded_one, context) and folded_one not in FIRST_NAMES:
                    hit = (1, "context", folded_one)            # "Pani Kowalska"
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


_NUMBER_AFTER_RE = re.compile(r"[ ]*,?[ ]*(?:[1-9]\d{0,3}[a-zA-Z]?(?![\d.,:/\-]\d)(?!\d)|\d{2}-\d{3}(?!\d))")


def _holds_name(run: list[tuple[int, int, str, str]], context: PseudonymContext) -> bool:
    folded = [fold(w[2]) for w in run]
    return any(f in FIRST_NAMES for f in folded) and any(
        f not in FIRST_NAMES and not _is_safe(f, context) for f in folded)


# Context that makes two capitalized words a person without a first name from the list.
_HONORIFIC_RE = re.compile(r"(?:^|[\s(,;:/])(?:pan|pani|pana|panu|pania|panem|panie|p\.|sz\.\s?p\.|ob\.)\s*$",
                           re.IGNORECASE)
_BEFORE_RE = re.compile(r"\bblik\b[\s:,-]*(?:dla|na rzecz|od)?\s*$", re.IGNORECASE)
_AFTER_RE = re.compile(r"^[\s,;:(-]{0,3}(?:tel\b|tel\.|telefon|pesel)", re.IGNORECASE)


def _line_before(text: str, start: int) -> str:
    cut = max(text.rfind("\n", 0, start), text.rfind("|", 0, start))
    return fold(text[cut + 1:start])


def _context_evidence(text: str, start: int, end: int) -> bool:
    before = _line_before(text, start)
    after = text[end:end + 24].split("\n", 1)[0].split("|", 1)[0]
    return bool(_HONORIFIC_RE.search(before) or _BEFORE_RE.search(before) or _AFTER_RE.search(after))


def _surname_matches(surname: str, token: str) -> bool:
    if token == surname:
        return True
    return len(surname) >= 6 and token.startswith(surname[:-1]) and len(token) <= len(surname) + 3


def detect(text: str, context: PseudonymContext | None = None) -> list[Detection]:
    """All personal values in ``text``, without overlaps, in text order."""
    context = context or PseudonymContext()
    ordered = [*_detect_ibans(text), *_detect_regex(text), *_detect_foreign_addresses(text),
               *_detect_plain_addresses(text, context, weak=False)]
    accepted: list[Detection] = []
    taken: list[tuple[int, int]] = []

    def free(d: Detection) -> bool:
        return all(d.end <= s or d.start >= e for s, e in taken)

    def take(d: Detection) -> None:
        accepted.append(Detection(d.kind, d.start, d.end, text[d.start:d.end], d.normalized, d.method))
        taken.append((d.start, d.end))

    def masked() -> str:
        # a masked value is a boundary: no person window reaches across it
        chars = list(text)
        for s, e in taken:
            chars[s:e] = _MASK * (e - s)
        return "".join(chars)

    for d in ordered:
        if free(d):
            take(d)
    # persons last, on the text with the other values masked out
    persons = [d for d in _detect_persons(masked(), context) if free(d)]
    for d in persons:
        take(d)
    # a bare "Kwiatowa 7" next to a person or after "zam." / "adres"
    for d in _detect_plain_addresses(masked(), context, weak=True):
        if not free(d):
            continue
        gap_ok = any(re.fullmatch(r"[ ,;]*", text[p.end:d.start]) for p in persons if p.end <= d.start) \
            or any(re.fullmatch(r"[ ,;]*", text[d.end:p.start]) for p in persons if p.start >= d.end)
        if gap_ok or _ADDRESS_CONTEXT_RE.search(text[max(0, d.start - 40):d.start]):
            take(d)
    return sorted(accepted, key=lambda d: d.start)


# ---------------------------------------------------------------------------
# Database: keys, context, pseudonymize / depseudonymize
# ---------------------------------------------------------------------------


# Why a value was taken for a person (pseudonym_key.basis, migration 0008).
EVIDENCE_BASES = frozenset({"registered", "first_name", "context", "known_person"})


def pseudonym_for(conn: psycopg.Connection, company_id: UUID, kind: str, value: str, *,
                  normalized: str | None = None, context: PseudonymContext | None = None,
                  basis: str | None = None) -> str:
    """The pseudonym of a value, created on first sight (stable: same value -> same code).

    ``basis`` (recorded with a new key): why the value is personal data - for a
    person ``registered``, ``first_name``, ``context`` or ``known_person``.
    """
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
                    INSERT INTO pseudonym_key (company_id, kind, pseudonym, value_hash, value_encrypted, basis)
                    VALUES (%s, %s, %s, %s, pgp_sym_encrypt(%s, %s), %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (company_id, kind, candidate, digest, value.strip(), _encryption_key(), basis),
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
    return pseudonym_for(conn, company_id, "person", name, context=context, basis="registered")


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
            SELECT pgp_sym_decrypt(k.value_encrypted, %s), k.basis,
                   EXISTS (SELECT 1 FROM l1_internal_record r
                            WHERE r.company_id = k.company_id AND r.person_pseudonym = k.pseudonym)
              FROM pseudonym_key k
             WHERE k.company_id = %s AND k.kind = 'person' AND k.value_encrypted IS NOT NULL
            """,
            (_encryption_key(), company_id),
        ).fetchall()
        person_parties = conn.execute(
            """
            SELECT name FROM l1_counterparty
             WHERE company_id = %s AND superseded_by IS NULL AND kind = 'person' AND name IS NOT NULL
            """,
            (company_id,),
        ).fetchall()
    except psycopg.Error as exc:
        raise PseudonymizationError(f"bramka pseudonimizacji nie działa: {exc}") from exc
    party_keys = {normalize_value("person", name) for (name,) in person_parties}
    for name, basis, in_sheet in persons:
        # Keys made before 0008 have no basis: a person only with evidence (a first name,
        # a row of an internal file, a bank counterparty of kind person); old false
        # "persons" (product or street names) stay in the key table but are not used.
        if (basis in EVIDENCE_BASES or in_sheet or normalize_value("person", name) in party_keys
                or any(fold(w) in FIRST_NAMES for w in _words(name))):
            context.add_person(name)
    for (name,) in person_parties:
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
        pseudonym = pseudonym_for(conn, company_id, d.kind, d.value, normalized=d.normalized, context=context,
                                  basis=d.method)
        out.append(text[pos:d.start])
        out.append(pseudonym)
        pos = d.end
        hits.append(PseudonymHit(d.kind, d.start, d.end, d.value, pseudonym, d.method))
    out.append(text[pos:])
    return "".join(out), hits


def depseudonymize(conn: psycopg.Connection, company_id: UUID, text: str | None, *, role: str,
                   only: Iterable[str] | None = None) -> str | None:
    """Put the original values back. Only for internal roles (never the agent or a model).

    ``only``: reverse just these codes (e.g. those present in the text sent to a model -
    a model may answer with a code it never saw); any other code stays as it is.
    """
    if role not in INTERNAL_ROLES:
        raise PermissionError(f"rola {role!r} nie może odwracać pseudonimów")
    if text is None:
        return None
    codes = {m.group(0) for m in PSEUDONYM_RE.finditer(text)}
    if only is not None:
        codes &= set(only)
    codes = sorted(codes)
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
