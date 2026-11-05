"""Keep the product catalog aligned with default tool registrations."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def test_tools_doc_matches_registry(tmp_path):
    # A fresh process excludes tools registered by other tests in this suite.
    result = subprocess.run(
        [sys.executable, "-c", """
import json
from unittest.mock import patch
from app.agent import tool_registry as registry
with patch('app.feature_flags.memory_vector_enabled', return_value=True):
    registry.ensure_defaults()
    print(json.dumps([
        [tool['name'], tool['risk_level'], tool['requires_confirm']]
        for tool in registry.list_tools()
    ]))
"""],
        cwd=ROOT,
        env={**os.environ, "MEMORY_VECTOR_ENABLED": "1",
             "PYTHON_DOTENV_DISABLED": "1", "DATA_DIR": str(tmp_path),
             "PROACTIVE_ENABLED": "0", "FEISHU_WS_ENABLED": "0"},
        capture_output=True, text=True, check=True, timeout=30,
    )
    tools = json.loads(result.stdout)
    assert "memory_facts_search" in {name for name, _, _ in tools}
    document = (ROOT / "docs/tools.md").read_text(encoding="utf-8")
    rows = re.findall(
        r"^\| `([^`]+)` \| [^|\n]+ \| `([^`]+)` \| (是|否) \|$",
        document, re.MULTILINE,
    )
    expected = [(name, risk, "是" if confirm else "否")
                for name, risk, confirm in tools]
    assert sorted(rows) == sorted(expected)
    assert len(rows) == len(set(rows)) == len(tools)
    assert re.findall(r"^## (.+)$", document, re.MULTILINE) == [
        "档案", "写作", "运动", "搭子", "记忆", "提醒和定时任务", "其他",
    ]
