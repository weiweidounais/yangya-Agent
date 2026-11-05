"""健康食谱小库与「不知道吃什么」的选项生成。

只用规则排序，不连接外部服务，也不承诺减重效果。每个选项都带热量和
推荐理由，用户自己决定；真正的记录仍走 companion.log_meal。
"""

from __future__ import annotations

from typing import Any, Iterable

# ~20 个常见、好买、做法简单的选项；热量为常见份量的估算值。
MEALS: tuple[dict[str, Any], ...] = (
    {"id": "oats_egg", "name": "燕麦牛奶 + 水煮蛋", "kcal": 350, "protein_g": 20, "carbs_g": 42, "fat_g": 12,
     "tags": ["早餐", "快手", "高蛋白"], "note": "饱腹感强，适合早上时间紧。"},
    {"id": "chicken_sandwich", "name": "全麦鸡胸三明治", "kcal": 420, "protein_g": 32, "carbs_g": 45, "fat_g": 11,
     "tags": ["早餐", "午餐", "快手", "高蛋白"], "note": "蛋白质够，路上也能吃。"},
    {"id": "tomato_noodle", "name": "番茄鸡蛋面", "kcal": 480, "protein_g": 22, "carbs_g": 62, "fat_g": 15,
     "tags": ["午餐", "晚餐", "快手"], "note": "好做、暖胃，蛋白质可以再加个蛋。"},
    {"id": "fish_rice", "name": "清蒸鲈鱼 + 糙米饭 + 西兰花", "kcal": 550, "protein_g": 38, "carbs_g": 55, "fat_g": 16,
     "tags": ["午餐", "晚餐", "高蛋白", "低脂"], "note": "训练日晚上很合适，油少蛋白足。"},
    {"id": "chicken_salad", "name": "鸡胸肉沙拉", "kcal": 380, "protein_g": 35, "carbs_g": 22, "fat_g": 16,
     "tags": ["午餐", "晚餐", "低脂", "高蛋白"], "note": "热量低，酱料少放一点更好。"},
    {"id": "beef_broccoli", "name": "牛肉西兰花盖饭", "kcal": 600, "protein_g": 40, "carbs_g": 62, "fat_g": 20,
     "tags": ["午餐", "晚餐", "高蛋白"], "note": "练力量的日子补蛋白和碳水。"},
    {"id": "tofu_soup", "name": "豆腐蔬菜汤 + 杂粮饭", "kcal": 450, "protein_g": 20, "carbs_g": 58, "fat_g": 12,
     "tags": ["晚餐", "素食", "低脂"], "note": "清淡好消化，适合晚上。"},
    {"id": "greek_yogurt", "name": "希腊酸奶 + 蓝莓 + 坚果", "kcal": 280, "protein_g": 18, "carbs_g": 24, "fat_g": 12,
     "tags": ["早餐", "加餐", "快手", "高蛋白"], "note": "训练后加餐，别再额外加糖。"},
    {"id": "banana_soy", "name": "香蕉 + 无糖豆浆", "kcal": 220, "protein_g": 9, "carbs_g": 38, "fat_g": 5,
     "tags": ["早餐", "加餐", "快手", "素食"], "note": "跑前 1 小时垫一下刚好。"},
    {"id": "salmon_quinoa", "name": "三文鱼藜麦碗", "kcal": 520, "protein_g": 35, "carbs_g": 45, "fat_g": 22,
     "tags": ["午餐", "晚餐", "高蛋白"], "note": "脂肪质量好，但热量不低，注意份量。"},
    {"id": "shrimp_rice", "name": "虾仁鸡蛋炒饭（少油）", "kcal": 480, "protein_g": 28, "carbs_g": 58, "fat_g": 14,
     "tags": ["午餐", "晚餐", "快手", "高蛋白"], "note": "一锅出，油控制在一勺。"},
    {"id": "mushroom_soup", "name": "菌菇鸡汤 + 全麦面包", "kcal": 400, "protein_g": 22, "carbs_g": 42, "fat_g": 14,
     "tags": ["晚餐", "快手"], "note": "训练后不想吃太撑时选它。"},
    {"id": "toast_pb", "name": "牛奶 + 全麦吐司 + 花生酱", "kcal": 380, "protein_g": 16, "carbs_g": 44, "fat_g": 16,
     "tags": ["早餐", "快手"], "note": "赶时间可以，花生酱一勺就够。"},
    {"id": "corn_egg", "name": "玉米 + 鸡蛋 + 小番茄", "kcal": 260, "protein_g": 14, "carbs_g": 32, "fat_g": 9,
     "tags": ["早餐", "加餐", "低脂"], "note": "最轻的一档，饿了可以加一份酸奶。"},
    {"id": "sweet_potato_chicken", "name": "红薯 + 鸡胸 + 生菜", "kcal": 420, "protein_g": 34, "carbs_g": 48, "fat_g": 8,
     "tags": ["午餐", "晚餐", "低脂", "高蛋白"], "note": "减脂期很稳的一套，饱腹感好。"},
    {"id": "protein_shake", "name": "蔬菜蛋白粉奶昔", "kcal": 250, "protein_g": 25, "carbs_g": 20, "fat_g": 6,
     "tags": ["加餐", "快手", "高蛋白", "低脂"], "note": "练完没胃口时先补蛋白。"},
    {"id": "beefball_soup", "name": "牛肉丸青菜汤", "kcal": 300, "protein_g": 24, "carbs_g": 18, "fat_g": 14,
     "tags": ["晚餐", "快手", "高蛋白"], "note": "晚上想吃热乎的，配半碗饭。"},
    {"id": "quinoa_egg_salad", "name": "藜麦鸡蛋蔬菜沙拉", "kcal": 350, "protein_g": 20, "carbs_g": 36, "fat_g": 14,
     "tags": ["午餐", "晚餐", "素食", "低脂"], "note": "不想吃肉的日子换换口味。"},
    {"id": "veg_rice_bowl", "name": "杂粮饭 + 清炒时蔬 + 卤鸡腿（去皮）", "kcal": 520, "protein_g": 32, "carbs_g": 58, "fat_g": 16,
     "tags": ["午餐", "晚餐", "高蛋白"], "note": "食堂也能凑出来，鸡皮去掉少不少油。"},
    {"id": "egg_soup", "name": "紫菜蛋花汤 + 半份米饭 + 凉拌黄瓜", "kcal": 320, "protein_g": 14, "carbs_g": 44, "fat_g": 8,
     "tags": ["晚餐", "低脂", "素食"], "note": "今天额度不多时的轻晚餐。"},
)

TIME_TAGS = {"早上": "早餐", "上午": "加餐", "中午": "午餐", "下午": "加餐", "晚上": "晚餐", "夜宵": "加餐"}


def time_tag(hour: int) -> str:
    if 5 <= hour < 10:
        return "早餐"
    if 10 <= hour < 11:
        return "加餐"
    if 11 <= hour < 15:
        return "午餐"
    if 15 <= hour < 17:
        return "加餐"
    if 17 <= hour < 21:
        return "晚餐"
    return "加餐"


def suggest(*, remaining_kcal: float | None = None, goal: str = "maintain",
            time_of_day: str = "", recent_names: Iterable[str] = (),
            protein_target_g: float = 0, protein_so_far_g: float = 0,
            limit: int = 3) -> list[dict[str, Any]]:
    """Rank the library for the remaining budget and the user's goal.

    ``remaining_kcal=None`` means the day has no intake logged yet, so the
    caller should pass a maintenance-based budget instead.
    """
    tag = str(time_of_day or "").strip()
    if tag in TIME_TAGS:
        tag = TIME_TAGS[tag]
    recent = {str(x) for x in recent_names}
    gap = max(0.0, float(protein_target_g or 0) - float(protein_so_far_g or 0))
    # A single meal is a share of the remaining budget, not the whole day.
    share = {"早餐": 0.25, "午餐": 0.4, "晚餐": 0.35, "加餐": 0.15}.get(tag, 0.35)
    scored: list[tuple[float, dict[str, Any]]] = []
    for meal in MEALS:
        score = 0.0
        kcal = float(meal["kcal"])
        if remaining_kcal is not None:
            budget = float(remaining_kcal)
            target = max(180.0, budget * share) if budget > 80 else 220.0
            score += 3.0 - abs(kcal - target) / max(90.0, target * 0.55)
            if budget <= -100:
                # Over budget: only small items should surface.
                score -= kcal / 100.0
        if tag and tag in meal["tags"]:
            score += 2.0
        if goal == "cut":
            score += meal["protein_g"] / 12.0 - kcal / 260.0
        elif goal == "bulk":
            score += kcal / 260.0 + meal["protein_g"] / 20.0
        if gap > 15 and meal["protein_g"] >= 24:
            score += 1.5
        if meal["name"] in recent:
            score -= 2.5
        scored.append((score, meal))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    chosen = [meal for _, meal in scored[: max(1, min(int(limit or 3), 5))]]
    out = []
    for meal in chosen:
        reasons = []
        if remaining_kcal is not None:
            if remaining_kcal <= 0:
                reasons.append("今天额度已经差不多，选小份")
            elif meal["kcal"] <= remaining_kcal:
                reasons.append(f"今天还有约 {int(remaining_kcal)} kcal 额度")
        if gap > 15 and meal["protein_g"] >= 24:
            reasons.append(f"补上约 {meal['protein_g']} g 蛋白")
        if goal == "cut":
            reasons.append("减脂期优先高蛋白、低油")
        elif goal == "bulk":
            reasons.append("增重期需要足够热量")
        if tag and tag in meal["tags"]:
            reasons.append(f"适合{tag}")
        out.append({**meal, "reason": "；".join(reasons[:2]) or meal["note"]})
    return out
