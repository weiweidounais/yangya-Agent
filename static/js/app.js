import { initSchedules } from "./schedules.js?v=__APP_VERSION__";
import { initCompanion, refreshCompanion } from "./companion.js?v=__APP_VERSION__";
import { $, h, DEBUG, VERSION, toast } from "./core.js?v=__APP_VERSION__";
import { api } from "./api.js?v=__APP_VERSION__";
import { currentUserId, ensureSession } from "./identity.js?v=__APP_VERSION__";
import { store } from "./store.js?v=__APP_VERSION__";
import { initThread } from "./thread.js?v=__APP_VERSION__";
import { initComposer, focusComposer } from "./composer.js?v=__APP_VERSION__";
import { initDrawer, loadProfile, resetDrawer } from "./drawer.js?v=__APP_VERSION__";
import {
  sendComposer,
  askMemory,
  restoreTranscript,
  showHero,
  resetThread,
  loadProactive,
  noteProfileEdited,
} from "./flows.js?v=__APP_VERSION__";

let health = null;
let lastUserId = currentUserId();

function badge(text, cls = "") {
  return h("span", { class: `badge${cls ? ` ${cls}` : ""}` }, [h("span", { class: "dot", "aria-hidden": "true" }), text]);
}

function renderDebugBadges() {
  if (!DEBUG) return;
  const box = $("debugBadges");
  box.replaceChildren();
  if (!health) {
    box.append(badge("服务离线", "is-off"), badge(`ui ${VERSION}`), badge(`user ${currentUserId()}`));
    return;
  }
  const fast = health.fast || (health.routing && health.routing.fast) || {};
  const strong = health.strong || (health.routing && health.routing.strong) || {};
  box.append(
    badge(
      `llm ${health.llm || "?"} · fast ${fast.provider || "?"}/${fast.model || "?"} · strong ${strong.provider || "?"}/${strong.model || "?"}`,
      health.llm === "live" ? "is-live" : "is-mock"
    ),
    badge(`api ${health.version || "?"} · ui ${VERSION}`),
    badge(`user ${currentUserId()}`)
  );
}

async function loadHealth() {
  try {
    health = await api.health();
  } catch {
    health = null;
  }
  renderDebugBadges();
}

function startConversation() {
  if (!restoreTranscript()) showHero();
  loadProactive();
}

function onUserChange() {
  const uid = currentUserId();
  if (uid === lastUserId) return;
  lastUserId = uid;
  store.reload();
  resetThread();
  resetDrawer();
  refreshCompanion();
  startConversation();
  renderDebugBadges();
  toast("已切换账号，档案和对话记录随之切换");
}

function onClearLocal() {
  store.clear();
  resetThread();
  showHero();
  toast("已清除这台设备上的对话记录");
  focusComposer();
}

async function boot() {
  if (DEBUG) document.body.classList.add("is-debug");
  initThread();
  initSchedules();
  initComposer({ onSubmit: sendComposer });
  initDrawer({ onUserChange, onProfileEdited: noteProfileEdited, onClearLocal });
  initCompanion();
  $("btnMemory").addEventListener("click", () => askMemory());

  try {
    await ensureSession();
  } catch {
    /* requests retry the session themselves */
  }
  onUserChange();
  const healthReady = loadHealth();
  loadProfile();
  await Promise.race([healthReady, new Promise((resolve) => window.setTimeout(resolve, 1500))]);
  startConversation();
  if (window.matchMedia("(pointer: fine)").matches) focusComposer();
}

boot();
