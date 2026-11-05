"""Run the actual browser delivery and render pipeline with a virtual clock."""

from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("entrypoint", ["chat", "draft"])
@pytest.mark.parametrize("scenario", [
    "fast", "timeout", "network", "proxy", "truncated", "failed", "server_error",
    "worker_timeout", "hard_timeout", "poll_hangs", "missing", "offline", "expired",
    "body_timeout", "transient_poll",
])
def test_turn_delivery_renders_and_clears_typing(entrypoint, scenario):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["node", "tests/turn_delivery_ui.mjs", scenario, entrypoint],
        cwd=root, text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
