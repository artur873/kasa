"""CLI subcommand plug-ins.

Every module in this package may define ``register(sub: argparse._SubParsersAction) -> None``
that adds its own subcommands. ``kasa.__main__`` discovers and calls them all, so
feature areas (bank, documents, matching, ...) add commands without editing one file.
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil


def register_all(sub: argparse._SubParsersAction) -> None:
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        module = importlib.import_module(f"{__name__}.{info.name}")
        register = getattr(module, "register", None)
        if register is not None:
            register(sub)
