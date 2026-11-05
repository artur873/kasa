"""Extension registry: one directory per company with extension.yaml (+ optional hooks.py).

Loading logic lives in kasa.core.extension; this package only holds the
built-in extensions and re-exports the registry functions.
"""

from kasa.core.extension import (
    BUILTIN_EXTENSIONS_DIR,
    EXTENSIONS_PATH_ENV,
    discover_extensions,
    extension_search_paths,
    get_extension,
)

__all__ = [
    "BUILTIN_EXTENSIONS_DIR",
    "EXTENSIONS_PATH_ENV",
    "discover_extensions",
    "extension_search_paths",
    "get_extension",
]
