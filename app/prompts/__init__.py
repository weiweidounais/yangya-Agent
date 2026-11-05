"""Load explicitly pinned, versioned UTF-8 prompts without changing whitespace."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SLOT = re.compile(r"\{\{([a-z_]+)\}\}")
_loaded: dict[tuple[str, str], str] = {}


def load_prompt(name: str, version: str, **variables: str) -> str:
    """Load ``<name>/<version>.md``; substitute only explicit template slots.

    JSON braces are literal. Missing/extra variables and invalid paths fail at
    startup instead of silently sending a broken prompt. No stripping or implicit
    newline is applied. Call sites pin versions; there is no latest-version lookup.
    """
    if not re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*", name):
        raise ValueError(f"Invalid prompt name: {name!r}")
    if not re.fullmatch(r"v[1-9][0-9]*", version):
        raise ValueError(f"Invalid prompt version: {version!r}")
    template = (_ROOT / name / f"{version}.md").read_bytes().decode("utf-8")
    slots = set(_SLOT.findall(template))
    if slots != variables.keys():
        raise ValueError(f"Prompt {name}@{version}: expected variables {sorted(slots)}, got {sorted(variables)}")
    text = _SLOT.sub(lambda match: variables[match[1]], template)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    _loaded[(name, digest)] = version
    return text


def file_version(name: str, content_hash: str) -> str:
    """Version of the loaded text, only when its content hash matches."""
    return _loaded.get((name, content_hash), "")
