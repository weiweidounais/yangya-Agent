import { h, icon, nextId, dayTime, announce, toast, copyText, debugBlock } from "./core.js?v=__APP_VERSION__";
import { api, friendlyError, debugText } from "./api.js?v=__APP_VERSION__";
import { draftKindLabel } from "./text.js?v=__APP_VERSION__";

/*
 * Generic draft confirm card. The server writes a draft {kind, title, body, notes};
 * the user may edit title/body in place, then 确认定稿 (edits travel with the confirm)
 * or 拒绝 (optionally with a reason, which feeds the automatic redraft).
 */

const CHANNELS = { feishu: "飞书", openclaw_wechat: "微信" };
const LONG_TEXT = 900;

function btn(label, cls, onClick, iconName = "") {
  return h("button", { type: "button", class: cls, onClick }, [iconName ? icon(iconName) : null, label]);
}

function humanNote(noteText) {
  const s = String(noteText || "").trim();
  if (/自动重出稿/.test(s)) return "这是你拒绝后自动重写的版本";
  if (/[a-z]_[a-z]|=|stub/i.test(s)) return "";
  return s;
}

/** Normalise the draft shape from /api/chat, /api/draft, /api/confirm and /api/continue. */
export function draftFromResponse(data) {
  const draft = (data && data.draft) || {};
  const intr = (data && data.interrupt) || {};
  const task = (data && data.task_card) || {};
  return {
    kind: String(draft.kind || task.kind || intr.kind || "other"),
    title: String(draft.title || task.draft_title || intr.title || ""),
    body: String(draft.body || task.body || intr.body || ""),
    notes: Array.isArray(draft.notes) ? draft.notes : Array.isArray(intr.notes) ? intr.notes : [],
  };
}

export function confirmCard(entry, ctx) {
  const d = entry.data;
  d.draft = d.draft || { kind: "other", title: "", body: "", notes: [] };
  const kindText = draftKindLabel(d.draft.kind);
  const titleId = nextId("t");
  const root = h("section", {
    class: `card card-confirm${ctx.restored ? "" : " enter"}`,
    "aria-labelledby": titleId,
    dataset: { thread: d.threadId || "" },
  });
  const bandTags = h("div", { class: "cc-tags" });
  const band = h("div", { class: "cc-band" }, [
    h("h3", { id: titleId, text: `待你确认：${kindText}草稿` }),
    bandTags,
  ]);
  const policy = h("p", { class: "cc-policy", text: "确认后才算定稿，我不会替你发送 · 这张卡不过期，之后改主意，以最后一次为准" });
  const sections = h("div", { class: "cc-sections" });
  const notes = h("ul", { class: "cc-notes", "aria-label": "草稿说明" });
  const rejectBox = h("div", { class: "cc-reject", hidden: true });
  const actions = h("div", { class: "card-actions cc-actions" });
  const status = h("p", { class: "card-status", "aria-live": "polite" });
  const error = h("div", { class: "card-error", role: "alert", hidden: true });
  const superseded = h("div", { class: "cc-superseded", hidden: true });
  root.append(
    band,
    h("div", { class: "cc-body" }, [policy, sections, notes, rejectBox, actions, status, error, superseded])
  );
  const raw = debugBlock("thread_id / 原始草稿", { thread_id: d.threadId, draft: d.draft, decision: d.decision });
  if (raw) root.append(raw);

  let editing = false;
  let rejecting = false;
  let busy = false;
  let inputs = {};

  const save = () => ctx.save(entry);
  const current = () => ({
    title: d.edits && d.edits.title != null ? d.edits.title : d.draft.title || "",
    body: d.edits && d.edits.body != null ? d.edits.body : d.draft.body || "",
  });

  function showError(text, debug = "") {
    error.replaceChildren(h("span", { text }));
    const block = debug ? debugBlock("调试信息", debug) : null;
    if (block) error.append(block);
    error.hidden = false;
  }

  function hideError() {
    error.hidden = true;
    error.replaceChildren();
  }

  function setBusy(value, text = "") {
    busy = value;
    root.setAttribute("aria-busy", String(value));
    for (const node of [...actions.children, ...rejectBox.children]) {
      if (node.tagName === "BUTTON") node.disabled = value;
    }
    if (value && text) status.textContent = text;
  }

  function renderTags() {
    const tags = [h("span", { class: "tag tag-inverse", text: kindText })];
    if ((d.version || 1) > 1) tags.push(h("span", { class: "tag tag-inverse", text: `第 ${d.version} 版` }));
    if (d.decision && !d.supersededBy) {
      tags.push(
        h("span", {
          class: `tag ${d.decision.confirmed ? "tag-ok" : "tag-inverse"}`,
          text: d.decision.confirmed ? "已定稿" : "已拒绝",
        })
      );
    }
    bandTags.replaceChildren(...tags);
  }

  function textBlock(text) {
    const textNode = h("div", { class: "draft-text", text });
    const parts = [textNode];
    if (text.length > LONG_TEXT || text.split("\n").length > 14) {
      textNode.classList.add("is-clamped");
      const toggle = h("button", { type: "button", class: "btn-text more-toggle", "aria-expanded": "false", text: "展开全文" });
      toggle.addEventListener("click", () => {
        const open = textNode.classList.toggle("is-clamped") === false;
        toggle.setAttribute("aria-expanded", String(open));
        toggle.textContent = open ? "收起" : "展开全文";
      });
      parts.push(toggle);
    }
    return parts;
  }

  function copyButton(label, getText) {
    return h(
      "button",
      {
        type: "button",
        class: "btn-text cc-copy",
        "aria-label": `复制${label}`,
        onClick: async () => {
          const ok = await copyText(getText());
          toast(ok ? `已复制${label}` : "复制失败，请手动选中文字复制", { error: !ok });
        },
      },
      [icon("copy"), "复制"]
    );
  }

  function renderSections() {
    sections.replaceChildren();
    const cur = current();
    if (editing) {
      inputs = {};
      const titleInputId = nextId("f");
      const titleInput = h("input", { class: "field-input draft-edit-title", id: titleInputId, type: "text", maxlength: "200", value: cur.title, placeholder: "标题（可留空）" });
      const bodyInputId = nextId("f");
      const bodyInput = h("textarea", { class: "field-input draft-edit", id: bodyInputId, rows: 12, value: cur.body });
      for (const area of [titleInput, bodyInput]) {
        area.addEventListener("keydown", (e) => {
          if (e.key === "Escape") {
            e.preventDefault();
            e.stopPropagation();
            cancelEdit();
          } else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            finishEdit();
          }
        });
      }
      inputs = { title: titleInput, body: bodyInput };
      sections.append(
        h("div", { class: "cc-section" }, [h("label", { class: "cc-label", for: titleInputId, text: "标题" }), titleInput]),
        h("div", { class: "cc-section" }, [h("label", { class: "cc-label", for: bodyInputId, text: "正文" }), bodyInput]),
        h("p", { class: "card-sub", text: "直接改成你想要的样子。Ctrl / ⌘ + Enter 完成，Esc 放弃。" })
      );
      return;
    }
    const titleEdited = d.edits && d.edits.title != null && d.edits.title !== (d.draft.title || "");
    const bodyEdited = d.edits && d.edits.body != null && d.edits.body !== (d.draft.body || "");
    if (cur.title) {
      sections.append(
        h("div", { class: "cc-section" }, [
          h("div", { class: "cc-section-head" }, [
            h("span", { class: "cc-label", text: "标题" }),
            titleEdited ? h("span", { class: "tag", text: "你改过" }) : null,
          ]),
          h("p", { class: "draft-title", text: cur.title }),
        ])
      );
    }
    if (cur.body) {
      sections.append(
        h("div", { class: "cc-section" }, [
          h("div", { class: "cc-section-head" }, [
            h("span", { class: "cc-label", text: d.decision && d.decision.confirmed ? "定稿正文" : "正文" }),
            bodyEdited ? h("span", { class: "tag", text: "你改过" }) : null,
            copyButton("正文", () => current().body),
          ]),
          ...textBlock(cur.body),
        ])
      );
    } else {
      sections.append(h("p", { class: "card-text", text: "这次没拿到草稿内容。可以点「拒绝」，我会重写一版。" }));
    }
  }

  function renderNotes() {
    const items = (d.draft.notes || []).map(humanNote).filter(Boolean);
    notes.replaceChildren(...items.map((t) => h("li", {}, [icon("check"), h("span", { text: t })])));
    notes.hidden = !items.length || editing;
  }

  function renderReject() {
    rejectBox.replaceChildren();
    rejectBox.hidden = !rejecting;
    if (!rejecting) return;
    const id = nextId("f");
    const reason = h("textarea", { class: "field-input", id, rows: 2, maxlength: "500", placeholder: "比如：语气太正式，短一点" });
    reason.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        rejecting = false;
        render();
      } else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        decide(false, reason.value.trim());
      }
    });
    rejectBox.append(
      h("label", { class: "cc-label", for: id, text: "哪里不合适？（选填，我会按这个重写）" }),
      reason,
      h("div", { class: "card-actions" }, [
        btn("确认拒绝", "btn btn-primary btn-sm", () => decide(false, reason.value.trim())),
        btn("取消", "btn btn-quiet btn-sm", () => {
          rejecting = false;
          render();
        }),
      ])
    );
  }

  function renderActions() {
    actions.replaceChildren();
    if (editing || rejecting) {
      if (editing) actions.append(btn("完成编辑", "btn btn-primary btn-sm", finishEdit), btn("放弃修改", "btn btn-quiet btn-sm", cancelEdit));
      return;
    }
    const dec = d.decision;
    if (!dec || d.editedAfterDecision) {
      actions.append(
        btn(d.editedAfterDecision ? "确认修改" : "确认定稿", "btn btn-primary", () => decide(true)),
        btn("编辑", "btn btn-ghost", enterEdit, "edit"),
        btn("拒绝", "btn btn-text", startReject)
      );
    } else if (dec.confirmed) {
      actions.append(btn("编辑", "btn btn-ghost", enterEdit, "edit"), btn("改为拒绝", "btn btn-text", startReject));
    } else {
      actions.append(btn("改为确认", "btn btn-primary", () => decide(true)), btn("编辑", "btn btn-ghost", enterEdit, "edit"));
    }
  }

  function renderStatus() {
    const dec = d.decision;
    root.classList.toggle("is-confirmed", !!(dec && dec.confirmed));
    root.classList.toggle("is-rejected", !!(dec && !dec.confirmed));
    if (!dec) {
      status.textContent = "等你决定：确认后才算定稿，我不会替你发送。";
      return;
    }
    const via = dec.channel && CHANNELS[dec.channel] ? `在${CHANNELS[dec.channel]}` : "";
    const over = dec.overridden ? "（覆盖了之前的决定）" : "";
    let text = dec.confirmed
      ? `已于 ${dayTime(dec.at)} ${via}定稿${over}。复制后自己发出去就行；改主意可以点「改为拒绝」。`
      : `已于 ${dayTime(dec.at)} ${via}拒绝${over}。卡片不会过期，想用时点「改为确认」。`;
    if (dec.reason) text += ` 你的意见：「${dec.reason}」。`;
    if (d.editedAfterDecision) text += " 你有新的修改还没确认。";
    status.textContent = text;
  }

  function renderSuperseded() {
    root.classList.add("is-superseded");
    policy.hidden = true;
    sections.replaceChildren();
    notes.hidden = true;
    rejectBox.hidden = true;
    actions.replaceChildren();
    status.textContent = d.decision ? `这一版你在 ${dayTime(d.decision.at)} 拒绝了。` : "这一版已被新版本取代。";
    superseded.hidden = false;
    superseded.replaceChildren(
      h("span", { text: `我已重写了第 ${(d.version || 1) + 1} 版。` }),
      btn("查看新版本", "btn btn-ghost btn-sm", () => ctx.revealEntry(d.supersededBy), "arrowDown")
    );
  }

  function render() {
    renderTags();
    if (d.supersededBy) {
      renderSuperseded();
      return;
    }
    renderSections();
    renderNotes();
    renderReject();
    renderActions();
    if (!busy) renderStatus();
  }

  function enterEdit() {
    editing = true;
    rejecting = false;
    hideError();
    render();
    if (inputs.body) inputs.body.focus();
  }

  function startReject() {
    rejecting = true;
    hideError();
    render();
    const area = rejectBox.querySelector("textarea");
    if (area) area.focus();
  }

  function finishEdit() {
    const next = { ...(d.edits || {}) };
    for (const [key, area] of Object.entries(inputs)) {
      if (area.value === (d.draft[key] || "")) delete next[key];
      else next[key] = area.value;
    }
    const changed = JSON.stringify(next) !== JSON.stringify(d.edits || {});
    d.edits = Object.keys(next).length ? next : null;
    if (changed && d.decision) d.editedAfterDecision = true;
    editing = false;
    save();
    render();
    if (changed) announce("修改已保存在卡片里，确认后生效");
    const first = actions.querySelector("button");
    if (first) first.focus();
  }

  function cancelEdit() {
    editing = false;
    render();
    const first = actions.querySelector("button");
    if (first) first.focus();
  }

  function editsPayload() {
    if (!d.edits) return null;
    const out = {};
    for (const key of ["title", "body"]) {
      if (d.edits[key] != null && d.edits[key] !== (d.draft[key] || "")) out[key] = d.edits[key];
    }
    return Object.keys(out).length ? out : null;
  }

  async function decide(confirmed, reason = "") {
    if (busy) return;
    const request = ctx.beginDecision();
    hideError();
    rejecting = false;
    render();
    setBusy(true, confirmed ? "正在定稿…" : reason ? "好的，我按你的意见重写…" : "好的，正在处理…");
    let res;
    try {
      res = await api.confirm(d.threadId, confirmed, { edits: confirmed ? editsPayload() : null, reason });
    } catch (err) {
      setBusy(false);
      if (!ctx.decisionIsCurrent(request)) return;
      render();
      showError(friendlyError(err, "confirm").text, debugText(err));
      return;
    }
    setBusy(false);
    if (!ctx.decisionIsCurrent(request)) return;
    applyResult(res, confirmed, reason, request);
  }

  function applyResult(res, confirmed, reason, request) {
    const data = res.data || {};
    const dec = data.decision || {};
    // A redraft shares its thread with the version it replaced, so the server flags its first decision as an override.
    const decision = {
      confirmed,
      at: dec.decided_at || new Date().toISOString(),
      overridden: !!dec.overridden && (!!d.decision || !d.basisDecisionAt),
      channel: "web",
      reason: reason || "",
    };
    if (!confirmed && (res.status === "awaiting_confirm" || res.awaiting_confirm)) {
      d.decision = decision;
      d.supersededBy = ctx.onRedraft(entry, res, request);
      save();
      render();
      announce("好，这版不用。我重写了一版，在下面");
      return;
    }
    if (res.status === "confirmed") decision.confirmed = true;
    if (res.status === "rejected") decision.confirmed = false;
    d.decision = decision;
    if (decision.confirmed) d.editedAfterDecision = false;
    const nd = data.draft || {};
    if (nd.body || nd.title) {
      d.draft = {
        ...d.draft,
        kind: nd.kind || d.draft.kind,
        title: nd.title != null ? nd.title : d.draft.title,
        body: nd.body || d.draft.body,
        notes: Array.isArray(nd.notes) ? nd.notes : d.draft.notes,
      };
      // The server already folded accepted edits into the final text.
      if (decision.confirmed) d.edits = null;
    }
    save();
    render();
    ctx.onDecided(entry, res);
    announce(decision.confirmed ? "已定稿" : "已拒绝这份草稿");
  }

  render();
  return root;
}
