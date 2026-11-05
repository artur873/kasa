from __future__ import annotations

import os
import textwrap
import uuid
from datetime import date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from kasa.core.extension import (
    EXTENSIONS_PATH_ENV,
    CandidateRecord,
    Extension,
    ExtensionError,
    MappingError,
    discover_extensions,
    extension_search_paths,
    get_extension,
    load_extension,
)

MINIMAL = """\
slug: {slug}
name: Firma {slug}
cost_object:
  kind: {kind}
  label: Obiekt
  name_template: "{{code}}"
  attrs:
    code: {{type: text, required: true}}
"""


def write_extension(base: Path, slug: str, text: str | None = None, hooks: str | None = None,
                    kind: str = "project") -> Path:
    directory = base / slug
    directory.mkdir(parents=True)
    (directory / "extension.yaml").write_text(text or MINIMAL.format(slug=slug, kind=kind), encoding="utf-8")
    if hooks is not None:
        (directory / "hooks.py").write_text(textwrap.dedent(hooks), encoding="utf-8")
    return directory


# ---------------------------------------------------------------------------
# Built-in extensions
# ---------------------------------------------------------------------------


def test_builtin_extensions_load():
    registry = discover_extensions()
    assert set(registry) == {"odm", "rentease"}
    assert registry["odm"].cost_object_kind == "event"
    assert registry["rentease"].cost_object_kind == "room_month"
    for extension in registry.values():
        assert isinstance(extension, Extension)
        assert extension.categories
        assert {"zdrowie", "związki", "komornik", "alimenty"} <= set(extension.sensitive_words)


def test_odm_event_schema():
    odm = get_extension("odm")
    attrs = odm.validate_cost_object_attrs(
        {"name": "Gala ACME", "client": "ACME", "date_start": "2026-06-12", "date_end": "2026-06-12", "city": "Kraków"}
    )
    assert attrs == {"name": "Gala ACME", "client": "ACME", "date_start": "2026-06-12",
                     "date_end": "2026-06-12", "city": "Kraków"}
    assert odm.cost_object_name(attrs) == "Gala ACME"
    # city is optional
    assert "city" not in odm.validate_cost_object_attrs(
        {"name": "Targi", "client": "ACME", "date_start": "2026-06-11", "date_end": "2026-06-15"}
    )
    with pytest.raises(ValidationError, match="Koniec eventu"):  # hooks.py
        odm.validate_cost_object_attrs(
            {"name": "Gala", "client": "ACME", "date_start": "2026-06-12", "date_end": "2026-06-10"}
        )
    with pytest.raises(ValidationError):
        odm.validate_cost_object_attrs({"name": "Gala", "client": "ACME", "date_start": "2026-06-12"})


def test_odm_pm_sheet_mapping():
    mapping = get_extension("odm").import_mapping("pm_sheet")
    assert mapping.target == "internal_record"
    row = mapping.map_row(
        {"Osoba": "Kowalska", "Data": "12.03.2026", "EVENT": "Allegro", "Kwota": "450", "Typ rozliczenia": None}
    )
    assert row.core == {
        "person": "Kowalska",
        "record_date": date(2026, 3, 12),
        "cost_object_ref": "Allegro",
        "amount": 45000,
        "settlement_type": None,
        "currency": "PLN",
    }
    assert row.extra == {}
    # Spreadsheet cells: datetimes and floats.
    row = mapping.map_row({"osoba": "Nowak", "data": datetime(2026, 3, 1, 0, 0), "event": "Gala",
                           "kwota": 1234.5, "typ_rozliczenia": "UZ"})
    assert row.core["record_date"] == date(2026, 3, 1)
    assert row.core["amount"] == 123450
    assert row.core["settlement_type"] == "UZ"
    with pytest.raises(MappingError, match="kwota"):
        mapping.map_row({"osoba": "Nowak", "data": "2026-03-01", "event": "Gala", "kwota": ""})
    with pytest.raises(MappingError, match="kwota"):
        mapping.map_row({"osoba": "Nowak", "data": "2026-03-01", "event": "Gala", "kwota": "dużo"})
    with pytest.raises(MappingError, match="data"):
        mapping.map_row({"osoba": "Nowak", "data": "1 marca", "event": "Gala", "kwota": "1"})


def test_rentease_room_month():
    rentease = get_extension("rentease")
    attrs = rentease.validate_cost_object_attrs({"room": "P3", "month": "2026-11"})
    assert rentease.cost_object_name(attrs) == "P3 / 2026-11"
    with pytest.raises(ValidationError):
        rentease.validate_cost_object_attrs({"room": "P3", "month": "2026-13"})
    row = rentease.import_mapping("rent_roll").map_row(
        {"Pokój": "P3", "Najemca": "Nowak", "Miesiąc": "11.2026", "Kwota": "2 300,00", "IBAN": "LT12..."}
    )
    assert row.core == {"cost_object_ref": "P3", "person": "Nowak", "amount": 230000, "currency": "PLN"}
    assert row.extra == {"month": "2026-11", "tenant_iban": "LT12..."}


def test_yaml_only_extension_proposes_no_candidates():
    assert get_extension("rentease").derive_cost_object_candidates([]) == []


def test_extension_base_is_abstract():
    with pytest.raises(TypeError):
        Extension()


# ---------------------------------------------------------------------------
# Loading from KASA_EXTENSIONS_PATH and validation of YAML
# ---------------------------------------------------------------------------


def test_extensions_path_env_adds_directories(tmp_path, monkeypatch):
    write_extension(tmp_path / "a", "alpha")
    write_extension(tmp_path / "b", "beta", kind="vehicle_trip")
    monkeypatch.setenv(EXTENSIONS_PATH_ENV, os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")]))
    assert extension_search_paths()[1:] == [tmp_path / "a", tmp_path / "b"]
    registry = discover_extensions()
    assert {"odm", "rentease", "alpha", "beta"} == set(registry)
    assert registry["beta"].cost_object_kind == "vehicle_trip"


def test_missing_extensions_directory_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv(EXTENSIONS_PATH_ENV, str(tmp_path / "nope"))
    with pytest.raises(ExtensionError, match="nie istnieje"):
        discover_extensions()


def test_duplicate_slug_is_an_error(tmp_path):
    write_extension(tmp_path / "a", "alpha")
    write_extension(tmp_path / "b", "alpha")
    with pytest.raises(ExtensionError, match="dwa razy"):
        discover_extensions([tmp_path / "a", tmp_path / "b"])


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (MINIMAL.format(slug="other", kind="project"), "różni się od nazwy katalogu"),
        (MINIMAL.format(slug="bad", kind="unassigned"), "zarezerwowany"),
        (MINIMAL.format(slug="bad", kind="Project"), "kind"),
        (MINIMAL.format(slug="bad", kind="project") + "categoriez: [A]\n", "categoriez"),
        (MINIMAL.format(slug="bad", kind="project").replace("{code}", "{nope}"), "nieznanych atrybutów"),
        (MINIMAL.format(slug="bad", kind="project") + "categories: [A, A]\n", "zdublowane"),
        (
            MINIMAL.format(slug="bad", kind="project")
            + "import_mappings:\n  m:\n    target: internal_record\n    columns:\n"
            + "      amount: {source: kwota, type: text}\n",
            "musi mieć typ amount",
        ),
        (
            MINIMAL.format(slug="bad", kind="project")
            + "import_mappings:\n  m:\n    target: internal_record\n    columns:\n"
            + "      a: {source: Kwota}\n      b: {source: kwota}\n",
            "dwóch pól",
        ),
        (
            MINIMAL.format(slug="bad", kind="project")
            + "import_mappings:\n  m:\n    target: document\n    columns:\n"
            + "      number_raw: {source: numer}\n",
            "wymaga pól",
        ),
        ("slug: bad\n: [", "Nie można odczytać"),
    ],
)
def test_invalid_extension_yaml(tmp_path, text, message):
    directory = write_extension(tmp_path, "bad", text=text)
    with pytest.raises(ExtensionError, match=message):
        load_extension(directory)


def test_broken_hooks_are_reported(tmp_path):
    directory = write_extension(tmp_path, "hooked", hooks="def broken(:\n")
    with pytest.raises(ExtensionError, match="hooks.py"):
        load_extension(directory)


def test_hooks_can_derive_candidates(tmp_path):
    hooks = """
        from kasa.core.extension import CostObjectCandidate

        def derive_cost_object_candidates(extension, records):
            return [
                CostObjectCandidate(kind=extension.cost_object_kind, name=r.text, attrs={"code": r.text},
                                    anchor=(r.record_type, r.record_id))
                for r in records
            ]
    """
    extension = load_extension(write_extension(tmp_path, "hooked", hooks=hooks))
    record = CandidateRecord(record_type="line", record_id=uuid.uuid4(), company_id=uuid.uuid4(), text="P-1")
    [candidate] = extension.derive_cost_object_candidates([record])
    assert candidate.attrs == {"code": "P-1"}
    assert candidate.anchor == ("line", record.record_id)


def test_hook_candidates_must_match_the_extension(tmp_path):
    hooks = """
        from kasa.core.extension import CostObjectCandidate

        def derive_cost_object_candidates(extension, records):
            return [CostObjectCandidate(kind="other", name="X", attrs={"code": "X"}, anchor=("line", None))]
    """
    extension = load_extension(write_extension(tmp_path, "hooked", hooks=hooks))
    with pytest.raises(ExtensionError, match="rodzaju"):
        extension.derive_cost_object_candidates([])
