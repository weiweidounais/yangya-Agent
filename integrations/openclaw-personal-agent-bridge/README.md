# openclaw-personal-agent-bridge

OpenClaw 插件：把个人微信（`@tencent-weixin/openclaw-weixin` 渠道）的私聊消息转给 personal-agent，
并把服务的回复**原样**发回微信。OpenClaw 自己的 LLM 不参与这些消息。

```
微信 ──> openclaw-weixin 插件 ──> OpenClaw Gateway ──before_dispatch──> 本插件
                                                                         │ POST /webhooks/openclaw/wechat (Bearer)
                                                                         ▼
                                                                personal-agent
                                                                         │ 慢回复 / 绑定成功通知
OpenClaw Gateway <── POST /tools/invoke {tool:"message", action:"send"} ─┘
```

- 入站：`before_dispatch` 钩子认领目标渠道的消息（`{handled: true, text}`），文本即微信回复。
- 任何失败（服务不可达、超时、非 2xx）都仍然认领消息并回复“暂时连不上”，
  避免消息落回 OpenClaw 默认 agent（那里没有绑定 / 隐私防护）。
- 超过 `syncWaitSeconds` 仍未出结果时，服务先回“收到，正在处理”，结果稍后经 Gateway `/tools/invoke` 主动推送。

## 安装

前提：OpenClaw ≥ 2026.5，Gateway 已运行。

```bash
# 1. 微信渠道（腾讯官方插件），扫码登录
openclaw plugins install "@tencent-weixin/openclaw-weixin"
openclaw plugins enable openclaw-weixin
openclaw channels login --channel openclaw-weixin

# 2. 本桥接插件（在仓库根目录执行）
openclaw plugins install --link ./integrations/openclaw-personal-agent-bridge --force
openclaw plugins enable personal-agent-bridge
```

## 配置（`~/.openclaw/openclaw.json`）

```json5
{
  gateway: {
    auth: { mode: "token", token: "<OPENCLAW_GATEWAY_TOKEN>" },
  },
  // 主动推送走 message 工具；如用 coding 等受限 profile，需要放行
  tools: { alsoAllow: ["message"] },
  plugins: {
    entries: {
      "personal-agent-bridge": {
        enabled: true,
        config: {
          agentUrl: "http://127.0.0.1:8787",   // personal-agent 地址
          token: "<OPENCLAW_WEBHOOK_TOKEN>",       // 与服务端一致
          // channels: ["openclaw-weixin"],        // 默认 openclaw-weixin / weixin / wechat
          // syncWaitSeconds: 12,                  // 与服务端 OPENCLAW_SYNC_WAIT_SECONDS 对齐
        },
      },
    },
  },
}
```

也可以不写 `config`，改用环境变量 `PERSONAL_AGENT_URL` / `PERSONAL_AGENT_WEBHOOK_TOKEN`。

服务端对应的环境变量：

| 变量 | 值 |
| --- | --- |
| `OPENCLAW_WECHAT_ENABLED` | `1` |
| `OPENCLAW_WEBHOOK_TOKEN` | 与插件 `token` 相同 |
| `OPENCLAW_GATEWAY_URL` | Gateway HTTP 地址，例如 `http://127.0.0.1:18789` |
| `OPENCLAW_GATEWAY_TOKEN` | 与 `gateway.auth.token` 相同 |
| `OPENCLAW_WECHAT_CHANNEL` | 默认 `openclaw-weixin` |

修改后重启 Gateway：`openclaw gateway restart`。

## 验证

1. 用另一个微信号给登录的微信发“你好” → 收到“先绑定账号”和绑定链接。
2. 打开链接登录 → 微信里收到“绑定成功”（这条是 `/tools/invoke` 推送，验证了出站链路）。
3. 发“帮我写一封请假邮件” → 收到草稿预览和“回复数字选择：1. 确认 2. 拒绝”，回复 `1` 生效。
