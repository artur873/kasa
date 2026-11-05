"""External providers (bank, KSeF, LLM). Each has an interface and at least two
implementations: ``mock`` (fixtures, no network) and the real one (keys from env).

Selection by environment variables; missing keys never raise at startup, they
fall back to ``mock``:

    KASA_BANK_PROVIDER = mock | enablebanking
    KASA_KSEF_PROVIDER = mock | ksef
    KASA_LLM_PROVIDER  = mock | vertex

Add a provider: implement the Protocol in ``kasa/core/providers/<kind>.py`` and
register it in ``REGISTRY`` of that module. Never import network clients at
module import time; do it inside the real implementation's constructor.
"""

from __future__ import annotations

import os


def provider_name(kind: str, default: str = "mock") -> str:
    """Resolve the configured provider for ``kind`` ('bank', 'ksef', 'llm')."""
    return os.environ.get(f"KASA_{kind.upper()}_PROVIDER", default).strip().lower() or default
