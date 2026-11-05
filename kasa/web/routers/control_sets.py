"""Control-set labeling: one card per screen, big buttons, no proposals (briefing, step 7).

HTML (``router``):
  GET  /control-sets                           sets of a company + form to create one
  POST /control-sets                           create (company, kind, size)
  GET  /control-sets/{set}/card[?after=N]      next card without a label (records when it was shown)
  GET  /control-sets/{set}/items/{item}?q=...  the same card with a document search (time keeps running)
  POST /control-sets/{set}/items/{item}        save the label, go to the next card
  GET  /control-sets/{set}/done                summary (sentinel results only when the set is complete)

JSON (``api``, under /api): the same operations for other clients (phase 4 reuses them).
The card never shows a model reading, a rule or a candidate ranking - only facts.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated, Any
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from kasa.core import control_sets as cs
from kasa.core.companies import Company, get_company, list_companies
from kasa.core.extension import ExtensionError, get_extension
from kasa.reference import ground_truth as reference
from kasa.web.app import db, templates

Conn = Annotated[psycopg.Connection, Depends(db)]
router = APIRouter(tags=["zbiory kontrolne"])
api = APIRouter(tags=["zbiory kontrolne (API)"])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _company(conn: psycopg.Connection, request: Request, slug: str | None) -> Company:
    slug = slug or request.app.state.company
    if slug:
        try:
            return get_company(conn, slug)
        except LookupError:
            raise HTTPException(404, f"Nie ma firmy {slug!r}") from None
    companies = list_companies(conn)
    if not companies:
        raise HTTPException(404, "Brak firm - uruchom: python -m kasa init-db")
    return companies[0]


def _set_or_404(conn: psycopg.Connection, set_id: UUID) -> dict[str, Any]:
    try:
        return cs.get_set(conn, set_id)
    except LookupError:
        raise HTTPException(404, f"Nie ma zbioru {set_id}") from None


def _categories(extension_slug: str) -> list[str]:
    try:
        return list(get_extension(extension_slug).categories)
    except ExtensionError:
        return []


def _documents_for(conn: psycopg.Connection, meta: dict[str, Any], facts: dict[str, Any],
                   query: str | None) -> list[dict[str, Any]]:
    if facts["type"] != "transaction":
        return []
    direction = "sales" if facts["amount_minor"] > 0 else "purchase"
    return cs.search_documents(conn, meta["company_id"], query=query or None,
                               counterparty_id=facts.get("counterparty_id"), direction=None if query else direction)


def _used_names(conn: psycopg.Connection, set_id: UUID) -> list[str]:
    """Cost-object names the labeler typed earlier in this set (their own words, for consistency)."""
    rows = conn.execute(
        """
        SELECT DISTINCT label ->> 'cost_object' FROM control_set_item
         WHERE set_id = %s AND label ->> 'cost_object' IS NOT NULL ORDER BY 1
        """,
        (set_id,),
    ).fetchall()
    return [r[0] for r in rows]


def label_from_form(kind: str, form: dict[str, Any]) -> dict[str, Any]:
    """The label from the card's form fields (validated later by kasa.core.control_sets)."""
    note = (form.get("note") or "").strip() or None
    if kind == "transaction":
        label: dict[str, Any] = {"status": form.get("status") or ""}
        if label["status"] == "matched":
            refs = []
            for value in form.get("documents") or []:
                ref_type, _, ref_id = str(value).partition(":")
                refs.append({"type": ref_type, "id": ref_id})
            label["documents"] = refs
            label["other_document"] = form.get("other_document") == "1"
        elif label["status"] == "no_document":
            label["subtype"] = form.get("subtype") or None
        if note:
            label["note"] = note
        return label
    label = {"category": form.get("category") or None}
    split_text = (form.get("split") or "").strip()
    if form.get("unassigned") == "1":
        label["unassigned"] = True
    elif split_text:
        items = []
        for line in split_text.splitlines():
            name, _, share = line.rpartition(";") if ";" in line else line.rpartition(" ")
            share = share.strip().rstrip("%").replace(",", ".")
            try:
                value = Decimal(share)
            except InvalidOperation:
                raise cs.ControlSetError(f"podział: nie rozumiem udziału w linii {line!r}") from None
            items.append({"name": name.strip(), "share": str(value / 100 if value > 1 else value)})
        label["split"] = items
    else:
        label["cost_object"] = (form.get("cost_object") or "").strip() or None
    if note:
        label["note"] = note
    return label


def _render_card(request: Request, conn: psycopg.Connection, meta: dict[str, Any], item: dict[str, Any], *,
                 query: str | None = None, error: str | None = None, status_code: int = 200) -> HTMLResponse:
    facts = cs.card(conn, item)
    context = {
        "meta": meta, "item": item, "facts": facts, "query": query or "", "error": error,
        "documents": _documents_for(conn, meta, facts, query),
        "statuses": cs.STATUS_LABELS, "subtypes": cs.SUBTYPE_LABELS,
        "categories": _categories(meta["extension_slug"]) if meta["kind"] == "cost" else [],
        "names": _used_names(conn, meta["id"]) if meta["kind"] == "cost" else [],
        "position": item["labeled"] + (0 if item["label"] is not None else 1),
    }
    return templates.TemplateResponse(request, "control_sets/card.html", context, status_code=status_code)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


@router.get("/control-sets", response_class=HTMLResponse)
def sets_page(request: Request, conn: Conn, company: str | None = None) -> HTMLResponse:
    owner = _company(conn, request, company)
    return templates.TemplateResponse(request, "control_sets/index.html", {
        "company": owner, "sets": cs.list_sets(conn, owner.id), "companies": list_companies(conn)})


@router.post("/control-sets")
async def create_from_form(request: Request, conn: Conn) -> Any:
    form = await request.form()
    owner = _company(conn, request, str(form.get("company") or "") or None)
    try:
        size = int(str(form.get("size") or "0"))
        info = cs.create_control_set(conn, owner, kind=str(form.get("kind") or ""), size=size,
                                     name=str(form.get("name") or "").strip() or None,
                                     expected=reference.sentinels_for(conn, owner, str(form.get("kind") or "")),
                                     created_by=request.app.state.user)
    except (cs.ControlSetError, ValueError) as exc:
        return templates.TemplateResponse(request, "control_sets/index.html", {
            "company": owner, "sets": cs.list_sets(conn, owner.id), "companies": list_companies(conn),
            "error": str(exc)}, status_code=422)
    return RedirectResponse(f"/control-sets/{info.id}/card", status_code=303)


@router.get("/control-sets/{set_id}")
def set_page(set_id: UUID) -> RedirectResponse:
    return RedirectResponse(f"/control-sets/{set_id}/card", status_code=303)


@router.get("/control-sets/{set_id}/card", response_class=HTMLResponse)
def card_page(request: Request, conn: Conn, set_id: UUID, after: int | None = None) -> Any:
    meta = _set_or_404(conn, set_id)
    item = cs.next_item(conn, set_id, after=after)
    if item is None:
        return RedirectResponse(f"/control-sets/{set_id}/done", status_code=303)
    return _render_card(request, conn, meta, item)


@router.get("/control-sets/{set_id}/items/{item_id}", response_class=HTMLResponse)
def item_page(request: Request, conn: Conn, set_id: UUID, item_id: UUID, q: str | None = None) -> HTMLResponse:
    meta = _set_or_404(conn, set_id)
    try:
        item = cs.get_item(conn, set_id, item_id)
    except LookupError:
        raise HTTPException(404, "Nie ma takiej pozycji") from None
    return _render_card(request, conn, meta, item, query=q)


@router.post("/control-sets/{set_id}/items/{item_id}")
async def label_from_page(request: Request, conn: Conn, set_id: UUID, item_id: UUID) -> Any:
    meta = _set_or_404(conn, set_id)
    form = await request.form()
    fields = {key: form.get(key) for key in form.keys()}
    fields["documents"] = form.getlist("documents")
    try:
        item = cs.get_item(conn, set_id, item_id)
        label = label_from_form(meta["kind"], fields)
        cs.save_label(conn, set_id, item_id, label, labeled_by=request.app.state.user)
    except LookupError:
        raise HTTPException(404, "Nie ma takiej pozycji") from None
    except cs.ControlSetError as exc:
        return _render_card(request, conn, meta, item, error=str(exc), status_code=422)
    return RedirectResponse(f"/control-sets/{set_id}/card?after={item['order_no']}", status_code=303)


@router.get("/control-sets/{set_id}/done", response_class=HTMLResponse)
def done_page(request: Request, conn: Conn, set_id: UUID) -> HTMLResponse:
    meta = _set_or_404(conn, set_id)
    return templates.TemplateResponse(request, "control_sets/done.html",
                                      {"meta": meta, "status": cs.status(conn, set_id)})


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


class CreateSet(BaseModel):
    company: str | None = None
    kind: str
    size: int = Field(gt=0)
    name: str | None = None
    seed: int | None = None


class LabelIn(BaseModel):
    label: dict[str, Any]
    labeled_by: str | None = None


@api.get("/control-sets")
def api_sets(request: Request, conn: Conn, company: str | None = None) -> list[dict]:
    owner = _company(conn, request, company)
    return cs.list_sets(conn, owner.id)


@api.post("/control-sets", status_code=201)
def api_create(request: Request, conn: Conn, body: CreateSet) -> dict[str, Any]:
    owner = _company(conn, request, body.company)
    try:
        info = cs.create_control_set(conn, owner, kind=body.kind, size=body.size, name=body.name, seed=body.seed,
                                     expected=reference.sentinels_for(conn, owner, body.kind),
                                     created_by=request.app.state.user)
    except cs.ControlSetError as exc:
        raise HTTPException(422, str(exc)) from None
    return cs.status(conn, info.id)


@api.get("/control-sets/{set_id}")
def api_status(conn: Conn, set_id: UUID) -> dict[str, Any]:
    _set_or_404(conn, set_id)
    return cs.status(conn, set_id)


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    """An item as a client may see it while labeling: no sentinel flag, no expected answer."""
    return {"id": item["id"], "order_no": item["order_no"], "subject_type": item["subject_type"],
            "subject_id": item["subject_id"], "label": item["label"], "labeled_at": item["labeled_at"],
            "progress": f"{item['labeled']}/{item['total']}"}


@api.get("/control-sets/{set_id}/next")
def api_next(conn: Conn, set_id: UUID, after: int | None = None) -> dict[str, Any]:
    meta = _set_or_404(conn, set_id)
    item = cs.next_item(conn, set_id, after=after)
    if item is None:
        return {"done": True, "status": cs.status(conn, set_id)}
    facts = cs.card(conn, item)
    return {"done": False, "item": _public_item(item), "card": facts,
            "documents": _documents_for(conn, meta, facts, None)}


@api.get("/control-sets/{set_id}/items/{item_id}/documents")
def api_documents(conn: Conn, set_id: UUID, item_id: UUID, q: str | None = None) -> list[dict[str, Any]]:
    meta = _set_or_404(conn, set_id)
    try:
        item = cs.get_item(conn, set_id, item_id)
    except LookupError:
        raise HTTPException(404, "Nie ma takiej pozycji") from None
    return _documents_for(conn, meta, cs.card(conn, item), q)


@api.post("/control-sets/{set_id}/items/{item_id}/label")
def api_label(request: Request, conn: Conn, set_id: UUID, item_id: UUID, body: LabelIn) -> dict[str, Any]:
    _set_or_404(conn, set_id)
    try:
        item = cs.save_label(conn, set_id, item_id, body.label,
                             labeled_by=body.labeled_by or request.app.state.user)
    except LookupError:
        raise HTTPException(404, "Nie ma takiej pozycji") from None
    except cs.ControlSetError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"item": _public_item(item), "status": cs.status(conn, set_id)}


@api.get("/control-sets/{set_id}/export")
def api_export(conn: Conn, set_id: UUID) -> dict[str, Any]:
    _set_or_404(conn, set_id)
    return cs.export_set(conn, set_id)
