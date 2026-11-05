import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

type BridgeConfig = {
  agentUrl?: string;
  token?: string;
  channels?: string[];
  syncWaitSeconds?: number;
};

const DEFAULT_CHANNELS = ["openclaw-weixin", "weixin", "wechat"];
const UNAVAILABLE = "个人助理暂时连不上，请稍后再试。";

export default definePluginEntry({
  id: "personal-agent-bridge",
  name: "Personal Agent Bridge",
  description: "Hand WeChat messages to the Personal Agent service and send its reply verbatim.",
  register(api) {
    const cfg = (api.pluginConfig ?? {}) as BridgeConfig;
    const baseUrl = (cfg.agentUrl || process.env.PERSONAL_AGENT_URL || "").replace(/\/+$/, "");
    const token = cfg.token || process.env.PERSONAL_AGENT_WEBHOOK_TOKEN || "";
    const channels = new Set(cfg.channels?.length ? cfg.channels : DEFAULT_CHANNELS);
    const syncWaitSeconds = cfg.syncWaitSeconds ?? 12;
    // Without a push path the service answers synchronously, so leave room for a full agent turn.
    const requestTimeoutMs = (syncWaitSeconds + 170) * 1000;

    if (!baseUrl || !token) {
      api.logger.warn("personal-agent-bridge: agentUrl / token not configured; bridge inactive");
      return;
    }

    api.on(
      "before_dispatch",
      async (event, ctx) => {
        const channel = event.channel || ctx.channelId || "";
        if (!channels.has(channel)) return undefined;

        // A thrown error would fall through to OpenClaw's own agent, which has no
        // privacy / binding guard, so every failure still claims the message.
        try {
          const res = await fetch(`${baseUrl}/webhooks/openclaw/wechat`, {
            method: "POST",
            headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
            body: JSON.stringify({
              channel,
              from: event.senderId || ctx.senderId || "",
              text: event.content || event.body || "",
              msg_id: event.messageId || ctx.messageId || "",
              conversation_id: ctx.conversationId || "",
              account_id: ctx.accountId || "",
              is_group: Boolean(event.isGroup),
              sync_wait_seconds: syncWaitSeconds,
            }),
            signal: AbortSignal.timeout(requestTimeoutMs),
          });
          if (!res.ok) {
            api.logger.warn(`personal-agent-bridge: service returned HTTP ${res.status}`);
            return { handled: true, text: UNAVAILABLE };
          }
          const data = (await res.json()) as { reply?: { text?: string } };
          const text = data.reply?.text || "";
          return text ? { handled: true, text } : { handled: true };
        } catch (err) {
          api.logger.warn(`personal-agent-bridge: ${String(err)}`);
          return { handled: true, text: UNAVAILABLE };
        }
      },
      { timeoutMs: requestTimeoutMs + 5000 },
    );
  },
});
