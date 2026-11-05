# 二次开发指南

写给拿到这个仓库、准备在上面二开或魔改的开发者。这不是 API 文档，是一张地图：告诉你改什么去哪里、改完要同步哪些契约测试、哪些东西不要动。各模块的细节仍在 `docs/` 里，本文只负责把你带到正确的门口。

## 0. 先跑起来

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt -c requirements.lock
cp .env.example .env
# .env 至少填 AUTH_SECRET（必填）和 KIMI_API_KEY（不填则全程 mock）
uvicorn app.main:app --port 8787
```

然后在改任何东西之前，先把门禁跑一遍，记住绿色长什么样：

```bash
python -m pytest -q                                   # 单元 + 集成，全程离线 mock
AUTH_SECRET=x python tests/golden/run_golden.py --mode mock --judge-gate   # 金标回归
```

两点需要知道：

- **测试永远是 mock 模式。** `tests/conftest.py` 会把环境里所有 `*_API_KEY` 删掉，并禁止加载 `.env`。你本地有 Key 也不影响测试结果，也不会花你的额度，这是故意的。
- **不填模型 Key 也能跑。** 聊天回离线占位、草稿是占位稿。所有流程、确认卡、记忆、定时任务都能走通。先用 mock 模式熟悉结构，再接真实模型。

## 1. 一张地图

```
app/
  main.py                 HTTP 入口。1800 行，所有路由都在这里（见第 5 节）
  graph.py                LangGraph 接线：START → router → 子图；run_action 是唯一入口
  auth.py / session_identity.py   服务端签发的会话；user_id 永远来自这里，不来自请求体
  privacy.py              进图之前的隐私门：密码、手机号、身份证拦下来转安全输入区
  gateway/                LLM 网关：FAST / STRONG 两档，模型链，降级，mock
  prompts/                版本化提示词文件，load_prompt(name, "v1")
  tools/                  可被注册的工具实现（写作、联网读取）
  agent/
    router.py             意图路由
    tool_registry.py      工具注册表：风险等级、确认门禁、超时、审计
    tool_catalog.py       评审过的工具描述
    tool_selection.py     模型选工具（选择 ≠ 授权）
    subgraphs/            chat / draft_confirm / profile 三个子图
    memory/               档案、情节、摘要、记忆卡、可选向量
    schedules/ proactive/ 定时任务与调度器
    harness/              轨迹、出站防泄露护栏
    loop.py task_loop.py runaway.py   预算循环与失控检测
  channels/               飞书、微信（OpenClaw）、渠道网关、绑定、QQ 预留位
  data_access/            数据库访问层；新代码请经这里，不要直接开 Session
static/                   原生 JS 前端，无构建链，改完刷新即见
tests/                    pytest；tests/golden/ 是金标回归
docs/                     各模块说明；docs/agent-stack.md 是最完整的一篇
```

## 2. 按改动深度分五层

### 第一层：换皮（半天）

改提示词、文案、样式、模型，不碰逻辑。

- **提示词**：`app/prompts/<名字>/v1.md`。不要改 `v1.md`，`tests/unit/test_prompt_files.py` 锁了它的哈希。新建 `v2.md`，然后把调用处的 `load_prompt("chat.persona", "v1")` 改成 `"v2"`。模板里的 `{{untrusted_data_rule}}` 这类槽位必须保留。
- **人格与语气**：主要在 `chat.persona` 和 `draft.text` 两个提示词。
- **前端文案和样式**：`static/js/*.js` 里的字符串、`static/design.css`、`static/chat.css`、`static/index.html`。没有打包步骤。
- **换模型厂商**：只改 `.env`。任何 OpenAI 兼容接口都行，见 `docs/llm-gateway.md`。单 Key 时两档会自动共用。

### 第二层：加工具（一到两天，最有价值的一层）

这是这个底座最值得复用的部分。工具注册后，确认卡、一次性凭证、超时、审计、轨迹、`/api/tools` 列表全部自动跟上。

1. **写函数并注册**。在 `app/tools/` 新建模块，参考 `app/tools/crawl.py`：

   ```python
   from app.agent.tool_registry import tool

   @tool("weather_lookup", risk_level="read",
         description="查询城市当前天气。 Look up current weather for a city.",
         when_to_use="用户问天气或出门穿衣时使用。 Use when the user asks about weather.",
         when_not_to_use="不用于历史气候数据。 Do not use for historical climate data.",
         timeout=20)
   def weather_lookup(city: str, user_id: str = "") -> dict:
       ...
   ```

   参数要带类型注解，注册表会据此生成 JSON Schema。`user_id` 和 `owner_id` 是身份参数，由服务端上下文注入，不会暴露给模型。

2. **让它在启动时加载**。把模块名加进 `app/agent/tool_registry.py` 里 `ensure_defaults()` 的导入列表。

3. **选对风险等级**，这决定了门禁行为：

   | 等级 | 含义 | 行为 |
   |------|------|------|
   | `read` | 只读查询 | 最多重试一次 |
   | `self_write` | 改用户自己的档案、记忆、任务 | 不重试 |
   | `notify_self` | 给用户自己发提醒 | 校验渠道绑定 |
   | `external` | 对外发消息、发邮件、提交表单 | **默认一个都没注册**。必须 `requires_confirm=True`，执行前要消费 `confirm_store` 签发的一次性、参数绑定的凭证 |

4. **让模型能选到它**。在 `app/agent/tool_selection.py` 的 `TOOL_ROUTES` 加一行，值是它所属的子图（`chat` 或 `draft`）。注意：**选到不等于结果会进对话**。目前只有联网工具有「结果注入对话」的通路，见 `app/agent/crawl_tools.py` 的 `source_context`：它让模型规划调用，校验参数，把结果作为不可信数据块拼进 system。新的只读工具要么加进 `CRAWL_TOOLS` 复用这条路，要么在 `app/agent/subgraphs/chat.py` 照这个模式加一个分支。定时任务工具是另一个例子，它们有自己的对话流程在 `app/agent/schedules/chat.py`。

5. **同步两处契约**，否则测试红：
   - `docs/tools.md` 的表格要加一行（名字、风险等级、是否需确认）。`tests/unit/test_tools_doc.py` 会和注册表逐项比对。
   - 描述不能是占位文字，`when_to_use` 不能和 `description` 相同。`tests/test_tool_registry.py` 会检查。
   - 可选：在 `app/agent/tool_catalog.py` 的 `_ROWS` 写一份评审过的中英双语描述和预算，它会覆盖装饰器里的文字。

### 第三层：加渠道（两到三天）

QQ、钉钉、Telegram、Slack 都走同一条路。`app/channels/qq.py` 是一个故意留空的模板，`app/channels/feishu.py` 和 `openclaw_wechat.py` 是两个完整实现。

1. 继承 `app/channels/base.py` 的 `ChannelAdapter`，实现 `enabled`、`config_status`、`render`、`send`、`parse_inbound`。
2. 环境变量开关**默认关闭**。两个现有渠道都是这样，不要破例。
3. 入站归一成 `parse_feishu_webhook` 产出的那种字典，交给 `app/channels/gateway.handle_inbound`。绑定、隐私拦截、草稿预览、确认按钮的渲染，都会自动工作。
4. 在 `app/channels/registry.py` 登记适配器，在 `app/main.py` 加 webhook 路由。
5. 卡片能力：`supports_cards = True` 的渠道渲染确认卡按钮，否则渲染文本菜单（微信回数字 `1` / `2`）。
6. 参考测试 `tests/test_channel_gateway.py`、`tests/test_channel_http.py`，用假 payload 把你的渠道走一遍。

### 第四层：改记忆（三到五天）

`app/agent/memory/` 分四层：工作记忆（LangGraph checkpoint）、档案（`profile.py`，SQLite JSON）、情节（`episodic.py`）、可选向量事实（`write_path.py` + `retrieval.py`，`MEMORY_VECTOR_ENABLED=1` 开启）。

- **加档案字段**：改 `profile.py` 的字段集合，记忆卡 `card.py` 和前端抽屉 `static/js/drawer.js` 要跟着改。
- **换向量库**：只动 `retrieval.py` 和 `write_path.py`，保持 `memory_facts_search` 工具的返回结构。现在用 sqlite-vec，嵌入走 SiliconFlow。
- **遗忘策略**：`card.py` 的 forget 路径，`/api/memory/{uid}/forget` 要求显式 `confirm: true`。
- **写入前必须脱敏**：`redact.py` 在任何东西落库前跑。不要绕过它。
- 相关测试很多，`tests/test_memory_*.py` 一共十几个文件，改完跑一遍。

### 第五层：改编排（一周以上）

- **路由**：`app/agent/router.py` 的 `route_from_start`。规则路由是默认，模型选工具在有 Key 时参与，危机规则永远最高优先。
- **图**：`app/graph.py` 的 `build_graph`。加新子图就是加一个节点和一条条件边。每个节点都要用 `wrap_node` 包起来，否则没有预算计数、护栏和轨迹。
- **需要用户确认的多步任务**：照 `app/agent/subgraphs/draft_confirm.py`。它用 LangGraph 的 `interrupt` 挂起，`/api/confirm` 带一次性凭证恢复。调试时用 `graph.get_thread_snapshot(thread_id)` 看挂起状态。
- **预算**：`budget_tiers.py` 分档，`loop.py` 四维计数，接近上限交付已完成部分并给「继续」卡。新任务类型要在 `graph.task_tier` 里归档。
- 改了编排，一定加金标用例：`tests/golden/cases/*.json`，然后更新 `baseline.json`。

## 3. 不要动的东西

下面这些是安全不变量。它们有测试守着，改坏了 CI 会红，但更重要的是理解为什么。

- **身份只来自服务端会话。** `user_id` 从签名 cookie 或请求头解出，注册表会把 `user_id` / `owner_id` 从模型可见的参数 schema 里剔掉。模型给的用户 ID 一律不信。
- **循环不能给自己授权。** `app/agent/runaway.py` 的 `EXTERNAL_TOOLS` 在 state 没有 `confirmed=True` 时直接 `blocked`。工具选择和工具授权是两件事。
- **所有出站文本过护栏。** `guard_text` 过滤密钥、系统提示词原文、他人数据、隐私沙盒值。检查失败 fail closed，返回 `E_GUARD_UNAVAILABLE` 而不是放行。
- **隐私门在图之前。** 密码、手机号、身份证在 `app/privacy.py` 被拦下，不进对话历史，不进记忆。
- **危机规则最高优先。** `crisis_rules.py` 命中后热线页脚在护栏之后追加，不会被任何过滤丢掉。
- **安全输入区从不返回明文。** `secrets_vault.py` 加密保存，`/api/secrets` 只给脱敏列表。
- **金标安全类必须零失败。** `tests/golden/` 里 crisis / leak / trace 的失败不能被整体通过率平均掉。

## 4. 提交前

```bash
python -m pytest -q
AUTH_SECRET=x INTENT_SCORING_MODE=rules python tests/golden/run_golden.py --mode mock --judge-gate
AUTH_SECRET=x python tests/golden/run_hybrid.py --mode mock --judge-gate
python scripts/harness_golden.py
python scripts/smoke_proactive_memory.py
```

这和 `.github/workflows/harness-golden.yml` 跑的一致。接了真实 Key 之后，再跑一次 `python scripts/live_smoke.py`，它会起一个临时服务，用真实模型走一遍聊天、记忆、草稿确认、危机兜底，把实际回复写进 `docs/live-smoke-report.md`。这份记录是「正常长什么样」的参照，建议每次大改后更新。

## 5. 已知的粗糙处

诚实列出来，免得你以为是自己的问题。

- **`app/main.py` 有 1800 行**，54 个路由堆在一起。`app/agent/schedules/api.py` 已经示范了用 `APIRouter` 拆出去的写法，把其余路由按 chat / confirm / memory / secrets / channels 拆成 `app/api/*.py` 是一个很好的第一个二开练习。
- **前端是原生 JS**，没有组件框架。好处是零依赖，坏处是大改 UI 费劲。换 React / Vue 需要重写前端，但后端 API 不用动，`static/js/api.js` 列出了全部调用。
- **只有 SQLite 在本地完整验证过。** Postgres 和 RLS 有代码、文档和一个 CI 任务，但本地没有跑过真实 Postgres。
- **意图评分的 hybrid 模式**依赖 SiliconFlow 的向量接口，需要先用 `scripts/embed_intents.py` 生成缓存。默认 `rules` 模式不需要。
- **飞书和微信渠道**需要各自的凭据才能真实验证，mock 模式下只能走假 payload 测试。
- `docs/tech-architecture.md` 和 `docs/技术架构.md` 内容重复，保留了两份。

## 6. 常见坑

- **接口全部 503**：`.env` 没填 `AUTH_SECRET`。这是必填项。
- **健康检查说 live，聊天却是占位回复**：某一档的模型没有对应 Key。现在会自动借用另一档的 Key 并打一条 warning 日志，看日志确认。
- **写作请求偶尔被当成普通聊天，网关日志里全是 `error: empty`**：FAST 档用了推理模型。选工具只给 160 个 token，全被隐藏思考花掉，内容为空。给 FAST 档关掉思考：`LLM_FAST_PARAMS={"enable_thinking": false}`（字段名以厂商文档为准）。
- **本地脚本请求 127.0.0.1 一律 502**：系统代理在作怪。Python `httpx` 要 `trust_env=False`，`curl` 不受影响。
- **测试里模型永远是 mock**：见第 0 节，这是设计。
- **改了提示词测试红**：你改的是 `v1.md`。新建 `v2.md` 并改 pin。
- **加了工具测试红**：`docs/tools.md` 表格没同步，或描述被判定为占位文字。
