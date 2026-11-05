"""离线演示用的假模型：本机跑通运动搭子的工具选择与参数抽取，不花额度。

只用于 UI / 链路演示，规则是写死的启发式，不含产品逻辑；真实效果请配真实 Key。

    python scripts/demo_sports_llm.py          # 127.0.0.1:9177/v1
    LLM_FAST_BASE_URL=http://127.0.0.1:9177/v1 LLM_FAST_API_KEY=fake \
    LLM_STRONG_BASE_URL=http://127.0.0.1:9177/v1 LLM_STRONG_API_KEY=fake \
    python -m uvicorn app.main:app --port 8900
"""
import json
import re
from fastapi import FastAPI, Request
import uvicorn

app = FastAPI()


def pick_tool(message: str) -> str:
    m = message or ""
    if re.search(r"身高|体重|体脂|建个搭子|建一个搭子", m):
        return "companion.set_body"
    if re.search(r"不知道吃什么|吃什么好|推荐.*吃|换一批", m):
        return "companion.meal_suggest"
    if re.search(r"搭子.*(怎么样|状态|胖|瘦)|我会胖|会不会胖|热量", m):
        return "companion.status"
    if re.search(r"吃了|喝了|早餐|午餐|晚餐|加餐|一碗|一份|一杯", m):
        return "companion.log_meal"
    if re.search(r"练了几次|练了多少|这周|本周|统计|历史|记录我", m):
        return "sport.history"
    if re.search(r"搭个|排.*计划|计划.*排|训练计划|安排一周", m):
        return "sport.plan_save"
    if re.search(r"我的计划|计划是什么|看看计划", m):
        return "sport.plan_get"
    if re.search(r"目标|每周.*天|每次.*分钟|伤病|膝盖|水平", m) and not re.search(r"公里|分钟.*跑|练完|跑了", m):
        return "sport.profile_update"
    if re.search(r"跑了|骑行|游泳|练完|练了|打球|瑜伽|徒步|公里|分钟", m):
        return "sport.log_workout"
    return "sport.profile_get"


def args_for(tool: str, message: str) -> dict:
    m = message or ""
    if tool == "sport.log_workout":
        sport = next((s for s in ("跑步", "骑行", "游泳", "力量训练", "球类", "瑜伽", "徒步") if s in m), "跑步")
        duration = re.search(r"(\d+)\s*分钟", m)
        distance = re.search(r"([\d.]+)\s*(公里|km|千米)", m)
        args = {"sport": sport, "duration_min": int(duration.group(1)) if duration else 30}
        if distance:
            args["distance_km"] = float(distance.group(1))
        if "轻松" in m:
            args["intensity"] = "轻松"
        elif "吃力" in m or "累" in m:
            args["intensity"] = "吃力"
        else:
            args["intensity"] = "中等"
        return args
    if tool == "sport.plan_save":
        goal = "半马 2 小时" if "半马" in m else ("10 公里跑进 60 分钟" if "10" in m else "一周三练，先把习惯养起来")
        return {"goal": goal, "weeks": 4, "days_per_week": 3, "session_minutes": 40,
                "items": [
                    {"day": "周一", "focus": "轻松跑", "duration_min": 30, "intensity": "轻松"},
                    {"day": "周三", "focus": "下肢力量", "duration_min": 35, "intensity": "中等"},
                    {"day": "周六", "focus": "长距离慢跑", "duration_min": 50, "intensity": "中等"},
                ]}
    if tool == "sport.history":
        return {"days": 7}
    if tool == "companion.set_body":
        height = re.search(r"(\d{3})\s*(?:cm|厘米|公分)?", m)
        weight = re.search(r"(\d{2,3}(?:\.\d)?)\s*(?:kg|公斤|斤)?", m)
        args = {"height_cm": int(height.group(1)) if height else 170,
                "weight_kg": float(weight.group(1)) if weight else 65}
        fat = re.search(r"体脂\s*(\d+(?:\.\d)?)", m)
        if fat:
            args["body_fat_pct"] = float(fat.group(1))
        if "减脂" in m:
            args["goal"] = "cut"
        elif "增重" in m or "增肌" in m:
            args["goal"] = "bulk"
        return args
    if tool == "companion.log_meal":
        kcal = re.search(r"(\d{2,4})\s*(?:kcal|千卡|大卡)", m)
        return {"name": m[:24] or "这一餐", "kcal": int(kcal.group(1)) if kcal else 500,
                "source": "exact" if kcal else "estimate"}
    if tool == "companion.meal_suggest":
        return {}
    if tool == "sport.profile_update":
        args = {}
        if "半马" in m:
            args["goal"] = "半马 2 小时"
        days = re.search(r"每周[^0-9]{0,6}(\d)\s*天", m)
        minutes = re.search(r"每次[^0-9]{0,6}(\d+)\s*分钟", m)
        if days:
            args["days_per_week"] = int(days.group(1))
        if minutes:
            args["session_minutes"] = int(minutes.group(1))
        if "膝" in m:
            args["injuries"] = "右膝外侧偶尔不适"
        if not args:
            args = {"goal": "恢复跑步"}
        return args
    return {}


@app.post("/v1/chat/completions")
async def completions(request: Request):
    body = await request.json()
    system = body["messages"][0]["content"]
    user = body["messages"][1]["content"] if len(body["messages"]) > 1 else ""
    try:
        payload = json.loads(user)
    except Exception:
        payload = {"message": user}
    message = str(payload.get("message") or "")
    if "Select one tool" in system:
        text = json.dumps({"tool": pick_tool(message), "confidence": 0.95})
    elif "运动工具提取参数" in system or "搭子工具提取参数" in system:
        tool = str(payload.get("tool") or "sport.profile_get")
        text = json.dumps({"args": args_for(tool, message)}, ensure_ascii=False)
    elif "识别图片里的食物" in system:
        text = json.dumps({"items": [
            {"name": "牛肉面", "portion": "一碗", "kcal": 620, "protein_g": 28, "carbs_g": 70, "fat_g": 20, "confidence": 0.82},
            {"name": "凉拌黄瓜", "portion": "一小碟", "kcal": 60, "protein_g": 2, "carbs_g": 8, "fat_g": 3, "confidence": 0.7},
        ], "note": "汤底油多，热量为估算"}, ensure_ascii=False)
    elif "心理安全" in system or "分类器" in system:
        text = "NO"
    else:
        text = "收到，我按你的节奏来。今天先做最容易开始的那一步，做完跟我说一声。"
    return {
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 300, "completion_tokens": 30},
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9177, log_level="warning")
