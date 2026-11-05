"""Paths shared by the bank tests (importable from test modules and conftest)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "fixtures" / "odm"
DATA = Path(__file__).resolve().parent / "data"
OCTOBER = (date(2026, 10, 1), date(2026, 10, 31))
