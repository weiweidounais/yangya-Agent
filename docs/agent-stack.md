# Personal Agent 栈

> LangGraph-centric · 显式循环预算 · 人在回路确认 · 三层记忆 · 主动调度

## 结构

```
app/agent/
  router.py              # 意图路由：update_profile / draft / confirm / chat
  intent_scoring.py      # 可选混合评分（crisis / unresolved 安全网）
  tool_registry.py       # 工具注册表（风险等级、确认门禁、超时、审计）
  tool_catalog.py        # 评审过的工具描述与预算
  tool_selection.py      # 模型选工具（选择≠授权）
  loop.py                # 任务预算：步数 / token / 费用 / 墙钟；超限进度报告与「继续」卡
  task_loop.py           # observe → plan → tool → validate 循环引擎
  runaway.py             # 失控检测（重复调用、重复错误、无进展、震荡）
  node_wrap.py           # 节点包装：计步 + 计量 + 护栏 + 轨迹
  ask_cards.py           # 结构化提问卡（single / multi / text / secret）
  context_assembler.py   # 有预算的提示上下文：近期对话、当前草稿、摘要、记忆
  crawl_tools.py         # 模型规划的只读联网读取
  proactive/             # 调度器（默认 OFF）：tick → 运行到期的用户定时任务
  schedules/             # 一次性提醒、自定义只读任务：确认卡、触发器、运行账本
  subgraphs/
    chat.py              # 对话（记忆上下文、危机识别、提问卡）
    draft_confirm.py     # 写作草稿 + 确认卡 + 拒绝自动重写
    profile.py           # 通用档案合并
  memory/
    profile.py           # 档案记忆（name/city/tz/occupation/interests/preferences/facts + 对话摘要）
    episodic.py          # 情节事件 + 关键词/标签检索
    summary.py           # 后台对话摘要 → 档案 + episodic
    card.py              # 用户可见的记忆卡（查看 / 修改 / 遗忘）
    write_path.py        # 可选向量事实抽取与对账
  harness/
    traces.py            # 步骤轨迹
    guardrails.py        # 出站泄露护栏
    leak.py / pii.py     # 密钥、系统提示词、他人数据、隐私沙盒
app/graph.py             # 接线：START→router→子图；reject→draft 循环；wrap_up
app/gateway/             # FAST/STRONG + 模型链 + 降级 + 限流 + 用量
app/channels/            # 入站 → run_action → 渲染 → 出站
app/tools/writing.py     # draft_text 工具（离线有占位稿）
app/tools/crawl.py       # web_fetch / web_search / fetch_feed / get_hn / search_social
```

## 图

```
START ─ route_from_start ─┬─ chat ──────────────── END
                          ├─ update_profile ────── END
                          ├─ draft ─ await_confirm ─┬─ (confirmed) END
                          │                         └─ (rejected, 可重写) draft
                          └─ await_confirm (resume) ─ 同上
wrap_up：预算接近上限时交付已完成部分 + 「继续」卡
```

`run_action(payload, thread_id)` 是唯一入口；HTTP、渠道、定时任务都走它。`_resume` 载荷恢复中断（确认 / 拒绝 + 原因 + 编辑）。

## Agent Loop

`task_loop.run_task_loop` 每轮 observe → plan → tool → validate。校验失败的原因写入 `loop_feedback`，下一轮带着原因重试，最多 `1 + AGENT_MAX_RETRIES` 次。`EXTERNAL_TOOLS`（发消息 / 发邮件 / 提交表单）在 state 没有 `confirmed=True` 时直接 `blocked`，循环不能给自己授权。

- **出稿**（`draft_confirm.node_draft`）：校验 = 正文非空且不超过 `MAX_BODY_CHARS`；用尽重试 → 交付最后一版并在确认卡注明「请重点检查」。
- **拒绝重写**：确认卡拒绝（可带原因）→ `add_feedback(user_reject)` → 预算内自动重写一版，再次出卡；超过 `AGENT_MAX_REDRAFTS` 则请用户换个说法。
- **预算**：按任务难度分档（simple / medium / hard），步数、token、费用、墙钟四个维度；接近上限先升一档，再不够就交付并给「继续」卡（+50%）。用户日 / 月费用上限独立生效。

## 意图

| 来源 | 结果 |
|------|------|
| HTTP `action` | 直接映射 |
| 恢复中断 | confirm |
| 模型工具选择（有 key） | `draft_text` → draft；`schedule.*` → 对话里走定时任务流程；联网工具 → chat 内带来源 |
| 离线规则 | 确认 / 拒绝短命令 → confirm；`looks_like_draft_request` → draft；其余 chat |
| 混合评分（`INTENT_SCORING_MODE=hybrid`） | 只用作 crisis 覆盖和 unresolved 澄清，不改变任务路由 |

## 记忆

| 层 | 存储 | 用途 |
|----|------|------|
| 工作 | LangGraph checkpoint（SQLite） | 线程内状态、草稿、预算计数 |
| 档案 | `user_profiles` JSON | 通用字段 + `dialogue_summary`；名字从对话里自动记住（`remember_name`） |
| 情节 | `episodic_events` | 关键词 / 标签检索，进入对话上下文 |
| 事实（可选） | `memory_facts` 向量 | `MEMORY_VECTOR_ENABLED=1` 时后台抽取、对账、确认 |

用户可在记忆卡上修改 / 删除任一条，`memory_rev` 递增使旧的后台摘要失效。

## 主动调度

`PROACTIVE_ENABLED=1` 启动进程内 tick。每次 tick 运行到期的 `user_schedules`（一次性提醒、自定义只读任务），产出写入 `proactive_items`（应用内收件箱），并按任务的 `delivery` 推送到已绑定渠道。自定义任务只允许白名单只读工具（联网读取、记忆查询），结果由模型汇总成中文报告。
