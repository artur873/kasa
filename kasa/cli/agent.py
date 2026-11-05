"""``python -m kasa agent serve|ask|tools`` - the MCP server of kasa and its test client (no model).

* ``agent serve [--company odm] [--model ...] [--client claude-desktop]`` - MCP over stdio (Claude Desktop,
  Gemini CLI, any MCP client); reads on the role ``kasa_agent_ro``, proposals on ``kasa_agent_suggest``;
* ``agent ask "<klucz metryki | narzędzie> klucz=wartość ..."`` - one question through the same MCP tools,
  answered by the test client from the tools' results only (e.g. ``agent ask "receivables_open as_of=2026-10-31"``,
  ``agent ask "margin_accrual filter.cost_object=Gala"``, ``agent ask "cit_2026"``);
* ``agent tools`` - the 17 tools.
"""

from __future__ import annotations

import argparse
import json


def _agent(args: argparse.Namespace):
    from kasa.core.agent.server import build_server, create_agent

    agent = create_agent(args.company, database_url=args.database_url, model=getattr(args, "model", None),
                         client=getattr(args, "client", None) or "kasa-cli",
                         prompt_version=getattr(args, "prompt_version", None))
    return build_server(agent)


def cmd_agent_serve(args: argparse.Namespace) -> int:
    from kasa.core.agent.server import serve

    serve(args.company, database_url=args.database_url, model=args.model, client=args.client,
          prompt_version=args.prompt_version)
    return 0


def cmd_agent_ask(args: argparse.Namespace) -> int:
    from kasa.core.agent.client import AgentClient

    client = AgentClient(_agent(args))
    answer = client.ask(args.question)
    if args.json:
        print(json.dumps({"answer": answer.text, "calls": [c.__dict__ for c in answer.calls]}, ensure_ascii=False,
                         indent=2, default=str))
    else:
        print(answer.text)
    return 1 if answer.refused else 0


def cmd_agent_tools(args: argparse.Namespace) -> int:
    from kasa.core.agent.client import AgentClient

    for name in AgentClient(_agent(args)).tools():
        print(name)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    ag = sub.add_parser("agent", help="agent AI przez MCP: 17 narzędzi tylko-do-odczytu + propozycje")
    a_sub = ag.add_subparsers(dest="agent_command", required=True, metavar="akcja")
    p = a_sub.add_parser("serve", help="serwer MCP na stdio (Claude Desktop, Gemini CLI)")
    p.add_argument("--company", default="odm")
    p.add_argument("--model", help="model klienta (do logu; domyślnie KASA_AGENT_MODEL)")
    p.add_argument("--client", help="nazwa klienta (do logu; domyślnie KASA_AGENT_CLIENT)")
    p.add_argument("--prompt-version", dest="prompt_version", help="wersja promptu klienta (do logu)")
    p.set_defaults(handler=cmd_agent_serve)
    p = a_sub.add_parser("ask", help="pytanie przez narzędzia MCP (klient testowy, bez modelu)")
    p.add_argument("question", help='"<klucz metryki | narzędzie> klucz=wartość ..." (filtr: filter.klucz=wartość)')
    p.add_argument("--company", default="odm")
    p.add_argument("--json", action="store_true", help="odpowiedź z wywołaniami narzędzi")
    p.set_defaults(handler=cmd_agent_ask)
    p = a_sub.add_parser("tools", help="lista narzędzi serwera MCP")
    p.add_argument("--company", default="odm")
    p.set_defaults(handler=cmd_agent_tools)
