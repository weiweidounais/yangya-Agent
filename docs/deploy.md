# 部署到 Render（GitHub 一键）

本服务是 **FastAPI**，不能用 GitHub Pages。推荐用 Render 免费 Web Service，从本仓库一键拉起。

## 一键部署

打开（需已登录 Render，并授权 GitHub 仓库）：

**https://render.com/deploy?repo=https://github.com/weiweidounais/yangya-Agent**（fork 后把仓库地址换成你自己的）

会读取根目录 `render.yaml` Blueprint，创建 Docker Web Service（free）。

也可在 Render Dashboard → **New** → **Blueprint** → 选本仓库。

## 必须配置的密钥（Environment）

在 Render 服务的 **Environment** 里填写（`sync: false` 的项不会从 Blueprint 自动带值）：

| 变量 | 说明 |
|------|------|
| `AUTH_SECRET` | **必填**，Blueprint 自动生成（`generateValue`）。缺失时所有 `/api` 返回 503 |
| `HARNESS_ID_HMAC_KEY` | Blueprint 自动生成。轨迹里的用户假名密钥，保持不变才能前后对得上 |
| `SECRETS_FERNET_KEY` | 安全输入区加密密钥（Fernet）。生成：`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `HARNESS_ADMIN_TOKENS` | 可选。管理接口令牌（`name:token[:scope]`，≥16 位），不填则管理接口关闭 |
| `KIMI_API_KEY` | **建议必填**。Moonshot/Kimi Key，用于真机对话 |
| `LLM_FAST_API_KEY` / `LLM_STRONG_API_KEY` | 可选；空则回退用 `KIMI_API_KEY` / `MOONSHOT_API_KEY` |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 可选。飞书通道；公网部署建议先关长连接（Blueprint 默认 `FEISHU_WS_ENABLED=0`） |
| `OPENCLAW_*` | 可选。微信 OpenClaw 桥 |

Blueprint 已预置：FAST/STRONG 都走 Kimi（`kimi-k2.6` / `kimi-k3`）、`CORS_ALLOW_ORIGINS=*`、`DATA_DIR=/app/data`。

不填任何 LLM Key 也能启动，但聊天走 **mock**（草稿为离线占位稿），仅便于冒烟。

## 部署完成后

1. Render 给一个 `https://xxx.onrender.com` 地址  
2. 浏览器打开根路径 `/` → 对话页；`/health` → 健康检查  
3. Mac 直接用该 HTTPS 地址即可聊（同域 API，无需额外 CORS）

免费实例**冷启动**可能要等几十秒。

> ⚠️ **免费套餐只适合演示**：磁盘非持久，每次重新部署或重启，`DATA_DIR` 里的全部数据（用户档案、对话、记忆、定时任务）都会清空。要给真实用户用，需升级到付费套餐并加 Disk 挂到 `/app/data`（`render.yaml` 里有注释示例）。

## 本地 Docker 自检（可选）

```bash
docker build -t personal-agent .
docker run --rm -p 8787:8787 -e KIMI_API_KEY=你的key personal-agent
# 打开 http://127.0.0.1:8787/
```

## GitHub Actions（必需）

部署必须通过门禁工作流 `.github/workflows/deploy-render.yml`，并在仓库 Secrets 中配置 `RENDER_API_KEY`（以及可选 `RENDER_SERVICE_ID`）。

## 相关文件

- `Dockerfile` — 生产镜像  
- `render.yaml` — Render Blueprint  
- `.dockerignore` — 排除 `.venv` / `data` / `.env`  
