"""组间休息计时卡：只给参数和一个可执行提示，倒计时由前端负责。"""

from __future__ import annotations

from typing import Any

DEFAULT_SECONDS = 90
MIN_SECONDS = 15
MAX_SECONDS = 600

# 组间提示按动作给一条，全部是可以立刻调整的动作要领，不是医疗建议。
TIPS = {
    "深蹲": "下一组先想着膝盖跟着脚尖走，最后两次别塌腰；宁可减 2.5 kg，也别憋气硬顶。",
    "硬拉": "起身前先把背拉直、杠铃贴腿；如果腰开始发紧，这组就停。",
    "卧推": "肩胛收住、脚踩稳；最后两次让同伴保护，别独自冲极限。",
    "推举": "核心收紧，别用腰借力；肘在手腕下方。",
    "划船": "先收肩胛再拉肘，别用身体甩。",
    "跑步": "下一组先把步频提起来，落地轻一点；呼吸乱了就降速。",
    "骑行": "下一组保持踏频，不要一上来就冲。",
}
GENERIC_TIP = "下一组动作先慢下来、做完整；如果这组动作变形，就减重量或提前结束。"


def rest_card(seconds: int = DEFAULT_SECONDS, *, exercise: str = "",
              set_index: int = 0, sets: int = 0, intensity: str = "") -> dict[str, Any]:
    try:
        value = int(seconds or DEFAULT_SECONDS)
    except (TypeError, ValueError):
        value = DEFAULT_SECONDS
    value = max(MIN_SECONDS, min(MAX_SECONDS, value))
    name = str(exercise or "").strip()[:24]
    tip = next((text for key, text in TIPS.items() if key in name), GENERIC_TIP)
    idx = max(0, min(int(set_index or 0), 99))
    total = max(0, min(int(sets or 0), 99))
    label = f"第 {idx} / {total} 组" if idx and total else (f"第 {idx} 组" if idx else "")
    return {
        "type": "rest_timer",
        "seconds": value,
        "exercise": name,
        "set_index": idx,
        "sets": total,
        "label": label,
        "intensity": str(intensity or "")[:12],
        "tip": tip,
    }


def rest_text(card: dict[str, Any]) -> str:
    head = "休息 " + f"{card['seconds']} 秒"
    if card.get("exercise"):
        head = f"{card['exercise']} · " + head
    if card.get("label"):
        head += f"（{card['label']}）"
    return f"{head}。{card.get('tip') or GENERIC_TIP}"
