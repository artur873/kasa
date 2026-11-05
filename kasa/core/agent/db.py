"""Connections of the MCP server - the safeguards are in the database, not in the prompt (briefing, step 8).

* ``ro()`` - role ``kasa_agent_ro``: SELECT only on ``metrics``, ``views``, ``evidence``; read-only
  transaction; ``statement_timeout`` 15 s; ``search_path = evidence, views, metrics, public`` (the shared
  metric code reads the per-company mirrors); the company = ``kasa.company_id``. Every read of every tool.
* ``suggest()`` - role ``kasa_agent_suggest``: INSERT only on ``l2_suggestion``, ``metric_proposal`` and
  ``agent_call_log``. The five "propose" tools, the call log, metric proposals.
* ``gate()`` - the owner role, used ONLY by the pseudonymization gate (``kasa.core.pseudonym``: the key
  table is never readable by the agent's roles) and by ``sync`` at start (definitions, evidence mirrors).

Production: give the server login roles of its own (``KASA_AGENT_RO_URL``, ``KASA_AGENT_SUGGEST_URL``,
members of the two roles) - then nothing in the server can switch back to the owner. Without them the server
logs in with ``KASA_DATABASE_URL`` and switches with ``SET ROLE`` (the local development set-up: the role
``kasa`` is a superuser).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

import psycopg

from kasa.core.companies import Company, get_company
from kasa.core.db import database_url

RO_ROLE = "kasa_agent_ro"
SUGGEST_ROLE = "kasa_agent_suggest"
RO_URL_ENV = "KASA_AGENT_RO_URL"
SUGGEST_URL_ENV = "KASA_AGENT_SUGGEST_URL"
STATEMENT_TIMEOUT = "15s"
RO_SEARCH_PATH = "evidence, views, metrics, public"


@dataclass
class AgentDatabase:
    company_slug: str
    url: str | None = None
    ro_url: str | None = None
    suggest_url: str | None = None

    def __post_init__(self) -> None:
        self.url = self.url or database_url()
        self.ro_url = self.ro_url or os.environ.get(RO_URL_ENV) or None
        self.suggest_url = self.suggest_url or os.environ.get(SUGGEST_URL_ENV) or None
        with psycopg.connect(self.url, autocommit=True) as conn:
            self.company: Company = get_company(conn, self.company_slug)

    @property
    def company_id(self) -> UUID:
        return self.company.id

    def _session(self, url: str | None, role: str, search_path: str) -> psycopg.Connection:
        conn = psycopg.connect(url or self.url, autocommit=True, application_name=f"kasa-agent:{role}")
        try:
            if url is None:
                conn.execute(f"SET ROLE {role}")
            conn.execute(f"SET search_path = {search_path}")
            conn.execute("SELECT set_config('statement_timeout', %s, false)", (STATEMENT_TIMEOUT,))
            conn.execute("SELECT set_config('kasa.company_id', %s, false)", (str(self.company_id),))
            current = conn.execute("SELECT current_user").fetchone()[0]
            if current != role and not current.startswith(role):
                member = conn.execute("SELECT pg_has_role(current_user, %s, 'MEMBER')", (role,)).fetchone()[0]
                if not member:
                    raise PermissionError(f"połączenie agenta działa jako {current}, nie {role}")
        except Exception:
            conn.close()
            raise
        conn.autocommit = False
        return conn

    @contextmanager
    def ro(self) -> Iterator[psycopg.Connection]:
        conn = self._session(self.ro_url, RO_ROLE, RO_SEARCH_PATH)
        conn.read_only = True
        try:
            yield conn
        finally:
            conn.rollback()
            conn.close()

    @contextmanager
    def suggest(self) -> Iterator[psycopg.Connection]:
        conn = self._session(self.suggest_url, SUGGEST_ROLE, "public")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def gate(self) -> Iterator[psycopg.Connection]:
        conn = psycopg.connect(self.url, application_name="kasa-agent:gate")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
