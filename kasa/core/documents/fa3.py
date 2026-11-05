"""Parser of KSeF invoices in the FA(3) structure (namespace of 25.06.2025).

``parse_fa3(xml, company_nip=...)`` returns :class:`InvoiceFacts` with every
group of fields from the briefing (step 2): identity, dates (P_6 or OkresFa),
parties with third parties, amounts per VAT rate and in total, currency with
rate and PLN, lines (P_7 ... P_12; for a correction the before/after pair),
relations (DaneFaKorygowanej, FakturaZaliczkowa, order, contract) and flags
(P_16 cash method, P_18 reverse charge, P_18A split payment).

Nothing is dropped: every leaf element the parser does not map to a field lands
in ``facts.extra`` (document level) or ``line.extra`` (line level) under its path.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from lxml import etree

from kasa.core.documents.facts import (
    InvoiceFacts,
    InvoiceLine,
    Party,
    VatRateTotal,
    allocate,
    ksef_number_valid,
    money,
    money_rounded,
)
from kasa.core.normalize import from_minor, normalize_iban, normalize_nip

FA3_NAMESPACE = "http://crd.gov.pl/wzor/2025/06/25/13775/"
# FA(2) is close enough to read the same fields (KSeF 1.0 history).
KNOWN_NAMESPACES = {FA3_NAMESPACE: "FA (3)", "http://crd.gov.pl/wzor/2023/06/29/12648/": "FA (2)"}
PARSER_NAME = "fa3"
PARSER_VERSION = 1

KIND_BY_RODZAJ = {
    "VAT": "vat",
    "UPR": "vat",
    "KOR": "correction",
    "ZAL": "advance",
    "KOR_ZAL": "correction",
    "ROZ": "settlement",
    "KOR_ROZ": "correction",
}

# (net field, VAT field, VAT-in-PLN field, label, line rates P_12 that belong to the group)
VAT_GROUPS: list[tuple[str, str | None, str | None, str, frozenset[str]]] = [
    ("P_13_1", "P_14_1", "P_14_1W", "23", frozenset({"23", "22"})),
    ("P_13_2", "P_14_2", "P_14_2W", "8", frozenset({"8", "7"})),
    ("P_13_3", "P_14_3", "P_14_3W", "5", frozenset({"5"})),
    ("P_13_4", "P_14_4", "P_14_4W", "4", frozenset({"4", "3"})),
    ("P_13_5", "P_14_5", None, "oss", frozenset()),
    ("P_13_6_1", None, None, "0 KR", frozenset({"0 KR", "0"})),
    ("P_13_6_2", None, None, "0 WDT", frozenset({"0 WDT"})),
    ("P_13_6_3", None, None, "0 EX", frozenset({"0 EX"})),
    ("P_13_7", None, None, "zw", frozenset({"zw"})),
    ("P_13_8", None, None, "np I", frozenset({"np I", "np"})),
    ("P_13_9", None, None, "np II", frozenset({"np II"})),
    ("P_13_10", None, None, "oo", frozenset({"oo"})),
    ("P_13_11", None, None, "marza", frozenset()),
]
GROUP_BY_RATE = {rate: group[0] for group in VAT_GROUPS for rate in group[4]}

THIRD_PARTY_ROLES = {
    "1": "faktor",
    "2": "odbiorca",
    "3": "podmiot pierwotny",
    "4": "dodatkowy nabywca",
    "5": "wystawca faktury",
    "6": "dokonujący płatności",
    "7": "JST - wystawca",
    "8": "JST - odbiorca",
    "9": "członek grupy VAT - wystawca",
    "10": "członek grupy VAT - odbiorca",
    "11": "pracownik",
}
AUTHORIZED_ROLES = {"1": "organ egzekucyjny", "2": "komornik sądowy", "3": "przedstawiciel podatkowy"}
# Roles in which a third party named on the invoice is effectively the buyer.
BUYER_SIDE_ROLES = {"2", "4", "8", "10"}
PAYMENT_FORMS = {
    "1": "gotówka", "2": "karta", "3": "bon", "4": "czek", "5": "kredyt", "6": "przelew", "7": "mobilna",
}


class Fa3Error(ValueError):
    """The XML is not a readable FA invoice, or it does not concern the company."""


class _Reader:
    """Namespace-aware access to the tree that remembers which leaves were read."""

    def __init__(self, root: etree._Element):
        self.root = root
        self.ns = etree.QName(root).namespace or ""
        self.consumed: set[etree._Element] = set()

    def q(self, name: str) -> str:
        return f"{{{self.ns}}}{name}" if self.ns else name

    def find(self, el: etree._Element | None, path: str) -> etree._Element | None:
        if el is None:
            return None
        node = el
        for part in path.split("/"):
            node = node.find(self.q(part))
            if node is None:
                return None
        return node

    def findall(self, el: etree._Element | None, name: str) -> list[etree._Element]:
        return [] if el is None else el.findall(self.q(name))

    def text(self, el: etree._Element | None, path: str) -> str | None:
        node = self.find(el, path)
        if node is None:
            return None
        self.consumed.add(node)
        value = (node.text or "").strip()
        return value or None

    def texts(self, el: etree._Element | None, name: str) -> list[str]:
        values = []
        for node in self.findall(el, name):
            self.consumed.add(node)
            if (node.text or "").strip():
                values.append(node.text.strip())
        return values

    def decimal(self, el: etree._Element | None, path: str) -> Decimal | None:
        value = self.text(el, path)
        if value is None:
            return None
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise Fa3Error(f"{path}: to nie jest liczba: {value!r}") from exc

    def date(self, el: etree._Element | None, path: str) -> date | None:
        value = self.text(el, path)
        if value is None:
            return None
        try:
            return date.fromisoformat(value[:10])
        except ValueError as exc:
            raise Fa3Error(f"{path}: to nie jest data: {value!r}") from exc

    def flag(self, el: etree._Element | None, path: str) -> bool:
        """FA(3) flags: 1 = yes, 2 = no."""
        return self.text(el, path) == "1"

    def unconsumed(self, el: etree._Element, *, mark: bool = True) -> dict[str, Any]:
        """Leaf elements under ``el`` not read by the parser, keyed by path relative to ``el``."""
        found: dict[str, list[Any]] = defaultdict(list)
        for node in el.iter():
            if not isinstance(node.tag, str) or node is el or len(node) or node in self.consumed:
                continue
            parts = [etree.QName(node).localname]
            parent = node.getparent()
            while parent is not None and parent is not el:
                parts.append(etree.QName(parent).localname)
                parent = parent.getparent()
            value: Any = (node.text or "").strip()
            if node.attrib:
                value = {"value": value, **{etree.QName(k).localname: v for k, v in node.attrib.items()}}
            found["/".join(reversed(parts))].append(value)
            if mark:
                self.consumed.add(node)
        return {path: values[0] if len(values) == 1 else values for path, values in found.items()}


def _nip(value: str | None, issues: list[str], where: str) -> str | None:
    if value is None:
        return None
    try:
        return normalize_nip(value)
    except ValueError:
        issues.append(f"{where}: NIP {value!r} ma niepoprawną sumę kontrolną")
        return normalize_nip(value, validate=False)


def _party(r: _Reader, el: etree._Element | None, issues: list[str], where: str) -> Party:
    if el is None:
        return Party()
    ident = r.find(el, "DaneIdentyfikacyjne")
    nip = _nip(r.text(ident, "NIP"), issues, where)
    foreign = None
    country = None
    eu_code, eu_vat = r.text(ident, "KodUE"), r.text(ident, "NrVatUE")
    if eu_vat:
        foreign, country = f"{eu_code or ''}{eu_vat}".upper(), eu_code
    other_country, other_id = r.text(ident, "KodKraju"), r.text(ident, "NrID")
    if other_id:
        foreign, country = f"{other_country or ''}{other_id}".upper().replace(" ", ""), other_country
    internal_id = r.text(ident, "IDWew")
    extra: dict[str, Any] = {}
    if internal_id:
        extra["IDWew"] = internal_id
    if r.text(ident, "BrakID") == "1":
        extra["BrakID"] = True
    address_el = r.find(el, "Adres")
    address_country = r.text(address_el, "KodKraju")
    address_lines = [line for line in (r.text(address_el, "AdresL1"), r.text(address_el, "AdresL2")) if line]
    contact = r.find(el, "DaneKontaktowe")
    emails = r.texts(contact, "Email")
    phones = r.texts(contact, "Telefon")
    if len(emails) > 1:
        extra["emails"] = emails
    if len(phones) > 1:
        extra["phones"] = phones
    prefix = r.text(el, "PrefiksPodatnika")
    if prefix:
        extra["PrefiksPodatnika"] = prefix
    role_code = r.text(el, "Rola") or r.text(el, "RolaPU")
    role_label = None
    if role_code:
        roles = AUTHORIZED_ROLES if etree.QName(el).localname == "PodmiotUpowazniony" else THIRD_PARTY_ROLES
        role_label = roles.get(role_code)
    if r.text(el, "RolaInna") == "1":
        role_code, role_label = "inna", r.text(el, "OpisRoli")
    share = r.decimal(el, "Udzial")
    return Party(
        nip=nip,
        foreign_tax_id=foreign,
        country=country or address_country or ("PL" if nip else None),
        name=r.text(ident, "Nazwa"),
        address=", ".join(address_lines) or None,
        email=emails[0] if emails else None,
        phone=phones[0] if phones else None,
        role=role_code,
        role_label=role_label,
        share=share,
        extra=extra,
    )


def _iban(value: str | None, issues: list[str]) -> str | None:
    if value is None:
        return None
    try:
        return normalize_iban(value)
    except ValueError:
        issues.append(f"rachunek {value!r} nie jest poprawnym IBAN/NRB")
        return normalize_iban(value, validate=False)


def _accounts(r: _Reader, platnosc: etree._Element | None, issues: list[str]) -> list[dict[str, Any]]:
    accounts = []
    for tag, role in (("RachunekBankowyFaktora", "factor"), ("RachunekBankowy", "seller")):
        for el in r.findall(platnosc, tag):
            account = {
                "role": role,
                "iban": _iban(r.text(el, "NrRB"), issues),
                "swift": r.text(el, "SWIFT"),
                "bank_own_account": r.text(el, "RachunekWlasnyBanku"),
                "bank": r.text(el, "NazwaBanku"),
                "description": r.text(el, "OpisRachunku"),
            }
            accounts.append({k: v for k, v in account.items() if v is not None})
    return accounts


def _payment(r: _Reader, fa: etree._Element, issues: list[str]) -> tuple[dict[str, Any], date | None, str | None]:
    platnosc = r.find(fa, "Platnosc")
    if platnosc is None:
        return {}, None, None
    terms = []
    for term in r.findall(platnosc, "TerminPlatnosci"):
        entry = {"date": r.text(term, "Termin")}
        opis = r.find(term, "TerminOpis")
        if opis is not None:
            entry["description"] = {
                "quantity": r.text(opis, "Ilosc"),
                "unit": r.text(opis, "Jednostka"),
                "from_event": r.text(opis, "ZdarzeniePoczatkowe"),
            }
        terms.append(entry)
    partial = [
        {
            "amount": r.text(el, "KwotaZaplatyCzesciowej"),
            "date": r.text(el, "DataZaplatyCzesciowej"),
            "form": r.text(el, "FormaPlatnosci"),
        }
        for el in r.findall(platnosc, "ZaplataCzesciowa")
    ]
    form = r.text(platnosc, "FormaPlatnosci")
    accounts = _accounts(r, platnosc, issues)
    payment: dict[str, Any] = {
        "form": form,
        "form_label": PAYMENT_FORMS.get(form or ""),
        "other_form": r.text(platnosc, "OpisPlatnosci") if r.text(platnosc, "PlatnoscInna") == "1" else None,
        "paid": r.text(platnosc, "Zaplacono") == "1",
        "paid_date": r.text(platnosc, "DataZaplaty"),
        "partial_payment": r.text(platnosc, "ZnacznikZaplatyCzesciowej"),
        "partial_payments": partial,
        "terms": terms,
        "accounts": accounts,
        "link": r.text(platnosc, "LinkDoPlatnosci"),
        "ip_ksef": r.text(platnosc, "IPKSeF"),
    }
    skonto = r.find(platnosc, "Skonto")
    if skonto is not None:
        payment["discount"] = {
            "conditions": r.text(skonto, "WarunkiSkonta"),
            "amount": r.text(skonto, "WysokoscSkonta"),
        }
    payment = {k: v for k, v in payment.items() if v not in (None, [], {}, False)}
    due_dates = sorted(date.fromisoformat(t["date"]) for t in terms if t.get("date"))
    # The factor's account is where the money must go, when the invoice names one.
    factor = next((a["iban"] for a in accounts if a["role"] == "factor" and a["iban"]), None)
    seller = next((a["iban"] for a in accounts if a["role"] == "seller" and a["iban"]), None)
    return payment, (due_dates[0] if due_dates else None), factor or seller


def _line(r: _Reader, el: etree._Element, currency: str, *, order: bool) -> tuple[InvoiceLine, Decimal | None, bool]:
    """One FaWiersz (or ZamowienieWiersz when ``order``). Returns (line, fx rate, is 'before' state)."""
    z = "Z" if order else ""
    line_no = int(r.text(el, "NrWierszaZam" if order else "NrWierszaFa") or 0)
    rate = r.text(el, f"P_12{z}")
    oss_rate = r.text(el, f"P_12{z}_XII")
    unit_price = r.decimal(el, f"P_9A{z}")
    gross_unit_price = None if order else r.decimal(el, "P_9B")
    net = money(r.text(el, "P_11NettoZ" if order else "P_11"), currency)
    gross = None if order else money(r.text(el, "P_11A"), currency)
    vat = money(r.text(el, "P_11VatZ" if order else "P_11Vat"), currency)
    fx = r.decimal(el, "KursWaluty")
    before = r.text(el, f"StanPrzed{z}") == "1"
    price = unit_price if unit_price is not None else gross_unit_price
    line = InvoiceLine(
        line_no=line_no,
        name=r.text(el, f"P_7{z}") or "",
        quantity=r.decimal(el, f"P_8B{z}"),
        unit=r.text(el, f"P_8A{z}"),
        unit_price=price,
        unit_price_minor=money_rounded(price, currency) if price is not None else None,
        net_minor=net,
        vat_rate=rate or (f"OSS {oss_rate}" if oss_rate else None),
        vat_minor=vat,
        gross_minor=gross,
        line_kind="order" if order else "invoice",
        extra={},
    )
    if gross_unit_price is not None and unit_price is None:
        line.extra["price_is_gross"] = True
    if fx is not None:
        line.extra["KursWaluty"] = str(fx)
    line.extra.update(r.unconsumed(el))
    return line, fx, before


def _line_group(line: InvoiceLine) -> str | None:
    if line.vat_rate is None:
        return None
    if line.vat_rate.startswith("OSS"):
        return "P_13_5"
    return GROUP_BY_RATE.get(line.vat_rate)


def _pair_correction(lines: list[tuple[InvoiceLine, bool]]) -> list[InvoiceLine]:
    """Correction: rows with StanPrzed=1 are the state before, rows without it the state after.

    One line per NrWierszaFa carries the delta (after - before) in its amounts,
    so the lines of a correction add up to its P_13/P_15 (which are differences).
    """
    before: dict[int, InvoiceLine] = {}
    after: dict[int, InvoiceLine] = {}
    order: list[int] = []
    for line, is_before in lines:
        (before if is_before else after)[line.line_no] = line
        if line.line_no not in order:
            order.append(line.line_no)

    def state(line: InvoiceLine | None) -> dict[str, Any] | None:
        if line is None:
            return None
        return line.model_dump(mode="json", exclude={"before", "after", "line_kind"}, exclude_none=True)

    def diff(a: int | None, b: int | None) -> int | None:
        if a is None and b is None:
            return None
        return (a or 0) - (b or 0)

    result = []
    for no in order:
        new, old = after.get(no), before.get(no)
        base = new or old
        result.append(InvoiceLine(
            line_no=no,
            name=base.name,
            quantity=new.quantity if new else None,
            unit=base.unit,
            unit_price=new.unit_price if new else None,
            unit_price_minor=new.unit_price_minor if new else None,
            net_minor=diff(new.net_minor if new else None, old.net_minor if old else None),
            vat_rate=base.vat_rate,
            vat_minor=diff(new.vat_minor if new else None, old.vat_minor if old else None),
            gross_minor=diff(new.gross_minor if new else None, old.gross_minor if old else None),
            line_kind="correction",
            before=state(old),
            after=state(new),
            extra=base.extra,
        ))
    return result


def _fill_line_vat(lines: list[InvoiceLine], summary: list[VatRateTotal]) -> None:
    """Lines carry no VAT amount in FA(3): give each line its share of the rate's P_14_x."""
    by_group: dict[str, list[InvoiceLine]] = defaultdict(list)
    for line in lines:
        group = _line_group(line)
        if group:
            by_group[group].append(line)
    totals = {s.field: s for s in summary}
    for group, members in by_group.items():
        total = totals.get(group)
        if any(m.vat_minor is not None for m in members):
            for m in members:
                if m.gross_minor is None and m.net_minor is not None and m.vat_minor is not None:
                    m.gross_minor = m.net_minor + m.vat_minor
            continue
        if total is None or total.vat_minor is None:
            vat_parts = [0 if m.net_minor is not None else None for m in members]
        else:
            vat_parts = allocate(total.vat_minor, [m.net_minor or 0 for m in members]) or [None] * len(members)
            for m in members:
                m.extra.setdefault("vat_minor_source", "allocated_from_P_14")
        for m, vat in zip(members, vat_parts, strict=True):
            if vat is None:
                continue
            if m.net_minor is not None:
                m.vat_minor = vat
                m.gross_minor = m.gross_minor if m.gross_minor is not None else m.net_minor + vat


def _vat_summary(r: _Reader, fa: etree._Element, currency: str, lines: list[InvoiceLine]) -> list[VatRateTotal]:
    summary = []
    for net_field, vat_field, vat_pln_field, label, _rates in VAT_GROUPS:
        net = money(r.text(fa, net_field), currency)
        if net is None:
            continue
        rates = {line.vat_rate for line in lines if _line_group(line) == net_field and line.vat_rate}
        summary.append(VatRateTotal(
            rate=rates.pop() if len(rates) == 1 else label,
            field=net_field,
            net_minor=net,
            vat_minor=money(r.text(fa, vat_field), currency) if vat_field else None,
            vat_pln_minor=money(r.text(fa, vat_pln_field), "PLN") if vat_pln_field else None,
        ))
    return summary


def _lines_check(kind: str, lines: list[InvoiceLine], summary: list[VatRateTotal],
                 order_value: int | None) -> tuple[bool | None, dict[str, Any]]:
    """Do the lines add up to the printed totals? Returns (ok, details)."""
    details: dict[str, Any] = {}
    if kind == "advance":
        order_lines = [l for l in lines if l.line_kind == "order"]
        if order_value is None or not order_lines:
            return None, {"reason": "zaliczkowa bez wartości zamówienia lub pozycji zamówienia"}
        total = sum((l.net_minor or 0) + (l.vat_minor or 0) for l in order_lines)
        details["order_lines_gross"] = total
        details["order_value"] = order_value
        return total == order_value, details
    if not any(l.line_kind != "order" for l in lines):
        return False, {"reason": "brak pozycji"}
    by_group: dict[str, int] = defaultdict(int)
    gross_only: dict[str, int] = defaultdict(int)
    for line in lines:
        if line.line_kind == "order":
            continue
        group = _line_group(line) or "?"
        if line.net_minor is not None:
            by_group[group] += line.net_minor
        elif line.gross_minor is not None:
            # Invoice computed from gross prices (P_11A): compare with net + VAT of the rate.
            gross_only[group] += line.gross_minor
    printed = {s.field: s for s in summary}
    mismatches = {}
    for group in set(by_group) | set(gross_only) | set(printed):
        expected = printed.get(group)
        if group in gross_only:
            got = gross_only[group] + by_group.get(group, 0)
            want = expected.net_minor + (expected.vat_minor or 0) if expected else None
        else:
            got = by_group.get(group, 0)
            want = expected.net_minor if expected else None
        if want is None or got != want:
            mismatches[group] = {"lines": got, "printed": want}
    details["per_rate_lines_net"] = dict(by_group)
    if kind == "settlement":
        # Lines of a settlement invoice are the whole order; P_13 is what is left after advances.
        lines_net = sum(by_group.values())
        printed_net = sum(s.net_minor for s in summary)
        details["advance_net_deducted"] = lines_net - printed_net
        return None, details
    if mismatches:
        details["mismatches"] = mismatches
    return not mismatches, details


def parse_fa3(
    xml: bytes,
    *,
    company_nip: str | None,
    ksef_number: str | None = None,
    accepted_at: datetime | None = None,
    direction_hint: str | None = None,
    parser_version: int = PARSER_VERSION,
) -> InvoiceFacts:
    """Read one FA(3) invoice. ``company_nip`` decides the direction (Podmiot1 = us -> sales)."""
    try:
        root = etree.fromstring(xml, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError as exc:
        raise Fa3Error(f"niepoprawny XML: {exc}") from exc
    r = _Reader(root)
    if etree.QName(root).localname != "Faktura":
        raise Fa3Error(f"to nie jest faktura FA (korzeń {etree.QName(root).localname!r})")
    issues: list[str] = []
    extra: dict[str, Any] = {}

    header = r.find(root, "Naglowek")
    form = r.find(header, "KodFormularza")
    form_code = None
    if form is not None:
        r.consumed.add(form)
        form_code = form.get("kodSystemowy")
        extra["form"] = {"code": form_code, "schema_version": form.get("wersjaSchemy"), "value": (form.text or "").strip()}
    if r.ns not in KNOWN_NAMESPACES:
        issues.append(f"nieznana przestrzeń nazw {r.ns!r} - odczyt jak FA(3)")
    elif form_code and form_code != KNOWN_NAMESPACES[r.ns]:
        issues.append(f"kodSystemowy {form_code!r} nie pasuje do przestrzeni nazw")
    extra["form_variant"] = r.text(header, "WariantFormularza")
    extra["created_at"] = r.text(header, "DataWytworzeniaFa")
    issuer_software = r.text(header, "SystemInfo")

    seller = _party(r, r.find(root, "Podmiot1"), issues, "Podmiot1")
    buyer = _party(r, r.find(root, "Podmiot2"), issues, "Podmiot2")
    third = [_party(r, el, issues, "Podmiot3") for el in r.findall(root, "Podmiot3")]
    authorized = r.find(root, "PodmiotUpowazniony")
    if authorized is not None:
        party = _party(r, authorized, issues, "PodmiotUpowazniony")
        party.extra["authorized_entity"] = True
        third.append(party)

    fa = r.find(root, "Fa")
    if fa is None:
        raise Fa3Error("brak elementu Fa")
    currency = r.text(fa, "KodWaluty") or "PLN"
    rodzaj = r.text(fa, "RodzajFaktury") or "VAT"
    kind = KIND_BY_RODZAJ.get(rodzaj)
    if kind is None:
        raise Fa3Error(f"nieznany RodzajFaktury {rodzaj!r}")
    extra["RodzajFaktury"] = rodzaj

    number_raw = r.text(fa, "P_2")
    issue_date = r.date(fa, "P_1")
    if not number_raw or issue_date is None:
        raise Fa3Error("brak numeru (P_2) albo daty wystawienia (P_1)")
    period_start = r.date(fa, "OkresFa/P_6_Od")
    period_end = r.date(fa, "OkresFa/P_6_Do")
    sale_date = r.date(fa, "P_6")
    if sale_date is None and period_start is None and kind != "correction":
        # FA(3) omits P_6 when the sale date equals the issue date. A correction without
        # P_6 has no sale date of its own: it follows the corrected invoice.
        sale_date = issue_date

    # lines: FaWiersz, and for an advance invoice the order lines (Zamowienie/ZamowienieWiersz)
    raw_lines = [_line(r, el, currency, order=False) for el in r.findall(fa, "FaWiersz")]
    zamowienie = r.find(fa, "Zamowienie")
    order_value = money(r.text(zamowienie, "WartoscZamowienia"), currency)
    order_lines = [_line(r, el, currency, order=True) for el in r.findall(zamowienie, "ZamowienieWiersz")]
    if kind == "correction" or any(before for _, _, before in raw_lines):
        lines = _pair_correction([(line, before) for line, _, before in raw_lines])
    else:
        lines = [line for line, _, _ in raw_lines]
    lines.extend(line for line, _, _ in order_lines)
    line_rates = {fx for _, fx, before in raw_lines if fx is not None and not before}
    line_rates |= {fx for _, fx, _ in order_lines if fx is not None}

    summary = _vat_summary(r, fa, currency, [l for l in lines if l.line_kind != "order"])
    invoice_lines = [l for l in lines if l.line_kind != "order"]
    _fill_line_vat(invoice_lines, summary)
    order_summary = _order_summary(order_lines)
    if order_summary:
        _fill_line_vat([l for l in lines if l.line_kind == "order"], [])
    gross = money(r.text(fa, "P_15"), currency)
    if gross is None:
        raise Fa3Error("brak kwoty należności ogółem (P_15)")
    net_total = sum(s.net_minor for s in summary) if summary else None
    vat_total = sum(s.vat_minor or 0 for s in summary) if summary else None

    # currency
    fx_rate = None
    fx_source = None
    rate_z = r.decimal(fa, "KursWalutyZ")
    if currency != "PLN":
        if len(line_rates) == 1:
            fx_rate, fx_source = line_rates.pop(), "document"
        elif rate_z is not None:
            fx_rate, fx_source = rate_z, "document"
        elif line_rates:
            issues.append("różne kursy w pozycjach - kwota PLN liczona z pozycji")
    elif rate_z is not None:
        extra["KursWalutyZ"] = str(rate_z)
    gross_pln = None
    if currency != "PLN" and fx_rate is None and line_rates:
        gross_pln = sum(
            money_rounded(from_minor(line.net_minor or 0, currency) * Decimal(line.extra["KursWaluty"]), "PLN")
            for line in invoice_lines if line.extra.get("KursWaluty")
        ) + sum(s.vat_pln_minor or 0 for s in summary)

    payment, due_date, payment_iban = _payment(r, fa, issues)

    # relations
    corrections = []
    for el in r.findall(fa, "DaneFaKorygowanej"):
        corrections.append({
            "number": r.text(el, "NrFaKorygowanej"),
            "issue_date": r.text(el, "DataWystFaKorygowanej"),
            "ksef_number": r.text(el, "NrKSeFFaKorygowanej") if r.text(el, "NrKSeF") == "1" else None,
            "outside_ksef": r.text(el, "NrKSeFN") == "1",
        })
    corrects_ref = None
    if corrections or kind == "correction":
        corrects_ref = dict(corrections[0]) if corrections else {}
        if len(corrections) > 1:
            corrects_ref["all"] = corrections
        for key, tag in (("reason", "PrzyczynaKorekty"), ("type", "TypKorekty"),
                         ("corrected_own_number", "NrFaKorygowany"), ("period", "OkresFaKorygowanej")):
            value = r.text(fa, tag)
            if value:
                corrects_ref[key] = value
        corrects_ref = {k: v for k, v in corrects_ref.items() if v not in (None, False)}
    advance_refs = []
    for el in r.findall(fa, "FakturaZaliczkowa"):
        ksef = r.text(el, "NrKSeFFaZaliczkowej")
        number = r.text(el, "NrFaZaliczkowej")
        advance_refs.append({k: v for k, v in {
            "ksef_number": ksef, "number": number, "outside_ksef": r.text(el, "NrKSeFZN") == "1"
        }.items() if v})
    warunki = r.find(fa, "WarunkiTransakcji")
    orders = [{"number": r.text(el, "NrZamowienia"), "date": r.text(el, "DataZamowienia")}
              for el in r.findall(warunki, "Zamowienia")]
    contracts = [{"number": r.text(el, "NrUmowy"), "date": r.text(el, "DataUmowy")}
                 for el in r.findall(warunki, "Umowy")]
    if orders:
        extra["orders"] = orders
    if contracts:
        extra["contracts"] = contracts

    adnotacje = r.find(fa, "Adnotacje")
    cash_method = r.flag(adnotacje, "P_16")
    self_billing = r.flag(adnotacje, "P_17")
    reverse_charge = r.flag(adnotacje, "P_18")
    mpp = r.flag(adnotacje, "P_18A")
    if self_billing:
        extra["self_billing"] = True

    issue_place = r.text(fa, "P_1M")

    direction = _direction(company_nip, seller, buyer, third, direction_hint)

    lines_ok, line_details = _lines_check(kind, lines, summary, order_value)
    checks: dict[str, Any] = {"lines": line_details}
    if summary:
        per_rate_gross = sum(s.net_minor + (s.vat_minor or 0) for s in summary)
        checks["per_rate_sum_equals_P_15"] = per_rate_gross == gross
        if per_rate_gross != gross:
            issues.append(f"suma stawek {per_rate_gross} różni się od P_15 {gross}")
    if order_summary:
        checks["order_per_rate"] = order_summary
    if ksef_number and not ksef_number_valid(ksef_number):
        issues.append(f"numer KSeF {ksef_number} ma niepoprawną sumę kontrolną")
    if issues:
        checks["issues"] = issues

    # everything not read above (Adnotacje details, Stopka, DodatkowyOpis, JST/GV, ...)
    extra.update({k: v for k, v in r.unconsumed(root).items()})
    extra = {k: v for k, v in extra.items() if v not in (None, "", [], {})}

    return InvoiceFacts(
        channel="ksef",
        parser=PARSER_NAME,
        parser_version=parser_version,
        direction=direction,
        kind=kind,
        ksef_number=ksef_number,
        number_raw=number_raw,
        file_hash=hashlib.sha256(xml).hexdigest(),
        issuer_software=issuer_software,
        issue_date=issue_date,
        issue_place=issue_place,
        sale_date=sale_date,
        service_period_start=period_start,
        service_period_end=period_end,
        ksef_accepted_at=accepted_at,
        due_date=due_date,
        seller=seller,
        buyer=buyer,
        third_parties=third,
        payment_iban=payment_iban,
        payment=payment,
        currency=currency,
        net_minor=net_total,
        vat_minor=vat_total,
        gross_minor=gross,
        fx_rate=fx_rate,
        fx_rate_source=fx_source,
        gross_pln_minor=gross_pln,
        vat_summary=summary,
        lines=lines,
        corrects_ref=corrects_ref,
        advance_refs=advance_refs,
        order_number=orders[0]["number"] if orders else None,
        contract_ref=contracts[0]["number"] if contracts else None,
        mpp_required=mpp,
        cash_method=cash_method,
        reverse_charge=reverse_charge,
        lines_sum_ok=lines_ok,
        checks=checks,
        extra=extra,
    )


def _order_summary(order_lines: list[tuple[InvoiceLine, Decimal | None, bool]]) -> dict[str, Any]:
    per_rate: dict[str, dict[str, int]] = {}
    for line, _, _ in order_lines:
        bucket = per_rate.setdefault(line.vat_rate or "?", {"net_minor": 0, "vat_minor": 0})
        bucket["net_minor"] += line.net_minor or 0
        bucket["vat_minor"] += line.vat_minor or 0
    return per_rate


def _direction(company_nip: str | None, seller: Party, buyer: Party, third: list[Party],
               hint: str | None) -> str:
    if company_nip:
        if seller.nip == company_nip:
            return "sales"
        if buyer.nip == company_nip:
            return "purchase"
        if any(p.nip == company_nip and p.role in BUYER_SIDE_ROLES for p in third):
            return "purchase"
        if any(p.nip == company_nip for p in third):
            return "sales"
        raise Fa3Error(
            f"faktura nie dotyczy firmy NIP {company_nip}: sprzedawca {seller.nip}, nabywca {buyer.tax_key}"
        )
    if hint in ("sales", "purchase"):
        return hint
    raise Fa3Error("nie znam NIP-u firmy - nie da się ustalić kierunku faktury")
