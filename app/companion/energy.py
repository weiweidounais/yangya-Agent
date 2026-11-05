"""能量计算：BMR、运动消耗、摄入、热量差与体重方向。

All outputs are estimates, never medical advice. The numbers are deliberately
transparent so the UI and the chat can show how a value was derived.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

KCAL_PER_KG = 7700.0
ACTIVITY_FACTORS = {
    "sedentary": 1.2,   # 久坐
    "light": 1.375,     # 每周 1–2 练
    "moderate": 1.55,   # 每周 3–4 练
    "high": 1.725,      # 每周 5 练以上
}
ACTIVITY_LABELS = {
    "sedentary": "久坐少动",
    "light": "每周 1–2 练",
    "moderate": "每周 3–4 练",
    "high": "每周 5 练以上",
}

# MET by sport and perceived intensity (轻松 / 中等 / 吃力).
MET_TABLE: dict[str, dict[str, float]] = {
    "跑步": {"轻松": 8.0, "中等": 10.0, "吃力": 12.0},
    "骑行": {"轻松": 6.0, "中等": 8.0, "吃力": 10.0},
    "游泳": {"轻松": 6.0, "中等": 8.0, "吃力": 10.0},
    "力量训练": {"轻松": 3.5, "中等": 5.0, "吃力": 6.0},
    "球类": {"轻松": 5.0, "中等": 7.0, "吃力": 9.0},
    "瑜伽": {"轻松": 2.5, "中等": 3.0, "吃力": 4.0},
    "徒步": {"轻松": 4.0, "中等": 6.0, "吃力": 7.5},
    "其他": {"轻松": 4.0, "中等": 5.5, "吃力": 7.0},
}
DEFAULT_MET = MET_TABLE["其他"]


@dataclass(frozen=True)
class EnergyDay:
    bmr: float
    base_burn: float
    exercise_burn: float
    intake: float
    maintenance: float
    net: float
    direction: str
    weekly_kg: float
    bmi: float | None
    flags: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "bmr": round(self.bmr),
            "base_burn": round(self.base_burn),
            "exercise_burn": round(self.exercise_burn),
            "intake": round(self.intake),
            "maintenance": round(self.maintenance),
            "net": round(self.net),
            "direction": self.direction,
            "weekly_kg": round(self.weekly_kg, 2),
            "bmi": round(self.bmi, 1) if self.bmi is not None else None,
            "flags": list(self.flags),
        }


def bmr(height_cm: float, weight_kg: float, age: int = 30, sex: str = "other") -> float:
    """Mifflin–St Jeor. ``sex`` accepts male/female/other (or 男/女/其他)."""
    h = float(height_cm or 0)
    w = float(weight_kg or 0)
    a = int(age or 30)
    if h <= 0 or w <= 0:
        return 0.0
    sex_key = str(sex or "").strip().lower()
    if sex_key in {"male", "m", "男", "男性"}:
        offset = 5.0
    elif sex_key in {"female", "f", "女", "女性"}:
        offset = -161.0
    else:
        offset = -78.0  # midpoint, clearly labelled as an estimate
    return max(0.0, 10.0 * w + 6.25 * h - 5.0 * a + offset)


def activity_factor(level: str) -> float:
    return ACTIVITY_FACTORS.get(str(level or "").strip().lower(), ACTIVITY_FACTORS["light"])


def exercise_burn(weight_kg: float, sessions: Iterable[dict[str, Any]]) -> float:
    """MET × kg × hours, per logged session. Unknown sports use the '其他' row."""
    total = 0.0
    w = float(weight_kg or 0)
    if w <= 0:
        return 0.0
    for session in sessions or []:
        payload = (session or {}).get("payload") or {}
        sport = str(payload.get("sport") or "其他")
        intensity = str(payload.get("intensity") or "中等")
        minutes = float(payload.get("duration_min") or 0)
        if minutes <= 0:
            continue
        met = MET_TABLE.get(sport, DEFAULT_MET).get(intensity) or MET_TABLE.get(sport, DEFAULT_MET).get("中等", 5.5)
        total += met * w * (minutes / 60.0)
    return total


def daily(
    *,
    height_cm: float,
    weight_kg: float,
    age: int = 30,
    sex: str = "other",
    activity_level: str = "light",
    intake_kcal: float = 0,
    sessions: Iterable[dict[str, Any]] = (),
    goal: str = "maintain",
) -> EnergyDay:
    """Combine intake and logged exercise into one day's balance.

    ``base_burn`` is NEAT only (BMR × 1.2); logged workouts are added on top so
    an activity factor never double-counts the same run.
    """
    b = bmr(height_cm, weight_kg, age, sex)
    base = b * 1.2
    burn = exercise_burn(weight_kg, sessions)
    maintenance = base + burn
    intake = max(0.0, float(intake_kcal or 0))
    net = intake - maintenance
    weekly_kg = net * 7.0 / KCAL_PER_KG
    if net > 100:
        direction = "增重"
    elif net < -100:
        direction = "减重"
    else:
        direction = "基本持平"
    bmi = (float(weight_kg) / ((float(height_cm) / 100.0) ** 2)) if height_cm and weight_kg else None
    flags: list[str] = []
    lower = 1500 if str(sex).lower() in {"male", "m", "男", "男性"} else 1200
    if 0 < intake < lower:
        flags.append("low_intake")
    if net < -1000:
        flags.append("aggressive_deficit")
    if weekly_kg < -1.0:
        flags.append("fast_weight_loss")
    if bmi is not None and bmi < 18.5 and goal == "cut":
        flags.append("underweight_cut")
    return EnergyDay(b, base, burn, intake, maintenance, net, direction, weekly_kg, bmi, tuple(flags))


def projection_text(day: EnergyDay) -> str:
    """One-line, non-judgemental reading of the balance."""
    net = day.net
    if day.direction == "基本持平":
        return "今天摄入和消耗基本持平，体重大概率不会有明显变化。"
    verb = "增" if net > 0 else "减"
    return (
        f"按今天的热量差（{net:+.0f} kcal）推算，一周约{verb} "
        f"{abs(day.weekly_kg):.2f} kg；这是估算，真实变化看 2–4 周趋势。"
    )
