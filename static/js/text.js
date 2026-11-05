const KIND_LABELS = {
  profile_updated: "档案更新",
  draft_created: "生成草稿",
  confirm_accepted: "确认草稿",
  confirm_rejected: "拒绝草稿",
  chat: "对话",
  summary: "对话要点",
  proactive: "主动提醒",
  reminder: "提醒",
  schedule: "定时任务",
  secret_saved: "安全保存",
  secret_deleted: "删除保存项",
  privacy_redacted: "隐私拦截",
};

export function kindLabel(kind) {
  return KIND_LABELS[kind] || "记忆";
}

export const DRAFT_KINDS = {
  email: "邮件",
  message: "消息",
  article: "文章",
  note: "笔记",
  other: "草稿",
};

export function draftKindLabel(kind) {
  return DRAFT_KINDS[String(kind || "").toLowerCase()] || DRAFT_KINDS.other;
}

const TECH_MARKERS =
  /(loop_redraft|thread_id|session_id|session=|stub|JWT|Traceback|Exception|[A-Z][a-z]+Error\b|\b[a-z]+_[a-z_]+\b|=)/;

export function looksTechnical(text) {
  return TECH_MARKERS.test(String(text || ""));
}

/** Strip backend implementation details from assistant copy; returns "" when nothing user-facing is left. */
export function cleanAssistantText(raw) {
  let s = String(raw || "").trim();
  if (!s) return "";
  s = s
    .replace(/（loop_redraft=\d+）/g, "")
    .replace(/（图已结束[^）]*）/g, "")
    .replace(/（[^（）]*(?:Error|Exception)[^（）]*）/g, "")
    .replace(/^stub[:：]\s*/i, "")
    .trim();
  if (!s || looksTechnical(s)) return "";
  return s;
}

export function stripTimeZone(headline) {
  return String(headline || "").replace(/\s+[A-Za-z]+\/[A-Za-z_]+$/, "");
}

const TURN_SUMMARY_RE = /^\[([a-z_]+)\]\s*用户：([\s\S]*?)(?:；助手：([\s\S]*))?$/;

/** Offline turn summaries read "[kind] 用户：…；助手：…"; keep the half that means something to the user. */
export function humanSummary(raw) {
  const s = String(raw || "").trim();
  const m = TURN_SUMMARY_RE.exec(s);
  if (!m) return s.replace(/^\[[a-z_]+\]\s*/i, "");
  const user = m[2].trim() === "-" ? "" : m[2].trim();
  const bot = cleanAssistantText((m[3] || "").replace(/^-$/, ""));
  if (m[1] === "confirm") return user.replace(/^用户/, "你");
  if (!user) return bot;
  return `你说：「${user.length > 60 ? `${user.slice(0, 60)}…` : user}」`;
}

/** One line of a reminder body: translate "（kind）" markers and drop technical leftovers. */
export function cleanReminderLine(line) {
  let s = String(line || "").replace(/（([a-z_]+)）/g, (m, kind) =>
    KIND_LABELS[kind] ? `（${KIND_LABELS[kind]}）` : ""
  );
  const recent = /^(近期对话要点[：:])\s*([\s\S]*)$/.exec(s);
  if (recent) {
    const body = humanSummary(recent[2]);
    s = body ? `${recent[1]}${body}` : "";
  }
  return looksTechnical(s) ? "" : s;
}

export function memoryText(ev) {
  const kind = ev && ev.kind;
  const summary = String((ev && ev.summary) || "").trim();
  const payload = (ev && ev.payload) || {};
  if (kind === "confirm_accepted") return "你确认了一份草稿。";
  if (kind === "confirm_rejected") {
    return payload.will_redraft ? "你拒绝了一份草稿，我自动重写了一版。" : "你拒绝了一份草稿。";
  }
  if (kind === "summary") {
    const s = humanSummary(summary);
    return !s || looksTechnical(s) ? "记下了这轮对话的要点。" : s;
  }
  const cleaned = cleanAssistantText(summary);
  return cleaned || `${kindLabel(kind)}`;
}

export function splitList(value) {
  return String(value || "")
    .split(/[,，、;；\n]/)
    .map((s) => s.trim())
    .filter(Boolean);
}
