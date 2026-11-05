"""注册给聊天模型的搭子工具：身体档案、饮食、状态、选项、总结、唤醒。

全部只读写本人数据，没有对外动作，因此不需要确认凭证。
"""

from __future__ import annotations

from typing import Any, Optional

from app.agent.tool_registry import tool

from . import report, store, timer


@tool("companion.set_body", risk_level="self_write",
      description="保存身高体重体脂等身体数据并创建搭子。 Set body metrics and create the companion.")
def set_body(user_id: str = "", height_cm: float = 0, weight_kg: float = 0,
             body_fat_pct: Optional[float] = None, age: int = 30,
             sex: str = "other", activity_level: str = "light",
             goal: str = "maintain") -> dict[str, Any]:
    return store.set_body(user_id, height_cm=height_cm, weight_kg=weight_kg,
                          body_fat_pct=body_fat_pct, age=age, sex=sex,
                          activity_level=activity_level, goal=goal)


@tool("companion.status", risk_level="read",
      description="查看搭子状态、今日热量差与体型。 Read the companion status.")
def status(user_id: str = "") -> dict[str, Any]:
    return {"ok": True, "status": store.status(user_id)}


@tool("companion.log_meal", risk_level="self_write",
      description="记录一次饮食摄入与热量。 Log one meal or snack.")
def log_meal(user_id: str = "", name: str = "", kcal: float = 0,
             protein_g: Optional[float] = None, carbs_g: Optional[float] = None,
             fat_g: Optional[float] = None, source: str = "text") -> dict[str, Any]:
    return store.log_meal(user_id, name=name, kcal=kcal, protein_g=protein_g,
                          carbs_g=carbs_g, fat_g=fat_g, source=source)


@tool("companion.meal_suggest", risk_level="read",
      description="按剩余热量给出三个健康餐选项。 Suggest three healthy meal options.")
def meal_suggest(user_id: str = "", limit: int = 3) -> dict[str, Any]:
    state = store.status(user_id)
    return {"ok": True, "options": (state.get("suggestions") or [])[: max(1, min(int(limit or 3), 3))],
            "energy": state.get("energy") or {}}


@tool("companion.recap", risk_level="read",
      description="汇总一段时间的运动、饮食与体重变化。 Summarise a period.")
def recap(user_id: str = "", days: int = 30) -> dict[str, Any]:
    return {"ok": True, "recap": store.recap(user_id, days=days)}


@tool("companion.set_style", risk_level="self_write",
      description="修改搭子名字或社交风格。 Rename the companion or set social style.")
def set_style(user_id: str = "", name: str = "", social_label: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {"ok": True}
    if name:
        from app import db

        uid = store._uid(user_id)
        db.merge_and_save(uid, {"companion_name": str(name).strip()[:24] or "阿动"})
        result["name"] = str(name).strip()[:24]
    if social_label:
        result["social"] = store.set_social(user_id, social_label)["social"]
    if len(result) == 1:
        raise ValueError("要改名字还是社交风格？")
    return result


@tool("companion.revive", risk_level="self_write",
      description="唤醒沉睡的搭子，进入下一代。 Revive the companion as a new generation.")
def revive(user_id: str = "", name: str = "") -> dict[str, Any]:
    return {"ok": True, "status": store.revive(user_id, name=name)}


@tool("companion.rest_timer", risk_level="read",
      description="生成组间休息计时卡并给一条动作提示。 Build a rest timer card.")
def rest_timer(seconds: int = 90, exercise: str = "", set_index: int = 0,
               sets: int = 0, intensity: str = "") -> dict[str, Any]:
    card = timer.rest_card(seconds, exercise=exercise, set_index=set_index,
                           sets=sets, intensity=intensity)
    return {"ok": True, "card": card, "text": timer.rest_text(card)}


@tool("companion.weekly_report", risk_level="read",
      description="生成本周搭子周报文本。 Build this week's companion report.")
def weekly_report(user_id: str = "", days: int = 7) -> dict[str, Any]:
    return {"ok": True, "report": report.build_report(user_id, days=max(1, min(int(days or 7), 30)))}
