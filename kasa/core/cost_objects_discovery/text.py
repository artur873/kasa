"""Text features shared by candidate discovery and allocation.

* ``tokens(text)`` - folded words (case and Polish letters ignored) with a light stem that
  removes one inflection ending ("Gali" = "Gala" = "gal", "Krakowie" = "krakow"); numbers,
  pseudonym codes (``P-7f3a``) and stop words are dropped, short codes with a digit
  ("p3", "a12") stay - a room or a hall is often named that way;
* ``client_tokens(name)`` - tokens of a company name without legal forms ("sp. z o.o.");
* ``city_key(city)`` - comparable form of a city name;
* ``dates_in_text(text, reference)`` - date windows written in free text ("03.10",
  "15-16.10.2026", "2026-10-08"), the year taken from the reference date when missing.

Nothing here knows what a cost object of a company is.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from kasa.core.sensitive import fold

_CODE_RE = re.compile(r"\b[A-Z]-[0-9a-f]{4,16}\b")          # pseudonyms: P-7f3a, T-34da
_WORD_RE = re.compile(r"[^\W_]+")
_ENDINGS = ("ami", "ach", "owi", "ego", "emu", "iu", "ie", "ia", "om", "a", "e", "i", "o", "u", "y")
MIN_STEM = 3

STOPWORDS = frozenset({
    "i", "w", "we", "z", "ze", "na", "do", "dla", "od", "po", "za", "oraz", "pod", "nad", "przy", "o", "a",
    "u", "bez", "lub", "albo", "jak", "nr", "ul", "lok", "tel", "pl", "the", "and", "of", "for", "to", "in",
    "on", "at", "by", "with", "inv", "txt", "idc", "vat", "fv", "faktura", "faktury", "fakture", "rachunek",
    "zaplata", "zaplaty", "przelew", "przelewu", "kwota", "kwot", "oryg", "kurs", "karta", "blik", "telefon",
    "termin", "umowa", "umowy", "wg",
})
LEGAL_FORMS = frozenset({
    "sp", "spolka", "spolki", "z", "o", "oo", "zoo", "sa", "s", "a", "k", "j", "gmbh", "ltd", "inc", "llc",
    "pbc", "ag", "bv", "sarl", "srl", "limited", "polska", "poland",
})


def norm_word(word: str) -> str | None:
    """One comparable token, or None for a number, a stop word or a too short word."""
    folded = fold(word)
    if not folded or folded.isdigit():
        return None
    has_digit = any(ch.isdigit() for ch in folded)
    if has_digit:
        # short codes starting with a letter (p3, a12) name rooms and halls; hex codes do not
        return folded if folded[0].isalpha() and len(folded) <= 4 else None
    if len(folded) <= 2 or folded in STOPWORDS:
        return None
    for ending in _ENDINGS:
        if folded.endswith(ending) and len(folded) - len(ending) >= MIN_STEM:
            return folded[: -len(ending)]
    return folded


def tokens(text: str | None) -> list[str]:
    if not text:
        return []
    clean = _CODE_RE.sub(" ", text)
    out = []
    for word in _WORD_RE.findall(clean):
        token = norm_word(word)
        if token:
            out.append(token)
    return out


def token_set(text: str | None) -> frozenset[str]:
    return frozenset(tokens(text))


def client_tokens(name: str | None) -> frozenset[str]:
    """Tokens of a company name that identify it (legal forms and country words removed)."""
    if not name:
        return frozenset()
    out = set()
    for word in _WORD_RE.findall(_CODE_RE.sub(" ", name)):
        if fold(word) in LEGAL_FORMS:
            continue
        token = norm_word(word)
        if token and token not in LEGAL_FORMS:
            out.add(token)
    return frozenset(out)


def city_key(city: str | None) -> str | None:
    if not city:
        return None
    parts = [norm_word(w) for w in _WORD_RE.findall(city)]
    parts = [p for p in parts if p]
    return " ".join(parts) or None


# "15-16.10.2026", "03.10", "08-10.10.2026", "15.09-14.10.2026"
_DM_RANGE_RE = re.compile(
    r"(?<![\d.])(\d{1,2})(?:\.(\d{1,2}))?\s*[-–]\s*(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?(?![\d,])"
)
_DM_RE = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?(?![\d,]|\.\d)")
_ISO_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _year_for(month: int, day: int, reference: date | None) -> int | None:
    if reference is None:
        return None
    candidate = _safe_date(reference.year, month, day)
    if candidate is None:
        return None
    # dates in a title or a row are in the past or close to the reference
    if candidate > reference + timedelta(days=45):
        return reference.year - 1
    return reference.year


def dates_in_text(text: str | None, reference: date | None = None) -> list[tuple[date, date]]:
    """Date windows written in a text (each window is (start, end), a single day has start = end)."""
    if not text:
        return []
    windows: list[tuple[date, date]] = []
    consumed: list[tuple[int, int]] = []
    for m in _ISO_RE.finditer(text):
        d = _safe_date(int(m[1]), int(m[2]), int(m[3]))
        if d:
            windows.append((d, d))
            consumed.append(m.span())
    for m in _DM_RANGE_RE.finditer(text):
        if any(a <= m.start() < b for a, b in consumed):
            continue
        day1 = int(m[1])
        month1 = int(m[2]) if m[2] else int(m[4])
        day2, month2 = int(m[3]), int(m[4])
        year = int(m[5]) if m[5] else _year_for(month2, day2, reference)
        if year is None:
            continue
        start = _safe_date(year if month1 <= month2 else year - 1, month1, day1)
        end = _safe_date(year, month2, day2)
        if start and end and start <= end:
            windows.append((start, end))
            consumed.append(m.span())
    for m in _DM_RE.finditer(text):
        if any(a <= m.start() < b for a, b in consumed):
            continue
        day, month = int(m[1]), int(m[2])
        if not (1 <= month <= 12):
            continue
        year = int(m[3]) if m[3] else _year_for(month, day, reference)
        d = _safe_date(year, month, day) if year else None
        if d:
            windows.append((d, d))
    return windows


def merge_windows(windows: list[tuple[date, date]]) -> tuple[date, date] | None:
    if not windows:
        return None
    return min(w[0] for w in windows), max(w[1] for w in windows)


def month_window(month: str | None) -> tuple[date, date] | None:
    """'2026-10' -> (2026-10-01, 2026-10-31)."""
    if not month or not re.fullmatch(r"\d{4}-\d{2}", month):
        return None
    year, mon = int(month[:4]), int(month[5:])
    if not 1 <= mon <= 12:
        return None
    start = date(year, mon, 1)
    end = date(year + (mon == 12), mon % 12 + 1, 1) - timedelta(days=1)
    return start, end


def window_gap_days(a: tuple[date, date], b: tuple[date, date]) -> int:
    """Days between two windows (0 = they overlap)."""
    if a[1] < b[0]:
        return (b[0] - a[1]).days
    if b[1] < a[0]:
        return (a[0] - b[1]).days
    return 0


def window_days(window: tuple[date, date]) -> int:
    return (window[1] - window[0]).days + 1


def untrusted(value) -> str | None:
    """Text of an entity stored as {"untrusted_text": ...} (or a plain string)."""
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("untrusted_text")
    if value is None:
        return None
    text = str(value).strip()
    return text or None
