# 统一模型网关（`app/gateway/`）

所有模型调用都走 `app.gateway.llm_gateway`（`app.llm.chat_text/chat_json` 只是转发）。
`chat_text(system, user, *, tier, fallback, timeout, task, ...)` 签名不变，新增可选 `max_tokens`。

```
task ──route()──▶ tier (fast/strong) ──▶ 有序模型链 [m1, m2, ...]
                                            │  每个模型：账号级限流 → 调用 → 失败/超时/429/空 → 下一个
                                            └─ 记录每次上游调用：模型、耗时、排队、token、估算费用
```

| 模块 | 作用 |
| --- | --- |
| `config.py` | 模型注册表 + tier 链 + task→tier；合并 env / 配置文件 |
| `providers.py` | 协议：OpenAI 兼容 `/chat/completions`、Anthropic `/v1/messages`；按 (base_url,key) 复用 httpx 连接池 |
| `limiter.py` | 按上游账号（同 key 的 k2.6 + k3 共用）自适应并发窗口（429 收缩、成功回升）+ 可选 RPM |
| `core.py` | 链执行：重试、Retry-After、总截止时间、降级、FAST 空内容升 STRONG |
| `usage.py` | 调用记录：`llm.usage` 日志 JSON 行、内存环形缓冲、可选 JSONL 文件 |

## 默认行为（不配任何新东西）

沿用现有 env（`LLM_FAST_*` / `LLM_STRONG_*` / `KIMI_API_KEY` …），经 `app.llm.resolve_tier` 解析：

- `fast = [kimi-k2.6]`（`.env.example` / `render.yaml` 的取值；代码内置默认是 `deepseek-chat`，由 `MODEL_FAST` / `LLM_FAST_MODEL` 覆盖）
- `strong = [kimi-k3, kimi-k2.6]`（`LLM_STRONG_FALLBACK_MODEL`，原来只在 k3 空内容时回退，现在错误/超时也回退）
- FAST 空内容 → 升级 STRONG 链一次（`LLM_EMPTY_ESCALATE=0` 关闭）
- Kimi 特性保持：k2.6 `thinking: disabled`，k3 `reasoning_effort`（`KIMI_K3_EFFORT`），不传 temperature
- 其他厂商的附加字段：`LLM_FAST_PARAMS` / `LLM_STRONG_PARAMS`（JSON 对象，原样并入请求体）。推理模型会把选工具、分类器这类小 `max_tokens` 预算全花在隐藏思考上、返回空内容，FAST 档建议关掉思考，例如 `LLM_FAST_PARAMS={"enable_thinking": false}`
- 超时默认值不变：文本 20s/45s，JSON 25s/60s（`LLM_FAST_TIMEOUT` / `LLM_STRONG_TIMEOUT` 覆盖）

## 声明多个模型

三种方式，后者覆盖前者：

1. `LLM_CONFIG_FILE=/app/config/llm_models.json`（`.toml` 亦可）
2. `LLM_CONFIG_JSON='{"models":{...},"tiers":{...}}'`（PaaS 只能配 env 时用）
3. `LLM_FAST_CHAIN=kimi-k2.6,deepseek-chat` / `LLM_STRONG_CHAIN=...`（只改顺序，id 须已声明）

示例见 [`llm_models.example.json`](llm_models.example.json)（**价格为占位，请按官网填写**）：

```json
{
  "providers": {
    "moonshot":  {"protocol": "openai",    "base_url": "https://api.moonshot.cn/v1", "api_key_env": "KIMI_API_KEY,MOONSHOT_API_KEY", "max_concurrency": 4},
    "anthropic": {"protocol": "anthropic", "base_url": "https://api.anthropic.com",  "api_key_env": "ANTHROPIC_API_KEY"}
  },
  "models": {
    "kimi-k2.6":     {"provider": "moonshot",  "model": "kimi-k2.6", "price": {"input": 4.0, "output": 16.0, "currency": "CNY"}},
    "kimi-k3":       {"provider": "moonshot",  "model": "kimi-k3"},
    "claude-sonnet": {"provider": "anthropic", "model": "claude-sonnet-4-5", "max_tokens": 2048, "timeout": 60}
  },
  "tiers": {"fast": ["kimi-k2.6"], "strong": ["kimi-k3", "claude-sonnet", "kimi-k2.6"]},
  "tasks": {"greeting": "strong"}
}
```

模型字段：`provider`、`protocol`(`openai`|`anthropic`)、`base_url`、`api_key_env`（逗号分隔多个 env 名，按序取第一个非空）、
`model`、`price.input/output`（每百万 token）、`price.currency`、`timeout`、`max_tokens`、`params`（原样并入请求体）、
`max_concurrency` / `rpm` / `limit_group`。provider 上的字段被模型继承，模型可覆盖。
没有 key 的模型在链中自动跳过；整条链都没 key → mock（与原来一致）。

`tasks` 值可为 `fast` / `strong` 或某个模型 id。未知 task 按难度分档（`app/agent/budget_tiers.py`）。

旧 env 方式下想算费用：`LLM_FAST_PRICE_INPUT/OUTPUT`、`LLM_STRONG_PRICE_INPUT/OUTPUT`、`LLM_STRONG_FALLBACK_PRICE_INPUT/OUTPUT`。

## 向量记忆基础（阶段 A）

`MEMORY_VECTOR_ENABLED` 默认关闭。关闭时，新的事实 DAL 和 embedding 客户端直接返回空结果，不读写事实、不调用模型；建表仍走 `init_db()`。现有画像、事件和聊天行为不变。

`app.gateway.embeddings.embed_text` 的任务名为 `memory_embed`，默认使用 SiliconFlow
`https://api.siliconflow.cn/v1` 的 `BAAI/bge-m3`（1024 维），通过现有 `ModelSpec.api_key()`
读取 `SILICONFLOW_API_KEY`。可在原有 `LLM_CONFIG_FILE` / `LLM_CONFIG_JSON` 中声明模型，
再用 `"embedding_tasks": {"memory_embed": "已声明的模型 id"}` 覆盖路由；聊天的 `tasks` / `tiers` 不变。
客户端复用网关 HTTP 连接池和错误分类，缺少 key 或返回无效向量会报错，不生成假向量。

`app.data_access.memory_facts` 提供 `add_fact`、`get_fact`、`invalidate_fact`、`delete_fact`、
`forget_all`、`knn(query_vec, k)`。必须处于服务器设置的 `user_scope`；传入的 `user_id` 被忽略。
向量以 float32 BLOB 存在事实行内，物理删除同时删除向量。搜索用 sqlite-vec 的
`vec_distance_cosine` 对当前用户有效行做精确 top-k（无独立向量索引）；扩展无法加载时回退到
Python cosine，并在进程内只告警一次。两种搜索都在排序、截断之前限制用户和 `valid_until IS NULL`，
耗时随该用户有效事实数线性增长。阶段 A 未接入自动抽取、聊天检索或历史数据迁移。

## 稳定性参数

| env | 默认 | 说明 |
| --- | --- | --- |
| `LLM_MAX_CONCURRENCY` | 8 | 每个上游账号的并发上限（**建议设成 Moonshot 控制台里账号的并发额度**） |
| `LLM_RPM` | 0 | 每账号每分钟请求数，0 = 不限 |
| `LLM_QUEUE_TIMEOUT` | 10 | 本地排队最长秒数，超时视为 busy → 换下一个模型 |
| `LLM_MAX_RETRIES` | 2 | 同一模型对 5xx/连接错误的重试次数 |
| `LLM_MAX_RETRIES_RATE_LIMITED` | 4 | 同一模型对 429 的重试次数（指数退避 + Retry-After，封顶 `LLM_RETRY_AFTER_MAX`=5s） |
| `LLM_RETRY_ON_TIMEOUT` | 0 | 超时默认不重试同模型，直接降级（重试只会翻倍等待） |
| `LLM_DEADLINE_FACTOR` | 2.0 | 单次 `chat_text` 总耗时上限 = 单次超时 × 系数（含重试、降级、排队） |
| `LLM_HTTP_MAX_CONNECTIONS` / `LLM_HTTP_MAX_KEEPALIVE` | 32 / 16 | httpx 连接池 |
| `LLM_USAGE_LOG` | 空 | 设为路径则把每次调用追加写入 JSONL |

401/403/400 不重试，直接换下一个模型。SDK 不再参与重试（原来 openai SDK 默认 `max_retries=2` 叠加我们自己的 3 次，单次调用最多 9 次 HTTP）。

## 观测

- 日志：logger `llm.usage`，每次上游尝试一行 JSON：`request_id, task, tier, model_id, model, attempt, ok, error, status, latency_ms, queue_ms, prompt_tokens, completion_tokens, cost, currency`
- `GET /api/llm/usage?limit=50`：按模型汇总（调用数、成功、错误分类、token、费用、平均耗时）+ 最近记录 + 限流器状态（窗口、排队、429 次数）
- `/health` → `gateway.models` / `gateway.usage` / `gateway.limits`（不含密钥）

## 压测（本地，无需真 key）

```bash
python scripts/fake_llm_upstream.py --port 9100 --max-concurrency 4 --latency 1.5 &
KIMI_API_KEY=sk-fake LLM_FAST_BASE_URL=http://127.0.0.1:9100/v1 LLM_FAST_MODEL=kimi-k2.6 \
LLM_STRONG_BASE_URL=http://127.0.0.1:9100/v1 LLM_STRONG_MODEL=kimi-k3 \
  uvicorn app.main:app --port 8788 &
python scripts/load_chat.py --base http://127.0.0.1:8788 -c 8 -n 34
```

## 并发 502 排查（QA：8 并发 34 请求 9 个 502）

**结论：502 不是应用返回的**（代码里没有任何 502；本地复现两版都是 34/34 200），是 Render 边缘代理拿不到实例的有效响应。
最可能的链路是：每条消息 3 次串行 LLM 调用 → Moonshot 账号并发被打满 → 429 → 旧代码叠加重试放大请求、每次新建客户端/TLS → 请求长时间占住实例（free 档 0.1 CPU / 512MB），代理侧超时或实例被判不健康重启 → 502。需要 Render 后台 Events / Logs（同时段是否有重启、OOM、健康检查失败）最终确认。

本地复现（假上游：账号并发 4、超出即 429、回复延迟 1.5s±0.5s；8 并发 × 34 条闲聊消息）：

| | 旧网关 | 新网关（默认参数） |
| --- | --- | --- |
| HTTP 状态 | 34×200 | 34×200 |
| 降级回复（"刚才我这边有点卡"） | 3 | **0** |
| 上游请求数（逻辑调用 102） | 205 | ~120 |
| 上游 429 | 115–122 | 16–18 |
| 延迟 p50 / p95 / max | 3.9s / 22s / 22s | 6.7s / 12–13.5s / 14s |
| 压测后进程 socket 数 | 78（依赖 GC 回收） | 10（连接池复用） |

发现：

1. **每条闲聊消息 = 3 次串行 LLM 调用**：危机分类器（`_llm_risk`，8s 超时）→ 正式回复（20s）→ 对话摘要（`maybe_summarize_turn`，20s）。8 并发瞬间就是 24 个上游请求，三分之二不是用户在等的回复。
2. **重试放大**：旧代码每次调用新建 `OpenAI()` 客户端（SDK 自带 `max_retries=2`），外面又套 `_create_with_backoff` 3 次，429 时最多 9 次 HTTP；本地测得上游请求量是逻辑调用的 2 倍，p95 22s。
3. **连接没复用**：每次调用新建 httpx 客户端 → 每次新 TLS 握手（Render 美西 → moonshot.cn 跨洋，握手本身就是几百毫秒，还吃 0.1 CPU），socket 靠 GC 回收。
4. **过载时的副作用**：分类器超时/429 会走"弱信号兜底"，带"累""算了"等字的消息会被判成危机并附热线——负载问题会表现成误报。

本 PR 已做：连接池复用、只保留一层重试、超时不重试直接降级、429 按 Retry-After 退避、账号级自适应并发（429 时收窄窗口，多余请求在本地排队而不是继续打上游）、总截止时间、按 task 打标签的调用记录。

建议（未在本 PR 改动业务流程，需产品确认）：

1. **把 `LLM_MAX_CONCURRENCY` 设成 Moonshot 账号的实际并发额度**（控制台查看账号等级），这样基本不会再出现 429。
2. **摘要移出关键路径**：`maybe_summarize_turn` 改为后台线程/队列执行，或每 N 轮做一次。用户等待少一次 LLM 调用，峰值并发也能削掉。
3. **分类器降频**：关键词已命中时本就不调；可再改为"只有出现弱信号才调 LLM 分类器"，或与正式回复并行发起（命中再补救）。涉及安全策略，需评估漏判风险。
4. **Render 侧**：free 档 0.1 CPU 撑不住 8 并发的 TLS + LangGraph；升级 Starter 或以上，并查看 QA 时段的 Events 是否有重启/OOM。
5. 用 `GET /api/llm/usage` 观察 `throttled_429`、`waited`、各模型平均耗时，以此调并发额度。

