# 多渠道接入（飞书 / 微信）

两个渠道共用一套渠道层。Web 端的对话、记忆看的是同一份数据。**飞书、微信默认都关**，用环境变量单独打开。

## 架构

```
飞书长连接 (lark-oapi ws) ─┐                         ┌─> FeishuAdapter.render → 交互卡片 → 飞书 API
                           ├─> ChannelInbound ─> gateway.handle_inbound ─┤
OpenClaw 桥接插件 (HTTP) ──┘   （归一化入站）       │                       └─> OpenClawWeChatAdapter.render → 文本 + 数字菜单
                                                     │                             → 同步 HTTP 回复 / Gateway /tools/invoke 推送
                                                     ├─ 1. 群聊：只提示去私聊
                                                     ├─ 2. 未绑定：回一次性绑定链接（/bind）
                                                     ├─ 3. 隐私检测：手机号 / 邮箱 / 身份证 / 银行卡 → 回安全通道链接（/sandbox）
                                                     ├─ 4. 命令：确认 / 拒绝 / 解绑 / 隐私
                                                     └─ 5. agent：意图路由 → 同一个 LangGraph thread
```

| 模块 | 作用 |
| --- | --- |
| `app/channels/base.py` | `ChannelInbound`、`ChannelReply`（文本 + 选项 + 链接）、`ChannelAdapter` 抽象类 |
| `app/channels/registry.py` | 渠道注册表；`/health`、`/api/channels` 里的渠道状态 |
| `app/channels/gateway.py` | 统一处理入口 `handle_inbound`、出站 `deliver_result` / `notify` |
| `app/channels/links.py` | 一次性链接 token（只存 sha256、带过期时间、只能用一次） |
| `app/channels/privacy.py` | 隐私识别与脱敏（银行卡校验 Luhn） |
| `app/channels/conversation.py` | 每个渠道用户的会话 thread、待选菜单（微信回数字用） |
| `app/channels/feishu*.py` | 飞书长连接、卡片渲染、HTTP 备用回调 |
| `app/channels/openclaw_wechat.py` | 微信入站解析、文本渲染、Gateway 推送 |
| `app/channels/qq.py` | 预留的扩展示例，未实现 |
| `integrations/openclaw-personal-agent-bridge/` | OpenClaw 插件，把微信消息交给本服务 |

### 会话 thread

- 闲聊：每个用户一个固定 thread（`chan-<hash>`），飞书和微信共用，所以换渠道也能接着聊。
- 一份草稿一个 thread：确认卡片里带着 `thread_id`，飞书点按钮、微信回 `1` 都落到同一个 LangGraph interrupt 上恢复。

## 绑定流程（走对话，无 demo 兜底）

1. 未绑定用户发任何消息 → 回「先绑定账号」和 `PUBLIC_BASE_URL/bind?t=…`（15 分钟有效，只能用一次）。
2. 用户用常用浏览器打开链接：页面复用（没有就新建）该浏览器的网页会话，用户点「确认绑定」。服务端只认会话 cookie 里签名的 `user_id`，不收任何账号密码。没有会话时返回 401，链接不作废。
3. 写入绑定 → 渠道里主动推送「绑定成功」。之后渠道和这个浏览器的网页版共用一份档案。
4. 已绑定用户发「绑定 / 我是谁」可以看到脱敏后的账号；发「解绑」时出确认选项。

`demo` / `anonymous` / `guest` / 空 id 都视为未绑定，也不能被绑定。运维接口 `POST /api/channels/bind`、`/bindings`、`/auto-bind` 需要 `CHANNEL_ADMIN_TOKEN`，没配就返回 403。

## 隐私

- 渠道消息里有手机号、邮箱、身份证、银行卡时，**不处理也不保存**这条消息，改回 `/sandbox?t=…` 安全通道链接（30 分钟有效）。
- 出站文本也会过一遍脱敏；agent 回复里如果在向用户要隐私信息，会自动附上安全通道链接。
- 草稿全文不发进聊天，只发预览，再加一个「在网页查看完整草稿」的链接。
- 安全通道页 `/sandbox`：在这里提交姓名、城市、手机号、邮箱、备注，写入用户档案；渠道里只收到「已收到你的手机号，档案已更新」这类提示，不带具体内容。

## 飞书

- 官方 SDK 长连接：`lark.ws.Client`，**不需要公网地址**。
- 卡片：确认 / 拒绝都是交互卡片按钮，按钮 `value` 里带 `cmd` + `thread_id`。点击回调 `card.action.trigger` 同样走长连接。
- 详细步骤见 [feishu-长连接.md](feishu-长连接.md)。

## 微信（OpenClaw）

OpenClaw Gateway 通过腾讯官方插件 `@tencent-weixin/openclaw-weixin`（渠道 id `openclaw-weixin`）登录个人微信。

- 入站：本仓库的插件 `personal-agent-bridge` 注册 `before_dispatch` 钩子，认领 `openclaw-weixin` 的消息，`POST /webhooks/openclaw/wechat`（Bearer `OPENCLAW_WEBHOOK_TOKEN`）。返回的 `reply` 文本由 OpenClaw 原样发出，不经过 OpenClaw 自己的 LLM。插件出任何错都仍然认领消息，防止消息落回 OpenClaw 默认 agent。
- 选项：微信没有卡片，渲染成「回复数字选择：1. 确认 2. 拒绝」，回 `1` / `2` 或回选项文字都能识别。
- 慢回复：超过 `OPENCLAW_SYNC_WAIT_SECONDS`（默认 12 秒）时先回「收到，正在处理」，结果稍后通过 `OPENCLAW_GATEWAY_URL/tools/invoke`（`message` 工具 `send`，Bearer `OPENCLAW_GATEWAY_TOKEN`）推送。绑定成功的通知也走这条路。没配 Gateway 推送时，同步一直等到出结果。
- 安装与 OpenClaw 配置见 [插件 README](../integrations/openclaw-personal-agent-bridge/README.md)。

## 扩展新渠道（QQ 等）

1. 继承 `ChannelAdapter`，实现 `enabled` / `config_status` / `render` / `send`；参考 `app/channels/qq.py`。
2. 把平台事件解析成 `ChannelInbound` 交给 `gateway.handle_inbound(parsed, "<name>")`，再用 `gateway.deliver_result` 发出。
3. `register_adapter(MyAdapter())`，或者加进 `registry._load_builtin()`。
4. 在 `tests/conftest.py` 里参照 `MockAdapter` 加测试。

绑定、隐私、选项、thread 都在 gateway 里，新渠道不需要重写。

## 上线配置清单

公共：

| 变量 | 说明 |
| --- | --- |
| `PUBLIC_BASE_URL` | 本服务的 HTTPS 地址，绑定 / 安全通道链接用它拼 |
| `AUTH_SECRET` | 网页会话签名密钥；绑定页靠它识别当前浏览器的用户 |
| `CHANNEL_ADMIN_TOKEN` | 运维绑定接口的口令 |
| `CHANNEL_BIND_TTL_SECONDS` / `CHANNEL_SANDBOX_TTL_SECONDS` | 链接有效期，默认 900 / 1800 秒 |

飞书：`FEISHU_ENABLED=1`、`FEISHU_APP_ID`、`FEISHU_APP_SECRET`。控制台里订阅方式选长连接，事件订阅 `im.message.receive_v1`，回调订阅 `card.action.trigger`，开通机器人能力和消息权限，然后发布版本。

微信：`OPENCLAW_WECHAT_ENABLED=1`、`OPENCLAW_WEBHOOK_TOKEN`、`OPENCLAW_GATEWAY_URL`、`OPENCLAW_GATEWAY_TOKEN`（可选 `OPENCLAW_WECHAT_CHANNEL`、`OPENCLAW_SYNC_WAIT_SECONDS`），再按插件 README 配好 OpenClaw。

检查：`GET /api/channels` 能看到每个渠道的 `enabled` 和缺哪些配置。
