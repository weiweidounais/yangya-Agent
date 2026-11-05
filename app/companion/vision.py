"""食物照片识别适配层。

原则：
* 图片只在内存里处理，不落盘、不进轨迹、不写日志。
* 没有配置视觉模型时明确拒绝（``vision_not_configured``），绝不猜热量。
* 识别结果只是候选，必须由用户确认后再调用 ``companion.log_meal``。

配置（OpenAI 兼容多模态）：
* ``VISION_MODEL`` 必填，例如 ``qwen-vl-max`` / ``gpt-4o-mini``；
* ``VISION_BASE_URL``（缺省回落到 ``LLM_FAST_BASE_URL``）；
* ``VISION_API_KEY``（缺省回落到 ``LLM_FAST_API_KEY``，再回落供应商 Key）。
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

import httpx

from app.agent.tool_registry import ToolError

MAX_BYTES = 4 * 1024 * 1024
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}

PROMPT = (
    "识别图片里的食物，估算每一样的份量和热量。只返回 JSON："
    '{"items":[{"name":"食物名","portion":"常见份量描述","kcal":整数,'
    '"protein_g":数字,"carbs_g":数字,"fat_g":数字,"confidence":0到1}],'
    '"note":"一句不确定的地方"}。不确定就降低 confidence，不要编造品牌或精确重量。'
)

_KEY_ENVS = ("VISION_API_KEY", "LLM_FAST_API_KEY", "OPENAI_API_KEY", "KIMI_API_KEY", "DASHSCOPE_API_KEY")


def config() -> dict[str, str]:
    model = (os.getenv("VISION_MODEL") or "").strip()
    base = (os.getenv("VISION_BASE_URL") or os.getenv("LLM_FAST_BASE_URL")
            or "https://api.openai.com/v1").strip().rstrip("/")
    key = next((os.getenv(name).strip() for name in _KEY_ENVS
                if (os.getenv(name) or "").strip()), "")
    return {"model": model, "base_url": base, "api_key": key}


def has_vision() -> bool:
    current = config()
    return bool(current["model"] and current["api_key"])


def status() -> dict[str, Any]:
    current = config()
    configured = bool(current["model"] and current["api_key"])
    missing = []
    if not current["model"]:
        missing.append("VISION_MODEL")
    if not current["api_key"]:
        missing.append("VISION_API_KEY")
    return {
        "configured": configured,
        "model": current["model"] if configured else "",
        "provider": current["base_url"] if configured else "",
        "missing": missing,
        "message": ("可以直接拍食物照片，我给出候选热量，你确认后才记账。"
                    if configured else "拍照识餐还没配置视觉模型；先用文字告诉我吃了什么。"),
        "max_bytes": MAX_BYTES,
        "stores_images": False,
    }


def _sniff(image: bytes) -> str:
    if image[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if image[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image[:4] == b"RIFF" and image[8:12] == b"WEBP":
        return "image/webp"
    return ""


def analyze_food(image: bytes, *, mime: str = "") -> dict[str, Any]:
    """Return candidate items; never persists the image."""
    if not image:
        raise ToolError("empty_image", "没有收到图片")
    if len(image) > MAX_BYTES:
        raise ToolError("image_too_large", "图片太大了，请压缩到 4 MB 以内")
    detected = _sniff(image)
    if not detected:
        raise ToolError("bad_image_type", "只支持 JPG / PNG / WebP")
    current = config()
    if not has_vision():
        raise ToolError(
            "vision_not_configured",
            "还没有接入视觉模型。可以先用文字告诉我吃了什么，我照样能记热量。",
        )
    data_uri = f"data:{detected};base64," + base64.b64encode(image).decode("ascii")
    body = {
        "model": current["model"],
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": "这是什么食物？估算热量。"},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ]},
        ],
        "max_tokens": 600,
        "temperature": 0,
    }
    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(f"{current['base_url']}/chat/completions", json=body,
                                   headers={"Authorization": f"Bearer {current['api_key']}"})
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
    except Exception:
        raise ToolError("vision_failed", "识别服务暂时不可用，先用文字记一下吧") from None
    items, note = _parse(content)
    if not items:
        raise ToolError("vision_unclear", "这张图我看不清，换个角度或者直接用文字描述？")
    return {"ok": True, "items": items, "note": note, "needs_confirm": True,
            "model": current["model"]}


def _parse(content: Any) -> tuple[list[dict[str, Any]], str]:
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    start, end = text.find("{"), text.rfind("}")
    parsed: Any = None
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            parsed = None
    if not isinstance(parsed, dict):
        return [], ""
    raw_items = parsed.get("items")
    if not isinstance(raw_items, list):
        return [], str(parsed.get("note") or "")[:200]

    def _num(value: Any, low: float, high: float) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return round(max(low, min(high, number)), 1)

    cleaned: list[dict[str, Any]] = []
    for item in raw_items[:8]:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            continue
        kcal = _num(item.get("kcal"), 1, 3000)
        if kcal is None:
            continue
        confidence = _num(item.get("confidence"), 0, 1)
        cleaned.append({
            "name": str(item["name"]).strip()[:60],
            "portion": str(item.get("portion") or "").strip()[:40],
            "kcal": int(round(kcal)),
            "protein_g": _num(item.get("protein_g"), 0, 300),
            "carbs_g": _num(item.get("carbs_g"), 0, 500),
            "fat_g": _num(item.get("fat_g"), 0, 200),
            "confidence": confidence if confidence is not None else 0.4,
        })
    return cleaned, str(parsed.get("note") or "").strip()[:200]
