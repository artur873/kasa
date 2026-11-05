"""``python -m kasa web`` - the owner's panel (control-set labeling; the review queue in phase 4)."""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import sys

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _lan_address() -> str:
    """The machine's address in the local network (for the phone), best effort, no traffic sent."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))      # TEST-NET-1: nothing is sent for UDP connect
            return probe.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def cmd_web(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("Brak serwera WWW: uv pip install fastapi uvicorn jinja2 python-multipart", file=sys.stderr)
        return 2
    from kasa.web.app import TOKEN_ENV, create_app

    token = args.token if args.token is not None else os.environ.get(TOKEN_ENV, "")
    if args.host not in LOOPBACK and not token:
        token = secrets.token_urlsafe(18)
        print(f"Panel jest widoczny w sieci - wygenerowano token (ustaw {TOKEN_ENV}, żeby był stały).")
    app = create_app(database_url=args.database_url, token=token, user=args.user, company=args.company)
    host = _lan_address() if args.host in ("0.0.0.0", "::") else args.host
    suffix = f"/?token={token}" if token else "/"
    print(f"Panel: http://{host}:{args.port}{suffix}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("web", help="panel WWW (telefon): etykietowanie zbiorów kontrolnych")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--host", default="127.0.0.1",
                   help="adres nasłuchu; 0.0.0.0 = sieć lokalna (wtedy wymagany token)")
    p.add_argument("--token", help="token dostępu (domyślnie KASA_WEB_TOKEN; w sieci generowany)")
    p.add_argument("--user", help="kto etykietuje (labeled_by; domyślnie KASA_WEB_USER albo login)")
    p.add_argument("--company", help="firma domyślna (slug)")
    p.set_defaults(handler=cmd_web)
