import { currentUserId, ensureSession, sessionExpired } from "./identity.js?v=__APP_VERSION__";
import { setViewTz } from "./core.js?v=__APP_VERSION__";

const LIGHT_TIMEOUT = 20000;
const HEAVY_TIMEOUT = 150000;
const TURN_TIMEOUT = 600000;
const TURN_POLL_INTERVAL = 2000;

export class ApiError extends Error {
  constructor({ kind = "http", status = 0, code = "", message = "", detail = null } = {}) {
    super(message || `${kind} ${status}`);
    this.kind = kind;
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

function learnTz(data) {
  const d = data && data.data;
  const tz = (data && data.tz) || (d && (d.tz || (d.proposal && d.proposal.tz) || (d.schedule_proposal && d.schedule_proposal.tz)));
  if (typeof tz === "string") setViewTz(tz);
}

async function request(path, opts = {}) {
  const open = /^\/(api\/)?health/.test(path);
  if (!open) await ensureSession();
  try {
    return await rawRequest(path, opts);
  } catch (err) {
    if (!open && err.status === 401) throw sessionExpired();
    throw err;
  }
}

async function rawRequest(path, { method = "GET", json, timeout = LIGHT_TIMEOUT } = {}) {
  const ctrl = new AbortController();
  const timer = window.setTimeout(() => ctrl.abort(), timeout);
  let res;
  let raw = "";
  try {
    res = await fetch(path, {
      method,
      headers: json !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: json !== undefined ? JSON.stringify(json) : undefined,
      signal: ctrl.signal,
      credentials: "same-origin",
    });
    raw = await res.text();
  } catch (err) {
    throw new ApiError({
      kind: err && err.name === "AbortError" ? "timeout" : "network",
      message: String((err && err.message) || err),
    });
  } finally {
    window.clearTimeout(timer);
  }
  let data = null;
  if (raw) {
    try {
      data = JSON.parse(raw);
    } catch {
      data = null;
    }
  }
  if (!res.ok) {
    const detail = data && data.detail !== undefined ? data.detail : data || raw.slice(0, 400);
    const obj = detail && typeof detail === "object" && !Array.isArray(detail) ? detail : null;
    throw new ApiError({
      status: res.status,
      code: (obj && obj.code) || "",
      message: typeof detail === "string" ? detail : (obj && obj.message) || "",
      detail,
    });
  }
  if (data == null) throw new ApiError({ kind: "parse", status: res.status, detail: raw.slice(0, 400) });
  const traceId = res.headers.get("X-Trace-Id") || "";
  if (traceId && typeof data === "object" && !Array.isArray(data)) {
    Object.defineProperty(data, "traceId", { value: traceId, enumerable: false });
  }
  learnTz(data);
  return data;
}

const enc = encodeURIComponent;

// Thread identity survives reloads and is always scoped to the request owner.
const chatThreads = new Map();
function chatThread(owner) {
  if (chatThreads.has(owner)) return chatThreads.get(owner);
  try { return localStorage.getItem(`ja.chat.thread.v1:${owner}`) || ""; }
  catch { return ""; }
}

function rememberThread(owner, threadId) {
  if (!threadId) return;
  chatThreads.set(owner, threadId);
  try { localStorage.setItem(`ja.chat.thread.v1:${owner}`, threadId); } catch { /* tab only */ }
}

function completedTurn(response) {
  if (!response || !response.status) throw new ApiError({ kind: "parse" });
  if (response.status === "error" || response.ok === false) {
    throw new ApiError({ status: 500, code: "turn_failed" });
  }
  return response;
}

function recoverable(err) {
  return err.kind !== "session" &&
    (["timeout", "network", "parse"].includes(err.kind) || err.status >= 500);
}

async function turnRequest(path, json) {
  const requestId = Array.from(crypto.getRandomValues(new Uint8Array(16)),
    byte => byte.toString(16).padStart(2, "0")).join("");
  const deadline = Date.now() + TURN_TIMEOUT;
  let stopped = false;
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = window.setTimeout(() => reject(new ApiError({ kind: "timeout" })), TURN_TIMEOUT);
  });
  const deliver = async () => {
    let response;
    try {
      response = await request(path, {
        method: "POST", json: { ...json, request_id: requestId }, timeout: HEAVY_TIMEOUT,
      });
    } catch (err) {
      if (!recoverable(err)) throw err;
      // The synchronous server worker may still be running. Never POST again:
      // poll the saved response for this exact turn, including after proxy errors.
      while (!stopped && Date.now() < deadline) {
        let turn;
        try {
          turn = await request(`/api/turns/${enc(requestId)}`, {
            timeout: Math.min(LIGHT_TIMEOUT, deadline - Date.now()),
          });
        } catch (pollError) {
          if (!recoverable(pollError) && pollError.status !== 404) throw pollError;
        }
        if (turn && turn.status === "completed") return completedTurn(turn.response);
        if (turn && turn.status === "failed") {
          throw new ApiError({
            kind: turn.code === "turn_timeout" ? "timeout" : "http",
            status: 500, code: turn.code,
          });
        }
        if (!stopped && Date.now() < deadline) {
          await new Promise(resolve => window.setTimeout(resolve,
            Math.min(TURN_POLL_INTERVAL, deadline - Date.now())));
        }
      }
      throw new ApiError({ kind: "timeout" });
    }
    return completedTurn(response);
  };
  try {
    return await Promise.race([deliver(), timeout]);
  } finally {
    stopped = true;
    window.clearTimeout(timer);
  }
}

async function uploadFile(path, file) {
  await ensureSession();
  const ctrl = new AbortController();
  const timer = window.setTimeout(() => ctrl.abort(), 40000);
  let res;
  let raw = "";
  const form = new FormData();
  form.append("file", file, file.name || "food.jpg");
  try {
    res = await fetch(path, { method: "POST", body: form, credentials: "same-origin", signal: ctrl.signal });
    raw = await res.text();
  } catch (err) {
    throw new ApiError({
      kind: err && err.name === "AbortError" ? "timeout" : "network",
      message: String((err && err.message) || err),
    });
  } finally {
    window.clearTimeout(timer);
  }
  let data = null;
  try {
    data = raw ? JSON.parse(raw) : null;
  } catch {
    data = null;
  }
  if (!res.ok) {
    const detail = data && data.detail !== undefined ? data.detail : data || raw.slice(0, 400);
    const obj = detail && typeof detail === "object" && !Array.isArray(detail) ? detail : null;
    throw new ApiError({
      status: res.status,
      code: (obj && obj.code) || "",
      message: typeof detail === "string" ? detail : (obj && obj.message) || "",
      detail,
    });
  }
  if (data == null) throw new ApiError({ kind: "parse", status: res.status, detail: raw.slice(0, 400) });
  return data;
}

export const api = {
  schedules: () => request("/api/schedules"),
  schedule: (id) => request(`/api/schedules/${enc(id)}`),
  scheduleRuns: (id) => request(`/api/schedules/${enc(id)}/runs?limit=30`),
  updateSchedule: (id, patch) => request(`/api/schedules/${enc(id)}`, { method: "PATCH", json: patch }),
  scheduleAction: (id, action) => request(`/api/schedules/${enc(id)}/${action}`, { method: "POST" }),
  deleteSchedule: (id) => request(`/api/schedules/${enc(id)}`, { method: "DELETE" }),
  decideSchedule: (id, confirmed) => request(`/api/schedules/proposals/${enc(id)}/decide`, { method: "POST", json: { confirmed } }),
  async health() {
    try {
      return await request("/api/health");
    } catch (err) {
      if (err.status === 404) return request("/health");
      throw err;
    }
  },
  capabilities() {
    return request("/api/capabilities");
  },
  async chat(message, { replyTo, threadId } = {}) {
    const owner = currentUserId();
    const res = await turnRequest("/api/chat", {
      user_id: owner,
      message: message || "",
      reply_to: replyTo || null,
      thread_id: threadId || chatThread(owner) || null,
    });
    rememberThread(owner, res.thread_id);
    return res;
  },
  /** Explicit writing request: always a fresh draft thread. */
  draft(requestText) {
    return turnRequest("/api/draft", { user_id: currentUserId(), request: requestText });
  },
  secrets() {
    return request(`/api/secrets?user_id=${enc(currentUserId())}`);
  },
  /** Values go only to the vault endpoint; callers must not keep or log them. */
  saveSecret(key, values, purpose = "") {
    return request("/api/secrets", {
      method: "POST",
      json: { user_id: currentUserId(), key, purpose, values },
    });
  },
  deleteSecret(key) {
    return request(`/api/secrets/${enc(key)}?user_id=${enc(currentUserId())}`, { method: "DELETE" });
  },
  async confirm(threadId, confirmed, { edits = null, reason = "" } = {}) {
    // One-time, owner-bound token: a replayed confirm is refused by the server.
    const tok = await request(`/api/confirm/${enc(threadId)}/token`, { method: "POST" });
    const body = {
      user_id: currentUserId(),
      thread_id: threadId,
      confirmed,
      channel: "web",
      confirm_token: (tok.data && tok.data.confirm_token) || "",
    };
    if (confirmed && edits) body.edits = edits;
    if (!confirmed && reason) body.reason = reason;
    return request("/api/confirm", { method: "POST", json: body, timeout: HEAVY_TIMEOUT });
  },
  decision(threadId) {
    return request(`/api/confirm/${enc(threadId)}`);
  },
  continueTask(threadId) {
    return request("/api/continue", { method: "POST", json: { thread_id: threadId }, timeout: HEAVY_TIMEOUT });
  },
  profile() {
    return request(`/api/profile/${enc(currentUserId())}`);
  },
  companion() {
    return request("/api/companion");
  },
  companionSave(patch) {
    return request("/api/companion/profile", {
      method: "POST",
      json: { user_id: currentUserId(), ...patch },
      timeout: HEAVY_TIMEOUT,
    });
  },
  companionMeal(meal) {
    return request("/api/companion/meal", {
      method: "POST",
      json: { user_id: currentUserId(), ...meal },
    });
  },
  companionRevive(name = "") {
    return request("/api/companion/revive", {
      method: "POST",
      json: { user_id: currentUserId(), name },
    });
  },
  companionRestTimer({ seconds = 90, exercise = "", setIndex = 0, sets = 0 } = {}) {
    return request("/api/companion/rest-timer", {
      method: "POST",
      json: { user_id: currentUserId(), seconds, exercise, set_index: setIndex, sets },
    });
  },
  companionReport(days = 7) {
    return request(`/api/companion/report?days=${days}`);
  },
  companionSendReport(days = 7) {
    return request("/api/companion/report", {
      method: "POST",
      json: { user_id: currentUserId(), days },
      timeout: HEAVY_TIMEOUT,
    });
  },
  companionFoodAnalyze(file) {
    return uploadFile("/api/companion/food/analyze", file);
  },
  saveProfile(patch) {
    return request("/api/profile", {
      method: "POST",
      json: { user_id: currentUserId(), profile: patch },
      timeout: HEAVY_TIMEOUT,
    });
  },
  memory({ q = "", limit = 15 } = {}) {
    return request(`/api/memory/${enc(currentUserId())}?limit=${limit}&q=${enc(q)}`);
  },
  memoryCard(threadId) {
    const q = threadId ? `?thread_id=${enc(threadId)}` : "";
    return request(`/api/memory/${enc(currentUserId())}/card${q}`);
  },
  memoryUpdateFact(factId, value, threadId) {
    return request(`/api/memory/${enc(currentUserId())}/facts/${enc(factId)}`, {
      method: "PATCH",
      json: { value, thread_id: threadId || null },
    });
  },
  memoryDeleteFact(factId, threadId) {
    const q = threadId ? `?thread_id=${enc(threadId)}` : "";
    return request(`/api/memory/${enc(currentUserId())}/facts/${enc(factId)}${q}`, { method: "DELETE" });
  },
  memoryUpdateEvent(eventId, value, threadId) {
    return request(`/api/memory/${enc(currentUserId())}/events/${enc(eventId)}`, {
      method: "PATCH",
      json: { value, thread_id: threadId || null },
    });
  },
  memoryDeleteEvent(eventId, threadId) {
    const q = threadId ? `?thread_id=${enc(threadId)}` : "";
    return request(`/api/memory/${enc(currentUserId())}/events/${enc(eventId)}${q}`, { method: "DELETE" });
  },
  memoryForget(threadId) {
    return request(`/api/memory/${enc(currentUserId())}/forget`, {
      method: "POST",
      json: { confirm: true, thread_id: threadId || null },
    });
  },
  proactivePending() {
    return request(`/api/proactive/pending?user_id=${enc(currentUserId())}&limit=5`);
  },
  dismissProactive(id) {
    return request(`/api/proactive/items/${enc(id)}/dismiss`, { method: "POST" });
  },
  feedback({ traceId, threadId = "", value, comment = "" }) {
    return request("/api/feedback", {
      method: "POST",
      json: { user_id: currentUserId(), trace_id: traceId, thread_id: threadId || "", value, comment },
    });
  },
};

const TECHNICAL = /(Error|Exception|Traceback|stub|JWT|_[a-z]|[a-z]_[a-z]|=|https?:)/;

function plainServerMessage(message) {
  const m = String(message || "").trim();
  if (!m || TECHNICAL.test(m) || !/[一-鿿]/.test(m)) return "";
  return m.replace(/[。.]$/, "");
}

/** Plain-Chinese, next-step copy for any failure. Raw status/detail only via debugText(). */
export function friendlyError(err, ctx = "") {
  if (!(err instanceof ApiError)) {
    return { text: "出了点小问题，这一步没完成。稍后再试一次。", retry: true };
  }
  if (err.kind === "network") {
    return { text: "网络好像断开了，这一步没完成。检查网络后点「重试」。", retry: true };
  }
  if (err.kind === "timeout") {
    return { text: "这次处理太久了，我先停了下来。点「重试」再来一次。", retry: true };
  }
  if (err.kind === "parse") {
    return { text: "服务这次的回复不完整。稍后点「重试」。", retry: true };
  }
  const { status } = err;
  const onCard = ctx === "confirm" || ctx === "continue";
  if (err.kind === "session") {
    return { text: err.code === "unavailable" ? "暂时无法确认你的身份。点页面顶部的「稍后再试」。" : "登录已过期，之前的记录在原账号下。点页面顶部的「重新开始」继续。", retry: false };
  }
  if (status === 401) {
    return { text: "登录状态失效了。刷新页面重新进入，再回到这里继续。", retry: false };
  }
  if (status === 403) {
    return onCard
      ? { text: "这张卡片不属于当前账号，所以没法操作。", retry: false }
      : { text: "当前账号没有权限做这一步。确认账号无误后再试。", retry: false };
  }
  if (status === 404) {
    if (onCard) {
      return { text: "找不到这份草稿对应的会话了（服务可能刚重启过）。把需求再跟我说一次，我重新写一版。", retry: false };
    }
    return { text: "这个功能暂时用不了，稍后再试。", retry: true };
  }
  if (status === 409 && plainServerMessage(err.message)) {
    return { text: `${plainServerMessage(err.message)}。`, retry: false };
  }
  if (status === 400 || status === 422) {
    const m = plainServerMessage(err.message);
    return { text: m ? `${m}。` : "这次的内容我没法处理。换个说法，或者刷新页面后再试。", retry: false };
  }
  if (status === 429) {
    return { text: "操作有点频繁，稍等几秒再试。", retry: true };
  }
  if (status >= 500) {
    return { text: "服务开了个小差，这一步没完成。稍后点「重试」，或者换个说法再发。", retry: true };
  }
  return { text: "这一步没完成，稍后再试。", retry: true };
}

export function debugText(err) {
  if (!(err instanceof ApiError)) return String((err && err.stack) || err);
  let detail = "";
  try {
    detail = typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail);
  } catch {
    detail = String(err.detail);
  }
  return `${err.kind} ${err.status || ""} ${err.code || ""}\n${detail || err.message}`.trim();
}
