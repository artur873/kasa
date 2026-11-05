"""``python -m kasa queue|rules|audit ...`` - the review queue, learned rules and the audit of the automation."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import date
from typing import Any
from uuid import UUID

import psycopg

from kasa.core.companies import Company, get_company
from kasa.core.db import connect
from kasa.core.decisions import DecisionError
from kasa.core.matching.engine import money
from kasa.core.queue import actions, build
from kasa.core.rules import audit, lifecycle, mine
from kasa.core.rules.simulate import simulate


def _company(conn: psycopg.Connection, slug: str) -> Company:
    return get_company(conn, slug)


def _who(args: argparse.Namespace) -> str:
    return getattr(args, "by", None) or getpass.getuser()


def _resolve(conn: psycopg.Connection, table: str, text: str, company_id: UUID | None = None) -> UUID:
    try:
        return UUID(text)
    except ValueError:
        pass
    if len(text) < 6:
        raise LookupError(f"Za krótki identyfikator {text!r} (min. 6 znaków)")
    rows = conn.execute(
        f"SELECT id FROM {table} WHERE id::text LIKE %s AND (%s::uuid IS NULL OR company_id = %s::uuid) LIMIT 2",
        (text.lower() + "%", company_id, company_id),
    ).fetchall()
    if len(rows) != 1:
        raise LookupError(f"Identyfikator {text!r}: {'brak' if not rows else 'niejednoznaczny'}")
    return rows[0][0]


def _item(conn: psycopg.Connection, company: Company, ref: str) -> UUID:
    """A card: ``next``, its position in the queue (``3``) or its id (prefix)."""
    if ref == "next":
        item = actions.next_item(conn, company.id, mark_shown=False)
        if item is None:
            raise LookupError("Kolejka jest pusta")
        return item["id"]
    if ref.lstrip("#").isdigit():
        row = conn.execute("SELECT id FROM queue_state WHERE company_id = %s AND closed_at IS NULL AND position = %s",
                           (company.id, int(ref.lstrip("#")))).fetchone()
        if row is None:
            raise LookupError(f"Nie ma karty na pozycji {ref}")
        return row[0]
    return _resolve(conn, "queue_state", ref, company.id)


def _suggestion(conn: psycopg.Connection, item_id: UUID, ref: str | None) -> UUID | None:
    if ref is None:
        return None
    ids = conn.execute("SELECT suggestion_ids FROM queue_state WHERE id = %s", (item_id,)).fetchone()[0] or []
    if ref.isdigit() and 1 <= int(ref) <= len(ids):
        return ids[int(ref) - 1]
    return _resolve(conn, "l2_suggestion", ref)


def _json(value: str | None) -> Any:
    return json.loads(value) if value else None


def _run(fn, args: argparse.Namespace) -> int:
    try:
        return fn(args)
    except (DecisionError, actions.QueueError, lifecycle.RuleDecisionError, LookupError, PermissionError,
            ValueError) as exc:
        print(f"Błąd: {exc}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# queue
# ---------------------------------------------------------------------------


def cmd_build(args: argparse.Namespace) -> int:
    with connect(args.database_url) as conn:
        company = _company(conn, args.company)
        report = build.build(conn, company, mine_rules=not args.no_mine, min_months=args.min_months,
                             auto_rules=not args.no_auto)
        conn.commit()
    print(report.summary())
    return 0


def _print_card(card: dict[str, Any]) -> None:
    subject = card["subject"]
    label = f" [{card['label']}]" if card.get("label") else ""
    print(f"#{card['position']} {card['kind']}{label} - {card['rank_reason']}")
    if subject.get("amount"):
        print(f"  {subject.get('amount')} · {subject.get('direction', '')} · {subject.get('date', '')} · "
              f"{subject.get('counterparty', '')}")
    if subject.get("title"):
        print(f"  {subject['title']}")
    for p in card["proposals"]:
        print(f"  [{p['rank'] or '-'}] {p['label']}  (odznaka {p['badge_pl']}, precedensy {p['precedents']})")
        for fact in p["facts"][:6]:
            print(f"        · {fact}")
    for doc in card.get("tie_documents") or []:
        print(f"  równorzędna: {doc['number']} {doc['amount']} ({doc['issue_date']})")
    if card.get("batch"):
        print(f"  {card['batch']['label']}")
    if card.get("candidate"):
        c = card["candidate"]
        print(f"  {c['name']} · {c['client']} · {c['date_start']}..{c['date_end']} · {c['city']} · przychód "
              f"{c['revenue']} · koszt {c['cost']} · najsłabsze {c['weakest_link']}")
        for m in c["members"]:
            print(f"    {'kotwica ' if m['anchor'] else ''}{m['type']}: {m['label']} {m['amount']} ({m['strength']})")
    if card.get("rule"):
        r = card["rule"]
        print(f"  {r.get('question')} - {r.get('summary')}")
        for e in r.get("examples") or []:
            print(f"    przykład: {e['date']} {e['amount']} {e['label']}")
        for e in r.get("exceptions") or []:
            print(f"    wyjątek: {e['date']} {e['amount']} {e['label']} - {e['outcome']}")
    if card.get("audit"):
        a = card["audit"]
        print(f"  audyt {a['month']}: {a['decision']} ({a['decided_by']})")
    if card["details"].get("narration"):
        print(f"  Szczegóły: {card['details']['narration']}")


def cmd_list(args: argparse.Namespace) -> int:
    with connect(args.database_url) as conn:
        company = _company(conn, args.company)
        rows = actions.list_items(conn, company.id, include_deferred=args.all, limit=args.limit)
        for row in rows:
            deferred = f" (odłożona do {row['deferred_until']:%Y-%m-%d})" if row["deferred_until"] else ""
            print(f"#{row['position']:>3} {str(row['id'])[:8]} {row['card_kind']:<22} {row['badge'] or '-':<6} "
                  f"{row['rank_reason']}{deferred}")
        print(f"Kart: {len(rows)}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    with connect(args.database_url) as conn:
        company = _company(conn, args.company)
        card = actions.card(conn, _item(conn, company, args.item), mark_shown=not args.no_mark)
        conn.commit()
    if args.json:
        print(json.dumps(card, ensure_ascii=False, indent=2, default=str))
    else:
        _print_card(card)
    return 0


def _answer(args: argparse.Namespace, action: str, extra: dict[str, Any] | None = None, until=None) -> int:
    with connect(args.database_url) as conn:
        company = _company(conn, args.company)
        item = _item(conn, company, args.item)
        reply = actions.answer(conn, item, action, decided_by=_who(args),
                               suggestion_id=_suggestion(conn, item, getattr(args, "suggestion", None)),
                               extra=extra, reason=getattr(args, "reason", None), until=until)
        conn.commit()
    print(reply.get("message"))
    if reply.get("decision_ids"):
        print("Decyzje: " + ", ".join(str(d) for d in reply["decision_ids"]))
    if reply.get("batch_id"):
        print(f"Paczka: {reply['batch_id']} (cofnięcie: queue undo batch:{reply['batch_id']})")
    return 0


def cmd_accept(args: argparse.Namespace) -> int:
    extra = _json(args.extra) or {}
    if args.category:
        extra["category"] = args.category
    if args.shares:
        extra["shares"] = _json(args.shares)
    action = args.action or ("bulk" if args.bulk else "accept")
    return _answer(args, action, extra)


def cmd_reject(args: argparse.Namespace) -> int:
    return _answer(args, "reject")


def cmd_defer(args: argparse.Namespace) -> int:
    return _answer(args, "defer", until=date.fromisoformat(args.until) if args.until else None)


def cmd_other(args: argparse.Namespace) -> int:
    extra: dict[str, Any] = {"kind": args.kind}
    if args.document:
        targets = []
        for ref in args.document:
            kind, _, ident = ref.partition(":") if ":" in ref else ("document", "", ref)
            key = {"document": "document_id", "row": "internal_record_id", "internal_record": "internal_record_id",
                   "transaction": "related_transaction_id"}[kind]
            targets.append({key: ident})
        extra["targets"] = targets
    if args.subtype:
        extra["subtype"] = args.subtype
    if args.category:
        extra["category"] = args.category
    if args.share:
        extra["shares"] = [{"cost_object_id": s.split("=")[0], "share": s.split("=")[1]} for s in args.share]
    return _answer(args, "other", extra)


def cmd_undo(args: argparse.Namespace) -> int:
    with connect(args.database_url) as conn:
        if args.decision.startswith("batch:"):
            reply = actions.undo_batch(conn, UUID(args.decision.removeprefix("batch:")), decided_by=_who(args),
                                       reason=args.reason)
        else:
            reply = actions.undo(conn, _resolve(conn, "l3_decision_log", args.decision), decided_by=_who(args),
                                 reason=args.reason)
        conn.commit()
    print(reply["message"])
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    with connect(args.database_url) as conn:
        company = _company(conn, args.company)
        for row in actions.history(conn, company.id, limit=args.limit, include_auto=args.auto):
            state = "cofnięta" if row["revoked_by"] else ("można cofnąć" if row["can_undo"] else "")
            batch = f" paczka {str(row['batch_id'])[:8]}" if row["batch_id"] else ""
            print(f"{row['decided_at']:%Y-%m-%d %H:%M:%S} {str(row['id'])[:8]} {row['kind']:<11} {row['decided_by']:<12} "
                  f"{row['label'][:70]}{batch} {state}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    with connect(args.database_url) as conn:
        company = _company(conn, args.company)
        data = actions.stats(conn, company.id)
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str) if args.json else actions.render_stats(data))
    return 0


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------


def cmd_rules_list(args: argparse.Namespace) -> int:
    with connect(args.database_url) as conn:
        company = _company(conn, args.company)
        rows = conn.execute(
            """
            SELECT rule_key, version, stored_stage::text, effective_stage::text, observations, agreed, disagreed,
                   wilson_lower, ready_for_auto, question, automatic_decisions
              FROM rule_stage_effective WHERE company_id = %s ORDER BY (rule_key LIKE 'builtin:%%'), rule_key
            """,
            (company.id,),
        ).fetchall()
    for key, version, stored, effective, obs, agreed, disagreed, wilson, ready, question, autos in rows:
        if key.startswith("builtin:") and not args.all:
            continue
        stage = effective if effective == stored else f"{stored}→{effective}"
        print(f"{key:<45} v{version} {stage:<16} obserwacje {obs} (zgodne {agreed}, niezgodne {disagreed}, "
              f"Wilson {float(wilson):.3f}){' GOTOWA DO AUTO' if ready else ''} automat {autos}"
              + (f"\n    {question}" if question else ""))
    if not args.all:
        print("(reguły wbudowane: --all)")
    return 0


def cmd_rules_show(args: argparse.Namespace) -> int:
    with connect(args.database_url) as conn:
        company = _company(conn, args.company)
        rule_id = lifecycle.find_rule(conn, company.id, args.rule)
        rule = lifecycle.rule_row(conn, rule_id)
        info = lifecycle.state(conn, rule_id)
        versions = conn.execute("SELECT version, stage::text, method::text, created_at, suspended_reason FROM l3_rule "
                                "WHERE company_id = %s AND rule_key = %s ORDER BY version",
                                (company.id, rule["rule_key"])).fetchall()
    print(f"{rule['rule_key']} v{rule['version']} ({rule['stage_text']}, metoda {rule['method_text']})")
    if rule["question"]:
        print(f"  {rule['question']}")
    print(f"  wzorzec: {json.dumps(rule['pattern'], ensure_ascii=False)}")
    print(f"  akcja: {json.dumps(rule['action'], ensure_ascii=False)}")
    print(f"  zakres: {json.dumps(rule['scope'], ensure_ascii=False)}")
    print(f"  dowody: {rule['evidence_count']}, wyjątki: {len(rule['exceptions'] or [])}")
    if info:
        print(f"  etap efektywny: {info['effective_stage']}; obserwacje {info['observations']} (zgodne "
              f"{info['agreed']}, niezgodne {info['disagreed']}), Wilson {float(info['wilson_lower']):.3f}, "
              f"gotowa do auto: {'tak' if info['ready_for_auto'] else 'nie'}, ostatnie trafienie {info['last_hit_at']}")
    if rule["suspended_reason"]:
        print(f"  zawieszona: {rule['suspended_reason']}")
    if rule["post_mortem"]:
        print("  " + rule["post_mortem"].replace("\n", "\n  "))
    for version, stage, method, created, reason in versions:
        print(f"  wersja {version}: {stage} ({method}, {created:%Y-%m-%d})" + (f" - {reason}" if reason else ""))
    return 0


def _rule_action(args: argparse.Namespace) -> int:
    with connect(args.database_url) as conn:
        company = _company(conn, args.company)
        rule_id = lifecycle.find_rule(conn, company.id, args.rule)
        who, reason = _who(args), args.reason
        if args.rules_command == "accept":
            decision, new_id = lifecycle.accept(conn, rule_id, decided_by=who, reason=reason, source="cli")
        elif args.rules_command == "correct":
            decision, new_id = lifecycle.correct(conn, rule_id, decided_by=who, pattern=_json(args.pattern),
                                                 action=_json(args.action), scope=_json(args.scope),
                                                 question=args.question, reason=reason, source="cli")
        elif args.rules_command == "reject":
            decision = lifecycle.reject(conn, rule_id, decided_by=who, reason=reason, source="cli")
        elif args.rules_command == "enable-auto":
            decision = lifecycle.enable_auto(conn, rule_id, decided_by=who, reason=reason, force=args.force,
                                             source="cli")
        elif args.rules_command == "suspend":
            decision = lifecycle.suspend(conn, rule_id, decided_by=who, reason=reason or "decyzja człowieka",
                                         source="cli")
        else:
            decision = lifecycle.resume(conn, rule_id, decided_by=who, to_stage=args.to, reason=reason, source="cli")
        conn.commit()
    print(f"Zapisano decyzję {decision}")
    return 0


def cmd_rules_simulate(args: argparse.Namespace) -> int:
    with connect(args.database_url) as conn:
        company = _company(conn, args.company)
        print(simulate(conn, lifecycle.find_rule(conn, company.id, args.rule)).summary())
    return 0


def cmd_rules_mine(args: argparse.Namespace) -> int:
    with connect(args.database_url) as conn:
        company = _company(conn, args.company)
        report = mine.mine(conn, company, min_cases=args.min_cases, min_months=args.min_months,
                           dry_run=args.dry_run, created_by=_who(args))
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
    print(report.summary())
    if args.verbose:
        for entry in report.below_threshold:
            print(f"  poniżej progu: {entry}")
    return 0


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


def _previous_month() -> str:
    today = date.today()
    year, month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    return f"{year}-{month:02d}"


def cmd_audit_sample(args: argparse.Namespace) -> int:
    with connect(args.database_url) as conn:
        company = _company(conn, args.company)
        report = audit.sample(conn, company, args.month or _previous_month(), seed=args.seed)
        conn.commit()
    print(report.summary())
    return 0


def cmd_audit_list(args: argparse.Namespace) -> int:
    with connect(args.database_url) as conn:
        company = _company(conn, args.company)
        rows = audit.list_samples(conn, company.id, month=args.month, open_only=args.open)
    for r in rows:
        print(f"{str(r['id'])[:8]} {r['month']} {r['kind']:<10} {r['rule_key'] or '-':<45} "
              f"{money(int(r['amount_minor'] or 0)):>14} {r['verdict'] or 'do przeglądu'}")
    print(f"Próbek: {len(rows)}")
    return 0


def cmd_audit_verdict(args: argparse.Namespace) -> int:
    with connect(args.database_url) as conn:
        company = _company(conn, args.company)
        out = audit.verdict(conn, _resolve(conn, "l2_audit_sample", args.sample, company.id), args.verdict,
                            decided_by=_who(args), note=args.note)
        conn.commit()
    print(f"Werdykt: {out['verdict']}" + (f", cofnięcie {out['review_decision_id']}" if out["review_decision_id"] else ""))
    return 0


# ---------------------------------------------------------------------------


def register(sub: argparse._SubParsersAction) -> None:
    def common(p, *, by: bool = False):
        p.add_argument("--company", default="odm")
        if by:
            p.add_argument("--by", help="kto decyduje (domyślnie użytkownik systemu)")
            p.add_argument("--reason", help="dlaczego (opcjonalnie)")

    q = sub.add_parser("queue", help="kolejka przeglądu: karty, decyzje, cofnięcia, KPI")
    qs = q.add_subparsers(dest="queue_command", required=True, metavar="akcja")
    p = qs.add_parser("build", help="zbuduj kolejkę (sugestie, kandydaci, reguły, audyt, wartownicy)")
    common(p)
    p.add_argument("--min-months", type=int, default=mine.MIN_MONTHS, help="próg miesięcy dla kandydatów na reguły")
    p.add_argument("--no-mine", action="store_true", help="bez szukania kandydatów na reguły")
    p.add_argument("--no-auto", action="store_true", help="bez kategorii z reguł w etapie auto")
    p.set_defaults(handler=lambda a: _run(cmd_build, a))
    p = qs.add_parser("list", help="karty w kolejności (z powodem rangi)")
    common(p)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--all", action="store_true", help="także odłożone")
    p.set_defaults(handler=lambda a: _run(cmd_list, a))
    p = qs.add_parser("show", help="karta: next, pozycja (3) albo id")
    common(p)
    p.add_argument("item", nargs="?", default="next")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-mark", action="store_true", help="nie zapisuj czasu pokazania")
    p.set_defaults(handler=lambda a: _run(cmd_show, a))
    p = qs.add_parser("accept", help="Akceptuj (albo --suggestion 2 = alternatywa, --bulk = paczka)")
    common(p, by=True)
    p.add_argument("item")
    p.add_argument("--suggestion", help="numer propozycji na karcie (1-3) albo id sugestii")
    p.add_argument("--category")
    p.add_argument("--shares", help='JSON: [{"cost_object_id": ..., "share": 0.2}, ...]')
    p.add_argument("--action", choices=["approve", "split", "merge", "distinct", "correct", "error"],
                   help="przycisk karty kandydata / reguły / audytu")
    p.add_argument("--bulk", action="store_true", help="cała jednorodna paczka z odznaką „wysoka”")
    p.add_argument("--extra", help="JSON z dodatkowymi polami (groups, with, into, other, pattern, rule_action)")
    p.set_defaults(handler=lambda a: _run(cmd_accept, a))
    p = qs.add_parser("reject", help="Odrzuć propozycję (nie wróci)")
    common(p, by=True)
    p.add_argument("item")
    p.add_argument("--suggestion")
    p.set_defaults(handler=lambda a: _run(cmd_reject, a))
    p = qs.add_parser("defer", help="Odłóż (domyślnie do jutra)")
    common(p, by=True)
    p.add_argument("item")
    p.add_argument("--until", help="YYYY-MM-DD")
    p.set_defaults(handler=lambda a: _run(cmd_defer, a))
    p = qs.add_parser("other", help="Inne…: własna decyzja (bez dokumentu zawsze z podtypem)")
    common(p, by=True)
    p.add_argument("item")
    p.add_argument("--kind", required=True,
                   choices=["matched", "no_document", "own_transfer", "to_clarify", "allocation", "category"])
    p.add_argument("--document", action="append", help="document:<id>, row:<id>, transaction:<id>")
    p.add_argument("--subtype", help="podtyp bez dokumentu: zus, tax, fee, advance, deposit, card_awaiting, ...")
    p.add_argument("--share", action="append", help="<cost_object_id>=<udział>")
    p.add_argument("--category")
    p.set_defaults(handler=lambda a: _run(cmd_other, a))
    p = qs.add_parser("undo", help="cofnij decyzję (id) albo paczkę (batch:<id>)")
    p.add_argument("decision")
    p.add_argument("--by")
    p.add_argument("--reason")
    p.set_defaults(handler=lambda a: _run(cmd_undo, a))
    p = qs.add_parser("history", help="historia decyzji z cofaniem")
    common(p)
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--auto", action="store_true", help="także decyzje automatu")
    p.set_defaults(handler=lambda a: _run(cmd_history, a))
    p = qs.add_parser("stats", help="KPI: minuty pracy i błędy ważone kwotą")
    common(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(handler=lambda a: _run(cmd_stats, a))

    r = sub.add_parser("rules", help="reguły uczone z decyzji: etapy, decyzje, symulacja, kandydaci")
    rs = r.add_subparsers(dest="rules_command", required=True, metavar="akcja")
    p = rs.add_parser("list", help="reguły z etapem efektywnym")
    common(p)
    p.add_argument("--all", action="store_true", help="także wbudowane")
    p.set_defaults(handler=lambda a: _run(cmd_rules_list, a))
    p = rs.add_parser("show", help="reguła: wzorzec, wersje, obserwacje, post-mortem")
    common(p)
    p.add_argument("rule")
    p.set_defaults(handler=lambda a: _run(cmd_rules_show, a))
    for name, text in (("accept", "kandydat → podpowiedź (nowa wersja)"), ("reject", "jawne „nie”"),
                       ("enable-auto", "włącz auto (gdy gotowa)"), ("suspend", "zawieś"), ("resume", "wznów")):
        p = rs.add_parser(name, help=text)
        common(p, by=True)
        p.add_argument("rule")
        if name == "enable-auto":
            p.add_argument("--force", action="store_true", help="włącz mimo braku gotowości (świadomie)")
        if name == "resume":
            p.add_argument("--to", default="hint", choices=["hint", "auto"])
        p.set_defaults(handler=lambda a: _run(_rule_action, a))
    p = rs.add_parser("correct", help="popraw wzorzec / akcję / zakres (nowa wersja)")
    common(p, by=True)
    p.add_argument("rule")
    p.add_argument("--pattern", help='JSON {"all": [...]}')
    p.add_argument("--action", help='JSON {"kind": ...}')
    p.add_argument("--scope", help='JSON {"counterparties": [...]}')
    p.add_argument("--question")
    p.set_defaults(handler=lambda a: _run(_rule_action, a))
    p = rs.add_parser("simulate", help="co reguła zrobiłaby na historii")
    common(p)
    p.add_argument("rule")
    p.set_defaults(handler=lambda a: _run(cmd_rules_simulate, a))
    p = rs.add_parser("mine", help="szukaj kandydatów (≥ 5 przypadków w ≥ 2 miesiącach)")
    common(p)
    p.add_argument("--min-months", type=int, default=mine.MIN_MONTHS)
    p.add_argument("--min-cases", type=int, default=mine.MIN_CASES)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true", help="pokaż wzorce poniżej progu")
    p.add_argument("--by", help=argparse.SUPPRESS)
    p.set_defaults(handler=lambda a: _run(cmd_rules_mine, a))

    a = sub.add_parser("audit", help="audyt automatu: 2%% decyzji miesięcznie (min. 20)")
    as_ = a.add_subparsers(dest="audit_command", required=True, metavar="akcja")
    p = as_.add_parser("sample", help="wylosuj próbkę miesiąca (domyślnie poprzedni)")
    common(p)
    p.add_argument("--month", help="YYYY-MM")
    p.add_argument("--seed", type=int)
    p.set_defaults(handler=lambda a: _run(cmd_audit_sample, a))
    p = as_.add_parser("list", help="próbki")
    common(p)
    p.add_argument("--month")
    p.add_argument("--open", action="store_true")
    p.set_defaults(handler=lambda a: _run(cmd_audit_list, a))
    p = as_.add_parser("verdict", help="confirmed albo error (błąd = cofnięcie + zawieszenie reguły)")
    common(p, by=True)
    p.add_argument("sample")
    p.add_argument("verdict", choices=["confirmed", "error"])
    p.add_argument("--note")
    p.set_defaults(handler=lambda a: _run(cmd_audit_verdict, a))
