"""ODM hooks: rules about events that YAML cannot express."""

from __future__ import annotations

from typing import Any


def validate_cost_object_attrs(attrs: dict[str, Any]) -> None:
    """An event ends on or after the day it starts."""
    if attrs["date_end"] < attrs["date_start"]:
        raise ValueError("Koniec eventu (date_end) nie może być wcześniej niż początek (date_start)")
