"""Personal Agent — a personal assistant agent service."""

import os as _os
from pathlib import Path as _Path


def _read_version() -> str:
    try:
        return (_Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


__version__ = _read_version()
# Optional build stamp (set by deploy: GIT_SHA=$(git rev-parse --short HEAD)).
GIT_SHA = (_os.getenv("GIT_SHA") or "").strip()[:12]
VERSION_TOKEN = "__APP_VERSION__"
