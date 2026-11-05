"""社交策略：i/e 风格怎么打招呼、怎么 PK、怎么捣乱（只生成文本，不联网）。

附近的人、定位与好友关系需要独立服务与合规章节，当前版本不实现匹配，
只保证：

* 位置默认不采集；开启后也只存 geohash（≈1 km 精度），不存坐标。
* 必须先双方同意才互相可见；打招呼有每日上限与一键拉黑。
* 性格标签只用于调整说话方式，绝不展示给第三方。
"""

from __future__ import annotations

from typing import Any

LABELS = ("偏内向", "中间型", "偏外向")

GREETINGS = {
    "偏内向": {
        "open": "嗨，我也在附近练。你要是想有人一起，回个『好』就行，不想聊也完全没关系。",
        "pace": "先聊训练本身，不发问句连击；对方回一句再补一句。",
    },
    "中间型": {
        "open": "嗨，附近看到你也在用阿动。今天练什么？",
        "pace": "可以问一个具体问题，聊得动就继续，聊不动就收。",
    },
    "偏外向": {
        "open": "嗨！附近居然遇到同款搭子，今天练什么，要不要互相关注一下？",
        "pace": "可以主动提议一起练或 PK，但给对方拒绝的台阶。",
    },
}


def greeting_strategy(label: str) -> dict[str, Any]:
    style = GREETINGS.get(label, GREETINGS["中间型"])
    return {"style": label if label in GREETINGS else "中间型", **style,
            "daily_limit": 3, "requires_mutual_opt_in": True}


def privacy_contract() -> dict[str, Any]:
    return {
        "location": "默认关闭；开启后只存 geohash5（≈1 km），不存经纬度。",
        "visibility": "双方都开启附近可见才互相出现；随时可关闭。",
        "greeting": "每人每天最多 3 次主动打招呼；被拉黑即永久静默。",
        "profile": "只展示昵称、搭子形象和运动标签，不展示体重、体脂、饮食记录。",
        "pk": "只比步数/训练分钟/连续天数这类可验证指标；不涉及金钱。",
        "prank": "捣乱仅限视觉小动画（如偷走一颗星星），有冷却和关闭开关。",
    }
