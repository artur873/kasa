"""``/settings`` - paste keys once; they are stored in ``$KASA_HOME/secrets.env`` (0600), never in git.

Access: only from this machine (loopback) when the panel has no token; with a token, the
token is required like everywhere else. Every POST carries a per-process CSRF value.
Values are never rendered back: secrets show as ``••••abcd``.
"""

from __future__ import annotations

import hmac
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from kasa.core import settings as st

router = APIRouter()
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


def guard(request: Request) -> None:
    if not request.app.state.token and (request.client is None or request.client.host not in LOOPBACK):
        raise HTTPException(403, "Ustawienia są dostępne tylko z tego komputera albo z tokenem panelu.")


def _csrf_ok(request: Request, value: str) -> None:
    if not hmac.compare_digest(value or "", request.app.state.csrf):
        raise HTTPException(400, "Formularz wygasł - odśwież stronę.")


def _pseudonyms_in_use(request: Request) -> bool:
    try:
        with request.app.state.connection_factory() as conn:
            return bool(conn.execute("select exists(select 1 from pseudonym_key)").fetchone()[0])
    except Exception:  # noqa: BLE001 - no database yet = nothing to protect
        return False


def _view(request: Request) -> dict[str, Any]:
    file_values = st.read_file_values()
    eff = st.effective()
    groups = []
    for g in st.GROUPS:
        fields = []
        for f in g.fields:
            value = eff.get(f.key, "")
            shown = value if f.kind in ("text", "choice") else st.mask(value)
            if f.kind == "file" and value:
                shown = "plik zapisany"
            fields.append({"f": f, "value": value if f.kind in ("text", "choice") else "", "shown": shown,
                           "set": bool(value) and value != f.default or f.kind in ("text", "choice") and bool(value),
                           "source": st.source_of(f.key, file_values)})
        filled = all(x["set"] for x in fields if x["f"].kind not in ("choice",) and x["f"].key not in
                     ("KASA_DATABASE_URL", "KASA_WEB_TOKEN", "KASA_VERTEX_MODEL"))
        mode = eff.get(g.provider_key, "") if g.provider_key else ""
        groups.append({"g": g, "fields": fields, "filled": filled, "mode": mode,
                       "live": bool(g.provider_key) and mode == g.real_value})
    return {"groups": groups, "home": str(st.secrets_path()), "csrf": request.app.state.csrf}


@router.get("/settings", response_class=HTMLResponse, dependencies=[Depends(guard)])
def page(request: Request, msg: str = "", ok: int = 1, group: str = "") -> HTMLResponse:
    from kasa.web.app import templates

    ctx = _view(request) | {"request": request, "msg": msg, "ok": bool(ok), "active": group}
    return templates.TemplateResponse(request, "settings/index.html", ctx)


@router.post("/settings/{slug}", dependencies=[Depends(guard)])
async def save(request: Request, slug: str) -> RedirectResponse:
    form = {k: str(v) for k, v in (await request.form()).items()}
    _csrf_ok(request, form.pop("csrf", ""))
    action = form.pop("action", "save")
    try:
        if action.startswith("generate:"):
            key = action.split(":", 1)[1]
            if key not in st.FIELDS or st.FIELDS[key].kind != "generated":
                raise st.SettingsError("tego pola nie da się wygenerować")
            form = {key: st.generate_secret()}
        changed = st.save_group(slug, form, confirm_pseudonym_change=form.get("confirm") == "1",
                                pseudonyms_in_use=_pseudonyms_in_use(request))
        if action == "test" or action == "save":
            ok, msg = st.check_group(slug)
            prefix = f"Zapisano ({len(changed)} zmian). " if changed else ""
            return RedirectResponse(f"/settings?group={slug}&ok={int(ok)}&msg={quote(prefix + msg)}#{slug}", 303)
        return RedirectResponse(f"/settings?group={slug}&ok=1&msg={quote('Wygenerowano i zapisano.')}#{slug}", 303)
    except st.SettingsError as exc:
        return RedirectResponse(f"/settings?group={slug}&ok=0&msg={quote(str(exc))}#{slug}", 303)


@router.post("/settings-export", response_class=HTMLResponse, dependencies=[Depends(guard)])
def export(request: Request, csrf: str = Form("")) -> HTMLResponse:
    from kasa.web.app import templates

    _csrf_ok(request, csrf)
    return templates.TemplateResponse(request, "settings/export.html", {"request": request,
                                                                        "lines": st.export_lines()})
