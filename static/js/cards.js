import { h, icon, nextId, dayTime, debugBlock } from "./core.js?v=__APP_VERSION__";
import { cleanReminderLine, stripTimeZone, memoryText, kindLabel } from "./text.js?v=__APP_VERSION__";

export function cardShell(kind, { title, sub = "", tags = [], restored = false, label = "" } = {}) {
  const titleId = nextId("t");
  const head = h("div", { class: "card-head" }, [
    h("h3", { id: titleId, text: title }),
    tags.filter(Boolean).length ? h("div", { class: "card-tags" }, tags) : null,
  ]);
  const root = h(
    "section",
    { class: `card card-${kind}${restored ? "" : " enter"}`, "aria-labelledby": titleId, "aria-label": label || null },
    [head, sub ? h("p", { class: "card-sub", text: sub }) : null]
  );
  return { root, head, titleId };
}

function tag(text, cls = "") {
  return h("span", { class: `tag${cls ? ` ${cls}` : ""}`, text });
}

function note(text = "", tone = "") {
  return h("p", { class: `card-note${tone ? ` is-${tone}` : ""}`, text, hidden: !text });
}

function setNote(node, text, tone = "") {
  node.textContent = text;
  node.className = `card-note${tone ? ` is-${tone}` : ""}`;
  node.hidden = !text;
}

/* ---------------- Proactive reminder ---------------- */

export function proactiveCard(entry, ctx) {
  const item = entry.data.item || {};
  const title = cleanReminderLine(stripTimeZone(item.title)) || "主动提醒";
  const { root } = cardShell("proactive", { title, tags: [tag(kindLabel(item.kind) === "记忆" ? "主动提醒" : kindLabel(item.kind), "tag-amber")], restored: ctx.restored });
  const lines = String(item.body || "")
    .split("\n")
    .map((s) => cleanReminderLine(s.replace(/^·\s*/, "")))
    .filter(Boolean);
  if (lines.length) root.append(h("ul", { class: "card-list" }, lines.map((s) => h("li", { text: s }))));
  if (item.created_at) root.append(h("p", { class: "card-foot", text: `提醒时间：${dayTime(item.created_at)}` }));
  if (entry.data.dismissed) {
    root.append(h("p", { class: "card-foot", text: "已标记为知道了。" }));
  } else {
    const inline = note();
    root.append(
      h("div", { class: "card-actions" }, [
        h("button", { type: "button", class: "btn btn-ghost btn-sm", text: "知道了", onClick: (e) => ctx.dismissProactive(entry, e.currentTarget, (t, tone) => setNote(inline, t, tone)) }),
      ]),
      inline
    );
  }
  return root;
}

/* ---------------- Budget: continue card ---------------- */

function cny(value) {
  const n = Number(value);
  return Number.isFinite(n) ? `¥${n.toFixed(n < 1 ? 3 : 2)}` : "";
}

export function continueCard(entry, ctx) {
  const d = entry.data || {};
  const card = d.card || {};
  const capped = card.kind === "cost_cap";
  const { root } = cardShell("continue", {
    title: capped ? "今天的用量到上限了" : "这一步先停在这里",
    tags: [d.result ? tag(d.result === "continued" ? "已继续" : "已停止", d.result === "continued" ? "tag-ok" : "") : tag(card.tier_zh || "预算", "tag-ink")],
    restored: ctx.restored,
  });
  if (card.reason) root.append(h("p", { class: "card-sub", text: String(card.reason) }));
  const done = (card.done || []).map(String).filter(Boolean);
  const todo = (card.todo || []).map(String).filter(Boolean);
  if (done.length) root.append(h("div", { class: "card-group" }, [h("span", { class: "card-group-label", text: "已完成" }), h("ul", { class: "card-list" }, done.map((t) => h("li", { text: t })))]));
  if (todo.length) root.append(h("div", { class: "card-group" }, [h("span", { class: "card-group-label", text: "还没做" }), h("ul", { class: "card-list" }, todo.map((t) => h("li", { text: t })))]));
  const spent = card.spent || {};
  const ext = card.extension || {};
  const spentText = [spent.steps ? `${spent.steps} 步` : "", cny(spent.cost_cny) ? `约 ${cny(spent.cost_cny)}` : ""].filter(Boolean).join(" · ");
  if (spentText) root.append(h("p", { class: "card-foot", text: `已用：${spentText}` }));
  if (!capped && ext.steps) {
    root.append(h("p", { class: "card-foot", text: `继续会再给 ${ext.steps} 步${cny(card.estimate_cny) ? `，预计 ${cny(card.estimate_cny)}` : ""}。` }));
  }
  if (capped && card.resume_after) root.append(h("p", { class: "card-foot", text: `${dayTime(card.resume_after)} 之后可以继续。` }));
  if (d.result) {
    root.append(h("p", { class: "card-foot", text: d.result === "continued" ? "已继续处理，结果在下面。" : "好，先停在这里。" }));
    return root;
  }
  const inline = note();
  const canContinue = card.can_continue !== false && !capped;
  root.append(
    h("div", { class: "card-actions" }, [
      canContinue
        ? h("button", { type: "button", class: "btn btn-primary btn-sm", text: "继续", onClick: (e) => ctx.continueTask(entry, e.currentTarget, (t, tone) => setNote(inline, t, tone)) })
        : null,
      h("button", { type: "button", class: canContinue ? "btn btn-quiet btn-sm" : "btn btn-ghost btn-sm", text: "先这样", onClick: () => ctx.stopTask(entry) }),
    ]),
    inline
  );
  const raw = debugBlock("continue_card", card);
  if (raw) root.append(raw);
  return root;
}

/* ---------------- Question card (single / multi / text) ---------------- */

const ASK_TAGS = { single: "单选", multi: "多选", text: "补充说明" };

export function askCard(entry, ctx) {
  const d = entry.data;
  const card = d.card || {};
  const answer = d.answer;
  const multi = card.type === "multi";
  const options = card.type === "text" ? [] : card.options || [];
  const hint = multi && card.max_select ? `最多选 ${card.max_select} 项` : ASK_TAGS[card.type] || "";
  const { root, titleId } = cardShell("ask", {
    title: card.question || "",
    tags: [answer ? tag("已回答", "tag-ok") : tag(hint)],
    restored: ctx.restored,
  });
  if (answer) root.classList.add("is-answered");
  const name = `${entry.id}-opt`;
  const inputs = [];
  const group = options.length
    ? h(
        "div",
        { class: "ask-options", role: multi ? "group" : "radiogroup", "aria-labelledby": titleId },
        options.map((label) => {
          const picked = !!answer && (answer.selected || []).includes(label);
          const input = h("input", {
            type: multi ? "checkbox" : "radio",
            name,
            value: label,
            class: "ask-input",
            checked: picked,
            disabled: !!answer,
          });
          inputs.push(input);
          return h("label", { class: `ask-opt${picked ? " is-picked" : ""}` }, [
            input,
            h("span", { class: "ask-mark", "aria-hidden": "true" }, [icon("check")]),
            h("span", { class: "ask-label", text: label }),
          ]);
        })
      )
    : null;
  if (group) root.append(group);

  const wantsText = card.type === "text" || card.allow_text;
  if (answer) {
    if (answer.text) {
      root.append(
        h("div", { class: "ask-answer-text" }, [
          h("span", { class: "card-group-label", text: card.type === "text" ? "你的回答" : "补充说明" }),
          h("p", { text: answer.text }),
        ])
      );
    }
    root.append(h("p", { class: "card-foot", text: answer.at ? `已回答 · ${dayTime(answer.at)}` : "已回答" }));
    return root;
  }

  let area = null;
  if (wantsText) {
    const id = `${entry.id}-text`;
    area = h("textarea", {
      class: "field-input ask-text",
      id,
      rows: card.type === "text" ? 3 : 2,
      maxlength: "500",
      placeholder: card.placeholder || (card.type === "text" ? "写下你的想法…" : "没有合适的选项？写在这里"),
    });
    root.append(
      h("div", { class: "ask-text-wrap" }, [
        h("label", { class: "dw-field-label", for: id, text: card.text_label || (card.type === "text" ? "你的回答" : "补充说明（选填）") }),
        area,
      ])
    );
  }
  const submit = h("button", { type: "button", class: "btn btn-primary btn-sm", text: "提交", disabled: true });
  const picked = () => inputs.filter((i) => i.checked).map((i) => i.value);
  const sync = () => {
    const sel = picked();
    if (multi && card.max_select) {
      inputs.forEach((i) => {
        i.disabled = !i.checked && sel.length >= card.max_select;
      });
    }
    inputs.forEach((i) => i.closest(".ask-opt").classList.toggle("is-picked", i.checked));
    submit.disabled = !(sel.length || (area && area.value.trim()));
  };
  inputs.forEach((i) => i.addEventListener("change", sync));
  if (area) {
    area.addEventListener("input", sync);
    area.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !submit.disabled) submit.click();
    });
  }
  submit.addEventListener("click", () => {
    const answerData = { selected: picked(), text: area ? area.value.trim() : "" };
    if (!answerData.selected.length && !answerData.text) return;
    submit.disabled = true;
    ctx.answerAsk(entry, answerData);
  });
  root.append(h("div", { class: "card-actions" }, [submit]));
  return root;
}

/* ---------------- Secret sandbox entry ---------------- */

export function secretCard(entry, ctx) {
  const d = entry.data;
  const card = d.card || {};
  const { root } = cardShell("secret", {
    title: `安全输入 · ${card.label || card.platform || "敏感信息"}`,
    tags: [d.saved ? tag("已加密保存", "tag-ok") : tag("不进聊天记录", "tag-ink")],
    restored: ctx.restored,
  });
  if (d.saved) {
    root.append(
      h("p", { class: "card-text strong secret-saved" }, [icon("check"), h("span", { text: d.saved.message || `已安全保存 ${card.label}` })]),
      h("div", { class: "card-actions" }, [
        h("button", { type: "button", class: "btn btn-quiet btn-sm", text: "查看或删除", onClick: () => ctx.openSecrets() }),
      ])
    );
    if (d.saved.at) root.append(h("p", { class: "card-foot", text: dayTime(d.saved.at) }));
    return root;
  }
  root.append(
    h("div", { class: "secret-purpose" }, [h("span", { class: "card-group-label", text: "用途" }), h("span", { text: card.purpose || "" })]),
    h("p", {
      class: "card-sub",
      text: "内容在单独的输入区里填写，加密保存在服务端；聊天记录、AI 模型和日志里都不会出现。",
    })
  );
  if (d.declined) {
    root.append(
      h("p", { class: "card-foot", text: "已跳过，没有保存任何信息。" }),
      h("div", { class: "card-actions" }, [
        h("button", { type: "button", class: "btn btn-ghost btn-sm", text: "改为填写", onClick: () => ctx.openSecret(entry) }),
      ])
    );
    return root;
  }
  root.append(
    h("div", { class: "card-actions" }, [
      h("button", { type: "button", class: "btn btn-primary btn-sm", onClick: () => ctx.openSecret(entry) }, [icon("lock"), "打开安全输入区"]),
      h("button", { type: "button", class: "btn btn-quiet btn-sm", text: "暂不提供", onClick: () => ctx.declineSecret(entry) }),
    ])
  );
  return root;
}

/* ---------------- Memory card ---------------- */

function memoryShown(item, kind) {
  if (kind !== "episode") return item.value || "";
  const nice = memoryText({ kind: item.kind, summary: item.value, payload: {} });
  if (!nice || nice === kindLabel(item.kind) || nice === "记下了这轮对话的要点。") return item.value || "";
  return nice;
}

export function memoryCard(entry, ctx) {
  const d = entry.data || {};
  const titleId = nextId("t");
  const root = h("section", {
    class: `card card-memory${ctx.restored ? "" : " enter"}`,
    "aria-labelledby": titleId,
  });
  const body = h("div", { class: "cc-body" });
  root.append(
    h("div", { class: "cc-band" }, [h("h3", { id: titleId, text: "我记得的" })]),
    body
  );

  let editing = null;
  const facts = d.facts || [];
  const episodes = d.episodes || [];

  function group(title, items, kind) {
    return h("div", { class: "mem-group" }, [
      h("p", { class: "mem-group-label", text: title }),
      h("ul", { class: "mem-list" }, items.map((item) => row(item, kind))),
    ]);
  }

  function row(item, kind) {
    const shown = memoryShown(item, kind);
    const isEdit = editing && editing.kind === kind && String(editing.id) === String(item.id);
    const main = h("div", { class: "mem-row-main" }, [h("span", { class: "mem-k", text: item.label || "记忆" })]);
    if (isEdit) {
      const long = shown.length > 48 || shown.includes("\n");
      const field = long
        ? h("textarea", { class: "field-input mem-edit-input", rows: "3", value: shown })
        : h("input", { class: "field-input mem-edit-input", type: "text", value: shown });
      main.append(field);
      return h("li", { class: "mem-row is-editing" }, [
        main,
        h("div", { class: "mem-row-actions" }, [
          h("button", {
            type: "button",
            class: "btn btn-primary btn-sm",
            text: "保存",
            onClick: () => {
              const value = field.value.trim();
              if (!value) return;
              if (kind === "fact") ctx.saveMemoryFact(entry, item.id, value);
              else ctx.saveMemoryEvent(entry, item.id, value);
            },
          }),
          h("button", {
            type: "button",
            class: "btn btn-quiet btn-sm",
            text: "取消",
            onClick: () => {
              editing = null;
              paint();
            },
          }),
        ]),
      ]);
    }
    main.append(h("p", { class: "mem-v", text: shown }));
    return h("li", { class: "mem-row" }, [
      main,
      h("div", { class: "mem-row-actions" }, [
        h("button", {
          type: "button",
          class: "btn btn-ghost btn-sm",
          text: "编辑",
          "aria-label": `编辑${item.label || "记忆"}：${shown.slice(0, 24)}`,
          onClick: () => {
            editing = { kind, id: item.id };
            paint();
            const input = root.querySelector(".mem-edit-input");
            if (input) input.focus();
          },
        }),
        h("button", {
          type: "button",
          class: "btn btn-quiet btn-sm",
          text: "删除",
          "aria-label": `删除${item.label || "记忆"}：${shown.slice(0, 24)}`,
          onClick: () => {
            if (kind === "fact") ctx.deleteMemoryFact(entry, item.id);
            else ctx.deleteMemoryEvent(entry, item.id);
          },
        }),
      ]),
    ]);
  }

  function forgetBlock() {
    if (d.confirmingForget) {
      return h("div", { class: "mem-forget", role: "group", "aria-label": "确认忘掉全部记忆" }, [
        h("p", { text: "忘掉全部记忆？档案要点和对话记忆都会清掉，不能恢复。" }),
        h("div", { class: "card-actions" }, [
          h("button", {
            type: "button",
            class: "btn btn-primary btn-sm",
            text: "确认忘掉",
            onClick: () => ctx.forgetMemory(entry),
          }),
          h("button", {
            type: "button",
            class: "btn btn-quiet btn-sm",
            text: "取消",
            onClick: () => ctx.armForget(entry, false),
          }),
        ]),
      ]);
    }
    if (!facts.length && !episodes.length) return null;
    return h("div", { class: "card-actions" }, [
      h("button", {
        type: "button",
        class: "btn btn-quiet btn-sm",
        text: "忘掉全部",
        onClick: () => ctx.armForget(entry, true),
      }),
    ]);
  }

  function paint() {
    body.replaceChildren(
      h("p", {
        class: "cc-policy",
        text: "档案要点和近期对话记忆。密码、手机号这类敏感信息不会出现在这里。",
      })
    );
    if (d.error) body.append(h("div", { class: "card-error", role: "alert", text: d.error }));
    if (!facts.length && !episodes.length) {
      body.append(
        h("p", {
          class: "card-text",
          text: d.forgotten
            ? "已经全部忘掉了。之后的对话会重新开始记。"
            : "还没有可显示的记忆。聊几句，告诉我你的名字、城市或者最近在忙什么，我会记在这里。",
        })
      );
    } else {
      if (facts.length) body.append(group("档案要点", facts, "fact"));
      if (episodes.length) body.append(group("近期记忆", episodes, "episode"));
    }
    const forget = forgetBlock();
    if (forget) body.append(forget);
  }

  paint();
  return root;
}
