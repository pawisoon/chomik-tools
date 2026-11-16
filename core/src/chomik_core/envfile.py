"""Minimal .env loader (no python-dotenv dependency)."""
from __future__ import annotations

import os


def load_env_file(path: str = ".env", *, override: bool = False) -> list[str]:
    """Load KEY=VALUE lines into os.environ. Skips blanks, comments and empty values.

    Existing variables win unless `override`. Returns the keys that were set.
    Missing file is not an error.
    """
    if not os.path.isfile(path):
        return []
    loaded = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.removeprefix("export ").partition("=")
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            if not key or not value:
                continue
            if override or key not in os.environ:
                os.environ[key] = value
                loaded.append(key)
    return loaded
