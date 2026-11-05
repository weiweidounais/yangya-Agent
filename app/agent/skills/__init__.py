"""Version-pinned, local-only skill guidance; never an execution authority.

The skill registry stays as a framework: a package under ``packages/<name>/<version>``
with ``metadata.json`` and ``SKILL.md`` can be attached to an intent. The generic
agent ships without packages; ``registry.json`` lists none.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
import json
from pathlib import Path
import re

_ROOT = Path(__file__).parent
_NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
_INTENTS = {"draft", "chat"}
MAX_SKILL_BYTES = 8192


@dataclass(frozen=True)
class SkillMetadata:
    name: str
    version: str
    description: str
    intents: tuple[str, ...]

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"


class SkillRegistry:
    """Discover metadata eagerly, read Markdown only for a selected pinned skill.

    The root is deployment-owned, never a request field. Invalid packages raise
    ValueError; unknown names/versions raise KeyError. No remote fetching or code.
    """
    def __init__(self, root: Path = _ROOT):
        self.root = root.resolve()
        manifest = json.loads((self.root / "registry.json").read_text())
        if manifest.get("schema_version") != 1 or not isinstance(manifest.get("skills"), dict):
            raise ValueError("Invalid skill registry schema")
        self._items: dict[str, SkillMetadata] = {}
        for name, version in manifest["skills"].items():
            if not isinstance(version, str) or not _NAME.fullmatch(name) or not _VERSION.fullmatch(version):
                raise ValueError("Invalid skill name or version")
            data = json.loads(self._file(name, version, "metadata.json").read_text())
            intents = data.get("intents")
            if (data.get("schema_version") != 1 or data.get("name") != name
                    or data.get("version") != version
                    or not isinstance(data.get("description"), str) or not data["description"].strip()
                    or not isinstance(intents, list) or not intents
                    or any(not isinstance(i, str) or i not in _INTENTS for i in intents)):
                raise ValueError(f"Invalid skill metadata: {name}")
            self._items[name] = SkillMetadata(name, version, data["description"], tuple(intents))

    def _file(self, name: str, version: str, filename: str) -> Path:
        path = (self.root / "packages" / name / version / filename).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Skill path escapes registry")
        return path

    def catalog(self) -> tuple[SkillMetadata, ...]:
        return tuple(self._items.values())

    def select(self, intent: str) -> tuple[SkillMetadata, ...]:
        return tuple(m for m in self.catalog() if intent in m.intents)

    def load(self, name: str, version: str | None = None) -> str:
        meta = self._items[name]
        if version is not None and version != meta.version:
            raise KeyError(f"Unregistered skill version: {name}@{version}")
        with self._file(meta.name, meta.version, "SKILL.md").open("rb") as stream:
            content = stream.read(MAX_SKILL_BYTES + 1)
        if len(content) > MAX_SKILL_BYTES or not content.strip():
            raise ValueError(f"Empty or oversized skill: {meta.ref}")
        body = content.decode("utf-8")
        from app.agent.harness.leak import register_system_prompt
        register_system_prompt(f"skill.{meta.ref}", body)
        return body


@lru_cache(maxsize=1)
def registry() -> SkillRegistry:
    return SkillRegistry()


def skills_for_intent(intent: str) -> list[dict]:
    """Metadata only; does not read skill bodies or alter intent routing."""
    return [asdict(m) for m in registry().select(intent)]


def with_skill_context(system: str, *names: str) -> str:
    """Compose bounded trusted guidance; user data remains in data blocks.

    Keep the caller's output contract and safety rules last. No skill grants
    tools, changes permissions, or mutates session state.
    """
    reg = registry()
    blocks = [reg.load(name) for name in dict.fromkeys(names)]
    return "\n\n".join([*blocks, system])
