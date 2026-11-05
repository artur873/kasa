"""Secrets and connection settings, kept outside git, loaded into the environment at startup.

Where they live
---------------
``$KASA_HOME/secrets.env`` (default ``<repo>/.kasa/secrets.env``), file mode 0600, directory
0700. File-shaped secrets (EnableBanking PEM key, Google service-account JSON, KSeF
certificate) go to ``$KASA_HOME/files/`` (0600) and the variable holds the path.

Precedence
----------
A variable already present in the process environment (set by the hosting
environment, e.g. the cloud environment settings) always wins over the file.
``*_CONTENT`` style variables (``KASA_EB_PRIVATE_KEY``, ``GOOGLE_APPLICATION_CREDENTIALS_JSON``)
are written to ``files/`` and turned into the matching ``*_PATH`` variable, so a
secret can be pasted as text wherever files are awkward.

Never: log a value, render a value back into HTML, or commit the file.
"""

from __future__ import annotations

import json
import os
import re
import secrets as _secrets
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

HOME_ENV = "KASA_HOME"
REPO_ROOT = Path(__file__).resolve().parents[2]

# content variables -> (path variable, file name)
CONTENT_VARS = {
    "KASA_EB_PRIVATE_KEY": ("KASA_EB_PRIVATE_KEY_PATH", "enablebanking_key.pem"),
    "GOOGLE_APPLICATION_CREDENTIALS_JSON": ("GOOGLE_APPLICATION_CREDENTIALS", "google_service_account.json"),
    "KASA_KSEF_CERT": ("KASA_KSEF_CERT_PATH", "ksef_cert.pem"),
    "KASA_KSEF_KEY": ("KASA_KSEF_KEY_PATH", "ksef_key.pem"),
}

Check = Callable[[dict[str, str]], tuple[bool, str]]


@dataclass(frozen=True)
class SettingField:
    key: str
    label: str
    kind: str                      # text | secret | file | choice | generated
    help: str = ""
    choices: tuple[str, ...] = ()
    default: str = ""
    file_name: str = ""            # for kind=file: name under files/
    placeholder: str = ""


@dataclass(frozen=True)
class SettingGroup:
    slug: str
    title: str
    intro: str
    fields: tuple[SettingField, ...]
    check: Check | None = None
    docs: str = ""
    provider_key: str = ""         # the field that switches mock -> real
    real_value: str = ""


# ---------------------------------------------------------------- checks (no network)

def _check_bank(v: dict[str, str]) -> tuple[bool, str]:
    missing = [k for k in ("KASA_EB_APP_ID", "KASA_EB_PRIVATE_KEY_PATH", "KASA_EB_SESSION_ID") if not v.get(k)]
    if missing:
        return False, "Brakuje: " + ", ".join(missing)
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        key = serialization.load_pem_private_key(Path(v["KASA_EB_PRIVATE_KEY_PATH"]).read_bytes(), password=None)
        key.sign(b"kasa-settings-check", padding.PKCS1v15(), hashes.SHA256())
    except FileNotFoundError:
        return False, "Plik klucza prywatnego nie istnieje."
    except Exception as exc:  # noqa: BLE001 - any parsing problem is a user-facing message
        return False, f"Klucz prywatny nie daje się odczytać jako PEM RSA ({type(exc).__name__})."
    if not re.fullmatch(r"[0-9a-fA-F-]{20,}", v["KASA_EB_APP_ID"]):
        return False, "Application ID wygląda nietypowo (oczekiwany identyfikator UUID z panelu EnableBanking)."
    return True, "Klucz podpisuje poprawnie (RS256), komplet pól. Połączenie z bankiem sprawdzi: python -m kasa bank consent"


def _check_ksef(v: dict[str, str]) -> tuple[bool, str]:
    from kasa.core.normalize import normalize_nip

    try:
        normalize_nip(v.get("KASA_KSEF_NIP", ""))
    except Exception:  # noqa: BLE001
        return False, "NIP jest pusty albo ma błędną sumę kontrolną."
    token = v.get("KASA_KSEF_TOKEN", "")
    if not token and not v.get("KASA_KSEF_CERT_PATH"):
        return False, "Brakuje tokenu KSeF (albo certyfikatu)."
    if token and len(token) < 20:
        return False, "Token KSeF jest podejrzanie krótki."
    return True, "NIP poprawny, token zapisany. Połączenie sprawdzi: python -m kasa documents ksef-sync --company <firma> --from <data> --to <data>"


def _check_llm(v: dict[str, str]) -> tuple[bool, str]:
    loc = v.get("KASA_VERTEX_LOCATION", "")
    if not loc.startswith("europe-"):
        return False, "Region musi być w UE (europe-*), decyzja D5."
    if not v.get("KASA_VERTEX_PROJECT"):
        return False, "Brakuje identyfikatora projektu Google Cloud."
    path = v.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not path:
        return False, "Brakuje klucza konta serwisowego (JSON)."
    try:
        data = json.loads(Path(path).read_text())
    except Exception:  # noqa: BLE001
        return False, "Klucz konta serwisowego nie jest poprawnym JSON-em."
    if data.get("type") != "service_account" or "private_key" not in data:
        return False, "To nie jest klucz konta serwisowego (brak type=service_account / private_key)."
    if data.get("project_id") and data["project_id"] != v["KASA_VERTEX_PROJECT"]:
        return False, f"Klucz należy do projektu {data['project_id']!r}, a wpisano {v['KASA_VERTEX_PROJECT']!r}."
    if not v.get("KASA_PSEUDONYM_SECRET"):
        return False, "Najpierw ustaw sekret pseudonimizacji (bez niego nic nie pójdzie do modelu)."
    return True, f"Klucz konta {data.get('client_email', '?')} poprawny, region UE."


def _check_privacy(v: dict[str, str]) -> tuple[bool, str]:
    s = v.get("KASA_PSEUDONYM_SECRET", "")
    if len(s) < 32:
        return False, "Sekret musi mieć co najmniej 32 znaki - użyj przycisku „Wygeneruj”."
    return True, "Sekret ustawiony."


def _check_db(v: dict[str, str]) -> tuple[bool, str]:
    url = v.get("KASA_DATABASE_URL", "")
    if not url:
        return True, "Pusto: używana jest lokalna baza postgresql://kasa:kasa@localhost:5432/kasa."
    try:
        import psycopg

        with psycopg.connect(url, connect_timeout=5) as conn:
            version = conn.execute("show server_version").fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        return False, f"Nie udało się połączyć ({type(exc).__name__})."
    return True, f"Połączenie działa (PostgreSQL {version}). Następnie: python -m kasa init-db"


GROUPS: tuple[SettingGroup, ...] = (
    SettingGroup(
        "privacy", "1. Sekret pseudonimizacji",
        "Ustaw go PRZED pierwszym prawdziwym importem. Z niego powstają kody osób (P-7f3a). "
        "Zmiana później oznacza nowe kody dla tych samych osób.",
        (SettingField("KASA_PSEUDONYM_SECRET", "Sekret", "generated",
                      "Losowy ciąg, nikomu go nie podawaj. Kliknij „Wygeneruj”."),),
        _check_privacy, "docs/BANK.md (pseudonimizacja)"),
    SettingGroup(
        "bank", "2. Bank: EnableBanking",
        "mBank i Millennium (z rachunkiem VAT). Konto EB jest darmowe; zgoda w banku ważna 180 dni.",
        (SettingField("KASA_BANK_PROVIDER", "Tryb", "choice", "mock = dane testowe, enablebanking = prawdziwy bank",
                      ("mock", "enablebanking"), "mock"),
         SettingField("KASA_EB_APP_ID", "Application ID", "text", "Z panelu EnableBanking → Applications.",
                      placeholder="np. 3f1c2b9e-..."),
         SettingField("KASA_EB_PRIVATE_KEY_PATH", "Klucz prywatny aplikacji (PEM)", "file",
                      "Wklej całą zawartość pliku .pem pobranego przy tworzeniu aplikacji.",
                      file_name="enablebanking_key.pem", placeholder="-----BEGIN PRIVATE KEY-----"),
         SettingField("KASA_EB_SESSION_ID", "Session ID", "secret",
                      "Po autoryzacji rachunków w banku (docs/BANK.md, krok „sesja”).")),
        _check_bank, "docs/BANK.md", "KASA_BANK_PROVIDER", "enablebanking"),
    SettingGroup(
        "ksef", "3. KSeF",
        "Token z Aplikacji Podatnika z uprawnieniem tylko do odczytu faktur. Kasa nigdy nie wystawia faktur.",
        (SettingField("KASA_KSEF_PROVIDER", "Tryb", "choice", "mock = dane testowe, ksef = prawdziwy KSeF",
                      ("mock", "ksef"), "mock"),
         SettingField("KASA_KSEF_ENV", "Środowisko", "choice", "prod = produkcja; test/demo = środowiska MF",
                      ("prod", "test", "demo"), "prod"),
         SettingField("KASA_KSEF_NIP", "NIP firmy", "text", "Kontekst, w którym czytamy faktury.",
                      placeholder="10 cyfr"),
         SettingField("KASA_KSEF_TOKEN", "Token KSeF", "secret", "Wygenerowany w Aplikacji Podatnika.")),
        _check_ksef, "docs/DOKUMENTY.md", "KASA_KSEF_PROVIDER", "ksef"),
    SettingGroup(
        "llm", "4. Model AI: Vertex AI (UE)",
        "Gemini i Claude pod jedną umową Google (decyzja D5). Model widzi tylko tekst po pseudonimizacji.",
        (SettingField("KASA_LLM_PROVIDER", "Tryb", "choice", "mock = bez modelu, vertex = prawdziwy model",
                      ("mock", "vertex"), "mock"),
         SettingField("KASA_VERTEX_PROJECT", "Projekt Google Cloud", "text", "Project ID, nie nazwa.",
                      placeholder="np. firma-kasa-123456"),
         SettingField("KASA_VERTEX_LOCATION", "Region", "choice", "Tylko regiony UE.",
                      ("europe-west1", "europe-west3", "europe-west4", "europe-central2"), "europe-west1"),
         SettingField("KASA_VERTEX_MODEL", "Model", "text", "Domyślnie gemini-2.5-flash.",
                      default="gemini-2.5-flash"),
         SettingField("GOOGLE_APPLICATION_CREDENTIALS", "Klucz konta serwisowego (JSON)", "file",
                      "Wklej całą zawartość pliku JSON konta serwisowego z rolą „Vertex AI User”.",
                      file_name="google_service_account.json", placeholder='{"type": "service_account", ...}')),
        _check_llm, "docs/SEMANTYKA.md", "KASA_LLM_PROVIDER", "vertex"),
    SettingGroup(
        "db", "5. Baza i panel",
        "Docelowo Supabase (Frankfurt). Pusto = lokalna baza.",
        (SettingField("KASA_DATABASE_URL", "Adres bazy (connection string)", "secret",
                      "Supabase → Project Settings → Database → Connection string (URI).",
                      placeholder="postgresql://..."),
         SettingField("KASA_WEB_TOKEN", "Token panelu", "generated",
                      "Wymagany, gdy panel jest widoczny w sieci (telefon).")),
        _check_db, "README.md"),
)

FIELDS: dict[str, SettingField] = {f.key: f for g in GROUPS for f in g.fields}
GROUP_BY_SLUG = {g.slug: g for g in GROUPS}


# ---------------------------------------------------------------- storage

def home() -> Path:
    return Path(os.environ.get(HOME_ENV) or REPO_ROOT / ".kasa")


def secrets_path() -> Path:
    return home() / "secrets.env"


def files_dir() -> Path:
    return home() / "files"


def _ensure_dirs() -> None:
    for d in (home(), files_dir()):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, stat.S_IRWXU)


def read_file_values() -> dict[str, str]:
    path = secrets_path()
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        try:
            values[key.strip()] = json.loads(raw)
        except json.JSONDecodeError:
            values[key.strip()] = raw.strip()
    return values


def _write_private(path: Path, text: str) -> None:
    _ensure_dirs()
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def write_file_values(values: dict[str, str]) -> None:
    lines = ["# Kasa: klucze i ustawienia. Plik prywatny (0600), poza gitem. Edytuj przez panel /settings."]
    lines += [f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in sorted(values.items()) if v != ""]
    _write_private(secrets_path(), "\n".join(lines) + "\n")


def store_file_secret(field_key: str, content: str) -> str:
    """Write a file-shaped secret under files/ and return its path."""
    name = FIELDS[field_key].file_name if field_key in FIELDS else CONTENT_VARS[field_key][1]
    path = files_dir() / name
    _write_private(path, content.strip() + "\n")
    return str(path)


def load_secrets(environ: dict[str, str] | os._Environ = os.environ) -> list[str]:
    """Put file values into the environment (existing variables win); materialize content variables.

    Returns the keys taken from the file. Safe to call many times.
    """
    loaded = []
    for key, value in read_file_values().items():
        if key not in environ and value != "":
            environ[key] = value
            loaded.append(key)
    for content_key, (path_key, _name) in CONTENT_VARS.items():
        content = environ.get(content_key, "")
        if content.startswith('"'):                  # exported multi-line value (JSON string)
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                pass
        if content and not environ.get(path_key):
            environ[path_key] = store_file_secret(content_key, content)
    return loaded


def source_of(key: str, file_values: dict[str, str], environ: dict[str, str] | os._Environ = os.environ) -> str:
    """'env' (hosting environment wins), 'file', or ''."""
    if key in file_values and file_values[key] != "":
        return "env" if environ.get(key, file_values[key]) != file_values[key] else "file"
    return "env" if environ.get(key) else ""


def effective(environ: dict[str, str] | os._Environ = os.environ) -> dict[str, str]:
    file_values = read_file_values()
    return {k: (environ.get(k) or file_values.get(k) or FIELDS[k].default) for k in FIELDS}


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "••••"
    return "••••" + value[-4:]


def generate_secret() -> str:
    return _secrets.token_urlsafe(36)


class SettingsError(ValueError):
    pass


def save_group(slug: str, form: dict[str, str], *, confirm_pseudonym_change: bool = False,
               pseudonyms_in_use: bool = False, environ: dict[str, str] | os._Environ = os.environ) -> list[str]:
    """Validate and store one group's submitted fields. Empty secret inputs keep the stored value.

    Returns the keys that changed. Updates ``environ`` immediately (the running process uses them).
    """
    group = GROUP_BY_SLUG.get(slug)
    if group is None:
        raise SettingsError(f"nieznana grupa ustawień {slug!r}")
    values = read_file_values()
    changed: list[str] = []
    for f in group.fields:
        raw = (form.get(f.key) or "").strip()
        if f.kind in ("secret", "file", "generated") and raw == "":
            continue                                         # keep what is stored
        if f.kind == "choice" and raw not in f.choices:
            raise SettingsError(f"{f.label}: niedozwolona wartość")
        if f.key == "KASA_PSEUDONYM_SECRET":
            current = environ.get(f.key) or values.get(f.key, "")
            if current and raw != current and pseudonyms_in_use and not confirm_pseudonym_change:
                raise SettingsError("Baza ma już kody osób z obecnym sekretem. Zmiana da nowe kody tym samym "
                                    "osobom - zaznacz potwierdzenie, jeśli naprawdę tego chcesz.")
            if len(raw) < 32:
                raise SettingsError("Sekret pseudonimizacji musi mieć co najmniej 32 znaki.")
        if f.key == "KASA_KSEF_NIP" and raw:
            from kasa.core.normalize import normalize_nip
            try:
                raw = normalize_nip(raw)
            except Exception as exc:  # noqa: BLE001
                raise SettingsError("NIP ma błędną sumę kontrolną.") from exc
        if f.kind == "file":
            if f.key == "GOOGLE_APPLICATION_CREDENTIALS":
                try:
                    json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise SettingsError("Klucz konta serwisowego musi być poprawnym JSON-em (cały plik).") from exc
            elif "-----BEGIN" not in raw:
                raise SettingsError(f"{f.label}: wklej całą zawartość pliku PEM (z linią -----BEGIN ...).")
            raw = store_file_secret(f.key, raw)
        if values.get(f.key) != raw:
            changed.append(f.key)
        values[f.key] = raw
        environ[f.key] = raw
    write_file_values(values)
    return changed


def check_group(slug: str, environ: dict[str, str] | os._Environ = os.environ) -> tuple[bool, str]:
    group = GROUP_BY_SLUG[slug]
    if group.check is None:
        return True, "Brak testu dla tej grupy."
    return group.check(effective(environ))


def export_lines(environ: dict[str, str] | os._Environ = os.environ) -> list[str]:
    """KEY=value lines for pasting into a hosting environment's variables (file secrets as content)."""
    eff = effective(environ)
    path_to_content = {path_key: content_key for content_key, (path_key, _n) in CONTENT_VARS.items()}
    lines = []
    for key, value in eff.items():
        if not value:
            continue
        if key in path_to_content and Path(value).is_file():
            content = Path(value).read_text(encoding="utf-8").strip()
            lines.append(f"{path_to_content[key]}={json.dumps(content) if chr(10) in content else content}")
        else:
            lines.append(f"{key}={value}")
    return lines
