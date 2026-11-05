import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="harness_unit_")
os.environ["PROACTIVE_ENABLED"] = "0"
os.environ["FEISHU_WS_ENABLED"] = "0"
for _k in (
    "OPENAI_API_KEY",
    "KIMI_API_KEY",
    "MOONSHOT_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "LLM_FAST_API_KEY",
    "LLM_STRONG_API_KEY",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
):
    os.environ.pop(_k, None)
