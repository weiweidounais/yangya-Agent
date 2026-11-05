# 运动搭子二开说明

本仓库是 **Personal Agent 的运动搭子版**（0.9.0）。底座（LangGraph 编排、记忆、确认卡、渠道、护栏、轨迹、审计、模型网关）原样保留；本文件只说明这一版新增/改动了什么，以及怎么继续改。

## 1. 定位

把「通用个人助理」收窄成**一个会陪你练、记得你身体情况、替你记账、按时提醒的训练搭子**：

- 人设换成训练搭子「阿动」（`app/prompts/chat.persona/v3.md`）：先问目标/水平/时间/伤病，再排能执行的计划；不诊断、不灌鸡汤、不承诺成绩。
- 领域数据三件套：**运动档案**（目标、水平、每周天数、单次时长、伤病、器械）、**训练记录**、**周计划**。
- 聊到训练量、计划、伤病时，服务端把已保存的运动档案和近 7 天训练量注入提示词；模型不用「凭印象」回答。
- 训练提醒复用现有定时任务（`schedule.*`，带确认卡），没有另造一套调度器。

## 2. 数据放哪

刻意不加表，避免动数据库/RLS 契约：

| 数据 | 存储 | 说明 |
| --- | --- | --- |
| 运动档案 | `user_profiles` 的 JSON（`sport_*` 字段） | 记忆卡与档案抽屉都能直接编辑；`format_for_prompt` 会带进提示词 |
| 训练记录 | `episodic_events`，`kind="workout"` | payload 存 sport / duration_min / distance_km / intensity / note；可搜索、可单条删除 |
| 周计划 | `user_profiles.sport_plan` | 目标、周次、每周天数、单次时长、每天安排、备注 |

代码：`app/sports/store.py`（`get_profile` / `update_profile` / `log_workout` / `stats` / `save_plan` / `get_plan` / `prompt_block`）。

身份只来自服务端会话：`store._uid()` 从 `app.data_access.current_user()` 取当前用户，传入别人的 `user_id` 直接 `NotFound`（fail closed）。

## 3. 六个工具

| name | 风险 | 做什么 |
| --- | --- | --- |
| `sport.profile_get` | `read` | 读运动档案 |
| `sport.profile_update` | `self_write` | 合并保存档案字段（空值不覆盖旧值） |
| `sport.log_workout` | `self_write` | 记一次训练，返回近 7 天汇总 |
| `sport.history` | `read` | 按天窗口汇总次数/时长/距离/连续天数 |
| `sport.plan_get` | `read` | 读当前计划（附可读文本） |
| `sport.plan_save` | `self_write` | 保存周计划（目标 + 每天安排） |

全部只写本人数据，没有对外动作，所以 `requires_confirm=false`；改动清单见 `docs/tools.md` 的「运动」段，`/api/tools` 自动列出。

## 4. 从一句话到一条记录

```
用户：今天跑了 5 公里，32 分钟
  1. /api/chat → 隐私门（无关）
  2. router.select_tool → 模型在注册表里选 sport.log_workout（置信度 ≥ 0.8）
  3. main._chat 分支 → app/sports/chat.respond
       用工具 JSON Schema 让模型只提取参数：{"sport":"跑步","duration_min":32,"distance_km":5}
       参数缺失 → 返回一个问题并记 pending_sport（下一轮补齐，不猜）
  4. tool_registry.invoke_tool → 校验 schema、注入可信 user_id、写记忆事件
  5. 渲染成一句中文反馈，返回 status="chat"
```

关键约束：

- **参数抽取与工具执行分离**，模型只能选工具、填参数，不能改身份、不能跳过校验。
- 计划/距离/时长**缺就不填**，先追问；不会把「我练完了」写成 30 分钟。
- 没有模型 Key 时（mock 模式）工具选择不会触发，聊天走占位回复，符合原有离线约定。

## 5. 提示词与技能包

| 文件 | 作用 |
| --- | --- |
| `app/prompts/chat.persona/v3.md` | 训练搭子人设、工作方式、安全边界、示例（含 `确认卡` / `CRISIS_NOTE` 等底座必需锚点） |
| `app/agent/skills/packages/training-coach/1.0.0/` | 训练指导规范：渐进超负荷、RPE、10% 原则、伤病分流、不做医疗诊断 |
| `app/agent/skills/registry.json` | 默认注册 `training-coach@1.0.0`（仅 chat 意图） |

技能包只提供文本，不授予工具；正文加载时会登记进泄露护栏。要停用：把 `registry.json` 的 `skills` 改回 `{}`，并删掉 `app/agent/subgraphs/chat.py` 里的 `COACH_SKILLS` 组合。

## 6. HTTP 与前端

- `GET /api/sports/summary?days=28`：返回 `{profile, stats, plan}`，仅本人；`/api/*` 仍带 `X-Trace-Id`。
- 对话页仍然只有一条消息流：品牌/hero/快捷 chips 改为「记录今天的训练 / 帮我排一周训练计划 / 每周一三五提醒我练腿 / 写一条训练打卡」。
- 档案抽屉新增运动字段：运动目标、训练水平、每周训练天数、单次时长、伤病与限制、器械与场地。
- 记忆卡会显示训练记录（`训练记录`）和计划（`训练计划`），可编辑/删除。

## 7. 怎么继续加

**加一个运动工具**：在 `app/sports/tools.py` 用 `@tool("sport.xxx", risk_level=...)` 注册 → 在 `app/agent/tool_catalog.py` 加中英说明与超时 → 在 `app/agent/tool_selection.py` 的 `TOOL_ROUTES` 加 `"sport.xxx": "chat"` → 在 `app/sports/chat.py` 的 `_TOOLS` 与 `_reply()` 加分支 → 更新 `docs/tools.md` 与 `tests/fixtures/main_tool_catalog.json`。

**换运动类型**：`store.SPORTS` 是展示与统计用的枚举；`log_workout` 对其他名字也会落成「其他」，想严格限制就改这里并在 schema 里加 `enum`。

**接真实模型**：`.env` 填 `KIMI_API_KEY`（或任意 OpenAI 兼容 Key）后，工具选择与参数抽取才真正走模型；`tests/golden/` 仍建议先跑 mock 门禁。

**接数据源**：想把 Garmin/Strava/Apple Health 的跑步记录同步进来，在 `app/sports/store.py` 加一个 `import_workouts()`，复用 `log_workout` 的写入与去重逻辑，再挂一个管理接口；不要绕过工具注册表直接写库。

## 8. 边界（不要动）

- 不替用户对外发布：打卡文案走 `draft_text` + 确认卡，`sport.*` 里没有 `external` 工具。
- 不做医疗诊断：疼痛/胸痛/晕厥/孕期/术后一律建议专业评估；`training-coach` 与 persona 都写了这条。
- 不把模型输出当身份或授权：工具参数校验、`user_id` 注入、确认凭证都由 `tool_registry` 负责。
- 轨迹与审计照旧：训练记录是普通记忆事件，写入时经过 `memory_safe`，轨迹里以 HMAC 假名出现。

## 9. 验证

```bash
python -m pytest -q                                   # 全量（含新增 tests/test_sports_agent.py）
AUTH_SECRET=x python tests/golden/run_golden.py --mode mock
python scripts/live_smoke.py                          # 配好 Key 后的真实链路参照
```

没有 Key 也想看真实链路（工具选择 + 参数抽取 + 写入）：起一个本机假模型，再起应用。

```bash
python scripts/demo_sports_llm.py                     # 127.0.0.1:9177/v1，仅演示用
LLM_FAST_BASE_URL=http://127.0.0.1:9177/v1 LLM_FAST_API_KEY=fake \
LLM_STRONG_BASE_URL=http://127.0.0.1:9177/v1 LLM_STRONG_API_KEY=fake \
python -m uvicorn app.main:app --port 8900
```

新增测试覆盖：六个工具的注册与风险等级、档案/记录/统计、跨用户隔离、计划往返、提示词数据块、记忆卡标签、参数抽取与追问、`/api/chat` 真实路由。
