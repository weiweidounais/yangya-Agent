"""Prompt versions: content hash of every registered system prompt.

A generation records ``prompt.name`` + ``prompt.version`` (sha256[:12] of the prompt
text), plus ``prompt_file_version`` (e.g. ``v1``) for file-backed prompts,
so a trace records both the pinned file revision and the rendered content. After
``python -m app.evals.cli push-prompts`` the Langfuse version numbers are saved to
``DATA_DIR/langfuse_prompts.json`` and generations are linked to the Langfuse
prompt as well (``langfuse.observation.prompt.name`` / ``.version``).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Optional


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def prompt_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


def registry() -> dict[str, dict[str, str]]:
    """name -> {"hash", "text"} for every registered system prompt."""
    from app.agent.harness.leak import registered_prompts

    return {name: {"hash": prompt_hash(text), "text": text} for name, text in registered_prompts().items()}


def versions() -> dict[str, str]:
    return {name: v["hash"] for name, v in registry().items()}


def identify(system: str) -> Optional[tuple[str, str]]:
    """(name, hash) of the longest registered prompt contained in ``system``."""
    sys_n = _norm(system)
    if not sys_n:
        return None
    best: Optional[tuple[str, str, int]] = None
    for name, v in registry().items():
        p = _norm(v["text"])
        if p and p[: min(len(p), 400)] in sys_n and (best is None or len(p) > best[2]):
            best = (name, v["hash"], len(p))
    return (best[0], best[1]) if best else None


def _link_file() -> Path:
    from app.db import DATA_DIR

    return Path(DATA_DIR) / "langfuse_prompts.json"


def langfuse_links() -> dict[str, Any]:
    try:
        data = json.loads(_link_file().read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_langfuse_link(name: str, hash_: str, version: int, lf_name: str) -> None:
    data = langfuse_links()
    data[name] = {"hash": hash_, "version": int(version), "langfuse_name": lf_name}
    _link_file().write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def langfuse_link(name: str, hash_: str) -> Optional[dict[str, Any]]:
    link = langfuse_links().get(name)
    return link if isinstance(link, dict) and link.get("hash") == hash_ else None
