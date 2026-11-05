#!/usr/bin/env python3
"""Deterministic generator of ODM Partners test fixtures for October 2026.

Run:   python fixtures/generate.py
Output: fixtures/odm/** (committed to the repo).

This script is the single source of truth for the scenario: to change the data,
change this file and regenerate. Every random value comes from a generator
seeded with SEED=42 and a stable per-entity key, so adding an entity does not
shift the identifiers of the others.

Requires: reportlab (PDF) and openpyxl (XLSX); everything else is stdlib.
The script asserts the internal consistency of the scenario (balances, MPP,
invoice sums, matches, receivables, margins) and fails loudly if it breaks.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import random
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree as ET

SEED = 42
HERE = Path(__file__).resolve().parent
OUT = HERE / "odm"
ASOF = date(2026, 10, 31)
PERIOD_FROM = date(2026, 10, 1)
PERIOD_TO = date(2026, 10, 31)
KSEF_WINDOW_FROM = date(2026, 9, 14)  # mock KSeF holds everything accepted from this day on
FIXED_ZIP_TIME = (2026, 11, 1, 0, 0, 0)

FA_NS = "http://crd.gov.pl/wzor/2025/06/25/13775/"
JPK_NS = "http://crd.gov.pl/wzor/2021/12/27/11148/"
CAMT_NS = "urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"


def R(key: str) -> random.Random:
    """Per-entity deterministic generator (string seeds are stable across runs)."""
    return random.Random(f"{SEED}:{key}")


# ---------------------------------------------------------------------------
# Money and dates
# ---------------------------------------------------------------------------

TWO = Decimal("0.01")


def D(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def r2(x) -> Decimal:
    return D(x).quantize(TWO, rounding=ROUND_HALF_UP)


def s2(x) -> str:
    """Machine format: '1234.56'."""
    return f"{r2(x):.2f}"


def pl2(x, sep: str = " ") -> str:
    """Polish human format: '1 234,56'."""
    q = r2(x)
    neg = q < 0
    whole, frac = f"{abs(q):.2f}".split(".")
    groups = []
    while whole:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    return ("-" if neg else "") + sep.join(groups) + "," + frac


def gr(x) -> int:
    return int(r2(x) * 100)


def d(s: str) -> date:
    return date.fromisoformat(s)


def waw_offset(day: date) -> str:
    # Europe/Warsaw: CEST until the last Sunday of October (25.10.2026 03:00).
    return "+02:00" if date(2026, 3, 29) <= day < date(2026, 10, 25) else "+01:00"


def ts(day: date, hh: int = 12, mm: int = 0, ss: int = 0) -> str:
    return f"{day.isoformat()}T{hh:02d}:{mm:02d}:{ss:02d}{waw_offset(day)}"


def utc_ts(day: date, hh: int, mm: int, ss: int = 0) -> str:
    off = 2 if waw_offset(day) == "+02:00" else 1
    dt = datetime(day.year, day.month, day.day, hh, mm, ss) - timedelta(hours=off)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def dpl(day: date) -> str:
    return day.strftime("%d.%m.%Y")


def business_days(start: date, end: date):
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            yield cur
        cur += timedelta(days=1)


def prev_business_day(day: date) -> date:
    cur = day - timedelta(days=1)
    while cur.weekday() >= 5:
        cur -= timedelta(days=1)
    return cur


# ---------------------------------------------------------------------------
# Identifiers: NIP, NRB/IBAN (mod-97), German USt-IdNr, KSeF number (CRC-8)
# ---------------------------------------------------------------------------

NIP_WEIGHTS = (6, 5, 7, 2, 3, 4, 5, 6, 7)


def nip_valid(nip: str) -> bool:
    if not re.fullmatch(r"\d{10}", nip or ""):
        return False
    check = sum(int(a) * b for a, b in zip(nip[:9], NIP_WEIGHTS)) % 11
    return check != 10 and check == int(nip[9])


def make_nip(prefix: str, rng: random.Random) -> str:
    while True:
        body = prefix + "".join(str(rng.randint(0, 9)) for _ in range(9 - len(prefix)))
        check = sum(int(a) * b for a, b in zip(body, NIP_WEIGHTS)) % 11
        if check != 10:
            return body + str(check)


def sort_code(bank3: str, branch4: str) -> str:
    """Polish bank sort code (8 digits) with its own check digit (weights 3,9,7,1)."""
    digits = bank3 + branch4
    check = sum(int(a) * b for a, b in zip(digits, (3, 9, 7, 1, 3, 9, 7))) % 10
    return digits + str((10 - check) % 10)


def _iban_int(s: str) -> int:
    return int("".join(str(int(ch, 36)) for ch in s))


def iban_make(country: str, bban: str) -> str:
    check = 98 - _iban_int(bban + country + "00") % 97
    return f"{country}{check:02d}{bban}"


def iban_valid(iban: str) -> bool:
    s = iban.replace(" ", "")
    return bool(re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]+", s)) and _iban_int(s[4:] + s[:4]) % 97 == 1


def iban_fmt(iban: str) -> str:
    return " ".join(iban[i:i + 4] for i in range(0, len(iban), 4))


def nrb(iban: str) -> str:
    """26-digit Polish account number as printed on invoices (no country code)."""
    assert iban.startswith("PL") and len(iban) == 28
    return iban[2:]


def nrb_fmt(iban: str) -> str:
    n = nrb(iban)
    return n[:2] + " " + " ".join(n[i:i + 4] for i in range(2, 26, 4))


def de_vat_check(d8: str) -> int:
    """ISO 7064 MOD 11,10 check digit used by the German USt-IdNr."""
    product = 10
    for ch in d8:
        total = (int(ch) + product) % 10 or 10
        product = (2 * total) % 11
    check = 11 - product
    return 0 if check == 10 else check


def make_de_vat(rng: random.Random) -> str:
    body = str(rng.randint(1, 9)) + "".join(str(rng.randint(0, 9)) for _ in range(7))
    return f"DE{body}{de_vat_check(body)}"


def de_vat_valid(vat: str) -> bool:
    m = re.fullmatch(r"DE(\d{8})(\d)", vat)
    return bool(m) and m.group(1)[0] != "0" and de_vat_check(m.group(1)) == int(m.group(2))


def crc8(data: bytes) -> int:
    """CRC-8, polynomial 0x07, init 0x00 (checksum of the KSeF number)."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def ksef_number(nip: str, accepted: date, rng: random.Random) -> str:
    core = f"{nip}-{accepted:%Y%m%d}-{rng.getrandbits(48):012X}"
    return f"{core}-{crc8(core.encode()):02X}"


KSEF_RE = re.compile(r"^(\d{10})-(\d{8})-([0-9A-F]{12})-([0-9A-F]{2})$")


def ksef_valid(number: str) -> bool:
    m = KSEF_RE.match(number)
    return bool(m) and len(number) == 35 and nip_valid(m.group(1)) and \
        crc8(number[:32].encode()) == int(m.group(4), 16)


def normalize_number(raw: str) -> str:
    """Same rule as kasa.core.normalize: upper case, separators dropped, leading zeros dropped."""
    tokens = re.findall(r"[^\W\d_]+|\d+", raw.upper())
    return "".join(t.lstrip("0") or "0" if t.isdigit() else t for t in tokens)


def phone(rng: random.Random) -> str:
    return "500" + "".join(str(rng.randint(0, 9)) for _ in range(6))


def phone_fmt(p: str) -> str:
    return f"{p[:3]} {p[3:6]} {p[6:]}"


def phone_dash(p: str) -> str:
    return f"{p[:3]}-{p[3:6]}-{p[6:]}"


# ---------------------------------------------------------------------------
# Parties (all invented; NIPs, VAT ids and IBANs have valid checksums)
# ---------------------------------------------------------------------------

BANKS = {
    "PKO": ("102", "PKO Bank Polski S.A.", "BPKOPLPW"),
    "PEKAO": ("124", "Bank Pekao S.A.", "PKOPPLPW"),
    "ING": ("105", "ING Bank Śląski S.A.", "INGBPLPW"),
    "SAN": ("109", "Santander Bank Polska S.A.", "WBKPPLPP"),
    "ALIOR": ("249", "Alior Bank S.A.", "ALBPPLPW"),
    "BNP": ("160", "BNP Paribas Bank Polska S.A.", "PPABPLPK"),
    "MBANK": ("114", "mBank S.A.", "BREXPLPWMBK"),
    "MILL": ("116", "Bank Millennium S.A.", "BIGBPLPW"),
}


def pl_iban(bank: str, rng: random.Random) -> str:
    code = BANKS[bank][0]
    branch = "".join(str(rng.randint(0, 9)) for _ in range(4))
    account = "".join(str(rng.randint(0, 9)) for _ in range(16))
    return iban_make("PL", sort_code(code, branch) + account)


@dataclass
class Party:
    key: str
    name: str
    nip: str | None = None
    vat_eu: str | None = None
    country: str = "PL"
    addr1: str = ""
    addr2: str = ""
    iban: str | None = None
    bank: str | None = None
    bic: str | None = None
    person: bool = False          # natural person (pseudonymisation target)
    person_name: str | None = None  # natural person hidden in a business name (JDG)
    phone: str | None = None
    email: str | None = None
    role: str = ""

    @property
    def tax_id(self) -> str | None:
        return self.nip or self.vat_eu


P: dict[str, Party] = {}


def company(key, name, nip_prefix, addr1, addr2, bank, role, email=None, person_name=None) -> Party:
    rng = R(f"party:{key}")
    p = Party(key=key, name=name, nip=make_nip(nip_prefix, rng), addr1=addr1, addr2=addr2,
              iban=pl_iban(bank, rng) if bank else None,
              bank=BANKS[bank][1] if bank else None, bic=BANKS[bank][2] if bank else None,
              role=role, email=email, person_name=person_name)
    P[key] = p
    return p


def person(key, name, bank, addr1="", addr2="", role="") -> Party:
    rng = R(f"person:{key}")
    p = Party(key=key, name=name, iban=pl_iban(bank, rng), bank=BANKS[bank][1], bic=BANKS[bank][2],
              person=True, phone=phone(rng), addr1=addr1, addr2=addr2, role=role)
    P[key] = p
    return p


ODM = Party(key="odm", name="ODM Partners sp. z o.o.", nip="5342650778",
            addr1="ul. Chmielna 73 lok. 4", addr2="00-801 Warszawa", email="finanse@odmpartners.test")
P["odm"] = ODM

# Own accounts (fixed numbers, mod-97 computed).
ACCOUNTS = {
    "MB": dict(slug="mbank_biezacy", iban=iban_make("PL", sort_code("114", "0200") + "0000350277812345"),
               name="mBank Biznes - rachunek bieżący PLN", bank="mBank S.A.", bic="BREXPLPWMBK",
               product="mBiznes konto Standard", kind="current", opening=D("142386.52"),
               card="5375 **** **** 4417", balance_after=True),
    "MV": dict(slug="mbank_vat", iban=iban_make("PL", sort_code("114", "0200") + "0000310277812346"),
               name="mBank Biznes - rachunek VAT", bank="mBank S.A.", bic="BREXPLPWMBK",
               product="Rachunek VAT (split payment)", kind="vat", opening=D("6912.00"),
               card=None, balance_after=True),
    "ML": dict(slug="millennium_biezacy", iban=iban_make("PL", sort_code("116", "0220") + "0000000412906733"),
               name="Millennium - rachunek bieżący PLN", bank="Bank Millennium S.A.", bic="BIGBPLPW",
               product="Konto Biznes Millennium", kind="current", opening=D("38450.19"),
               card="4026 **** **** 9031", balance_after=False),
}
for _code, _acc in ACCOUNTS.items():
    _uid_rng = R(f"acct:{_code}")
    _acc["uid"] = "%08x-%04x-4%03x-a%03x-%012x" % tuple(_uid_rng.getrandbits(b) for b in (32, 16, 12, 12, 48))
MB, MV, ML = (ACCOUNTS[k]["iban"] for k in ("MB", "MV", "ML"))

# Clients
company("acme", "ACME Polska sp. z o.o.", "521", "ul. Prosta 68", "00-838 Warszawa", "PKO", "klient",
        email="ksiegowosc@acmepolska.test")
company("nordis", "Nordis Retail sp. z o.o.", "586", "ul. Świętojańska 41", "81-391 Gdynia", "PEKAO", "klient",
        email="faktury@nordis.test")
company("fala", "Agencja Fala sp. z o.o.", "525", "ul. Grochowska 306", "03-840 Warszawa", "ING", "klient")
company("kwadrat", "Kwadrat Development sp. z o.o.", "677", "ul. Rakowicka 7", "31-511 Kraków", None, "klient")
company("orbita", "Orbita Media sp. z o.o.", "779", "ul. Garbary 48", "61-758 Poznań", "SAN", "klient")
_h = R("party:helios")
P["helios"] = Party(key="helios", name="Helios Software GmbH", vat_eu=make_de_vat(_h), country="DE",
                    addr1="Torstraße 140", addr2="10119 Berlin",
                    iban=iban_make("DE", "10090000" + "".join(str(_h.randint(0, 9)) for _ in range(10))),
                    bank="Berliner Volksbank eG", bic="BEVODEBBXXX", role="klient")
# Kwadrat pays from a Revolut Business account (LT IBAN): the bank shows "Revolut", the client is in the title.
_k = R("party:kwadrat:revolut")
P["kwadrat"].iban = iban_make("LT", "32500" + "".join(str(_k.randint(0, 9)) for _ in range(11)))
P["kwadrat"].bank, P["kwadrat"].bic = "Revolut Bank UAB", "REVOLT21"

# Suppliers
company("biuro_centrum", "Biuro Centrum sp. z o.o.", "526", "ul. Złota 59", "00-120 Warszawa", "PEKAO", "dostawca")
company("wawel", "Hotel Wawel Grand sp. z o.o.", "676", "ul. Stradomska 21", "31-068 Kraków", "PKO", "dostawca",
        email="recepcja@wawelgrand.test")
company("smaki", "Smaki Wisły Catering sp. z o.o.", "679", "ul. Zabłocie 23", "30-701 Kraków", "ALIOR", "dostawca")
company("scena", "Scena Pro sp. z o.o.", "951", "ul. Annopol 3", "03-236 Warszawa", "MBANK", "dostawca")
company("kadr", "Kadr Studio Paweł Wierzbicki", "583", "ul. Grunwaldzka 211/4", "80-266 Gdańsk", "ING", "dostawca",
        person_name="Paweł Wierzbicki")
company("orange", "Orange Polska S.A.", "526", "ul. Telekomunikacyjna 1", "02-326 Warszawa", None, "dostawca")
company("mobilis", "Mobilis Leasing sp. z o.o.", "527", "ul. Domaniewska 39", "02-672 Warszawa", "BNP", "dostawca")
company("bilans", "Biuro Rachunkowe Bilans sp. z o.o.", "534", "ul. Okrężna 9", "05-825 Grodzisk Mazowiecki", "SAN",
        "dostawca")
company("kreska", "Studio Graficzne Kreska sp. z o.o.", "897", "ul. Ruska 46", "50-079 Wrocław", "MBANK", "dostawca")
company("drukarnia", "Drukarnia Format sp. z o.o.", "725", "ul. Piotrkowska 270", "90-361 Łódź", "PKO", "dostawca")
company("hala", "Hala Expo Łódź sp. z o.o.", "727", "al. Politechniki 4", "93-590 Łódź", "PEKAO", "dostawca")
company("bufet", "Bufet Mazowsze sp. z o.o.", "113", "ul. Ząbkowska 27", "03-736 Warszawa", "ING", "dostawca")
company("odra", "Przestrzeń Odra sp. z o.o.", "898", "ul. Wybrzeże Wyspiańskiego 20", "50-370 Wrocław", "SAN",
        "dostawca")
company("loft", "Loft Praga sp. z o.o.", "524", "ul. Mińska 25", "03-808 Warszawa", "MILL", "dostawca")
company("kalina", "Dwór Kalina Event sp. z o.o.", "944", "Kalina 12", "32-050 Skawina", "PKO", "dostawca")
company("petrova", "PETROVA S.A. Stacja Paliw nr 214", "728", "ul. Rokicińska 154", "92-412 Łódź", None, "dostawca")

# Public bodies and the bailiff: individual accounts derived from ODM's NIP (simplified structure).
P["zus"] = Party(key="zus", name="Zakład Ubezpieczeń Społecznych", addr1="ul. Szamocka 3, 5", addr2="01-748 Warszawa",
                 iban=iban_make("PL", "60000002" + "026" + ODM.nip + "000"), bank="ZUS", role="publiczny")
P["us"] = Party(key="us", name="Urząd Skarbowy - mikrorachunek podatkowy", addr2="Warszawa",
                iban=iban_make("PL", "10100071" + "222" + ODM.nip + "000"), bank="NBP", role="publiczny")
company("komornik", "Komornik Sądowy przy Sądzie Rejonowym dla Warszawy-Mokotowa Rafał Ziemiański",
        "951", "ul. Puławska 98 lok. 12", "02-620 Warszawa", "PEKAO", "komornik", person_name="Rafał Ziemiański")
P["revolut"] = Party(key="revolut", name="Revolut Bank UAB", country="LT", addr2="Vilnius", role="pośrednik")

# Foreign SaaS (documents arrive as PDF by e-mail; never in KSeF). Tax ids are invented.
P["google"] = Party(key="google", name="Google Cloud EMEA Limited", vat_eu="IE6392017LH", country="IE",
                    addr1="70 Sir John Rogerson's Quay", addr2="Dublin 2, Ireland", role="dostawca")
P["anthropic"] = Party(key="anthropic", name="Anthropic, PBC", vat_eu="EU372041857", country="US",
                       addr1="500 Market Street, Suite 900", addr2="San Francisco, CA 94105, USA", role="dostawca")
P["notion"] = Party(key="notion", name="Notion Labs, Inc.", vat_eu="EU372058163", country="US",
                    addr1="548 Folsom Street", addr2="San Francisco, CA 94105, USA", role="dostawca")

# Natural persons (invented names; phones 500-xxx-xxx)
person("mazur", "Anna Mazur", "MBANK", "ul. Ogrodowa 14/2", "00-876 Warszawa", "pracownik (PM)")
person("sowinski", "Bartłomiej Sowiński", "PKO", role="pracownik")
person("wierzchowska", "Karolina Wierzchowska", "ING", role="pracownik")
person("grabowiecki", "Tomasz Grabowiecki", "SAN", role="pracownik (dłużnik w egzekucji)")
person("zawadzki", "Marek Zawadzki", "ALIOR", "ul. Lipowa 12/3", "31-216 Kraków", "konferansjer (rachunek)")
person("wrona", "Jakub Wrona", "MBANK", role="scenograf (umowa o dzieło)")
person("michalak", "Dawid Michalak", "PKO", role="pomocnik (BLIK bez wiersza w arkuszu)")
HOSTESSES = [
    # key, name, bank
    ("lewandowicz", "Maja Lewandowicz", "PKO"), ("drozdowska", "Hanna Drozdowska", "ING"),
    ("wojcicka", "Natalia Wójcicka", "MBANK"), ("sobieraj", "Julia Sobieraj", "PEKAO"),
    ("karczmarek", "Oliwia Karczmarek", "SAN"), ("pietrasik", "Zuzanna Pietrasik", "PKO"),
    ("rutkowska", "Kinga Rutkowska", "ING"), ("wesolowska", "Paula Wesołowska", "MBANK"),
    ("jablonowska", "Ewa Jabłonowska", "ALIOR"), ("zawilska", "Amelia Zawilska", "PKO"),
    ("kuczynska", "Lena Kuczyńska", "PEKAO"), ("bartoszek", "Wiktoria Bartoszek", "SAN"),
    ("grabarczyk", "Alicja Grabarczyk", "ING"), ("mikulska", "Laura Mikulska", "MBANK"),
    ("szafranska", "Nikola Szafrańska", "PKO"), ("olejniczak", "Martyna Olejniczak", "BNP"),
    ("sadowska", "Weronika Sadowska", "PKO"),
]
for _key, _name, _bank in HOSTESSES:
    person(_key, _name, _bank, role="hostessa (umowa zlecenie)")

# Events (ODM cost objects). Lists are discovered from data in the product;
# here they are the ground truth the discovery should reach.
EVENTS = {
    "EV-01": dict(name="Gala ACME 2026", client="acme", date_from=d("2026-10-08"), date_to=d("2026-10-10"),
                  city="Kraków", pm_label="Gala ACME"),
    "EV-02": dict(name="Roadshow Nordis Gdańsk", client="nordis", date_from=d("2026-10-03"), date_to=d("2026-10-03"),
                  city="Gdańsk", pm_label="Nordis Gdańsk"),
    "EV-03": dict(name="Roadshow Nordis Wrocław", client="nordis", date_from=d("2026-10-17"),
                  date_to=d("2026-10-17"), city="Wrocław", pm_label="Nordis Wrocław"),
    "EV-04": dict(name="Roadshow Nordis Łódź", client="nordis", date_from=d("2026-10-24"), date_to=d("2026-10-24"),
                  city="Łódź", pm_label="Nordis Łódź"),
    "EV-05": dict(name="Helios Partner Summit 2026", client="helios", date_from=d("2026-10-15"),
                  date_to=d("2026-10-16"), city="Warszawa", pm_label="Helios Summit"),
}
UNASSIGNED = "UNASSIGNED"
QUEUE = "queue"

# Fictional NBP table A mid rates. Pinned days are the ones the scenario uses.
NBP_PINNED = {
    ("EUR", d("2026-10-13")): D("4.2512"),   # Google Workspace invoice of 14.10
    ("EUR", d("2026-10-15")): D("4.2468"),   # Helios invoice, sale date 16.10
    ("USD", d("2026-09-30")): D("3.8420"),   # Anthropic invoice of 01.10
    ("USD", d("2026-10-20")): D("3.8275"),   # Notion invoice of 21.10
}


def build_nbp_rates() -> dict:
    rates = {"EUR": {}, "USD": {}}
    for cur, base in (("EUR", D("4.2500")), ("USD", D("3.8400"))):
        rng = R(f"nbp:{cur}")
        value = base
        for day in business_days(d("2026-09-01"), d("2026-10-30")):
            value = (value + D(rng.randint(-60, 60)) / D(10000)).quantize(D("0.0001"))
            rates[cur][day] = NBP_PINNED.get((cur, day), value)
    return rates


NBP = build_nbp_rates()


def nbp_rate(cur: str, doc_day: date) -> tuple[Decimal, date]:
    """Rate of the last business day before the document date (art. 31a VAT)."""
    day = prev_business_day(doc_day)
    return NBP[cur][day], day


# ---------------------------------------------------------------------------
# Invoices (KSeF FA(3))
# ---------------------------------------------------------------------------

RATE_FIELDS = {"23": ("P_13_1", "P_14_1"), "8": ("P_13_2", "P_14_2"), "5": ("P_13_3", "P_14_3"),
               "zw": ("P_13_7", None), "np I": ("P_13_8", None), "np II": ("P_13_9", None)}
RATE_VALUE = {"23": D("0.23"), "8": D("0.08"), "5": D("0.05"), "zw": D(0), "np I": D(0), "np II": D(0)}
RATE_ORDER = ["23", "8", "5", "zw", "np I", "np II"]


@dataclass
class Line:
    name: str
    unit: str
    qty: Decimal
    price: Decimal
    rate: str
    before: bool = False  # KOR: row describes the state before the correction (StanPrzed)

    @property
    def net(self) -> Decimal:
        return r2(self.qty * self.price)


def L(name, unit, qty, price, rate="23", before=False) -> Line:
    return Line(name=name, unit=unit, qty=D(qty), price=D(price), rate=rate, before=before)


@dataclass
class Invoice:
    key: str
    direction: str            # sales | purchase
    number: str
    kind: str                 # VAT | KOR | ZAL | ROZ
    seller: Party
    buyer: Party
    issue: date
    lines: list
    sale: date | None = None
    period: tuple | None = None
    due: date | None = None
    currency: str = "PLN"
    mpp: bool = False
    reverse_charge: bool = False
    corrects: str | None = None
    correction_reason: str | None = None
    correction_type: int = 2
    advances: list = field(default_factory=list)
    advance_net: Decimal | None = None
    paid_on: date | None = None
    pay_to: str | None = None
    accepted_day: date | None = None
    offline: bool = False
    system: str = "Fakturownia"
    expected_event: str = UNASSIGNED
    signal: str = "brak sygnału"
    final: list = field(default_factory=lambda: [(UNASSIGNED, Fraction(1))])
    suggested: list | None = None
    jpk_number: str | None = None   # number as written in JPK (None = not in JPK)
    pdf: str | None = None
    note: str = ""
    ksef: str = ""
    accepted_ts: str = ""
    totals: dict = field(default_factory=dict)

    @property
    def counterparty(self) -> Party:
        return self.buyer if self.direction == "sales" else self.seller

    @property
    def net(self) -> Decimal:
        return sum((v[0] for v in self.totals.values()), D(0))

    @property
    def vat(self) -> Decimal:
        return sum((v[1] for v in self.totals.values()), D(0))

    @property
    def gross(self) -> Decimal:
        return self.net + self.vat

    @property
    def fx(self) -> Decimal | None:
        if self.currency == "PLN":
            return None
        return nbp_rate(self.currency, self.sale or self.issue)[0]

    def pln(self, amount: Decimal) -> Decimal:
        return amount if self.fx is None else r2(amount * self.fx)

    @property
    def gross_pln(self) -> Decimal:
        return self.pln(self.gross)

    @property
    def economic_net(self) -> Decimal:
        """Net value that counts as revenue/cost (memoriał): ZAL = 0, ROZ = full order."""
        if self.kind == "ZAL":
            return D(0)
        if self.kind == "ROZ":
            return sum((ln.net for ln in self.lines), D(0))
        return self.net


INV: dict[str, Invoice] = {}


def inv(**kw) -> Invoice:
    i = Invoice(**kw)
    assert i.key not in INV, i.key
    INV[i.key] = i
    return i


def compute_totals(i: Invoice) -> dict:
    if i.kind == "ZAL":
        rate = i.lines[0].rate
        net = r2(i.advance_net)
        return {rate: (net, r2(net * RATE_VALUE[rate]))}
    per: dict[str, Decimal] = {}
    for ln in i.lines:
        sign = -1 if (i.kind == "KOR" and ln.before) else 1
        per[ln.rate] = per.get(ln.rate, D(0)) + sign * ln.net
    totals = {rate: (r2(net), r2(net * RATE_VALUE[rate])) for rate, net in per.items()}
    if i.kind == "ROZ":
        for adv_key in i.advances:
            for rate, (net, vat) in INV[adv_key].totals.items():
                n0, v0 = totals[rate]
                totals[rate] = (n0 - net, v0 - vat)
    return {rate: totals[rate] for rate in RATE_ORDER if rate in totals}


def alloc(*pairs) -> list:
    """Allocation as exact fractions: alloc(("EV-02", "1/3"), ...)."""
    return [(ev, share if isinstance(share, Fraction) else Fraction(str(share))) for ev, share in pairs]


ODM_MB, ODM_ML = MB, ML
S, PU = "sales", "purchase"

# --- Sales: September (KSeF window from 14.09, also in JPK_V7M 09/2026) ---
inv(key="S-FALA-LED", direction=S, number="FV 4/09/2026", kind="VAT", seller=ODM, buyer=P["fala"],
    issue=d("2026-09-14"), sale=d("2026-09-13"), due=d("2026-09-28"), pay_to=ODM_ML,
    lines=[L("Wynajem ekranu LED P3,9 (12 m²) z obsługą techniczną, 12-13.09.2026", "kpl.", 1, "7500.00")],
    jpk_number="FV 4/09/2026", note="Klient płaci częściowo (5 000 z 9 225) - reszta po terminie.")
inv(key="S-ORBITA-SEP", direction=S, number="FV 5/09/2026", kind="VAT", seller=ODM, buyer=P["orbita"],
    issue=d("2026-09-28"), sale=d("2026-09-26"), due=d("2026-10-12"), pay_to=ODM_MB,
    lines=[L("Wynajem fotobudki z obsługą i wydrukami - impreza firmowa, 26.09.2026", "usł.", 1, "2500.00")],
    jpk_number="FV/5/09/2026", note="Zapłacona dwa razy (duplikat), nadpłata zwrócona 21.10.")

# --- Sales: October ---
inv(key="S-ACME-ZAL", direction=S, number="FV 1/10/2026", kind="ZAL", seller=ODM, buyer=P["acme"],
    issue=d("2026-10-02"), sale=d("2026-10-01"), paid_on=d("2026-10-01"), pay_to=ODM_MB, advance_net=D("24000.00"),
    lines=[L("Organizacja Gali ACME 2026 (Kraków, 08-10.10.2026) - usługa kompleksowa wg umowy 14/2026",
             "usł.", 1, "80000.00")],
    expected_event="EV-01", signal="A: nazwa eventu w pozycji", final=alloc(("EV-01", 1)),
    note="Faktura zaliczkowa wystawiona dzień PO wpłacie zaliczki (01.10). Zaliczka nie jest przychodem.")
inv(key="S-NORDIS-GDA", direction=S, number="FV 2/10/2026", kind="VAT", seller=ODM, buyer=P["nordis"],
    issue=d("2026-10-05"), sale=d("2026-10-03"), due=d("2026-10-19"), pay_to=ODM_ML,
    lines=[L("Materiały promocyjne i branding strefy wejścia - Roadshow Nordis Gdańsk 03.10.2026",
             "kpl.", 1, "8130.08")],
    expected_event="EV-02", signal="A: nazwa eventu w pozycji", final=alloc(("EV-02", 1)))
inv(key="S-ACME-ROZ", direction=S, number="FV 3/10/2026", kind="ROZ", seller=ODM, buyer=P["acme"],
    issue=d("2026-10-12"), sale=d("2026-10-10"), due=d("2026-10-26"), pay_to=ODM_MB, advances=["S-ACME-ZAL"],
    lines=[L("Organizacja Gali ACME 2026 (Kraków, 08-10.10.2026) - usługa kompleksowa wg umowy 14/2026",
             "usł.", 1, "80000.00")],
    expected_event="EV-01", signal="A: nazwa eventu w pozycji", final=alloc(("EV-01", 1)),
    note="Rozliczeniowa: pozycje = całe zamówienie 80 000 netto, kwoty P_13/P_14/P_15 = do zapłaty po zaliczce.")
inv(key="S-NORDIS-KOR", direction=S, number="KOR 1/10/2026", kind="KOR", seller=ODM, buyer=P["nordis"],
    issue=d("2026-10-14"), pay_to=ODM_ML, corrects="S-NORDIS-GDA",
    correction_reason="Rabat za opóźniony montaż strefy wejścia - Roadshow Nordis Gdańsk",
    lines=[L("Materiały promocyjne i branding strefy wejścia - Roadshow Nordis Gdańsk 03.10.2026",
             "kpl.", 1, "8130.08", before=True),
           L("Materiały promocyjne i branding strefy wejścia - Roadshow Nordis Gdańsk 03.10.2026",
             "kpl.", 1, "6504.06")],
    expected_event="EV-02", signal="A: korekta dziedziczy podział + opis wskazuje event",
    final=alloc(("EV-02", 1)), note="Korekta zmniejszająca -2 000,00 brutto; VAT liczony od różnicy.")
inv(key="S-KWADRAT", direction=S, number="FV 4/10/2026", kind="VAT", seller=ODM, buyer=P["kwadrat"],
    issue=d("2026-10-14"), period=(d("2026-09-15"), d("2026-10-14")), due=d("2026-10-28"), pay_to=ODM_MB,
    lines=[L("Wynajem zestawu nagłośnieniowego (line array 2x12) bez obsługi, okres 15.09-14.10.2026",
             "mies.", 1, "12000.00")],
    note="Okres usługi zamiast daty sprzedaży; przychód rozkłada się po dniach: 16/30 wrzesień, 14/30 październik.")
inv(key="S-HELIOS", direction=S, number="FV 5/10/2026", kind="VAT", seller=ODM, buyer=P["helios"],
    issue=d("2026-10-16"), sale=d("2026-10-16"), due=d("2026-10-30"), pay_to=ODM_MB, currency="EUR",
    reverse_charge=True,
    lines=[L("Event management services - Helios Partner Summit 2026, Warsaw, 15-16.10.2026",
             "usł.", 1, "12500.00", "np II")],
    expected_event="EV-05", signal="A: nazwa eventu w pozycji", final=alloc(("EV-05", 1)),
    note="Faktura w EUR, usługa dla podatnika z UE (np II, odwrotne obciążenie); kurs NBP z 15.10 w pozycji.")
inv(key="S-FALA-LIGHT", direction=S, number="FV 6/10/2026", kind="VAT", seller=ODM, buyer=P["fala"],
    issue=d("2026-10-19"), sale=d("2026-10-18"), due=d("2026-11-02"), pay_to=ODM_ML,
    lines=[L("Wynajem oświetlenia scenicznego (12x moving head, konsola) z obsługą, 17-18.10.2026",
             "kpl.", 1, "5000.00")],
    note="Płatna w listopadzie - otwarta na 31.10, jeszcze niewymagalna.")
inv(key="S-ACME-FV7", direction=S, number="FV 7/10/2026", kind="VAT", seller=ODM, buyer=P["acme"],
    issue=d("2026-10-21"), sale=d("2026-10-10"), due=d("2026-11-04"), pay_to=ODM_MB, mpp=True,
    lines=[L("Dodatkowe atrakcje - Gala ACME 2026: fotobudka 360° (09.10.2026)", "usł.", 1, "4000.00"),
           L("Dodatkowe atrakcje - Gala ACME 2026: pokaz laserowy (09.10.2026)", "usł.", 1, "6000.00")],
    expected_event="EV-01", signal="A: nazwa eventu w pozycji", final=alloc(("EV-01", 1)),
    note="Adnotacja MPP (P_18A=1); klient płaci split paymentem. Ten sam numer ma faktura czynszowa Biura Centrum.")
inv(key="S-NORDIS-WRO", direction=S, number="FV 8/10/2026", kind="VAT", seller=ODM, buyer=P["nordis"],
    issue=d("2026-10-22"), sale=d("2026-10-17"), due=d("2026-11-05"), pay_to=ODM_ML,
    lines=[L("Materiały promocyjne i branding strefy wejścia - Roadshow Nordis Wrocław 17.10.2026",
             "kpl.", 1, "6000.00")],
    expected_event="EV-03", signal="A: nazwa eventu w pozycji", final=alloc(("EV-03", 1)))
inv(key="S-ORBITA-OCT", direction=S, number="FV 9/10/2026", kind="VAT", seller=ODM, buyer=P["orbita"],
    issue=d("2026-10-26"), sale=d("2026-10-24"), due=None, pay_to=ODM_MB,
    lines=[L("Wynajem fotobudki z obsługą - event firmowy, 24.10.2026", "usł.", 1, "3000.00")],
    note="BRAK terminu płatności (pole opcjonalne w FA(3)) - wiek należności liczony od daty wystawienia.")
inv(key="S-NORDIS-ZB", direction=S, number="FV 10/10/2026", kind="VAT", seller=ODM, buyer=P["nordis"],
    issue=d("2026-10-31"), sale=d("2026-10-31"), due=d("2026-11-30"), pay_to=ODM_ML,
    lines=[L("Obsługa eventów - październik 2026 (zamówienie ramowe NR/2026/04)", "usł.", 1, "50000.00")],
    expected_event=QUEUE, signal="faktura zbiorcza - nigdy kotwica; brak ofert -> podział wg osobodni z arkusza PM",
    suggested=alloc(("EV-02", "0.2"), ("EV-03", "0.3"), ("EV-04", "0.5")),
    final=alloc(("EV-02", "0.2"), ("EV-03", "0.3"), ("EV-04", "0.5")),
    note="Jedna pozycja za trzy eventy. Osobodni hostess w arkuszu PM: Gdańsk 2, Wrocław 3, Łódź 5 -> 20/30/50.")

# --- Purchases: September (paid in October) ---
inv(key="P-KRESKA", direction=PU, number="FV 12/09/2026", kind="VAT", seller=P["kreska"], buyer=ODM,
    issue=d("2026-09-25"), sale=d("2026-09-25"), due=d("2026-10-09"), pay_to=P["kreska"].iban,
    lines=[L("Projekt graficzny materiałów - Roadshow Nordis 2026 (Gdańsk, Wrocław, Łódź)", "usł.", 1, "6000.00")],
    expected_event=QUEUE, signal="pozycja wskazuje trzy eventy - podział równy wymaga decyzji",
    suggested=alloc(("EV-02", "1/3"), ("EV-03", "1/3"), ("EV-04", "1/3")),
    final=alloc(("EV-02", "1/3"), ("EV-03", "1/3"), ("EV-04", "1/3")),
    jpk_number="FV 12/09/2026", note="Ten sam numer co wrześniowa faktura czynszowa Biura Centrum w JPK - inny NIP.")
inv(key="P-DRUK", direction=PU, number="214/09/2026", kind="VAT", seller=P["drukarnia"], buyer=ODM,
    issue=d("2026-09-29"), sale=d("2026-09-29"), due=d("2026-10-13"), pay_to=P["drukarnia"].iban,
    lines=[L("Roll-up 85x200 cm z nadrukiem - Gala ACME 2026", "szt.", 6, "150.00"),
           L("Ścianka tekstylna 3x2,3 m z nadrukiem - Gala ACME 2026", "szt.", 1, "1500.00")],
    expected_event="EV-01", signal="A: nazwa eventu w pozycji", final=alloc(("EV-01", 1)),
    jpk_number="214/09/2026")

# --- Purchases: October ---
inv(key="P-CZYNSZ", direction=PU, number="FV 7/10/2026", kind="VAT", seller=P["biuro_centrum"], buyer=ODM,
    issue=d("2026-10-01"), period=(d("2026-10-01"), d("2026-10-31")), due=d("2026-10-10"),
    pay_to=P["biuro_centrum"].iban,
    lines=[L("Najem lokalu biurowego ul. Chmielna 73 lok. 4 - październik 2026", "mies.", 1, "8000.00")],
    note="Numer identyczny z naszą sprzedażową FV 7/10/2026 (ACME) - inny NIP, inny kierunek.")
inv(key="P-BILANS", direction=PU, number="FV/2026/10/033", kind="VAT", seller=P["bilans"], buyer=ODM,
    issue=d("2026-10-05"), sale=d("2026-09-30"), due=d("2026-10-15"), pay_to=P["bilans"].iban,
    lines=[L("Usługi księgowe i kadrowo-płacowe - wrzesień 2026", "usł.", 1, "1500.00")])
inv(key="P-SCENA-GDA", direction=PU, number="SP/2026/10/081", kind="VAT", seller=P["scena"], buyer=ODM,
    issue=d("2026-10-05"), sale=d("2026-10-03"), due=d("2026-10-30"), pay_to=P["scena"].iban,
    lines=[L("Obsługa techniczna (nagłośnienie, oświetlenie) - Roadshow Nordis Gdańsk 03.10.2026",
             "usł.", 1, "4000.00")],
    expected_event="EV-02", signal="A: nazwa eventu w pozycji", final=alloc(("EV-02", 1)))
inv(key="P-KADR-GDA", direction=PU, number="FV 14/2026", kind="VAT", seller=P["kadr"], buyer=ODM,
    issue=d("2026-10-05"), sale=d("2026-10-03"), due=d("2026-10-19"), pay_to=P["kadr"].iban,
    accepted_day=d("2026-10-06"), offline=True,
    lines=[L("Fotorelacja - Roadshow Nordis Gdańsk 03.10.2026", "usł.", 1, "3658.54")],
    expected_event="EV-02", signal="A: nazwa eventu w pozycji", final=alloc(("EV-02", 1)),
    note="Wystawiona w trybie offline: przyjęta w KSeF dzień później (data w numerze KSeF = 06.10).")
inv(key="P-WAWEL", direction=PU, number="FV/2026/10/0412", kind="VAT", seller=P["wawel"], buyer=ODM,
    issue=d("2026-10-10"), period=(d("2026-10-08"), d("2026-10-10")), due=d("2026-10-24"), pay_to=P["wawel"].iban,
    lines=[L("Nocleg w pokoju dwuosobowym, 14 pokoi (08-10.10.2026)", "doba", 28, "420.00", "8"),
           L("Parking hotelowy, 14 miejsc (08-10.10.2026)", "doba", 28, "40.00", "23")],
    expected_event=QUEUE, signal="B: kontrahent + okres usługi (Kraków 08-10.10, jeden event w oknie)",
    suggested=alloc(("EV-01", 1)), final=alloc(("EV-01", 1)), pdf="EM-06",
    note="Ta sama faktura przychodzi też jako PDF w mailu (EM-06) - deduplikacja PDF<->KSeF.")
inv(key="P-SCENA-GALA", direction=PU, number="SP/2026/10/087", kind="VAT", seller=P["scena"], buyer=ODM,
    issue=d("2026-10-12"), sale=d("2026-10-10"), due=d("2026-10-26"), pay_to=P["scena"].iban,
    lines=[L("Nagłośnienie, oświetlenie i multimedia - Gala ACME 2026, Kraków 08-10.10.2026", "usł.", 1,
             "25000.00")],
    expected_event="EV-01", signal="A: nazwa eventu w pozycji", final=alloc(("EV-01", 1)),
    note="Faktura bez adnotacji MPP, ale ODM płaci ją split paymentem (VAT z rachunku VAT).")
inv(key="P-SMAKI", direction=PU, number="2026/10/0193", kind="VAT", seller=P["smaki"], buyer=ODM,
    issue=d("2026-10-12"), sale=d("2026-10-09"), due=d("2026-10-26"), pay_to=P["smaki"].iban,
    lines=[L("Catering - Gala ACME 2026, kolacja galowa 09.10.2026", "os.", 180, "160.00", "8")],
    expected_event="EV-01", signal="A: nazwa eventu w pozycji", final=alloc(("EV-01", 1)))
inv(key="P-MOBILIS", direction=PU, number="L/10/2026/0815", kind="VAT", seller=P["mobilis"], buyer=ODM,
    issue=d("2026-10-12"), sale=d("2026-10-12"), due=d("2026-10-26"), pay_to=P["mobilis"].iban,
    lines=[L("Rata leasingowa 10/36 - umowa L/2025/0815 (samochód dostawczy)", "szt.", 1, "2850.00")],
    note="Pobierana poleceniem zapłaty.")
inv(key="P-LOFT", direction=PU, number="LP/118/2026", kind="VAT", seller=P["loft"], buyer=ODM,
    issue=d("2026-10-16"), sale=d("2026-10-16"), due=d("2026-10-23"), pay_to=P["loft"].iban,
    lines=[L("Wynajem przestrzeni eventowej z wyposażeniem - Helios Partner Summit 2026, 15-16.10.2026",
             "doba", 2, "7500.00")],
    expected_event="EV-05", signal="A: nazwa eventu w pozycji", final=alloc(("EV-05", 1)))
inv(key="P-BUFET", direction=PU, number="BM/1016/2026", kind="VAT", seller=P["bufet"], buyer=ODM,
    issue=d("2026-10-16"), sale=d("2026-10-16"), due=d("2026-11-15"), pay_to=P["bufet"].iban,
    lines=[L("Catering konferencyjny - Helios Partner Summit 2026 (15-16.10.2026)", "os.", 240, "85.00", "8")],
    expected_event="EV-05", signal="A: nazwa eventu w pozycji", final=alloc(("EV-05", 1)),
    note="Płatna w listopadzie - zobowiązanie otwarte na 31.10.")
inv(key="P-SCENA-WRO", direction=PU, number="SP/2026/10/093", kind="VAT", seller=P["scena"], buyer=ODM,
    issue=d("2026-10-19"), sale=d("2026-10-17"), due=d("2026-11-02"), pay_to=P["scena"].iban,
    lines=[L("Obsługa techniczna (nagłośnienie, oświetlenie) - Roadshow Nordis Wrocław 17.10.2026",
             "usł.", 1, "4000.00")],
    expected_event="EV-03", signal="A: nazwa eventu w pozycji", final=alloc(("EV-03", 1)))
inv(key="P-KADR-WRO", direction=PU, number="FV 15/2026", kind="VAT", seller=P["kadr"], buyer=ODM,
    issue=d("2026-10-19"), sale=d("2026-10-17"), due=d("2026-11-02"), pay_to=P["kadr"].iban,
    lines=[L("Fotorelacja - Roadshow Nordis Wrocław 17.10.2026", "usł.", 1, "3658.54")],
    expected_event="EV-03", signal="A: nazwa eventu w pozycji", final=alloc(("EV-03", 1)),
    note="Druga faktura Kadr Studio na 4 500,00 - remis przy przelewie 'zapłata'.")
inv(key="P-ODRA", direction=PU, number="2026/10/27", kind="VAT", seller=P["odra"], buyer=ODM,
    issue=d("2026-10-19"), sale=d("2026-10-17"), due=d("2026-10-26"), pay_to=P["odra"].iban,
    lines=[L("Wynajem sali konferencyjnej A z wyposażeniem AV, 17.10.2026", "doba", 1, "5500.00")],
    expected_event=QUEUE, signal="B: kontrahent + data (Wrocław 17.10, jeden event w oknie)",
    suggested=alloc(("EV-03", 1)), final=alloc(("EV-03", 1)))
inv(key="P-SCENA-LDZ", direction=PU, number="SP/2026/10/101", kind="VAT", seller=P["scena"], buyer=ODM,
    issue=d("2026-10-26"), sale=d("2026-10-24"), due=d("2026-11-09"), pay_to=P["scena"].iban,
    lines=[L("Obsługa techniczna (nagłośnienie, oświetlenie, scena mobilna) - Roadshow Nordis Łódź 24.10.2026",
             "usł.", 1, "5200.00")],
    expected_event="EV-04", signal="A: nazwa eventu w pozycji", final=alloc(("EV-04", 1)))
inv(key="P-HALA", direction=PU, number="HEL/2026/10/0057", kind="VAT", seller=P["hala"], buyer=ODM,
    issue=d("2026-10-26"), sale=d("2026-10-24"), due=d("2026-11-09"), pay_to=P["hala"].iban,
    lines=[L("Wynajem hali B z zapleczem - Roadshow Nordis Łódź 24.10.2026", "doba", 1, "9000.00")],
    expected_event="EV-04", signal="A: nazwa eventu w pozycji", final=alloc(("EV-04", 1)),
    note="Płatna w listopadzie - zobowiązanie otwarte na 31.10.")


def orange_nets(total_gross: Decimal, n: int, rng: random.Random) -> list[Decimal]:
    """n net amounts at 23% whose gross values sum exactly to total_gross."""
    while True:
        nets = [D(rng.randint(12000, 38000)) / 100 for _ in range(n - 1)]
        rest = total_gross - sum(x + r2(x * D("0.23")) for x in nets)
        if not D(100) < rest < D(600):
            continue
        approx = r2(rest / D("1.23"))
        for delta in range(-3, 4):
            cand = approx + D(delta) / 100
            if cand + r2(cand * D("0.23")) == rest:
                return nets + [cand]


ORANGE_TOTAL = D("4380.00")
_orng = R("orange:2026-10")
ORANGE_ACCOUNTS = sorted({"4002" + "".join(str(_orng.randint(0, 9)) for _ in range(4)) for _ in range(40)})[:12]
assert len(ORANGE_ACCOUNTS) == 12
for _idx, (_acct, _net) in enumerate(zip(ORANGE_ACCOUNTS, orange_nets(ORANGE_TOTAL, 12, _orng)), start=1):
    inv(key=f"P-ORANGE-{_idx:02d}", direction=PU, number=f"F/{_acct}/10/26", kind="VAT", seller=P["orange"],
        buyer=ODM, issue=d("2026-10-05"), period=(d("2026-09-01"), d("2026-09-30")), due=d("2026-10-23"),
        pay_to=pl_iban("PEKAO", R(f"orange:va:{_acct}")), system="Orange Billing",
        lines=[L(f"Usługi telekomunikacyjne - konto {_acct}, abonament i połączenia 09.2026", "usł.", 1, _net)],
        note="Jedna z 12 drobnych faktur Orange (każda z własnym rachunkiem wirtualnym).")

for _i in INV.values():
    _i.totals = compute_totals(_i)
    _rng = R(f"ksef:{_i.key}")
    _i.accepted_day = _i.accepted_day or _i.issue
    _hh, _mm, _ss = _rng.randint(8, 18), _rng.randint(0, 59), _rng.randint(0, 59)
    _i.accepted_ts = ts(_i.accepted_day, _hh, _mm, _ss)
    _i.ksef = ksef_number(_i.seller.nip, _i.accepted_day, _rng)
    if _i.direction == S:
        _i.system = "Fakturownia"


# ---------------------------------------------------------------------------
# Internal PM sheet (arkusz PM) - rows are internal documents
# ---------------------------------------------------------------------------

@dataclass
class PmRow:
    id: str
    person: str
    day: date
    event: str
    amount: Decimal
    form: str
    settlement: str
    remarks: str = ""
    same_cost_as: str | None = None  # another document describing the same cost
    note: str = ""


PM_ROWS: list[PmRow] = []


def pm(id_, person_key, day, event, amount, form="BLIK", settlement="umowa zlecenie", remarks="", **kw):
    PM_ROWS.append(PmRow(id=id_, person=person_key, day=d(day), event=event, amount=D(amount), form=form,
                         settlement=settlement, remarks=remarks, **kw))


def tel(key: str) -> str:
    return f"tel. {phone_fmt(P[key].phone)}"


pm("R01", "lewandowicz", "2026-10-03", "EV-02", "400.00", remarks=f"hostessa, {tel('lewandowicz')}")
pm("R02", "drozdowska", "2026-10-03", "EV-02", "400.00", remarks="hostessa")
pm("R03", "mazur", "2026-10-08", "EV-01", "286.40", form="przelew", settlement="zwrot kosztów",
   remarks="dekoracje kwiatowe, paragon w kopercie")
pm("R04", "wojcicka", "2026-10-09", "EV-01", "520.00", remarks=f"koordynatorka hostess, {tel('wojcicka')}")
pm("R05", "sobieraj", "2026-10-09", "EV-01", "450.00", remarks="hostessa")
pm("R06", "karczmarek", "2026-10-09", "EV-01", "450.00", remarks="hostessa")
pm("R07", "pietrasik", "2026-10-09", "EV-01", "450.00", remarks="hostessa")
pm("R08", "zawadzki", "2026-10-09", "EV-01", "1800.00", form="przelew", settlement="faktura",
   remarks="konferansjer, rachunek 4/2026 przyszedł mailem", same_cost_as="EM-05",
   note="Ten sam koszt co rachunek PDF EM-05 i przelew 16.10 - jeden koszt, trzy dowody.")
pm("R09", "rutkowska", "2026-10-15", "EV-05", "900.00", remarks=f"2 dni (15-16.10), {tel('rutkowska')}")
pm("R10", "wesolowska", "2026-10-15", "EV-05", "900.00", remarks="2 dni (15-16.10)")
pm("R11", "jablonowska", "2026-10-15", "EV-05", "900.00", remarks="2 dni (15-16.10)")
pm("R12", "wrona", "2026-10-16", "EV-05", "1808.00", form="przelew", settlement="dzieło",
   remarks="projekt scenografii; brutto 2000, PIT 192 w PIT-4")
pm("R13", "zawilska", "2026-10-17", "EV-03", "400.00", remarks=f"hostessa, {tel('zawilska')}")
pm("R14", "kuczynska", "2026-10-17", "EV-03", "400.00", remarks="hostessa")
pm("R15", "bartoszek", "2026-10-17", "EV-03", "400.00", remarks="hostessa")
pm("R16", "grabarczyk", "2026-10-24", "EV-04", "400.00", remarks=f"hostessa, {tel('grabarczyk')}")
pm("R17", "mikulska", "2026-10-24", "EV-04", "400.00", remarks="hostessa")
pm("R18", "szafranska", "2026-10-24", "EV-04", "400.00", remarks="hostessa")
pm("R19", "olejniczak", "2026-10-24", "EV-04", "400.00", remarks="hostessa")
pm("R20", "sadowska", "2026-10-24", "EV-04", "400.00", remarks="do wypłaty",
   note="Wiersz BEZ odpowiednika w banku (wypłata jeszcze nie wykonana) - koszt memoriałowy jest.")
PM = {r.id: r for r in PM_ROWS}


def pm_display_name(key: str) -> str:
    first, last = P[key].name.split(" ", 1)
    return f"{last} {first}"  # the PM writes "Surname Name"


# ---------------------------------------------------------------------------
# Bank transactions
# ---------------------------------------------------------------------------

CODES = {
    "in": ("PMNT", "RCDT", "DMCT", "PRZELEW PRZYCHODZĄCY"),
    "out": ("PMNT", "ICDT", "DMCT", "PRZELEW WYCHODZĄCY"),
    "express": ("PMNT", "ICDT", "SDVA", "PRZELEW EXPRESS ELIXIR"),
    "mpp_in": ("PMNT", "RCDT", "DMCT", "PRZELEW MPP PRZYCHODZĄCY - NETTO"),
    "mpp_vat_in": ("PMNT", "RCDT", "OTHR", "PRZELEW MPP PRZYCHODZĄCY - VAT"),
    "mpp_out": ("PMNT", "ICDT", "DMCT", "PRZELEW MPP WYCHODZĄCY - NETTO"),
    "mpp_vat_out": ("PMNT", "ICDT", "OTHR", "PRZELEW MPP WYCHODZĄCY - VAT"),
    "foreign_in": ("PMNT", "RCDT", "XBCT", "PRZELEW SEPA PRZYCHODZĄCY"),
    "card": ("PMNT", "CCRD", "POSD", "TRANSAKCJA KARTĄ"),
    "card_fx": ("PMNT", "CCRD", "POSD", "TRANSAKCJA KARTĄ - WALUTA OBCA"),
    "blik": ("PMNT", "ICDT", "OTHR", "PRZELEW NA TELEFON BLIK"),
    "tax": ("PMNT", "ICDT", "TAXS", "PRZELEW DO URZĘDU SKARBOWEGO"),
    "zus": ("PMNT", "ICDT", "OTHR", "PRZELEW DO ZUS"),
    "salary": ("PMNT", "ICDT", "SALA", "PRZELEW - WYNAGRODZENIE"),
    "own_out": ("PMNT", "ICDT", "BOOK", "PRZELEW WŁASNY WYCHODZĄCY"),
    "own_in": ("PMNT", "RCDT", "BOOK", "PRZELEW WŁASNY PRZYCHODZĄCY"),
    "vat_out": ("PMNT", "ICDT", "BOOK", "PRZEKSIĘGOWANIE Z RACHUNKU VAT"),
    "vat_in": ("PMNT", "RCDT", "BOOK", "PRZEKSIĘGOWANIE Z RACHUNKU VAT"),
    "dd": ("PMNT", "IDDT", "PMDD", "POLECENIE ZAPŁATY"),
    "fee": ("ACMT", "MDOP", "CHRG", "OPŁATA / PROWIZJA BANKOWA"),
    "interest": ("ACMT", "MCOP", "INTR", "KAPITALIZACJA ODSETEK"),
}

assert all(len(v[3]) <= 35 for v in CODES.values()), "camt.053 Prtry/Cd is Max35Text"


@dataclass
class Tx:
    key: str
    account: str
    booking: date | None
    amount: Decimal
    code: str
    op_type: str
    cp: Party | None = None
    title: list = field(default_factory=list)
    cp_name: str | None = None
    cp_iban: str | None = None
    tx_date: date | None = None
    value: date | None = None
    status: str = "BOOK"
    mcc: str | None = None
    fx: tuple | None = None          # (currency, instructed amount, rate)
    # ground truth
    gt: str = ""                     # matched | no_document | own_transfer | to_clarify
    route: str = "any"               # auto | queue | any
    category: str | None = None
    subtype: str | None = None
    matches: list = field(default_factory=list)
    diff: tuple | None = None        # (name, amount PLN)
    mpp: str | None = None
    pair: str | None = None
    candidates: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    sensitive: list = field(default_factory=list)
    scenario: str = ""
    note: str = ""
    seq: int = 0
    ref: str = ""
    balance_after: Decimal | None = None


TXS: list[Tx] = []


def tx(key, account, day, amount, code, op_type, cp=None, title="", **kw) -> Tx:
    t = Tx(key=key, account=account, booking=d(day) if day else None, amount=D(amount), code=code,
           op_type=op_type, cp=P[cp] if isinstance(cp, str) else cp,
           title=[title] if isinstance(title, str) else list(title), seq=len(TXS), **kw)
    if t.cp is not None and t.cp_iban is None and op_type not in ("card", "blik", "fee", "interest"):
        t.cp_iban = t.cp.iban
    assert all(x.key != key for x in TXS), key
    TXS.append(t)
    return t


def M(kind: str, ref: str, amount, doc_amount=None, **extra) -> dict:
    """One matched document. kind: ksef | pdf | pm | bank; amount = PLN settled by this transaction."""
    return dict(kind=kind, ref=ref, amount=D(amount), doc_amount=None if doc_amount is None else D(doc_amount),
                **extra)


def mpp_title(vat, nip, number, text) -> str:
    return f"/VAT/{pl2(vat, '')}/IDC/{nip}/INV/{number}/TXT/{text}"


def tax_title(form: str, text: str, period: str = "26M09") -> str:
    return f"/TI/N{ODM.nip}/OKR/{period}/SFP/{form}/TXT/{text}"


def blik(key, day, person_key, amount, text, row):
    p = P[person_key]
    return tx(key, "MB", day, -D(amount), "blik", "blik", cp=p, cp_name=p.name.upper(),
              title=[text, f"Przelew na telefon BLIK +48 {phone_fmt(p.phone)}"],
              gt="matched", route="queue", matches=[M("pm", row, amount)], scenario="blik",
              note="Arkusz PM jest dokumentem wewnętrznym, ale nie twardym dowodem (D2) - kolejka.")


def card(key, day, tx_day, amount, merchant, lines, mcc, account="MB", fx=None, **kw):
    acc = ACCOUNTS[account]
    title = list(lines)
    if fx:
        title.append(f"Kwota oryg. {pl2(fx[1])} {fx[0]}, kurs {str(fx[2]).replace('.', ',')}")
    title.append(f"Karta {acc['card']}")
    return tx(key, account, day, -D(amount), "card_fx" if fx else "card", "card", cp_name=merchant, title=title,
              tx_date=d(tx_day), mcc=mcc, fx=fx, **kw)


def inv_m(key: str, amount=None, doc_amount=None) -> dict:
    i = INV[key]
    return M("ksef", key, i.gross_pln if amount is None else amount,
             (i.gross if amount is None else amount) if doc_amount is None else doc_amount)


ZUS_SEP = D("14382.47")
PIT4_SEP = D("3214.00")
CIT_SEP = D("6450.00")

# ===== mBank - rachunek bieżący (MB) =====
tx("MB-ACME-ZAL", "MB", "2026-10-01", "29520.00", "in", "transfer", cp="acme",
   title="Zaliczka 30% - Gala ACME 2026 - umowa 14/2026",
   gt="matched", route="any", matches=[inv_m("S-ACME-ZAL")], scenario="zaliczka",
   note="Wpłata zaliczki PRZED wystawieniem faktury zaliczkowej (FV 1/10/2026 z 02.10); w tytule brak numeru.")
card("MB-CARD-ANTHROPIC", "2026-10-02", "2026-10-01", "586.35", "ANTHROPIC* CLAUDE TEAM",
     ["ANTHROPIC* CLAUDE TEAM SAN FRANCISCO US"], "5734", fx=("USD", D("150.00"), D("3.9090")),
     gt="matched", route="queue", scenario="karta-saas")
blik("MB-BLIK-01", "2026-10-05", "lewandowicz", "400.00", "Nordis Gdansk hostessa 03.10", "R01")
blik("MB-BLIK-02", "2026-10-05", "drozdowska", "400.00", "Nordis Gdansk hostessa 03.10", "R02")
tx("MB-OWN-TO-ML", "MB", "2026-10-06", "-20000.00", "own_out", "own", cp=ODM, cp_iban=ML,
   title="Przelew wlasny - zasilenie rachunku Millennium", gt="own_transfer", route="auto", pair="ML-OWN-FROM-MB",
   scenario="wlasne")
tx("MB-ZALICZKA-KALINA", "MB", "2026-10-07", "-5000.00", "out", "transfer", cp="kalina",
   title="Zaliczka na poczet faktury - rezerwacja Dwor Kalina, termin 14.11.2026",
   gt="no_document", route="queue", category="Zaliczki", subtype="zaliczka na poczet faktury (czeka 180 dni)",
   scenario="zaliczka", note="Brak faktury w październiku - faktura zaliczkowa dostawcy przyjdzie w listopadzie.")
tx("MB-DRUK", "MB", "2026-10-08", "-2952.00", "out", "transfer", cp="drukarnia", title="Faktura 214/09/2026",
   gt="matched", route="auto", matches=[inv_m("P-DRUK")], scenario="podstawowe")
tx("MB-CZYNSZ", "MB", "2026-10-09", "-9840.00", "out", "transfer", cp="biuro_centrum",
   title="Czynsz pazdziernik 2026 FV 7/10/2026", gt="matched", route="auto", matches=[inv_m("P-CZYNSZ")],
   scenario="ten-sam-numer",
   note="Numer FV 7/10/2026 jest też numerem naszej faktury sprzedaży dla ACME - kierunek i NIP rozstrzygają.")
for _k, _amt in (("mazur", "7240.18"), ("sowinski", "6512.40"), ("wierzchowska", "5980.77"),
                 ("grabowiecki", "4105.33")):
    tx(f"MB-SALARY-{_k.upper()}", "MB", "2026-10-09", -D(_amt), "salary", "transfer", cp=_k,
       title="Wynagrodzenie 09/2026", gt="no_document", category="Wynagrodzenia", subtype="lista płac 09/2026",
       scenario="bez-dokumentu")
tx("MB-KOMORNIK", "MB", "2026-10-09", "-1250.00", "out", "transfer", cp="komornik",
   title="Komornik KM 1187/26 - zajecie wynagrodzenia dluznika Tomasz Grabowiecki za 09/2026",
   gt="no_document", route="queue", category="Zajęcie komornicze", subtype="potrącenie z wynagrodzenia",
   sensitive=["komornik", "zajęcie wynagrodzenia"], scenario="wrazliwe",
   note="Słowo wrażliwe + nazwisko dłużnika: tytuł omija model AI, kategorię nadaje człowiek.")
tx("MB-BILANS", "MB", "2026-10-12", "-1845.00", "out", "transfer", cp="bilans",
   title="FV/2026/10/033 uslugi ksiegowe 09/2026", gt="matched", route="auto", matches=[inv_m("P-BILANS")],
   scenario="podstawowe")
tx("MB-ZWROT-MAZUR", "MB", "2026-10-12", "-286.40", "out", "transfer", cp="mazur",
   title="Zwrot kosztow - dekoracje kwiatowe Gala ACME", gt="matched", route="queue", matches=[M("pm", "R03", "286.40")],
   scenario="blik", note="Zwrot kosztów pracownikowi - dokumentem jest wiersz arkusza PM.")
blik("MB-BLIK-03", "2026-10-12", "wojcicka", "520.00", "Gala ACME koordynacja hostess 09.10", "R04")
blik("MB-BLIK-04", "2026-10-12", "sobieraj", "450.00", "Gala ACME hostessa 09.10", "R05")
blik("MB-BLIK-05", "2026-10-12", "karczmarek", "450.00", "Gala ACME hostessa 09.10", "R06")
blik("MB-BLIK-06", "2026-10-12", "pietrasik", "450.00", "Gala ACME hostessa 09.10", "R07")
tx("MB-ORBITA-1", "MB", "2026-10-13", "3075.00", "in", "transfer", cp="orbita", title="Zaplata za FV 5/09/2026",
   gt="matched", route="auto", matches=[inv_m("S-ORBITA-SEP")], scenario="duplikat")
tx("MB-BLIK-ORPHAN", "MB", "2026-10-13", "-300.00", "blik", "blik", cp="michalak", cp_name="DAWID MICHALAK",
   title=["pomoc przy demontazu", f"Przelew na telefon BLIK +48 {phone_fmt(P['michalak'].phone)}"],
   gt="to_clarify", route="queue", scenario="blik",
   note="BLIK bez wiersza w arkuszu PM - nie wiadomo, do którego eventu i na jakiej podstawie.")
tx("MB-ORBITA-2", "MB", "2026-10-14", "3075.00", "in", "transfer", cp="orbita", title="Zaplata za FV 5/09/2026",
   gt="matched", route="queue", matches=[M("bank", "MB-ORBITA-ZWROT", "3075.00", relation="refunded_by")],
   diff=("nadpłata (możliwy duplikat)", D("3075.00")), scenario="duplikat",
   note="Identyczna druga wpłata dzień później: nadpłata z flagą duplikatu, zwrócona 21.10.")
tx("MB-SCENA-MPP", "MB", "2026-10-14", "-25000.00", "mpp_out", "transfer", cp="scena",
   title=mpp_title("5750.00", P["scena"].nip, "SP/2026/10/087", "technika Gala ACME"),
   gt="matched", route="auto", matches=[inv_m("P-SCENA-GALA", D("25000.00"))], mpp="MPP-OUT-1", scenario="mpp",
   note="Wychodzący split payment: netto z bieżącego, VAT z rachunku VAT (MV-SCENA-MPP-VAT).")
tx("MB-ZUS", "MB", "2026-10-15", -ZUS_SEP, "zus", "zus", cp="zus", title="Skladki ZUS za 09/2026",
   gt="no_document", category="ZUS", subtype="składki 09/2026", scenario="bez-dokumentu")
card("MB-CARD-GOOGLE", "2026-10-16", "2026-10-15", "437.12", "GOOGLE *WORKSPACE ODMPAR",
     ["GOOGLE *Workspace odmpar G.CO/HELPPAY# IE"], "7372", fx=("EUR", D("99.00"), D("4.4154")),
     gt="matched", route="queue", scenario="karta-saas")
tx("MB-ZAWADZKI", "MB", "2026-10-16", "-1800.00", "out", "transfer", cp="zawadzki",
   title="Rachunek 4/2026 - konferansjerka Gala ACME 09.10", gt="matched", route="any",
   matches=[M("pdf", "EM-05", "1800.00", "1800.00")], evidence=["pm:R08"], scenario="jeden-koszt",
   note="Jeden koszt, trzy dowody: przelew, rachunek PDF (EM-05), wiersz arkusza PM R08.")
blik("MB-BLIK-07", "2026-10-19", "zawilska", "400.00", "Nordis Wroclaw hostessa 17.10", "R13")
blik("MB-BLIK-08", "2026-10-19", "kuczynska", "400.00", "Nordis Wroclaw hostessa 17.10", "R14")
blik("MB-BLIK-09", "2026-10-19", "bartoszek", "400.00", "Nordis Wroclaw hostessa 17.10", "R15")
blik("MB-BLIK-10", "2026-10-19", "rutkowska", "900.00", "Helios Summit hostessa 15-16.10", "R09")
blik("MB-BLIK-11", "2026-10-19", "wesolowska", "900.00", "Helios Summit hostessa 15-16.10", "R10")
blik("MB-BLIK-12", "2026-10-19", "jablonowska", "900.00", "Helios Summit hostessa 15-16.10", "R11")
tx("MB-SMAKI", "MB", "2026-10-20", "-31104.00", "out", "transfer", cp="smaki",
   title="Faktura 2026/10/0193 catering Gala ACME", gt="matched", route="auto", matches=[inv_m("P-SMAKI")],
   scenario="podstawowe")
tx("MB-WRONA", "MB", "2026-10-20", "-1808.00", "out", "transfer", cp="wrona",
   title="Umowa o dzielo 07/2026 - projekt scenografii Helios Summit", gt="matched", route="queue",
   matches=[M("pm", "R12", "1808.00")], scenario="blik")
tx("MB-US-PIT4", "MB", "2026-10-20", -PIT4_SEP, "tax", "tax", cp="us",
   title=tax_title("PIT4R", "zaliczki PIT 09/2026"),
   gt="no_document", category="Podatki (US)", subtype="PIT-4R 09/2026", scenario="bez-dokumentu")
tx("MB-US-CIT", "MB", "2026-10-20", -CIT_SEP, "tax", "tax", cp="us", title=tax_title("CIT8", "zaliczka CIT 09/2026"),
   gt="no_document", category="Podatki (US)", subtype="CIT-8 zaliczka 09/2026", scenario="bez-dokumentu")
tx("MB-ORBITA-ZWROT", "MB", "2026-10-21", "-3075.00", "out", "transfer", cp="orbita",
   title="Zwrot nadplaty - podwojna wplata z 14.10.2026 za FV 5/09/2026", gt="matched", route="queue",
   matches=[M("bank", "MB-ORBITA-2", "3075.00", relation="refund_of")], diff=("zwrot nadpłaty", D("-3075.00")),
   scenario="duplikat", note="Zwrot wiąże się z drugą (zdublowaną) wpłatą, nie z fakturą.")
card("MB-CARD-NOTION", "2026-10-22", "2026-10-21", "281.95", "NOTION LABS INC",
     ["NOTION LABS, INC. SAN FRANCISCO US"], "5734", fx=("USD", D("72.00"), D("3.9160")),
     gt="matched", route="queue", scenario="karta-saas")
tx("MB-ACME-ROZ", "MB", "2026-10-23", "56000.00", "mpp_in", "transfer", cp="acme",
   title=mpp_title(INV["S-ACME-ROZ"].vat, ODM.nip, "FV 3/10/2026", "rozliczenie Gala ACME"),
   gt="matched", route="auto", matches=[inv_m("S-ACME-ROZ", D("56000.00"))], mpp="MPP-IN-1", scenario="mpp",
   note="Faktura ROZ nie ma adnotacji MPP, a klient i tak płaci splitem.")
tx("MB-REVOLUT-KWADRAT", "MB", "2026-10-23", "14760.00", "in", "transfer", cp=P["revolut"], cp_iban=P["kwadrat"].iban,
   title="KWADRAT DEVELOPMENT SP. Z O.O. - wynajem naglosnienia", gt="matched", route="queue",
   matches=[inv_m("S-KWADRAT")], scenario="revolut",
   note="Nadawcą jest Revolut (LT IBAN), klient tylko w tytule; jedyna otwarta faktura Kwadrat na 14 760,00.")
for _idx in range(1, 13):
    _i = INV[f"P-ORANGE-{_idx:02d}"]
    tx(f"MB-ORANGE-{_idx:02d}", "MB", "2026-10-23", -_i.gross, "out", "transfer", cp="orange", cp_iban=_i.pay_to,
       title=_i.number, gt="matched", route="auto", matches=[inv_m(_i.key)], scenario="orange")
tx("MB-MOBILIS-DD", "MB", "2026-10-26", "-3505.50", "dd", "direct_debit", cp="mobilis",
   title="Polecenie zaplaty L/10/2026/0815 rata 10/36", gt="matched", route="auto", matches=[inv_m("P-MOBILIS")],
   scenario="podstawowe")
card("MB-CARD-PETROVA", "2026-10-26", "2026-10-24", "377.20", "STACJA PETROVA 214 LODZ",
     ["STACJA PETROVA 214 LODZ PL"], "5542", gt="matched", route="queue", scenario="karta-saas")
blik("MB-BLIK-13", "2026-10-26", "grabarczyk", "400.00", "Nordis Lodz hostessa 24.10", "R16")
blik("MB-BLIK-14", "2026-10-26", "mikulska", "400.00", "Nordis Lodz hostessa 24.10", "R17")
blik("MB-BLIK-15", "2026-10-26", "szafranska", "400.00", "Nordis Lodz hostessa 24.10", "R18")
blik("MB-BLIK-16", "2026-10-26", "olejniczak", "400.00", "Nordis Lodz hostessa 24.10", "R19")
tx("MB-ACME-FV7", "MB", "2026-10-27", "10000.00", "mpp_in", "transfer", cp="acme",
   title=mpp_title(INV["S-ACME-FV7"].vat, ODM.nip, "FV 7/10/2026", "atrakcje Gala ACME"),
   gt="matched", route="auto", matches=[inv_m("S-ACME-FV7", D("10000.00"))], mpp="MPP-IN-2", scenario="mpp")
tx("MB-KADR-REMIS", "MB", "2026-10-27", "-4500.00", "out", "transfer", cp="kadr", title="zaplata",
   gt="to_clarify", route="queue", candidates=["P-KADR-GDA", "P-KADR-WRO"], scenario="remis",
   note="Dwie otwarte faktury Kadr Studio po 4 500,00 i przelew 'zaplata' - remis, zawsze człowiek.")
tx("MB-HELIOS-EUR", "MB", "2026-10-28", "52737.50", "foreign_in", "foreign_transfer", cp="helios",
   title="INV FV 5/10/2026 HELIOS PARTNER SUMMIT 2026", fx=("EUR", D("12500.00"), D("4.2190")),
   gt="matched", route="auto", matches=[inv_m("S-HELIOS", D("52737.50"), D("12500.00"))],
   diff=("kurs", D("52737.50") - INV["S-HELIOS"].gross_pln), scenario="waluta",
   note="12 500 EUR przewalutowane przez bank po 4,2190; faktura w PLN po NBP 4,2468.")
tx("MB-FEE-SEPA", "MB", "2026-10-28", "-25.00", "fee", "fee", title="Prowizja - przelew zagraniczny przychodzacy SEPA",
   gt="no_document", category="Prowizje bankowe", subtype="przelew zagraniczny", scenario="bez-dokumentu")
tx("MB-SCENA-3FV", "MB", "2026-10-28", "-16236.00", "out", "transfer", cp="scena",
   title="Zaplata za FV SP/2026/10/081, SP/2026/10/093, SP/2026/10/101", gt="matched", route="auto",
   matches=[inv_m("P-SCENA-GDA"), inv_m("P-SCENA-WRO"), inv_m("P-SCENA-LDZ")], scenario="trzy-faktury")
tx("MB-FROM-VAT", "MB", "2026-10-28", "3000.00", "vat_in", "own", cp=ODM, cp_iban=MV,
   title="Przeksiegowanie z rachunku VAT - postanowienie NUS 1471-SPV.4012.37.2026 z 21.10.2026",
   gt="own_transfer", route="auto", pair="MV-TO-MB", scenario="mpp")
tx("MB-OWN-FROM-ML", "MB", "2026-10-30", "15000.00", "own_in", "own", cp=ODM, cp_iban=ML,
   title="Przelew wlasny", value=d("2026-10-30"), gt="own_transfer", route="auto", pair="ML-OWN-TO-MB",
   scenario="wlasne", note="Obciążenie w Millennium 29.10, uznanie w mBanku 30.10 (sesja Elixir).")
tx("MB-FEE-ACCOUNT", "MB", "2026-10-30", "-89.00", "fee", "fee", title="Oplata za prowadzenie rachunku 10/2026",
   gt="no_document", category="Prowizje bankowe", subtype="opłata miesięczna", scenario="bez-dokumentu")

# ===== mBank - rachunek VAT (MV) =====
tx("MV-SCENA-MPP-VAT", "MV", "2026-10-14", "-5750.00", "mpp_vat_out", "vat", cp="scena",
   title=mpp_title("5750.00", P["scena"].nip, "SP/2026/10/087", "technika Gala ACME"),
   gt="matched", route="auto", matches=[inv_m("P-SCENA-GALA", D("5750.00"))], mpp="MPP-OUT-1", scenario="mpp")
tx("MV-ACME-ROZ-VAT", "MV", "2026-10-23", INV["S-ACME-ROZ"].vat, "mpp_vat_in", "vat", cp="acme",
   title=mpp_title(INV["S-ACME-ROZ"].vat, ODM.nip, "FV 3/10/2026", "rozliczenie Gala ACME"),
   gt="matched", route="auto", matches=[inv_m("S-ACME-ROZ", INV["S-ACME-ROZ"].vat)], mpp="MPP-IN-1",
   scenario="mpp")
tx("MV-US-VAT7", "MV", "2026-10-26", "0.00", "tax", "tax", cp="us", title=tax_title("VAT7", "VAT za 09/2026"),
   gt="no_document", category="Podatki (US)", subtype="VAT-7 09/2026", scenario="bez-dokumentu",
   note="Kwota = P_51 z JPK_V7M za wrzesień (fixtures/odm/history).")
tx("MV-ACME-FV7-VAT", "MV", "2026-10-27", INV["S-ACME-FV7"].vat, "mpp_vat_in", "vat", cp="acme",
   title=mpp_title(INV["S-ACME-FV7"].vat, ODM.nip, "FV 7/10/2026", "atrakcje Gala ACME"),
   gt="matched", route="auto", matches=[inv_m("S-ACME-FV7", INV["S-ACME-FV7"].vat)], mpp="MPP-IN-2",
   scenario="mpp")
tx("MV-TO-MB", "MV", "2026-10-28", "-3000.00", "vat_out", "own", cp=ODM, cp_iban=MB,
   title="Przeksiegowanie z rachunku VAT - postanowienie NUS 1471-SPV.4012.37.2026 z 21.10.2026",
   gt="own_transfer", route="auto", pair="MB-FROM-VAT", scenario="mpp")

# ===== Millennium - rachunek bieżący (ML) =====
tx("ML-OWN-FROM-MB", "ML", "2026-10-06", "20000.00", "own_in", "own", cp=ODM, cp_iban=MB,
   title="Przelew wlasny - zasilenie rachunku Millennium", gt="own_transfer", route="auto", pair="MB-OWN-TO-ML",
   scenario="wlasne")
tx("ML-FALA-CZESC", "ML", "2026-10-08", "5000.00", "in", "transfer", cp="fala", title="FV 4/09/2026 czesc 1",
   gt="matched", route="queue", matches=[inv_m("S-FALA-LED", D("5000.00"))], diff=("niedopłata", D("-4225.00")),
   scenario="czesciowa", note="Numer faktury w tytule, ale kwota 5 000 z 9 225 - częściowa zapłata.")
tx("ML-KRESKA", "ML", "2026-10-09", "-7380.00", "out", "transfer", cp="kreska",
   title="FV 12/09/2026 projekt graficzny Roadshow Nordis", gt="matched", route="auto", matches=[inv_m("P-KRESKA")],
   scenario="podstawowe")
tx("ML-LOFT", "ML", "2026-10-19", "-18450.00", "express", "instant", cp="loft",
   title="LP/118/2026 Helios Partner Summit", gt="matched", route="auto", matches=[inv_m("P-LOFT")],
   scenario="podstawowe")
tx("ML-FEE-EXPRESS", "ML", "2026-10-19", "-5.00", "fee", "fee", title="Prowizja za przelew Express Elixir",
   gt="no_document", category="Prowizje bankowe", subtype="przelew natychmiastowy", scenario="bez-dokumentu")
tx("ML-NORDIS-KOR", "ML", "2026-10-20", "8000.00", "in", "transfer", cp="nordis",
   title="Zaplata FV 2/10/2026 po korekcie", gt="matched", route="auto",
   matches=[inv_m("S-NORDIS-GDA"), inv_m("S-NORDIS-KOR")], scenario="korekta",
   note="Saldo z łańcucha: 10 000,00 - 2 000,00 = 8 000,00; jeden przelew spina fakturę i korektę.")
tx("ML-ODRA", "ML", "2026-10-21", "-6765.00", "out", "transfer", cp="odra", title="Faktura 2026/10/27",
   gt="matched", route="auto", matches=[inv_m("P-ODRA")], scenario="podstawowe")
tx("ML-WAWEL", "ML", "2026-10-22", "-14078.40", "out", "transfer", cp="wawel", title="FV/2026/10/0412",
   gt="matched", route="auto", matches=[inv_m("P-WAWEL")], scenario="dedup")
tx("ML-OWN-TO-MB", "ML", "2026-10-29", "-15000.00", "own_out", "own", cp=ODM, cp_iban=MB, title="Przelew wlasny",
   gt="own_transfer", route="auto", pair="MB-OWN-FROM-ML", scenario="wlasne")
tx("ML-NORDIS-WRO", "ML", "2026-10-30", "7380.00", "in", "transfer", cp="nordis", title="FV 8/10/2026",
   gt="matched", route="auto", matches=[inv_m("S-NORDIS-WRO")], scenario="podstawowe")
tx("ML-FEE-ACCOUNT", "ML", "2026-10-30", "-39.00", "fee", "fee", title="Oplata miesieczna za prowadzenie rachunku",
   gt="no_document", category="Prowizje bankowe", subtype="opłata miesięczna", scenario="bez-dokumentu")
tx("ML-INTEREST", "ML", "2026-10-30", "3.12", "interest", "interest", title="Kapitalizacja odsetek 10/2026",
   gt="no_document", category="Odsetki", subtype="odsetki od środków", scenario="bez-dokumentu")

# ===== Pending card blocks (status PDNG; never part of the balance) =====
card("MB-PDNG-HOTEL", None, "2026-10-30", "1200.00", "HOTEL VISTULA WARSZAWA",
     ["HOTEL VISTULA WARSZAWA PL - preautoryzacja"], "7011", status="PDNG")
card("MB-PDNG-ALLEGRO", None, "2026-10-31", "389.00", "ALLEGRO.PL", ["ALLEGRO.PL POZNAN PL"], "5311", status="PDNG")
card("MB-PDNG-UBER", None, "2026-10-31", "46.80", "UBER *TRIP", ["UBER *TRIP HELP.UBER.COM NL"], "4121",
     status="PDNG")
card("ML-PDNG-BOLT", None, "2026-10-31", "32.50", "BOLT.EU", ["BOLT.EU/R/2610311845 TALLINN EE"], "4121",
     account="ML", status="PDNG")


TX = {t.key: t for t in TXS}

# ---------------------------------------------------------------------------
# E-mail PDFs (documents that never reach KSeF, plus one KSeF duplicate)
# ---------------------------------------------------------------------------


@dataclass
class EmailDoc:
    id: str
    kind: str                 # invoice | receipt | bill
    seller: Party
    number: str
    issue: date
    currency: str
    lines: list               # dicts: name, qty, unit, unit_price, vat_rate
    vat_mode: str             # reverse | none | included (receipt: PTU inside gross) | standard
    filename: str
    mail: dict
    payment: dict
    tx: str | None = None
    ksef_dup: str | None = None
    period: tuple | None = None
    sale: date | None = None
    expected_event: str = UNASSIGNED
    final: list = field(default_factory=lambda: [(UNASSIGNED, Fraction(1))])
    signal: str = "brak sygnału"
    note: str = ""

    def line_values(self) -> list[dict]:
        out = []
        for ln in self.lines:
            amount = r2(D(ln["qty"]) * D(ln["unit_price"]))
            rate = ln["vat_rate"]
            if self.vat_mode == "included":
                gross = amount
                vat = r2(gross * RATE_VALUE[rate] / (1 + RATE_VALUE[rate]))
                net = gross - vat
            else:
                net = amount
                vat = r2(net * RATE_VALUE.get(rate, D(0))) if self.vat_mode == "standard" else D(0)
                gross = net + vat
            out.append(dict(ln, net=net, vat=vat, gross=gross))
        return out

    @property
    def totals(self) -> dict:
        if self.ksef_dup:
            i = INV[self.ksef_dup]
            return dict(net=i.net, vat=i.vat, gross=i.gross)
        vals = self.line_values()
        return {k: sum((v[k] for v in vals), D(0)) for k in ("net", "vat", "gross")}

    @property
    def fx(self) -> tuple[Decimal, date] | None:
        return None if self.currency == "PLN" else nbp_rate(self.currency, self.issue)

    @property
    def cost_pln(self) -> Decimal:
        """Cost for the memoriał margin: net (VAT deductible) or gross when VAT is not deductible."""
        t = self.totals
        base = t["gross"] if self.kind in ("receipt", "bill") else t["net"]
        return base if self.fx is None else r2(base * self.fx[0])

    @property
    def gross_pln(self) -> Decimal:
        g = self.totals["gross"]
        return g if self.fx is None else r2(g * self.fx[0])


EMAILS: dict[str, EmailDoc] = {}


def email_doc(**kw) -> EmailDoc:
    e = EmailDoc(**kw)
    EMAILS[e.id] = e
    return e


ODM_MAIL = "finanse@odmpartners.test"
email_doc(id="EM-01", kind="invoice", seller=P["google"], number="5384029117", issue=d("2026-10-14"), currency="EUR",
          period=(d("2026-09-14"), d("2026-10-13")), vat_mode="reverse",
          lines=[dict(name="Google Workspace Business Standard (Flexible Plan)", qty="9", unit="user",
                      unit_price="11.00", vat_rate="np II")],
          filename="EM-01_google_workspace_5384029117.pdf", tx="MB-CARD-GOOGLE",
          payment=dict(method="card", card_last4="4417", status="paid"),
          mail=dict(from_="Google Payments <payments-noreply@google.com>", to=ODM_MAIL,
                    date=utc_ts(d("2026-10-14"), 7, 41, 12),
                    subject="Google Workspace: faktura za odmpartners.test jest dostępna"),
          note="Faktura w EUR, płatność kartą w PLN 2 dni później - różnica kursowa wobec NBP.")
email_doc(id="EM-02", kind="invoice", seller=P["anthropic"], number="INV-2610-0437", issue=d("2026-10-01"),
          currency="USD", period=(d("2026-10-01"), d("2026-10-31")), vat_mode="reverse",
          lines=[dict(name="Claude Team plan - standard seat (Oct 1 - Oct 31, 2026)", qty="5", unit="seat",
                      unit_price="30.00", vat_rate="np II")],
          filename="EM-02_anthropic_INV-2610-0437.pdf", tx="MB-CARD-ANTHROPIC",
          payment=dict(method="card", card_last4="4417", status="paid"),
          mail=dict(from_="Anthropic, PBC <invoice+statements@mail.anthropic.com>", to=ODM_MAIL,
                    date=utc_ts(d("2026-10-01"), 9, 2, 55), subject="Your receipt from Anthropic, PBC #2481-7730"))
email_doc(id="EM-03", kind="invoice", seller=P["notion"], number="8F2C41A0-0007", issue=d("2026-10-21"),
          currency="USD", period=(d("2026-10-21"), d("2026-11-20")), vat_mode="reverse",
          lines=[dict(name="Notion Plus plan (monthly)", qty="6", unit="member", unit_price="12.00",
                      vat_rate="np II")],
          filename="EM-03_notion_8F2C41A0-0007.pdf", tx="MB-CARD-NOTION",
          payment=dict(method="card", card_last4="4417", status="paid"),
          mail=dict(from_="Notion <team@makenotion.com>", to=ODM_MAIL, date=utc_ts(d("2026-10-21"), 18, 14, 3),
                    subject="Your Notion receipt [#8F2C41A0-0007]"))
email_doc(id="EM-04", kind="receipt", seller=P["petrova"], number="028417", issue=d("2026-10-24"), currency="PLN",
          vat_mode="included",
          lines=[dict(name="ON EFECTA DIESEL", qty="58.12", unit="l", unit_price="6.49", vat_rate="23")],
          filename="EM-04_paragon_petrova_2026-10-24.pdf", tx="MB-CARD-PETROVA",
          payment=dict(method="card", card_last4="4417", status="paid"),
          mail=dict(from_="Anna Mazur <anna.mazur@odmpartners.test>", to=ODM_MAIL,
                    date=utc_ts(d("2026-10-25"), 10, 5, 40), subject="paragon paliwo Łódź (roadshow)"),
          expected_event=QUEUE, final=alloc(("EV-04", 1)),
          signal="D/C: tylko miejsce i data (Łódź 24.10) - podpowiedź, nie przypisanie",
          note="Paragon bez NIP nabywcy: VAT nieodliczalny, kosztem jest brutto.")
email_doc(id="EM-05", kind="bill", seller=P["zawadzki"], number="4/2026", issue=d("2026-10-10"), currency="PLN",
          sale=d("2026-10-09"), vat_mode="none",
          lines=[dict(name="Prowadzenie konferansjerki podczas Gali ACME 2026 (09.10.2026, Kraków)", qty="1",
                      unit="usł.", unit_price="1800.00", vat_rate="zw")],
          filename="EM-05_rachunek_4_2026_zawadzki.pdf", tx="MB-ZAWADZKI",
          payment=dict(method="transfer", bank_account=P["zawadzki"].iban, due_date="2026-10-17", status="unpaid"),
          mail=dict(from_="Marek Zawadzki <marek.zawadzki@poczta.test>", to=ODM_MAIL,
                    date=utc_ts(d("2026-10-10"), 21, 33, 9), subject="Rachunek za prowadzenie Gali"),
          expected_event="EV-01", final=alloc(("EV-01", 1)), signal="A: nazwa eventu w pozycji",
          note="Rachunek osoby fizycznej (działalność nierejestrowana, bez VAT). Ten sam koszt co wiersz PM R08.")
_w = INV["P-WAWEL"]
email_doc(id="EM-06", kind="invoice", seller=P["wawel"], number=_w.number, issue=_w.issue, currency="PLN",
          period=_w.period, vat_mode="standard",
          lines=[dict(name=ln.name, qty=str(ln.qty), unit=ln.unit, unit_price=s2(ln.price), vat_rate=ln.rate)
                 for ln in _w.lines],
          filename="EM-06_hotel_wawel_grand_FV-2026-10-0412.pdf", tx="ML-WAWEL", ksef_dup="P-WAWEL",
          payment=dict(method="transfer", bank_account=_w.pay_to, due_date=_w.due.isoformat(), status="unpaid"),
          mail=dict(from_="Hotel Wawel Grand <recepcja@wawelgrand.test>", to=ODM_MAIL,
                    date=utc_ts(d("2026-10-10"), 11, 47, 21), subject="Faktura FV/2026/10/0412 - pobyt 08-10.10.2026"),
          expected_event=QUEUE, final=alloc(("EV-01", 1)), signal="duplikat faktury KSeF - jeden dokument",
          note="TA SAMA faktura co w KSeF (bez numeru KSeF na wydruku): deduplikacja po NIP + numer + data + kwota.")

# Card transactions settle the e-mailed invoices: named FX difference against NBP.
for _e in EMAILS.values():
    if not _e.tx or _e.ksef_dup:
        continue
    _t = TX[_e.tx]
    _paid = -_t.amount
    _t.matches = [M("pdf", _e.id, _paid, _e.totals["gross"])]
    if _e.currency != "PLN":
        _t.diff = ("kurs", _paid - _e.gross_pln)

# ---------------------------------------------------------------------------
# History: JPK_V7M for September 2026 and the bookkeeping office export (10.2025)
# ---------------------------------------------------------------------------


@dataclass
class JpkRow:
    section: str      # sales | purchase
    number: str
    issue: date
    sale_or_receipt: date
    party: Party
    net: Decimal
    vat: Decimal
    ksef_key: str | None = None


def jpk_rows() -> list[JpkRow]:
    rows: list[JpkRow] = []
    for number, issue, sale, key, net in (("FV 1/09/2026", "2026-09-03", "2026-09-02", "kwadrat", "4000.00"),
                                         ("FV 2/09/2026", "2026-09-07", "2026-09-05", "nordis", "32000.00"),
                                         ("FV 3/09/2026", "2026-09-10", "2026-09-10", "acme", "18500.00")):
        rows.append(JpkRow("sales", number, d(issue), d(sale), P[key], D(net), r2(D(net) * D("0.23"))))
    for i in INV.values():
        if i.direction == S and i.jpk_number:
            rows.append(JpkRow("sales", i.jpk_number, i.issue, i.sale, i.buyer, i.net, i.vat, i.key))
    purchases = [JpkRow("purchase", "FV 12/09/2026", d("2026-09-01"), d("2026-09-01"), P["biuro_centrum"],
                        D("8000.00"), D("1840.00"))]
    rng = R("orange:2026-09")
    for acct in ORANGE_ACCOUNTS:
        net = D(rng.randint(11000, 36000)) / 100
        purchases.append(JpkRow("purchase", f"F/{acct}/09/26", d("2026-09-05"), d("2026-09-07"), P["orange"], net,
                                r2(net * D("0.23"))))
    for number, issue, recv, key, net in (("FV/2026/09/031", "2026-09-04", "2026-09-04", "bilans", "1500.00"),
                                         ("SP/2026/09/064", "2026-09-08", "2026-09-09", "scena", "3000.00"),
                                         ("L/09/2026/0815", "2026-09-10", "2026-09-11", "mobilis", "2850.00")):
        purchases.append(JpkRow("purchase", number, d(issue), d(recv), P[key], D(net), r2(D(net) * D("0.23"))))
    for i in INV.values():
        if i.direction == PU and i.jpk_number:
            purchases.append(JpkRow("purchase", i.jpk_number, i.issue, i.issue + timedelta(days=1), i.seller, i.net,
                                    i.vat, i.key))
    rows.extend(sorted(purchases, key=lambda r: (r.issue, r.number)))
    return rows


JPK = jpk_rows()
JPK_NALEZNY = sum((r.vat for r in JPK if r.section == "sales"), D(0))
JPK_NALICZONY = sum((r.vat for r in JPK if r.section == "purchase"), D(0))
JPK_P38 = JPK_NALEZNY.quantize(D(1), rounding=ROUND_HALF_UP)
JPK_P48 = JPK_NALICZONY.quantize(D(1), rounding=ROUND_HALF_UP)
JPK_P51 = JPK_P38 - JPK_P48
assert JPK_P51 > 0
TX["MV-US-VAT7"].amount = -D(JPK_P51).quantize(TWO)


# ---------------------------------------------------------------------------
# Derived values and consistency checks
# ---------------------------------------------------------------------------

STATUSES = {"matched", "no_document", "own_transfer", "to_clarify"}
ROUTES = {"auto", "queue", "any"}


def assign_refs() -> None:
    seen = set()
    for t in TXS:
        rng = R(f"ref:{t.key}")
        day = t.booking or t.tx_date
        if t.account in ("MB", "MV"):
            t.ref = f"{day:%Y%m%d}{rng.randrange(10 ** 10):010d}"
        else:
            t.ref = f"ML{day:%y%m%d}{rng.randrange(10 ** 7):07d}"
        if t.status == "PDNG":
            t.ref = "P" + t.ref
        assert t.ref not in seen, t.ref
        seen.add(t.ref)


def booked(code: str | None = None) -> list[Tx]:
    rows = [t for t in TXS if t.status == "BOOK" and (code is None or t.account == code)]
    return sorted(rows, key=lambda t: (t.booking, t.seq))


def pending(code: str | None = None) -> list[Tx]:
    return [t for t in TXS if t.status == "PDNG" and (code is None or t.account == code)]


BALANCES: dict[str, dict] = {}


def compute_balances() -> None:
    for code, acc in ACCOUNTS.items():
        bal = acc["opening"]
        for t in booked(code):
            assert t.booking.weekday() < 5, f"{t.key}: booking on a weekend"
            assert PERIOD_FROM <= t.booking <= PERIOD_TO, t.key
            bal += t.amount
            assert bal >= 0, f"{code} below zero after {t.key}: {bal}"
            t.balance_after = bal
        rows = booked(code)
        credits = sum((t.amount for t in rows if t.amount > 0), D(0))
        debits = sum((t.amount for t in rows if t.amount < 0), D(0))
        pend = sum((t.amount for t in pending(code)), D(0))
        assert acc["opening"] + credits + debits == bal, code
        BALANCES[code] = dict(opening=acc["opening"], credits=credits, debits=debits, closing=bal,
                              booked_count=len(rows), pending_count=len(pending(code)), pending_sum=pend,
                              available=bal + pend, last_ref=rows[-1].ref, last_day=rows[-1].booking)


VAT_RE = re.compile(r"/VAT/(\d+,\d{2})/IDC/(\d{10})/INV/(.+?)/TXT/")


def parse_vat_block(title: str) -> tuple[Decimal, str, str] | None:
    m = VAT_RE.search(title)
    return (D(m.group(1).replace(",", ".")), m.group(2), m.group(3)) if m else None


MPP_CHECK: dict = {}


def check_mpp() -> None:
    groups: dict[str, list[Tx]] = {}
    for t in booked():
        if t.mpp:
            groups.setdefault(t.mpp, []).append(t)
    incoming_vat = D(0)
    for gid, legs in groups.items():
        assert len(legs) == 2, gid
        main = next(x for x in legs if x.account == "MB")
        vat_leg = next(x for x in legs if x.account == "MV")
        block = parse_vat_block(main.title[0])
        assert block and block == parse_vat_block(vat_leg.title[0]), gid
        assert abs(vat_leg.amount) == block[0], gid
        assert (main.amount > 0) == (vat_leg.amount > 0), gid
        inv_key = main.matches[0]["ref"]
        assert abs(main.amount) + abs(vat_leg.amount) == INV[inv_key].gross, gid
        assert INV[inv_key].vat == block[0] and normalize_number(INV[inv_key].number) == normalize_number(block[2])
        if main.amount > 0:
            assert block[1] == ODM.nip
            incoming_vat += block[0]
        else:
            assert block[1] == INV[inv_key].seller.nip
    vat_credits = sum((t.amount for t in booked("MV") if t.amount > 0), D(0))
    assert incoming_vat == vat_credits, (incoming_vat, vat_credits)
    MPP_CHECK.update(groups={g: [x.ref for x in legs] for g, legs in groups.items()},
                     vat_blocks_incoming=incoming_vat, vat_account_credits=vat_credits)


SETTLED: dict[str, Decimal] = {}     # invoice key -> amount settled, invoice currency
SETTLED_BY: dict[str, list] = {}


def check_matches() -> None:
    for t in TXS:
        if t.status != "BOOK":
            assert not t.gt and not t.matches, t.key
            continue
        assert t.gt in STATUSES, t.key
        assert t.route in ROUTES, t.key
        if t.gt == "matched":
            assert t.matches, t.key
            total = sum((m["amount"] for m in t.matches), D(0))
            assert total == abs(t.amount), (t.key, total, t.amount)
        else:
            assert not t.matches, t.key
        if t.gt == "no_document":
            assert t.category, t.key
        if t.gt == "own_transfer":
            other = TX[t.pair]
            assert other.pair == t.key and other.amount == -t.amount and other.account != t.account, t.key
        if t.gt == "to_clarify" and t.candidates:
            assert all(INV[c].gross == abs(t.amount) for c in t.candidates), t.key
        foreign = [m for m in t.matches if (m["kind"] == "ksef" and INV[m["ref"]].currency != "PLN") or
                   (m["kind"] == "pdf" and EMAILS[m["ref"]].currency != "PLN")]
        if foreign:  # every FX settlement carries a named difference against the NBP-derived PLN value
            doc_pln = sum((INV[m["ref"]].gross_pln if m["kind"] == "ksef" else EMAILS[m["ref"]].gross_pln
                           for m in foreign), D(0))
            assert t.diff and t.diff[0] == "kurs" and t.diff[1] == abs(t.amount) - doc_pln, t.key
        for m in t.matches:
            if m["kind"] == "ksef":
                i = INV[m["ref"]]
                assert i.direction == ("sales" if t.amount > 0 else "purchase"), t.key
                SETTLED[i.key] = SETTLED.get(i.key, D(0)) + m["doc_amount"]
                SETTLED_BY.setdefault(i.key, []).append(t.key)
            elif m["kind"] == "pdf":
                assert EMAILS[m["ref"]].tx == t.key
            elif m["kind"] == "pm":
                assert PM[m["ref"]].amount == m["amount"]
            elif m["kind"] == "bank":
                assert TX[m["ref"]].amount == -t.amount
            else:
                raise AssertionError(m)
    for key, amount in SETTLED.items():
        i = INV[key]
        assert abs(amount) <= abs(i.gross), (key, amount, i.gross)
    # PM sheet <-> bank: every BLIK has a row except the orphan; exactly one row has no bank counterpart.
    matched_rows = [m["ref"] for t in booked() for m in t.matches if m["kind"] == "pm"]
    assert len(matched_rows) == len(set(matched_rows))
    evidence_rows = [e.split(":")[1] for t in booked() for e in t.evidence if e.startswith("pm:")]
    without_bank = [r.id for r in PM_ROWS if r.id not in matched_rows and r.id not in evidence_rows]
    assert without_bank == ["R20"], without_bank
    blik_without_row = [t.key for t in booked() if t.op_type == "blik" and not t.matches]
    assert blik_without_row == ["MB-BLIK-ORPHAN"], blik_without_row


def open_items(direction: str) -> list[dict]:
    rows = []
    for i in INV.values():
        if i.direction != direction:
            continue
        open_amount = i.gross - SETTLED.get(i.key, D(0))
        if open_amount == 0:
            continue
        if i.due:
            days = (ASOF - i.due).days
            base, flag = days, False
        else:
            days = (ASOF - i.issue).days
            base, flag = days, True
        if i.due and days <= 0:
            bucket = "nie_wymagalne"
        elif base <= 30:
            bucket = "1-30"
        elif base <= 60:
            bucket = "31-60"
        elif base <= 90:
            bucket = "61-90"
        else:
            bucket = ">90"
        pending_payments = [t.key for t in booked() if i.key in t.candidates]
        rows.append(dict(key=i.key, open=open_amount, days=base, bucket=bucket, due_missing=flag,
                         pending=pending_payments))
    return rows


def split_amount(amount: Decimal, shares: list) -> list[tuple[str, Decimal]]:
    """Split an amount by exact fractions; grosze distributed by largest remainder."""
    assert sum((s for _, s in shares), Fraction(0)) == 1, shares
    total = gr(amount)
    sign = -1 if total < 0 else 1
    total = abs(total)
    raw = [(ev, Fraction(total) * s) for ev, s in shares]
    base = [int(x) for _, x in raw]
    rest = total - sum(base)
    order = sorted(range(len(raw)), key=lambda k: (-(raw[k][1] - base[k]), k))
    for k in order[:rest]:
        base[k] += 1
    return [(ev, sign * D(b) / 100) for (ev, _), b in zip(raw, base)]


LEDGER: dict[str, dict] = {}


def compute_ledger() -> None:
    """Memoriał margin per event from final allocations (one cost, many evidences)."""
    buckets = list(EVENTS) + [UNASSIGNED]
    for b in buckets:
        LEDGER[b] = dict(revenue=[], cost=[])
    for i in INV.values():
        value = i.pln(i.economic_net)
        if value == 0:
            continue
        side = "revenue" if i.direction == S else "cost"
        for ev, amount in split_amount(value, i.final):
            LEDGER[ev][side].append(dict(source="ksef", ref=i.key, number=i.number, ksef=i.ksef,
                                         party=i.counterparty.name, amount=amount))
    for e in EMAILS.values():
        if e.ksef_dup:
            continue
        for ev, amount in split_amount(e.cost_pln, e.final):
            LEDGER[ev]["cost"].append(dict(source="email_pdf", ref=e.id, number=e.number, party=e.seller.name,
                                           amount=amount))
    for r in PM_ROWS:
        if r.same_cost_as:
            continue
        LEDGER[r.event]["cost"].append(dict(source="pm_sheet", ref=r.id, number=r.id, party=P[r.person].name,
                                            amount=r.amount))
    for b in buckets:
        rev = sum((x["amount"] for x in LEDGER[b]["revenue"]), D(0))
        cost = sum((x["amount"] for x in LEDGER[b]["cost"]), D(0))
        LEDGER[b].update(revenue_total=rev, cost_total=cost, margin=rev - cost)


EXPECTED_MARGINS = {"EV-01": D("16963.60"), "EV-02": D("6045.52"), "EV-03": D("4641.46"), "EV-04": D("6422.80"),
                    "EV-05": D("13177.00")}

CASH: dict[str, Decimal] = {}


def compute_cash() -> None:
    """Cash contribution: every booked transaction lands in an event, UNASSIGNED or 'unmatched'."""
    buckets = {b: D(0) for b in list(EVENTS) + [UNASSIGNED, "UNMATCHED"]}
    own = D(0)
    for t in booked():
        sign = 1 if t.amount > 0 else -1
        if t.gt == "own_transfer":
            own += t.amount
            continue
        if t.gt in ("no_document",):
            buckets[UNASSIGNED] += t.amount
            continue
        if t.gt == "to_clarify":
            buckets["UNMATCHED"] += t.amount
            continue
        for m in t.matches:
            if m["kind"] == "ksef":
                shares = INV[m["ref"]].final
            elif m["kind"] == "pdf":
                shares = EMAILS[m["ref"]].final
            elif m["kind"] == "pm":
                shares = [(PM[m["ref"]].event, Fraction(1))]
            else:
                shares = [(UNASSIGNED, Fraction(1))]
            for ev, amount in split_amount(sign * m["amount"], shares):
                buckets[ev] += amount
    assert own == 0
    delta = sum((BALANCES[c]["closing"] - BALANCES[c]["opening"] for c in ACCOUNTS), D(0))
    assert sum(buckets.values(), D(0)) == delta, (sum(buckets.values()), delta)
    CASH.update(buckets)
    CASH["_delta"] = delta


def check_documents() -> None:
    every_iban = [a["iban"] for a in ACCOUNTS.values()] + [p.iban for p in P.values() if p.iban] + \
        [i.pay_to for i in INV.values() if i.pay_to] + [t.cp_iban for t in TXS if t.cp_iban]
    assert all(iban_valid(x) for x in every_iban), [x for x in every_iban if not iban_valid(x)]
    nips = [p.nip for p in P.values() if p.nip]
    assert len(nips) == len(set(nips)), "NIP collision"
    party_ibans = [p.iban for p in P.values() if p.iban] + [a["iban"] for a in ACCOUNTS.values()]
    assert len(party_ibans) == len(set(party_ibans)), "IBAN collision"
    phones = [p.phone for p in P.values() if p.phone]
    assert len(phones) == len(set(phones)) and all(re.fullmatch(r"500\d{6}", x) for x in phones)
    for p in P.values():
        if p.nip:
            assert nip_valid(p.nip), (p.key, p.nip)
        if p.vat_eu and p.vat_eu.startswith("DE"):
            assert de_vat_valid(p.vat_eu), p.vat_eu
    numbers = set()
    for i in INV.values():
        assert ODM in (i.seller, i.buyer) and i.seller is not i.buyer, i.key
        cp = i.counterparty
        assert cp.name and (cp.nip and nip_valid(cp.nip) or cp.vat_eu and de_vat_valid(cp.vat_eu)), i.key
        assert ksef_valid(i.ksef) and i.ksef.startswith(i.seller.nip), i.ksef
        assert i.ksef[11:19] == f"{i.accepted_day:%Y%m%d}"
        assert KSEF_WINDOW_FROM <= i.accepted_day <= PERIOD_TO, i.key
        for rate, (net, vat) in i.totals.items():
            assert vat == r2(net * RATE_VALUE[rate]), (i.key, rate)
        assert i.gross == i.net + i.vat
        assert (i.sale is None) != (i.period is None) or i.kind == "KOR", i.key
        assert (i.seller.nip, i.kind, normalize_number(i.number)) not in numbers, i.key
        numbers.add((i.seller.nip, i.kind, normalize_number(i.number)))
        if i.kind == "KOR":
            assert i.gross < 0 and INV[i.corrects].gross + i.gross == D("8000.00")
    for e in EMAILS.values():
        vals = e.line_values()
        t = e.totals
        assert sum((v["gross"] for v in vals), D(0)) == t["gross"], e.id
        assert t["net"] + t["vat"] == t["gross"], e.id
    # September JPK rows that are also in KSeF must agree on NIP, number, date and amounts.
    for row in JPK:
        if row.ksef_key:
            i = INV[row.ksef_key]
            assert normalize_number(row.number) == normalize_number(i.number)
            assert (row.net, row.vat, row.issue) == (i.net, i.vat, i.issue)
            assert row.party.nip == i.counterparty.nip
    # Expected outcomes pinned here so that an accidental change of the scenario fails loudly.
    assert {r["key"] for r in open_items(S)} == {"S-FALA-LED", "S-FALA-LIGHT", "S-ORBITA-OCT", "S-NORDIS-ZB"}
    assert {r["key"] for r in open_items(PU)} == {"P-KADR-GDA", "P-KADR-WRO", "P-BUFET", "P-HALA"}
    assert sum(INV[f"P-ORANGE-{k:02d}"].gross for k in range(1, 13)) == ORANGE_TOTAL
    for ev, margin in EXPECTED_MARGINS.items():
        assert LEDGER[ev]["margin"] == margin, (ev, LEDGER[ev]["margin"], margin)


# ---------------------------------------------------------------------------
# Writers - generic
# ---------------------------------------------------------------------------

WRITTEN: list[Path] = []


def write_bytes(rel: str, data: bytes) -> Path:
    path = OUT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    WRITTEN.append(path)
    return path


def write_text(rel: str, text: str, encoding: str = "utf-8") -> Path:
    return write_bytes(rel, text.encode(encoding))


def write_json(rel: str, obj) -> Path:
    return write_text(rel, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def xml_bytes(root: ET.Element) -> bytes:
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True) + b"\n"


class X:
    """Tiny namespaced ElementTree builder."""

    def __init__(self, ns: str):
        self.ns = ns
        ET.register_namespace("", ns)

    def el(self, tag: str, **attrs) -> ET.Element:
        return ET.Element(f"{{{self.ns}}}{tag}", attrs)

    def sub(self, parent: ET.Element, tag: str, text=None, **attrs) -> ET.Element:
        node = ET.SubElement(parent, f"{{{self.ns}}}{tag}", attrs)
        if text is not None:
            node.text = text if isinstance(text, str) else (s2(text) if isinstance(text, Decimal) else str(text))
        return node


# ---------------------------------------------------------------------------
# Bank: EnableBanking-like JSON and camt.053
# ---------------------------------------------------------------------------

def eb_transaction(t: Tx) -> dict:
    acc = ACCOUNTS[t.account]
    credit = t.amount > 0
    own_party, own_acct, own_agent = {"name": ODM.name}, {"iban": acc["iban"]}, {"bic_fi": acc["bic"]}
    cp_name = t.cp_name or (t.cp.name if t.cp else None)
    cp_party = {"name": cp_name} if cp_name else None
    cp_acct = {"iban": t.cp_iban} if t.cp_iban else None
    cp_agent = None
    if t.cp_iban:
        bic = next((a["bic"] for a in ACCOUNTS.values() if a["iban"] == t.cp_iban), None) or \
            (t.cp.bic if t.cp and t.cp.iban == t.cp_iban else None)
        cp_agent = {"bic_fi": bic} if bic else None
    _, family, sub, desc = CODES[t.code]
    obj = {
        "entry_reference": t.ref,
        "merchant_category_code": t.mcc,
        "transaction_amount": {"currency": "PLN", "amount": s2(abs(t.amount))},
        "creditor": own_party if credit else cp_party,
        "creditor_account": own_acct if credit else cp_acct,
        "creditor_agent": own_agent if credit else cp_agent,
        "debtor": cp_party if credit else own_party,
        "debtor_account": cp_acct if credit else own_acct,
        "debtor_agent": cp_agent if credit else own_agent,
        "bank_transaction_code": {"description": desc, "code": family, "sub_code": sub},
        "credit_debit_indicator": "CRDT" if credit else "DBIT",
        "status": t.status,
        "booking_date": t.booking.isoformat() if t.booking else None,
        "value_date": (t.value or t.booking).isoformat() if t.booking else None,
        "transaction_date": (t.tx_date or t.booking).isoformat(),
        "balance_after_transaction": ({"currency": "PLN", "amount": s2(t.balance_after)}
                                      if acc["balance_after"] and t.status == "BOOK" else None),
        "reference_number": None,
        "remittance_information": t.title,
        "exchange_rate": None,
        "note": None,
    }
    if t.fx:
        obj["exchange_rate"] = {"unit_currency": t.fx[0], "exchange_rate": str(t.fx[2]),
                                "rate_type": "SALE" if t.op_type == "card" else "SPOT",
                                "contract_identification": None,
                                "instructed_amount": {"currency": t.fx[0], "amount": s2(t.fx[1])}}
    return obj


def write_bank() -> None:
    accounts = []
    for code, acc in ACCOUNTS.items():
        b = BALANCES[code]
        accounts.append({
            "uid": acc["uid"], "account_id": {"iban": acc["iban"]}, "name": ODM.name, "details": acc["name"],
            "product": acc["product"], "cash_account_type": "CACC", "currency": "PLN", "usage": "ORGA",
            "account_servicer": {"bic_fi": acc["bic"], "name": acc["bank"]},
            "fixture": {"slug": acc["slug"], "kind": acc["kind"], "has_balance_after_transaction": acc["balance_after"]},
        })
        base = f"bank/enablebanking/{acc['slug']}"
        write_json(f"{base}/transactions.json",
                   {"transactions": [eb_transaction(t) for t in booked(code)], "continuation_key": None})
        write_json(f"{base}/transactions_pending.json",
                   {"transactions": [eb_transaction(t) for t in pending(code)], "continuation_key": None})
        write_json(f"{base}/balances.json", {"balances": [
            {"name": "Saldo zaksięgowane na koniec dnia", "balance_amount": {"currency": "PLN", "amount": s2(b["closing"])},
             "balance_type": "CLBD", "reference_date": "2026-10-31", "last_change_date_time": ts(b["last_day"], 23, 59, 59),
             "last_committed_transaction": b["last_ref"]},
            {"name": "Saldo dostępne (po blokadach kartowych)",
             "balance_amount": {"currency": "PLN", "amount": s2(b["available"])}, "balance_type": "ITAV",
             "reference_date": "2026-10-31", "last_change_date_time": ts(ASOF, 23, 59, 59),
             "last_committed_transaction": None},
            {"name": "Saldo otwarcia okresu (rozszerzenie fixture: odpowiednik OPBD z camt.053)",
             "balance_amount": {"currency": "PLN", "amount": s2(b["opening"])}, "balance_type": "OPBD",
             "reference_date": "2026-10-01", "last_change_date_time": None, "last_committed_transaction": None},
        ]})
    write_json("bank/enablebanking/accounts.json", {"accounts": accounts})


def write_camt() -> None:
    code = "MB"
    acc, b = ACCOUNTS[code], BALANCES[code]
    x = X(CAMT_NS)
    root = x.el("Document")
    stmt_root = x.sub(root, "BkToCstmrStmt")
    hdr = x.sub(stmt_root, "GrpHdr")
    x.sub(hdr, "MsgId", "MBK-STMT-20261031-0350277812345")
    x.sub(hdr, "CreDtTm", "2026-11-01T06:00:00+01:00")
    stmt = x.sub(stmt_root, "Stmt")
    x.sub(stmt, "Id", "2026-10-0350277812345")
    x.sub(stmt, "ElctrncSeqNb", "10")
    x.sub(stmt, "CreDtTm", "2026-11-01T06:00:00+01:00")
    frto = x.sub(stmt, "FrToDt")
    x.sub(frto, "FrDtTm", "2026-10-01T00:00:00+02:00")
    x.sub(frto, "ToDtTm", "2026-10-31T23:59:59+01:00")
    acct = x.sub(stmt, "Acct")
    x.sub(x.sub(acct, "Id"), "IBAN", acc["iban"])
    x.sub(acct, "Ccy", "PLN")
    owner = x.sub(acct, "Ownr")
    x.sub(owner, "Nm", ODM.name)
    x.sub(x.sub(x.sub(acct, "Svcr"), "FinInstnId"), "BIC", acc["bic"])
    for kind, amount, day in (("OPBD", b["opening"], "2026-10-01"), ("CLBD", b["closing"], "2026-10-31")):
        bal = x.sub(stmt, "Bal")
        x.sub(x.sub(x.sub(bal, "Tp"), "CdOrPrtry"), "Cd", kind)
        x.sub(bal, "Amt", abs(amount), Ccy="PLN")
        x.sub(bal, "CdtDbtInd", "CRDT" if amount >= 0 else "DBIT")
        x.sub(x.sub(bal, "Dt"), "Dt", day)
    rows = booked(code)
    summ = x.sub(stmt, "TxsSummry")
    tot = x.sub(summ, "TtlNtries")
    x.sub(tot, "NbOfNtries", str(len(rows)))
    x.sub(tot, "Sum", sum((abs(t.amount) for t in rows), D(0)))
    x.sub(tot, "TtlNetNtryAmt", abs(b["credits"] + b["debits"]))
    x.sub(tot, "CdtDbtInd", "CRDT" if b["credits"] + b["debits"] >= 0 else "DBIT")
    for tag, sel in (("TtlCdtNtries", [t for t in rows if t.amount > 0]), ("TtlDbtNtries", [t for t in rows if t.amount < 0])):
        node = x.sub(summ, tag)
        x.sub(node, "NbOfNtries", str(len(sel)))
        x.sub(node, "Sum", sum((abs(t.amount) for t in sel), D(0)))
    for t in rows:
        credit = t.amount > 0
        domain, family, sub, desc = CODES[t.code]
        ntry = x.sub(stmt, "Ntry")
        x.sub(ntry, "NtryRef", t.ref)
        x.sub(ntry, "Amt", abs(t.amount), Ccy="PLN")
        x.sub(ntry, "CdtDbtInd", "CRDT" if credit else "DBIT")
        x.sub(ntry, "Sts", "BOOK")
        x.sub(x.sub(ntry, "BookgDt"), "Dt", t.booking.isoformat())
        x.sub(x.sub(ntry, "ValDt"), "Dt", (t.value or t.booking).isoformat())
        x.sub(ntry, "AcctSvcrRef", t.ref)
        btc = x.sub(ntry, "BkTxCd")
        dom = x.sub(btc, "Domn")
        x.sub(dom, "Cd", domain)
        fam = x.sub(dom, "Fmly")
        x.sub(fam, "Cd", family)
        x.sub(fam, "SubFmlyCd", sub)
        prtry = x.sub(btc, "Prtry")
        x.sub(prtry, "Cd", desc)
        x.sub(prtry, "Issr", "mBank")
        txd = x.sub(x.sub(ntry, "NtryDtls"), "TxDtls")
        x.sub(x.sub(txd, "Refs"), "EndToEndId", "NOTPROVIDED")
        if t.fx:
            ad = x.sub(txd, "AmtDtls")
            inst = x.sub(ad, "InstdAmt")
            x.sub(inst, "Amt", t.fx[1], Ccy=t.fx[0])
            xchg = x.sub(inst, "CcyXchg")
            x.sub(xchg, "SrcCcy", t.fx[0])
            x.sub(xchg, "TrgtCcy", "PLN")
            x.sub(xchg, "XchgRate", str(t.fx[2]))
            x.sub(x.sub(ad, "TxAmt"), "Amt", abs(t.amount), Ccy="PLN")
        cp_name = t.cp_name or (t.cp.name if t.cp else None)
        if cp_name or t.cp_iban:
            rp = x.sub(txd, "RltdPties")
            dbtr_nm, dbtr_iban = (cp_name, t.cp_iban) if credit else (ODM.name, acc["iban"])
            cdtr_nm, cdtr_iban = (ODM.name, acc["iban"]) if credit else (cp_name, t.cp_iban)
            if dbtr_nm:
                x.sub(x.sub(rp, "Dbtr"), "Nm", dbtr_nm)
            if dbtr_iban:
                x.sub(x.sub(x.sub(rp, "DbtrAcct"), "Id"), "IBAN", dbtr_iban)
            if cdtr_nm:
                x.sub(x.sub(rp, "Cdtr"), "Nm", cdtr_nm)
            if cdtr_iban:
                x.sub(x.sub(x.sub(rp, "CdtrAcct"), "Id"), "IBAN", cdtr_iban)
        rmt = x.sub(txd, "RmtInf")
        for line in t.title:
            for k in range(0, len(line), 140):
                x.sub(rmt, "Ustrd", line[k:k + 140])
    data = xml_bytes(root)
    write_bytes("bank/camt053/mbank_biezacy_2026-10.xml", data)
    # Parse back and verify the statement balances to the grosz.
    ns = {"c": CAMT_NS}
    doc = ET.fromstring(data)
    entries = doc.findall(".//c:Ntry", ns)
    total = sum((D(e.find("c:Amt", ns).text) * (1 if e.find("c:CdtDbtInd", ns).text == "CRDT" else -1)
                 for e in entries), D(0))
    bals = {b.find("c:Tp/c:CdOrPrtry/c:Cd", ns).text: D(b.find("c:Amt", ns).text) for b in doc.findall(".//c:Bal", ns)}
    opbd, clbd = bals["OPBD"], bals["CLBD"]
    assert opbd + total == clbd and len(entries) == len(rows)
    assert [e.find("c:NtryRef", ns).text for e in entries] == [t.ref for t in rows]


# ---------------------------------------------------------------------------
# KSeF: FA(3) XML + index
# ---------------------------------------------------------------------------

def qty_str(q: Decimal) -> str:
    return str(int(q)) if q == q.to_integral_value() else str(q)


def city(p: Party) -> str:
    return p.addr2.split(" ", 1)[1] if re.match(r"\d{2}-\d{3} ", p.addr2) else p.addr2


def fa_xml(i: Invoice) -> bytes:
    x = X(FA_NS)
    root = x.el("Faktura")
    hdr = x.sub(root, "Naglowek")
    x.sub(hdr, "KodFormularza", "FA", kodSystemowy="FA (3)", wersjaSchemy="1-0E")
    x.sub(hdr, "WariantFormularza", "3")
    created = datetime.fromisoformat(i.accepted_ts) - timedelta(minutes=3)
    x.sub(hdr, "DataWytworzeniaFa", utc_ts(created.date(), created.hour, created.minute, created.second))
    x.sub(hdr, "SystemInfo", i.system)
    for tag, p in (("Podmiot1", i.seller), ("Podmiot2", i.buyer)):
        node = x.sub(root, tag)
        if tag == "Podmiot1":
            x.sub(node, "PrefiksPodatnika", "PL")
        ident = x.sub(node, "DaneIdentyfikacyjne")
        if p.nip:
            x.sub(ident, "NIP", p.nip)
        else:
            x.sub(ident, "KodUE", p.vat_eu[:2])
            x.sub(ident, "NrVatUE", p.vat_eu[2:])
        x.sub(ident, "Nazwa", p.name)
        adr = x.sub(node, "Adres")
        x.sub(adr, "KodKraju", p.country)
        x.sub(adr, "AdresL1", p.addr1)
        x.sub(adr, "AdresL2", p.addr2)
        if p.email:
            x.sub(x.sub(node, "DaneKontaktowe"), "Email", p.email)
        if tag == "Podmiot2":
            x.sub(node, "JST", "2")
            x.sub(node, "GV", "2")
    fa = x.sub(root, "Fa")
    x.sub(fa, "KodWaluty", i.currency)
    x.sub(fa, "P_1", i.issue.isoformat())
    x.sub(fa, "P_1M", city(i.seller))
    x.sub(fa, "P_2", i.number)
    if i.period:
        okres = x.sub(fa, "OkresFa")
        x.sub(okres, "P_6_Od", i.period[0].isoformat())
        x.sub(okres, "P_6_Do", i.period[1].isoformat())
    elif i.sale and i.sale != i.issue:
        x.sub(fa, "P_6", i.sale.isoformat())
    for rate, (net, vat) in i.totals.items():
        f_net, f_vat = RATE_FIELDS[rate]
        x.sub(fa, f_net, net)
        if f_vat:
            x.sub(fa, f_vat, vat)
    x.sub(fa, "P_15", i.gross)
    adn = x.sub(fa, "Adnotacje")
    x.sub(adn, "P_16", "2")
    x.sub(adn, "P_17", "2")
    x.sub(adn, "P_18", "1" if i.reverse_charge else "2")
    x.sub(adn, "P_18A", "1" if i.mpp else "2")
    x.sub(x.sub(adn, "Zwolnienie"), "P_19N", "1")
    x.sub(x.sub(adn, "NoweSrodkiTransportu"), "P_22N", "1")
    x.sub(adn, "P_23", "2")
    x.sub(x.sub(adn, "PMarzy"), "P_PMarzyN", "1")
    x.sub(fa, "RodzajFaktury", i.kind)
    if i.kind == "KOR":
        base = INV[i.corrects]
        x.sub(fa, "PrzyczynaKorekty", i.correction_reason)
        x.sub(fa, "TypKorekty", str(i.correction_type))
        dk = x.sub(fa, "DaneFaKorygowanej")
        x.sub(dk, "DataWystFaKorygowanej", base.issue.isoformat())
        x.sub(dk, "NrFaKorygowanej", base.number)
        x.sub(dk, "NrKSeF", "1")
        x.sub(dk, "NrKSeFFaKorygowanej", base.ksef)
    if i.kind == "ROZ":
        for adv in i.advances:
            x.sub(x.sub(fa, "FakturaZaliczkowa"), "NrKSeFFaZaliczkowej", INV[adv].ksef)
    if i.kind != "ZAL":
        for n, ln in enumerate(i.lines, start=1):
            row = x.sub(fa, "FaWiersz")
            x.sub(row, "NrWierszaFa", "1" if i.kind == "KOR" else str(n))
            x.sub(row, "P_7", ln.name)
            x.sub(row, "P_8A", ln.unit)
            x.sub(row, "P_8B", qty_str(ln.qty))
            x.sub(row, "P_9A", ln.price)
            x.sub(row, "P_11", ln.net)
            x.sub(row, "P_12", ln.rate)
            if i.fx is not None:
                x.sub(row, "KursWaluty", str(i.fx))
            if ln.before:
                x.sub(row, "StanPrzed", "1")
    pay = x.sub(fa, "Platnosc")
    if i.paid_on:
        x.sub(pay, "Zaplacono", "1")
        x.sub(pay, "DataZaplaty", i.paid_on.isoformat())
    if i.due:
        x.sub(x.sub(pay, "TerminPlatnosci"), "Termin", i.due.isoformat())
    x.sub(pay, "FormaPlatnosci", "6")
    if i.pay_to:
        rb = x.sub(pay, "RachunekBankowy")
        if i.currency == "PLN":
            x.sub(rb, "NrRB", nrb(i.pay_to))
        else:
            x.sub(rb, "NrRB", i.pay_to)
            x.sub(rb, "SWIFT", next(a["bic"] for a in ACCOUNTS.values() if a["iban"] == i.pay_to))
        bank_name = next((a["bank"] for a in ACCOUNTS.values() if a["iban"] == i.pay_to), None) or \
            (i.seller.bank if i.seller.iban == i.pay_to else None)
        if bank_name:
            x.sub(rb, "NazwaBanku", bank_name)
    if i.kind == "ZAL":
        order = x.sub(fa, "Zamowienie")
        order_net = sum((ln.net for ln in i.lines), D(0))
        x.sub(order, "WartoscZamowienia", order_net + sum((r2(ln.net * RATE_VALUE[ln.rate]) for ln in i.lines), D(0)))
        for n, ln in enumerate(i.lines, start=1):
            row = x.sub(order, "ZamowienieWiersz")
            x.sub(row, "NrWierszaZam", str(n))
            x.sub(row, "P_7Z", ln.name)
            x.sub(row, "P_8AZ", ln.unit)
            x.sub(row, "P_8BZ", qty_str(ln.qty))
            x.sub(row, "P_9AZ", ln.price)
            x.sub(row, "P_11NettoZ", ln.net)
            x.sub(row, "P_11VatZ", r2(ln.net * RATE_VALUE[ln.rate]))
            x.sub(row, "P_12Z", ln.rate)
    return xml_bytes(root)


def verify_fa(i: Invoice, data: bytes) -> None:
    ns = {"f": FA_NS}
    doc = ET.fromstring(data)
    fa = doc.find("f:Fa", ns)
    p15 = D(fa.find("f:P_15", ns).text)
    sums = sum((D(fa.find(f"f:{f}", ns).text) for rate in i.totals for f in RATE_FIELDS[rate] if f), D(0))
    assert p15 == sums == i.gross, i.key
    rows = fa.findall("f:FaWiersz", ns)
    if i.kind in ("VAT", "ROZ"):
        line_net = sum((D(r.find("f:P_11", ns).text) for r in rows), D(0))
        expected = i.economic_net if i.kind == "ROZ" else i.net
        assert line_net == expected, i.key
    if i.kind == "KOR":
        diff = sum((D(r.find("f:P_11", ns).text) * (-1 if r.find("f:StanPrzed", ns) is not None else 1) for r in rows),
                   D(0))
        assert diff == i.net, i.key
    nip = doc.find("f:Podmiot1/f:DaneIdentyfikacyjne/f:NIP", ns).text
    assert nip == i.seller.nip


def write_ksef() -> list[dict]:
    index = []
    for i in sorted(INV.values(), key=lambda v: (v.accepted_ts, v.key)):
        data = fa_xml(i)
        verify_fa(i, data)
        rel = f"ksef/{i.direction}/{i.ksef}.xml"
        write_bytes(rel, data)
        index.append({
            "ksef_number": i.ksef, "file": rel.removeprefix("ksef/"), "direction": i.direction,
            "acceptance_timestamp": i.accepted_ts, "invoice_number": i.number, "invoice_type": i.kind,
            "issue_date": i.issue.isoformat(), "seller_nip": i.seller.nip, "seller_name": i.seller.name,
            "buyer_id": i.buyer.tax_id, "buyer_name": i.buyer.name, "currency": i.currency,
            "net": s2(i.net), "vat": s2(i.vat), "gross": s2(i.gross), "sha256": sha256(data),
            "offline": i.offline,
        })
    write_json("ksef/index.json", {
        "source": "mock KSeF - lista faktur przyjętych od 2026-09-14 do 2026-10-31 (sprzedaż i zakup ODM)",
        "subject_nip": ODM.nip, "window_from": KSEF_WINDOW_FROM.isoformat(), "window_to": PERIOD_TO.isoformat(),
        "count": len(index), "invoices": index})
    return index


# ---------------------------------------------------------------------------
# History: JPK_V7M (simplified) and the office export CSV
# ---------------------------------------------------------------------------

def write_jpk() -> None:
    x = X(JPK_NS)
    root = x.el("JPK")
    hdr = x.sub(root, "Naglowek")
    x.sub(hdr, "KodFormularza", "JPK_VAT", kodSystemowy="JPK_V7M (2)", wersjaSchemy="1-0E")
    x.sub(hdr, "WariantFormularza", "2")
    x.sub(hdr, "DataWytworzeniaJPK", "2026-10-19T14:22:31")
    x.sub(hdr, "NazwaSystemu", "BiuroFK (fixture, struktura uproszczona)")
    x.sub(hdr, "CelZlozenia", "1", poz="P_7")
    x.sub(hdr, "KodUrzedu", "1435")
    x.sub(hdr, "Rok", "2026")
    x.sub(hdr, "Miesiac", "9")
    p1 = x.sub(root, "Podmiot1", rola="Podatnik")
    onf = x.sub(p1, "OsobaNiefizyczna")
    x.sub(onf, "NIP", ODM.nip)
    x.sub(onf, "PelnaNazwa", ODM.name)
    x.sub(onf, "Email", ODM.email)
    dekl = x.sub(root, "Deklaracja")
    pd = x.sub(dekl, "PozycjeSzczegolowe")
    x.sub(pd, "P_19", str(sum((r.net for r in JPK if r.section == "sales"), D(0)).quantize(D(1), ROUND_HALF_UP)))
    x.sub(pd, "P_20", str(JPK_P38))
    x.sub(pd, "P_38", str(JPK_P38))
    x.sub(pd, "P_42", str(sum((r.net for r in JPK if r.section == "purchase"), D(0)).quantize(D(1), ROUND_HALF_UP)))
    x.sub(pd, "P_43", str(JPK_P48))
    x.sub(pd, "P_48", str(JPK_P48))
    x.sub(pd, "P_51", str(JPK_P51))
    ewid = x.sub(root, "Ewidencja")
    sales = [r for r in JPK if r.section == "sales"]
    purchases = [r for r in JPK if r.section == "purchase"]
    for n, r in enumerate(sales, start=1):
        w = x.sub(ewid, "SprzedazWiersz")
        x.sub(w, "LpSprzedazy", str(n))
        x.sub(w, "KodKrajuNadaniaTIN", "PL")
        x.sub(w, "NrKontrahenta", r.party.nip)
        x.sub(w, "NazwaKontrahenta", r.party.name)
        x.sub(w, "DowodSprzedazy", r.number)
        x.sub(w, "DataWystawienia", r.issue.isoformat())
        if r.sale_or_receipt != r.issue:
            x.sub(w, "DataSprzedazy", r.sale_or_receipt.isoformat())
        x.sub(w, "K_19", r.net)
        x.sub(w, "K_20", r.vat)
    ctrl = x.sub(ewid, "SprzedazCtrl")
    x.sub(ctrl, "LiczbaWierszySprzedazy", str(len(sales)))
    x.sub(ctrl, "PodatekNalezny", JPK_NALEZNY)
    for n, r in enumerate(purchases, start=1):
        w = x.sub(ewid, "ZakupWiersz")
        x.sub(w, "LpZakupu", str(n))
        x.sub(w, "KodKrajuNadaniaTIN", "PL")
        x.sub(w, "NrDostawcy", r.party.nip)
        x.sub(w, "NazwaDostawcy", r.party.name)
        x.sub(w, "DowodZakupu", r.number)
        x.sub(w, "DataZakupu", r.issue.isoformat())
        x.sub(w, "DataWplywu", r.sale_or_receipt.isoformat())
        x.sub(w, "K_42", r.net)
        x.sub(w, "K_43", r.vat)
    ctrl = x.sub(ewid, "ZakupCtrl")
    x.sub(ctrl, "LiczbaWierszyZakupow", str(len(purchases)))
    x.sub(ctrl, "PodatekNaliczony", JPK_NALICZONY)
    data = xml_bytes(root)
    write_bytes("history/JPK_V7M_2026-09.xml", data)
    ns = {"j": JPK_NS}
    doc = ET.fromstring(data)
    k20 = sum((D(e.text) for e in doc.findall(".//j:SprzedazWiersz/j:K_20", ns)), D(0))
    k43 = sum((D(e.text) for e in doc.findall(".//j:ZakupWiersz/j:K_43", ns)), D(0))
    assert k20 == D(doc.find(".//j:PodatekNalezny", ns).text) and k43 == D(doc.find(".//j:PodatekNaliczony", ns).text)
    assert D(doc.find(".//j:P_51", ns).text) == -TX["MV-US-VAT7"].amount


BIURO_ROWS: list[dict] = []


def write_biuro_export() -> None:
    """One month of 2025 from the bookkeeping office program: ';', decimal comma, Windows-1250."""
    rng = R("biuro:2025-10")
    sales_parties = ["acme", "nordis", "fala", "orbita", "kwadrat"]
    supplier_parties = ["biuro_centrum", "scena", "smaki", "wawel", "bilans", "mobilis", "drukarnia", "orange"]
    rows = []
    for n in range(1, 10):
        party = P[rng.choice(sales_parties)]
        net = D(rng.randint(150, 4200)) * 10
        rows.append(dict(numer=f"FV {n}/10/2025", data=date(2025, 10, min(28, 2 + 3 * n)), kontrahent=party.name,
                         nip=party.nip, netto=net, vat=r2(net * D("0.23")), typ="sprzedaż"))
    first = rows[2]
    kor_net = -r2(first["netto"] * D("0.1"))
    rows.append(dict(numer="KOR 1/10/2025", data=date(2025, 10, 29), kontrahent=first["kontrahent"], nip=first["nip"],
                     netto=kor_net, vat=r2(kor_net * D("0.23")), typ="korekta sprzedaży"))
    for n in range(1, 13):
        party = P[supplier_parties[(n - 1) % len(supplier_parties)]]
        net = D(rng.randint(2000, 180000)) / 100
        rate = D("0.08") if party.key in ("wawel", "smaki") else D("0.23")
        rows.append(dict(numer=f"{party.key[:3].upper()}/{rng.randint(100, 999)}/10/2025",
                         data=date(2025, 10, rng.randint(1, 31)), kontrahent=party.name, nip=party.nip, netto=r2(net),
                         vat=r2(net * rate), typ="zakup"))
    rows.append(dict(numer="5091772410", data=date(2025, 10, 14), kontrahent="Google Cloud EMEA Limited", nip="",
                     netto=D("341.88"), vat=D("78.63"), typ="import usług"))
    rows.sort(key=lambda r: (r["data"], r["numer"]))
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(["numer", "data", "kontrahent", "NIP", "netto", "VAT", "brutto", "typ"])
    for r in rows:
        r["brutto"] = r["netto"] + r["vat"]
        w.writerow([r["numer"], dpl(r["data"]), r["kontrahent"], r["nip"], pl2(r["netto"], ""), pl2(r["vat"], ""),
                    pl2(r["brutto"], ""), r["typ"]])
        BIURO_ROWS.append(r)
    write_text("history/biuro_export_2025-10.csv", buf.getvalue(), encoding="cp1250")


# ---------------------------------------------------------------------------
# Internal: PM sheet as XLSX and CSV
# ---------------------------------------------------------------------------

PM_HEADER = ["Osoba", "Data", "Event", "Kwota", "Forma", "Typ rozliczenia", "Uwagi"]


def normalize_zip(data: bytes) -> bytes:
    """Rewrite a ZIP container with fixed timestamps so the XLSX is byte-for-byte deterministic."""
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            zi = zipfile.ZipInfo(info.filename, date_time=FIXED_ZIP_TIME)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o600 << 16
            payload = src.read(info.filename)
            if info.filename == "docProps/core.xml":  # openpyxl stamps "modified" with the current time
                payload = re.sub(rb"(<dcterms:modified[^>]*>)[^<]*(</dcterms:modified>)",
                                 rb"\g<1>2026-11-01T08:00:00Z\g<2>", payload)
            dst.writestr(zi, payload)
    return out.getvalue()


def write_pm_sheet() -> None:
    import openpyxl
    from openpyxl.styles import Font, PatternFill

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Rozliczenia 10.2026"
    ws.append(PM_HEADER)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="DDEBF7")
    for r in PM_ROWS:
        ws.append([pm_display_name(r.person), datetime(r.day.year, r.day.month, r.day.day),
                   EVENTS[r.event]["pm_label"], float(r.amount), r.form, r.settlement, r.remarks])
        ws.cell(ws.max_row, 2).number_format = "DD.MM.YYYY"
        ws.cell(ws.max_row, 4).number_format = "#,##0.00"
    for col, width in zip("ABCDEFG", (24, 12, 16, 11, 10, 18, 48)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    fixed = datetime(2026, 11, 1, 8, 0, 0)
    wb.properties.creator = "Anna Mazur"
    wb.properties.lastModifiedBy = "Anna Mazur"
    wb.properties.created = fixed
    wb.properties.modified = fixed
    wb.properties.title = "Arkusz PM - październik 2026"
    buf = io.BytesIO()
    wb.save(buf)
    write_bytes("internal/arkusz_pm_2026-10.xlsx", normalize_zip(buf.getvalue()))

    out = io.StringIO()
    w = csv.writer(out, delimiter=";", lineterminator="\r\n")
    w.writerow(PM_HEADER)
    for r in PM_ROWS:
        w.writerow([pm_display_name(r.person), dpl(r.day), EVENTS[r.event]["pm_label"], pl2(r.amount, ""), r.form,
                    r.settlement, r.remarks])
    write_bytes("internal/arkusz_pm_2026-10.csv", b"\xef\xbb\xbf" + out.getvalue().encode("utf-8"))


# ---------------------------------------------------------------------------
# E-mail PDFs (reportlab, invariant mode = deterministic bytes)
# ---------------------------------------------------------------------------

FONT_CANDIDATES = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
]
FONT_INFO: dict = {}


def pdf_fonts() -> tuple[str, str]:
    if FONT_INFO:
        return FONT_INFO["regular"], FONT_INFO["bold"]
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for regular, bold in FONT_CANDIDATES:
        if Path(regular).exists() and Path(bold).exists():
            pdfmetrics.registerFont(TTFont("FixSans", regular))
            pdfmetrics.registerFont(TTFont("FixSans-Bold", bold))
            FONT_INFO.update(regular="FixSans", bold="FixSans-Bold", file=Path(regular).name, unicode=True)
            return "FixSans", "FixSans-Bold"
    print("UWAGA: brak czcionki TTF z polskimi znakami - PDF-y bez ogonków (Helvetica).", file=sys.stderr)
    FONT_INFO.update(regular="Helvetica", bold="Helvetica-Bold", file="Helvetica", unicode=False)
    return "Helvetica", "Helvetica-Bold"


_ASCII = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ°²", "acelnoszzACELNOSZZo2")


def pdf_text(s: str) -> str:
    return s if FONT_INFO.get("unicode") else s.translate(_ASCII)


def render_pdf(title: str, meta: list[tuple[str, str]], seller: list[str], buyer: list[str], header: list[str],
               widths: list[int], rows: list[list[str]], totals: list[tuple[str, str]], notes: list[str],
               receipt: bool = False) -> bytes:
    from reportlab.lib.pagesizes import A4, A6
    from reportlab.pdfgen import canvas

    regular, bold = pdf_fonts()
    buf = io.BytesIO()
    size = (A6[0] * 1.15, A6[1] * 1.6) if receipt else A4
    c = canvas.Canvas(buf, pagesize=size, invariant=1, pageCompression=0)
    c.setTitle(pdf_text(title))
    c.setAuthor("fixtures/generate.py")
    c.setCreator("ODM fixtures (dane testowe)")
    width, height = size
    m = 14 * 2.835 if receipt else 18 * 2.835
    y = height - m

    def line(text, x, yy, font=regular, sz=9.0):
        c.setFont(font, sz)
        c.drawString(x, yy, pdf_text(text))

    if receipt:
        for txt in seller:
            c.setFont(bold if txt is seller[0] else regular, 8)
            c.drawCentredString(width / 2, y, pdf_text(txt))
            y -= 11
        y -= 4
        for k, v in meta:
            line(f"{k} {v}", m, y, sz=7.5)
            y -= 10
        y -= 4
        c.setFont(bold, 9)
        c.drawCentredString(width / 2, y, pdf_text(title))
        y -= 14
        for row in rows:
            line(row[0], m, y, sz=7.5)
            c.setFont(regular, 7.5)
            c.drawRightString(width - m, y, pdf_text(row[1]))
            y -= 11
        y -= 4
        for k, v in totals:
            c.setFont(bold if k.startswith("SUMA PLN") else regular, 8 if k.startswith("SUMA PLN") else 7.5)
            c.drawString(m, y, pdf_text(k))
            c.drawRightString(width - m, y, pdf_text(v))
            y -= 11
        y -= 6
        for txt in notes:
            line(txt, m, y, sz=6.5)
            y -= 9
    else:
        line(title, m, y, bold, 15)
        y -= 22
        for k, v in meta:
            line(f"{k}: {v}", m, y)
            y -= 12
        y -= 8
        top = y
        for label, block, x0 in (("Sprzedawca / Seller", seller, m), ("Nabywca / Buyer", buyer, width / 2 + 5)):
            yy = top
            line(label, x0, yy, bold, 9)
            yy -= 13
            for txt in block:
                line(txt, x0, yy)
                yy -= 12
            y = min(y, yy)
        y -= 14
        xs = [m]
        for w_ in widths[:-1]:
            xs.append(xs[-1] + w_)
        c.setFillGray(0.9)
        c.rect(m, y - 4, width - 2 * m, 15, fill=1, stroke=0)
        c.setFillGray(0)
        for x0, h in zip(xs, header):
            line(h, x0 + 2, y, bold, 8)
        y -= 16
        for row in rows:
            for k, (x0, cell) in enumerate(zip(xs, row)):
                if k == 0:
                    line(cell[:70], x0 + 2, y, sz=8)
                else:
                    c.setFont(regular, 8)
                    c.drawRightString(x0 + widths[k] - 4, y, pdf_text(cell))
            y -= 13
            if len(row[0]) > 70:
                line(row[0][70:140], xs[0] + 2, y, sz=8)
                y -= 13
        c.line(m, y + 6, width - m, y + 6)
        y -= 8
        for k, v in totals:
            is_total = k.lower().startswith(("razem do zapłaty", "total", "amount due", "do zapłaty"))
            c.setFont(bold if is_total else regular, 10 if is_total else 9)
            c.drawRightString(width - m - 110, y, pdf_text(k))
            c.drawRightString(width - m, y, pdf_text(v))
            y -= 14
        y -= 10
        for txt in notes:
            line(txt, m, y, sz=8)
            y -= 11
    c.setFont(regular, 6.5)
    c.setFillGray(0.45)
    c.drawString(m, m * 0.6, pdf_text("DOKUMENT TESTOWY - dane fikcyjne (fixtures ODM, generate.py)"))
    c.showPage()
    c.save()
    return buf.getvalue()


def money(x, cur: str) -> str:
    return f"{pl2(x)} {cur}"


def usd(x, cur: str) -> str:
    return f"{cur} {r2(x):,.2f}"


def build_email_pdf(e: EmailDoc) -> bytes:
    vals = e.line_values()
    t = e.totals
    if e.id == "EM-01":
        return render_pdf(
            "Faktura VAT / Invoice", [("Numer faktury", e.number), ("Data faktury", dpl(e.issue)),
                                      ("Okres rozliczeniowy", f"{dpl(e.period[0])} - {dpl(e.period[1])}"),
                                      ("Identyfikator rozliczeniowy", "4812-6620-3017")],
            [e.seller.name, e.seller.addr1, e.seller.addr2, f"VAT: {e.seller.vat_eu}"],
            [ODM.name, ODM.addr1, ODM.addr2, f"VAT: PL{ODM.nip}", "Domena: odmpartners.test"],
            ["Opis", "Ilość", "Cena jedn.", "Kwota (EUR)"], [300, 50, 80, 80],
            [[v["name"], v["qty"], money(v["unit_price"], "EUR"), money(v["net"], "EUR")] for v in vals],
            [("Suma częściowa", money(t["net"], "EUR")), ("VAT (0%)", money(t["vat"], "EUR")),
             ("Razem do zapłaty", money(t["gross"], "EUR"))],
            ["Odwrotne obciążenie: VAT rozlicza nabywca (art. 196 dyrektywy 2006/112/WE).",
             "Płatność: obciążono kartę Mastercard zakończoną 4417. Nie należy dokonywać przelewu."])
    if e.id in ("EM-02", "EM-03"):
        return render_pdf(
            "Invoice", [("Invoice number", e.number), ("Date of issue", e.issue.strftime("%B %d, %Y")),
                        ("Service period", f"{e.period[0]:%b %d} - {e.period[1]:%b %d, %Y}"),
                        ("Payment method", "Mastercard - 4417")],
            [e.seller.name, e.seller.addr1, e.seller.addr2, f"EU VAT (OSS): {e.seller.vat_eu}"],
            [ODM.name, ODM.addr1, ODM.addr2, "Poland", f"VAT: PL{ODM.nip}"],
            ["Description", "Qty", "Unit price", "Amount"], [300, 50, 80, 80],
            [[v["name"], v["qty"], usd(v["unit_price"], "$"), usd(v["net"], "$")] for v in vals],
            [("Subtotal", usd(t["net"], "$")), ("Tax (reverse charge)", usd(t["vat"], "$")),
             ("Total", usd(t["gross"], "$")), ("Amount paid", usd(t["gross"], "$")), ("Amount due", usd(0, "$"))],
            ["Reverse charge - customer to account for VAT.", f"All amounts in {e.currency}."])
    if e.id == "EM-04":
        v = vals[0]
        return render_pdf(
            "PARAGON FISKALNY",
            [("Data:", "2026-10-24 17:42"), ("Nr wydr.:", e.number), ("Kasa:", "K02  Kasjer: 07")],
            [e.seller.name, e.seller.addr1, e.seller.addr2, f"NIP {e.seller.nip[:3]}-{e.seller.nip[3:6]}-"
             f"{e.seller.nip[6:8]}-{e.seller.nip[8:]}"], [], [], [],
            [[f"{v['name']}", ""], [f"  {pl2(v['qty'])} l x {pl2(v['unit_price'])}", f"{pl2(v['gross'])} A"]],
            [("SPRZEDAŻ OPODATKOWANA A", pl2(t["gross"])), ("PTU A 23,00%", pl2(t["vat"])),
             ("SUMA PTU", pl2(t["vat"])), ("SUMA PLN", pl2(t["gross"])), ("Karta płatnicza", pl2(t["gross"]))],
            ["Mastercard **** 4417  AUTH 604215", "#028417  NR UNIK. EAB 1802447133"], receipt=True)
    if e.id == "EM-05":
        p = e.seller
        return render_pdf(
            f"RACHUNEK nr {e.number}", [("Data wystawienia", dpl(e.issue)), ("Data wykonania usługi", dpl(e.sale)),
                                        ("Miejsce wystawienia", "Kraków")],
            [p.name, p.addr1, p.addr2, f"tel. {phone_fmt(p.phone)}"],
            [ODM.name, ODM.addr1, ODM.addr2, f"NIP {ODM.nip}"],
            ["Nazwa usługi", "Ilość", "Cena", "Wartość"], [300, 50, 80, 80],
            [[v["name"], v["qty"], money(v["unit_price"], "zł"), money(v["gross"], "zł")] for v in vals],
            [("Do zapłaty", money(t["gross"], "zł"))],
            ["Słownie: jeden tysiąc osiemset złotych 00/100.",
             "Działalność nierejestrowana (art. 5 ust. 1 Prawa przedsiębiorców) - sprzedawca nie jest podatnikiem VAT.",
             f"Płatne przelewem w terminie 7 dni na rachunek: {iban_fmt(p.iban)}"])
    if e.id == "EM-06":
        i = INV[e.ksef_dup]
        return render_pdf(
            f"Faktura VAT nr {i.number}", [("Data wystawienia", dpl(i.issue)),
                                           ("Okres świadczenia usługi", f"{dpl(i.period[0])} - {dpl(i.period[1])}"),
                                           ("Termin płatności", dpl(i.due)), ("Forma płatności", "przelew")],
            [i.seller.name, i.seller.addr1, i.seller.addr2, f"NIP {i.seller.nip}"],
            [ODM.name, ODM.addr1, ODM.addr2, f"NIP {ODM.nip}"],
            ["Nazwa", "Ilość", "Cena netto", "VAT", "Wartość netto"], [250, 45, 75, 40, 90],
            [[ln.name, f"{qty_str(ln.qty)} {ln.unit}", money(ln.price, "zł"), f"{ln.rate}%", money(ln.net, "zł")]
             for ln in i.lines],
            [(f"Netto {r}%", money(n, "zł")) for r, (n, _) in i.totals.items()] +
            [(f"VAT {r}%", money(v, "zł")) for r, (_, v) in i.totals.items()] +
            [("Razem netto", money(i.net, "zł")), ("Razem VAT", money(i.vat, "zł")),
             ("Razem do zapłaty", money(i.gross, "zł"))],
            [f"Rachunek: {i.seller.bank} {nrb_fmt(i.pay_to)}", "Dziękujemy za pobyt w Hotelu Wawel Grand."])
    raise KeyError(e.id)


def write_email_pdfs() -> None:
    index, expected = [], {}
    for e in EMAILS.values():
        data = build_email_pdf(e)
        write_bytes(f"email_pdfs/{e.filename}", data)
        mid = R(f"mail:{e.id}")
        index.append({
            "id": e.id, "mailbox": "glos", "message_id": f"<{mid.getrandbits(64):016x}.{e.id.lower()}@mail.test>",
            "from": e.mail["from_"], "to": e.mail["to"], "date": e.mail["date"], "subject": e.mail["subject"],
            "attachments": [{"filename": e.filename, "content_type": "application/pdf", "size": len(data),
                             "sha256": sha256(data)}],
        })
        vals = e.line_values()
        t = e.totals
        fx = e.fx
        expected[e.id] = {
            "file": e.filename, "doc_kind": e.kind,
            "seller": {"name": e.seller.name, "tax_id": e.seller.tax_id or None, "country": e.seller.country},
            "buyer": {"name": ODM.name, "tax_id": f"PL{ODM.nip}" if e.kind == "invoice" and e.currency != "PLN"
                      else (ODM.nip if e.kind != "receipt" else None)},
            "number": e.number, "issue_date": e.issue.isoformat(),
            "sale_date": e.sale.isoformat() if e.sale else None,
            "period_from": e.period[0].isoformat() if e.period else None,
            "period_to": e.period[1].isoformat() if e.period else None,
            "currency": e.currency,
            "lines": [{"name": v["name"], "quantity": v["qty"], "unit": v["unit"], "unit_price": v["unit_price"],
                       "vat_rate": v["vat_rate"], "net": s2(v["net"]), "vat": s2(v["vat"]), "gross": s2(v["gross"])}
                      for v in vals],
            "totals": {k: s2(t[k]) for k in ("net", "vat", "gross")},
            "lines_sum_equals_total": True,
            "vat_mode": e.vat_mode,
            "payment": e.payment,
            "pln": None if fx is None else {"nbp_rate": str(fx[0]), "nbp_table_date": fx[1].isoformat(),
                                            "gross_pln": s2(e.gross_pln)},
            "same_as_ksef": INV[e.ksef_dup].ksef if e.ksef_dup else None,
            "expected_bank_match": TX[e.tx].ref if e.tx else None,
            "expected_event": e.expected_event, "final_allocation": alloc_json(e.final, e.cost_pln),
            "note": e.note,
        }
    write_json("email_pdfs/index.json", {"source": "mock archiwum maili (projekt glos)", "messages": index})
    write_json("email_pdfs/expected.json", {
        "description": "Oczekiwana ekstrakcja z PDF-ów (to, co model ma odczytać; pozycje sumują się do kwoty).",
        "documents": expected})


def alloc_json(shares: list, amount: Decimal | None = None) -> list[dict]:
    parts = split_amount(amount, shares) if amount is not None else [(ev, None) for ev, _ in shares]
    out = []
    for (ev, share), (_, part) in zip(shares, parts):
        out.append({"event": ev, "event_name": EVENTS[ev]["name"] if ev in EVENTS else ev,
                    "share": f"{float(share):.6f}", "share_exact": str(share),
                    "amount_pln": None if part is None else s2(part)})
    return out


# ---------------------------------------------------------------------------
# Reference data: NBP rates
# ---------------------------------------------------------------------------

def write_nbp() -> None:
    def table_no(day: date) -> str:
        return f"{sum(1 for _ in business_days(date(2026, 1, 1), day)):03d}/A/NBP/2026"

    write_json("reference/nbp_rates.json", {
        "source": "FIKCYJNE kursy średnie NBP (tabela A) - tylko do testów; dni robocze 01.09-30.10.2026",
        "rule": "faktura walutowa: kurs z ostatniego dnia roboczego przed datą sprzedaży/wystawienia",
        "rates": {cur: [{"date": day.isoformat(), "table": table_no(day), "mid": str(rate)}
                        for day, rate in sorted(days.items())] for cur, days in NBP.items()}})


# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------

def pm_sheet_row(row_id: str) -> int:
    return next(n for n, r in enumerate(PM_ROWS, start=2) if r.id == row_id)


def doc_json(m: dict) -> dict:
    if m["kind"] == "ksef":
        i = INV[m["ref"]]
        return {"doc_type": "ksef", "doc_id": i.ksef, "invoice_number": i.number, "invoice_type": i.kind,
                "direction": i.direction, "amount_pln": s2(m["amount"]), "doc_currency": i.currency,
                "doc_amount": s2(m["doc_amount"])}
    if m["kind"] == "pdf":
        e = EMAILS[m["ref"]]
        return {"doc_type": "email_pdf", "doc_id": e.id, "invoice_number": e.number, "amount_pln": s2(m["amount"]),
                "doc_currency": e.currency, "doc_amount": s2(m["doc_amount"])}
    if m["kind"] == "pm":
        return {"doc_type": "pm_sheet", "doc_id": f"arkusz_pm_2026-10:{m['ref']}", "sheet_row": pm_sheet_row(m["ref"]),
                "amount_pln": s2(m["amount"])}
    return {"doc_type": "bank_tx", "doc_id": TX[m["ref"]].ref, "relation": m["relation"], "amount_pln": s2(m["amount"])}


def tx_json(t: Tx) -> dict:
    acc = ACCOUNTS[t.account]
    out = {
        "entry_reference": t.ref, "scenario_key": t.key, "account": acc["slug"], "status": t.status,
        "booking_date": t.booking.isoformat() if t.booking else None,
        "transaction_date": (t.tx_date or t.booking).isoformat(),
        "amount": s2(t.amount), "currency": "PLN",
        "counterparty": t.cp_name or (t.cp.name if t.cp else None), "counterparty_iban": t.cp_iban,
        "title": " | ".join(t.title), "op_type": t.op_type,
    }
    if t.status != "BOOK":
        out["note"] = "Blokada kartowa (PDNG): nie wchodzi do bilansu, zmniejsza tylko saldo dostępne."
        return out
    out.update({
        "expected_status": t.gt, "expected_route": t.route,
        "no_document_category": t.category, "no_document_subtype": t.subtype,
        "matched_documents": [doc_json(m) for m in t.matches],
        "difference": None if not t.diff else {"name": t.diff[0], "amount_pln": s2(t.diff[1])},
        "mpp_group": t.mpp,
        "mpp_vat_block": (lambda b: None if not b else {"vat": s2(b[0]), "idc": b[1], "inv": b[2]})(
            parse_vat_block(t.title[0])),
        "own_transfer_counterpart": TX[t.pair].ref if t.pair else None,
        "candidates": [{"doc_type": "ksef", "doc_id": INV[c].ksef, "invoice_number": INV[c].number,
                        "gross": s2(INV[c].gross)} for c in t.candidates],
        "linked_evidence": [f"arkusz_pm_2026-10:{e.split(':')[1]}" if e.startswith("pm:") else e for e in t.evidence],
        "sensitive_words": t.sensitive,
        "fx": None if not t.fx else {"currency": t.fx[0], "instructed_amount": s2(t.fx[1]), "bank_rate": str(t.fx[2])},
        "also_in_camt053": t.account == "MB",
        "scenario": t.scenario, "note": t.note or None,
    })
    return out


def revenue_recognition(i: Invoice) -> dict[str, str]:
    value = i.pln(i.economic_net)
    if i.period and i.direction == S:
        days = (i.period[1] - i.period[0]).days + 1
        months: dict[str, int] = {}
        for k in range(days):
            day = i.period[0] + timedelta(days=k)
            months[f"{day:%Y-%m}"] = months.get(f"{day:%Y-%m}", 0) + 1
        parts = split_amount(value, [(mth, Fraction(n, days)) for mth, n in months.items()])
        return {mth: s2(v) for mth, v in parts}
    day = i.sale or i.issue
    return {f"{day:%Y-%m}": s2(value)}


def invoice_json(i: Invoice) -> dict:
    settled = SETTLED.get(i.key, D(0))
    return {
        "ksef_number": i.ksef, "scenario_key": i.key, "file": f"ksef/{i.direction}/{i.ksef}.xml",
        "invoice_number": i.number, "normalized_number": normalize_number(i.number), "direction": i.direction,
        "invoice_type": i.kind, "acceptance_timestamp": i.accepted_ts, "offline": i.offline,
        "seller": {"nip": i.seller.nip, "name": i.seller.name},
        "buyer": {"tax_id": i.buyer.tax_id, "name": i.buyer.name},
        "counterparty_name": i.counterparty.name,
        "issue_date": i.issue.isoformat(), "sale_date": i.sale.isoformat() if i.sale else None,
        "period": None if not i.period else {"from": i.period[0].isoformat(), "to": i.period[1].isoformat()},
        "due_date": i.due.isoformat() if i.due else None, "due_date_missing": i.due is None,
        "currency": i.currency, "fx_rate_nbp": str(i.fx) if i.fx else None,
        "net": s2(i.net), "vat": s2(i.vat), "gross": s2(i.gross), "gross_pln": s2(i.gross_pln),
        "per_rate": {rate: {"net": s2(n), "vat": s2(v)} for rate, (n, v) in i.totals.items()},
        "economic_net_pln": s2(i.pln(i.economic_net)),
        "revenue_recognition": revenue_recognition(i) if i.direction == S else None,
        "mpp_flag": i.mpp, "reverse_charge": i.reverse_charge,
        "corrects": INV[i.corrects].ksef if i.corrects else None,
        "settles_advances": [INV[a].ksef for a in i.advances],
        "expected_event": i.expected_event,
        "expected_event_name": EVENTS[i.expected_event]["name"] if i.expected_event in EVENTS else i.expected_event,
        "allocation_signal": i.signal,
        "suggested_allocation": alloc_json(i.suggested) if i.suggested else None,
        "final_allocation": alloc_json(i.final, i.pln(i.economic_net)),
        "settled_doc_currency": s2(settled), "settled_by": [TX[k].ref for k in SETTLED_BY.get(i.key, [])],
        "open_at_2026_10_31": s2(i.gross - settled),
        "in_jpk_2026_09": i.jpk_number, "same_as_email_pdf": i.pdf, "note": i.note or None,
    }


def open_json(direction: str) -> dict:
    rows = []
    for r in open_items(direction):
        i = INV[r["key"]]
        rows.append({
            "ksef_number": i.ksef, "invoice_number": i.number, "counterparty": i.counterparty.name,
            "issue_date": i.issue.isoformat(), "due_date": i.due.isoformat() if i.due else None,
            "gross": s2(i.gross), "settled": s2(SETTLED.get(i.key, D(0))), "open": s2(r["open"]),
            "currency": i.currency, "age_days": r["days"],
            "age_basis": "od daty wystawienia (brak terminu płatności)" if r["due_missing"] else "po terminie płatności",
            "bucket": r["bucket"], "due_date_missing": r["due_missing"],
            "unresolved_payments": [TX[k].ref for k in r["pending"]],
        })
    buckets: dict[str, Decimal] = {}
    for r in open_items(direction):
        buckets[r["bucket"]] = buckets.get(r["bucket"], D(0)) + r["open"]
    return {"as_of": ASOF.isoformat(), "total": s2(sum((r["open"] for r in open_items(direction)), D(0))),
            "by_bucket": {k: s2(v) for k, v in sorted(buckets.items())}, "items": rows}


def ledger_doc_id(x: dict) -> str:
    if x["source"] == "ksef":
        return x["ksef"]
    return f"arkusz_pm_2026-10:{x['ref']}" if x["source"] == "pm_sheet" else x["ref"]


def event_json(ev: str) -> dict:
    e, led = EVENTS[ev], LEDGER[ev]
    client = P[e["client"]]

    def item(x: dict) -> dict:
        return {"source": x["source"], "doc_id": ledger_doc_id(x), "number": x["number"], "party": x["party"],
                "amount_pln": s2(x["amount"])}

    return {"id": ev, "name": e["name"], "client": client.name, "client_tax_id": client.tax_id,
            "date_from": e["date_from"].isoformat(), "date_to": e["date_to"].isoformat(), "city": e["city"],
            "pm_sheet_label": e["pm_label"],
            "person_days_pm_sheet": sum(1 for r in PM_ROWS if r.event == ev and r.settlement == "umowa zlecenie"),
            "revenue_net_pln": s2(led["revenue_total"]), "cost_net_pln": s2(led["cost_total"]),
            "margin_net_pln": s2(led["margin"]), "cash_contribution_pln": s2(CASH[ev]),
            "revenue_items": [item(x) for x in led["revenue"]], "cost_items": [item(x) for x in led["cost"]]}


def jpk_lp() -> list[tuple[int, JpkRow]]:
    out, counters = [], {"sales": 0, "purchase": 0}
    for r in JPK:
        counters[r.section] += 1
        out.append((counters[r.section], r))
    return out


def person_address(p: Party) -> dict | None:
    """Home address of a natural person (pseudonymisation target), split for leak checks."""
    if not p.addr1:
        return None
    street = re.sub(r"^(?:ul\.|al\.|pl\.|os\.)\s*", "", p.addr1)
    m = re.match(r"(\d{2}-\d{3})\s+(.+)$", p.addr2)
    return {"line1": p.addr1, "line2": p.addr2, "street": re.sub(r"\s+\d.*$", "", street),
            "postal_code": m.group(1) if m else None, "city": m.group(2) if m else p.addr2}


def build_ground_truth(ksef_index: list[dict]) -> dict:
    sales = [i for i in INV.values() if i.direction == S]
    purchases = [i for i in INV.values() if i.direction == PU]
    cash_in_sales = sum((m["amount"] for t in booked() if t.amount > 0 for m in t.matches if m["kind"] == "ksef"),
                        D(0))
    oct_revenue = sum((D(v) for i in sales for mth, v in revenue_recognition(i).items() if mth == "2026-10"), D(0))
    events_total = sum((CASH[ev] for ev in EVENTS), D(0))
    persons = [p for p in P.values() if p.person]
    return {
        "meta": {
            "company": {"name": ODM.name, "nip": ODM.nip}, "period": {"from": PERIOD_FROM.isoformat(),
                                                                       "to": PERIOD_TO.isoformat()},
            "as_of": ASOF.isoformat(), "seed": SEED, "generated_by": "fixtures/generate.py",
            "conventions": {
                "amounts": "kwoty jako napisy dziesiętne z kropką; 'amount' transakcji ze znakiem (+ wpływ, - wypływ)",
                "expected_status": "matched | no_document | own_transfer | to_clarify (status końcowy po przeglądzie)",
                "expected_route": "auto = automat może zamknąć sam; queue = MUSI trafić do kolejki "
                                  "(zamknięcie automatem to błąd); any = bez wymagań",
                "expected_event": "id eventu (automat, sygnał A) | UNASSIGNED | queue (sugestia, decyduje człowiek)",
                "final_allocation": "przypisanie po decyzjach człowieka - z niego liczona marża",
                "margin": "memoriałowo netto: przychód (ROZ = całe zamówienie, ZAL = 0, KOR = różnica) minus koszt "
                          "(faktury netto, paragon/rachunek brutto, wiersze PM); jeden koszt liczony raz",
                "difference.amount_pln": "kwota rozliczona przelewem minus wartość dokumentu w PLN",
            },
        },
        "accounts": [{
            "slug": acc["slug"], "iban": acc["iban"], "name": acc["name"], "kind": acc["kind"],
            "opening_balance": s2(BALANCES[c]["opening"]), "sum_credits": s2(BALANCES[c]["credits"]),
            "sum_debits": s2(BALANCES[c]["debits"]), "closing_balance": s2(BALANCES[c]["closing"]),
            "booked_count": BALANCES[c]["booked_count"], "pending_count": BALANCES[c]["pending_count"],
            "pending_sum": s2(BALANCES[c]["pending_sum"]), "available_balance": s2(BALANCES[c]["available"]),
        } for c, acc in ACCOUNTS.items()],
        "transactions": [tx_json(t) for t in booked()],
        "pending_transactions": [tx_json(t) for t in pending()],
        "invoices": [invoice_json(i) for i in sorted(INV.values(), key=lambda v: (v.direction != S, v.issue, v.key))],
        "email_documents": [{"id": e.id, "number": e.number, "seller": e.seller.name, "kind": e.kind,
                             "currency": e.currency, "gross": s2(e.totals["gross"]), "gross_pln": s2(e.gross_pln),
                             "cost_pln": s2(e.cost_pln), "expected_bank_match": TX[e.tx].ref if e.tx else None,
                             "same_as_ksef": INV[e.ksef_dup].ksef if e.ksef_dup else None,
                             "expected_event": e.expected_event, "final_allocation": alloc_json(e.final, e.cost_pln)}
                            for e in EMAILS.values()],
        "pm_sheet_rows": [{
            "id": f"arkusz_pm_2026-10:{r.id}", "sheet_row": pm_sheet_row(r.id), "person": P[r.person].name,
            "date": r.day.isoformat(), "event_label": EVENTS[r.event]["pm_label"], "amount": s2(r.amount),
            "form": r.form, "settlement_type": r.settlement,
            "expected_event": QUEUE, "final_event": r.event, "final_event_name": EVENTS[r.event]["name"],
            "expected_bank_match": next((t.ref for t in booked() for m in t.matches
                                         if m["kind"] == "pm" and m["ref"] == r.id), None) or
            next((t.ref for t in booked() if f"pm:{r.id}" in t.evidence), None),
            "same_cost_as": r.same_cost_as, "note": r.note or None,
        } for r in PM_ROWS],
        "events": [event_json(ev) for ev in EVENTS],
        "unassigned": {
            "revenue_items": [{"doc_id": ledger_doc_id(x), "number": x["number"], "party": x["party"],
                               "amount_pln": s2(x["amount"])} for x in LEDGER[UNASSIGNED]["revenue"]],
            "cost_items": [{"doc_id": ledger_doc_id(x), "number": x["number"], "party": x["party"],
                            "amount_pln": s2(x["amount"])} for x in LEDGER[UNASSIGNED]["cost"]],
            "note": "Kwoty całych dokumentów (bez podziału na okresy); Kwadrat FV 4/10/2026 rozkłada się 09/10.",
        },
        "receivables_open": open_json(S),
        "payables_open": open_json(PU),
        "metrics_expected": {
            "revenue_memorial_net_2026_10": s2(oct_revenue),
            "revenue_cash_gross_2026_10": s2(cash_in_sales),
            "receivables_open_total_2026_10_31": open_json(S)["total"],
            "payables_open_total_2026_10_31": open_json(PU)["total"],
            "cash_report_2026_10": {
                "events": s2(events_total), "unassigned": s2(CASH[UNASSIGNED]), "unmatched": s2(CASH["UNMATCHED"]),
                "balance_change": s2(CASH["_delta"]),
                "control_difference": s2(events_total + CASH[UNASSIGNED] + CASH["UNMATCHED"] - CASH["_delta"]),
            },
            "no_document_outflows_over_100": [
                {"entry_reference": t.ref, "amount": s2(t.amount), "category": t.category, "subtype": t.subtype}
                for t in booked() if t.gt == "no_document" and t.amount < -100],
        },
        "history_only_documents": [{
            "source": "JPK_V7M_2026-09", "section": r.section, "jpk_lp": lp, "number": r.number,
            "issue_date": r.issue.isoformat(), "counterparty": r.party.name, "nip": r.party.nip,
            "net": s2(r.net), "vat": s2(r.vat),
            "note": "rozliczona przed październikiem (poza zakresem wyciągów) - nie jest należnością/zobowiązaniem na 31.10"}
            for lp, r in jpk_lp() if not r.ksef_key],
        "dedup": {
            "ksef_jpk": [{"jpk_section": r.section, "jpk_lp": lp, "jpk_number": r.number,
                          "ksef_number": INV[r.ksef_key].ksef, "ksef_invoice_number": INV[r.ksef_key].number,
                          "normalized_number": normalize_number(r.number), "nip": r.party.nip}
                         for lp, r in jpk_lp() if r.ksef_key],
            "ksef_pdf": [{"ksef_number": INV[e.ksef_dup].ksef, "email_doc": e.id,
                          "reason": "ten sam NIP sprzedawcy, numer, data i kwota; PDF bez numeru KSeF"}
                         for e in EMAILS.values() if e.ksef_dup],
            "enablebanking_camt053": {"account": "mbank_biezacy", "count": BALANCES["MB"]["booked_count"],
                                      "rule": "NtryRef w camt.053 = entry_reference w JSON EnableBanking"},
            "not_duplicates": [
                {"a": {"source": "ksef", "doc_id": INV["P-KRESKA"].ksef, "number": "FV 12/09/2026",
                       "nip": P["kreska"].nip},
                 "b": {"source": "JPK_V7M_2026-09", "section": "purchase", "number": "FV 12/09/2026",
                       "nip": P["biuro_centrum"].nip},
                 "reason": "ten sam numer, inny NIP - nigdy duplikat"},
                {"a": {"source": "ksef", "doc_id": INV["S-ACME-FV7"].ksef, "number": "FV 7/10/2026",
                       "nip": ODM.nip},
                 "b": {"source": "ksef", "doc_id": INV["P-CZYNSZ"].ksef, "number": "FV 7/10/2026",
                       "nip": P["biuro_centrum"].nip},
                 "reason": "ten sam numer, inny wystawca i kierunek"},
            ],
            "one_cost_many_evidence": [
                {"cost": "konferansjer Gala ACME 1 800,00", "evidence": ["email_pdf:EM-05", "arkusz_pm_2026-10:R08",
                                                                         f"bank:{TX['MB-ZAWADZKI'].ref}"],
                 "count_in_margin": 1},
                {"cost": "hostessy opłacone BLIK-iem", "evidence": "wiersz arkusza PM + przelew BLIK (16 par)",
                 "count_in_margin": "raz, z wiersza arkusza"},
            ],
        },
        "checks": {
            "balances": {ACCOUNTS[c]["slug"]: f"{s2(b['opening'])} + {s2(b['credits'])} {s2(b['debits'])} = "
                                              f"{s2(b['closing'])}" for c, b in BALANCES.items()},
            "mpp": {"vat_blocks_in_incoming_titles": s2(MPP_CHECK["vat_blocks_incoming"]),
                    "credits_on_vat_account": s2(MPP_CHECK["vat_account_credits"]),
                    "groups": {g: refs for g, refs in MPP_CHECK["groups"].items()}},
            "ksef_completeness": {
                "count": len(ksef_index), "sales_count": len(sales), "purchase_count": len(purchases),
                "sales_gross_pln": s2(sum((i.gross_pln for i in sales), D(0))),
                "purchase_gross_pln": s2(sum((i.gross_pln for i in purchases), D(0)))},
            "jpk_2026_09": {"P_38": str(JPK_P38), "P_48": str(JPK_P48), "P_51": str(JPK_P51),
                            "vat7_payment": TX["MV-US-VAT7"].ref},
            "pm_sheet": {"rows": len(PM_ROWS), "rows_without_bank": ["arkusz_pm_2026-10:R20"],
                         "blik_without_row": [TX["MB-BLIK-ORPHAN"].ref]},
        },
        "pii": {
            "note": "dane osobowe (wymyślone), które NIE mogą trafić do modelu AI bez pseudonimizacji",
            "persons": [{"name": p.name, "phone": p.phone, "phone_formats": [phone_fmt(p.phone), phone_dash(p.phone),
                                                                               f"+48{p.phone}"],
                         "iban": p.iban, "role": p.role, "address": person_address(p)} for p in persons],
            "business_names_with_person": [{"name": p.name, "person": p.person_name, "iban": p.iban}
                                           for p in P.values() if p.person_name],
            "sensitive_transactions": [t.ref for t in booked() if t.sensitive],
        },
    }


# ---------------------------------------------------------------------------
# SCENARIUSZ.md (generated - always in sync with the data)
# ---------------------------------------------------------------------------

SCENARIOS = [
    ("mpp", "Split payment (MPP) i przeksięgowanie z rachunku VAT",
     "ACME płaci faktury FV 3/10/2026 (rozliczeniowa, BEZ adnotacji MPP) i FV 7/10/2026 (z adnotacją P_18A=1) "
     "podzieloną płatnością: kwota netto wpływa na rachunek bieżący mBank, kwota VAT na rachunek VAT, obie nogi mają "
     "ten sam tytuł `/VAT/…/IDC/5342650778/INV/…/TXT/…`. Dwie nogi to jedno zdarzenie dopasowane do faktury na kwotę "
     "brutto. ODM sam płaci splitem fakturę Scena Pro SP/2026/10/087 (netto z bieżącego, VAT z rachunku VAT). "
     "Przeksięgowanie z rachunku VAT na bieżący (postanowienie naczelnika US) to transfer własny. Kontrola MPP: "
     "suma bloków /VAT/ we wpływach = suma wpływów na rachunek VAT. Uwaga: numer FV 7/10/2026 ma też faktura "
     "czynszowa Biura Centrum - rozstrzyga NIP (IDC) i kierunek.",
     ["MB-ACME-ROZ", "MV-ACME-ROZ-VAT", "MB-ACME-FV7", "MV-ACME-FV7-VAT", "MB-SCENA-MPP", "MV-SCENA-MPP-VAT",
      "MB-FROM-VAT", "MV-TO-MB"], ["S-ACME-ROZ", "S-ACME-FV7", "P-SCENA-GALA"]),
    ("trzy-faktury", "Jeden przelew za trzy faktury",
     "ODM płaci Scena Pro jednym przelewem za trzy faktury roadshow Nordis; tytuł zawiera trzy numery, kwota = suma "
     "co do grosza. Oczekiwane: trzy dopasowania z kwotami w jednej grupie.",
     ["MB-SCENA-3FV"], ["P-SCENA-GDA", "P-SCENA-WRO", "P-SCENA-LDZ"]),
    ("korekta", "Korekta zmniejszająca: 10 000 - 2 000 = 8 000",
     "Faktura FV 2/10/2026 dla Nordis na 10 000,00 brutto, korekta KOR 1/10/2026 na -2 000,00 (rabat, wiersze "
     "StanPrzed/po, VAT liczony od różnicy). Klient płaci 8 000,00 z tytułem zawierającym tylko numer faktury "
     "pierwotnej. Saldo liczone z łańcucha faktura + korekta; jeden przelew spina oba dokumenty.",
     ["ML-NORDIS-KOR"], ["S-NORDIS-GDA", "S-NORDIS-KOR"]),
    ("revolut", "Wpływ od „Revolut” z klientem w tytule",
     "Nadawcą jest Revolut Bank UAB (IBAN LT), a nie klient; klient (Kwadrat Development) jest tylko w tytule, bez "
     "numeru faktury. Kwota równa jedynej otwartej fakturze Kwadrat. Oczekiwane: kolejka z jedną propozycją; po "
     "akceptacji reguła „ten IBAN LT = Kwadrat”.", ["MB-REVOLUT-KWADRAT"], ["S-KWADRAT"]),
    ("karta-saas", "Karta za zagraniczny SaaS (różnica kursowa) i paragon",
     "Google Workspace (99 EUR), Anthropic (150 USD), Notion (72 USD) - faktury przychodzą PDF-em, płatność kartą w "
     "PLN po kursie banku, 1-2 dni po dacie faktury. Faktura trzyma walutę; PLN z kursu NBP z dnia poprzedniego "
     "(reference/nbp_rates.json). Różnica ma nazwę „kurs”. Paragon za paliwo (Łódź, 24.10) - dowód PDF, "
     "przypisanie do eventu tylko jako podpowiedź (miejsce + data).",
     ["MB-CARD-GOOGLE", "MB-CARD-ANTHROPIC", "MB-CARD-NOTION", "MB-CARD-PETROVA"], []),
    ("blik", "BLIK dla hostess na telefon + arkusz PM",
     "16 przelewów BLIK na telefon (500-xxx-xxx w tytule, bez IBAN-u) ma odpowiedniki w arkuszu PM "
     "(internal/arkusz_pm_2026-10.*; nazwisko w formacie „Nazwisko Imię”, data pracy wcześniejsza niż przelew). "
     "Arkusz to dokument wewnętrzny, ale nie twardy dowód (decyzja D2) - kolejka. Dodatkowo: zwrot kosztów dla PM "
     "i umowa o dzieło (przelewy z dokumentem w arkuszu), JEDEN wiersz bez odpowiednika w banku (R20, Sadowska - do "
     "wypłaty) i JEDEN BLIK bez wiersza (Michalak, 300,00 - do wyjaśnienia).",
     [f"MB-BLIK-{k:02d}" for k in range(1, 17)] + ["MB-BLIK-ORPHAN", "MB-ZWROT-MAZUR", "MB-WRONA"], []),
    ("bez-dokumentu", "ZUS, US, prowizje, wynagrodzenia - bez dokumentów",
     "Składki ZUS, PIT-4R, zaliczka CIT, VAT-7 (z rachunku VAT; kwota = P_51 z JPK_V7M za wrzesień), prowizje i "
     "opłaty bankowe, odsetki, wynagrodzenia. To osobne hipotezy z kategoriami, nie „nie znaleźliśmy”. "
     "Tytuły podatkowe w formacie `/TI/N…/OKR/26M09/SFP/…/TXT/…`.",
     ["MB-ZUS", "MB-US-PIT4", "MB-US-CIT", "MV-US-VAT7", "MB-FEE-SEPA", "MB-FEE-ACCOUNT", "ML-FEE-EXPRESS",
      "ML-FEE-ACCOUNT", "ML-INTEREST", "MB-SALARY-MAZUR", "MB-SALARY-SOWINSKI", "MB-SALARY-WIERZCHOWSKA",
      "MB-SALARY-GRABOWIECKI"], []),
    ("zaliczka", "Zaliczki: przychodząca przed fakturą i wychodząca bez faktury",
     "ACME wpłaca 29 520,00 zaliczki 01.10 bez numeru faktury; faktura zaliczkowa FV 1/10/2026 (RodzajFaktury ZAL, "
     "P_6 = data wpłaty, Zamowienie z wartością zamówienia) powstaje 02.10, a rozliczeniowa FV 3/10/2026 (ROZ) po "
     "evencie. Zaliczka nie jest przychodem - przychodem jest całe zamówienie 80 000 netto w dacie sprzedaży ROZ. "
     "ODM wpłaca 5 000,00 zaliczki na poczet faktury Dworowi Kalina (rezerwacja na listopad) - w październiku "
     "brak dokumentu: no_document/Zaliczki, czeka do 180 dni.", ["MB-ACME-ZAL", "MB-ZALICZKA-KALINA"],
     ["S-ACME-ZAL", "S-ACME-ROZ"]),
    ("duplikat", "Nadpłata (duplikat wpłaty) i zwrot",
     "Orbita Media płaci FV 5/09/2026 dwa razy (13.10 i 14.10, ten sam tytuł i kwota). Druga wpłata = nadpłata z "
     "flagą „możliwy duplikat” (zawsze kolejka); zwrot 21.10 wiąże się z drugą wpłatą, nic nie znika. Dopasowania "
     "faktury nie przekraczają jej kwoty.", ["MB-ORBITA-1", "MB-ORBITA-2", "MB-ORBITA-ZWROT"], ["S-ORBITA-SEP"]),
    ("remis", "Remis: dwie faktury po 4 500 zł i przelew „zaplata”",
     "Kadr Studio wystawiło dwie faktury po 4 500,00 (fotorelacje Gdańsk i Wrocław). ODM wysyła 4 500,00 z tytułem "
     "„zaplata”. Remis = zawsze człowiek; do rozstrzygnięcia obie faktury pozostają otwarte w zobowiązaniach.",
     ["MB-KADR-REMIS"], ["P-KADR-GDA", "P-KADR-WRO"]),
    ("czesciowa", "Numer faktury w tytule, ale niezgodna kwota (częściowa zapłata)",
     "Agencja Fala płaci 5 000,00 tytułem „FV 4/09/2026 czesc 1” przy fakturze 9 225,00. Twardy identyfikator "
     "zostaje, ale kwota idzie do warstwy 3: dopasowanie częściowe z różnicą „niedopłata”; reszta 4 225,00 jest "
     "należnością przeterminowaną (kubełek 31-60).", ["ML-FALA-CZESC"], ["S-FALA-LED"]),
    ("waluta", "Przelew walutowy EUR na rachunek PLN",
     "Helios Software GmbH płaci 12 500,00 EUR za fakturę FV 5/10/2026 (EUR, np II, odwrotne obciążenie). Bank "
     "przewalutowuje po 4,2190 i księguje 52 737,50 PLN (exchange_rate w JSON, AmtDtls/CcyXchg w camt). Faktura w "
     "PLN po NBP 4,2468 = 53 085,00; różnica „kurs” -347,50. Osobna prowizja za przelew SEPA bez dokumentu.",
     ["MB-HELIOS-EUR", "MB-FEE-SEPA"], ["S-HELIOS"]),
    ("wlasne", "Przelewy własne mBank <-> Millennium",
     "Zasilenie Millennium z mBanku (ten sam dzień) i zwrot z Millennium do mBanku (obciążenie 29.10, uznanie 30.10 - "
     "parowanie z przesunięciem daty). Status own_transfer, obie nogi.",
     ["MB-OWN-TO-ML", "ML-OWN-FROM-MB", "ML-OWN-TO-MB", "MB-OWN-FROM-ML"], []),
    ("wrazliwe", "Tytuł z nazwiskiem i słowem wrażliwym (komornik)",
     "Przelew do komornika z tytułem zawierającym słowo „Komornik”, sygnaturę i nazwisko dłużnika (pracownika). "
     "Bramka pseudonimizacji: tytuł omija model AI w ogóle, trafia do kolejki, kategorię nadaje człowiek. Pełna "
     "lista danych osobowych z fixtures jest w ground_truth.json -> pii.", ["MB-KOMORNIK"], []),
    ("ten-sam-numer", "Ten sam numer faktury u innego NIP-u",
     "Czynsz Biura Centrum ma numer FV 7/10/2026 - taki sam jak nasza sprzedaż dla ACME; w JPK za wrzesień Biuro "
     "Centrum ma FV 12/09/2026 - taki sam numer jak faktura Studia Kreska w KSeF. Ten sam numer u innego NIP-u to "
     "nigdy nie duplikat.", ["MB-CZYNSZ", "ML-KRESKA"], ["P-CZYNSZ", "S-ACME-FV7", "P-KRESKA"]),
    ("jeden-koszt", "Jeden koszt, trzy dowody",
     "Konferansjer Marek Zawadzki: rachunek PDF z maila (EM-05), wiersz arkusza PM R08 i przelew 1 800,00. Do marży "
     "Gali ACME liczony raz.", ["MB-ZAWADZKI"], []),
    ("dedup", "Deduplikacja: ta sama faktura z kilku kanałów",
     "Faktura Hotelu Wawel Grand jest w KSeF i jako PDF w mailu (EM-06, bez numeru KSeF na wydruku). Wrześniowe "
     "faktury (Fala, Orbita, Kreska, Drukarnia) są w KSeF i w JPK_V7M (jedna z numerem zapisanym „FV/5/09/2026”). "
     "Wszystkie transakcje mBanku bieżącego są w JSON EnableBanking i w camt.053 (ten sam entry_reference).",
     ["ML-WAWEL"], ["P-WAWEL", "S-FALA-LED", "S-ORBITA-SEP", "P-KRESKA", "P-DRUK"]),
    ("orange", "12 drobnych faktur Orange (jednorodna paczka)",
     "12 faktur Orange na łącznie 4 380,00 zł, każda z własnym rachunkiem wirtualnym i numerem w tytule przelewu. "
     "Kandydat na zbiorczą akceptację paczki; koszt ogólny (UNASSIGNED).",
     [f"MB-ORANGE-{k:02d}" for k in range(1, 13)], [f"P-ORANGE-{k:02d}" for k in range(1, 13)]),
    ("podstawowe", "Ścieżka podstawowa",
     "Przelewy z numerem faktury, zgodnym kontrahentem i kwotą co do grosza - automat może zamknąć sam.",
     ["MB-DRUK", "MB-BILANS", "MB-SMAKI", "MB-MOBILIS-DD", "ML-KRESKA", "ML-LOFT", "ML-ODRA", "ML-NORDIS-WRO"], []),
]

DOC_TRAPS = [
    ("Faktura zbiorcza za trzy eventy", "S-NORDIS-ZB",
     "Jedna pozycja „Obsługa eventów - październik 2026” na 50 000 netto za trzy roadshow Nordis. Faktura zbiorcza "
     "nigdy nie jest kotwicą eventu. Brak ofert -> podział wg osobodni z arkusza PM (Gdańsk 2, Wrocław 3, Łódź 5 = "
     "20/30/50) jako SUGESTIA w kolejce. Wystawiona 31.10, płatna 30.11 - należność niewymagalna."),
    ("Okres usługi zamiast daty sprzedaży (przez dwa miesiące)", "S-KWADRAT",
     "OkresFa 15.09-14.10.2026 zamiast P_6: przychód 12 000 netto rozkłada się po dniach - 6 400 na wrzesień, "
     "5 600 na październik."),
    ("Faktura bez terminu płatności", "S-ORBITA-OCT",
     "Brak TerminPlatnosci (pole opcjonalne w FA(3)). Brak terminu nie znaczy „płatne od ręki”: wiek należności "
     "liczony od daty wystawienia i oznaczony."),
    ("Faktura w EUR z kursem w pozycji", "S-HELIOS",
     "KodWaluty EUR, P_13_9 (np II), P_18=1 (odwrotne obciążenie), KursWaluty w FaWiersz. Kwota główna w EUR, "
     "PLN pochodna."),
    ("Hotel z okresem pobytu (sygnał B)", "P-WAWEL",
     "OkresFa 08-10.10.2026, dwie stawki VAT (8% nocleg, 23% parking), w pozycjach brak nazwy eventu. Kontrahent "
     "+ okres + miasto wskazują jedyny event w oknie (Gala ACME) - sugestia, nie automat."),
    ("Sala bez nazwy eventu (sygnał B)", "P-ODRA",
     "Wynajem sali we Wrocławiu 17.10 bez nazwy eventu - sugestia Roadshow Nordis Wrocław, kolejka."),
    ("Projekt na trzy eventy", "P-KRESKA",
     "Pozycja wymienia trzy miasta roadshow - podział równy tylko decyzją człowieka (kolejka)."),
    ("KSeF offline", "P-KADR-GDA",
     "Faktura wystawiona 05.10, przyjęta w KSeF 06.10 - data w numerze KSeF różni się od daty wystawienia."),
    ("Brak P_6 = data sprzedaży jak data wystawienia", "P-LOFT",
     "Gdy data sprzedaży jest równa dacie wystawienia, FA(3) nie zawiera P_6 - parser musi przyjąć P_1."),
]


def md_table(header: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    out += ["| " + " | ".join(str(c).replace("|", "\\|") for c in row) + " |" for row in rows]
    return "\n".join(out)


def match_label(m: dict) -> str:
    if m["kind"] == "ksef":
        return f"{INV[m['ref']].number} ({pl2(m['amount'])})"
    if m["kind"] == "pdf":
        return f"{m['ref']} {EMAILS[m['ref']].number} ({pl2(m['amount'])})"
    if m["kind"] == "pm":
        return f"PM {m['ref']} ({pl2(m['amount'])})"
    return f"przelew `{TX[m['ref']].ref}` ({m['relation']})"


def tx_row(t: Tx) -> list[str]:
    status = t.gt + (f" ({t.category})" if t.category else "")
    docs = "; ".join(match_label(m) for m in t.matches)
    if t.candidates:
        docs = "remis: " + " / ".join(INV[c].number for c in t.candidates)
    if t.pair:
        docs = f"para `{TX[t.pair].ref}`"
    if t.diff:
        docs += f"; różnica „{t.diff[0]}” {pl2(t.diff[1])}"
    title = t.title[0] if len(t.title) == 1 else f"{t.title[0]} (+{len(t.title) - 1})"
    return [f"`{t.ref}`", ACCOUNTS[t.account]["slug"], t.booking.isoformat(), pl2(t.amount),
            (t.cp_name or (t.cp.name if t.cp else "-"))[:40], title[:100], status, t.route, docs or "-"]


def inv_row(i: Invoice) -> list[str]:
    return [i.number, i.kind, "sprzedaż" if i.direction == S else "zakup", i.counterparty.name, i.issue.isoformat(),
            f"{pl2(i.gross)} {i.currency}", f"`{i.ksef}`"]


def write_scenario(gt: dict) -> None:
    L_ = []
    a = L_.append
    a("# Scenariusz danych testowych ODM - październik 2026")
    a("")
    a("> Plik generowany przez `fixtures/generate.py` - nie edytuj ręcznie. Wszystkie firmy, osoby, NIP-y, IBAN-y,")
    a("> telefony i numery KSeF są wymyślone (sumy kontrolne poprawne). Prawda referencyjna: `ground_truth.json`.")
    a("")
    a(f"Firma: **{ODM.name}**, NIP {ODM.nip}. Okres: 01.10-31.10.2026 plus faktury wrześniowe płacone w "
      "październiku i październikowe płatne w listopadzie (należności mają wiek). Stan na 31.10.2026.")
    a("")
    a("## Liczby")
    a("")
    n_book, n_pend = len(booked()), len(pending())
    n_sales = sum(1 for i in INV.values() if i.direction == S)
    n_pur = len(INV) - n_sales
    a(md_table(["Co", "Ile"], [
        ["Transakcje zaksięgowane (BOOK), 3 rachunki", n_book], ["Transakcje oczekujące (PDNG)", n_pend],
        ["Faktury KSeF - sprzedaż / zakup", f"{n_sales} / {n_pur}"], ["PDF-y z maili", len(EMAILS)],
        ["Wiersze arkusza PM", len(PM_ROWS)], ["Wiersze JPK_V7M 09/2026 (sprzedaż / zakup)",
                                                f"{sum(1 for r in JPK if r.section == 'sales')} / "
                                                f"{sum(1 for r in JPK if r.section == 'purchase')}"],
        ["Wiersze eksportu biura 10/2025", len(BIURO_ROWS)], ["Eventy", len(EVENTS)]]))
    a("")
    a("## Rachunki i bilans")
    a("")
    a(md_table(["Rachunek", "IBAN", "Saldo otwarcia", "Wpływy", "Wypływy", "Saldo zamknięcia", "BOOK", "PDNG",
                "Dostępne"],
               [[acc["name"], f"`{acc['iban']}`", pl2(BALANCES[c]["opening"]), pl2(BALANCES[c]["credits"]),
                 pl2(BALANCES[c]["debits"]), pl2(BALANCES[c]["closing"]), BALANCES[c]["booked_count"],
                 BALANCES[c]["pending_count"], pl2(BALANCES[c]["available"])] for c, acc in ACCOUNTS.items()]))
    a("")
    a("Saldo otwarcia + wszystkie operacje BOOK = saldo zamknięcia co do grosza (asercja w skrypcie, także po "
      "ponownym odczycie camt.053). Blokady kartowe (PDNG) są w osobnych plikach `transactions_pending.json` i "
      "zmniejszają tylko saldo dostępne (ITAV).")
    a("")
    a("## Eventy i marża memoriałowa netto")
    a("")
    a(md_table(["Id", "Event", "Klient", "Daty", "Miasto", "Przychód", "Koszt", "Marża", "Cash contribution"],
               [[ev["id"], ev["name"], ev["client"], f"{ev['date_from']} - {ev['date_to']}", ev["city"],
                 pl2(D(ev["revenue_net_pln"])), pl2(D(ev["cost_net_pln"])), pl2(D(ev["margin_net_pln"])),
                 pl2(D(ev["cash_contribution_pln"]))] for ev in gt["events"]]))
    a("")
    a("Marża liczona z `final_allocation` (po decyzjach człowieka): faktura zbiorcza 20/30/50, projekt Kreska po "
      "1/3, hotel i sala Odra wg sugestii, paragon za paliwo -> Łódź. Zaliczka nie jest przychodem, ROZ = całe "
      "zamówienie, korekta = różnica. Wiersze arkusza PM to koszt (jeden raz, nawet gdy jest też PDF i przelew). "
      "ZUS/PIT od umów cywilnoprawnych -> UNASSIGNED (D9).")
    a("")
    cr = gt["metrics_expected"]["cash_report_2026_10"]
    a("Raport kasowy października: suma eventów "
      f"{pl2(D(cr['events']))} + UNASSIGNED {pl2(D(cr['unassigned']))} + niesparowane {pl2(D(cr['unmatched']))} "
      f"= zmiana sald {pl2(D(cr['balance_change']))}; różnica kontrolna {pl2(D(cr['control_difference']))}.")
    a("")
    a("## Pułapki w banku")
    a("")
    for _sid, title, desc, tx_keys, inv_keys in SCENARIOS:
        a(f"### {title}")
        a("")
        a(desc)
        a("")
        if tx_keys:
            a(md_table(["entry_reference", "Rachunek", "Data", "Kwota", "Kontrahent", "Tytuł", "Status", "Ścieżka",
                        "Dokumenty / różnica"],
                       [tx_row(TX[k]) for k in tx_keys]))
            a("")
        if inv_keys:
            a(md_table(["Faktura", "Rodzaj", "Kierunek", "Kontrahent", "Wystawiona", "Brutto", "Numer KSeF"],
                       [inv_row(INV[k]) for k in inv_keys]))
            a("")
    a("## Pułapki w dokumentach")
    a("")
    for title, key, desc in DOC_TRAPS:
        i = INV[key]
        a(f"- **{title}** - {i.number} (`{i.ksef}`): {desc}")
    a("")
    a("## Należności otwarte na 31.10.2026")
    a("")
    rec = gt["receivables_open"]
    a(md_table(["Faktura", "Kontrahent", "Wystawiona", "Termin", "Brutto", "Zapłacono", "Otwarte",
                "Dni po terminie*", "Kubełek"],
               [[r["invoice_number"], r["counterparty"], r["issue_date"], r["due_date"] or "brak",
                 pl2(D(r["gross"])), pl2(D(r["settled"])), pl2(D(r["open"])), r["age_days"], r["bucket"]]
                for r in rec["items"]]))
    a("")
    a(f"Razem: {pl2(D(rec['total']))} zł. *Ujemne = dni do terminu; przy braku terminu - dni od wystawienia "
      "(flaga `due_date_missing`).")
    a("")
    a("## Zobowiązania otwarte na 31.10.2026")
    a("")
    pay = gt["payables_open"]
    a(md_table(["Faktura", "Kontrahent", "Termin", "Otwarte", "Kubełek", "Nierozstrzygnięta płatność"],
               [[r["invoice_number"], r["counterparty"], r["due_date"] or "brak", pl2(D(r["open"])), r["bucket"],
                 ", ".join(f"`{x}`" for x in r["unresolved_payments"]) or "-"] for r in pay["items"]]))
    a("")
    a(f"Razem: {pl2(D(pay['total']))} zł.")
    a("")
    a("## Faktury KSeF")
    a("")
    a(md_table(["Faktura", "Rodzaj", "Kierunek", "Kontrahent", "Wystawiona", "Brutto", "Numer KSeF"],
               [inv_row(i) for i in sorted(INV.values(), key=lambda v: (v.direction != S, v.issue, v.key))]))
    a("")
    a("## Wszystkie transakcje zaksięgowane")
    a("")
    a(md_table(["entry_reference", "Rachunek", "Data", "Kwota", "Kontrahent", "Tytuł", "Status", "Ścieżka",
                        "Dokumenty / różnica"],
               [tx_row(t) for t in booked()]))
    a("")
    a("Ścieżka: `auto` - automat może zamknąć sam; `queue` - musi trafić do kolejki; `any` - bez wymagań.")
    a("")
    write_text("SCENARIUSZ.md", "\n".join(L_))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def verify_written_bank() -> None:
    """Re-read the JSON files and check the balance per account from what was written."""
    for code, acc in ACCOUNTS.items():
        base = OUT / "bank/enablebanking" / acc["slug"]
        txs = json.loads((base / "transactions.json").read_text(encoding="utf-8"))["transactions"]
        bals = {b["balance_type"]: D(b["balance_amount"]["amount"])
                for b in json.loads((base / "balances.json").read_text(encoding="utf-8"))["balances"]}
        total = sum((D(t["transaction_amount"]["amount"]) * (1 if t["credit_debit_indicator"] == "CRDT" else -1)
                     for t in txs), D(0))
        assert bals["OPBD"] + total == bals["CLBD"], code
        assert all(t["status"] == "BOOK" for t in txs)
        pend = json.loads((base / "transactions_pending.json").read_text(encoding="utf-8"))["transactions"]
        assert all(t["status"] == "PDNG" and t["booking_date"] is None for t in pend)
        assert bals["ITAV"] == bals["CLBD"] - sum((D(t["transaction_amount"]["amount"]) for t in pend), D(0))


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    assign_refs()
    compute_balances()
    check_mpp()
    check_matches()
    compute_ledger()
    compute_cash()
    check_documents()
    write_bank()
    verify_written_bank()
    write_camt()
    ksef_index = write_ksef()
    write_jpk()
    write_biuro_export()
    write_pm_sheet()
    write_email_pdfs()
    write_nbp()
    gt = build_ground_truth(ksef_index)
    assert gt["metrics_expected"]["cash_report_2026_10"]["control_difference"] == "0.00"
    write_json("ground_truth.json", gt)
    write_scenario(gt)
    n_sales = sum(1 for i in INV.values() if i.direction == S)
    print(f"OK: {len(WRITTEN)} plików w {OUT.relative_to(HERE.parent)}")
    print(f"  transakcje: {len(booked())} BOOK + {len(pending())} PDNG na {len(ACCOUNTS)} rachunkach")
    print(f"  faktury KSeF: {len(INV)} ({n_sales} sprzedaż, {len(INV) - n_sales} zakup); PDF: {len(EMAILS)}; "
          f"arkusz PM: {len(PM_ROWS)} wierszy; eventy: {len(EVENTS)}")
    for c, b in BALANCES.items():
        print(f"  {ACCOUNTS[c]['slug']}: {pl2(b['opening'])} + {pl2(b['credits'])} {pl2(b['debits'])} = "
              f"{pl2(b['closing'])}")
    print(f"  czcionka PDF: {FONT_INFO.get('file')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
