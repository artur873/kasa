"""FastAPI application of the panel.

* One database connection per request, in one transaction (committed when the
  request succeeds). Tests inject their own ``connection_factory``.
* Access: the panel shows plain data (role ``owner``), so outside localhost it
  needs a token - ``?token=...`` once (then a cookie), or ``Authorization: Bearer``.
* Templates: ``kasa/web/templates`` (Jinja2), mobile first, no JavaScript framework (the queue's
  10-second undo toast hides itself with a few lines of plain JS).
"""

from __future__ import annotations

import getpass
import hmac
import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from kasa.core.db import transaction

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
TOKEN_ENV = "KASA_WEB_TOKEN"
USER_ENV = "KASA_WEB_USER"
COMPANY_ENV = "KASA_WEB_COMPANY"
COOKIE = "kasa_token"

ConnectionFactory = Callable[[], AbstractContextManager[psycopg.Connection]]
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _url_factory(url: str | None) -> ConnectionFactory:
    @contextmanager
    def factory() -> Iterator[psycopg.Connection]:
        with transaction(url=url) as conn:
            yield conn

    return factory


def create_app(*, database_url: str | None = None, connection_factory: ConnectionFactory | None = None,
               token: str | None = None, user: str | None = None, company: str | None = None) -> FastAPI:
    """The panel application. ``token=None`` takes ``KASA_WEB_TOKEN`` (empty = no token required)."""
    from kasa.web.routers import control_sets, queue

    app = FastAPI(title="Kasa - panel", docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.state.connection_factory = connection_factory or _url_factory(database_url)
    app.state.token = token if token is not None else os.environ.get(TOKEN_ENV, "")
    app.state.user = user or os.environ.get(USER_ENV) or getpass.getuser()
    app.state.company = company or os.environ.get(COMPANY_ENV) or None

    @app.middleware("http")
    async def require_token(request: Request, call_next: Callable[[Request], Any]) -> Any:
        expected = request.app.state.token
        if not expected:
            return await call_next(request)
        given = (request.query_params.get("token") or request.cookies.get(COOKIE)
                 or request.headers.get("authorization", "").removeprefix("Bearer ").strip())
        if not given or not hmac.compare_digest(given, expected):
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": "brak lub zły token (KASA_WEB_TOKEN)"}, status_code=401)
            return PlainTextResponse("Brak dostępu: otwórz adres z ?token=... wypisany przez `kasa web`.",
                                     status_code=401)
        response = await call_next(request)
        if request.query_params.get("token"):
            response.set_cookie(COOKIE, expected, httponly=True, samesite="strict", max_age=60 * 60 * 24 * 30)
        return response

    @app.get("/", include_in_schema=False)
    def home() -> RedirectResponse:
        return RedirectResponse("/queue", status_code=303)        # the queue is the main screen (step 7)

    @app.get("/healthz", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(control_sets.router)
    app.include_router(control_sets.api, prefix="/api")
    app.include_router(queue.router)
    app.include_router(queue.api, prefix="/api")
    return app


def db(request: Request) -> Iterator[psycopg.Connection]:
    """FastAPI dependency: the request's connection (one transaction)."""
    with request.app.state.connection_factory() as conn:
        yield conn
