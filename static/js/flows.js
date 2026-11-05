import { currentUserId } from "./identity.js?v=__APP_VERSION__";
import { scheduleConfirmCard } from "./schedules.js?v=__APP_VERSION__";
import { showRestCard } from "./restcard.js?v=__APP_VERSION__";
import { DEBUG, nextId, announce, toast, parseTime, debugBlock } from "./core.js?v=__APP_VERSION__";
import { api, friendlyError, debugText } from "./api.js?v=__APP_VERSION__";
import { store } from "./store.js?v=__APP_VERSION__";
import {
  append,
  reveal,
  clearLog,
  scrollToBottom,
  programmaticScroll,
  userMessage,
  userRow,
  assistantMessage,
  systemLine,
  errorMessage,
  typing,
  renderHero,
} from "./thread.js?v=__APP_VERSION__";
import {
  proactiveCard,
  continueCard,
  askCard,
  secretCard,
  memoryCard,
} from "./cards.js?v=__APP_VERSION__";
import { openSecretSandbox } from "./secret.js?v=__APP_VERSION__";
import { confirmCard, draftFromResponse } from "./confirm.js?v=__APP_VERSION__";
import { focusComposer } from "./composer.js?v=__APP_VERSION__";
import { getProfile, setProfile, openDrawer, refreshSecrets, loadProfile } from "./drawer.js?v=__APP_VERSION__";
import { cleanAssistantText } from "./text.js?v=__APP_VERSION__";

const SLOW_HINT = "还在处理…写草稿通常需要 10–30 秒";
const MAX_STORED_TEXT = 1500;

const nodes = new Map();
let displayRevision = 0;

/* ---------------- Transcript entries ---------------- */

const CARDS = {
  confirm: confirmCard,
  schedule_confirm: scheduleConfirmCard,
  proactive: proactiveCard,
  continue: continueCard,
  ask: askCard,
  secret: secretCard,
  memory: memoryCard,
};

function contextFor(entry, restored) {
  if (entry.type === "schedule_confirm") {
    return { restored, save: (e) => store.update(e.id, {}) };
  }
  if (entry.type === "confirm") {
    return {
      restored,
      save: (e) => {
        const { draft, edits, decision, editedAfterDecision, supersededBy } = e.data;
        store.update(e.id, { draft, edits, decision, editedAfterDecision, supersededBy });
      },
      beginDecision: () => ({ owner: currentUserId(), revision: displayRevision }),
      decisionIsCurrent: (request) => request.owner === currentUserId() && request.revision === displayRevision,
      onRedraft,
      onDecided,
      revealEntry: (id) => revealEntry(id, { user: true }),
    };
  }
  return {
    restored,
    focusComposer,
    openDrawer: (field) => openDrawer({ focusField: field }),
    dismissProactive,
    continueTask,
    stopTask,
    answerAsk,
    openSecret,
    declineSecret,
    openSecrets: () => openDrawer({ section: "secrets" }),
    saveMemoryFact,
    deleteMemoryFact,
    saveMemoryEvent,
    deleteMemoryEvent,
    forgetMemory,
    armForget,
  };
}

function buildCard(entry, restored) {
  const build = CARDS[entry.type];
  return build ? build(entry, contextFor(entry, restored)) : null;
}

function renderEntry(entry, { restored = false, live = null } = {}) {
  const d = entry.data || {};
  let node = null;
  if (entry.type === "user") node = userMessage(live || d, { animate: !restored, scroll: !restored });
  else if (entry.type === "assistant") {
    node = assistantMessage(d.text || "", {
      animate: !restored,
      speak: !restored,
      scroll: !restored,
      feedback: d.feedback || null,
      onFeedback: d.traceId ? (value) => sendFeedback(entry, value) : null,
    });
  }
  else if (entry.type === "system") node = systemLine(d.text, { scroll: !restored });
  else {
    const card = buildCard(entry, restored);
    if (card) node = append(card, { scroll: !restored });
  }
  if (node) nodes.set(entry.id, node);
  return node;
}

function rerender(id) {
  const entry = store.get(id);
  const old = nodes.get(id);
  if (!entry || !old || !old.isConnected) return null;
  const scroller = document.getElementById("logScroll");
  const scrollTop = scroller.scrollTop;
  const oldHeight = old.offsetHeight;
  const aboveViewport = old.getBoundingClientRect().bottom <= scroller.getBoundingClientRect().top;
  const node = entry.type === "assistant" ? renderEntry(entry, { restored: true }) : buildCard(entry, true);
  if (!node) return null;
  old.replaceWith(node);
  programmaticScroll(() => {
    scroller.scrollTop = scrollTop + (aboveViewport ? node.offsetHeight - oldHeight : 0);
  });
  nodes.set(id, node);
  return node;
}

function emit(type, data, { live = null } = {}) {
  const entry = store.add({ id: nextId("e"), type, at: new Date().toISOString(), data });
  renderEntry(entry, { live });
  return entry;
}

const say = (text, { traceId = "", threadId = "" } = {}) =>
  emit("assistant", { text, threadId, ...(traceId ? { traceId, feedback: null } : {}) });

async function sendFeedback(entry, value) {
  const d = entry.data || {};
  await api.feedback({ traceId: d.traceId, threadId: d.threadId, value });
  store.update(entry.id, { feedback: value });
}
const note = (text) => emit("system", { text });

function latest(type, predicate = () => true) {
  const list = store.find((e) => e.type === type && predicate(e));
  return list.length ? list[list.length - 1] : null;
}

function revealEntry(id, { user = false } = {}) {
  const node = nodes.get(id);
  if (!node || !node.isConnected) return;
  reveal(node, { user, focus: node.querySelector(".cc-actions button, .card-actions a, .card-actions button") });
}

function revealDraft(threadId) {
  const conf = latest("confirm", (e) => e.data.threadId === threadId && !e.data.supersededBy);
  if (conf) revealEntry(conf.id, { user: true });
}

let lastEcho = null;

function echoUser(text) {
  const value = String(text || "");
  const stored = value.length > MAX_STORED_TEXT ? value.slice(0, MAX_STORED_TEXT) : value;
  lastEcho = emit("user", { text: stored, omitted: value.length - stored.length }, { live: { text: value } });
  return lastEcho;
}

/** The server hid credentials / PII in what the user typed: replace the stored and visible copy too. */
function applyRedaction(redacted) {
  const entry = lastEcho && store.get(lastEcho.id);
  if (!entry || typeof redacted !== "string") return;
  const text = redacted.slice(0, MAX_STORED_TEXT);
  store.update(entry.id, { text, omitted: 0, redacted: true });
  const old = nodes.get(entry.id);
  if (old && old.isConnected) {
    const node = userRow(entry.data, { animate: false });
    old.replaceWith(node);
    nodes.set(entry.id, node);
  }
}

async function withTyping(task, { hint = "", slowHint = "" } = {}) {
  const indicator = typing({ hint, slowHint });
  try {
    return await task();
  } finally {
    indicator.done();
  }
}

function showError(err, ctx, retry) {
  const f = friendlyError(err, ctx);
  errorMessage({ text: f.text, onRetry: f.retry && retry ? retry : null, debug: debugText(err) });
}

function debugResponse(label, res) {
  if (!DEBUG) return;
  const node = debugBlock(label, res);
  if (node) append(node);
}

const timeOf = (value) => {
  const t = parseTime(value);
  return t ? t.getTime() : 0;
};

/* ---------------- Composer pipeline ---------------- */

/** Everything typed in the composer goes to the agent as one chat turn. */
export async function sendComposer({ text }) {
  const value = String(text || "").trim();
  if (!value) return;
  echoUser(value);
  await sendText(value);
}

async function sendText(text, { replyTo = null } = {}) {
  const owner = currentUserId();
  const revision = displayRevision;
  let res;
  try {
    res = await withTyping(() => api.chat(text, { replyTo }), { slowHint: SLOW_HINT });
  } catch (err) {
    if (owner !== currentUserId()) return false;
    showError(err, "chat", () => sendText(text, { replyTo }));
    return false;
  }
  if (owner !== currentUserId()) return false;
  debugResponse(`/api/chat → ${res.status}`, res);
  handleChat(res, owner, revision);
  return true;
}

/** Explicit writing request (hero chip / future shortcuts): POST /api/draft. */
export async function requestDraft(requestText) {
  const owner = currentUserId();
  const revision = displayRevision;
  let res;
  try {
    res = await withTyping(() => api.draft(requestText), { hint: "正在写草稿…", slowHint: SLOW_HINT });
  } catch (err) {
    if (owner !== currentUserId()) return false;
    showError(err, "draft", () => requestDraft(requestText));
    return false;
  }
  if (owner !== currentUserId()) return false;
  debugResponse(`/api/draft → ${res.status}`, res);
  handleChat(res, owner, revision);
  return true;
}

function replyText(messages) {
  return (messages || [])
    .filter((m) => m && m.role !== "user" && m.content)
    .map((m) => String(m.content).replace(/（loop_redraft=\d+）/g, "").trim())
    .filter(Boolean)
    .join("\n\n");
}

function errorReply(data, messages) {
  const reply = replyText(messages);
  if (reply) return reply;
  const cleaned = cleanAssistantText(data.error);
  return cleaned || "这一步没完成，换个说法再试一次。";
}

function handleChat(res, owner, revision) {
  if (typeof document !== "undefined" && typeof document.dispatchEvent === "function") {
    document.dispatchEvent(new CustomEvent("turn-finished"));
  }
  const data = res.data || {};
  const status = res.status || "";
  if (revision !== displayRevision) {
    toast("档案刚更新过，这条回复已失效，请重新发送。");
    return;
  }
  if (data.profile) setProfile(data.profile, owner);
  if (typeof data.redacted_message === "string") applyRedaction(data.redacted_message);
  const meta = { traceId: res.traceId || "", threadId: res.thread_id || "" };

  if (status === "error") {
    say(errorReply(data, data.messages), meta);
    return;
  }
  if (status === "schedules") {
    const reply = replyText(data.messages);
    if (reply) say(reply, meta);
    if (data.schedule_proposal) emit("schedule_confirm", { proposal: data.schedule_proposal });
    return;
  }
  if (status === "confirmed" || status === "rejected") {
    // Typed decision (e.g. 「拒绝，太长了」) on the latest draft card.
    applyTypedDecision(res);
    return;
  }
  if (status === "awaiting_confirm" || status === "drafted" || data.draft) {
    presentDraft(res);
    return;
  }
  if (data.rest_card && typeof data.rest_card === "object") showRestCard(data.rest_card);
  const reply = replyText(data.messages);
  const ask = data.ask && typeof data.ask === "object" ? data.ask : null;
  if (reply || !ask) say(reply || "收到。", meta);
  if (ask) emitAsk(ask, reply);
  if (data.schedule_proposal) emit("schedule_confirm", { proposal: data.schedule_proposal });
  if (data.continue_card) emitContinue(data.continue_card, res.thread_id);
}

/* ---------------- Draft → confirm card ---------------- */

function presentDraft(res) {
  const data = res.data || {};
  const reply = replyText(data.messages);
  if (reply) say(reply, { traceId: res.traceId || "", threadId: res.thread_id || "" });
  const threadId = res.thread_id || "";
  const draft = draftFromResponse(data);
  const confirmable = !!threadId && !!(res.awaiting_confirm || data.needs_confirm);
  if (confirmable) {
    const entry = emitConfirm({ threadId, draft });
    announce("草稿写好了，等你确认。");
    return entry;
  }
  if (draft.body) {
    // A draft that needs no confirmation (e.g. the loop ended at its budget) still gets shown.
    emitConfirm({ threadId, draft, decision: { confirmed: true, at: new Date().toISOString(), channel: "web" } });
  }
  if (data.continue_card) emitContinue(data.continue_card, threadId);
  return null;
}

function emitConfirm({ threadId, draft, version = 1, basisDecisionAt = null, decision = null }) {
  return emit("confirm", {
    threadId,
    draft,
    version,
    basisDecisionAt,
    decision,
    edits: null,
    editedAfterDecision: false,
    supersededBy: null,
  });
}

function applyTypedDecision(res) {
  const data = res.data || {};
  const confirmed = res.status === "confirmed";
  const reply = replyText(data.messages) || (data.side_effect_note && cleanAssistantText(data.side_effect_note)) || "";
  if (reply) say(reply, { traceId: res.traceId || "", threadId: res.thread_id || "" });
  const card = latest("confirm", (e) => e.data.threadId === res.thread_id && !e.data.supersededBy);
  if (!card) {
    if (data.draft && res.awaiting_confirm) presentDraft(res);
    return;
  }
  const dec = data.decision || {};
  const decision = { confirmed, at: dec.decided_at || new Date().toISOString(), overridden: !!dec.overridden, channel: "web", reason: data.reject_reason || "" };
  if (!confirmed && res.awaiting_confirm) {
    store.update(card.id, { decision, supersededBy: onRedraft(card, res, { owner: currentUserId() }) });
    rerender(card.id);
    return;
  }
  const nd = data.draft || {};
  const patch = { decision, editedAfterDecision: false };
  if (nd.body) patch.draft = { ...card.data.draft, ...draftFromResponse(data) };
  store.update(card.id, patch);
  rerender(card.id);
  revealDraft(res.thread_id);
}

/* ---------------- Question cards & secret sandbox ---------------- */

function emitAsk(card, prompt = "") {
  if (card.type === "secret") {
    if (!card.key) return;
    emit("secret", { card, saved: null, declined: false });
    return;
  }
  if (!card.question || (card.type !== "text" && !(card.options || []).length)) return;
  emit("ask", { card, prompt: String(prompt || "").slice(0, 600), answer: null });
}

function answerText(card, { selected = [], text = "" }) {
  const picked = selected.join("、");
  if (picked && text) return `${picked}（补充：${text}）`;
  return picked || text;
}

async function answerAsk(entry, answer) {
  const card = entry.data.card || {};
  store.update(entry.id, { answer: { ...answer, at: new Date().toISOString() } });
  rerender(entry.id);
  const display = answerText(card, answer);
  echoUser(display);
  await sendText(display, {
    replyTo: { ask_id: card.id || "", type: card.type || "", question: card.question || "", prompt: entry.data.prompt || "" },
  });
}

async function openSecret(entry) {
  const result = await openSecretSandbox(entry.data.card || {});
  if (!result) {
    revealEntry(entry.id);
    return;
  }
  const label = (result.item && result.item.label) || entry.data.card.label || "";
  store.update(entry.id, {
    saved: { message: result.message || `已安全保存 ${label}`, at: new Date().toISOString() },
    declined: false,
  });
  const node = rerender(entry.id);
  if (node) reveal(node, { user: true, focus: node.querySelector(".card-actions button") });
  toast(result.message || "已安全保存");
  refreshSecrets();
}

function declineSecret(entry) {
  store.update(entry.id, { declined: true });
  const node = rerender(entry.id);
  if (node) reveal(node, { user: true, focus: node.querySelector(".card-actions button") });
  say("好，不提供也没关系，我们换个方式继续。");
}

/* ---------------- Budget: continue card ---------------- */

function emitContinue(card, threadId) {
  if (!card || typeof card !== "object") return null;
  return emit("continue", { card, threadId: card.thread_id || threadId || "", result: null });
}

async function continueTask(entry, btn, setNote) {
  const threadId = entry.data.threadId || (entry.data.card && entry.data.card.thread_id) || "";
  if (!threadId) return;
  const owner = currentUserId();
  const revision = displayRevision;
  btn.disabled = true;
  let res;
  try {
    res = await withTyping(() => api.continueTask(threadId), { hint: "继续处理…", slowHint: SLOW_HINT });
  } catch (err) {
    if (owner !== currentUserId()) return;
    btn.disabled = false;
    setNote(friendlyError(err, "continue").text, "error");
    return;
  }
  if (owner !== currentUserId()) return;
  store.update(entry.id, { result: "continued" });
  rerender(entry.id);
  debugResponse(`/api/continue → ${res.status}`, res);
  handleChat({ ...res, thread_id: res.thread_id || threadId }, owner, revision);
}

function stopTask(entry) {
  store.update(entry.id, { result: "stopped" });
  rerender(entry.id);
  say("好，先到这里。需要的时候再告诉我继续。");
}

/* ---------------- Profile ---------------- */

export function noteProfileEdited(summary) {
  note(`已记住：${summary}。`);
}

/* ---------------- Confirm card hooks ---------------- */

function onRedraft(entry, res) {
  const d = entry.data;
  const reply = replyText((res.data || {}).messages);
  say(reply || "好，这版不用。我重写了一版，你再看看：");
  const next = emitConfirm({
    threadId: res.thread_id || d.threadId,
    draft: draftFromResponse(res.data || {}),
    version: (d.version || 1) + 1,
    basisDecisionAt: d.decision ? d.decision.at : null,
  });
  if ((res.data || {}).continue_card) emitContinue(res.data.continue_card, res.thread_id || d.threadId);
  return next.id;
}

function onDecided(entry, res) {
  const d = entry.data;
  const confirmed = !!(d.decision && d.decision.confirmed);
  const data = (res && res.data) || {};
  const reply = replyText(data.messages);
  if (reply) say(reply, { threadId: res.thread_id || d.threadId || "" });
  note(confirmed ? "已定稿。复制正文后自己发出去就行，我不会替你发送。" : "这份草稿不用了。需要的话，再跟我说一次要写什么。");
  const suggestion = cleanAssistantText(data.next_suggestion);
  if (suggestion) say(suggestion);
}

/* ---------------- Memory card ---------------- */

let memoryBusy = false;

async function applyMemory(entry, task, { forgotten = false } = {}) {
  if (memoryBusy) return false;
  memoryBusy = true;
  try {
    const res = await task();
    const data = res.data || {};
    store.update(entry.id, {
      facts: data.facts || [],
      episodes: data.episodes || [],
      error: "",
      confirmingForget: false,
      forgotten,
    });
    rerender(entry.id);
    loadProfile();
    announce(forgotten ? "已忘掉全部记忆" : "记忆已更新");
    return true;
  } catch (err) {
    store.update(entry.id, { error: friendlyError(err, "memory").text, confirmingForget: entry.data.confirmingForget });
    rerender(entry.id);
    return false;
  } finally {
    memoryBusy = false;
  }
}

function saveMemoryFact(entry, factId, value) {
  return applyMemory(entry, () => api.memoryUpdateFact(factId, value, entry.data.threadId));
}

function deleteMemoryFact(entry, factId) {
  return applyMemory(entry, () => api.memoryDeleteFact(factId, entry.data.threadId));
}

function saveMemoryEvent(entry, eventId, value) {
  return applyMemory(entry, () => api.memoryUpdateEvent(eventId, value, entry.data.threadId));
}

function deleteMemoryEvent(entry, eventId) {
  return applyMemory(entry, () => api.memoryDeleteEvent(eventId, entry.data.threadId));
}

function forgetMemory(entry) {
  return applyMemory(entry, () => api.memoryForget(entry.data.threadId), { forgotten: true });
}

function armForget(entry, armed) {
  store.update(entry.id, { confirmingForget: !!armed, error: "" });
  rerender(entry.id);
}

/** 「记忆」button: load the memory card straight from the server, no model turn. */
export async function askMemory({ echo = true } = {}) {
  const owner = currentUserId();
  if (echo) echoUser("你记得我什么");
  let res;
  try {
    res = await withTyping(() => api.memoryCard(""), { hint: "正在整理我记得的…" });
  } catch (err) {
    if (owner !== currentUserId()) return false;
    showError(err, "memory", () => askMemory({ echo: false }));
    return false;
  }
  if (owner !== currentUserId()) return false;
  const data = res.data || {};
  const entry = emit("memory", {
    threadId: "",
    facts: data.facts || [],
    episodes: data.episodes || [],
    error: "",
    confirmingForget: false,
    forgotten: false,
  });
  revealEntry(entry.id, { user: true });
  return true;
}

/* ---------------- Proactive reminders ---------------- */

if (typeof window !== "undefined") window.addEventListener("profile-updated", () => {
  displayRevision++;
});

export async function loadProactive() {
  const owner = currentUserId();
  let items = [];
  try {
    const res = await api.proactivePending();
    items = (res.data && res.data.items) || [];
  } catch {
    return;
  }
  if (owner !== currentUserId()) return;
  items = items.filter((item) => !item.user_id || item.user_id === owner);
  const seen = new Set(store.find((e) => e.type === "proactive").map((e) => String(e.data.item && e.data.item.id)));
  items
    .slice()
    .reverse()
    .forEach((item) => {
      if (!seen.has(String(item.id))) emit("proactive", { item, dismissed: false });
    });
}

async function dismissProactive(entry, btn, setNote) {
  btn.disabled = true;
  try {
    await api.dismissProactive(entry.data.item.id);
  } catch (err) {
    if (err.status !== 404) {
      btn.disabled = false;
      setNote(friendlyError(err, "proactive").text, "error");
      return;
    }
  }
  store.update(entry.id, { dismissed: true });
  rerender(entry.id);
}

/* ---------------- Restore / reset ---------------- */

/** Decisions can also be made in Feishu / WeChat; the latest one on the server wins. */
async function syncDecisions() {
  const cards = store.find((e) => e.type === "confirm" && e.data.threadId && !e.data.supersededBy).slice(-12);
  await Promise.all(
    cards.map(async (entry) => {
      let dec;
      try {
        const res = await api.decision(entry.data.threadId);
        dec = res.data && res.data.decision;
      } catch {
        return;
      }
      if (!dec || !dec.decided_at) return;
      const d = entry.data;
      const serverAt = timeOf(dec.decided_at);
      if (d.decision && timeOf(d.decision.at) >= serverAt) return;
      if (d.basisDecisionAt && timeOf(d.basisDecisionAt) >= serverAt) return;
      d.decision = { confirmed: !!dec.confirmed, at: dec.decided_at, overridden: !!dec.overridden, channel: dec.channel || "", reason: dec.reason || "" };
      store.update(entry.id, {});
      rerender(entry.id);
    })
  );
}

export function restoreTranscript() {
  const entries = store.entries();
  if (!entries.length) return false;
  nodes.clear();
  for (const entry of entries) {
    try {
      renderEntry(entry, { restored: true });
    } catch {
      /* skip an entry this version can no longer render */
    }
  }
  systemLine("以上是之前的对话", { divider: true, scroll: false });
  requestAnimationFrame(() => scrollToBottom());
  syncDecisions();
  return true;
}

export function resetThread() {
  displayRevision++;
  nodes.clear();
  clearLog();
}

export function showHero() {
  renderHero({
    onChip: (text) => sendComposer({ text }),
    onMemory: () => askMemory(),
  });
}
