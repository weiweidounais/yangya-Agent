import { $, h, icon, relTime, toast, announce, debugBlock } from "./core.js?v=__APP_VERSION__";
import { api, friendlyError, debugText } from "./api.js?v=__APP_VERSION__";
import { kindLabel, memoryText } from "./text.js?v=__APP_VERSION__";
import { currentUserId } from "./identity.js?v=__APP_VERSION__";

const OVERLAY_MQ = window.matchMedia("(max-width: 900px)");
const SCALARS = [
  ["name", "姓名", "比如：李四"],
  ["city", "城市", "比如：杭州"],
  ["tz", "时区", "比如：Asia/Shanghai"],
  ["occupation", "职业", "比如：产品经理"],
  ["sport_goal", "运动目标", "比如：半马 2 小时"],
  ["sport_level", "训练水平", "比如：刚开始跑 / 有基础 / 进阶"],
  ["sport_days_per_week", "每周训练天数", "比如：3"],
  ["sport_session_minutes", "单次时长（分钟）", "比如：40"],
  ["sport_injuries", "伤病与限制", "比如：右膝外侧偶尔不适"],
];
const LISTS = [
  ["interests", "兴趣", "skill", "一行一条，比如：跑步"],
  ["preferences", "偏好", "ok", "一行一条，比如：回复尽量简短"],
  ["sport_equipment", "器械与场地", "exp", "一行一条，比如：跑鞋"],
  ["facts", "备注", "exp", "一行一条，比如：下周三出差"],
];
const HIDDEN_KEYS = ["dialogue_summary", "last_summary_kind", "privacy_sandbox", "user_id", "updated_at", "created_at"];
const LABELS = Object.fromEntries([...SCALARS, ...LISTS].map(([key, label]) => [key, label]));

let hooks = { onUserChange() {}, onProfileEdited() {}, onClearLocal() {} };
let isOpen = false;
let profile = null;
let profileOwner = "";
let profileRevision = 0;
let profileError = "";
let editingField = "";
let rowError = "";
let saving = false;
let saveRequest = 0;
let memory = { state: "idle", events: [], error: "", query: "" };
let memoryTimer = 0;
let profileBox;
let memoryBox;
let secretsBox;
let secrets = { state: "idle", items: [], error: "", armed: "" };

export function initDrawer(options) {
  hooks = { ...hooks, ...options };
  profileBox = h("div", { class: "dw-profile" });
  memoryBox = h("div", { class: "dw-memory" });
  secretsBox = h("div", { class: "dw-secrets" });
  $("drawerBody").append(
    h("p", { class: "drawer-tip", text: "可选项 — 不填也能用。在对话里告诉我，我会自动记住。" }),
    h("section", { class: "drawer-section", "aria-labelledby": "dwProfileTitle" }, [
      h("h3", { class: "drawer-section-title", id: "dwProfileTitle", text: "阿动记住了什么" }),
      profileBox,
    ]),
    h("section", { class: "drawer-section", "aria-labelledby": "dwMemoryTitle" }, [
      h("div", { class: "drawer-section-head" }, [
        h("h3", { class: "drawer-section-title", id: "dwMemoryTitle", text: "最近的记忆" }),
        h("button", { type: "button", class: "btn-icon btn-icon-sm", "aria-label": "刷新记忆", onClick: () => loadMemory() }, [icon("refresh")]),
      ]),
      memoryBox,
    ]),
    h("section", { class: "drawer-section", id: "dwSecrets", "aria-labelledby": "dwSecretsTitle" }, [
      h("h3", { class: "drawer-section-title", id: "dwSecretsTitle", tabindex: "-1", text: "安全保存的信息" }),
      secretsBox,
    ])
  );

  $("btnProfile").addEventListener("click", () => (isOpen ? closeDrawer() : openDrawer()));
  $("btnDrawerClose").addEventListener("click", () => closeDrawer());
  $("backdrop").addEventListener("click", () => closeDrawer());
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && isOpen && !e.defaultPrevented) closeDrawer();
  });
  $("drawer").addEventListener("keydown", trapFocus);
  OVERLAY_MQ.addEventListener("change", syncMode);
  const userInput = $("userId");
  userInput.addEventListener("change", () => hooks.onUserChange());
  userInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      hooks.onUserChange();
    }
  });
  $("btnClearLocal").addEventListener("click", onClearClick);
  renderProfile();
  renderMemory();
  renderSecrets();
}

export function getProfile() {
  return typeof currentUserId !== "function" || profileOwner === currentUserId() ? profile : null;
}

function profileKey(value) {
  if (Array.isArray(value)) return value.map(profileKey);
  if (value && typeof value === "object") return Object.fromEntries(
    Object.keys(value).sort().map(key => [key, profileKey(value[key])])
  );
  return value;
}

export function setProfile(next, owner) {
  const active = typeof currentUserId === "function" ? currentUserId() : owner;
  if (owner !== active || !next || typeof next !== "object") return;
  const changed = profileOwner !== owner || JSON.stringify(profileKey(profile)) !== JSON.stringify(profileKey(next));
  profile = next;
  profileOwner = owner;
  if (changed) {
    profileRevision++;
    if (typeof window.dispatchEvent === "function") window.dispatchEvent(new Event("profile-updated"));
  }
  profileError = "";
  renderProfile();
}

export async function loadProfile() {
  const owner = typeof currentUserId === "function" ? currentUserId() : profileOwner;
  const revision = profileRevision;
  try {
    const res = await api.profile();
    if ((typeof currentUserId === "function" && owner !== currentUserId()) || revision !== profileRevision) return;
    setProfile((res.data && res.data.profile) || {}, owner);
    profileError = "";
  } catch (err) {
    if ((typeof currentUserId === "function" && owner !== currentUserId()) || revision !== profileRevision) return;
    profileError = friendlyError(err, "profile").text;
  }
  renderProfile();
}

export function resetDrawer() {
  saveRequest++;
  saving = false;
  profileRevision++;
  profileOwner = "";
  profile = null;
  profileError = "";
  editingField = "";
  memory = { state: "idle", events: [], error: "", query: "" };
  secrets = { state: "idle", items: [], error: "", armed: "" };
  renderProfile();
  renderMemory();
  renderSecrets();
  loadProfile();
  if (isOpen) {
    loadMemory();
    loadSecrets();
  }
}

export function openDrawer({ focusField = "", section = "" } = {}) {
  const drawer = $("drawer");
  if (!isOpen) {
    isOpen = true;
    drawer.inert = false;
    drawer.removeAttribute("inert");
    drawer.classList.add("is-open");
    document.body.classList.add("drawer-open");
    $("btnProfile").setAttribute("aria-expanded", "true");
    syncMode();
    loadProfile();
    loadMemory();
    loadSecrets();
  }
  if (section === "secrets") {
    window.setTimeout(() => {
      $("dwSecrets").scrollIntoView({ block: "start", behavior: "smooth" });
      $("dwSecretsTitle").focus({ preventScroll: true });
    }, 80);
  } else if (focusField && LABELS[focusField]) {
    editingField = focusField;
    rowError = "";
    renderProfile();
    focusEditor();
  } else {
    window.setTimeout(() => $("btnDrawerClose").focus(), 60);
  }
}

export function closeDrawer() {
  if (!isOpen) return;
  const drawer = $("drawer");
  const hadFocus = drawer.contains(document.activeElement);
  isOpen = false;
  editingField = "";
  drawer.classList.remove("is-open");
  drawer.inert = true;
  drawer.setAttribute("inert", "");
  drawer.removeAttribute("role");
  drawer.removeAttribute("aria-modal");
  document.body.classList.remove("drawer-open");
  $("backdrop").classList.remove("is-open");
  $("btnProfile").setAttribute("aria-expanded", "false");
  if (hadFocus || document.activeElement === document.body) $("btnProfile").focus();
}

function syncMode() {
  const drawer = $("drawer");
  const overlay = OVERLAY_MQ.matches && isOpen;
  $("backdrop").classList.toggle("is-open", overlay);
  if (overlay) {
    drawer.setAttribute("role", "dialog");
    drawer.setAttribute("aria-modal", "true");
  } else {
    drawer.removeAttribute("role");
    drawer.removeAttribute("aria-modal");
  }
}

function trapFocus(e) {
  if (e.key !== "Tab" || !isOpen || !OVERLAY_MQ.matches) return;
  const focusables = Array.from(
    $("drawer").querySelectorAll("button, [href], input, select, textarea, summary, [tabindex]:not([tabindex='-1'])")
  ).filter((n) => !n.disabled && n.offsetParent !== null);
  if (!focusables.length) return;
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}

function focusEditor() {
  window.setTimeout(() => {
    const input = profileBox.querySelector(".dw-edit .field-input");
    if (input) {
      input.focus();
      input.scrollIntoView({ block: "center" });
    }
  }, 80);
}

function startEdit(field) {
  editingField = field;
  rowError = "";
  renderProfile();
  focusEditor();
}

function cancelEdit() {
  const field = editingField;
  editingField = "";
  rowError = "";
  renderProfile();
  const back = profileBox.querySelector(`[data-edit="${field}"]`);
  if (back) back.focus();
}

async function save(field, patch, summary) {
  const request = ++saveRequest;
  const owner = currentUserId();
  const revision = profileRevision;
  saving = true;
  rowError = "";
  renderProfile();
  try {
    const res = await api.saveProfile(patch);
    if (owner !== currentUserId() || request !== saveRequest) return;
    if (revision !== profileRevision) await loadProfile();
    else setProfile((res.data && res.data.profile) || profile, owner);
    if (owner !== currentUserId() || request !== saveRequest) return;
    editingField = "";
    saving = false;
    renderProfile();
    toast("已记住");
    hooks.onProfileEdited(summary);
    const back = profileBox.querySelector(`[data-edit="${field}"]`);
    if (back) back.focus();
    loadMemory();
  } catch (err) {
    if (owner !== currentUserId() || request !== saveRequest) return;
    saving = false;
    rowError = friendlyError(err, "profile").text;
    renderProfile();
    focusEditor();
  } finally {
    if (request === saveRequest) {
      saving = false;
      renderProfile();
    }
  }
}

function submitField(field, raw) {
  const label = LABELS[field];
  if (SCALARS.some(([key]) => key === field)) {
    const value = raw.trim();
    if (!value) {
      rowError = "不能留空：暂时不支持清空，改成新的内容就行。";
      renderProfile();
      focusEditor();
      return;
    }
    if (value === String((profile && profile[field]) || "")) {
      cancelEdit();
      return;
    }
    save(field, { [field]: value }, `${label}改为「${value}」`);
    return;
  }
  const values = raw.split("\n").map((s) => s.trim()).filter(Boolean);
  const existing = new Set(((profile && profile[field]) || []).map(String));
  const fresh = values.filter((v) => !existing.has(v));
  if (!fresh.length) {
    rowError = values.length ? "这些已经在档案里了。" : "先写点内容再保存。";
    renderProfile();
    focusEditor();
    return;
  }
  save(field, { [field]: fresh }, `${label}补充了「${fresh.join("、")}」`);
}

function editor(field, initial, multiline, placeholder) {
  const id = `dw-input-${field}`;
  const isList = !SCALARS.some(([key]) => key === field);
  const input = multiline
    ? h("textarea", { class: "field-input", id, rows: 3, value: initial, placeholder })
    : h("input", { class: "field-input", id, value: initial, placeholder, autocomplete: "off" });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      cancelEdit();
    }
  });
  const errorId = `${id}-err`;
  if (rowError) input.setAttribute("aria-describedby", errorId);
  return h(
    "form",
    {
      class: "dw-edit",
      onSubmit: (e) => {
        e.preventDefault();
        if (!saving) submitField(field, input.value);
      },
    },
    [
      h("label", { class: "dw-field-label", for: id, text: isList ? `补充${LABELS[field]}` : LABELS[field] }),
      input,
      isList ? h("p", { class: "dw-hint", text: "一行一条，可以一次补充多条" }) : null,
      rowError ? h("p", { class: "dw-error", id: errorId, role: "alert", text: rowError }) : null,
      h("div", { class: "dw-edit-actions" }, [
        h("button", { type: "submit", class: `btn btn-primary btn-sm${saving ? " is-loading" : ""}`, disabled: saving, text: "保存" }),
        h("button", { type: "button", class: "btn btn-quiet btn-sm", text: "取消", disabled: saving, onClick: cancelEdit }),
      ]),
    ]
  );
}

function scalarRow([field, label, placeholder]) {
  const value = String((profile && profile[field]) || "");
  if (editingField === field) return h("div", { class: "dw-row is-editing" }, [editor(field, value, false, placeholder)]);
  return h("div", { class: "dw-row" }, [
    h("div", { class: "dw-row-main" }, [
      h("span", { class: "dw-label", text: label }),
      h("span", { class: `dw-value${value ? "" : " is-empty"}`, text: value || "未填写" }),
    ]),
    h("button", {
      type: "button",
      class: "btn-text dw-edit-btn",
      dataset: { edit: field },
      "aria-label": `${value ? "修改" : "填写"}${label}`,
      text: value ? "修改" : "填写",
      onClick: () => startEdit(field),
    }),
  ]);
}

function listRow([field, label, cls, placeholder]) {
  const items = ((profile && profile[field]) || []).map(String).filter(Boolean);
  const head = h("div", { class: "dw-row-main" }, [
    h("span", { class: "dw-label", text: label }),
    h("span", { class: "dw-count", text: items.length ? `${items.length} 项` : "暂无" }),
  ]);
  const addBtn = h("button", {
    type: "button",
    class: "btn-text dw-edit-btn",
    dataset: { edit: field },
    "aria-label": `补充${label}`,
    text: "补充",
    onClick: () => startEdit(field),
  });
  const row = h("div", { class: `dw-row dw-list${editingField === field ? " is-editing" : ""}` }, [
    h("div", { class: "dw-row-top" }, [head, editingField === field ? null : addBtn]),
  ]);
  if (items.length) {
    row.append(h("ul", { class: "dw-exp" }, items.slice(0, 20).map((t) => h("li", {}, [h("span", { text: t })]))));
    if (items.length > 20) row.append(h("p", { class: "dw-hint", text: `还有 ${items.length - 20} 条，可在对话里的记忆卡上查看。` }));
  }
  if (editingField === field) row.append(editor(field, "", true, placeholder));
  return row;
}

function renderProfile() {
  if (!profileBox) return;
  profileBox.replaceChildren();
  if (!profile) {
    profileBox.append(
      profileError
        ? h("div", { class: "dw-empty" }, [
            h("p", { text: profileError }),
            h("button", { type: "button", class: "btn btn-ghost btn-sm", text: "重新加载", onClick: () => loadProfile() }),
          ])
        : h("p", { class: "dw-empty", text: "正在读取档案…" })
    );
    return;
  }
  const empty = !Object.keys(profile).some((k) => {
    const v = profile[k];
    return Array.isArray(v) ? v.length && !HIDDEN_KEYS.includes(k) : !!v && !HIDDEN_KEYS.includes(k);
  });
  if (empty) {
    profileBox.append(h("p", { class: "dw-empty", text: "还没有档案。在对话里告诉我你的情况，或者在下面直接填写几项。" }));
  }
  SCALARS.forEach((def) => profileBox.append(scalarRow(def)));
  LISTS.forEach((def) => profileBox.append(listRow(def)));
  profileBox.append(
    h("p", {
      class: "dw-note",
      text: "这里改的内容会直接记进档案。单条记忆可以在对话里的记忆卡上修改或删除。",
    })
  );
  const dbg = debugBlock("profile JSON", profile);
  if (dbg) profileBox.append(dbg);
}

async function loadMemory() {
  memory.state = "loading";
  renderMemory();
  try {
    const res = await api.memory({ q: memory.query });
    memory.events = (res.data && res.data.events) || [];
    memory.state = "ok";
  } catch (err) {
    memory.state = "error";
    memory.error = friendlyError(err, "memory").text;
    memory.debug = debugText(err);
  }
  renderMemory();
}

function renderMemory() {
  if (!memoryBox) return;
  let search = memoryBox.querySelector("input[type=search]");
  if (!search) {
    search = h("input", {
      type: "search",
      class: "field-input mem-search",
      id: "memSearch",
      placeholder: "搜索记忆，比如 出差",
      "aria-label": "搜索记忆",
    });
    search.addEventListener("input", () => {
      window.clearTimeout(memoryTimer);
      memoryTimer = window.setTimeout(() => {
        memory.query = search.value.trim();
        loadMemory();
      }, 320);
    });
    memoryBox.append(search, h("div", { class: "mem-results", "aria-live": "polite" }));
  }
  const results = memoryBox.querySelector(".mem-results");
  results.replaceChildren();
  if (memory.state === "loading" && !memory.events.length) {
    results.append(h("p", { class: "dw-empty", text: "正在读取记忆…" }));
    return;
  }
  if (memory.state === "error") {
    results.append(
      h("div", { class: "dw-empty" }, [
        h("p", { text: memory.error }),
        h("button", { type: "button", class: "btn btn-ghost btn-sm", text: "重试", onClick: () => loadMemory() }),
        debugBlock("调试信息", memory.debug),
      ])
    );
    return;
  }
  if (memory.state === "idle") return;
  if (!memory.events.length) {
    results.append(
      h("p", {
        class: "dw-empty",
        text: memory.query ? `没有找到和「${memory.query}」相关的记忆。` : "还没有记忆。聊几句，我会把要点记在这里。",
      })
    );
    return;
  }
  results.append(
    h(
      "ol",
      { class: "mem-list" },
      memory.events.map((ev) =>
        h("li", { class: "mem-item" }, [
          h("div", { class: "mem-top" }, [
            h("span", { class: "tag", text: kindLabel(ev.kind) }),
            h("span", { class: "mem-time", text: relTime(ev.created_at) }),
          ]),
          h("p", { class: "mem-text", text: memoryText(ev) }),
        ])
      )
    )
  );
  announce(`找到 ${memory.events.length} 条记忆`);
}

function onClearClick() {
  const btn = $("btnClearLocal");
  if (btn.dataset.armed !== "1") {
    btn.dataset.armed = "1";
    btn.textContent = "再点一次，确认清除";
    btn.classList.add("is-armed");
    window.setTimeout(() => disarm(btn), 4000);
    return;
  }
  disarm(btn);
  hooks.onClearLocal();
}

function disarm(btn) {
  btn.dataset.armed = "";
  btn.textContent = "清除本机对话记录";
  btn.classList.remove("is-armed");
}

/* ---------------- Secret vault (masked list + delete; values never reach the browser) ---------------- */

export function refreshSecrets() {
  if (isOpen || secrets.state !== "idle") loadSecrets();
}

const LOGIN_FOR_SECRETS = "进入你自己的账号后，可以在这里查看和删除安全保存的信息。";

async function loadSecrets() {
  if (currentUserId() === "demo") {
    secrets = { state: "error", items: [], error: LOGIN_FOR_SECRETS, armed: "" };
    renderSecrets();
    return;
  }
  secrets.state = "loading";
  renderSecrets();
  try {
    const res = await api.secrets();
    secrets.items = (res.data && res.data.items) || [];
    secrets.state = "ok";
  } catch (err) {
    secrets.state = "error";
    secrets.error = err.status === 401 ? LOGIN_FOR_SECRETS : friendlyError(err, "secret").text;
  }
  renderSecrets();
}

async function removeSecret(item, btn) {
  if (secrets.armed !== item.key) {
    secrets.armed = item.key;
    renderSecrets();
    const again = secretsBox.querySelector(`[data-secret="${item.key}"] .sec-delete`);
    if (again) {
      again.focus();
      again.addEventListener("focusout", () => {
        if (secrets.armed !== item.key) return;
        secrets.armed = "";
        window.setTimeout(renderSecrets, 0);
      });
    }
    return;
  }
  btn.disabled = true;
  try {
    await api.deleteSecret(item.key);
    secrets.items = secrets.items.filter((it) => it.key !== item.key);
    secrets.armed = "";
    renderSecrets();
    toast(`已删除 ${item.label}`);
    announce(`已删除 ${item.label}`);
    $("dwSecretsTitle").focus();
  } catch (err) {
    btn.disabled = false;
    toast(err.status === 404 ? "这条信息已经不在了" : friendlyError(err, "secret").text, { error: true });
    if (err.status === 404) loadSecrets();
  }
}

function renderSecrets() {
  if (!secretsBox) return;
  secretsBox.replaceChildren();
  if (secrets.state === "loading" && !secrets.items.length) {
    secretsBox.append(h("p", { class: "dw-empty", text: "正在读取…" }));
    return;
  }
  if (secrets.state === "error") {
    secretsBox.append(h("p", { class: "dw-empty", text: secrets.error }));
    return;
  }
  if (!secrets.items.length) {
    secretsBox.append(
      h("p", { class: "dw-empty", text: "还没有保存。需要账号密码、手机号这类信息时，我会在对话里打开安全输入区。" })
    );
    return;
  }
  secretsBox.append(
    h(
      "ul",
      { class: "sec-list" },
      secrets.items.map((item) => {
        const armed = secrets.armed === item.key;
        return h("li", { class: "sec-item", dataset: { secret: item.key } }, [
          h("div", { class: "sec-main" }, [
            h("span", { class: "sec-label" }, [icon("lock"), h("span", { text: item.label })]),
            h(
              "span",
              { class: "sec-masked" },
              (item.masked || []).map((m) => h("span", { class: "sec-field", text: `${m.label.replace(/（.*）$/, "")} ${m.preview}` }))
            ),
            item.updated_at ? h("span", { class: "mem-time", text: `更新于 ${relTime(item.updated_at)}` }) : null,
          ]),
          h("button", {
            type: "button",
            class: `btn-text sec-delete${armed ? " is-armed" : ""}`,
            "aria-label": armed ? `确认删除${item.label}` : `删除${item.label}`,
            text: armed ? "确认删除" : "删除",
            onClick: (e) => removeSecret(item, e.currentTarget),
          }),
        ]);
      })
    ),
    h("p", { class: "dw-note", text: "只显示脱敏信息，原文加密保存在服务端，网页上看不到。" })
  );
}
