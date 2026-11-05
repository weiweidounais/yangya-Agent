# Langfuse 自托管 + 接入说明（测试机：Ubuntu + nginx）

本文覆盖：在测试机上部署 Langfuse v3、nginx/TLS、应用侧开关、金标/评测接入、备份与排障。
应用代码默认 **不开启** 导出；不配置 `LANGFUSE_*` 时行为与之前完全一致。

## 1. 导出了什么 / 不导出什么

| 层级 | Langfuse 观测类型 | 名称 | 主要字段 |
|---|---|---|---|
| 请求 | span（trace 根） | `POST /api/chat` 等 | 路由、状态码、耗时、trace 输入/输出（脱敏）、tags（`node:chat`、`guardrail_hit`…） |
| 图节点 | agent | `node.chat` / `node.draft` … | intent、step、ok、错误码、模型档位、LLM 调用数/失败数、token 估算、护栏命中数与类型 |
| LLM 调用 | generation | `llm.<task>` | 请求模型 / 实际模型、重试次数与错误、回退原因、真实 usage（无则估算并标注 `usage_source`）、成本（`LLM_PRICES_JSON`）、prompt 名称 + 版本哈希 |
| 工具 | tool | `tool.draft_text` 等 | 参数/结果的形状（长度、键名），不含正文 |
| 护栏 | span event | `guardrail.no_leak` 等 | 命中类型、动作（mask / blocked / withheld / fail_closed） |

本地脱敏在导出 **之前** 完成，两层：

1. 设置属性时：当前用户隐私沙盒值、密钥（环境变量值 + 常见 key 形态）、手机号/邮箱/身份证/姓名/地址（`PII_BACKEND`）全部打码，长文本截断到 2000 字；
2. `RedactingExporter` 在导出线程里对每个 span 的所有字符串属性和事件再扫一遍；扫描失败的 span 直接丢弃，不会原样发出。

用户 id / 会话 id 只以 HMAC 假名导出（`u_…` / `t_…`，密钥 `HARNESS_ID_HMAC_KEY`），与本地 `agent_traces` 中存的一致。
`LANGFUSE_EXPORT_IO=none` 时连脱敏后的正文都不导出，只有元数据。

可靠性：span 走 `BatchSpanProcessor`（有界队列 + 后台线程，满了丢弃而不是阻塞）；Langfuse 不可达、鉴权失败、
OTel 内部异常都只记日志，不影响聊天。反馈打分走后台队列，同样不阻塞请求。

## 2. 机器准备

建议 4 vCPU / 16 GB 内存 / 100 GB SSD（ClickHouse 是大头；最低 2 vCPU / 8 GB 可跑测试量）。

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl nginx certbot python3-certbot-nginx ufw
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"   # 重新登录生效
sudo ufw allow OpenSSH && sudo ufw allow 'Nginx Full' && sudo ufw enable
```

DNS：把 `langfuse.example.com`（替换成实际域名）的 A 记录指向测试机。

## 3. 启动 Langfuse

```bash
git clone <仓库地址> personal-agent && cd personal-agent
cp deploy/langfuse/.env.example deploy/langfuse/.env && chmod 600 deploy/langfuse/.env
# 编辑 .env：所有 CHANGE_ME；ENCRYPTION_KEY 用 `openssl rand -hex 32`；
# NEXTAUTH_URL=https://langfuse.example.com；
# LANGFUSE_INIT_PROJECT_PUBLIC_KEY / SECRET_KEY 自定（pk-lf-… / sk-lf-…），应用就用这对 key。
docker compose -f deploy/langfuse/docker-compose.yml --env-file deploy/langfuse/.env up -d
docker compose -f deploy/langfuse/docker-compose.yml --env-file deploy/langfuse/.env ps
curl -fsS http://127.0.0.1:3000/api/public/health
```

所有端口只绑定 `127.0.0.1`；对外只有 nginx。首次启动 ClickHouse 迁移需要 1–3 分钟。
启动成功后把 `LANGFUSE_VERSION` 固定到具体版本（`docker image ls langfuse/langfuse`），升级时再显式修改。

## 4. nginx + TLS

```bash
sudo cp deploy/langfuse/nginx.conf /etc/nginx/sites-available/langfuse
sudo sed -i 's/langfuse.example.com/<你的域名>/g' /etc/nginx/sites-available/langfuse
# 先只启用 80 端口那段拿证书：
sudo certbot certonly --nginx -d <你的域名>
sudo ln -s /etc/nginx/sites-available/langfuse /etc/nginx/sites-enabled/langfuse
sudo nginx -t && sudo systemctl reload nginx
```

`client_max_body_size 20m` 给 OTLP 批量上报留了余量。如需只允许办公网访问 UI，
在 `location /` 前加 allow/deny，但保留 `/api/public/`（应用和 CI 用 API key 鉴权）。

## 5. 应用侧配置（Render / 测试机 / 本地 `.env`）

```bash
LANGFUSE_ENABLED=1
LANGFUSE_HOST=https://langfuse.example.com
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_ENVIRONMENT=test            # trace 环境标签
LANGFUSE_RELEASE=$GIT_SHA            # 可选；Render 上默认读 RENDER_GIT_COMMIT
LANGFUSE_SAMPLE_RATE=1.0             # 0..1
LANGFUSE_EXPORT_IO=redacted          # redacted | none
HARNESS_ID_HMAC_KEY=<>=16 位随机串>  # 固定后 trace 里的用户假名跨重启稳定
LLM_PRICES_JSON='{"kimi-k2.6":{"input":4,"output":16},"kimi-k3":{"input":8,"output":32},"*":{"input":0,"output":0}}'
LLM_PRICE_CURRENCY=CNY
```

验证：`GET /health` → `gateway.observability.langfuse == true`；发一条聊天，响应头里有 `X-Trace-Id`，
在 Langfuse → Traces 里能按这个 id 找到 `POST /api/chat → node.chat → llm.chat` 的层级。

本地也能通过 `GET /api/admin/traces?trace_id=<X-Trace-Id>` 查到同一请求的轨迹行（行内有 `trace_id` / `span_id`）。

## 6. Prompt 版本

```bash
python -m app.evals.cli push-prompts --label production
```

把所有注册的系统提示词发布为 `personal-agent/<name>`（label `sha-<哈希>`），评审 rubric 发布为 `judge/<name>`。
版本号写入 `DATA_DIR/langfuse_prompts.json`，之后的 generation 会直接关联到 Langfuse prompt；
没推送时 generation 仍带 `prompt_name` / `prompt_version`（内容哈希）元数据。

## 7. 评测（LLM-as-judge）

三个 rubric：`empathy`（安慰质量）、`crisis_handling`（危机回应，安全类）、`fabrication`（编造，安全类）。

- **离线**：`python tests/golden/run_golden.py --judge`（mock 模式用确定性启发式；`--judge-gate` 低于阈值即判失败，CI 已开启）。
  单条：`python -m app.evals.cli judge --rubric empathy --input "又被拒了" --output "..."`。
- **Langfuse 托管评估器**：`push-prompts` 之后，在 Langfuse → Evaluators → New，选择 `judge/empathy` 等 prompt 作为模板，
  变量映射 `{{input}}` → trace input、`{{output}}` → trace output，过滤条件 tag `node:chat`（危机评估器再加过滤
  或直接对全部 chat trace 运行）。需要在 Langfuse 里配置一个 LLM 连接（Settings → LLM Connections）。
- **外部评估器（不依赖托管功能）**：定时跑
  `python -m app.evals.cli evaluate-traces --hours 24 --limit 200`，拉取最近 trace，按内容选 rubric，回写 `judge.<rubric>` 分数；已打过分的跳过。

## 8. 金标 → 数据集 / 实验

```bash
python tests/golden/run_golden.py --mode live --repeat 3 --judge --langfuse
```

- 每个 case upsert 为数据集 `golden-live`（或 `golden-mock`）的条目 `golden-<case_id>`；
- 本次运行记录为数据集 run（`golden-<mode>-<sha>-<run_id>`，重复 3 次时每次一个 run，后缀 `-r1..-r3`），链接到每个 case 最后一步的 trace；
- 每个 trace 打 `golden.pass`（布尔）和 `judge.*`（0–1）分数；在 Langfuse 数据集页面可直接对比各次运行。

报告（JSON / Markdown）内嵌运行清单：git sha、依赖版本、模型路由、case / fixture / prompt / rubric 哈希、开关与参数。

## 9. 用户反馈

聊天气泡下的 👍 / 👎 调 `POST /api/feedback`（带响应头里的 trace id），后端校验这条 trace 属于该用户后：
本地存一条 `feedback`，并异步向 Langfuse 写分数 `user_feedback`（+1 / −1，附脱敏后的评论）。

管理员一键入库：`POST /api/admin/feedback/{id}/to-dataset`（需要 `feedback:write` 权限的管理员令牌），
把这条 trace 的脱敏轨迹写入数据集 `LANGFUSE_FEEDBACK_DATASET`（默认 `user-feedback`，`sourceTraceId` 关联原 trace）；
Langfuse 未启用时写到 `DATA_DIR/golden_candidates/*.json`，可人工整理成新的金标 case。

## 10. 夜间 live 金标（GitHub Actions）

`.github/workflows/golden-nightly.yml`：每天 03:17（北京时间）跑 live，每个 case 3 次；安全类必须 3/3 通过，质量按逐次通过率对比 `baseline.json`。
需要的配置：

- Secrets：`LLM_FAST_API_KEY` / `LLM_STRONG_API_KEY`（或 `DEEPSEEK_API_KEY` 等），`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`
- Variables：`LANGFUSE_HOST`、`LLM_FAST_MODEL`、`LLM_STRONG_MODEL`、`LLM_FAST_BASE_URL`、`LLM_STRONG_BASE_URL`、`LLM_PRICES_JSON`

没有 LLM key 时 job 输出 notice 并跳过；没有 Langfuse secret 时只出报告（artifact 保留 30 天）。

## 11. 备份 / 升级 / 安全

```bash
# Postgres（项目、用户、prompt、数据集、分数元数据）
docker compose -f deploy/langfuse/docker-compose.yml exec -T postgres pg_dump -U langfuse langfuse | gzip > langfuse-pg-$(date +%F).sql.gz
# ClickHouse（traces / observations / scores）与 MinIO（原始事件）：按卷快照
docker run --rm -v langfuse_langfuse_clickhouse_data:/v -v "$PWD":/b alpine tar czf /b/clickhouse-$(date +%F).tgz -C /v .
docker run --rm -v langfuse_langfuse_minio_data:/v -v "$PWD":/b alpine tar czf /b/minio-$(date +%F).tgz -C /v .
```

升级：修改 `.env` 的 `LANGFUSE_VERSION` → `docker compose pull && docker compose up -d`（先备份；跨大版本看官方迁移说明）。

安全清单：注册关闭（`AUTH_DISABLE_SIGNUP=true`）；`.env` 600 权限；只有 nginx 对外；定期轮换项目 API key（Settings → API Keys，
轮换后更新应用与 CI secret）；测试机数据保留期在 Langfuse 项目设置里配置（建议 30 天）。

## 12. 排障

| 现象 | 排查 |
|---|---|
| `/health` 中 `observability.reason` 不为空 | 读原因：开关没开 / host 不是 http(s) / 缺 key |
| 有 `X-Trace-Id` 但 Langfuse 里没有 | 应用日志里的 `span export failed`；`curl -u pk:sk https://<域名>/api/public/health`；nginx `client_max_body_size`；worker 日志 `docker compose logs langfuse-worker` |
| 401 | public/secret key 配反或属于别的项目 |
| trace 层级断开 | 确认请求经过 `/api/*`（middleware 只追踪 API 路由）；后台任务（proactive）不在请求 trace 下 |
| 反馈返回 404 | trace id 不属于该用户，或该请求没有产生轨迹行 |
