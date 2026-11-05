"""Company registry (one company per extension)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

import psycopg

from kasa.core.extension import Extension, ExtensionError


@dataclass(frozen=True)
class Company:
    id: UUID
    slug: str
    name: str
    extension_slug: str


def ensure_company(conn: psycopg.Connection, extension: Extension) -> tuple[Company, bool]:
    """Create the company of an extension if missing. Returns (company, created).

    The database trigger creates the company's UNASSIGNED cost object.
    """
    row = conn.execute(
        """
        INSERT INTO company (slug, name, extension_slug) VALUES (%s, %s, %s)
        ON CONFLICT (slug) DO NOTHING
        RETURNING id, slug, name, extension_slug
        """,
        (extension.slug, extension.name, extension.slug),
    ).fetchone()
    if row is not None:
        return Company(*row), True
    company = get_company(conn, extension.slug)
    if company.extension_slug != extension.slug:
        raise ExtensionError(
            f"Firma {company.slug!r} używa rozszerzenia {company.extension_slug!r}, nie {extension.slug!r}"
        )
    return company, False


def get_company(conn: psycopg.Connection, slug: str) -> Company:
    row = conn.execute(
        "SELECT id, slug, name, extension_slug FROM company WHERE slug = %s", (slug,)
    ).fetchone()
    if row is None:
        raise LookupError(f"Nie ma firmy {slug!r}")
    return Company(*row)


def list_companies(conn: psycopg.Connection) -> list[Company]:
    rows = conn.execute("SELECT id, slug, name, extension_slug FROM company ORDER BY slug").fetchall()
    return [Company(*row) for row in rows]


def init_companies(conn: psycopg.Connection, extensions: Iterable[Extension]) -> list[tuple[Company, bool]]:
    return [ensure_company(conn, extension) for extension in extensions]
