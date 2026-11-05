"""JPK files from the accounting office: JPK_V7M/V7K (VAT registers) and JPK_FA (invoices).

JPK_V7 gives the skeleton of history before KSeF: who, how much, when - one
``SprzedazWiersz`` / ``ZakupWiersz`` per document, no lines. JPK_FA gives the
sales invoices with lines. Each row becomes :class:`InvoiceFacts` with
``channel = 'jpk'`` and the locator of the row inside the file
(``sales:4``, ``purchase:17``, ``invoice:FV 1/2025``).

A register cannot tell an advance invoice from a VAT invoice, so its kind is
``kind_certain = False`` (deduplication then only separates corrections).
Rows that only self-assess VAT (WNT, import of services, reverse charge as the
buyer: K_23-K_32) are not our sales documents; they are reported as skipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from lxml import etree

from kasa.core.documents.facts import InvoiceFacts, InvoiceLine, Party, VatRateTotal, ksef_number_valid, money
from kasa.core.normalize import normalize_nip

PARSER_NAME = "jpk"
PARSER_VERSION = 1

# Sales register (JPK_V7): (net field, VAT field, rate label). K_12 and K_14 are
# "of which" subsets of K_11 and K_13 and are not added again.
SALES_FIELDS: list[tuple[str, str | None, str]] = [
    ("K_10", None, "zw"),
    ("K_11", None, "np I"),
    ("K_13", None, "0 KR"),
    ("K_15", "K_16", "5"),
    ("K_17", "K_18", "8"),
    ("K_19", "K_20", "23"),
    ("K_21", None, "0 WDT"),
    ("K_22", None, "0 EX"),
]
# Fields of self-assessed VAT (we are the buyer): WNT, import, reverse charge.
SELF_ASSESSMENT_FIELDS = {f"K_{n}" for n in range(23, 37)}
PURCHASE_FIELDS: list[tuple[str, str | None, str]] = [
    ("K_40", "K_41", "środki trwałe"),
    ("K_42", "K_43", "pozostałe"),
]
PURCHASE_VAT_ADJUSTMENTS = ("K_44", "K_45", "K_46", "K_47")
SALES_DOC_TYPES = {"RO": "receipt", "WEW": "internal", "FP": "vat"}
PURCHASE_DOC_TYPES = {"MK": "vat", "VAT_RR": "vat", "WEW": "internal"}


class JpkError(ValueError):
    """The file is not a JPK this importer reads."""


@dataclass
class JpkFile:
    form: str                      # JPK_V7M, JPK_V7K, JPK_FA
    subject_nip: str | None
    subject_name: str | None
    period: dict[str, Any]
    declaration: dict[str, str] = field(default_factory=dict)
    rows: list[tuple[str, InvoiceFacts]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _local(el: etree._Element) -> str:
    return etree.QName(el).localname


def _children(el: etree._Element) -> dict[str, str]:
    """Leaf children by local name (the last one wins for repeated names)."""
    return {_local(c): (c.text or "").strip() for c in el if isinstance(c.tag, str) and len(c) == 0}


def _find(el: etree._Element, *names: str) -> etree._Element | None:
    node: etree._Element | None = el
    for name in names:
        if node is None:
            return None
        node = next((c for c in node if isinstance(c.tag, str) and _local(c) == name), None)
    return node


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value[:10]) if value else None


def _party_from_register(country: str | None, number: str | None, name: str | None) -> Party:
    number = (number or "").strip()
    if not number or number.lower() == "brak":
        return Party(name=name or None, country=country or None)
    if (country or "PL") == "PL":
        try:
            return Party(nip=normalize_nip(number), name=name, country="PL")
        except ValueError:
            pass
    return Party(foreign_tax_id=f"{country or ''}{number}".upper().replace(" ", ""), name=name, country=country)


def parse_jpk(payload: bytes, *, company_nip: str | None = None, parser_version: int = PARSER_VERSION) -> JpkFile:
    try:
        root = etree.fromstring(payload, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError as exc:
        raise JpkError(f"niepoprawny XML: {exc}") from exc
    header = _find(root, "Naglowek")
    if _local(root) != "JPK" or header is None:
        raise JpkError("to nie jest plik JPK (brak JPK/Naglowek)")
    form_el = _find(header, "KodFormularza")
    system_code = (form_el.get("kodSystemowy") if form_el is not None else "") or ""
    if system_code:
        form = system_code.split(" ")[0]
    else:
        form = (form_el.text or "").strip() if form_el is not None else ""
    head = _children(header)
    subject = _find(root, "Podmiot1")
    ident = _find(subject, "OsobaNiefizyczna") if subject is not None else None
    ident = ident if ident is not None else (_find(subject, "OsobaFizyczna") if subject is not None else None)
    subject_fields = _children(ident) if ident is not None else {}
    if subject is not None and not subject_fields:
        nested = _find(subject, "IdentyfikatorPodmiotu")
        subject_fields = {**_children(subject), **(_children(nested) if nested is not None else {})}
    subject_nip = subject_fields.get("NIP")
    subject_nip = normalize_nip(subject_nip) if subject_nip else None
    subject_name = subject_fields.get("PelnaNazwa") or subject_fields.get("Nazwisko")
    if company_nip and subject_nip and company_nip != subject_nip:
        raise JpkError(f"plik JPK dotyczy NIP {subject_nip}, a firma ma NIP {company_nip}")
    our_nip = subject_nip or company_nip
    period = {k: head[k] for k in ("Rok", "Miesiac", "Kwartal", "DataOd", "DataDo") if k in head}
    result = JpkFile(form=form, subject_nip=subject_nip, subject_name=subject_name, period=period)
    meta = {"form": form, "system_code": system_code, "period": period,
            "generated_at": head.get("DataWytworzeniaJPK"), "software": head.get("NazwaSystemu")}
    if form.startswith("JPK_V7"):
        declaration = _find(root, "Deklaracja", "PozycjeSzczegolowe")
        if declaration is not None:
            result.declaration = _children(declaration)
        _parse_v7(root, result, our_nip, subject_name, meta, parser_version)
    elif form.startswith("JPK_FA"):
        _parse_fa(root, result, our_nip, subject_name, meta, parser_version)
    else:
        raise JpkError(f"nieobsługiwany rodzaj JPK: {system_code or form!r}")
    return result


def _amounts(cells: dict[str, str], fields: list[tuple[str, str | None, str]], currency: str = "PLN"
             ) -> tuple[list[VatRateTotal], int, int]:
    summary = []
    for net_field, vat_field, label in fields:
        net = money(cells.get(net_field), currency)
        vat = money(cells.get(vat_field), currency) if vat_field else None
        if net is None and vat is None:
            continue
        summary.append(VatRateTotal(rate=label, field=net_field, net_minor=net or 0, vat_minor=vat))
    net_total = sum(s.net_minor for s in summary)
    vat_total = sum(s.vat_minor or 0 for s in summary)
    return summary, net_total, vat_total


def _ksef(cells: dict[str, str], extra: dict[str, Any]) -> str | None:
    """JPK_V7(3) rows carry the KSeF number (or a marker OFF/BFK/DI)."""
    value = cells.get("NrKSeF")
    for marker in ("OFF", "BFK", "DI"):
        if cells.get(marker) == "1":
            extra["ksef_marker"] = marker
    if value and ksef_number_valid(value):
        return value
    if value:
        extra["NrKSeF"] = value
    return None


def _parse_v7(root: etree._Element, result: JpkFile, our_nip: str | None, our_name: str | None,
              meta: dict[str, Any], parser_version: int) -> None:
    ewidencja = _find(root, "Ewidencja")
    if ewidencja is None:
        return
    us = Party(nip=our_nip, name=our_name, country="PL")
    for row in ewidencja:
        if not isinstance(row.tag, str):
            continue
        name = _local(row)
        if name not in ("SprzedazWiersz", "ZakupWiersz"):
            continue
        cells = _children(row)
        sales = name == "SprzedazWiersz"
        lp = cells.get("LpSprzedazy" if sales else "LpZakupu")
        locator = f"{'sales' if sales else 'purchase'}:{lp}"
        extra: dict[str, Any] = {"jpk": meta}
        if sales:
            own = {k for k, _, _ in SALES_FIELDS} | {v for _, v, _ in SALES_FIELDS if v}
            if not (own & set(cells)) and (SELF_ASSESSMENT_FIELDS & set(cells)):
                result.skipped.append(f"{locator}: wiersz samonaliczenia VAT (nie jest naszą sprzedażą)")
                continue
            summary, net, vat = _amounts(cells, SALES_FIELDS)
            counterparty = _party_from_register(cells.get("KodKrajuNadaniaTIN"), cells.get("NrKontrahenta"),
                                                cells.get("NazwaKontrahenta"))
            number, issue = cells.get("DowodSprzedazy"), _date(cells.get("DataWystawienia"))
            sale = _date(cells.get("DataSprzedazy")) or issue
            doc_type = cells.get("TypDokumentu")
            kind = SALES_DOC_TYPES.get(doc_type or "", "vat")
            seller, buyer = us, counterparty
        else:
            summary, net, vat = _amounts(cells, PURCHASE_FIELDS)
            adjustments = {k: cells[k] for k in PURCHASE_VAT_ADJUSTMENTS if k in cells}
            if adjustments:
                extra["vat_adjustments"] = adjustments
            counterparty = _party_from_register(cells.get("KodKrajuNadaniaTIN"), cells.get("NrDostawcy"),
                                                cells.get("NazwaDostawcy"))
            number, issue = cells.get("DowodZakupu"), _date(cells.get("DataZakupu"))
            sale = None
            doc_type = cells.get("DokumentZakupu")
            kind = PURCHASE_DOC_TYPES.get(doc_type or "", "vat")
            seller, buyer = counterparty, us
            if cells.get("DataWplywu"):
                extra["received_date"] = cells["DataWplywu"]
        if not number or issue is None:
            result.skipped.append(f"{locator}: brak numeru dowodu lub daty")
            continue
        gross = net + vat
        if gross < 0 or net < 0:
            kind = "correction"
        mapped = {"LpSprzedazy", "LpZakupu", "KodKrajuNadaniaTIN", "NrKontrahenta", "NazwaKontrahenta",
                  "NrDostawcy", "NazwaDostawcy", "DowodSprzedazy", "DowodZakupu", "DataWystawienia",
                  "DataSprzedazy", "DataZakupu", "DataWplywu", "NrKSeF"}
        extra["row"] = {k: v for k, v in cells.items() if k not in mapped}
        ksef_number = _ksef(cells, extra)
        result.rows.append((locator, InvoiceFacts(
            channel="jpk",
            parser=f"{PARSER_NAME}:{result.form}",
            parser_version=parser_version,
            direction="sales" if sales else "purchase",
            kind=kind,
            kind_certain=False,
            ksef_number=ksef_number,
            number_raw=number,
            issue_date=issue,
            sale_date=sale,
            seller=seller,
            buyer=buyer,
            currency="PLN",
            net_minor=net,
            vat_minor=vat,
            gross_minor=gross,
            vat_summary=summary,
            mpp_required=cells.get("MPP") == "1",
            cash_method=doc_type == "MK",
            lines_sum_ok=None,
            checks={"amounts": "rejestr VAT: brutto = netto + VAT z ewidencji (bez pozycji)"},
            extra=extra,
        )))


FA_VAT_FIELDS: list[tuple[str, str | None, str]] = [
    ("P_13_1", "P_14_1", "23"), ("P_13_2", "P_14_2", "8"), ("P_13_3", "P_14_3", "5"),
    ("P_13_4", "P_14_4", "4"), ("P_13_5", "P_14_5", "oss"), ("P_13_6", None, "0"),
    ("P_13_7", None, "zw"),
]


def _parse_fa(root: etree._Element, result: JpkFile, our_nip: str | None, our_name: str | None,
              meta: dict[str, Any], parser_version: int) -> None:
    """JPK_FA(4): Faktura + FakturaWiersz (lines joined by P_2B = invoice number)."""
    lines_by_number: dict[str, list[dict[str, str]]] = {}
    invoices = []
    default_currency = "PLN"
    for el in root:
        if not isinstance(el.tag, str):
            continue
        if _local(el) == "Faktura":
            invoices.append(_children(el))
        elif _local(el) == "FakturaWiersz":
            cells = _children(el)
            lines_by_number.setdefault(cells.get("P_2B", ""), []).append(cells)
        elif _local(el) == "KodWaluty":
            default_currency = (el.text or "PLN").strip()
    for cells in invoices:
        number = cells.get("P_2A")
        issue = _date(cells.get("P_1"))
        locator = f"invoice:{number}"
        if not number or issue is None:
            result.skipped.append(f"{locator}: brak numeru lub daty")
            continue
        currency = cells.get("KodWaluty") or default_currency
        seller = _party_from_register(cells.get("P_4A") or "PL", cells.get("P_4B"), cells.get("P_3C"))
        buyer = _party_from_register(cells.get("P_5A") or "PL", cells.get("P_5B"), cells.get("P_3A"))
        if our_nip and buyer.nip == our_nip and seller.nip != our_nip:
            direction = "purchase"
        else:
            direction = "sales"
        summary, net, vat = _amounts(cells, FA_VAT_FIELDS, currency)
        gross = money(cells.get("P_15"), currency)
        rodzaj = (cells.get("RodzajFaktury") or "VAT").upper()
        kind = {"KOREKTA": "correction", "ZAL": "advance"}.get(rodzaj, "vat")
        lines = []
        for i, line in enumerate(lines_by_number.get(number, []), start=1):
            lines.append(InvoiceLine(
                line_no=i,
                name=line.get("P_7", ""),
                quantity=Decimal(line["P_8B"]) if line.get("P_8B") else None,
                unit=line.get("P_8A"),
                unit_price=Decimal(line["P_9A"]) if line.get("P_9A") else None,
                net_minor=money(line.get("P_11"), currency),
                gross_minor=money(line.get("P_11A"), currency),
                vat_rate=line.get("P_12"),
                extra={k: v for k, v in line.items() if k not in {"P_2B", "P_7", "P_8A", "P_8B", "P_9A", "P_11",
                                                                  "P_11A", "P_12"}},
            ))
        lines_net = sum(line.net_minor or 0 for line in lines)
        corrects = None
        if kind == "correction":
            corrects = {k: v for k, v in {"number": cells.get("NrFaKorygowanej"),
                                         "reason": cells.get("PrzyczynaKorekty"),
                                         "period": cells.get("OkresFaKorygowanej")}.items() if v}
        result.rows.append((locator, InvoiceFacts(
            channel="jpk",
            parser=f"{PARSER_NAME}:{result.form}",
            parser_version=parser_version,
            direction=direction,
            kind=kind,
            kind_certain=True,
            number_raw=number,
            issue_date=issue,
            sale_date=_date(cells.get("P_6")) or issue,
            seller=seller,
            buyer=buyer,
            currency=currency,
            net_minor=net,
            vat_minor=vat,
            gross_minor=gross if gross is not None else net + vat,
            vat_summary=summary,
            lines=lines,
            corrects_ref=corrects,
            advance_refs=[{"number": cells["NrFaZaliczkowej"]}] if cells.get("NrFaZaliczkowej") else [],
            mpp_required=cells.get("P_18A") in ("1", "true"),
            cash_method=cells.get("P_16") in ("1", "true"),
            reverse_charge=cells.get("P_18") in ("1", "true"),
            lines_sum_ok=(lines_net == net) if lines and kind != "advance" else None,
            extra={"jpk": meta, "row": {k: v for k, v in cells.items()
                                        if not k.startswith(("P_13", "P_14", "P_15"))}},
        )))
