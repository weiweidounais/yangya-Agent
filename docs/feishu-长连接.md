# 飞书长连接（WebSocket）接入

本服务用 **长连接** 收飞书入站消息与卡片回调，**不需要**公网 `/webhooks/feishu` URL。  
HTTP webhook 仍保留作备用；生产推荐长连接。

## 代码

| 组件 | 路径 |
|------|------|
| Worker | `app/channels/feishu_ws.py` |
| 独立进程 | `python scripts/feishu_ws.py` |
| 随 API 启动 | `uvicorn` startup → `feishu_ws.start_background()` |
| 开关 | `FEISHU_ENABLED=1`（**默认关**；旧的 `FEISHU_WS_ENABLED=1` 等价）+ App ID/Secret |

入站走 `parse_feishu_webhook(..., skip_auth=True)` → `gateway.handle_inbound`（绑定 / 隐私 / agent）→ `FeishuAdapter.render` 出卡片 → `feishu_client` 出站。

SDK：`lark-oapi` 的 `lark.ws.Client` + `EventDispatcherHandler`。  
已对 SDK 丢弃 `CARD` 帧的问题做了 monkey-patch（否则 `card.action.trigger` 点不动）。

## 开放平台（一次性订阅，可选）

应用：当前 `.env` 里 `FEISHU_APP_ID` 对应的企业自建应用。

1. 在开放平台后台打开该应用的「事件与回调」页  
2. **订阅方式**选：**使用长连接接收事件**（回调页同样选长连接）  
3. **先启动本机长连接客户端**（`uvicorn` 或 `scripts/feishu_ws.py`），再点保存（官方要求连接在线才能保存成功）  
4. 添加事件：**接收消息** `im.message.receive_v1`  
5. 添加回调：**卡片回传交互** `card.action.trigger`  
6. 发布/生效应用版本

### 建议权限 / 能力

- 应用能力：启用 **机器人**
- 权限（按控制台提示勾选，常见）：
  - `im:message` / 获取与发送单聊、群组消息
  - `im:message.p2p_msg:readonly`（或等价：接收机器人单聊消息）
  - `im:message.group_at_msg:readonly`（群内 @ 机器人时）
  - 以「发送消息」相关权限保证出站文本/卡片
- 可用范围：把测试用户加入可用成员

### 不需要

- 公网 Request URL / 内网穿透  
- 为长连接单独配置 Encrypt Key（SDK builder 两个参数必须是空字符串）

## 启动

```bash
cd personal-agent
source .venv/bin/activate
# .env 已填 FEISHU_ENABLED=1 / FEISHU_APP_ID / FEISHU_APP_SECRET / PUBLIC_BASE_URL

# 方式 A：API + 长连接同进程
uvicorn app.main:app --host 0.0.0.0 --port 8787

# 方式 B：只跑长连接
python scripts/feishu_ws.py
```

健康检查：`GET /health` → `feishu_ws.running` / `connected`。

成功连上时日志会出现类似 `connected to wss://...`。

## 联调步骤

1. 控制台切到长连接并保存成功  
2. 飞书里给机器人发一句话 → 未绑定时回「先绑定账号」卡片，带一次性绑定链接  
3. 用浏览器打开链接，点「确认绑定」 → 飞书里收到「绑定成功」  
4. 私聊说「帮我写一封请假邮件」→ 草稿确认卡；点确认/拒绝 → toast「处理中」+ 后续结果卡片  
5. 发手机号 / 身份证 → 不处理，回网页安全通道链接  

群聊不处理（只提示去私聊），也不发绑定链接。

## 限制（飞书官方）

- 仅企业自建应用  
- 回调处理需在约 3 秒内不抛异常（本服务立刻 ACK，业务进线程池）  
- 同一应用最多约 50 条连接；多实例时消息按集群随机分发到一台

## 排障

- `1000040345 app_id or app_secret is invalid`：先确认 `GET /health` 里 `feishu.configured` 且 tenant token 可用；若 token 正常而 WS 仍报此错，多为**瞬时限流**或控制台尚未切到「长连接」——本服务会自动重试。
- 日志：`data/feishu_ws.log`（子进程模式）或 uvicorn 输出。
- 同应用最多约 50 条连接；多开会抢事件。

## 实现说明

- `scripts/feishu_ws.py` 用**监督进程**每次拉起全新 Python worker；避免 `lark_oapi.ws` 在失败后复用已关闭的 asyncio loop（否则易连续报 `1000040345`）。
- `uvicorn` 启动时默认 `FEISHU_WS_MODE=subprocess` 调用该脚本；不设 `FEISHU_ENABLED` 时不会拉起；`scripts/feishu_ws.py` 在开关关闭时直接退出（exit 2）。
- 入站：`im.message.receive_v1` + `card.action.trigger`（含 CARD 帧 monkey-patch）→ `parse_feishu_webhook(skip_auth=True)` → `gateway.handle_inbound`。卡片按钮的 `value` 带 `cmd` / `thread_id`，回调同样经长连接到达。
