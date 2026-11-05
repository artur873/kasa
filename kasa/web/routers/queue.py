"""The review queue on the phone (briefing, step 7): one card, big buttons, a 10-second undo.

HTML (``router``):
  GET  /queue                         the next card (or "pusto"), toast "Cofnij" after a decision
  GET  /queue/items/{id}              a given card
  POST /queue/items/{id}/answer       a button: accept (rank 1 / alternative), reject, defer, bulk, approve, …
  GET  /queue/items/{id}/other        "Inne…" (document search, no document WITH a subtype, own split, category)
  POST /queue/items/{id}/other        save "Inne…"
  POST /queue/undo                    undo a decision or a batch (toast and history)
  GET  /queue/history                 the person's decisions, each with "Cofnij"
  GET  /queue/stats                   KPI: minutes of work and errors weighted by amount
  POST /queue/build                   rebuild the queue

JSON (``api``, under /api/queue): the same for every action. Nothing on a card shows a percentage (D8).
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from kasa.core import control_sets as cs
from kasa.core.companies import Company, get_company, list_companies
from kasa.core.decisions import DecisionError
from kasa.core.extension import ExtensionError, get_extension
from kasa.core.queue import actions, build
from kasa.core.queue import cards as queue_cards
from kasa.core.rules.lifecycle import RuleDecisionError
from kasa.web.app import db, templates

Conn = Annotated[psycopg.Connection, Depends(db)]
router = APIRouter(tags=["kolejka"])
api = APIRouter(prefix="/queue", tags=["kolejka (API)"])
ERRORS = (DecisionError, actions.QueueError, RuleDecisionError, ValueError, PermissionError)
SUBTYPES = {"zus": "ZUS", "tax": "podatek (US)", "fee": "prowizja / opłata", "interest": "odsetki",
            "advance": "zaliczka", "deposit": "kaucja", "card_awaiting": "karta - czeka na fakturę",
            "salary": "wynagrodzenie", "refund": "zwrot", "other": "inne"}


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
    return next((c for c in companies if c.slug == "odm"), companies[0])


def _item_or_404(conn: psycopg.Connection, item_id: UUID) -> dict[str, Any]:
    try:
        from kasa.core.queue import state

        return state.get(conn, item_id)
    except LookupError:
        raise HTTPException(404, "Nie ma takiej karty") from None


def _categories(conn: psycopg.Connection, company_id: UUID) -> list[str]:
    slug = conn.execute("SELECT extension_slug FROM company WHERE id = %s", (company_id,)).fetchone()[0]
    try:
        return list(get_extension(slug).categories)
    except ExtensionError:
        return []


def _objects(conn: psycopg.Connection, company_id: UUID) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, name FROM l3_cost_object WHERE company_id = %s AND status = 'approved' ORDER BY system_code NULLS "
        "FIRST, name", (company_id,)).fetchall()
    return [{"id": str(r[0]), "name": r[1]} for r in rows]


def _toast(reply: dict[str, Any]) -> str:
    parts = [f"msg={quote(str(reply.get('message') or 'Zapisano'))}"]
    if reply.get("batch_id"):
        parts.append(f"batch={reply['batch_id']}")
    elif reply.get("decision_ids"):
        parts.append(f"undo={reply['decision_ids'][0]}")
    if reply.get("undo_until"):
        parts.append(f"until={quote(str(reply['undo_until']))}")
    return "&".join(parts)


def _render(request: Request, conn: psycopg.Connection, company: Company, item: dict[str, Any] | None, *,
            error: str | None = None, status_code: int = 200) -> HTMLResponse:
    q = request.query_params
    toast = None
    if q.get("msg"):
        toast = {"message": q.get("msg"), "undo": q.get("undo"), "batch": q.get("batch"), "until": q.get("until")}
    remaining = conn.execute(
        "SELECT count(*) FILTER (WHERE deferred_until IS NULL OR deferred_until <= now()), count(*) FROM queue_state "
        "WHERE company_id = %s AND closed_at IS NULL", (company.id,)).fetchone()
    context = {"company": company, "toast": toast, "error": error, "remaining": remaining[0],
               "deferred": remaining[1] - remaining[0], "card": None, "item": item,
               "categories": _categories(conn, company.id)}
    if item is not None:
        context["card"] = queue_cards.card(conn, item)
    return templates.TemplateResponse(request, "queue/card.html", context, status_code=status_code)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


@router.get("/queue", response_class=HTMLResponse)
def queue_page(request: Request, conn: Conn, company: str | None = None) -> HTMLResponse:
    owner = _company(conn, request, company)
    item = actions.next_item(conn, owner.id)
    return _render(request, conn, owner, item)


@router.get("/queue/items/{item_id}", response_class=HTMLResponse)
def item_page(request: Request, conn: Conn, item_id: UUID) -> HTMLResponse:
    item = _item_or_404(conn, item_id)
    owner = get_company(conn, conn.execute("SELECT slug FROM company WHERE id = %s",
                                           (item["company_id"],)).fetchone()[0])
    if item["closed_at"] is None:
        from kasa.core.queue import state

        item["shown_at"] = state.mark_shown(conn, item_id)
    return _render(request, conn, owner, item)


def _form_extra(form: dict[str, Any]) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    for key in ("category", "subtype", "other", "into"):
        if form.get(key):
            extra[key] = form[key]
    if form.get("with"):
        extra["with"] = [form["with"]]
    if form.get("group_a") is not None:
        a = form.get("group_a") or []
        b = [m for m in form.get("members") or [] if m not in a]
        extra["groups"] = [a, b]
    if form.get("suggestion_ids"):
        extra["suggestion_ids"] = form["suggestion_ids"]
    if form.get("action") == "distinct" and not extra.get("other") and form.get("with"):
        extra["other"] = form["with"]
    for key in ("pattern", "rule_action", "scope"):
        if form.get(key):
            try:
                extra[key] = json.loads(str(form[key]))
            except json.JSONDecodeError as exc:
                raise DecisionError(f"{key}: niepoprawny JSON ({exc})") from None
    if form.get("question"):
        extra["question"] = form["question"]
    return extra


@router.post("/queue/items/{item_id}/answer")
async def answer_page(request: Request, conn: Conn, item_id: UUID) -> Any:
    item = _item_or_404(conn, item_id)
    owner = get_company(conn, conn.execute("SELECT slug FROM company WHERE id = %s",
                                           (item["company_id"],)).fetchone()[0])
    form = await request.form()
    fields: dict[str, Any] = {k: form.get(k) for k in form.keys()}
    fields["members"] = form.getlist("members")
    fields["group_a"] = form.getlist("group_a") if "split" == fields.get("action") else None
    fields["suggestion_ids"] = form.getlist("suggestion_ids")
    action = str(fields.get("action") or "accept")
    until = None
    if fields.get("until"):
        until = date.fromisoformat(str(fields["until"]))
    try:
        reply = actions.answer(conn, item_id, action, decided_by=request.app.state.user,
                               suggestion_id=UUID(str(fields["suggestion_id"])) if fields.get("suggestion_id") else None,
                               extra=_form_extra({**fields, "action": action}),
                               reason=str(fields.get("reason") or "") or None, until=until)
    except ERRORS as exc:
        return _render(request, conn, owner, item, error=str(exc), status_code=422)
    return RedirectResponse(f"/queue?company={owner.slug}&{_toast(reply)}", status_code=303)


@router.get("/queue/items/{item_id}/other", response_class=HTMLResponse)
def other_page(request: Request, conn: Conn, item_id: UUID, q: str | None = None,
               error: str | None = None) -> HTMLResponse:
    item = _item_or_404(conn, item_id)
    card = queue_cards.card(conn, item)
    documents = []
    if item["subject_type"] == "transaction" and item["card_kind"] in ("match", "sentinel"):
        tx = conn.execute("SELECT amount_minor, counterparty_id FROM l1_bank_transaction WHERE id = %s",
                          (item["subject_id"],)).fetchone()
        documents = cs.search_documents(conn, item["company_id"], query=q or None,
                                        counterparty_id=None if q else tx[1],
                                        direction=None if q else ("sales" if tx[0] > 0 else "purchase"))
    return templates.TemplateResponse(request, "queue/other.html", {
        "item": item, "card": card, "documents": documents, "query": q or "", "subtypes": SUBTYPES,
        "objects": _objects(conn, item["company_id"]), "categories": _categories(conn, item["company_id"]),
        "error": error}, status_code=422 if error else 200)


@router.post("/queue/items/{item_id}/other")
async def other_save(request: Request, conn: Conn, item_id: UUID) -> Any:
    item = _item_or_404(conn, item_id)
    slug = conn.execute("SELECT slug FROM company WHERE id = %s", (item["company_id"],)).fetchone()[0]
    form = await request.form()
    kind = str(form.get("kind") or "")
    extra: dict[str, Any] = {"kind": kind}
    if kind == "matched":
        targets = []
        for value in form.getlist("documents"):
            ref_type, _, ref_id = str(value).partition(":")
            key = {"document": "document_id", "internal_record": "internal_record_id",
                   "transaction": "related_transaction_id"}.get(ref_type, "document_id")
            targets.append({key: ref_id})
        extra["targets"] = targets
    elif kind == "no_document":
        extra["subtype"] = str(form.get("subtype") or "") or None
    elif kind == "allocation":
        shares = []
        for obj in form.getlist("object"):
            value = str(form.get(f"share_{obj}") or "").replace(",", ".").rstrip("%").strip()
            if value:
                number = float(value)
                shares.append({"cost_object_id": obj, "share": number / 100 if number > 1 else number})
        extra["shares"] = shares
    elif kind == "category":
        extra["category"] = str(form.get("category") or "") or None
    try:
        reply = actions.answer(conn, item_id, "other", decided_by=request.app.state.user, extra=extra,
                               reason=str(form.get("reason") or "") or None)
    except ERRORS as exc:
        return RedirectResponse(f"/queue/items/{item_id}/other?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/queue?company={slug}&{_toast(reply)}", status_code=303)


@router.post("/queue/undo")
async def undo_page(request: Request, conn: Conn) -> Any:
    form = await request.form()
    back = str(form.get("back") or "/queue")
    try:
        if form.get("batch"):
            reply = actions.undo_batch(conn, UUID(str(form["batch"])), decided_by=request.app.state.user)
        else:
            reply = actions.undo(conn, UUID(str(form.get("decision"))), decided_by=request.app.state.user)
        build_company = conn.execute("SELECT c.slug FROM company c JOIN l3_decision_log d ON d.company_id = c.id "
                                     "WHERE d.id = %s", (UUID(reply["revoke_ids"][0]),)).fetchone()[0]
        build.build(conn, get_company(conn, build_company), mine_rules=False, auto_rules=False)
    except ERRORS + (LookupError,) as exc:
        return RedirectResponse(f"{back}?msg={quote('Nie cofnięto: ' + str(exc))}", status_code=303)
    return RedirectResponse(f"{back}?msg={quote(reply['message'])}", status_code=303)


@router.get("/queue/history", response_class=HTMLResponse)
def history_page(request: Request, conn: Conn, company: str | None = None, auto: bool = False) -> HTMLResponse:
    owner = _company(conn, request, company)
    return templates.TemplateResponse(request, "queue/history.html", {
        "company": owner, "rows": actions.history(conn, owner.id, limit=100, include_auto=auto), "auto": auto,
        "message": request.query_params.get("msg")})


@router.get("/queue/stats", response_class=HTMLResponse)
def stats_page(request: Request, conn: Conn, company: str | None = None) -> HTMLResponse:
    owner = _company(conn, request, company)
    return templates.TemplateResponse(request, "queue/stats.html",
                                      {"company": owner, "stats": actions.stats(conn, owner.id)})


@router.post("/queue/build")
async def build_page(request: Request, conn: Conn) -> Any:
    form = await request.form()
    owner = _company(conn, request, str(form.get("company") or "") or None)
    report = build.build(conn, owner)
    headline = report.summary(for_person=True).splitlines()[0]
    return RedirectResponse(f"/queue?company={owner.slug}&msg={quote(headline)}", status_code=303)


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


class AnswerIn(BaseModel):
    action: str = "accept"
    suggestion_id: UUID | None = None
    reason: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    until: date | None = None
    decided_by: str | None = None


class OtherIn(BaseModel):
    kind: str
    targets: list[dict[str, Any]] | None = None
    subtype: str | None = None
    shares: list[dict[str, Any]] | None = None
    category: str | None = None
    reason: str | None = None
    decided_by: str | None = None


class UndoIn(BaseModel):
    decision_id: UUID | None = None
    batch_id: UUID | None = None
    reason: str | None = None
    decided_by: str | None = None


class BuildIn(BaseModel):
    """Rebuild the queue. The mining threshold (``--min-months``) is a CLI-only option (review of 4C/5,
    point 14): the panel always mines with the briefing's 2 months; an unknown field is refused."""

    model_config = ConfigDict(extra="forbid")

    company: str | None = None


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, UUID | date | datetime):
        return value.isoformat() if not isinstance(value, UUID) else str(value)
    return value


def _do(request: Request, conn: psycopg.Connection, item_id: UUID, action: str, *, decided_by: str | None = None,
        **kwargs: Any) -> dict[str, Any]:
    _item_or_404(conn, item_id)
    try:
        return _json_safe(actions.answer(conn, item_id, action, decided_by=decided_by or request.app.state.user,
                                         **kwargs))
    except ERRORS as exc:
        raise HTTPException(422, str(exc)) from None
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None


@api.get("")
def api_list(request: Request, conn: Conn, company: str | None = None, deferred: bool = True) -> list[dict]:
    """The open cards in order. A sentinel is listed as the match card it looks like (``card_kind`` through
    ``SHOWN_KIND``, no ``sentinel_id``) - nothing in the answer tells it apart (review of 4C/5, point 1)."""
    owner = _company(conn, request, company)
    keys = ("id", "position", "subject_type", "subject_id", "rank_reason", "badge", "deferred_until", "batch_key",
            "shown_at")
    return [_json_safe({**{k: row[k] for k in keys},
                        "card_kind": queue_cards.SHOWN_KIND.get(row["card_kind"], row["card_kind"])})
            for row in actions.list_items(conn, owner.id, include_deferred=deferred)]


@api.post("/build")
def api_build(request: Request, conn: Conn, body: BuildIn) -> dict[str, Any]:
    owner = _company(conn, request, body.company)
    report = build.build(conn, owner)
    return {"items": report.items, "by_kind": report.shown_by_kind(), "created": report.created,
            "closed": report.closed, "summary": report.summary(for_person=True)}


@api.get("/next")
def api_next(request: Request, conn: Conn, company: str | None = None) -> dict[str, Any]:
    owner = _company(conn, request, company)
    item = actions.next_item(conn, owner.id)
    if item is None:
        return {"done": True, "card": None}
    return {"done": False, "card": _json_safe(queue_cards.card(conn, item))}


@api.get("/items/{item_id}")
def api_item(conn: Conn, item_id: UUID) -> dict[str, Any]:
    _item_or_404(conn, item_id)
    return _json_safe(actions.card(conn, item_id))


@api.get("/items/{item_id}/documents")
def api_documents(conn: Conn, item_id: UUID, q: str | None = None) -> list[dict[str, Any]]:
    item = _item_or_404(conn, item_id)
    if item["subject_type"] != "transaction":
        return []
    tx = conn.execute("SELECT amount_minor, counterparty_id FROM l1_bank_transaction WHERE id = %s",
                      (item["subject_id"],)).fetchone()
    return _json_safe(cs.search_documents(conn, item["company_id"], query=q or None,
                                          counterparty_id=None if q else tx[1],
                                          direction=None if q else ("sales" if tx[0] > 0 else "purchase")))


@api.post("/items/{item_id}/answer")
def api_answer(request: Request, conn: Conn, item_id: UUID, body: AnswerIn) -> dict[str, Any]:
    return _do(request, conn, item_id, body.action, decided_by=body.decided_by, suggestion_id=body.suggestion_id,
               extra=body.extra, reason=body.reason, until=body.until)


@api.post("/items/{item_id}/accept")
def api_accept(request: Request, conn: Conn, item_id: UUID, body: AnswerIn) -> dict[str, Any]:
    return _do(request, conn, item_id, "accept" if body.action == "accept" else body.action,
               decided_by=body.decided_by, suggestion_id=body.suggestion_id, extra=body.extra, reason=body.reason)


@api.post("/items/{item_id}/reject")
def api_reject(request: Request, conn: Conn, item_id: UUID, body: AnswerIn) -> dict[str, Any]:
    return _do(request, conn, item_id, "reject", decided_by=body.decided_by, suggestion_id=body.suggestion_id,
               reason=body.reason)


@api.post("/items/{item_id}/defer")
def api_defer(request: Request, conn: Conn, item_id: UUID, body: AnswerIn) -> dict[str, Any]:
    return _do(request, conn, item_id, "defer", decided_by=body.decided_by, until=body.until)


@api.post("/items/{item_id}/other")
def api_other(request: Request, conn: Conn, item_id: UUID, body: OtherIn) -> dict[str, Any]:
    extra = {"kind": body.kind, "targets": body.targets, "subtype": body.subtype, "shares": body.shares,
             "category": body.category}
    return _do(request, conn, item_id, "other", decided_by=body.decided_by, extra=extra, reason=body.reason)


@api.post("/items/{item_id}/bulk")
def api_bulk(request: Request, conn: Conn, item_id: UUID, body: AnswerIn) -> dict[str, Any]:
    return _do(request, conn, item_id, "bulk", decided_by=body.decided_by, extra=body.extra, reason=body.reason)


@api.post("/undo")
def api_undo(request: Request, conn: Conn, body: UndoIn) -> dict[str, Any]:
    who = body.decided_by or request.app.state.user
    try:
        if body.batch_id:
            return actions.undo_batch(conn, body.batch_id, decided_by=who, reason=body.reason)
        if body.decision_id:
            return actions.undo(conn, body.decision_id, decided_by=who, reason=body.reason)
    except ERRORS as exc:
        raise HTTPException(422, str(exc)) from None
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    raise HTTPException(422, "podaj decision_id albo batch_id")


@api.get("/history")
def api_history(request: Request, conn: Conn, company: str | None = None, limit: int = 50,
                auto: bool = False) -> list[dict[str, Any]]:
    owner = _company(conn, request, company)
    keys = ("id", "kind", "subject_type", "subject_id", "decided_by", "decided_at", "method", "undo_until",
            "batch_id", "reason", "label", "can_undo", "in_toast_window", "revoked_by")
    return [_json_safe({k: r[k] for k in keys}) for r in actions.history(conn, owner.id, limit=limit,
                                                                         include_auto=auto)]


@api.get("/stats")
def api_stats(request: Request, conn: Conn, company: str | None = None) -> dict[str, Any]:
    owner = _company(conn, request, company)
    return _json_safe(actions.stats(conn, owner.id))
