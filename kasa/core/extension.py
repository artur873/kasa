"""Company extensions: everything the core must not know about a company.

An extension lives in ``<dir>/<slug>/extension.yaml`` with an optional
``hooks.py`` next to it. It declares the company's cost object (kind and
attribute schema), categories, import mappings for internal files and
sensitive words. Built-in extensions live in ``kasa/extensions/``; more
directories can be added with ``KASA_EXTENSIONS_PATH`` (``os.pathsep``-separated).
Adding a company never requires a change to the core.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import unicodedata
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import ModuleType
from typing import Annotated, Any, ClassVar, Literal
from uuid import UUID

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    create_model,
    field_validator,
    model_validator,
)

from kasa.core.normalize import minor_exponent, parse_amount, to_minor

EXTENSION_FILE = "extension.yaml"
HOOKS_FILE = "hooks.py"
EXTENSIONS_PATH_ENV = "KASA_EXTENSIONS_PATH"
BUILTIN_EXTENSIONS_DIR = Path(__file__).resolve().parents[1] / "extensions"

IDENTIFIER_RE = r"^[a-z][a-z0-9_]*$"
# Kinds owned by the core (the system cost object UNASSIGNED).
RESERVED_COST_OBJECT_KINDS = frozenset({"unassigned"})
# Words that always bypass the model, whatever the extension adds (privacy by default).
BASELINE_SENSITIVE_WORDS: tuple[str, ...] = ("zdrowie", "związki", "komornik", "alimenty")

Identifier = Annotated[str, StringConstraints(pattern=IDENTIFIER_RE)]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Month = Annotated[str, StringConstraints(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]


class ExtensionError(RuntimeError):
    """An extension cannot be loaded or is used inconsistently."""


class MappingError(ValueError):
    """A row cannot be mapped with an import mapping."""


# ---------------------------------------------------------------------------
# Cost object schema
# ---------------------------------------------------------------------------

AttrType = Literal["text", "int", "decimal", "date", "bool", "month"]
_ATTR_PY_TYPES: dict[str, Any] = {
    "text": NonEmptyText,
    "int": int,
    "decimal": Decimal,
    "date": date,
    "bool": bool,
    "month": Month,
}


class AttrSpec(BaseModel):
    """One attribute of the cost object, declared in YAML."""

    model_config = ConfigDict(extra="forbid")

    type: AttrType = "text"
    required: bool = False
    label: str | None = None


class CostObjectSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Identifier
    label: str
    name_template: str
    attrs: dict[Identifier, AttrSpec]

    @field_validator("kind")
    @classmethod
    def _kind_not_reserved(cls, value: str) -> str:
        if value in RESERVED_COST_OBJECT_KINDS:
            raise ValueError(f"rodzaj {value!r} jest zarezerwowany przez rdzeń")
        return value

    @model_validator(mode="after")
    def _template_uses_known_attrs(self) -> CostObjectSpec:
        used = set(re.findall(r"{(\w+)[^}]*}", self.name_template))
        unknown = used - set(self.attrs)
        if unknown:
            raise ValueError(f"name_template używa nieznanych atrybutów: {sorted(unknown)}")
        optional = {name for name in used if not self.attrs[name].required}
        if optional:
            raise ValueError(f"name_template może używać tylko atrybutów wymaganych: {sorted(optional)}")
        return self


class CostObjectAttrs(BaseModel):
    """Base of the per-extension attribute models built from YAML."""

    model_config = ConfigDict(extra="forbid")

    # Optional cross-field validator from hooks.py: validate_cost_object_attrs(attrs: dict) -> None.
    __kasa_hook__: ClassVar[Callable[[dict[str, Any]], None] | None] = None

    @model_validator(mode="after")
    def _run_hook(self) -> CostObjectAttrs:
        hook = type(self).__kasa_hook__
        if hook is not None:
            hook(dict(self))
        return self


def build_attrs_model(slug: str, spec: CostObjectSpec, hooks: ModuleType | None) -> type[CostObjectAttrs]:
    fields: dict[str, Any] = {}
    for name, attr in spec.attrs.items():
        py_type = _ATTR_PY_TYPES[attr.type]
        if attr.required:
            fields[name] = (py_type, Field(..., description=attr.label))
        else:
            fields[name] = (py_type | None, Field(None, description=attr.label))
    model_name = "".join(part.capitalize() for part in slug.split("_")) + "CostObjectAttrs"
    model = create_model(model_name, __base__=CostObjectAttrs, **fields)
    hook = getattr(hooks, "validate_cost_object_attrs", None) if hooks else None
    if hook is not None:
        model.__kasa_hook__ = staticmethod(hook)
    return model


# ---------------------------------------------------------------------------
# Import mappings (column mapping for the import wizard)
# ---------------------------------------------------------------------------

ColumnType = Literal["text", "date", "amount", "decimal", "int", "month"]
MappingTarget = Literal["internal_record", "document"]

# Logical fields that land in dedicated core columns; any other field goes to
# the record's jsonb (l1_internal_record.fields) untouched by the core.
CORE_FIELDS: dict[str, dict[str, str]] = {
    "internal_record": {
        "person": "text",
        "record_date": "date",
        "amount": "amount",
        "currency": "text",
        "cost_object_ref": "text",
        "settlement_type": "text",
    },
    "document": {
        "number_raw": "text",
        "ksef_number": "text",
        "kind": "text",
        "direction": "text",
        "issue_date": "date",
        "sale_date": "date",
        "due_date": "date",
        "seller_nip": "text",
        "seller_name": "text",
        "buyer_nip": "text",
        "buyer_name": "text",
        "currency": "text",
        "net": "amount",
        "vat": "amount",
        "gross": "amount",
    },
}
REQUIRED_CORE_FIELDS: dict[str, set[str]] = {
    "internal_record": set(),
    "document": {"number_raw", "issue_date", "gross"},
}
DEFAULT_DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%d-%m-%Y")
DEFAULT_MONTH_FORMATS = ("%Y-%m", "%m.%Y", "%m/%Y", "%m-%Y")


class ColumnSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: NonEmptyText                 # column header in the file
    aliases: list[NonEmptyText] = []     # other headers meaning the same column
    type: ColumnType = "text"
    required: bool = False
    formats: list[str] = []              # strptime formats for date/month columns

    def headers(self) -> list[str]:
        return [self.source, *self.aliases]


@dataclass(frozen=True)
class MappedRow:
    core: dict[str, Any]     # fields with a dedicated core column
    extra: dict[str, Any]    # extension-specific fields (stored as jsonb)


class ColumnMapping(BaseModel):
    """How to read one kind of internal file (e.g. a project manager sheet)."""

    model_config = ConfigDict(extra="forbid")

    target: MappingTarget
    description: str = ""
    file_types: list[Literal["xlsx", "csv"]] = ["xlsx", "csv"]
    sheet: str | None = None
    header_row: int = Field(1, ge=1)
    currency: str = "PLN"                # default currency of amount columns
    columns: dict[Identifier, ColumnSpec]

    @field_validator("currency")
    @classmethod
    def _currency_known(cls, value: str) -> str:
        minor_exponent(value)
        return value

    @model_validator(mode="after")
    def _consistent(self) -> ColumnMapping:
        core = CORE_FIELDS[self.target]
        for name, spec in self.columns.items():
            expected = core.get(name)
            if expected is not None and spec.type != expected:
                raise ValueError(f"pole {name!r} dla celu {self.target} musi mieć typ {expected}, a ma {spec.type}")
        missing = REQUIRED_CORE_FIELDS[self.target] - set(self.columns)
        if missing:
            raise ValueError(f"mapowanie dla celu {self.target} wymaga pól: {sorted(missing)}")
        seen: dict[str, str] = {}
        for name, spec in self.columns.items():
            for header in spec.headers():
                key = header_key(header)
                if key in seen and seen[key] != name:
                    raise ValueError(f"nagłówek {header!r} przypisany do dwóch pól: {seen[key]!r} i {name!r}")
                seen[key] = name
        return self

    def map_row(self, row: Mapping[str, Any]) -> MappedRow:
        """Map one row (header -> cell value) to typed core and extra fields."""
        cells = {header_key(str(k)): v for k, v in row.items() if k is not None}
        currency = self.currency
        if "currency" in self.columns:
            raw_currency = self._cell(cells, "currency")
            if not _is_blank(raw_currency):
                currency = str(raw_currency).strip().upper()
                minor_exponent(currency)
        core_names = CORE_FIELDS[self.target]
        core: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        for name, spec in self.columns.items():
            value = self._cell(cells, name)
            if _is_blank(value):
                if spec.required:
                    raise MappingError(f"Brak wartości w kolumnie {spec.source!r} (pole {name})")
                parsed = None
            else:
                try:
                    parsed = _parse_cell(value, spec, currency)
                except (ValueError, InvalidOperation) as exc:
                    raise MappingError(f"Kolumna {spec.source!r}: nie da się odczytać {value!r} ({exc})") from exc
            (core if name in core_names else extra)[name] = parsed
        if "currency" in core_names and any(core_names.get(n) == "amount" for n in self.columns):
            core["currency"] = currency
        return MappedRow(core=core, extra=extra)

    def _cell(self, cells: Mapping[str, Any], name: str) -> Any:
        for header in self.columns[name].headers():
            key = header_key(header)
            if key in cells:
                return cells[key]
        return None


def header_key(header: str) -> str:
    """Compare headers loosely: case, diacritics, spaces and punctuation are ignored."""
    text = unicodedata.normalize("NFKD", header.replace("ł", "l").replace("Ł", "L")).casefold()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^0-9a-z]+", "_", text).strip("_")


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _parse_cell(value: Any, spec: ColumnSpec, currency: str) -> Any:
    match spec.type:
        case "text":
            return str(value).strip()
        case "int":
            if isinstance(value, bool):
                raise ValueError("bool is not an int")
            if isinstance(value, int):
                return value
            return int(str(value).strip())
        case "decimal":
            return _to_decimal(value)
        case "amount":
            return to_minor(_to_decimal(value), currency)
        case "date":
            if isinstance(value, datetime):
                return value.date()
            if isinstance(value, date):
                return value
            return _strptime(str(value).strip(), spec.formats or DEFAULT_DATE_FORMATS).date()
        case "month":
            if isinstance(value, (date, datetime)):
                return f"{value.year:04d}-{value.month:02d}"
            parsed = _strptime(str(value).strip(), spec.formats or DEFAULT_MONTH_FORMATS)
            return f"{parsed.year:04d}-{parsed.month:02d}"
    raise ValueError(f"unknown column type {spec.type}")


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("bool is not a number")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # Spreadsheet cells arrive as floats; the shortest repr is the value as displayed.
        return Decimal(repr(value))
    return parse_amount(str(value))


def _strptime(text: str, formats: Sequence[str]) -> datetime:
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"nieznany format daty: {text!r}")


# ---------------------------------------------------------------------------
# Allocation settings of a company
# ---------------------------------------------------------------------------


class AllocationSpec(BaseModel):
    """How allocation treats the company's evidence (``allocation:`` in extension.yaml).

    * ``internal_record_is_hard`` - an explicit assignment in an internal file (a row of a
      sheet naming the cost object) is hard evidence: allocation by it may be automatic.
      Off by default: an internal file is a document, but not hard evidence until a rule matures.
    * ``person_day_settlement_types`` - settlement types of internal rows that count as one
      person-day of work (the cascade of a collective invoice splits by person-days); empty =
      every row with a person counts.
    """

    model_config = ConfigDict(extra="forbid")

    internal_record_is_hard: bool = False
    person_day_settlement_types: list[NonEmptyText] = []


# ---------------------------------------------------------------------------
# Cost object candidates. The core groups records (kasa/core/cost_objects_discovery/)
# and calls the extension once per group; the extension says what the group is.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateRecord:
    """A record that may belong to a cost object (input of candidate derivation)."""

    record_type: Literal["transaction", "document", "line", "internal_record"]
    record_id: UUID
    company_id: UUID
    record_date: date | None = None
    amount_minor: int | None = None
    currency: str = "PLN"
    text: str = ""                       # pseudonymized description (line name, title, row)
    attrs: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CostObjectCandidate:
    """A proposed cost object; it becomes real only after a human approves it."""

    kind: str
    name: str
    attrs: dict[str, Any]
    anchor: tuple[str, UUID]                                   # (record_type, record_id)
    supporting: list[tuple[str, UUID]] = field(default_factory=list)
    confidence: float | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Extension
# ---------------------------------------------------------------------------


class Extension(ABC):
    """What a company is made of, as far as the core needs to know."""

    slug: str
    name: str                                     # company display name
    cost_object_kind: str
    cost_object_label: str
    cost_object_schema: type[BaseModel]           # pydantic model of l3_cost_object.attrs
    categories: list[str]
    import_mappings: dict[str, ColumnMapping]
    sensitive_words: list[str]
    allocation: AllocationSpec = AllocationSpec()

    @abstractmethod
    def derive_cost_object_candidates(self, records: Sequence[CandidateRecord]) -> list[CostObjectCandidate]:
        """Group records into cost object candidates for the review card."""

    @abstractmethod
    def cost_object_name(self, attrs: Mapping[str, Any]) -> str:
        """Display name of a cost object from its validated attributes."""

    def validate_cost_object_attrs(self, attrs: Mapping[str, Any]) -> dict[str, Any]:
        """Validate attributes against the extension schema; return them JSON-ready."""
        model = self.cost_object_schema.model_validate(dict(attrs))
        return model.model_dump(mode="json", exclude_none=True)

    def is_anchor_eligible(self, record: CandidateRecord) -> bool | None:
        """May this record anchor a cost object candidate? None = the core decides (default rule)."""
        return None

    def import_mapping(self, key: str) -> ColumnMapping:
        try:
            return self.import_mappings[key]
        except KeyError:
            raise ExtensionError(f"Rozszerzenie {self.slug} nie ma mapowania importu {key!r}") from None

    def is_sensitive(self, text: str) -> bool:
        """True when the text contains a sensitive word (it must bypass the model), matched by stem."""
        from kasa.core.sensitive import code_terms, find_sensitive

        return bool(find_sensitive(text, code_terms(self.sensitive_words)))


class ExtensionConfig(BaseModel):
    """Schema of extension.yaml."""

    model_config = ConfigDict(extra="forbid")

    slug: Identifier
    name: NonEmptyText
    description: str = ""
    cost_object: CostObjectSpec
    categories: list[NonEmptyText] = []
    import_mappings: dict[Identifier, ColumnMapping] = {}
    sensitive_words: list[NonEmptyText] = []
    allocation: AllocationSpec = AllocationSpec()

    @field_validator("categories")
    @classmethod
    def _unique_categories(cls, value: list[str]) -> list[str]:
        duplicates = sorted({c for c in value if value.count(c) > 1})
        if duplicates:
            raise ValueError(f"zdublowane kategorie: {duplicates}")
        return value


class YamlExtension(Extension):
    """Extension declared in extension.yaml, optionally extended by hooks.py."""

    def __init__(self, config: ExtensionConfig, directory: Path, hooks: ModuleType | None = None):
        self.config = config
        self.directory = directory
        self.hooks = hooks
        self.slug = config.slug
        self.name = config.name
        self.cost_object_kind = config.cost_object.kind
        self.cost_object_label = config.cost_object.label
        self.cost_object_schema = build_attrs_model(config.slug, config.cost_object, hooks)
        self.categories = list(config.categories)
        self.import_mappings = dict(config.import_mappings)
        self.allocation = config.allocation
        words = [*BASELINE_SENSITIVE_WORDS, *config.sensitive_words]
        self.sensitive_words = list(dict.fromkeys(w.casefold() for w in words))

    def __repr__(self) -> str:
        return f"<YamlExtension {self.slug} kind={self.cost_object_kind} dir={self.directory}>"

    def cost_object_name(self, attrs: Mapping[str, Any]) -> str:
        return self.config.cost_object.name_template.format_map(dict(attrs))

    def is_anchor_eligible(self, record: CandidateRecord) -> bool | None:
        hook = getattr(self.hooks, "is_anchor_eligible", None)
        return None if hook is None else hook(self, record)

    def derive_cost_object_candidates(self, records: Sequence[CandidateRecord]) -> list[CostObjectCandidate]:
        hook = getattr(self.hooks, "derive_cost_object_candidates", None)
        if hook is None:
            return []
        candidates = list(hook(self, records))
        for candidate in candidates:
            if candidate.kind != self.cost_object_kind:
                raise ExtensionError(
                    f"Hook rozszerzenia {self.slug} zwrócił kandydata rodzaju {candidate.kind!r}, "
                    f"a rozszerzenie definiuje {self.cost_object_kind!r}"
                )
            self.validate_cost_object_attrs(candidate.attrs)
        return candidates


# ---------------------------------------------------------------------------
# Loading and registry
# ---------------------------------------------------------------------------


def load_extension(directory: Path) -> YamlExtension:
    """Load one extension from its directory."""
    path = directory / EXTENSION_FILE
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ExtensionError(f"Nie można odczytać {path}: {exc}") from exc
    try:
        config = ExtensionConfig.model_validate(data)
    except ValidationError as exc:
        raise ExtensionError(f"Niepoprawny plik {path}:\n{exc}") from exc
    if config.slug != directory.name:
        raise ExtensionError(f"{path}: slug {config.slug!r} różni się od nazwy katalogu {directory.name!r}")
    hooks = _load_hooks(directory, config.slug)
    try:
        return YamlExtension(config, directory, hooks)
    except Exception as exc:  # schema building may fail on a broken hook
        raise ExtensionError(f"Rozszerzenie {config.slug} nie daje się zbudować: {exc}") from exc


def _load_hooks(directory: Path, slug: str) -> ModuleType | None:
    path = directory / HOOKS_FILE
    if not path.is_file():
        return None
    module_name = f"kasa_extension_hooks_{slug}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ExtensionError(f"Nie można załadować {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise ExtensionError(f"Błąd w {path}: {exc}") from exc
    return module


def extension_search_paths() -> list[Path]:
    """Built-in directory first, then KASA_EXTENSIONS_PATH entries."""
    paths = [BUILTIN_EXTENSIONS_DIR]
    for entry in os.environ.get(EXTENSIONS_PATH_ENV, "").split(os.pathsep):
        if entry.strip():
            paths.append(Path(entry.strip()).expanduser().resolve())
    return paths


def discover_extensions(paths: Sequence[Path] | None = None) -> dict[str, Extension]:
    """Load every extension found in the search paths, keyed by slug."""
    found: dict[str, Extension] = {}
    for base in paths if paths is not None else extension_search_paths():
        if not base.is_dir():
            raise ExtensionError(f"Katalog rozszerzeń nie istnieje: {base}")
        for child in sorted(base.iterdir()):
            if not (child / EXTENSION_FILE).is_file():
                continue
            extension = load_extension(child)
            if extension.slug in found:
                other = getattr(found[extension.slug], "directory", "?")
                raise ExtensionError(f"Rozszerzenie {extension.slug!r} zdefiniowane dwa razy: {other} i {child}")
            found[extension.slug] = extension
    return found


def get_extension(slug: str, paths: Sequence[Path] | None = None) -> Extension:
    extensions = discover_extensions(paths)
    try:
        return extensions[slug]
    except KeyError:
        available = ", ".join(sorted(extensions))
        raise ExtensionError(f"Nie znaleziono rozszerzenia {slug!r} (dostępne: {available})") from None
