"""Closed vocabularies of the semantics layer (intents, service kinds, cities).

The core does not know what the company's cost object is: a line reading gives a
proper name, a client hint, dates, a city and a service kind - the extension
decides what they mean (phase 4: anchors of cost object candidates).
"""

from __future__ import annotations

from kasa.core.sensitive import fold

INTENTS: tuple[str, ...] = (
    "invoice_payment", "advance", "refund", "rent", "salary", "tax", "zus", "fee", "own_transfer",
    "subscription", "deposit", "unknown",
)

SERVICE_KINDS: tuple[str, ...] = ("catering", "hotel", "technika", "personel", "sala", "transport", "saas", "other")

# canonical city -> forms seen in titles and lines (folded on lookup; also without Polish letters)
_CITY_FORMS: dict[str, tuple[str, ...]] = {
    "Warszawa": ("warszawa", "warszawy", "warszawie", "warszawe", "warsaw"),
    "Kraków": ("krakow", "krakowa", "krakowie", "cracow", "krakau"),
    "Gdańsk": ("gdansk", "gdanska", "gdansku"),
    "Gdynia": ("gdynia", "gdyni"),
    "Sopot": ("sopot", "sopotu", "sopocie"),
    "Wrocław": ("wroclaw", "wroclawia", "wroclawiu"),
    "Łódź": ("lodz", "lodzi"),
    "Poznań": ("poznan", "poznania", "poznaniu"),
    "Katowice": ("katowice", "katowic", "katowicach"),
    "Szczecin": ("szczecin", "szczecina", "szczecinie"),
    "Lublin": ("lublin", "lublina", "lublinie"),
    "Bydgoszcz": ("bydgoszcz", "bydgoszczy"),
    "Toruń": ("torun", "torunia", "toruniu"),
    "Rzeszów": ("rzeszow", "rzeszowa", "rzeszowie"),
    "Białystok": ("bialystok", "bialegostoku", "bialymstoku"),
    "Olsztyn": ("olsztyn", "olsztyna", "olsztynie"),
    "Kielce": ("kielce", "kielc", "kielcach"),
    "Opole": ("opole", "opola", "opolu"),
    "Zakopane": ("zakopane", "zakopanego", "zakopanem"),
    "Częstochowa": ("czestochowa", "czestochowy", "czestochowie"),
    "Gliwice": ("gliwice", "gliwic", "gliwicach"),
    "Berlin": ("berlin", "berlina", "berlinie"),
    "Praga": ("praga", "pragi", "pradze", "prague"),
    "Wiedeń": ("wieden", "wiednia", "wiedniu", "vienna"),
    "Londyn": ("londyn", "londynu", "london"),
    "Dublin": ("dublin", "dublina", "dublinie"),
    "San Francisco": ("san francisco",),
}
CITY_BY_FORM: dict[str, str] = {form: city for city, forms in _CITY_FORMS.items() for form in forms}


def canonical_city(value: str | None) -> str | None:
    """'Krakowie', 'KRAKOW', 'Kraków' -> 'Kraków'; None when it is not a known city."""
    if not value:
        return None
    return CITY_BY_FORM.get(" ".join(fold(value).split()))
