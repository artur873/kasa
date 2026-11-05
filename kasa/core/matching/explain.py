"""``match explain``: the chain of one transfer - facts -> signals -> candidates -> suggestions -> decision.

The candidates are evaluated again now (nothing is written), as if the transfer were undecided,
so the explanation shows also why a decided transfer was decided the way it was.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from kasa.core.matching import auto
from kasa.core.matching.engine import evaluate, money
from kasa.core.matching.weights import badge


def find_transaction(conn: psycopg.Connection, ref: str, company_id: UUID | None = None) -> UUID:
    """A booked current transaction by entry reference or id (prefix)."""
    rows = conn.execute(
        """
        SELECT id FROM l1_bank_transaction
         WHERE superseded_by IS NULL AND (entry_reference = %s OR id::text LIKE %s || '%%')
           AND (%s::uuid IS NULL OR company_id = %s::uuid)
        """,
        (ref, ref.lower(), company_id, company_id),
    ).fetchall()
    if len(rows) != 1:
        raise LookupError(f"Przelew {ref!r}: " + ("nie ma takiego" if not rows else "niejednoznaczny"))
    return rows[0][0]


def explain(conn: psycopg.Connection, transaction_id: UUID) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        tx = cur.execute(
            """
            SELECT t.*, t.op_type::text AS op_type, a.iban AS account_iban, a.kind::text AS account_kind,
                   c.name AS cp_name, c.nip AS cp_nip, c.kind::text AS cp_kind, s.intent, s.entities, s.model
              FROM l1_bank_transaction t JOIN l1_bank_account a ON a.id = t.account_id
              LEFT JOIN l1_counterparty c ON c.id = t.counterparty_id
              LEFT JOIN title_semantics_current s ON s.transaction_id = t.id
             WHERE t.id = %s
            """,
            (transaction_id,),
        ).fetchone()
        company_id = tx["company_id"]
        subject = transaction_id
        if tx["mpp_group_id"] and tx["mpp_role"] == "vat_leg":
            main = cur.execute(
                "SELECT id FROM l1_bank_transaction WHERE mpp_group_id = %s AND mpp_role = 'main' "
                "AND superseded_by IS NULL", (tx["mpp_group_id"],)).fetchone()
            subject = main["id"] if main else transaction_id
        suggestions = cur.execute(
            """
            SELECT id, rank, status::text AS status, score, proposal, evidence, created_at, resolved_at,
                   resolved_by_decision
              FROM l2_suggestion
             WHERE kind = 'match' AND subject_type = 'transaction' AND subject_id = %s
             ORDER BY created_at, rank
            """,
            (subject,),
        ).fetchall()
        statuses = cur.execute(
            """
            SELECT final_status::text AS final_status, subtype, awaiting_until, status::text AS status, method::text
                   AS method, rule_id, rule_version, decision_id, created_by, created_at
              FROM l3_transaction_status WHERE transaction_id = %s ORDER BY created_at
            """,
            (transaction_id,),
        ).fetchall()
        matches = cur.execute(
            """
            SELECT m.document_id, d.number_raw, m.internal_record_id, m.related_transaction_id, r.entry_reference,
                   m.amount_minor, m.document_amount_minor, m.difference_kind::text AS difference_kind,
                   m.difference_minor, m.group_id, m.status::text AS status, m.method::text AS method,
                   m.evidence_only, m.evidence ->> 'label' AS label
              FROM l3_match m LEFT JOIN l1_document d ON d.id = m.document_id
              LEFT JOIN l1_bank_transaction r ON r.id = m.related_transaction_id
             WHERE m.transaction_id = %s ORDER BY m.created_at
            """,
            (transaction_id,),
        ).fetchall()
        decisions = cur.execute(
            """
            SELECT id, kind::text AS kind, method::text AS method, decided_by, decided_at, rule_id, rule_version,
                   reason, reverts_decision_id
              FROM l3_decision_log WHERE subject_type = 'transaction' AND subject_id = %s ORDER BY decided_at
            """,
            (subject,),
        ).fetchall()
    evaluation = evaluate(conn, company_id, subject_ids={subject}, include_decided=True)
    result = evaluation.results[0] if evaluation.results else None
    eligible = auto.eligibility(result.best, evaluation.params, evaluation.ctx.builtin_rules) if result else (False, "")
    return {"transaction": tx, "subject_id": subject, "result": result, "params": evaluation.params,
            "auto": eligible, "suggestions": suggestions, "statuses": statuses, "matches": matches,
            "decisions": decisions}


def render(data: dict[str, Any]) -> str:
    tx, result = data["transaction"], data["result"]
    out = []
    out.append(f"PRZELEW {tx['entry_reference'] or tx['id']} ({tx['account_kind']} {tx['account_iban']})")
    out.append("1. Fakty")
    out.append(f"   data {tx['booking_date']} (operacja {tx['transaction_date'] or '-'}), kwota "
               f"{money(tx['amount_minor'], tx['currency'].strip())}"
               + (f", oryginalnie {money(tx['original_amount_minor'], tx['original_currency'])}"
                  if tx["original_currency"] else ""))
    out.append(f"   kontrahent: {tx['counterparty_name_raw'] or '-'} ({tx['cp_kind'] or 'nieznany'}"
               + (f", NIP {tx['cp_nip']}" if tx["cp_nip"] else "") + ")"
               + (f", IBAN {tx['counterparty_iban_raw']}" if tx["counterparty_iban_raw"] else ""))
    out.append(f"   tytuł: {tx['title_raw'] or '-'}")
    out.append(f"   typ operacji: {tx['op_type']}" + (f", split payment: {tx['mpp_role']}" if tx["mpp_role"] else "")
               + (", SŁOWO WRAŻLIWE" if tx["sensitive"] else ""))
    extracted = {k: v for k, v in (tx["extracted"] or {}).items() if k not in ("op_type_reason", "extractor_version")}
    if extracted:
        parts = []
        for key in ("invoice_numbers", "ksef_numbers", "mpp", "tax", "periods", "dates", "phones", "operator"):
            if key in extracted:
                value = extracted[key]
                if key == "invoice_numbers":
                    value = [i.get("raw") for i in value]
                parts.append(f"{key}={value}")
        out.append("   kod wyłuskał: " + "; ".join(parts))
    if tx["intent"]:
        out.append(f"   semantyka tytułu (model {tx['model']}): intencja {tx['intent']}")
    if data["subject_id"] != tx["id"]:
        out.append(f"   noga VAT split payment - rozstrzyga noga główna {data['subject_id']}")
    out.append("2-3. Sygnały i kandydaci (przeliczone teraz, bez zapisu; "
               f"temperatura {data['params']['temperature']}, próg {data['params']['auto_threshold']})")
    if result is None:
        out.append("   brak - przelew nie jest podmiotem dopasowania")
    else:
        if len(result.group) > 1:
            out.append("   zdarzenie split payment: " + ", ".join(
                f"{t.label} ({t.mpp_role}, {money(t.amount_minor)})" for t in result.group)
                + f" = {money(result.amount_minor)}")
        ranked = {id(c): n for n, c in enumerate(result.candidates, start=1)}
        shown = list(result.considered) + [c for c in result.candidates if c not in result.considered]
        for cand in sorted(shown, key=lambda c: (id(c) not in ranked, ranked.get(id(c), 0), -c.score)):
            mark = f"#{ranked[id(cand)]}" if id(cand) in ranked else "  "
            items = ", ".join(f"{i.label} {money(i.amount_minor)}"
                              + (f" [{i.difference_kind} {money(i.difference_minor)}]" if i.difference_kind else "")
                              for i in cand.items) or "-"
            out.append(f"   {mark} {cand.final_status}{'/' + cand.subtype if cand.subtype else ''}: {items}  "
                       f"surowy {cand.raw:.2f} -> p={cand.score:.4f}, twardy={'tak' if cand.hard else 'nie'}, "
                       f"odznaka {badge(cand.score, cand.hard)}")
            for s in cand.signals:
                out.append(f"        {s.weight:+.1f} {s.tag}: {s.fact_pl}")
            for blocker in cand.blockers:
                out.append(f"        ! {blocker}")
        if result.candidates:
            ok, why = data["auto"]
            out.append(f"   automat: {'TAK' if ok else 'nie'} - {why}")
    out.append("4. Sugestie (l2_suggestion)")
    if not data["suggestions"]:
        out.append("   brak")
    for s in data["suggestions"]:
        ev = s["evidence"] or {}
        out.append(f"   [{s['status']}] #{s['rank']} p={float(s['score'] or 0):.4f} {ev.get('badge')} "
                   f"{s['proposal'].get('final_status')}/{s['proposal'].get('subtype') or '-'} "
                   f"({s['created_at']:%Y-%m-%d %H:%M}) - " + "; ".join(ev.get("facts") or []))
    out.append("5. Decyzja (L3)")
    if not data["statuses"] and not data["decisions"]:
        out.append("   brak - przelew czeka w kolejce")
    for d in data["decisions"]:
        out.append(f"   dziennik: {d['kind']} {d['method']} {d['decided_by']} {d['decided_at']:%Y-%m-%d %H:%M}"
                   + (f" reguła {d['rule_id']}@{d['rule_version']}" if d["rule_id"] else "")
                   + (f" - {d['reason']}" if d["reason"] else ""))
    for s in data["statuses"]:
        out.append(f"   status [{s['status']}]: {s['final_status']}{'/' + s['subtype'] if s['subtype'] else ''}"
                   + (f", czeka do {s['awaiting_until']}" if s["awaiting_until"] else "") + f" ({s['method']})")
    for m in data["matches"]:
        target = m["number_raw"] or (f"przelew {m['entry_reference']}" if m["related_transaction_id"] else
                                     f"wiersz {m.get('label') or m['internal_record_id']}")
        if m.get("evidence_only"):
            out.append(f"   dowód [{m['status']}]: {target} - ten sam koszt co dokument grupy (bez kwoty) "
                       f"grupa {str(m['group_id'])[:8]}")
            continue
        out.append(f"   dopasowanie [{m['status']}]: {target} {money(m['amount_minor'])}"
                   + (f" [{m['difference_kind']} {money(m['difference_minor'])}]" if m["difference_kind"] else "")
                   + f" grupa {str(m['group_id'])[:8]}")
    return "\n".join(out)
