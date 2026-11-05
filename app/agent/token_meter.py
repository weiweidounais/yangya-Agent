"""Per-task token metering for budget checks.

The gateway records an estimate for every completion; the loop reads the
meter that is active in the current context. Estimates only — vendors bill
with their own tokenizers, so treat numbers as a budget guard, not an invoice.
"""

from __future__ import annotations

import contextvars
import math
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Optional

_CJK = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]")
_MSG_OVERHEAD = 4


def estimate_tokens(text: str) -> int:
    """CJK ≈ 1 token per char, other text ≈ 1 token per 4 chars."""
    s = text or ""
    if not s:
        return 0
    cjk = len(_CJK.findall(s))
    other = len(s) - cjk
    return cjk + math.ceil(other / 4)


@dataclass
class TokenMeter:
    tokens: int = 0
    calls: int = 0
    by_task: dict[str, int] = field(default_factory=dict)

    def add(self, n: int, *, task: str = "") -> None:
        n = max(0, int(n))
        self.tokens += n
        self.calls += 1
        key = task or "default"
        self.by_task[key] = self.by_task.get(key, 0) + n


_current: contextvars.ContextVar[Optional[TokenMeter]] = contextvars.ContextVar(
    "agent_token_meter", default=None
)


def current() -> Optional[TokenMeter]:
    return _current.get()


def record_completion(system: str, user: str, output: str, *, task: str = "") -> int:
    """Add prompt + completion estimate to the active meter (no-op without one)."""
    n = (
        estimate_tokens(system)
        + estimate_tokens(user)
        + estimate_tokens(output)
        + 2 * _MSG_OVERHEAD
    )
    meter = _current.get()
    if meter is not None:
        meter.add(n, task=task)
    return n


@contextmanager
def metered() -> Iterator[TokenMeter]:
    """Open a fresh meter; nested meters roll their usage up to the parent."""
    parent = _current.get()
    meter = TokenMeter()
    token = _current.set(meter)
    try:
        yield meter
    finally:
        _current.reset(token)
        if parent is not None and meter.tokens:
            parent.tokens += meter.tokens
            parent.calls += meter.calls
            for k, v in meter.by_task.items():
                parent.by_task[k] = parent.by_task.get(k, 0) + v
