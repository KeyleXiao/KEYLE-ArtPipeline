/**
 * 统一路径输入：手动填写 + 文件夹/文件选择 + 合法性校验
 */
import { API } from "./api.js";
import { t } from "./i18n.js";
import { withBtnBusy } from "./effects.js";

const FOLDER_SVG = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>`;
const FILE_SVG = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M10 13h4"/><path d="M10 17h7"/></svg>`;

const enhanced = new WeakSet();
const validateTimers = new WeakMap();

function pathKind(input) {
  return input.dataset.pathKind === "file" ? "file" : "dir";
}

function pathBase(input) {
  const b = input.dataset.pathBase || "absolute";
  return b === "art" || b === "project" ? b : "absolute";
}

function isOptional(input) {
  return input.dataset.pathOptional !== undefined;
}

function skipValidate(input) {
  const v = (input.value || "").trim();
  if (v === "$asset") return true;
  if (input.readOnly && !input.dataset.pathField) return true;
  return false;
}

function setFieldState(wrap, state, message = "") {
  wrap.classList.remove("is-valid", "is-warning", "is-invalid");
  const hint = wrap.querySelector(".path-field-hint");
  if (state) wrap.classList.add(state);
  if (hint) {
    if (message) {
      hint.textContent = message;
      hint.hidden = false;
    } else {
      hint.textContent = "";
      hint.hidden = true;
    }
  }
}

export async function validatePathInput(input, { quiet = false } = {}) {
  const wrap = input.closest(".path-field");
  if (!wrap || skipValidate(input)) {
    if (wrap) setFieldState(wrap, "", "");
    return { valid: true };
  }
  const raw = (input.value || "").trim();
  if (!raw) {
    if (isOptional(input)) {
      setFieldState(wrap, "", "");
      return { valid: true, empty: true };
    }
    const msg = t("path.invalidEmpty");
    setFieldState(wrap, "is-invalid", quiet ? "" : msg);
    return { valid: false, empty: true, message: msg };
  }
  try {
    const r = await API.post("/api/validate-path", {
      path: raw,
      kind: pathKind(input),
      base: pathBase(input),
      optional: isOptional(input),
    });
    if (r.valid) {
      setFieldState(wrap, r.exists === false ? "is-warning" : "is-valid", r.exists === false && !quiet ? t("path.notExistsYet") : "");
      return r;
    }
    const msg = r.message || t("path.invalid");
    setFieldState(wrap, "is-invalid", quiet ? "" : msg);
    return r;
  } catch (err) {
    const msg = err?.message || t("path.invalid");
    setFieldState(wrap, "is-invalid", quiet ? "" : msg);
    return { valid: false, message: msg };
  }
}

function scheduleValidate(input) {
  clearTimeout(validateTimers.get(input));
  validateTimers.set(
    input,
    setTimeout(() => {
      void validatePathInput(input, { quiet: true });
    }, 420),
  );
}

function pickedAbsolutePath(r) {
  return (r?.path || r?.absolute || "").trim();
}

async function pickPathForInput(input, btn) {
  const kind = pathKind(input);
  const base = pathBase(input);
  await withBtnBusy(btn, async () => {
    let picked;
    if (kind === "file") {
      const r = await API.post("/api/pick-image-file", {
        initial_dir: input.value.trim() || undefined,
      });
      if (r.cancelled) return;
      picked = pickedAbsolutePath(r);
    } else {
      const r = await API.post("/api/pick-output-dir", {
        initial_dir: input.value.trim() || undefined,
        title: t("path.pickDirTitle"),
        relative_base: base === "absolute" ? "none" : base,
      });
      if (r.cancelled) return;
      picked = pickedAbsolutePath(r);
    }
    if (!picked) return;
    input.value = picked;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new CustomEvent("pathpicked", { bubbles: true, detail: { path: picked } }));
    await validatePathInput(input);
  }).catch(() => {
    /* withBtnBusy */
  });
}

export function enhancePathInput(input) {
  if (!input || enhanced.has(input)) return input;
  if (input.type === "hidden" || input.type === "checkbox") return input;
  enhanced.add(input);

  const kind = pathKind(input);
  const wrap = document.createElement("div");
  wrap.className = "path-field";
  input.classList.add("path-field-input");
  input.parentNode.insertBefore(wrap, input);
  wrap.appendChild(input);

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "path-field-btn";
  btn.innerHTML = kind === "file" ? FILE_SVG : FOLDER_SVG;
  btn.title = kind === "file" ? t("path.pickFile") : t("path.pickDir");
  btn.setAttribute("aria-label", btn.title);
  wrap.appendChild(btn);

  const hint = document.createElement("span");
  hint.className = "path-field-hint muted sm";
  hint.hidden = true;
  wrap.insertAdjacentElement("afterend", hint);

  btn.addEventListener("click", () => {
    if (input.disabled || input.readOnly) return;
    void pickPathForInput(input, btn);
  });

  input.addEventListener("input", () => scheduleValidate(input));
  input.addEventListener("blur", () => {
    void validatePathInput(input, { quiet: true });
  });

  const obs = new MutationObserver(() => {
    btn.disabled = input.disabled || input.readOnly;
  });
  obs.observe(input, { attributes: true, attributeFilter: ["disabled", "readonly"] });
  btn.disabled = input.disabled || input.readOnly;

  return input;
}

/** 初始化容器内所有带 data-path-field 的输入框 */
export function initPathFields(root = document) {
  root.querySelectorAll("input[data-path-field]").forEach((el) => enhancePathInput(el));
}

/** 保存前批量校验；返回首个错误信息或 null */
export async function validatePathFields(root = document) {
  const inputs = [...root.querySelectorAll("input[data-path-field]")];
  for (const input of inputs) {
    if (input.disabled || (input.readOnly && skipValidate(input))) continue;
    const r = await validatePathInput(input);
    if (!r.valid) return r.message || t("path.invalid");
  }
  return null;
}
