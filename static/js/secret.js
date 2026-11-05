import { h, icon, nextId, announce } from "./core.js?v=__APP_VERSION__";
import { api, ApiError, friendlyError } from "./api.js?v=__APP_VERSION__";

/*
 * Secret sandbox: a modal outside the chat thread. Values live only in these inputs
 * and the single POST to /api/secrets — never in the transcript store, the composer,
 * the chat request or the console. Inputs are wiped on every close.
 */

let dialog = null;

function saveError(err) {
  if (err instanceof ApiError) {
    if (err.status === 401) return "会话已失效，刷新页面后才能安全保存。这次没有保存任何内容。";
    if (err.status === 503) return "安全保存暂未开启，这次没有保存任何内容。";
    if (err.status === 400 && /[\u4e00-\u9fff]/.test(err.message || "")) return `${err.message.replace(/[。.]$/, "")}。`;
  }
  return friendlyError(err, "secret").text;
}

function fieldRow(field) {
  const id = nextId("sf");
  const masked = field.input === "password";
  const input = h("input", {
    class: "field-input secret-input",
    id,
    name: field.name,
    type: masked ? "password" : "text",
    autocomplete: field.autocomplete || "off",
    spellcheck: "false",
    autocapitalize: "off",
    autocorrect: "off",
    "data-lpignore": "true",
    "data-1p-ignore": "true",
    dataset: masked ? { masked: "1" } : null,
  });
  const children = [input];
  if (masked) {
    const toggle = h("button", {
      type: "button",
      class: "btn-icon btn-icon-sm secret-eye",
      "aria-label": `显示${field.label}`,
      "aria-pressed": "false",
      "aria-controls": id,
    }, [icon("eye")]);
    toggle.addEventListener("click", () => {
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      toggle.setAttribute("aria-pressed", String(show));
      toggle.setAttribute("aria-label", `${show ? "隐藏" : "显示"}${field.label}`);
      toggle.replaceChildren(icon(show ? "eyeOff" : "eye"));
      input.focus();
    });
    children.push(toggle);
  }
  return h("div", { class: "secret-field" }, [
    h("label", { class: "dw-field-label", for: id, text: field.label }),
    h("div", { class: "secret-input-wrap" }, children),
  ]);
}

function wipe(form) {
  form.querySelectorAll("input").forEach((input) => {
    input.value = "";
    if (input.dataset.masked) input.type = "password";
  });
}

/** Open the sandbox for a secret card. Resolves to the saved item, or null if cancelled. */
export function openSecretSandbox(card) {
  if (dialog) dialog.remove();
  const titleId = nextId("st");
  const descId = nextId("sd");
  const errorNode = h("p", { class: "secret-error", role: "alert", hidden: true });
  const submit = h("button", { type: "submit", class: "btn btn-primary btn-sm", text: "加密保存" });
  const cancel = h("button", { type: "button", class: "btn btn-quiet btn-sm", text: "取消" });
  const form = h("form", { class: "secret-form", autocomplete: "off", novalidate: true }, [
    h("div", { class: "secret-head" }, [
      h("span", { class: "secret-lock", "aria-hidden": "true" }, [icon("lock")]),
      h("div", { class: "secret-head-text" }, [
        h("h2", { id: titleId, text: "安全输入区" }),
        h("p", { class: "secret-platform", text: card.label || card.platform || "" }),
      ]),
    ]),
    h("div", { class: "secret-purpose", id: descId }, [
      h("span", { class: "card-group-label", text: "用途" }),
      h("span", { text: card.purpose || "仅在你授权的操作里使用" }),
    ]),
    h("div", { class: "secret-fields" }, (card.fields || []).map(fieldRow)),
    h("ul", { class: "secret-promise" }, [
      h("li", { text: "只加密保存在服务端，聊天记录、AI 模型和日志里都不会出现。" }),
      h("li", { text: "保存后只显示脱敏信息，随时可以在「档案 › 安全保存的信息」里删除。" }),
    ]),
    errorNode,
    h("div", { class: "secret-actions" }, [cancel, submit]),
  ]);
  dialog = h("dialog", { class: "secret-dialog", "aria-labelledby": titleId, "aria-describedby": descId }, [form]);
  document.body.append(dialog);

  return new Promise((resolve) => {
    let result = null;
    const close = () => {
      wipe(form);
      dialog.close();
    };
    cancel.addEventListener("click", close);
    dialog.addEventListener("cancel", () => wipe(form));
    dialog.addEventListener("close", () => {
      wipe(form);
      dialog.remove();
      dialog = null;
      resolve(result);
    });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const values = {};
      form.querySelectorAll("input.secret-input").forEach((input) => {
        if (input.value.trim()) values[input.name] = input.value;
      });
      if (!Object.keys(values).length) {
        errorNode.textContent = "先填写内容再保存。";
        errorNode.hidden = false;
        form.querySelector("input").focus();
        return;
      }
      submit.disabled = true;
      submit.classList.add("is-loading");
      errorNode.hidden = true;
      try {
        const res = await api.saveSecret(card.key, values, card.purpose || "");
        result = { item: (res.data && res.data.item) || null, message: (res.data && res.data.message) || "" };
        announce(result.message || "已安全保存");
        close();
      } catch (err) {
        submit.disabled = false;
        submit.classList.remove("is-loading");
        errorNode.textContent = saveError(err);
        errorNode.hidden = false;
      }
    });
    dialog.showModal();
    const first = form.querySelector("input");
    if (first) first.focus();
  });
}
