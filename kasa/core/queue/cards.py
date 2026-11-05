"""What a card of the queue shows (briefing, step 7) - plain data for the HTML panel, the API and the CLI.

* the subject in the open (role owner): amount, counterparty, date, title;
* 1-3 proposals as buttons, under each its evidence in facts (``evidence.facts``) and a badge
  high / medium / low with the number of precedents - NEVER a percentage (decision D8);
* the model's narration (summary of the semantics) only under "Szczegóły";
* no candidate: the first proposal is "bez dokumentu: …"; a tie: "niejednoznaczne" with EVERY
  equivalent invoice (review of 4A/4B, point 10 - computed here from the open invoices of the
  counterparty with exactly this open amount, not only the two the engine kept);
* a homogeneous batch (same rule/tag, same counterparty, all high) is offered as one button,
  every item still its own decision;
* cards of a cost object candidate, of a rule candidate, of a rule ready for auto and of an audit
  sample ("audyt");
* a sentinel looks exactly like a match card (nothing tells it apart).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from kasa.core.matching.engine import money
from kasa.core.rules.facts import subject_facts

BADGE_PL = {"high": "wysoka", "medium": "średnia", "low": "niska"}
STATUS_PL = {"matched": "dopasowany", "no_document": "bez dokumentu", "own_transfer": "transfer własny",
             "to_clarify": "do wyjaśnienia"}
SUBTYPE_PL = {"zus": "ZUS", "tax": "podatek (US)", "fee": "prowizja / opłata", "interest": "odsetki",
              "advance": "zaliczka (czeka 180 dni)", "deposit": "kaucja", "card_awaiting": "karta - czeka na fakturę",
              "salary": "wynagrodzenie", "refund": "zwrot", "duplicate": "duplikat", "other": "inne"}
RECORD_PL = {"document_line": "pozycja faktury", "document": "dokument", "transaction": "przelew",
             "internal_record": "wiersz arkusza", "email_case": "sprawa z maila"}
SHOWN_KIND = {"sentinel": "match"}


def _suggestions(conn: psycopg.Connection, ids: list[UUID]) -> list[dict[str, Any]]:
    if not ids:
        return []
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            """
            SELECT id, kind::text AS kind, subject_type::text AS subject_type, subject_id, proposal, rank,
                   status::text AS status, evidence
              FROM l2_suggestion WHERE id = ANY(%s)
            """,
            (list(ids),),
        ).fetchall()
    by_id = {r["id"]: r for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def _document_label(conn: psycopg.Connection, document_id: str) -> str:
    row = conn.execute(
        """
        SELECT number_raw, CASE WHEN direction = 'purchase' THEN seller_name ELSE buyer_name END, gross_minor,
               currency, issue_date
          FROM l1_document WHERE id = %s
        """,
        (document_id,),
    ).fetchone()
    if row is None:
        return "dokument"
    number, party, gross, currency, issued = row
    return f"{number or 'bez numeru'} · {party or '-'} · {money(int(gross or 0), currency.strip())} · {issued}"


def _item_label(conn: psycopg.Connection, item: dict[str, Any]) -> str:
    if item.get("document_id"):
        text = _document_label(conn, item["document_id"])
    elif item.get("internal_record_id"):
        row = conn.execute("SELECT cost_object_ref, record_date, amount_minor, row_no FROM l1_internal_record "
                           "WHERE id = %s", (item["internal_record_id"],)).fetchone()
        text = (f"arkusz, wiersz {row[3]}: {row[0] or '-'} {row[1] or ''} · {money(int(row[2] or 0))}"
                if row else "wiersz arkusza")
    elif item.get("related_transaction_id"):
        row = conn.execute("SELECT booking_date, amount_minor, coalesce(title_raw, '') FROM l1_bank_transaction "
                           "WHERE id = %s", (item["related_transaction_id"],)).fetchone()
        text = f"przelew {row[0]} · {money(int(row[1]))} · {row[2][:40]}" if row else "przelew"
    else:
        text = "?"
    if item.get("difference_kind"):
        text += f" (różnica {item['difference_kind']}: {money(int(item.get('difference_minor') or 0))})"
    return text


def proposal_label(conn: psycopg.Connection, kind: str, proposal: dict[str, Any], evidence: dict[str, Any]) -> str:
    """The text of the button."""
    if kind == "match":
        status = proposal.get("final_status")
        if status == "matched":
            items = proposal.get("items") or []
            labels = [_item_label(conn, i) for i in items]
            head = f"{len(items)} dokumenty: " if len(items) > 1 else ""
            return head + " + ".join(labels)
        if status == "no_document":
            return f"bez dokumentu: {SUBTYPE_PL.get(proposal.get('subtype'), proposal.get('subtype') or '?')}"
        if status == "to_clarify":
            tie = "tie" in (evidence.get("tags") or []) or (evidence.get("detail") or {}).get("tie")
            return "niejednoznaczne (remis) - do wyjaśnienia" if tie else "do wyjaśnienia"
        return STATUS_PL.get(status, status or "?")
    if kind == "allocation":
        shares = proposal.get("shares") or []
        text = " · ".join(f"{s.get('cost_object_name') or '?'} {round(float(s.get('share') or 0) * 100, 2):g}%"
                          for s in shares)
        return text + (" (częściowa - uzupełnij)" if evidence.get("partial") else "")
    if kind == "category":
        if proposal.get("needs_human"):
            return "nadaj kategorię"
        return f"kategoria: {proposal.get('category')}"
    if kind == "duplicate":
        return f"to duplikat {proposal.get('number') or ''}".strip()
    return proposal.get("name") or proposal.get("question") or kind


def _subject(conn: psycopg.Connection, company_id: UUID, subject_type: str, subject_id: UUID) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        if subject_type == "transaction":
            r = cur.execute(
                """
                SELECT t.booking_date, t.amount_minor, t.currency, t.counterparty_name_raw, t.title_raw,
                       t.op_type::text AS op_type, a.iban AS account_iban, t.sensitive, t.entry_reference
                  FROM l1_bank_transaction t JOIN l1_bank_account a ON a.id = t.account_id WHERE t.id = %s
                """,
                (subject_id,),
            ).fetchone()
            return {"type": "transaction", "amount": money(int(r["amount_minor"]), r["currency"].strip()),
                    "amount_minor": int(r["amount_minor"]), "inflow": r["amount_minor"] > 0,
                    "direction": "wpływ" if r["amount_minor"] > 0 else "wypływ", "date": r["booking_date"],
                    "counterparty": r["counterparty_name_raw"] or "-", "title": r["title_raw"] or "(bez tytułu)",
                    "op_type": r["op_type"], "account": r["account_iban"], "sensitive": r["sensitive"],
                    "reference": r["entry_reference"]}
        if subject_type in ("document", "line"):
            facts = subject_facts(conn, company_id, subject_type, subject_id)
            doc = cur.execute(
                """
                SELECT d.number_raw, d.kind::text AS kind, d.direction::text AS direction, d.issue_date, d.sale_date,
                       d.seller_name, d.buyer_name, d.gross_minor, d.currency
                  FROM l1_document d WHERE d.id = %s
                """,
                (facts.get("document_id"),),
            ).fetchone()
            lines = cur.execute("SELECT name, coalesce(gross_minor, net_minor) AS amount FROM l1_document_line "
                                "WHERE document_id = %s ORDER BY line_no", (facts.get("document_id"),)).fetchall()
            return {"type": subject_type, "amount": money(int(facts.get("amount_minor") or 0)),
                    "amount_minor": int(facts.get("amount_minor") or 0), "inflow": doc["direction"] == "sales",
                    "direction": "sprzedaż" if doc["direction"] == "sales" else "zakup",
                    "date": doc["sale_date"] or doc["issue_date"], "counterparty": facts.get("counterparty_name") or "-",
                    "title": facts.get("title") if subject_type == "line" else doc["number_raw"],
                    "document": f"{doc['number_raw']} ({doc['kind']}) · {money(int(doc['gross_minor'] or 0), doc['currency'].strip())}",
                    "lines": [f"{ln['name']} - {money(int(ln['amount'] or 0), doc['currency'].strip())}" for ln in lines]}
        if subject_type == "internal_record":
            r = cur.execute("SELECT fields, record_date, amount_minor, cost_object_ref FROM l1_internal_record "
                            "WHERE id = %s", (subject_id,)).fetchone()
            return {"type": "internal_record", "amount": money(int(r["amount_minor"] or 0)),
                    "amount_minor": int(r["amount_minor"] or 0), "inflow": False, "direction": "wiersz arkusza",
                    "date": r["record_date"], "counterparty": (r["fields"] or {}).get("osoba") or "-",
                    "title": r["cost_object_ref"] or "", "fields": r["fields"] or {}}
        if subject_type == "cost_object":
            r = cur.execute("SELECT name, attrs, status::text AS status FROM l3_cost_object WHERE id = %s",
                            (subject_id,)).fetchone()
            return {"type": "cost_object", "title": r["name"], "attrs": r["attrs"], "status": r["status"]}
        if subject_type == "rule":
            r = cur.execute("SELECT rule_key, version, stage::text AS stage, question FROM l3_rule WHERE id = %s",
                            (subject_id,)).fetchone()
            return {"type": "rule", "title": r["question"] or r["rule_key"], "rule_key": r["rule_key"],
                    "version": r["version"], "stage": r["stage"]}
    return {"type": subject_type}


def _narration(conn: psycopg.Connection, subject_type: str, subject_id: UUID) -> str | None:
    if subject_type not in ("transaction", "line", "internal_record", "document"):
        return None
    row = conn.execute(
        """
        SELECT summary FROM l1_semantics WHERE subject_type = %s AND subject_id = %s AND superseded_by IS NULL
           AND summary IS NOT NULL ORDER BY created_at DESC LIMIT 1
        """,
        (subject_type, subject_id),
    ).fetchone()
    return row[0] if row else None


def tie_documents(conn: psycopg.Connection, transaction_id: UUID) -> list[dict[str, Any]]:
    """Every open invoice of the counterparty with exactly the transfer's amount open (all equivalents of a tie)."""
    from kasa.core.rules.facts import canonical_counterparty

    tx = conn.execute("SELECT company_id, amount_minor, counterparty_id FROM l1_bank_transaction WHERE id = %s",
                      (transaction_id,)).fetchone()
    if tx is None or tx[2] is None:
        return []
    company_id, amount, party = tx
    canonical = canonical_counterparty(conn, party)
    rows = conn.execute(
        """
        SELECT d.id, d.number_raw, d.issue_date, d.due_date, d.gross_minor - coalesce((
                   SELECT sum(coalesce(m.document_amount_minor, m.amount_minor)) FROM l3_match m
                     JOIN document_lineage l ON l.document_id = m.document_id
                    WHERE m.status = 'active' AND l.head_id = d.id), 0) AS open_minor
          FROM l1_document d
         WHERE d.company_id = %s AND d.superseded_by IS NULL AND d.currency = 'PLN'
           AND d.direction = %s AND d.counterparty_id IN (%s, %s)
         ORDER BY d.issue_date, d.number_raw
        """,
        (company_id, "sales" if amount > 0 else "purchase", party, canonical),
    ).fetchall()
    return [{"document_id": str(r[0]), "number": r[1], "issue_date": r[2], "due_date": r[3],
             "amount": money(int(r[4]))} for r in rows if int(r[4]) == abs(int(amount))]


def _batch(conn: psycopg.Connection, item: dict[str, Any]) -> dict[str, Any] | None:
    if not item.get("batch_key"):
        return None
    rows = conn.execute(
        """
        SELECT suggestion_ids[1], amount_minor FROM queue_state
         WHERE company_id = %s AND batch_key = %s AND closed_at IS NULL AND badge = 'high'
           AND card_kind = %s AND array_length(suggestion_ids, 1) >= 1
           AND (deferred_until IS NULL OR deferred_until <= now())
         ORDER BY position
        """,
        (item["company_id"], item["batch_key"], item["card_kind"]),
    ).fetchall()
    if len(rows) < 2:
        return None
    kind, rule, party, _outcome = item["batch_key"].split("|", 3)
    name = conn.execute("SELECT name FROM l1_counterparty WHERE id = %s", (party,)).fetchone()
    total = sum(int(r[1] or 0) for r in rows)
    noun = {"match": "przelewów", "allocation": "alokacji", "category": "kategorii"}.get(kind, "pozycji")
    return {"key": item["batch_key"], "count": len(rows), "amount": money(total), "amount_minor": total,
            "suggestion_ids": [str(r[0]) for r in rows], "rule": rule,
            "label": f"Paczka: {len(rows)} {noun} {name[0] if name else ''}, {money(total)}, {rule} - wszystkie „wysoka”"}


def strength_pl(value: float) -> str:
    """A link of the pair model as a word (the probability stays in the database - decision D8)."""
    return "mocne" if value >= 0.9 else ("średnie" if value >= 0.7 else "słabe")


def _candidate(conn: psycopg.Connection, company_id: UUID, proposal: dict[str, Any],
               cost_object_id: UUID | None = None) -> dict[str, Any]:
    member_ids = {(r[0], str(r[1])): str(r[2]) for r in conn.execute(
        "SELECT record_type::text, record_id, id FROM l2_cost_object_member WHERE candidate_id = %s "
        "AND status IN ('open', 'accepted')", (cost_object_id,)).fetchall()} if cost_object_id else {}
    members = []
    for m in [{"record_type": (proposal.get("anchor") or {}).get("record_type"),
               "record_id": (proposal.get("anchor") or {}).get("record_id"), "link_strength": 1.0, "anchor": True},
              *(proposal.get("members") or [])]:
        if not m.get("record_id"):
            continue
        subject_type = {"document_line": "line"}.get(m["record_type"], m["record_type"])
        facts = subject_facts(conn, company_id, subject_type, m["record_id"]) if subject_type in (
            "line", "document", "transaction", "internal_record") else {}
        members.append({"type": RECORD_PL.get(m["record_type"], m["record_type"]), "record_type": m["record_type"],
                        "member_id": member_ids.get((m["record_type"], str(m["record_id"]))),
                        "record_id": m["record_id"], "label": (facts.get("label") or "")[:90],
                        "amount": money(int(facts.get("amount_minor") or 0)) if facts else "",
                        "strength": strength_pl(float(m.get("link_strength") or 0)), "anchor": bool(m.get("anchor")),
                        "_strength": float(m.get("link_strength") or 0)})
    weakest = min(members, key=lambda m: m["_strength"]) if members else None
    for m in members:
        m.pop("_strength", None)
    attrs = proposal.get("attrs") or {}
    others = conn.execute(
        """
        SELECT id, name, status::text FROM l3_cost_object
         WHERE company_id = %s AND status IN ('candidate', 'approved') AND system_code IS NULL AND id IS DISTINCT FROM %s
         ORDER BY name
        """,
        (company_id, cost_object_id),
    ).fetchall()
    return {"name": proposal.get("name"), "client": attrs.get("client"), "date_start": attrs.get("date_start"),
            "date_end": attrs.get("date_end"), "city": attrs.get("city"), "members": members,
            "weakest_link": f"{weakest['type']}: {weakest['label'][:60]} ({weakest['strength']})" if weakest else None,
            "revenue": money(int(proposal.get("revenue_minor") or 0)),
            "cost": money(int(proposal.get("cost_minor") or 0)),
            "others": [{"id": str(r[0]), "name": r[1], "status": r[2]} for r in others]}


def card(conn: psycopg.Connection, item: dict[str, Any]) -> dict[str, Any]:
    """The card of one queue item (a row of ``queue_state``)."""
    kind = item["card_kind"]
    company_id = item["company_id"]
    out: dict[str, Any] = {
        "id": str(item["id"]), "kind": SHOWN_KIND.get(kind, kind), "position": item.get("position"),
        "rank_reason": item["rank_reason"], "badge": item.get("badge"),
        "badge_pl": BADGE_PL.get(item.get("badge"), "-"), "label": "audyt" if kind == "audit" else None,
        "subject_type": item["subject_type"], "subject_id": str(item["subject_id"]),
        "subject": _subject(conn, company_id, item["subject_type"], item["subject_id"]),
        "proposals": [], "details": {}, "tie_documents": [], "batch": None,
    }
    suggestions = _suggestions(conn, list(item.get("suggestion_ids") or []))
    for s in suggestions:
        evidence = s["evidence"] or {}
        out["proposals"].append({
            "suggestion_id": str(s["id"]), "rank": s["rank"], "kind": s["kind"],
            "label": proposal_label(conn, s["kind"], s["proposal"] or {}, evidence),
            "facts": list(evidence.get("facts") or []), "badge": evidence.get("badge"),
            "badge_pl": BADGE_PL.get(evidence.get("badge"), "-"), "precedents": int(evidence.get("precedents") or 0),
            "partial": bool(evidence.get("partial")), "proposal": s["proposal"],
        })
    if out["proposals"]:
        out["precedents"] = out["proposals"][0]["precedents"]
        out["badge"] = out["badge"] or out["proposals"][0]["badge"]
        out["badge_pl"] = BADGE_PL.get(out["badge"], "-")
    if not (out["subject"].get("sensitive")):
        narration = _narration(conn, item["subject_type"], item["subject_id"])
        if narration:
            out["details"]["narration"] = narration
    if kind in ("match", "sentinel") and suggestions:
        top = suggestions[0]
        tags = (top["evidence"] or {}).get("tags") or []
        if (top["proposal"] or {}).get("final_status") == "to_clarify" and "tie" in tags:
            out["tie_documents"] = tie_documents(conn, item["subject_id"])
    if kind != "sentinel":
        out["batch"] = _batch(conn, item)
    if kind == "cost_object_candidate" and suggestions:
        out["candidate"] = _candidate(conn, company_id, suggestions[0]["proposal"] or {}, item["subject_id"])
    if kind == "rule_candidate" and suggestions:
        p = suggestions[0]["proposal"] or {}
        out["rule"] = {"question": p.get("question"), "hits": p.get("hits"), "total": p.get("total"),
                       "counterparties": p.get("counterparties"), "names": p.get("counterparty_names") or [],
                       "examples": p.get("examples") or [], "exceptions": p.get("exceptions") or [],
                       "months": p.get("months") or [], "pattern": p.get("pattern"), "action": p.get("action"),
                       "rule_key": p.get("rule_key"),
                       "summary": f"{p.get('hits')} z {p.get('total')} u {p.get('counterparties')} kontrahenta"}
    if kind == "rule_ready":
        row = conn.execute(
            "SELECT rule_key, version, observations, agreed, disagreed, wilson_lower, question, action FROM "
            "rule_stage_effective WHERE rule_id = %s", (item["subject_id"],)).fetchone()
        if row:
            out["rule"] = {"rule_key": row[0], "version": row[1], "observations": row[2], "agreed": row[3],
                           "disagreed": row[4], "wilson_lower": float(row[5]), "question": row[6], "action": row[7],
                           "summary": f"{row[3]} zgodnych z {row[2]}, {row[4]} niezgodnych"}
    if kind == "audit":
        out["audit"] = _audit(conn, item)
    return out


def _audit(conn: psycopg.Connection, item: dict[str, Any]) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT s.month, d.kind::text, d.payload, d.decided_by, d.decided_at, d.suggestion_id, r.rule_key
          FROM l2_audit_sample s JOIN l3_decision_log d ON d.id = s.decision_id
          LEFT JOIN l3_rule r ON r.id = s.rule_id WHERE s.id = %s
        """,
        (item["audit_sample_id"],),
    ).fetchone()
    if row is None:
        return {}
    month, kind, payload, decided_by, decided_at, suggestion_id, rule_key = row
    facts: list[str] = []
    if suggestion_id:
        evidence = conn.execute("SELECT evidence FROM l2_suggestion WHERE id = %s", (suggestion_id,)).fetchone()
        facts = list(((evidence[0] if evidence else None) or {}).get("facts") or [])
    label = proposal_label(conn, kind if kind in ("match", "allocation", "category") else "other", payload or {}, {})
    if kind == "allocation":
        names = {str(r[0]): r[1] for r in conn.execute(
            "SELECT id, name FROM l3_cost_object WHERE id = ANY(%s::uuid[])",
            ([s["cost_object_id"] for s in (payload or {}).get("shares") or []],)).fetchall()}
        label = " · ".join(f"{names.get(s['cost_object_id'], '?')} {round(float(s['share']) * 100, 2):g}%"
                           for s in (payload or {}).get("shares") or [])
    return {"month": month, "kind": kind, "decision": label, "decided_by": decided_by, "decided_at": decided_at,
            "rule_key": rule_key, "facts": facts, "sample_id": str(item["audit_sample_id"])}
