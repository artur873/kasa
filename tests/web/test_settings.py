"""/settings: keys pasted once, stored privately, loaded into the environment, never shown back."""

from __future__ import annotations

import json
import os
import re
import stat

import pytest
from fastapi.testclient import TestClient

from kasa.core import settings as st
from tests.web.conftest import app_on

PEM = None


def _pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(st.HOME_ENV, str(tmp_path / "kasa-home"))
    for key in list(st.FIELDS) + list(st.CONTENT_VARS):
        monkeypatch.delenv(key, raising=False)
    return tmp_path / "kasa-home"


@pytest.fixture
def web(conn, home, monkeypatch):
    with TestClient(app_on(conn, company="odm")) as client:
        yield client
    for key in list(st.FIELDS) + list(st.CONTENT_VARS):   # the app wrote into os.environ
        os.environ.pop(key, None)


def _csrf(client) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', client.get("/settings").text).group(1)


def test_page_lists_every_group_without_values(web):
    html = web.get("/settings").text
    for g in st.GROUPS:
        assert g.title in html
    assert "dane testowe" in html


def test_secret_is_stored_privately_masked_and_loaded(web, home):
    token = "ksef-token-" + "x" * 30 + "TAIL"
    r = web.post("/settings/ksef", data={"csrf": _csrf(web), "action": "save", "KASA_KSEF_PROVIDER": "ksef",
                                          "KASA_KSEF_ENV": "test", "KASA_KSEF_NIP": "5342650778",
                                          "KASA_KSEF_TOKEN": token})
    assert r.status_code == 200 and "NIP poprawny" in r.text
    assert token not in r.text and "••••TAIL" in r.text
    path = home / "secrets.env"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert st.read_file_values()["KASA_KSEF_TOKEN"] == token
    env: dict[str, str] = {}
    st.load_secrets(env)
    assert env["KASA_KSEF_TOKEN"] == token and env["KASA_KSEF_PROVIDER"] == "ksef"


def test_empty_secret_input_keeps_the_stored_value(web):
    csrf = _csrf(web)
    base = {"csrf": csrf, "action": "save", "KASA_KSEF_PROVIDER": "mock", "KASA_KSEF_ENV": "prod",
            "KASA_KSEF_NIP": "5342650778"}
    web.post("/settings/ksef", data=base | {"KASA_KSEF_TOKEN": "a" * 40})
    web.post("/settings/ksef", data=base | {"KASA_KSEF_TOKEN": ""})
    assert st.read_file_values()["KASA_KSEF_TOKEN"] == "a" * 40


def test_bad_nip_and_csrf_are_refused(web):
    r = web.post("/settings/ksef", data={"csrf": _csrf(web), "KASA_KSEF_PROVIDER": "mock", "KASA_KSEF_ENV": "prod",
                                          "KASA_KSEF_NIP": "1234567890"})
    assert "błędną sumę kontrolną" in r.text
    assert web.post("/settings/ksef", data={"csrf": "zly", "KASA_KSEF_NIP": "5342650778"}).status_code == 400


def test_pem_goes_to_a_private_file_and_signs(web, home):
    pem = _pem()
    r = web.post("/settings/bank", data={"csrf": _csrf(web), "action": "save", "KASA_BANK_PROVIDER": "mock",
                                          "KASA_EB_APP_ID": "3f1c2b9e-1111-2222-3333-444455556666",
                                          "KASA_EB_PRIVATE_KEY_PATH": pem, "KASA_EB_SESSION_ID": "sess-123456789"})
    assert "podpisuje poprawnie" in r.text
    key_file = home / "files" / "enablebanking_key.pem"
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert pem.splitlines()[1] not in r.text and "sess-123456789" not in r.text


def test_service_account_json_checked_against_project(web):
    sa = {"type": "service_account", "project_id": "odm-kasa", "private_key": "x", "client_email": "k@odm-kasa.iam"}
    csrf = _csrf(web)
    web.post("/settings/privacy", data={"csrf": csrf, "action": "generate:KASA_PSEUDONYM_SECRET"})
    r = web.post("/settings/llm", data={"csrf": csrf, "action": "save", "KASA_LLM_PROVIDER": "vertex",
                                         "KASA_VERTEX_PROJECT": "inny-projekt", "KASA_VERTEX_LOCATION": "europe-west1",
                                         "KASA_VERTEX_MODEL": "gemini-2.5-flash",
                                         "GOOGLE_APPLICATION_CREDENTIALS": json.dumps(sa)})
    assert "należy do projektu" in r.text
    r = web.post("/settings/llm", data={"csrf": csrf, "action": "save", "KASA_LLM_PROVIDER": "vertex",
                                         "KASA_VERTEX_PROJECT": "odm-kasa", "KASA_VERTEX_LOCATION": "europe-west1",
                                         "KASA_VERTEX_MODEL": "gemini-2.5-flash"})
    assert "region UE" in r.text


def test_pseudonym_secret_change_needs_confirmation_when_codes_exist(web, conn):
    csrf = _csrf(web)
    web.post("/settings/privacy", data={"csrf": csrf, "action": "generate:KASA_PSEUDONYM_SECRET"})
    first = st.read_file_values()["KASA_PSEUDONYM_SECRET"]
    assert len(first) >= 32
    from kasa.core.pseudonym import pseudonym_for
    from tests.semantics.helpers import odm_company
    pseudonym_for(conn, odm_company(conn).id, "person", "Jan Testowy")
    r = web.post("/settings/privacy", data={"csrf": csrf, "action": "generate:KASA_PSEUDONYM_SECRET"})
    assert "nowe kody" in r.text and st.read_file_values()["KASA_PSEUDONYM_SECRET"] == first


def test_environment_variable_wins_over_the_file(home, monkeypatch):
    st.write_file_values({"KASA_KSEF_NIP": "5342650778"})
    env = {"KASA_KSEF_NIP": "1111111111"}
    st.load_secrets(env)
    assert env["KASA_KSEF_NIP"] == "1111111111"


def test_exported_multiline_content_round_trips(home):
    pem = _pem()
    path = st.store_file_secret("KASA_EB_PRIVATE_KEY_PATH", pem)
    st.write_file_values({"KASA_EB_PRIVATE_KEY_PATH": path})
    line = next(l for l in st.export_lines({}) if l.startswith("KASA_EB_PRIVATE_KEY="))
    value = line.split("=", 1)[1]
    other_home = home.parent / "other"
    os.environ[st.HOME_ENV] = str(other_home)
    env = {"KASA_EB_PRIVATE_KEY": value}
    st.load_secrets(env)
    assert open(env["KASA_EB_PRIVATE_KEY_PATH"]).read().strip() == pem.strip()


def test_remote_client_without_token_is_refused(conn, home):
    from kasa.web.app import create_app
    from tests.web.conftest import app_on as _app
    app = _app(conn, company="odm")
    with TestClient(app, client=("192.168.1.20", 5000)) as remote:
        assert remote.get("/settings").status_code == 403


def test_secrets_file_is_ignored_by_git():
    ignore = (st.REPO_ROOT / ".gitignore").read_text()
    assert ".kasa/" in ignore
