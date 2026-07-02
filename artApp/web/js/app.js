/**
 * ArtPipeline Studio · Web 前端
 */
import { API } from "./api.js";
import {
  initI18n,
  t,
  applyDomI18n,
  bindLangSwitcher,
  onLangChange,
  getLang,
} from "./i18n.js";
import {
  bindRipple,
  closeAllFloatingMenus,
  closeFloatingMenu,
  hideGlobalOverlay,
  hideSplash,
  openFloatingMenu,
  setBtnBusy,
  setPanelLoading,
  showGlobalOverlay,
  withBtnBusy,
} from "./effects.js";
import { closeLogDrawer, initLogPanel } from "./log-panel.js";
import { initPathFields, validatePathFields } from "./path-input.js";

const state = {
  categories: [],
  categoryId: null,
  assets: [],
  assetId: null,
  assetFull: null,
  previewSource: "inbox",
  statusMap: {},
  statusScanning: false,
  searchTimer: null,
  checkpoints: [],
  cloudModels: [],
  cloudGenModes: {},
  jobTimer: null,
  jobRunId: null,
  jobTrack: { active: false, sawBusy: false, postOk: false, submittedAt: 0 },
  paths: null,
  apiPortals: {},
  previewReq: 0,
  previewBlobUrl: null,
  previewInfo: null,
  previewInfoReq: 0,
  aiMode: "free",
  aiBusy: false,
  aiChatHistory: [],
  workflowPresets: [],
  /** 资源列表多选（顺序与当前列表一致） */
  selectedAssetIds: [],
  /** Shift 范围选择的锚点 */
  lastAssetClickId: null,
};

let genModeHydrating = false;

const previewView = {
  zoom: 1,
  panX: 0,
  panY: 0,
  imgW: 0,
  imgH: 0,
  minZoom: 0.08,
  maxZoom: 12,
  panning: null,
};

let appBooted = false;
/** 上次 ComfyUI 在线状态；用于检测离线→在线后刷新本地 checkpoint 列表 */
let comfyOnlineLast = null;

const AI_MODE_KEYS = ["free", "prompt", "refine", "workflow", "basic", "category"];
const AI_INTRO_DISMISSED_KEY = "aiIntroDismissed";
/** 各资源进行中的 AI 请求数（切换资源后会话独立，互不影响 UI） */
const aiPendingCounts = new Map();
/** 资源切换序号，用于丢弃过期的表单/预览加载 */
let assetLoadSeq = 0;

function isStaleAssetLoad(loadSeq, assetId = state.assetId) {
  return loadSeq !== assetLoadSeq || !assetId || state.assetId !== assetId;
}
let aiIntroAutoHideTimer = null;
let aiIntroAutoHideScheduled = false;
let aiIntroDismissedMemory = false;
const CLOUD_PROVIDER_TAB_ORDER = ["dashscope", "tencent", "volcengine", "stability"];

function aiModeText(mode, field) {
  return t(`ai.${field}.${mode}`);
}

function pathChipTips(kind, chipState) {
  const key = kind === "unity" ? "engine" : kind;
  return t(`path.chip.${key}.${chipState}`);
}

function previewSourceLabel() {
  const key = currentPreviewSourceKey();
  if (key === "unity") return t("path.engine");
  return key;
}

function currentPreviewSourceKey() {
  const active = document.querySelector(".preview-side .seg-btn.active");
  const fromUi = active?.dataset?.source;
  if (fromUi) {
    state.previewSource = fromUi;
    return fromUi;
  }
  return state.previewSource || "inbox";
}

function refreshI18nUi() {
  renderCategories();
  renderAssets();
  fillCheckpointSelects();
  updateGenerationTabLabels();
  updateGenerationTabsVisibility();
  updateAiModeUi();
  refreshAiMessages();
  refreshComfy();
  if (state.assetId) {
    loadPreview();
    loadPreviewInfo(state.assetId);
  } else clearPreview();
  const pill = $("#job-pill");
  if (pill && !pill.dataset.busy) pill.textContent = t("job.ready");
  if (jobProg.visible) renderJobFloat();
  if (state.previewInfo) renderPreviewInfo(state.previewInfo);
  if (welcomeDialogData && $("#dlg-welcome")?.open) fillWelcomeReleaseNotes(welcomeDialogData);
}

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function esc(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function toast(msg, { variant = "info" } = {}) {
  const pill = $("#job-pill");
  if (pill) {
    const prev = pill.dataset.prev || pill.textContent;
    pill.dataset.prev = prev;
    pill.textContent = msg;
    pill.classList.toggle("is-error", variant === "error");
    clearTimeout(pill._toastTimer);
    pill._toastTimer = setTimeout(() => {
      if (pill.dataset.prev) pill.textContent = pill.dataset.prev;
      pill.classList.remove("is-error");
    }, 3500);
  }
}

function setFormError(form, msg) {
  if (!form) return;
  const el = form.querySelector(".form-error") || $("#new-asset-error");
  if (!el) return;
  if (msg) {
    el.textContent = msg;
    el.hidden = false;
  } else {
    el.textContent = "";
    el.hidden = true;
  }
}

function effectiveSelectedIds() {
  const visible = new Set(state.assets.map((a) => a.id));
  const sel = state.selectedAssetIds.filter((id) => visible.has(id));
  if (sel.length) return sel;
  return state.assetId && visible.has(state.assetId) ? [state.assetId] : [];
}

function selectedIds() {
  return effectiveSelectedIds();
}

function isAssetRowSelected(assetId) {
  if (state.selectedAssetIds.length) return state.selectedAssetIds.includes(assetId);
  return assetId === state.assetId;
}

function toggleAssetInSelection(assetId) {
  const ordered = state.assets.map((a) => a.id);
  const base =
    state.selectedAssetIds.length > 0
      ? state.selectedAssetIds
      : state.assetId
        ? [state.assetId]
        : [];
  const set = new Set(base);
  if (set.has(assetId)) {
    set.delete(assetId);
    if (!set.size) set.add(assetId);
  } else {
    set.add(assetId);
  }
  return ordered.filter((id) => set.has(id));
}

function handleAssetListClick(assetId, ev) {
  const ordered = state.assets.map((a) => a.id);
  const idx = ordered.indexOf(assetId);
  if (idx < 0) return;

  const shift = ev.shiftKey;
  const toggle = ev.metaKey || ev.ctrlKey;

  if (shift && state.lastAssetClickId) {
    const anchor = ordered.indexOf(state.lastAssetClickId);
    if (anchor >= 0) {
      const lo = Math.min(anchor, idx);
      const hi = Math.max(anchor, idx);
      const range = ordered.slice(lo, hi + 1);
      if (toggle) {
        const set = new Set(
          state.selectedAssetIds.length
            ? state.selectedAssetIds
            : state.assetId
              ? [state.assetId]
              : [],
        );
        for (const id of range) {
          if (set.has(id)) set.delete(id);
          else set.add(id);
        }
        state.selectedAssetIds = ordered.filter((id) => set.has(id));
        if (!state.selectedAssetIds.length) state.selectedAssetIds = [assetId];
      } else {
        state.selectedAssetIds = range;
      }
    } else {
      state.selectedAssetIds = toggle ? toggleAssetInSelection(assetId) : [assetId];
    }
  } else if (toggle) {
    state.selectedAssetIds = toggleAssetInSelection(assetId);
  } else {
    state.selectedAssetIds = [assetId];
  }
  state.lastAssetClickId = assetId;
}

function pruneAssetSelection() {
  const visible = new Set(state.assets.map((a) => a.id));
  state.selectedAssetIds = state.selectedAssetIds.filter((id) => visible.has(id));
  if (state.lastAssetClickId && !visible.has(state.lastAssetClickId)) {
    state.lastAssetClickId = state.selectedAssetIds[0] || state.assetId || null;
  }
}

function categoryAssetIds() {
  return state.assets.filter((a) => a.enabled !== false).map((a) => a.id);
}

async function enabledAssetsForCategory(catId) {
  if (catId === state.categoryId) {
    const assets = state.assets.filter((a) => a.enabled !== false);
    return { ids: assets.map((a) => a.id), assets };
  }
  const data = await API.get(`/api/assets?category=${encodeURIComponent(catId)}`);
  const assets = (data.assets || []).filter((a) => a.enabled !== false);
  return { ids: assets.map((a) => a.id), assets };
}

function effectiveCheckpointFromRow(row) {
  if (!row) return "";
  if (row.checkpoint_effective) return row.checkpoint_effective;
  if (row.checkpoint) return row.checkpoint;
  const cat = state.categories.find((c) => c.id === row.category);
  return cat?.checkpoint || "";
}

function assetUsesCloudBackendFromRow(row) {
  if (!row) return false;
  if (row.is_cloud_model === true) return true;
  if (row.is_cloud_model === false) return false;
  return isCloudCheckpoint(effectiveCheckpointFromRow(row));
}

function batchNeedsComfy(assetIds, assetRows = null) {
  const byId = new Map((assetRows || state.assets).map((a) => [a.id, a]));
  return (assetIds || []).some((id) => !assetUsesCloudBackendFromRow(byId.get(id)));
}

async function runGenerate(
  assetIds,
  { exportAfter = false, emptyToastKey = "toast.noCategoryAssets", assetRows = null } = {},
) {
  if (!assetIds?.length) {
    toast(t(emptyToastKey));
    return;
  }
  const rows = assetRows || state.assets;
  if (batchNeedsComfy(assetIds, rows) && !(await ensureComfyOnline())) return;
  if (state.assetId && assetIds.includes(state.assetId)) {
    if (assetUsesCloudBackend()) {
      await persistCloudAssetFields();
    } else {
      await saveGenModeOnly();
    }
  }
  markJobSubmitting("generate", assetIds);
  try {
    const res = await API.post("/api/generate", { asset_ids: assetIds, export_after: exportAfter });
    state.jobRunId = res.run_id ?? null;
    state.jobTrack.postOk = true;
    if (res.queue_position > 1) {
      toast(t("toast.jobQueued", { position: res.queue_position }));
    }
    ensureJobPoll();
  } catch (err) {
    jobProg.submitting = false;
    try {
      const j = await API.get("/api/jobs/status");
      if (j.busy || (j.queued_count || 0) > 0 || (j.queue || []).length > 0) {
        updateJobFromApi(j);
        ensureJobPoll();
      } else {
        resetJobTracking();
        clearJobUi();
      }
    } catch {
      resetJobTracking();
      clearJobUi();
    }
    toast(err.message);
  }
}

async function runExport(assetIds, { emptyToastKey = "toast.noCategoryAssets" } = {}) {
  if (!assetIds?.length) {
    toast(t(emptyToastKey));
    return;
  }
  markJobSubmitting("export", assetIds);
  try {
    const res = await API.post("/api/export", { asset_ids: assetIds });
    state.jobRunId = res.run_id ?? null;
    state.jobTrack.postOk = true;
    if (res.queue_position > 1) {
      toast(t("toast.jobQueued", { position: res.queue_position }));
    }
    ensureJobPoll();
  } catch (err) {
    jobProg.submitting = false;
    try {
      const j = await API.get("/api/jobs/status");
      if (j.busy || (j.queued_count || 0) > 0 || (j.queue || []).length > 0) {
        updateJobFromApi(j);
        ensureJobPoll();
      } else {
        resetJobTracking();
        clearJobUi();
      }
    } catch {
      resetJobTracking();
      clearJobUi();
    }
    toast(err.message);
  }
}

// ── 渲染 ─────────────────────────────────────────────

function renderCategories() {
  const ul = $("#cat-list");
  if (!ul) return;
  ul.innerHTML = "";
  for (const cat of state.categories) {
    const li = document.createElement("li");
    li.className = "cat-item" + (cat.id === state.categoryId ? " active" : "");
    li.dataset.catId = cat.id;
    li.innerHTML = `
      <div class="label">${esc(cat.label)}</div>
      <div class="sub">${esc(cat.id)} · ${esc(cat.checkpoint_short)}</div>`;
    li.addEventListener("click", () => selectCategory(cat.id));
    ul.appendChild(li);
  }
}

const PATH_KINDS = [
  { key: "source", label: "S", title: "source" },
  { key: "inbox", label: "in", title: "inbox" },
  { key: "unity", label: "U", title: "engine" },
];

function pathChipClass(st) {
  if (st === "ok") return "ok";
  if (st === "none") return "none";
  if (st === "modified") return "warn";
  return "bad";
}

function pathChipHtml(assetId, kind, label, st, scanning) {
  let chipState = "pending";
  if (!scanning && st?.state) chipState = st.state;
  const tipKey = kind === "unity" ? "engine" : kind;
  const tip =
    (pathChipTips(tipKey, chipState) || pathChipTips(tipKey, "pending")) +
    (st?.file ? `\n${st.file}` : "");
  const cls = scanning ? "pending" : pathChipClass(chipState);
  return `<button type="button" class="path-chip ${cls}" data-path-kind="${kind}" data-asset-id="${esc(assetId)}" title="${esc(tip)}">${label}</button>`;
}

function renderAssets() {
  const box = $("#asset-list");
  const countEl = $("#asset-count");
  if (!box) return;
  if (!state.categoryId) {
    box.innerHTML = `<p class="hint">${esc(t("asset.selectCategory"))}</p>`;
    if (countEl) countEl.textContent = "";
    return;
  }
  const selCount = effectiveSelectedIds().length;
  if (countEl) {
    if (selCount > 1) {
      countEl.textContent = state.statusScanning
        ? t("asset.countSelectedScanning", { n: state.assets.length, sel: selCount })
        : t("asset.countSelected", { n: state.assets.length, sel: selCount });
    } else {
      countEl.textContent = state.statusScanning
        ? t("asset.countScanning", { n: state.assets.length })
        : t("asset.count", { n: state.assets.length });
    }
  }
  const frag = document.createDocumentFragment();
  for (const asset of state.assets) {
    const row = document.createElement("div");
    const selected = isAssetRowSelected(asset.id);
    row.className =
      "asset-row" +
      (asset.id === state.assetId ? " active" : "") +
      (selected ? " selected" : "");
    row.dataset.id = asset.id;
    const st = state.statusMap[asset.id];
    const chips = PATH_KINDS.map(({ key, label }) =>
      pathChipHtml(asset.id, key, label, st?.[key], state.statusScanning),
    ).join("");
    row.innerHTML = `
      <span class="name" title="${esc(asset.filename)}">${esc(asset.filename)}</span>
      <span class="size">${esc(asset.size_label)}</span>
      <span class="path-chips" aria-label="${esc(t("asset.chipsAria"))}">${chips}</span>`;
    row.addEventListener("click", (ev) => {
      if (assetDrag.suppressClick) {
        assetDrag.suppressClick = false;
        ev.preventDefault();
        ev.stopPropagation();
        return;
      }
      handleAssetListClick(asset.id, ev);
      void selectAsset(asset.id, { keepSelection: true });
    });
    frag.appendChild(row);
  }
  box.innerHTML = "";
  box.appendChild(frag);
}

function isCloudCheckpoint(value) {
  return String(value || "").startsWith("cloud:");
}

function effectiveCheckpointForAsset(data = state.assetFull) {
  if (!data) return "";
  return data.checkpoint_effective || data.checkpoint || "";
}

function assetUsesCloudBackend(data = state.assetFull) {
  return isCloudCheckpoint(effectiveCheckpointForAsset(data));
}

function resolveCheckpointForAssetId(assetId) {
  if (assetId === state.assetId && state.assetFull) {
    return effectiveCheckpointForAsset(state.assetFull);
  }
  const row = state.assets.find((a) => a.id === assetId);
  return effectiveCheckpointFromRow(row);
}

function cloudProviderFromCheckpoint(checkpoint) {
  const ckpt = String(checkpoint || "").trim();
  if (!isCloudCheckpoint(ckpt)) return "";
  const model = (state.cloudModels || []).find((m) => m.id === ckpt);
  if (model?.provider) return model.provider;
  const rest = ckpt.slice("cloud:".length);
  return rest.split("/")[0] || "";
}

function effectiveGenTabId(data = state.assetFull) {
  const ckpt = effectiveCheckpointForAsset(data);
  if (!ckpt) return null;
  if (isCloudCheckpoint(ckpt)) {
    const provider = cloudProviderFromCheckpoint(ckpt);
    return provider ? `cloud-${provider}` : null;
  }
  return "comfyui";
}

function isGenerationTab(tabId) {
  return tabId === "comfyui" || String(tabId || "").startsWith("cloud-");
}

function tabBodyElementId(tabId) {
  if (String(tabId || "").startsWith("cloud-")) return "tab-cloud";
  return `tab-${tabId}`;
}

function providerTabLabel(providerId) {
  const lang = document.documentElement.lang || "zh-CN";
  const en = lang.startsWith("en");
  const model = (state.cloudModels || []).find((m) => m.provider === providerId);
  if (model) return en ? model.provider_label_en || providerId : model.provider_label_zh || providerId;
  return providerId;
}

function updateGenerationTabLabels() {
  const comfyTab = $('#main-tabs .tab[data-tab="comfyui"]');
  if (comfyTab) comfyTab.textContent = t("tab.comfyui");
  for (const pid of CLOUD_PROVIDER_TAB_ORDER) {
    const tab = $(`#main-tabs .tab[data-tab="cloud-${pid}"]`);
    if (tab) tab.textContent = providerTabLabel(pid);
  }
}

function updateGenerationTabsVisibility() {
  const genTabId = state.assetId ? effectiveGenTabId() : null;
  $$("#main-tabs .tab[data-gen-tab]").forEach((tab) => {
    tab.hidden = !genTabId || tab.dataset.tab !== genTabId;
  });
  updateGenerationTabLabels();
  const active = $("#main-tabs .tab.active");
  if (active?.hasAttribute("data-gen-tab") && active.hidden) {
    if (genTabId) switchTab(genTabId);
    else switchTab("basic");
  }
}

function cloudModelLabel(model) {
  const lang = document.documentElement.lang || "zh-CN";
  return lang.startsWith("en") ? model.label_en || model.id : model.label_zh || model.id;
}

function rebuildCheckpointSelect(sel, emptyLabel, preferred = "") {
  if (!sel) return;
  const value = (preferred || sel.value || "").trim();
  sel.innerHTML = "";
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = emptyLabel;
  sel.appendChild(empty);

  const addGroup = (label, items, getValue, getText) => {
    if (!items.length) return;
    const og = document.createElement("optgroup");
    og.label = label;
    for (const item of items) {
      const o = document.createElement("option");
      o.value = getValue(item);
      o.textContent = getText(item);
      og.appendChild(o);
    }
    sel.appendChild(og);
  };

  addGroup(
    t("form.checkpointGroupLocal"),
    state.checkpoints || [],
    (ck) => ck,
    (ck) => ck.split("/").pop(),
  );

  const globalCloud = (state.cloudModels || []).filter((m) => m.region === "global");
  const cnCloud = (state.cloudModels || []).filter((m) => m.region === "cn");
  addGroup(t("form.checkpointGroupCloudGlobal"), globalCloud, (m) => m.id, cloudModelLabel);
  addGroup(t("form.checkpointGroupCloudCn"), cnCloud, (m) => m.id, cloudModelLabel);

  if (value && ![...(state.checkpoints || []), ...(state.cloudModels || []).map((m) => m.id)].includes(value)) {
    const o = document.createElement("option");
    o.value = value;
    const short = value.split("/").pop().replace(/^cloud:/, "");
    o.textContent = `${short} (${t("dlg.checkpointOfflineHint")})`;
    sel.appendChild(o);
  }
  if (value) sel.value = value;
}

function cloudModelSummary(data = state.assetFull) {
  const ckpt = effectiveCheckpointForAsset(data);
  const model = (state.cloudModels || []).find((m) => m.id === ckpt);
  if (!model) return ckpt || "";
  const lang = document.documentElement.lang || "zh-CN";
  const summary = lang.startsWith("en") ? model.summary_en || model.label_en : model.summary_zh || model.label_zh;
  return summary || cloudModelLabel(model);
}

function updateCloudTabHero(data = state.assetFull) {
  const tab = $("#tab-cloud");
  const ckpt = effectiveCheckpointForAsset(data);
  const provider = cloudProviderFromCheckpoint(ckpt);
  if (tab) tab.dataset.provider = provider || "";
  const title = $("#cloud-tab-hero-title");
  const desc = $("#cloud-tab-hero-desc");
  const mark = $("#cloud-tab-mark");
  if (title) title.textContent = provider ? providerTabLabel(provider) : "—";
  if (desc) desc.textContent = cloudModelSummary(data);
  if (mark) {
    mark.className = "cloud-api-mark";
    if (provider) mark.classList.add(`cloud-api-mark--${provider}`);
  }
}

function currentCloudGenMode() {
  return document.querySelector('input[name="cloud_gen_mode"]:checked')?.value || "text_to_image";
}

function cloudGenModePayloadFromForm() {
  const mode = currentCloudGenMode();
  return {
    cloud_gen_mode: mode,
    ref_image: mode === "image_to_image" ? ($("#cloud-ref-image-path")?.value.trim() || "") : "",
    cloud_strength: parseFloat($("#cloud-strength")?.value) || 0.65,
  };
}

function updateCloudGenModeUi() {
  const mode = currentCloudGenMode();
  const panel = $("#cloud-ref-panel");
  const refSection = $("#cloud-ref-image-section");
  const editSection = $("#cloud-edit-source-section");
  const hint = $("#cloud-gen-ref-hint");
  const desc = $("#cloud-gen-mode-desc");
  panel?.classList.toggle("hidden", mode === "text_to_image");
  refSection?.classList.toggle("hidden", mode !== "image_to_image");
  editSection?.classList.toggle("hidden", mode !== "image_edit");
  if (mode === "image_edit") {
    const pathEl = $("#cloud-edit-source-path");
    const inbox = state.paths?.inbox || state.assetFull?.inbox_path || "";
    const src = refImageSourcePath();
    const display = inbox || src;
    if (pathEl) {
      pathEl.textContent = display || t("preview.infoFileMissing");
      pathEl.classList.toggle("is-missing", !display);
    }
  }
  const descKey =
    mode === "text_to_image"
      ? "form.genModeTextToImageDesc"
      : mode === "image_to_image"
        ? "form.genModeImageToImageDesc"
        : "form.genModeImageEditDesc";
  if (desc) desc.textContent = t(descKey);
  if (hint) {
    hint.textContent =
      mode === "image_edit" ? t("form.redrawHint") : mode === "image_to_image" ? t("form.img2imgHint") : "";
  }
}

function syncCloudStrengthFromRange() {
  const range = $("#cloud-strength-range");
  const num = $("#cloud-strength");
  if (range && num) num.value = range.value;
}

function syncCloudStrengthFromNumber() {
  const range = $("#cloud-strength-range");
  const num = $("#cloud-strength");
  if (!range || !num) return;
  let v = parseFloat(num.value);
  if (Number.isNaN(v)) v = 0.65;
  v = Math.max(0.01, Math.min(1, v));
  num.value = v;
  range.value = v;
}

async function saveCloudGenModeOnly() {
  if (!state.assetId || genModeHydrating) return;
  const payload = cloudGenModePayloadFromForm();
  const prev = state.assetFull;
  if (
    prev &&
    (prev.cloud_gen_mode || "text_to_image") === payload.cloud_gen_mode &&
    (prev.ref_image || "").trim() === payload.ref_image &&
    (prev.cloud_strength ?? 0.65) === payload.cloud_strength
  ) {
    return;
  }
  try {
    state.assetFull = await API.put(`/api/assets/${state.assetId}`, payload);
    updateCloudGenModeUi();
  } catch (err) {
    toast(err.message);
  }
}

async function persistCloudAssetFields() {
  if (!state.assetId || genModeHydrating) return;
  const body = {
    cloud_prompt: $("#cloud-prompt-text")?.value || "",
    cloud_negative: $("#cloud-negative-text")?.value || "",
    ...cloudGenModePayloadFromForm(),
  };
  state.assetFull = await API.put(`/api/assets/${state.assetId}`, body);
  updateCloudGenModeUi();
}

async function saveCloudPrompts(btn) {
  if (!state.assetId) return;
  await withBtnBusy(btn || document.querySelector('[data-action="save-cloud-prompts"]'), async () => {
    await persistCloudAssetFields();
    toast(t("toast.promptsSaved"));
  }).catch((err) => {
    if (err) toast(err.message);
  });
}

function fillCheckpointSelects() {
  const cat = state.categories.find((c) => c.id === state.categoryId);
  rebuildCheckpointSelect(
    $("#asset-ckpt"),
    t("form.checkpointInherit"),
    state.assetFull?.checkpoint || "",
  );
  rebuildCheckpointSelect($("#cat-ckpt"), t("form.checkpointUnset"), cat?.checkpoint || "");
}

async function fillNewCatCheckpointSelect() {
  await loadGenerationModels();
  const sel = $("#new-cat-ckpt");
  if (!sel) return;
  let preferred = "";
  try {
    const settings = await API.get("/api/settings");
    preferred = settings.checkpoint || "";
  } catch {
    /* ignore */
  }
  rebuildCheckpointSelect(sel, t("form.checkpointUnset"), preferred);
}

function categoryHasAssets() {
  return state.assets.length > 0;
}

function updateMainTabsVisibility() {
  const hasAssets = categoryHasAssets();
  $$("#main-tabs .tab").forEach((tab) => {
    const needsAsset = tab.hasAttribute("data-requires-asset");
    tab.hidden = needsAsset && !hasAssets;
  });
  const active = $("#main-tabs .tab.active");
  const activeId = active?.dataset.tab;
  if (activeId && !hasAssets && active?.hidden) {
    switchTab("category");
    return;
  }
  updateGenerationTabsVisibility();
}

function fillCategorySelect() {
  const sel = $("#asset-category-select");
  if (!sel) return;
  sel.innerHTML = "";
  for (const c of state.categories) {
    const o = document.createElement("option");
    o.value = c.id;
    o.textContent = c.label;
    sel.appendChild(o);
  }
}

async function loadBasicForm(loadSeq = assetLoadSeq) {
  if (!state.assetId || isStaleAssetLoad(loadSeq)) return;
  const assetId = state.assetId;
  let data = state.assetFull?.id === assetId ? state.assetFull : null;
  if (!data) {
    data = await API.get(`/api/assets/${assetId}`);
    if (isStaleAssetLoad(loadSeq, assetId)) return;
  }
  state.assetFull = data;
  const form = $("#form-basic");
  if (!form) return;
  form.id.value = data.id;
  form.filename.value = data.filename;
  form.category.value = data.category;
  form.width.value = data.width;
  form.height.value = data.height;
  form.seed.value = data.seed || "";
  form.subject.value = data.subject || "";
  form.enabled.checked = data.enabled !== false;
  const mode = data.remove_bg_mode || "inherit";
  form.querySelectorAll('input[name="remove_bg_mode"]').forEach((r) => {
    r.checked = r.value === mode;
  });
  rebuildCheckpointSelect($("#asset-ckpt"), t("form.checkpointInherit"), data.checkpoint || "");
  updateGenerationTabsVisibility();
}

async function loadCategoryForm() {
  if (!state.categoryId) return;
  const data = await API.get(`/api/categories/${state.categoryId}`);
  const form = $("#form-category");
  if (!form) return;
  form.source.value = data.source || "";
  form.inbox.value = data.inbox || "";
  form.unity.value = data.unity || "";
  form.alpha_matte.checked = !!data.alpha_matte && data.alpha_matte !== "none";
  form.positive_common.value = data.positive_common || "";
  form.negative_common.value = data.negative_common || "";
  rebuildCheckpointSelect($("#cat-ckpt"), t("form.checkpointUnset"), data.checkpoint || "");
  initPathFields(form);
}

function bindPathFieldSideEffects() {
  $("#ref-image-path")?.addEventListener("pathpicked", async () => {
    genModeHydrating = true;
    $$('input[name="gen_mode"]').forEach((r) => {
      if (r.value === "img2img") r.checked = true;
    });
    genModeHydrating = false;
    updateGenModeUi();
    await saveGenModeOnly();
  });

  $("#cloud-ref-image-path")?.addEventListener("pathpicked", async () => {
    genModeHydrating = true;
    $$('input[name="cloud_gen_mode"]').forEach((r) => {
      if (r.value === "image_to_image") r.checked = true;
    });
    genModeHydrating = false;
    updateCloudGenModeUi();
    await saveCloudGenModeOnly();
  });
}

async function loadComfyUiTab(loadSeq = assetLoadSeq) {
  if (!state.assetId || isStaleAssetLoad(loadSeq)) return;
  const assetId = state.assetId;
  let data = state.assetFull?.id === assetId ? state.assetFull : null;
  if (!data) {
    data = await API.get(`/api/assets/${assetId}`);
    if (isStaleAssetLoad(loadSeq, assetId)) return;
  }
  state.assetFull = data;
  $("#positive-prefix-text").value = data.positive_prefix || "";
  $("#positive-subject-text").value = data.positive_subject || "";
  $("#positive-scene-text").value = data.positive_scene || "";
  $("#positive-light-text").value = data.positive_light || "";
  $("#negative-text").value = data.negative || "";
  if (
    !data.positive_prefix &&
    !data.positive_subject &&
    !data.positive_scene &&
    !data.positive_light &&
    data.positive
  ) {
    $("#positive-subject-text").value = data.positive;
  }
  updatePromptMergedPreview();
  let genMode = data.gen_mode || "txt2img";
  if (genMode === "img2img" && data.ref_image_use_source) genMode = "redraw";
  genModeHydrating = true;
  $$('input[name="gen_mode"]').forEach((r) => {
    r.checked = r.value === genMode;
  });
  genModeHydrating = false;
  $("#ref-image-path").value = data.ref_image || "";
  const denoise = data.img2img_denoise ?? 0.65;
  $("#img2img-denoise").value = denoise;
  $("#img2img-denoise-range").value = denoise;
  updateGenModeUi();
  try {
    const wf = await API.get(`/api/assets/${assetId}/workflow`);
    if (isStaleAssetLoad(loadSeq, assetId)) return;
    $("#workflow-text").value = wf.text || "";
  } catch {
    if (isStaleAssetLoad(loadSeq, assetId)) return;
    $("#workflow-text").value = "";
  }
  await refreshWorkflowPresetSelect();
}

async function loadCloudTab(loadSeq = assetLoadSeq) {
  if (!state.assetId || isStaleAssetLoad(loadSeq)) return;
  const assetId = state.assetId;
  let data = state.assetFull?.id === assetId ? state.assetFull : null;
  if (!data) {
    data = await API.get(`/api/assets/${assetId}`);
    if (isStaleAssetLoad(loadSeq, assetId)) return;
  }
  state.assetFull = data;
  updateCloudTabHero(data);
  let cloudMode = data.cloud_gen_mode || "text_to_image";
  genModeHydrating = true;
  $$('input[name="cloud_gen_mode"]').forEach((r) => {
    r.checked = r.value === cloudMode;
  });
  genModeHydrating = false;
  if (assetUsesCloudBackend(data)) {
    $("#cloud-prompt-text").value = data.cloud_prompt ?? "";
    $("#cloud-negative-text").value = data.cloud_negative ?? "";
  } else {
    $("#cloud-prompt-text").value = data.cloud_prompt || data.positive || "";
    $("#cloud-negative-text").value = data.cloud_negative || data.negative || "";
  }
  $("#cloud-ref-image-path").value = data.ref_image || "";
  const strength = data.cloud_strength ?? data.img2img_denoise ?? 0.65;
  $("#cloud-strength").value = strength;
  $("#cloud-strength-range").value = strength;
  updateCloudGenModeUi();
}

async function refreshWorkflowPresetSelect() {
  const sel = $("#workflow-preset-select");
  if (!sel || !state.assetId) return;
  const data = state.assetFull || {};
  let genMode = data.gen_mode || "txt2img";
  if (genMode === "img2img" && data.ref_image_use_source) genMode = "redraw";
  try {
    const r = await API.get(
      `/api/workflows/templates?category=${encodeURIComponent(data.category || "")}&gen_mode=${encodeURIComponent(genMode)}`,
    );
    const presets = r.presets || [];
    state.workflowPresets = presets;
    sel.innerHTML = presets
      .map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`)
      .join("");
    updateWorkflowPresetDesc();
  } catch {
    sel.innerHTML = "";
    state.workflowPresets = [];
  }
}

function updateWorkflowPresetDesc() {
  const sel = $("#workflow-preset-select");
  const panel = $("#workflow-preset-detail");
  const modeEl = $("#workflow-preset-mode");
  const descEl = $("#workflow-preset-desc");
  const howEl = $("#workflow-preset-how");
  const recEl = $("#workflow-preset-recommended");
  if (!sel || !panel) return;
  const preset = state.workflowPresets?.find((p) => p.id === sel.value);
  if (!preset) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  const modeKey = preset.mode || "";
  if (modeEl) {
    modeEl.textContent = modeKey ? t(`form.wfMode.${modeKey}`) : "";
    modeEl.className = "wf-preset-mode-badge";
    if (modeKey) modeEl.dataset.mode = modeKey;
    else delete modeEl.dataset.mode;
  }
  if (descEl) descEl.textContent = preset.description || "";
  if (howEl) howEl.textContent = preset.how_it_works || "";
  if (recEl) recEl.textContent = preset.recommended_for || "";
}

async function loadWorkflowPreset(btn) {
  if (!state.assetId) return;
  const sel = $("#workflow-preset-select");
  const presetId = sel?.value;
  if (!presetId) return;
  await withBtnBusy(btn, async () => {
    const data = await API.get(`/api/workflows/templates/${encodeURIComponent(presetId)}`);
    $("#workflow-text").value = data.text || "";
    updateWorkflowPresetDesc();
    toast(t("toast.workflowPresetLoaded", { name: data.name || presetId }));
  }).catch((err) => {
    if (err) toast(err.message);
  });
}

function promptSegmentsFromForm() {
  return {
    positive_prefix: $("#positive-prefix-text")?.value.trim() || "",
    positive_subject: $("#positive-subject-text")?.value.trim() || "",
    positive_scene: $("#positive-scene-text")?.value.trim() || "",
    positive_light: $("#positive-light-text")?.value.trim() || "",
  };
}

function composePromptPreview(segments) {
  return [
    segments.positive_prefix,
    segments.positive_subject,
    segments.positive_scene,
    segments.positive_light,
  ]
    .filter(Boolean)
    .join(", ");
}

function updatePromptMergedPreview() {
  const preview = $("#positive-merged-preview");
  if (!preview) return;
  const merged = composePromptPreview(promptSegmentsFromForm());
  preview.value = merged;
}

function bindPromptSegmentListeners() {
  for (const id of [
    "positive-prefix-text",
    "positive-subject-text",
    "positive-scene-text",
    "positive-light-text",
  ]) {
    const el = document.getElementById(id);
    if (!el || el.dataset.bound) continue;
    el.dataset.bound = "1";
    el.addEventListener("input", updatePromptMergedPreview);
  }
}

function currentGenMode() {
  return document.querySelector('input[name="gen_mode"]:checked')?.value || "txt2img";
}

function currentImportGenMode() {
  return document.querySelector('input[name="import_gen_mode"]:checked')?.value || "txt2img";
}

function genModePayloadFromForm() {
  const mode = currentGenMode();
  return {
    gen_mode: mode,
    ref_image: mode === "img2img" ? ($("#ref-image-path")?.value.trim() || "") : "",
    ref_image_use_source: false,
    img2img_denoise: parseFloat($("#img2img-denoise")?.value) || 0.65,
  };
}

function applyGenModeToPromptPanel(mode) {
  const normalized = mode === "redraw" ? "redraw" : mode === "img2img" ? "img2img" : "txt2img";
  genModeHydrating = true;
  $$('input[name="gen_mode"]').forEach((r) => {
    r.checked = r.value === normalized;
  });
  genModeHydrating = false;
  updateGenModeUi();
}

function setImportGenMode(mode, { syncPrompt = true } = {}) {
  const normalized = mode === "redraw" ? "redraw" : "txt2img";
  $$('input[name="import_gen_mode"]').forEach((r) => {
    r.checked = r.value === normalized;
  });
  updateImportHintText();
  if (syncPrompt) applyGenModeToPromptPanel(normalized);
}

function updateImportGenModeBarVisibility() {
  const bar = $("#import-gen-mode-bar");
  if (!bar) return;
  const show = newAssetDlgTab === "import";
  bar.classList.toggle("hidden", !show);
  bar.hidden = !show;
}

function updateImportHintText() {
  const hint = $("#import-asset-hint");
  if (!hint) return;
  const key = currentImportGenMode() === "redraw" ? "dlg.importHintRedraw" : "dlg.importHint";
  hint.dataset.i18n = key;
  hint.textContent = t(key);
}

function onGenModePanelChange() {
  updateGenModeUi();
  void saveGenModeOnly();
  void refreshWorkflowPresetSelect();
  const dlg = $("#dlg-new-asset");
  if (dlg?.open && newAssetDlgTab === "import") {
    const mode = currentGenMode();
    setImportGenMode(mode === "redraw" ? "redraw" : "txt2img", { syncPrompt: false });
  }
}

async function saveGenModeOnly() {
  if (!state.assetId || genModeHydrating) return;
  const payload = genModePayloadFromForm();
  const prev = state.assetFull;
  if (prev) {
    let prevMode = prev.gen_mode || "txt2img";
    if (prevMode === "img2img" && prev.ref_image_use_source) prevMode = "redraw";
    const prevDenoise = prev.img2img_denoise ?? 0.65;
    const prevRef = (prev.ref_image || "").trim();
    if (
      payload.gen_mode === prevMode &&
      payload.ref_image === prevRef &&
      payload.img2img_denoise === prevDenoise
    ) {
      return;
    }
  }
  try {
    state.assetFull = await API.put(`/api/assets/${state.assetId}`, payload);
    updateGenModeUi();
  } catch (err) {
    toast(err.message);
  }
}

function onImportGenModeChange() {
  updateImportHintText();
  setImportGenMode(currentImportGenMode(), { syncPrompt: true });
}

function isImg2imgFamily(mode = currentGenMode()) {
  return mode === "img2img" || mode === "redraw";
}

function refImageSourcePath() {
  return state.paths?.source || state.assetFull?.source_path || "";
}

function updateGenModeUi() {
  const mode = currentGenMode();
  const panel = $("#img2img-panel");
  const refSection = $("#ref-image-section");
  const redrawSection = $("#redraw-source-section");
  const hint = $("#gen-ref-hint");
  panel?.classList.toggle("hidden", !isImg2imgFamily(mode));
  refSection?.classList.toggle("hidden", mode !== "img2img");
  redrawSection?.classList.toggle("hidden", mode !== "redraw");
  if (mode === "redraw") {
    const pathEl = $("#redraw-source-path");
    const inbox = state.paths?.inbox || state.assetFull?.inbox_path || "";
    const src = refImageSourcePath();
    const display = inbox || src;
    if (pathEl) {
      pathEl.textContent = display || t("preview.infoFileMissing");
      pathEl.classList.toggle("is-missing", !display);
    }
  }
  if (hint) {
    const key = mode === "redraw" ? "form.redrawHint" : "form.img2imgHint";
    hint.dataset.i18n = key;
    hint.textContent = t(key);
  }
}

function syncDenoiseFromRange() {
  const range = $("#img2img-denoise-range");
  const num = $("#img2img-denoise");
  if (range && num) num.value = range.value;
}

function syncDenoiseFromNumber() {
  const range = $("#img2img-denoise-range");
  const num = $("#img2img-denoise");
  if (!range || !num) return;
  let v = parseFloat(num.value);
  if (Number.isNaN(v)) v = 0.65;
  v = Math.max(0.01, Math.min(1, v));
  num.value = v;
  range.value = v;
}

async function pickRefImageFile() {
  try {
    const r = await API.post("/api/pick-image-file", {});
    if (r.cancelled) return null;
    return (r.path || r.absolute || "").trim() || null;
  } catch (err) {
    toast(err.message);
    return null;
  }
}

function resolveTencentApiKey(keys) {
  if (keys.tencent?.trim()) return keys.tencent.trim();
  const sk = keys.tencent_secret_key?.trim();
  if (sk?.startsWith("sk-")) return sk;
  const sid = keys.tencent_secret_id?.trim();
  if (sid?.startsWith("sk-")) return sid;
  return sk || "";
}

async function loadSettingsForm() {
  const data = await API.get("/api/settings");
  const form = $("#form-settings");
  if (!form) return;
  for (const k of [
    "project_root",
    "art_pipeline_root",
    "log_dir",
    "comfyui_url",
    "steps",
    "cfg",
    "sampler",
    "scheduler",
    "seed",
    "deepseek_api_key",
    "deepseek_model",
    "cloud_max_concurrent",
  ]) {
    if (form[k] !== undefined) form[k].value = data[k] ?? "";
  }
  const keys = data.cloud_api_keys || {};
  const map = {
    "cloud_api_keys.stability": keys.stability,
    "cloud_api_keys.dashscope": keys.dashscope,
    "cloud_api_keys.tencent": resolveTencentApiKey(keys),
    "cloud_api_keys.volcengine": keys.volcengine,
    "cloud_api_keys.volcengine_endpoint": keys.volcengine_endpoint,
  };
  for (const [name, val] of Object.entries(map)) {
    const el = form.elements.namedItem(name);
    if (el) el.value = val ?? "";
  }
  updateLogDirHint(data);
  state.apiPortals = data.api_portals || {};
  resetAllCloudApiStatuses();
  resetDeepseekApiStatus();
  initPathFields(form);
}

const CLOUD_API_FIELD_MAP = {
  stability: ["cloud_api_keys.stability"],
  dashscope: ["cloud_api_keys.dashscope"],
  tencent: ["cloud_api_keys.tencent"],
  volcengine: ["cloud_api_keys.volcengine", "cloud_api_keys.volcengine_endpoint"],
};

const SETTINGS_API_PROVIDERS = {
  deepseek: {
    fields: ["deepseek_api_key"],
    hasInput(form) {
      return !!form?.deepseek_api_key?.value?.trim();
    },
  },
};

function cloudApiCard(provider) {
  return document.querySelector(`.cloud-api-card[data-provider="${provider}"]`);
}

function cloudKeysFromForm() {
  const form = $("#form-settings");
  const keys = {};
  if (!form) return keys;
  for (const name of [
    "cloud_api_keys.stability",
    "cloud_api_keys.dashscope",
    "cloud_api_keys.tencent",
    "cloud_api_keys.volcengine",
    "cloud_api_keys.volcengine_endpoint",
  ]) {
    const el = form.elements.namedItem(name);
    if (!el) continue;
    keys[name.slice("cloud_api_keys.".length)] = el.value.trim();
  }
  return keys;
}

function cloudProviderHasInput(provider) {
  const form = $("#form-settings");
  if (!form) return false;
  return (CLOUD_API_FIELD_MAP[provider] || []).some((name) => {
    const el = form.elements.namedItem(name);
    return el?.value?.trim();
  });
}

function setCloudApiStatus(provider, status, message = "") {
  const card = cloudApiCard(provider);
  if (!card) return;
  const badge = card.querySelector(".cloud-api-status");
  const textEl = badge?.querySelector(".cloud-api-status-text");
  card.classList.remove("is-verified", "is-error", "is-testing");
  if (status === "ok") card.classList.add("is-verified");
  else if (status === "error") card.classList.add("is-error");
  else if (status === "testing") card.classList.add("is-testing");
  if (badge) badge.dataset.status = status;
  if (textEl) {
    if (message) textEl.textContent = message;
    else if (status === "idle") textEl.textContent = t("form.cloudStatusIdle");
    else if (status === "empty") textEl.textContent = t("form.cloudStatusEmpty");
    else if (status === "testing") textEl.textContent = t("form.cloudStatusTesting");
    else if (status === "ok") textEl.textContent = t("form.cloudStatusOk");
    else if (status === "error") textEl.textContent = t("form.cloudStatusError");
  }
}

function resetCloudApiStatus(provider) {
  const status = cloudProviderHasInput(provider) ? "idle" : "empty";
  setCloudApiStatus(provider, status);
}

function resetAllCloudApiStatuses() {
  for (const provider of Object.keys(CLOUD_API_FIELD_MAP)) {
    resetCloudApiStatus(provider);
  }
}

async function testCloudApi(provider, btn) {
  await withBtnBusy(btn, async () => {
    setCloudApiStatus(provider, "testing");
    const keys = cloudKeysFromForm();
    const result = await API.post("/api/cloud/verify", { provider, keys });
    if (result.ok) {
      setCloudApiStatus(provider, "ok", result.message || t("form.cloudStatusOk"));
      toast(t("toast.cloudVerified"));
    } else {
      setCloudApiStatus(provider, "error", result.message || t("form.cloudStatusError"));
      toast(result.message || t("toast.cloudVerifyFailed"), { variant: "error" });
    }
  }).catch((err) => {
    if (err) {
      setCloudApiStatus(provider, "error", err.message || t("form.cloudStatusError"));
      toast(err.message || t("toast.cloudVerifyFailed"), { variant: "error" });
    }
  });
}

function resetDeepseekApiStatus() {
  const form = $("#form-settings");
  const status = SETTINGS_API_PROVIDERS.deepseek?.hasInput(form) ? "idle" : "empty";
  setCloudApiStatus("deepseek", status);
}

async function testDeepseekApi(btn) {
  const form = $("#form-settings");
  if (!form) return;
  await withBtnBusy(btn, async () => {
    setCloudApiStatus("deepseek", "testing");
    const result = await API.post("/api/cloud/verify", {
      provider: "deepseek",
      keys: {
        deepseek_api_key: form.deepseek_api_key?.value?.trim() || "",
        deepseek_model: form.deepseek_model?.value || "",
      },
    });
    if (result.ok) {
      setCloudApiStatus("deepseek", "ok", result.message || t("form.cloudStatusOk"));
      toast(t("toast.deepseekVerified"));
    } else {
      setCloudApiStatus("deepseek", "error", result.message || t("form.cloudStatusError"));
      toast(result.message || t("toast.deepseekVerifyFailed"), { variant: "error" });
    }
  }).catch((err) => {
    if (err) {
      const msg =
        err.status === 404 ? t("toast.apiRouteMissingRestart") : err.message || t("form.cloudStatusError");
      setCloudApiStatus("deepseek", "error", msg);
      toast(msg, { variant: "error" });
    }
  });
}

function bindCloudApiFieldListeners() {
  $$(".cloud-api-card [data-cloud-field]").forEach((input) => {
    input.addEventListener("input", () => {
      const card = input.closest(".cloud-api-card");
      const provider = card?.dataset.provider;
      if (provider && CLOUD_API_FIELD_MAP[provider]) resetCloudApiStatus(provider);
    });
  });
  $$("[data-settings-api-field]").forEach((el) => {
    const reset = () => {
      if (el.dataset.settingsApiField === "deepseek") resetDeepseekApiStatus();
    };
    el.addEventListener("input", reset);
    el.addEventListener("change", reset);
  });
}

function updateLogDirHint(data) {
  const hint = $("#log-dir-hint");
  if (!hint) return;
  const effective = data?.log_dir_effective || data?.log_dir_default || "";
  hint.textContent = t("form.logDirHint", { path: effective, file: data?.log_file || "" });
}

function formSettingsLogDir() {
  const form = $("#form-settings");
  return form?.log_dir?.value?.trim() || "";
}

function aiHistoryUrl(mode = state.aiMode) {
  return `/api/ai/history/${encodeURIComponent(state.assetId)}?mode=${encodeURIComponent(mode)}`;
}

function updateAiModeUi() {
  const mode = state.aiMode;
  $$(".ai-mode-tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.aiMode === mode));
  const hint = $("#ai-mode-hint");
  if (hint) hint.textContent = aiModeText(mode, "hint");
  const input = $("#ai-input");
  if (input && !isAssetAiBusy(state.assetId)) input.placeholder = aiModeText(mode, "placeholder");
}

function renderAiChat(history = []) {
  state.aiChatHistory = history;
  const box = $("#ai-history");
  if (!box) return;
  if (!state.assetId) {
    box.innerHTML = `<div class="ai-empty"><p>${esc(t("ai.selectAssetFirst"))}</p></div>`;
    return;
  }
  if (!history.length) {
    box.innerHTML = `<div class="ai-empty"><p>${esc(aiModeText(state.aiMode, "empty"))}</p></div>`;
    return;
  }
  box.innerHTML = history
    .map((item) => {
      if (item.failed) {
        const retry = item.retry_message || "";
        return `<article class="ai-msg assistant failed" tabindex="0" data-retry-message="${esc(retry)}" title="${esc(t("ai.failedClickHint"))}">
        <div class="ai-msg-head">
          <span class="ai-msg-role">${esc(t("ai.roleFailed"))}</span>
          <span class="ai-failed-icon" aria-hidden="true" title="${esc(t("ai.failed"))}">!</span>
          <button type="button" class="btn sm ghost ai-resend-btn" data-action="ai-resend" data-retry-message="${esc(retry)}">${esc(t("ai.resend"))}</button>
        </div>
        <div class="ai-msg-body">${esc(item.content || "")}</div>
      </article>`;
      }
      const role = item.role === "user" ? "user" : "assistant";
      const label = role === "user" ? t("ai.roleUser") : t("ai.roleAssistant");
      return `<article class="ai-msg ${role}">
        <div class="ai-msg-head"><span class="ai-msg-role">${esc(label)}</span></div>
        <div class="ai-msg-body">${esc(item.content || "")}</div>
      </article>`;
    })
    .join("");
  box.scrollTop = box.scrollHeight;
}

function appendFailedAiTurn(userMsg, errorMsg) {
  const hist = [...(state.aiChatHistory || [])];
  hist.push({ role: "user", content: userMsg });
  hist.push({
    role: "assistant",
    content: errorMsg,
    failed: true,
    retry_message: userMsg,
  });
  renderAiChat(hist);
}

function fillAiInput(message) {
  const input = $("#ai-input");
  if (!input) return;
  input.value = message;
  input.focus();
}

async function sendAiWithMessage(msg) {
  const text = String(msg || "").trim();
  if (!text) return;
  fillAiInput(text);
  await sendAi();
}

function aiPendingCount(assetId) {
  return aiPendingCounts.get(assetId) || 0;
}

function isAssetAiBusy(assetId) {
  return Boolean(assetId) && aiPendingCount(assetId) > 0;
}

function beginAiRequest(assetId) {
  aiPendingCounts.set(assetId, aiPendingCount(assetId) + 1);
  if (assetId === state.assetId) syncAiBusyUi();
}

function endAiRequest(assetId) {
  const next = aiPendingCount(assetId) - 1;
  if (next <= 0) aiPendingCounts.delete(assetId);
  else aiPendingCounts.set(assetId, next);
  if (assetId === state.assetId) syncAiBusyUi();
}

function syncAiBusyUi() {
  const busy = isAssetAiBusy(state.assetId);
  state.aiBusy = busy;
  const btn = $("#ai-send-btn");
  const input = $("#ai-input");
  if (btn) {
    setBtnBusy(btn, busy);
    btn.textContent = busy ? t("ai.thinking") : t("ai.send");
  }
  if (input) input.disabled = busy;
  const box = $("#ai-history");
  if (busy && box && state.assetId) {
    const typing = box.querySelector(".ai-typing");
    if (!typing) {
      box.insertAdjacentHTML(
        "beforeend",
        `<div class="ai-typing muted sm"><span class="ai-typing-dots" aria-hidden="true"><span></span><span></span><span></span></span>${esc(t("ai.typing"))}</div>`,
      );
      box.scrollTop = box.scrollHeight;
    }
  } else {
    box?.querySelector(".ai-typing")?.remove();
  }
}

async function clearAiChat(mode = state.aiMode) {
  if (state.assetId) {
    try {
      await API.del(aiHistoryUrl(mode));
    } catch {
      /* ignore */
    }
  }
  if (mode === state.aiMode) renderAiChat([]);
}

async function refreshAiMessages() {
  if (!state.assetId) {
    renderAiChat([]);
    return;
  }
  try {
    const data = await API.get(aiHistoryUrl());
    renderAiChat(data.history || []);
  } catch {
    renderAiChat([]);
  }
  if (isAssetAiBusy(state.assetId)) syncAiBusyUi();
}

function isAiIntroDismissed() {
  if (aiIntroDismissedMemory) return true;
  try {
    return sessionStorage.getItem(AI_INTRO_DISMISSED_KEY) === "1";
  } catch {
    return aiIntroDismissedMemory;
  }
}

function dismissAiIntro() {
  aiIntroDismissedMemory = true;
  if (aiIntroAutoHideTimer) {
    clearTimeout(aiIntroAutoHideTimer);
    aiIntroAutoHideTimer = null;
  }
  try {
    sessionStorage.setItem(AI_INTRO_DISMISSED_KEY, "1");
  } catch {
    /* ignore */
  }
  $("#ai-intro-section")?.classList.add("is-hidden");
}

function setupAiIntroBanner() {
  const section = $("#ai-intro-section");
  if (!section) return;
  if (isAiIntroDismissed()) {
    section.classList.add("is-hidden");
    return;
  }
  section.classList.remove("is-hidden");
  if (aiIntroAutoHideScheduled) return;
  aiIntroAutoHideScheduled = true;
  aiIntroAutoHideTimer = setTimeout(() => {
    aiIntroAutoHideTimer = null;
    dismissAiIntro();
  }, 5000);
}

async function openAiPanel() {
  setupAiIntroBanner();
  updateAiModeUi();
  await refreshAiMessages();
  syncAiBusyUi();
}

async function switchAiMode(mode) {
  if (!AI_MODE_KEYS.includes(mode) || state.aiMode === mode) return;
  state.aiMode = mode;
  updateAiModeUi();
  await refreshAiMessages();
}

function switchTab(tabId) {
  const tabBtn = $(`#main-tabs .tab[data-tab="${tabId}"]`);
  if (tabBtn?.hidden) {
    const fallbackGen = effectiveGenTabId();
    if (fallbackGen && !$(`#main-tabs .tab[data-tab="${fallbackGen}"]`)?.hidden) tabId = fallbackGen;
    else tabId = categoryHasAssets() ? "ai" : "category";
  }
  $$("#main-tabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tabId));
  $$(".tab-body").forEach((b) => b.classList.add("hidden"));
  const body = $(`#${tabBodyElementId(tabId)}`);
  if (body) body.classList.remove("hidden");
  if (tabId === "category") loadCategoryForm();
  if (tabId === "comfyui") loadComfyUiTab();
  if (tabId.startsWith("cloud-")) loadCloudTab();
  if (tabId === "settings") loadSettingsForm();
  if (tabId === "ai") openAiPanel();
}

function revokePreviewBlob() {
  if (state.previewBlobUrl) {
    URL.revokeObjectURL(state.previewBlobUrl);
    state.previewBlobUrl = null;
  }
}

function previewMaxSize() {
  const stage = $("#preview-stage");
  if (!stage) return 480;
  const dpr = window.devicePixelRatio || 1;
  const base = Math.max(stage.clientWidth, stage.clientHeight, 160);
  return Math.min(2048, Math.max(256, Math.ceil(base * dpr * 1.5)));
}

function resetPreviewView() {
  previewView.zoom = 1;
  previewView.panX = 0;
  previewView.panY = 0;
  previewView.imgW = 0;
  previewView.imgH = 0;
  previewView.panning = null;
  $("#preview-viewport")?.classList.remove("is-grabbing");
}

function previewViewportSize() {
  const vp = $("#preview-viewport");
  const stage = $("#preview-stage");
  const rect = vp?.getBoundingClientRect();
  const w = rect?.width || stage?.clientWidth || 320;
  const h = rect?.height || stage?.clientHeight || 320;
  return { w: Math.max(w, 1), h: Math.max(h, 1) };
}

function previewViewportOffset() {
  const { w: vpW, h: vpH } = previewViewportSize();
  if (!previewView.imgW) return { ox: 0, oy: 0, docW: 0, docH: 0 };
  const docW = previewView.imgW * previewView.zoom;
  const docH = previewView.imgH * previewView.zoom;
  return {
    ox: (vpW - docW) / 2 + previewView.panX,
    oy: (vpH - docH) / 2 + previewView.panY,
    docW,
    docH,
  };
}

function layoutPreviewImage() {
  const img = $("#preview-img");
  if (!img || img.hidden || !previewView.imgW) return;
  const { ox, oy, docW, docH } = previewViewportOffset();
  img.style.width = `${docW}px`;
  img.style.height = `${docH}px`;
  img.style.left = `${ox}px`;
  img.style.top = `${oy}px`;
  const badge = $("#preview-zoom-label");
  if (badge) badge.textContent = `${Math.round(previewView.zoom * 100)}%`;
}

function previewZoom100() {
  previewView.zoom = 1;
  previewView.panX = 0;
  previewView.panY = 0;
  layoutPreviewImage();
}

function previewZoomFit() {
  const { w: vpW, h: vpH } = previewViewportSize();
  if (!previewView.imgW) return;
  const zx = vpW / previewView.imgW;
  const zy = vpH / previewView.imgH;
  previewView.zoom = Math.max(
    previewView.minZoom,
    Math.min(previewView.maxZoom, Math.min(zx, zy)),
  );
  previewView.panX = 0;
  previewView.panY = 0;
  layoutPreviewImage();
}

function previewZoomBy(factor, clientX, clientY) {
  const vp = $("#preview-viewport");
  if (!vp || !previewView.imgW) return;
  const rect = vp.getBoundingClientRect();
  const cx = clientX != null ? clientX - rect.left : rect.width / 2;
  const cy = clientY != null ? clientY - rect.top : rect.height / 2;
  const { ox, oy } = previewViewportOffset();
  const imgX = (cx - ox) / previewView.zoom;
  const imgY = (cy - oy) / previewView.zoom;
  const nextZoom = Math.max(
    previewView.minZoom,
    Math.min(previewView.maxZoom, previewView.zoom * factor),
  );
  previewView.zoom = nextZoom;
  const { w: vpW, h: vpH } = previewViewportSize();
  previewView.panX = cx - imgX * nextZoom - (vpW - previewView.imgW * nextZoom) / 2;
  previewView.panY = cy - imgY * nextZoom - (vpH - previewView.imgH * nextZoom) / 2;
  layoutPreviewImage();
}

function hidePreviewLoadingUi() {
  const ph = $("#preview-ph");
  const loading = $("#preview-loading");
  const stage = $("#preview-stage");
  if (ph) ph.hidden = true;
  if (loading) {
    loading.hidden = true;
    loading.setAttribute("aria-hidden", "true");
  }
  stage?.classList.remove("is-loading");
}

async function applyPreviewBlob(reqId, asset, blob) {
  const img = $("#preview-img");
  const stage = $("#preview-stage");
  const badge = $("#preview-zoom-label");
  if (!img || reqId !== state.previewReq) return;

  revokePreviewBlob();
  resetPreviewView();
  state.previewBlobUrl = URL.createObjectURL(blob);
  img.src = state.previewBlobUrl;

  try {
    if (typeof img.decode === "function") {
      await img.decode();
    } else {
      await new Promise((resolve, reject) => {
        if (img.complete) resolve();
        else {
          img.onload = () => resolve();
          img.onerror = () => reject(new Error("image load failed"));
        }
      });
    }
  } catch {
    if (reqId !== state.previewReq) return;
    showPreviewEmpty(asset);
    return;
  }

  if (reqId !== state.previewReq) return;

  previewView.imgW = img.naturalWidth || 1;
  previewView.imgH = img.naturalHeight || 1;
  previewZoomFit();

  img.hidden = false;
  hidePreviewLoadingUi();
  layoutPreviewImage();
  if (badge) badge.hidden = false;
  stage?.classList.add("has-image");
}

function bindPreviewViewport() {
  const vp = $("#preview-viewport");
  if (!vp || vp.dataset.bound) return;
  vp.dataset.bound = "1";

  vp.addEventListener(
    "wheel",
    (e) => {
      if ($("#preview-img")?.hidden) return;
      e.preventDefault();
      previewZoomBy(e.deltaY < 0 ? 1.12 : 1 / 1.12, e.clientX, e.clientY);
    },
    { passive: false },
  );

  vp.addEventListener("mousedown", (e) => {
    if ($("#preview-img")?.hidden || e.button !== 0) return;
    e.preventDefault();
    previewView.panning = {
      x: e.clientX,
      y: e.clientY,
      panX: previewView.panX,
      panY: previewView.panY,
    };
    vp.classList.add("is-grabbing");
  });

  window.addEventListener("mousemove", (e) => {
    if (!previewView.panning) return;
    previewView.panX = previewView.panning.panX + (e.clientX - previewView.panning.x);
    previewView.panY = previewView.panning.panY + (e.clientY - previewView.panning.y);
    layoutPreviewImage();
  });

  window.addEventListener("mouseup", () => {
    if (!previewView.panning) return;
    previewView.panning = null;
    vp.classList.remove("is-grabbing");
  });

  vp.addEventListener("dblclick", (e) => {
    if ($("#preview-img")?.hidden) return;
    e.preventDefault();
    previewZoomFit();
  });

  if (typeof ResizeObserver !== "undefined") {
    new ResizeObserver(() => layoutPreviewImage()).observe(vp);
  }
}

function formatBytes(n) {
  if (!n || n <= 0) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDateTime(epochOrIso) {
  if (epochOrIso == null || epochOrIso === "") return "—";
  const d =
    typeof epochOrIso === "number"
      ? new Date(epochOrIso * 1000)
      : new Date(epochOrIso);
  if (Number.isNaN(d.getTime())) return "—";
  const lang = document.documentElement.lang || "zh-CN";
  return d.toLocaleString(lang, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatActivityTime(entry) {
  if (entry?.ts_epoch) return formatDateTime(entry.ts_epoch);
  return entry?.ts || "—";
}

function fileInfoLine(info) {
  if (!info?.exists) return t("preview.infoFileMissing");
  const date = formatDateTime(info.mtime);
  const size = formatBytes(info.size);
  return size
    ? t("preview.infoFileLine", { date, size })
    : t("preview.infoFileLineNoSize", { date });
}

function dlRow(label, value, { active = false, muted = false } = {}) {
  const cls = ["preview-dl-row", active && "is-active", muted && "is-muted"]
    .filter(Boolean)
    .join(" ");
  return `<div class="${cls}"><dt>${esc(label)}</dt><dd class="${muted ? "muted" : ""}">${esc(value)}</dd></div>`;
}

function clearPreviewInfo() {
  state.previewInfo = null;
  $("#preview-info-name").textContent = "—";
  $("#preview-info-sub").textContent = "—";
  $("#preview-dl-files").innerHTML = "";
  $("#preview-dl-config").innerHTML = "";
  $("#preview-activity").innerHTML = "";
}

function previewActivityMessage(msg) {
  const m = String(msg || "").trim();
  if (!m) return "";
  // AI 自动保存若主要为提示词字段，不在详情面板展示
  if (/AI 已自动保存|Auto-saved/i.test(m)) {
    const inner = m.match(/[（(]([^）)]+)[）)]/)?.[1] || "";
    if (inner && /(?:^|,\s*)(?:subject|positive|negative|cloud_prompt|cloud_negative)/i.test(inner)) {
      return "";
    }
  }
  if (/^\(?subject,\s*positive/i.test(m)) return "";
  return m;
}

function renderPreviewInfo(info) {
  if (!info) {
    clearPreviewInfo();
    return;
  }
  const sourceKey = currentPreviewSourceKey();
  const sourceLabel = previewSourceLabel();
  const nameEl = $("#preview-info-name");
  const subEl = $("#preview-info-sub");
  const filesEl = $("#preview-dl-files");
  const configEl = $("#preview-dl-config");
  const activityEl = $("#preview-activity");

  if (nameEl) nameEl.textContent = info.filename || "—";
  if (subEl) {
    const parts = [info.size_label, sourceLabel];
    subEl.textContent = parts.filter(Boolean).join(" · ");
  }

  const fileLabels = {
    source: t("preview.infoFileSource"),
    inbox: t("preview.infoFileInbox"),
    unity: t("preview.infoFileEngine"),
  };
  if (filesEl) {
    filesEl.innerHTML = ["source", "inbox", "unity"]
      .map((key) => {
        const slot = info.files?.[key];
        return dlRow(fileLabels[key], fileInfoLine(slot), {
          active: sourceKey === key,
          muted: !slot?.exists,
        });
      })
      .join("");
  }

  if (configEl) {
    const rows = [
      dlRow(t("preview.infoWorkflow"), fileInfoLine(info.workflow), {
        muted: !info.workflow?.exists,
      }),
      dlRow(t("preview.infoPipelineConfig"), fileInfoLine(info.config_file), {
        muted: !info.config_file?.exists,
      }),
      dlRow(
        t("preview.infoPostprocess"),
        info.has_postprocess
          ? t("preview.infoPostprocessCustom")
          : t("preview.infoPostprocessDefault")
      ),
    ];
    if (info.seed) rows.push(dlRow(t("form.seed"), info.seed));
    configEl.innerHTML = rows.join("");
  }

  if (activityEl) {
    const items = (info.activity || [])
      .map((entry) => ({ ...entry, msg: previewActivityMessage(entry.msg) }))
      .filter((entry) => entry.msg);
    if (!items.length) {
      activityEl.innerHTML = `<li class="empty">${esc(t("preview.infoNoActivity"))}</li>`;
    } else {
      activityEl.innerHTML = items
        .map(
          (entry) =>
            `<li><time>${esc(formatActivityTime(entry))}</time><span>${esc(entry.msg || "")}</span></li>`
        )
        .join("");
    }
  }
}

async function loadPreviewInfo(assetId) {
  if (!assetId) {
    clearPreviewInfo();
    return;
  }
  const panel = $("#preview-info");
  const reqId = ++state.previewInfoReq;
  setPanelLoading(panel, true);
  try {
    const info = await API.get(`/api/assets/${encodeURIComponent(assetId)}/info`);
    if (reqId !== state.previewInfoReq || state.assetId !== assetId) return;
    state.previewInfo = info;
    renderPreviewInfo(info);
  } catch {
    if (reqId !== state.previewInfoReq || state.assetId !== assetId) return;
    const asset = state.assets.find((a) => a.id === assetId);
    if (asset) {
      renderPreviewInfo({
        filename: asset.filename,
        size_label: asset.size_label,
        files: {},
        activity: [],
      });
    }
  } finally {
    if (reqId === state.previewInfoReq) setPanelLoading(panel, false);
  }
}

function showPreviewEmpty(asset, message = null) {
  const img = $("#preview-img");
  const ph = $("#preview-ph");
  const loading = $("#preview-loading");
  const badge = $("#preview-zoom-label");
  img.hidden = true;
  revokePreviewBlob();
  img.removeAttribute("src");
  img.style.width = "";
  img.style.height = "";
  img.style.left = "";
  img.style.top = "";
  resetPreviewView();
  if (badge) badge.hidden = true;
  $("#preview-stage")?.classList.remove("has-image");
  if (ph) {
    ph.hidden = false;
    ph.textContent = message ?? t("preview.noFile");
  }
  if (loading) loading.hidden = true;
  $("#preview-stage")?.classList.remove("is-loading");
  if (asset) loadPreviewInfo(asset.id);
}

function clearPreview() {
  clearPreviewInfo();
  revokePreviewBlob();
  const img = $("#preview-img");
  const badge = $("#preview-zoom-label");
  img.hidden = true;
  img.removeAttribute("src");
  img.style.width = "";
  img.style.height = "";
  img.style.left = "";
  img.style.top = "";
  resetPreviewView();
  if (badge) badge.hidden = true;
  $("#preview-stage")?.classList.remove("has-image", "is-loading");
  $("#preview-loading").hidden = true;
  const ph = $("#preview-ph");
  if (ph) {
    ph.hidden = false;
    ph.textContent = t("preview.selectAsset");
  }
}

async function loadPreview({ bustCache = false } = {}) {
  const asset = state.assets.find((a) => a.id === state.assetId);
  if (!asset) {
    clearPreview();
    return;
  }
  const stage = $("#preview-stage");
  const img = $("#preview-img");
  const ph = $("#preview-ph");
  const loading = $("#preview-loading");
  const hasCurrentImage = !!state.previewBlobUrl && !img.hidden;
  const reqId = ++state.previewReq;

  stage?.classList.add("is-loading");
  if (loading) {
    loading.hidden = false;
    loading.removeAttribute("aria-hidden");
  }
  if (hasCurrentImage) {
    if (ph) ph.hidden = true;
  } else {
    img.hidden = true;
    revokePreviewBlob();
    img.removeAttribute("src");
    if (ph) ph.hidden = true;
  }
  await loadPreviewInfo(asset.id);

  const params = new URLSearchParams({
    source: currentPreviewSourceKey(),
    max: String(previewMaxSize()),
  });
  const srcKey = currentPreviewSourceKey();
  const mtime = state.previewInfo?.files?.[srcKey]?.mtime;
  if (bustCache) params.set("v", String(Date.now()));
  else if (mtime) params.set("v", String(Math.floor(mtime)));

  const url = `/api/assets/${encodeURIComponent(asset.id)}/preview.png?${params}`;

  try {
    const res = await fetch(url);
    if (reqId !== state.previewReq) return;

    if (!res.ok) {
      showPreviewEmpty(asset);
      return;
    }
    const blob = await res.blob();
    if (reqId !== state.previewReq) return;
    if (blob.size < 8) {
      showPreviewEmpty(asset);
      return;
    }
    const isImage =
      !blob.type || blob.type.startsWith("image/") || blob.type === "application/octet-stream";
    if (!isImage) {
      showPreviewEmpty(asset);
      return;
    }

    await applyPreviewBlob(reqId, asset, blob);
  } catch {
    if (reqId !== state.previewReq) return;
    showPreviewEmpty(asset);
  }
}

async function loadPaths(loadSeq = assetLoadSeq) {
  if (!state.assetId) {
    state.paths = null;
    return;
  }
  const assetId = state.assetId;
  const paths = await API.get(`/api/assets/${assetId}/paths`);
  if (isStaleAssetLoad(loadSeq, assetId)) return;
  state.paths = paths;
  if (currentGenMode() === "redraw") updateGenModeUi();
}

// ── 数据加载 ─────────────────────────────────────────────

async function loadAssetList() {
  if (!state.categoryId) return;
  const panel = $("#panel-assets");
  setPanelLoading(panel, true);
  try {
    const q = $("#asset-search")?.value.trim() || "";
    const data = await API.get(
      `/api/assets?category=${encodeURIComponent(state.categoryId)}&q=${encodeURIComponent(q)}`,
    );
    state.assets = data.assets || [];
    pruneAssetSelection();
    renderAssets();
    updateMainTabsVisibility();
    scanStatus();
  } catch (err) {
    toast(t("toast.listFailed", { msg: err.message }));
  } finally {
    setPanelLoading(panel, false);
  }
}

async function selectCategory(catId) {
  const changed = state.categoryId !== catId;
  state.categoryId = catId;
  if (changed) {
    state.assetId = null;
    state.assetFull = null;
    state.statusMap = {};
    state.selectedAssetIds = [];
    state.lastAssetClickId = null;
    clearPreview();
  }
  renderCategories();
  await loadAssetList();
  await loadCategoryForm();
  updateMainTabsVisibility();
  if (state.assets.length > 0 && (changed || !state.assetId)) {
    await selectAsset(state.assets[0].id);
  } else if (state.assets.length === 0) {
    clearPreview();
    const active = $("#main-tabs .tab.active");
    if (active?.hasAttribute("data-requires-asset")) switchTab("category");
  }
}

async function selectAsset(assetId, { keepSelection = false } = {}) {
  const loadSeq = ++assetLoadSeq;
  if (!keepSelection) {
    state.selectedAssetIds = assetId ? [assetId] : [];
    if (assetId) state.lastAssetClickId = assetId;
  }
  state.assetId = assetId;
  state.assetFull = null;
  renderAssets();
  await loadPaths(loadSeq);
  if (isStaleAssetLoad(loadSeq, assetId)) return;
  await loadBasicForm(loadSeq);
  if (isStaleAssetLoad(loadSeq, assetId)) return;
  // 无论当前在哪个 Tab，都刷新提示词表单，避免切换资源后仍显示上一张图的内容
  if (assetUsesCloudBackend()) await loadCloudTab(loadSeq);
  else await loadComfyUiTab(loadSeq);
  if (isStaleAssetLoad(loadSeq, assetId)) return;
  await loadPreview();
  if (isStaleAssetLoad(loadSeq, assetId)) return;
  if ($("#main-tabs .tab.active")?.dataset.tab === "ai") await openAiPanel();
}

async function scanStatus() {
  if (!state.assets.length) {
    state.statusMap = {};
    state.statusScanning = false;
    renderAssets();
    return;
  }
  state.statusScanning = true;
  renderAssets();
  try {
    const data = await API.post("/api/assets/status", { ids: state.assets.map((a) => a.id) });
    state.statusMap = data.status || {};
  } catch (err) {
    toast(t("toast.scanFailed", { msg: err.message }));
  } finally {
    state.statusScanning = false;
    renderAssets();
  }
}

async function openAssetPathDir(assetId, kind) {
  const st = state.statusMap[assetId]?.[kind];
  if (st?.dir) {
    await openPath(st.dir);
    return;
  }
  try {
    const paths = await API.get(`/api/assets/${assetId}/paths`);
    const file = paths[kind];
    if (!file) {
      toast(t("toast.noPath"));
      return;
    }
    const dir = file.replace(/[/\\][^/\\]+$/, "");
    await openPath(dir || file);
  } catch (err) {
    toast(err.message);
  }
}

async function loadGenerationModels() {
  try {
    const data = await API.get("/api/generation/models");
    state.checkpoints = data.local?.checkpoints || [];
    state.cloudModels = data.cloud?.models || [];
    fillCheckpointSelects();
    updateGenerationTabLabels();
  } catch {
    try {
      const data = await API.get("/api/comfyui/checkpoints");
      state.checkpoints = data.checkpoints || [];
    } catch {
      state.checkpoints = [];
    }
    try {
      const cloud = await API.get("/api/cloud/models");
      state.cloudModels = cloud.models || [];
    } catch {
      state.cloudModels = [];
    }
    fillCheckpointSelects();
  }
  updateGenerationTabLabels();
}

async function loadCheckpoints() {
  await loadGenerationModels();
}

// ── 任务 / ComfyUI ─────────────────────────────────────────────

const JOB_FLOAT_POS_KEY = "artApp.jobFloatPos";
const JOB_FLOAT_DRAG_THRESHOLD = 4;

const jobProg = {
  batchIdx: 0,
  batchTotal: 1,
  stepPct: 0,
  filename: "",
  message: "",
  lastApiKind: "",
  phase: "",
  cancelling: false,
  visible: false,
  queueItems: [],
  queuedCount: 0,
  submitting: false,
  cloudTasks: [],
};

function jobHasActivity() {
  return (
    jobProg.visible ||
    state.jobTrack.active ||
    state.jobTrack.sawBusy ||
    jobProg.queuedCount > 0 ||
    jobProg.queueItems.length > 0
  );
}

function jobLabelForAssets(assetIds) {
  if (assetIds.length === 1) {
    const first = state.assets.find((a) => a.id === assetIds[0]);
    return first?.filename || assetIds[0];
  }
  return t("job.batchLabel", { n: assetIds.length });
}

function resetJobProg(kind = "") {
  jobProg.batchIdx = 0;
  jobProg.batchTotal = 1;
  jobProg.stepPct = 0;
  jobProg.filename = "";
  jobProg.message = "";
  jobProg.phase = "";
  jobProg.lastApiKind = kind;
  jobProg.cancelling = false;
  jobProg.queueItems = [];
  jobProg.queuedCount = 0;
  jobProg.submitting = false;
  jobProg.cloudTasks = [];
}

function resetJobTracking() {
  state.jobRunId = null;
  state.jobTrack = { active: false, sawBusy: false, postOk: false, submittedAt: 0 };
}

const JOB_START_GRACE_MS = 3000;

function stopJobPoll() {
  if (state.jobTimer) {
    clearInterval(state.jobTimer);
    state.jobTimer = null;
  }
}

function markJobSubmitting(kind, assetIds) {
  stopJobPoll();
  state.jobTrack.active = true;
  state.jobTrack.sawBusy = false;
  state.jobTrack.postOk = false;
  state.jobTrack.submittedAt = Date.now();
  jobProg.submitting = true;
  if (!jobProg.lastApiKind) jobProg.lastApiKind = kind;
  jobProg.filename = jobLabelForAssets(assetIds);
  jobProg.message = t("job.submitting");
  jobProg.batchTotal = Math.max(assetIds.length, 1);
  jobProg.batchIdx = 1;
  jobProg.cloudTasks = [];
  showJobFloat();
  const pill = $("#job-pill");
  if (pill) {
    pill.textContent = kind === "export" ? t("job.exporting") : t("job.busy");
    pill.classList.add("busy");
    pill.dataset.busy = "1";
  }
  renderJobFloat();
}

function prepareJobUi(kind, assetIds) {
  markJobSubmitting(kind, assetIds);
}

function clearJobUi() {
  stopJobPoll();
  resetJobTracking();
  hideJobFloat();
  const pill = $("#job-pill");
  if (pill) {
    pill.textContent = t("job.ready");
    pill.classList.remove("busy");
    delete pill.dataset.busy;
  }
  jobProg.cancelling = false;
}

function collectCloudTaskFailures() {
  return (jobProg.cloudTasks || []).filter(
    (task) => String(task.status || "").toUpperCase() === "FAILED",
  );
}

function finishJobUi({ quickFail = false, cloudFailures = null } = {}) {
  const wasGenerate = jobProg.lastApiKind === "generate";
  const failures = cloudFailures ?? collectCloudTaskFailures();
  stopJobPoll();
  resetJobTracking();
  hideJobFloat();
  const pill = $("#job-pill");
  if (pill) {
    pill.textContent = t("job.ready");
    pill.classList.remove("busy");
    delete pill.dataset.busy;
  }
  jobProg.cancelling = false;
  void notifyJobFinish({ quickFail, wasGenerate, cloudFailures: failures });
}

async function notifyJobFinish({ quickFail, wasGenerate, cloudFailures = [] }) {
  if (cloudFailures.length) {
    const lines = cloudFailures.map((task) => {
      const name = task.filename || task.asset_id || "…";
      const msg = task.message || t("cloud.taskFailed");
      return `${name}: ${msg}`;
    });
    toast(lines.join("\n"), { variant: "error" });
    return;
  }
  if (!wasGenerate && !quickFail) return;
  try {
    const data = await API.get("/api/logs?tab=生成&limit=40");
    const entries = data.entries || [];
    const failLine = [...entries].reverse().find((e) => {
      const m = logEntryText(e);
      return (
        /^FAIL\b/i.test(m) ||
        m.includes("未配置") ||
        m.includes("没有可生成") ||
        m.includes("不存在") ||
        m.includes("ComfyUI") ||
        m.includes("云 prompt") ||
        m.includes("即梦") ||
        m.includes("万相") ||
        m.includes("混元") ||
        m.includes("HTTP ")
      );
    });
    if (failLine) {
      toast(logEntryText(failLine), { variant: "error" });
      return;
    }
  } catch {
    /* ignore */
  }
  if (quickFail) {
    toast(t("toast.jobEndedCheckLogs"), { variant: "error" });
  }
}

function cloudTaskStatusLabel(status) {
  const key = {
    PENDING: "cloud.taskPending",
    SUBMITTING: "cloud.taskRunning",
    RUNNING: "cloud.taskRunning",
    DOWNLOADING: "cloud.taskDownloading",
    SUCCEEDED: "cloud.taskDone",
    FAILED: "cloud.taskFailed",
  }[String(status || "").toUpperCase()];
  return key ? t(key) : status || "";
}

function ingestJobProgress(p) {
  if (!p?.kind) return;
  const kind = p.kind;
  jobProg.phase = kind;
  if (kind === "cloud_batch") {
    jobProg.cloudTasks = Array.isArray(p.cloud_tasks) ? p.cloud_tasks : jobProg.cloudTasks;
    if (p.overall_pct != null) jobProg.stepPct = parseInt(p.overall_pct, 10) || jobProg.stepPct;
    if (p.filename) jobProg.filename = p.filename;
    if (p.index != null) jobProg.batchIdx = parseInt(p.index, 10) || jobProg.batchIdx || 0;
    if (p.total != null) jobProg.batchTotal = Math.max(parseInt(p.total, 10) || 1, 1);
    const running = jobProg.cloudTasks.find((task) =>
      ["RUNNING", "PENDING", "SUBMITTING", "DOWNLOADING"].includes(String(task.status || "").toUpperCase()),
    );
    if (running?.message) jobProg.message = running.message;
    else if (p.overall_pct != null) jobProg.stepPct = parseInt(p.overall_pct, 10) || jobProg.stepPct;
    return;
  }
  if (p.index != null) {
    jobProg.batchIdx = parseInt(p.index, 10) || jobProg.batchIdx || 0;
  }
  if (p.total != null) {
    jobProg.batchTotal = Math.max(parseInt(p.total, 10) || 1, 1);
  }
  if (p.filename) jobProg.filename = p.filename;
  if (Array.isArray(p.cloud_tasks) && p.cloud_tasks.length) {
    jobProg.cloudTasks = p.cloud_tasks;
  }
  if (kind === "batch") {
    if (!jobProg.cloudTasks?.length) jobProg.stepPct = 0;
  } else if (kind === "progress") {
    const val = parseInt(p.value, 10) || 0;
    const mx = Math.max(parseInt(p.max, 10) || 1, 1);
    jobProg.stepPct = Math.min(100, Math.floor((val / mx) * 100));
    if (p.message) jobProg.message = p.message;
  } else if (kind === "queue") {
    if (p.message) jobProg.message = p.message;
    jobProg.stepPct = Math.max(jobProg.stepPct, 8);
  } else if (kind === "running") {
    if (p.message) jobProg.message = p.message;
    const elapsed = parseInt(p.elapsed, 10) || 0;
    if (elapsed > 0) {
      jobProg.stepPct = Math.max(
        jobProg.stepPct,
        Math.min(92, Math.floor((elapsed / 90) * 88) + 4),
      );
    } else {
      jobProg.stepPct = Math.max(jobProg.stepPct, 12);
    }
  } else if (["status", "executing"].includes(kind) && p.message) {
    jobProg.message = p.message;
    if (kind === "executing") {
      jobProg.stepPct = Math.max(jobProg.stepPct, 20);
    }
  }
}

function overallJobPct() {
  if (jobProg.lastApiKind === "export") return null;
  if (jobProg.cloudTasks?.length) {
    const tasks = jobProg.cloudTasks;
    const sum = tasks.reduce((acc, task) => acc + (parseInt(task.pct, 10) || 0), 0);
    const cloudPart = sum / Math.max(tasks.length, 1) / 100;
    const total = Math.max(jobProg.batchTotal, 1);
    const idx = Math.max(Number(jobProg.batchIdx) || 0, 0);
    return Math.min(100, Math.max(0, Math.floor(((idx + cloudPart) / total) * 100)));
  }
  if (jobProg.phase === "cloud_batch" && jobProg.stepPct > 0) {
    return Math.min(100, jobProg.stepPct);
  }
  const idx = Number(jobProg.batchIdx);
  if (!Number.isFinite(idx) || idx < 1) {
    if (jobProg.stepPct > 0 && jobProg.message) return Math.min(100, jobProg.stepPct);
    return null;
  }
  const total = Math.max(jobProg.batchTotal, 1);
  return Math.min(
    100,
    Math.max(0, Math.floor(((idx - 1) + jobProg.stepPct / 100) / total * 100))
  );
}

function renderJobFloat() {
  const root = $("#job-float");
  const title = $("#job-float-title");
  const file = $("#job-float-file");
  const pctEl = $("#job-float-pct");
  const fill = $("#job-float-fill");
  const batch = $("#job-float-batch");
  const status = $("#job-float-status");
  const cancel = $("#job-float-cancel");
  const queueEl = $("#job-float-queue");
  if (!root) return;

  const isExport = jobProg.lastApiKind === "export";
  const queuedOnly = !jobProg.submitting && jobProg.queuedCount > 1 && jobProg.stepPct === 0 && !jobProg.message;
  if (title) {
    if (jobProg.submitting) {
      title.textContent = isExport ? t("job.exporting") : t("job.generating");
    } else if (queuedOnly && jobProg.queueItems.length) {
      title.textContent = t("job.queuedTitle", { n: jobProg.queuedCount });
    } else {
      title.textContent = isExport ? t("job.exporting") : t("job.generating");
    }
  }
  if (file) {
    if (jobProg.submitting) {
      file.textContent = jobProg.filename || t("job.submitting");
    } else {
      file.textContent = jobProg.filename || (isExport ? "…" : t("job.preparing"));
    }
  }

  const pct = overallJobPct();
  const cloudActive = (jobProg.cloudTasks || []).some((task) =>
    ["PENDING", "RUNNING", "SUBMITTING", "DOWNLOADING"].includes(String(task.status || "").toUpperCase()),
  );
  const waiting =
    jobProg.stepPct < 100 &&
    (["queue", "running", "status", "executing", "cloud_batch"].includes(jobProg.phase) || cloudActive);
  const hasCloudProgress = jobProg.phase === "cloud_batch" || (jobProg.cloudTasks?.length ?? 0) > 0;
  const indeterminate =
    isExport ||
    (jobProg.submitting && !hasCloudProgress) ||
    pct === null ||
    (pct === 0 && waiting && !jobProg.message);

  root.classList.toggle("is-indeterminate", indeterminate);

  if (pctEl) {
    if (indeterminate) pctEl.textContent = "…";
    else if (waiting && pct === 0 && jobProg.message) pctEl.textContent = "…";
    else pctEl.textContent = `${pct}%`;
  }
  if (fill) {
    if (indeterminate) fill.style.width = "";
    else fill.style.width = `${pct}%`;
  }
  if (batch) {
    const batchText =
      jobProg.batchTotal > 1 && jobProg.batchIdx ? `${jobProg.batchIdx}/${jobProg.batchTotal}` : "";
    const queueNote =
      jobProg.queueItems.length > 0
        ? t("job.queueWaiting", { n: jobProg.queueItems.length })
        : "";
    batch.textContent = [batchText, queueNote].filter(Boolean).join(" · ");
  }
  if (status) {
    status.textContent =
      jobProg.message || (jobProg.submitting ? t("job.submitting") : indeterminate ? t("job.preparing") : "");
  }
  if (cancel) {
    cancel.disabled = jobProg.cancelling;
    cancel.textContent = jobProg.cancelling ? t("job.cancelling") : t("job.cancel");
  }
  if (queueEl) {
    const items = jobProg.queueItems || [];
    if (items.length) {
      queueEl.classList.remove("hidden");
      queueEl.innerHTML = items
        .map(
          (item, idx) => `<li class="job-float-queue-item">
            <span class="job-float-queue-pos">${idx + 1}</span>
            <span class="job-float-queue-copy">
              <span class="job-float-queue-label">${esc(item.label || "…")}</span>
              <span class="job-float-queue-kind">${esc(item.kind === "export" ? t("job.exportShort") : t("job.generateShort"))}${item.count > 1 ? ` · ${item.count}` : ""}</span>
            </span>
          </li>`,
        )
        .join("");
    } else {
      queueEl.classList.add("hidden");
      queueEl.innerHTML = "";
    }
  }
  const cloudEl = $("#job-float-cloud");
  if (cloudEl) {
    const tasks = jobProg.cloudTasks || [];
    if (tasks.length) {
      cloudEl.classList.remove("hidden");
      cloudEl.innerHTML = tasks
        .map(
          (task) => `<li class="job-float-cloud-item">
            <span class="job-float-cloud-pct">${Math.max(0, Math.min(100, parseInt(task.pct, 10) || 0))}%</span>
            <span class="job-float-cloud-copy">
              <span class="job-float-cloud-label">${esc(task.filename || task.message || "…")}</span>
              <span class="job-float-cloud-kind">${esc(task.message || cloudTaskStatusLabel(task.status))}</span>
            </span>
          </li>`,
        )
        .join("");
    } else {
      cloudEl.classList.add("hidden");
      cloudEl.innerHTML = "";
    }
  }
}

function showJobFloat() {
  const root = $("#job-float");
  if (!root) return;
  root.classList.remove("hidden");
  void root.offsetWidth;
  requestAnimationFrame(() => root.classList.add("is-visible"));
  jobProg.visible = true;
  renderJobFloat();
}

function hideJobFloat() {
  const root = $("#job-float");
  if (!root || root.classList.contains("hidden")) return;
  root.classList.remove("is-visible");
  jobProg.visible = false;
  window.setTimeout(() => {
    if (!jobProg.visible) {
      root.classList.add("hidden");
      resetJobProg();
    }
  }, 280);
}

function updateJobFromApi(j) {
  const pill = $("#job-pill");
  jobProg.submitting = false;
  jobProg.queueItems = Array.isArray(j.queue) ? j.queue : [];
  jobProg.queuedCount = Math.max(parseInt(j.queued_count, 10) || 0, 0);
  const prog = j.progress || {};
  if (Array.isArray(j.cloud_tasks)) jobProg.cloudTasks = j.cloud_tasks;
  else if (Array.isArray(prog.cloud_tasks)) jobProg.cloudTasks = prog.cloud_tasks;

  const hasQueue = jobProg.queuedCount > 0 || jobProg.queueItems.length > 0;
  if (
    j.busy ||
    hasQueue ||
    prog.kind === "cloud_batch" ||
    (jobProg.cloudTasks?.length ?? 0) > 0
  ) {
    state.jobTrack.sawBusy = true;
  }

  if (j.busy) {
    if (!state.jobTrack.active) {
      state.jobTrack = { active: true, sawBusy: true, postOk: true, submittedAt: state.jobTrack.submittedAt || 0 };
    } else {
      state.jobTrack.sawBusy = true;
    }
    if (j.run_id != null) state.jobRunId = j.run_id;
    if (j.kind) jobProg.lastApiKind = j.kind;
    ingestJobProgress(prog);
    showJobFloat();
    renderJobFloat();
    if (pill) {
      pill.textContent = j.kind === "export" ? t("job.exporting") : t("job.busy");
      pill.classList.add("busy");
      pill.dataset.busy = "1";
    }
    return;
  }

  if (hasQueue) {
    state.jobTrack.active = true;
    state.jobTrack.sawBusy = true;
    showJobFloat();
    renderJobFloat();
    if (pill) {
      pill.textContent = t("job.queuedPill", { n: jobProg.queuedCount });
      pill.classList.add("busy");
      pill.dataset.busy = "1";
    }
    return;
  }

  if (!state.jobTrack.active && !state.jobTrack.sawBusy) {
    return;
  }

  if (state.jobTrack.active && state.jobTrack.postOk && !state.jobTrack.sawBusy) {
    const elapsed = Date.now() - (state.jobTrack.submittedAt || 0);
    if (elapsed < JOB_START_GRACE_MS) {
      showJobFloat();
      renderJobFloat();
      return;
    }
  }

  const quickFail = state.jobTrack.active && state.jobTrack.postOk && !state.jobTrack.sawBusy;
  if (prog?.kind) ingestJobProgress(prog);
  const cloudFailures = collectCloudTaskFailures();
  finishJobUi({ quickFail, cloudFailures });
  if (state.assetId) {
    loadPreview();
    loadPreviewInfo(state.assetId);
  }
  scanStatus();
}

function ensureJobPoll() {
  startJobPoll();
}

function clampFloatPosition(x, y, el) {
  if (!el) return { x, y };
  const pad = 8;
  const w = el.offsetWidth || 280;
  const h = el.offsetHeight || 100;
  return {
    x: Math.max(pad, Math.min(x, window.innerWidth - w - pad)),
    y: Math.max(pad, Math.min(y, window.innerHeight - h - pad)),
  };
}

function applyJobFloatPosition(x, y, el, persist = true) {
  const root = el || $("#job-float");
  if (!root) return;
  const p = clampFloatPosition(x, y, root);
  root.style.left = `${p.x}px`;
  root.style.top = `${p.y}px`;
  root.style.right = "auto";
  if (persist) {
    try {
      localStorage.setItem(JOB_FLOAT_POS_KEY, JSON.stringify(p));
    } catch {
      /* ignore */
    }
  }
  return p;
}

function initJobFloatPosition() {
  const root = $("#job-float");
  if (!root) return;
  try {
    const raw = localStorage.getItem(JOB_FLOAT_POS_KEY);
    if (raw) {
      const p = JSON.parse(raw);
      if (Number.isFinite(p.x) && Number.isFinite(p.y)) {
        applyJobFloatPosition(p.x, p.y, root);
        return;
      }
    }
  } catch {
    /* ignore */
  }
  root.style.top = "76px";
  root.style.right = "20px";
  root.style.left = "auto";
}

function bindJobFloat() {
  const root = $("#job-float");
  const handle = $("#job-float-handle");
  const cancel = $("#job-float-cancel");
  if (!root || !handle) return;
  initJobFloatPosition();

  let drag = null;

  handle.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    const rect = root.getBoundingClientRect();
    root.style.left = `${rect.left}px`;
    root.style.top = `${rect.top}px`;
    root.style.right = "auto";
    drag = {
      pointerId: e.pointerId,
      startX: e.clientX,
      startY: e.clientY,
      origX: rect.left,
      origY: rect.top,
      moved: false,
    };
    handle.setPointerCapture(e.pointerId);
    e.preventDefault();
  });

  handle.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.pointerId) return;
    const dx = e.clientX - drag.startX;
    const dy = e.clientY - drag.startY;
    if (!drag.moved && Math.abs(dx) + Math.abs(dy) < JOB_FLOAT_DRAG_THRESHOLD) return;
    drag.moved = true;
    root.classList.add("is-dragging");
    applyJobFloatPosition(drag.origX + dx, drag.origY + dy, root, false);
  });

  const endDrag = (e) => {
    if (!drag || e.pointerId !== drag.pointerId) return;
    const wasDrag = drag.moved;
    drag = null;
    root.classList.remove("is-dragging");
    try {
      handle.releasePointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
    if (wasDrag) {
      const x = parseInt(root.style.left, 10);
      const y = parseInt(root.style.top, 10);
      if (Number.isFinite(x) && Number.isFinite(y)) applyJobFloatPosition(x, y, root, true);
    }
  };

  handle.addEventListener("pointerup", endDrag);
  handle.addEventListener("pointercancel", endDrag);

  cancel?.addEventListener("click", async (e) => {
    e.stopPropagation();
    if (jobProg.cancelling) return;
    jobProg.cancelling = true;
    renderJobFloat();
    try {
      await API.post("/api/jobs/cancel");
      toast(t("toast.cancelRequested"));
    } catch (err) {
      toast(err.message);
      jobProg.cancelling = false;
      renderJobFloat();
    }
  });
}

async function refreshComfy() {
  const pill = $("#comfy-pill");
  if (!pill) return false;
  pill.classList.add("is-checking");
  const wasOnline = comfyOnlineLast;
  try {
    const data = await API.get("/api/comfyui/status");
    const ok = !!data.ok;
    pill.textContent = ok ? t("comfy.online") : t("comfy.offline");
    pill.classList.toggle("ok", ok);
    pill.title = data.message || "";
    comfyOnlineLast = ok;
    if (ok && (wasOnline === false || (wasOnline === null && !(state.checkpoints?.length)))) {
      void loadGenerationModels();
    } else if (!ok && wasOnline === true) {
      state.checkpoints = [];
      fillCheckpointSelects();
    }
    return ok;
  } catch {
    pill.textContent = "ComfyUI ?";
    pill.classList.remove("ok");
    pill.title = "";
    if (comfyOnlineLast === true) {
      state.checkpoints = [];
      fillCheckpointSelects();
    }
    comfyOnlineLast = false;
    return false;
  } finally {
    pill.classList.remove("is-checking");
  }
}

async function ensureComfyOnline() {
  const ok = await refreshComfy();
  if (ok) return true;
  const pill = $("#comfy-pill");
  toast(pill?.title || t("comfy.offline"), { variant: "error" });
  return false;
}

function startJobPoll() {
  stopJobPoll();
  const tick = async () => {
    try {
      const j = await API.get("/api/jobs/status");
      updateJobFromApi(j);
    } catch {
      /* ignore */
    }
  };
  tick();
  state.jobTimer = setInterval(tick, 500);
}

async function startGenerate(exportAfter = false, onlySelected = false) {
  const assets = onlySelected
    ? state.assets.filter((a) => selectedIds().includes(a.id))
    : state.assets.filter((a) => a.enabled !== false);
  const ids = onlySelected ? selectedIds() : categoryAssetIds();
  await runGenerate(ids, {
    exportAfter,
    emptyToastKey: onlySelected ? "toast.selectAsset" : "toast.noCategoryAssets",
    assetRows: assets,
  });
}

async function startExport(onlySelected = false) {
  const ids = onlySelected ? selectedIds() : categoryAssetIds();
  await runExport(ids, {
    emptyToastKey: onlySelected ? "toast.selectAsset" : "toast.noCategoryAssets",
  });
}

// ── 保存操作 ─────────────────────────────────────────────

async function saveBasic(e) {
  e?.preventDefault();
  if (!state.assetId) return;
  const submitBtn = e?.submitter || e?.target?.querySelector('[type="submit"]');
  await withBtnBusy(submitBtn, async () => {
    const form = $("#form-basic");
    const body = {
      filename: form.filename.value.trim(),
      category: form.category.value,
      width: parseInt(form.width.value, 10),
      height: parseInt(form.height.value, 10),
      seed: form.seed.value.trim(),
      subject: form.subject.value.trim(),
      enabled: form.enabled.checked,
      remove_bg_mode: form.querySelector('input[name="remove_bg_mode"]:checked')?.value || "inherit",
      checkpoint: $("#asset-ckpt")?.value || "",
    };
    state.assetFull = await API.put(`/api/assets/${state.assetId}`, body);
    toast(t("toast.saved"));
    updateGenerationTabsVisibility();
    if (body.category !== state.categoryId) {
      await bootstrap(false);
      await selectCategory(body.category);
      await selectAsset(state.assetId);
    } else {
      await loadAssetList();
    }
  }).catch((err) => {
    if (err) toast(err.message);
  });
}

async function saveCategory(e) {
  e?.preventDefault();
  if (!state.categoryId) return;
  const submitBtn = e?.submitter || e?.target?.querySelector('[type="submit"]');
  await withBtnBusy(submitBtn, async () => {
    const form = $("#form-category");
    const pathErr = await validatePathFields(form);
    if (pathErr) {
      toast(pathErr, { variant: "error" });
      return;
    }
    const body = {
      source: form.source.value.trim(),
      inbox: form.inbox.value.trim(),
      unity: form.unity.value.trim(),
      checkpoint: $("#cat-ckpt")?.value || "",
      alpha_matte: form.alpha_matte.checked ? "default" : "none",
      positive_common: form.positive_common.value,
      negative_common: form.negative_common.value,
    };
    await API.put(`/api/categories/${state.categoryId}`, body);
    toast(t("toast.categorySaved"));
    const data = await API.get("/api/categories");
    state.categories = data.categories || [];
    renderCategories();
  }).catch((err) => {
    if (err) toast(err.message);
  });
}

async function savePrompts(btn) {
  if (!state.assetId) return;
  await withBtnBusy(btn || document.querySelector('[data-action="save-prompts"]'), async () => {
    const segments = promptSegmentsFromForm();
    const body = {
      negative: $("#negative-text").value,
      ...segments,
      positive: composePromptPreview(segments),
      ...genModePayloadFromForm(),
    };
    state.assetFull = await API.put(`/api/assets/${state.assetId}`, body);
    updateGenModeUi();
    updatePromptMergedPreview();
    toast(t("toast.promptsSaved"));
  }).catch((err) => {
    if (err) toast(err.message);
  });
}

async function saveWorkflow(btn) {
  if (!state.assetId) return;
  await withBtnBusy(btn || document.querySelector('[data-action="save-wf"]'), async () => {
    await API.put(`/api/assets/${state.assetId}/workflow`, { text: $("#workflow-text").value });
    state.assetFull = await API.put(`/api/assets/${state.assetId}`, genModePayloadFromForm());
    updateGenModeUi();
    toast(t("toast.workflowSaved"));
  }).catch((err) => {
    if (err) toast(err.message);
  });
}

async function validateWorkflow(btn) {
  if (!state.assetId) return;
  await withBtnBusy(btn || document.querySelector('[data-action="validate-wf"]'), async () => {
    await API.post(`/api/assets/${state.assetId}/workflow/validate`, {
      text: $("#workflow-text").value,
    });
    toast(t("toast.jsonValid"));
  }).catch((err) => {
    if (err) toast(err.message);
  });
}

async function loadDefaultWorkflow(btn) {
  if (!state.assetId) return;
  await withBtnBusy(btn || document.querySelector('[data-action="load-default-wf"]'), async () => {
    const data = await API.get(`/api/assets/${state.assetId}/workflow/default`);
    $("#workflow-text").value = data.text || "";
    const sel = $("#workflow-preset-select");
    if (sel && data.preset_id) sel.value = data.preset_id;
    updateWorkflowPresetDesc();
    toast(t("toast.defaultTemplateLoaded"));
  }).catch((err) => {
    if (err) toast(err.message);
  });
}

async function saveSettings(e) {
  e?.preventDefault();
  const submitBtn = e?.submitter || e?.target?.querySelector('[type="submit"]');
  await withBtnBusy(submitBtn, async () => {
    const form = $("#form-settings");
    const pathErr = await validatePathFields(form);
    if (pathErr) {
      toast(pathErr, { variant: "error" });
      return;
    }
    const body = {};
    for (const el of form.elements) {
      if (!el.name || el.type === "submit") continue;
      if (el.name.startsWith("cloud_api_keys.")) continue;
      body[el.name] = el.type === "number" ? Number(el.value) : el.value;
    }
    body.cloud_api_keys = {};
    for (const el of form.elements) {
      if (!el.name?.startsWith("cloud_api_keys.")) continue;
      const key = el.name.slice("cloud_api_keys.".length);
      body.cloud_api_keys[key] = el.value.trim();
    }
    await API.put("/api/settings", body);
    toast(t("toast.settingsSaved"));
    await loadGenerationModels();
    await loadSettingsForm();
  }).catch((err) => {
    if (err) toast(err.message);
  });
}

async function refreshUiAfterAi(applied, assetId = state.assetId) {
  if (!applied?.length || !assetId) return;
  if (state.assetId !== assetId) return;
  state.assetFull = null;
  const categoryChanged = applied.includes("category");
  const catSettingsChanged = applied.some((k) => k.startsWith("cat."));
  const basicFields = new Set([
    "filename",
    "category",
    "width",
    "height",
    "seed",
    "subject",
    "enabled",
    "remove_bg_mode",
    "checkpoint",
  ]);
  const promptFields = new Set([
    "positive",
    "negative",
    "positive_prefix",
    "positive_subject",
    "positive_scene",
    "positive_light",
    "positive_g",
    "positive_l",
    "gen_mode",
    "ref_image",
    "img2img_denoise",
    "workflow",
    "cloud_prompt",
    "cloud_negative",
    "cloud_gen_mode",
    "cloud_strength",
  ]);

  if (categoryChanged) {
    const fresh = await API.get(`/api/assets/${assetId}`);
    if (state.assetId !== assetId) return;
    await bootstrap(false);
    await selectCategory(fresh.category);
    if (state.assetId !== assetId) return;
    await selectAsset(assetId);
    return;
  } else {
    if (applied.some((k) => basicFields.has(k))) await loadBasicForm();
    if (state.assetId !== assetId) return;
    if (applied.some((k) => k === "cloud_prompt" || k === "cloud_negative")) {
      state.assetFull = null;
      const fresh = await API.get(`/api/assets/${assetId}`);
      if (state.assetId !== assetId) return;
      state.assetFull = fresh;
      const genTab = effectiveGenTabId(fresh);
      if (genTab?.startsWith("cloud-")) switchTab(genTab);
      await loadCloudTab();
    } else if (applied.some((k) => promptFields.has(k))) {
      if (assetUsesCloudBackend()) await loadCloudTab();
      else await loadComfyUiTab();
    }
    if (state.assetId !== assetId) return;
    if (catSettingsChanged) {
      const fresh = await API.get(`/api/assets/${assetId}`);
      if (state.assetId !== assetId) return;
      if (state.categoryId === fresh.category) await loadCategoryForm();
    }
    if (applied.includes("filename")) await loadAssetList();
    else if (applied.some((k) => basicFields.has(k))) await loadAssetList();
  }
  if (state.assetId !== assetId) return;
  await loadPreview();
  await scanStatus();
  toast(t("toast.aiAutoSaved", { fields: applied.join(", ") }));
}

async function sendAi() {
  if (!state.assetId) {
    toast(t("toast.selectAsset"));
    return;
  }
  const assetId = state.assetId;
  const mode = state.aiMode;
  if (isAssetAiBusy(assetId)) return;
  const msg = $("#ai-input").value.trim();
  if (!msg) return;
  $("#ai-input").value = "";
  beginAiRequest(assetId);
  try {
    const data = await API.post("/api/ai/chat", {
      asset_id: assetId,
      message: msg,
      mode,
    });
    const stillViewing = assetId === state.assetId && mode === state.aiMode;
    if (stillViewing) {
      await refreshAiMessages();
      if (data.applied?.length) {
        await refreshUiAfterAi(data.applied, assetId);
      } else {
        const u = data.updates || {};
        const hasPromptIntent =
          u.cloud_prompt != null ||
          u.cloud_negative != null ||
          u.positive != null ||
          u.negative != null ||
          u.positive_prefix != null ||
          u.positive_subject != null ||
          u.positive_scene != null ||
          u.positive_light != null;
        if (hasPromptIntent) {
          toast(t("toast.aiPromptNotApplied"));
        } else {
          toast(t("toast.aiReplyOnly"));
        }
      }
    }
  } catch (err) {
    const stillViewing = assetId === state.assetId && mode === state.aiMode;
    if (stillViewing) {
      toast(err.message);
      await refreshAiMessages();
      const hasFailed = (state.aiChatHistory || []).some(
        (item) => item.failed && item.retry_message === msg,
      );
      if (!hasFailed) appendFailedAiTurn(msg, err.message);
    }
  } finally {
    endAiRequest(assetId);
    if (assetId === state.assetId) updateAiModeUi();
  }
}

async function openAssetFile(assetId, kind) {
  try {
    const paths = await API.get(`/api/assets/${assetId}/paths`);
    const file = paths[kind];
    if (!file) {
      toast(t("toast.noPath"));
      return;
    }
    await openPath(file);
  } catch (err) {
    toast(err.message);
  }
}

function setPreviewSourceKey(key) {
  const next = key || "inbox";
  state.previewSource = next;
  $$(".preview-side .seg-btn").forEach((btn) => {
    btn.classList.toggle("active", (btn.dataset.source || "inbox") === next);
  });
  if (state.assetId) {
    loadPreview();
    if (state.previewInfo) renderPreviewInfo(state.previewInfo);
  }
}

/** source 预览下打开后处理：inbox / source / 取消 */
function showPostprocessSourceChoiceDialog() {
  const dlg = $("#dlg-pp-subject");
  const titleEl = $("#pp-subject-title");
  const msgEl = $("#pp-subject-message");
  const listEl = $("#pp-subject-details");
  const inboxBtn = $("#pp-subject-inbox");
  const sourceBtn = $("#pp-subject-source");
  const cancelBtn = $("#pp-subject-cancel");
  if (!dlg || !titleEl || !msgEl || !listEl || !inboxBtn || !sourceBtn || !cancelBtn) {
    return Promise.resolve(null);
  }

  titleEl.textContent = t("pp.sourceChoiceTitle");
  msgEl.textContent = t("pp.sourceChoiceMessage");
  listEl.innerHTML = [
    t("pp.sourceChoiceDetailInbox"),
    t("pp.sourceChoiceDetailRestore"),
    t("pp.sourceChoiceDetailSource"),
  ]
    .map((line) => `<li>${esc(line)}</li>`)
    .join("");
  inboxBtn.textContent = t("pp.sourceChoiceInbox");
  sourceBtn.textContent = t("pp.sourceChoiceSource");
  cancelBtn.textContent = t("dlg.cancel");

  dlg.showModal();
  return new Promise((resolve) => {
    const finish = (val) => {
      dlg.close();
      inboxBtn.removeEventListener("click", onInbox);
      sourceBtn.removeEventListener("click", onSource);
      cancelBtn.removeEventListener("click", onCancel);
      dlg.removeEventListener("cancel", onCancel);
      resolve(val);
    };
    const onInbox = () => finish("inbox");
    const onSource = () => finish("source");
    const onCancel = (e) => {
      e?.preventDefault?.();
      finish(null);
    };
    inboxBtn.addEventListener("click", onInbox);
    sourceBtn.addEventListener("click", onSource);
    cancelBtn.addEventListener("click", onCancel);
    dlg.addEventListener("cancel", onCancel);
  });
}

async function launchPostprocessWindow(assetId, subject) {
  try {
    const prep = await API.post(`/api/assets/${encodeURIComponent(assetId)}/postprocess/prepare`, {
      subject_path: subject,
    });
    if (prep.inbox_initialized) {
      toast(t("toast.inboxFromSource"));
    }
  } catch (err) {
    toast(err.message);
    return;
  }
  const q = new URLSearchParams({ asset: assetId, subject });
  const returnUrl = `/?asset=${encodeURIComponent(assetId)}`;
  try {
    sessionStorage.setItem("artApp.ppReturn", returnUrl);
    sessionStorage.setItem(
      "artApp.ppSkipCanvasSync",
      JSON.stringify({ assetId, subject }),
    );
  } catch {
    /* ignore */
  }
  location.assign(`/postprocess.html?${q}`);
}

function openPostprocess(assetId) {
  return openPostprocessAsync(assetId);
}

async function openPostprocessAsync(assetId) {
  let subject = currentPreviewSourceKey();

  if (subject === "source") {
    const choice = await showPostprocessSourceChoiceDialog();
    if (!choice) return;
    if (choice === "inbox") {
      subject = "inbox";
      setPreviewSourceKey("inbox");
    }
  } else if (subject === "unity") {
    if (!confirm(t("pp.confirmOpenUnityEdit"))) return;
  }

  await launchPostprocessWindow(assetId, subject);
}

function renameConflictKindLabel(kind) {
  if (kind === "source") return t("confirm.moveAsset.kindSource");
  if (kind === "inbox") return t("confirm.moveAsset.kindInbox");
  if (kind === "unity") return t("confirm.moveAsset.kindUnity");
  return kind;
}

async function postAssetRename(assetId, filename, overwrite = false) {
  return API.post(`/api/assets/${assetId}/rename`, { filename, overwrite });
}

async function finishAssetRename(assetId, data) {
  const n = data.renamed?.length || 0;
  const suffix = data.overwritten ? t("toast.renamedOverwrite") : "";
  toast(
    (n > 0 ? t("toast.renamed", { n, name: data.filename }) : t("toast.renamedConfig", { name: data.filename })) +
      suffix
  );
  if (assetId === state.assetId) {
    state.assetFull = null;
    await loadBasicForm();
    await loadPaths();
    await loadPreview();
  }
  await loadAssetList();
  await scanStatus();
}

async function renameAssetDialog(assetId) {
  let filename = state.assets.find((a) => a.id === assetId)?.filename;
  if (!filename) {
    const data = await API.get(`/api/assets/${assetId}`);
    filename = data.filename;
  }
  const dlg = $("#dlg-rename-asset");
  const form = $("#form-rename-asset");
  if (!dlg || !form) return;
  form.reset();
  form.filename.value = filename || "";
  dlg.showModal();
  form.onsubmit = async (e) => {
    e.preventDefault();
    const submitBtn = e.submitter || form.querySelector('[type="submit"]');
    const newName = form.filename.value.trim();
    if (!newName || newName === filename) {
      dlg.close();
      return;
    }
    await withBtnBusy(submitBtn, async () => {
      try {
        const data = await postAssetRename(assetId, newName);
        dlg.close();
        await finishAssetRename(assetId, data);
      } catch (err) {
        if (err?.code !== "rename_file_conflict" || !err.detail?.can_overwrite) {
          throw err;
        }
        const conflicts = err.detail.conflicts || [];
        const targetName = err.detail.filename || newName;
        const details = [
          t("confirm.renameOverwrite.detailHint"),
          ...conflicts.map((c) =>
            t("confirm.renameOverwrite.detailPath", {
              kind: renameConflictKindLabel(c.kind),
              path: c.path,
            })
          ),
        ];
        const ok = await showConfirmDialog({
          title: t("confirm.renameOverwrite.title"),
          message: t("confirm.renameOverwrite.message", { name: targetName }),
          details,
          confirmText: t("confirm.renameOverwrite.confirm"),
          danger: true,
        });
        if (!ok) return;
        const data = await postAssetRename(assetId, newName, true);
        dlg.close();
        await finishAssetRename(assetId, data);
      }
    }).catch((err) => {
      if (err) toast(err.message);
    });
  };
}

async function deleteAsset(assetId) {
  if (!assetId) return;
  const asset = state.assets.find((a) => a.id === assetId);
  const filename = asset?.filename || assetId;
  const ok = await showConfirmDialog({
    title: t("confirm.deleteAsset.title"),
    message: t("confirm.deleteAsset.message", { name: filename, id: assetId }),
    details: [t("confirm.deleteAsset.detailConfig"), t("confirm.deleteAsset.detailFiles")],
    confirmText: t("confirm.deleteAsset.confirm"),
    danger: true,
  });
  if (!ok) return;
  try {
    await API.del(`/api/assets/${assetId}`);
    state.selectedAssetIds = state.selectedAssetIds.filter((id) => id !== assetId);
    if (state.assetId === assetId) {
      state.assetId = null;
      state.assetFull = null;
      clearPreview();
    }
    await loadAssetList();
    updateMainTabsVisibility();
    if (!categoryHasAssets()) switchTab("category");
  } catch (err) {
    toast(err.message);
  }
}

async function deleteAssets(assetIds) {
  const ids = [...new Set((assetIds || []).filter(Boolean))];
  if (!ids.length) return;
  if (ids.length === 1) {
    await deleteAsset(ids[0]);
    return;
  }
  const ok = await showConfirmDialog({
    title: t("confirm.deleteAssets.title"),
    message: t("confirm.deleteAssets.message", { n: ids.length }),
    details: [t("confirm.deleteAssets.detailConfig"), t("confirm.deleteAssets.detailFiles")],
    confirmText: t("confirm.deleteAssets.confirm"),
    danger: true,
  });
  if (!ok) return;
  showGlobalOverlay(t("toast.deleteAssetsWorking", { cur: 0, total: ids.length }));
  let deleted = 0;
  let errors = 0;
  try {
    for (let i = 0; i < ids.length; i++) {
      const id = ids[i];
      showGlobalOverlay(t("toast.deleteAssetsWorking", { cur: i + 1, total: ids.length }));
      try {
        await API.del(`/api/assets/${id}`);
        deleted++;
        state.selectedAssetIds = state.selectedAssetIds.filter((sid) => sid !== id);
        if (state.assetId === id) {
          state.assetId = null;
          state.assetFull = null;
          clearPreview();
        }
      } catch {
        errors++;
      }
    }
  } finally {
    hideGlobalOverlay();
  }
  await loadAssetList();
  updateMainTabsVisibility();
  if (!categoryHasAssets()) switchTab("category");
  if (errors) toast(t("toast.batchPartialFail", { errors, total: ids.length }), { variant: "error" });
  else if (deleted) toast(t("toast.deleteAssetsDone", { n: deleted }));
}

async function refreshAssetEntry(assetId, { bustCache = true } = {}) {
  await selectAsset(assetId);
  await scanStatus();
  await loadPreview({ bustCache });
}

async function refreshAssetEntries(assetIds, { bustCache = true } = {}) {
  const ids = [...new Set((assetIds || []).filter(Boolean))];
  if (!ids.length) return;
  if (ids.length === 1) {
    await refreshAssetEntry(ids[0], { bustCache });
    return;
  }
  showGlobalOverlay(t("toast.refreshWorking"));
  try {
    await loadAssetList();
    await scanStatus();
    if (state.assetId && ids.includes(state.assetId)) {
      await loadPreview({ bustCache });
      await loadPreviewInfo(state.assetId);
    }
  } finally {
    hideGlobalOverlay();
  }
}

async function switchPreviewToInboxIfCurrent(assetIds) {
  if (!state.assetId || !assetIds.includes(state.assetId)) return;
  const keepSelection = [...state.selectedAssetIds];
  state.previewSource = "inbox";
  $$(".preview-side .seg-btn").forEach((btn) => {
    btn.classList.toggle("active", (btn.dataset.source || "inbox") === "inbox");
  });
  await loadAssetList();
  await scanStatus();
  await loadPreview({ bustCache: true });
  await loadPreviewInfo(state.assetId);
  state.selectedAssetIds = keepSelection.filter((id) => state.assets.some((a) => a.id === id));
  renderAssets();
}

async function removeMagentaFromAssets(assetIds) {
  const ids = [...new Set((assetIds || []).filter(Boolean))];
  if (!ids.length) return;
  const total = ids.length;
  showGlobalOverlay(t("toast.removeMagentaWorking", { cur: 1, total }));
  let changed = 0;
  let skipped = 0;
  let errors = 0;
  try {
    for (let i = 0; i < ids.length; i++) {
      const id = ids[i];
      showGlobalOverlay(t("toast.removeMagentaWorking", { cur: i + 1, total }));
      try {
        const r = await API.post(`/api/assets/${encodeURIComponent(id)}/remove-magenta`);
        if (r.changed) changed++;
        else skipped++;
      } catch {
        errors++;
      }
    }
  } finally {
    hideGlobalOverlay();
  }
  if (changed) {
    if (ids.includes(state.assetId)) {
      await switchPreviewToInboxIfCurrent(ids);
    } else {
      await loadAssetList();
      await scanStatus();
    }
  } else if (!errors) {
    await loadAssetList();
  }
  if (errors) {
    toast(t("toast.batchPartialFail", { errors, total }), { variant: "error" });
  } else if (changed) {
    toast(
      total > 1
        ? t("toast.batchRemoveMagentaDone", { changed, skipped })
        : t("toast.removeMagentaDone"),
    );
  } else {
    toast(t("toast.removeMagentaNoChange"));
  }
}

async function removeMagentaFromAsset(assetId) {
  await removeMagentaFromAssets([assetId]);
}

async function autoTrimInboxAssets(assetIds) {
  const ids = [...new Set((assetIds || []).filter(Boolean))];
  if (!ids.length) return;
  const total = ids.length;
  showGlobalOverlay(t("toast.autoTrimWorking", { cur: 1, total }));
  let changed = 0;
  let skipped = 0;
  let errors = 0;
  let lastResult = null;
  try {
    for (let i = 0; i < ids.length; i++) {
      const id = ids[i];
      showGlobalOverlay(t("toast.autoTrimWorking", { cur: i + 1, total }));
      try {
        const r = await API.post(`/api/assets/${encodeURIComponent(id)}/auto-trim-inbox`);
        lastResult = r;
        if (r.unchanged) skipped++;
        else changed++;
      } catch {
        errors++;
      }
    }
  } finally {
    hideGlobalOverlay();
  }
  if (changed) {
    if (ids.includes(state.assetId)) {
      await switchPreviewToInboxIfCurrent(ids);
    } else {
      await loadAssetList();
      await scanStatus();
    }
  } else if (!errors) {
    await loadAssetList();
  }
  if (errors) {
    toast(t("toast.batchPartialFail", { errors, total }), { variant: "error" });
  } else if (changed) {
    if (total > 1) {
      toast(t("toast.batchAutoTrimDone", { changed, skipped }));
    } else if (lastResult) {
      const r = lastResult;
      const canvas = r.canvas;
      if (canvas?.changed) {
        toast(
          t("toast.autoTrimDoneWithCanvas", {
            w: r.width,
            h: r.height,
            ow: r.old_width,
            oh: r.old_height,
            cw: canvas.canvas_width,
            ch: canvas.canvas_height,
            ocw: canvas.old_canvas_width,
            och: canvas.old_canvas_height,
          }),
        );
      } else {
        toast(
          t("toast.autoTrimDone", {
            w: r.width,
            h: r.height,
            ow: r.old_width,
            oh: r.old_height,
          }),
        );
      }
    }
  } else {
    toast(t("toast.autoTrimNoChange"));
  }
}

async function autoTrimInboxAsset(assetId) {
  await autoTrimInboxAssets([assetId]);
}

let assetCtxIds = [];

const ASSET_CTX_SINGLE_ONLY = new Set([
  "rename",
  "duplicate",
  "postprocess",
  "open-source",
  "open-inbox",
  "open-unity",
]);

const ASSET_CTX_LABEL_KEYS = {
  "gen-one": ["ctx.genOne", "ctx.genMany"],
  "gen-one-export": ["ctx.genOneExport", "ctx.genManyExport"],
  "export-one": ["ctx.exportOne", "ctx.exportMany"],
  "remove-magenta": ["ctx.removeMagenta", "ctx.removeMagentaMany"],
  "auto-trim": ["ctx.autoTrim", "ctx.autoTrimMany"],
  refresh: ["ctx.refresh", "ctx.refreshMany"],
  delete: ["ctx.delete", "ctx.deleteMany"],
};

function updateAssetCtxMenu(ids) {
  const menu = $("#asset-ctx-menu");
  if (!menu) return;
  const n = ids.length;
  const multi = n > 1;
  for (const [act, [singleKey, multiKey]] of Object.entries(ASSET_CTX_LABEL_KEYS)) {
    const btn = menu.querySelector(`[data-ctx="${act}"]`);
    if (!btn) continue;
    btn.textContent = multi ? t(multiKey, { n }) : t(singleKey);
  }
  menu.querySelectorAll("[data-ctx]").forEach((btn) => {
    const act = btn.dataset.ctx;
    const disabled = multi && ASSET_CTX_SINGLE_ONLY.has(act);
    btn.disabled = disabled;
    btn.classList.toggle("is-disabled", disabled);
    btn.setAttribute("aria-disabled", disabled ? "true" : "false");
  });
}

function showAssetCtxMenu(x, y, assetId) {
  closeNavMenus();
  hideCatCtxMenu();
  const menu = $("#asset-ctx-menu");
  if (!menu) return;
  if (!isAssetRowSelected(assetId)) {
    state.selectedAssetIds = [assetId];
    state.lastAssetClickId = assetId;
    renderAssets();
  }
  assetCtxIds = effectiveSelectedIds();
  updateAssetCtxMenu(assetCtxIds);
  openFloatingMenu(menu, () => {
    menu.style.left = "0px";
    menu.style.top = "0px";
    const pad = 8;
    const rect = menu.getBoundingClientRect();
    const maxX = window.innerWidth - rect.width - pad;
    const maxY = window.innerHeight - rect.height - pad;
    menu.style.left = `${Math.max(pad, Math.min(x, maxX))}px`;
    menu.style.top = `${Math.max(pad, Math.min(y, maxY))}px`;
  });
}

const assetDrag = {
  active: false,
  suppressClick: false,
  assetId: null,
  fromCategory: null,
  dropCatId: null,
  row: null,
  ghost: null,
  timer: null,
  pointerId: null,
  startX: 0,
  startY: 0,
};

const ASSET_DRAG_LONG_PRESS_MS = 450;
const ASSET_DRAG_MOVE_THRESHOLD = 10;

function clearCategoryDropTargets() {
  $$(".cat-item.drop-target").forEach((el) => el.classList.remove("drop-target"));
}

function positionAssetDragGhost(x, y) {
  const ghost = assetDrag.ghost;
  if (!ghost) return;
  ghost.style.left = `${x + 12}px`;
  ghost.style.top = `${y + 12}px`;
}

function cleanupAssetDrag() {
  clearTimeout(assetDrag.timer);
  assetDrag.timer = null;
  assetDrag.active = false;
  assetDrag.assetId = null;
  assetDrag.fromCategory = null;
  assetDrag.dropCatId = null;
  assetDrag.pointerId = null;
  assetDrag.row?.classList.remove("is-drag-source");
  assetDrag.row = null;
  if (assetDrag.ghost) {
    assetDrag.ghost.remove();
    assetDrag.ghost = null;
  }
  clearCategoryDropTargets();
}

function startAssetDrag(e, row, assetId) {
  const asset = state.assets.find((a) => a.id === assetId);
  assetDrag.active = true;
  assetDrag.suppressClick = true;
  assetDrag.assetId = assetId;
  assetDrag.fromCategory = state.categoryId;
  assetDrag.row = row;
  row.classList.add("is-drag-source");

  const ghost = document.createElement("div");
  ghost.className = "asset-drag-ghost";
  ghost.innerHTML = `<span class="asset-drag-ghost-label">${esc(asset?.filename || assetId)}</span>`;
  document.body.appendChild(ghost);
  assetDrag.ghost = ghost;
  positionAssetDragGhost(e.clientX, e.clientY);
  try {
    row.setPointerCapture(e.pointerId);
  } catch {
    /* ignore */
  }
  toast(t("toast.assetDragHint"));
}

function updateAssetDragTarget(clientX, clientY) {
  clearCategoryDropTargets();
  assetDrag.dropCatId = null;
  const el = document.elementFromPoint(clientX, clientY);
  const cat = el?.closest?.(".cat-item");
  if (!cat?.dataset.catId) return;
  if (cat.dataset.catId === assetDrag.fromCategory) return;
  cat.classList.add("drop-target");
  assetDrag.dropCatId = cat.dataset.catId;
}

function onAssetDragPointerMove(e) {
  if (assetDrag.timer && !assetDrag.active) {
    const dx = e.clientX - assetDrag.startX;
    const dy = e.clientY - assetDrag.startY;
    if (Math.hypot(dx, dy) > ASSET_DRAG_MOVE_THRESHOLD) {
      clearTimeout(assetDrag.timer);
      assetDrag.timer = null;
    }
    return;
  }
  if (!assetDrag.active) return;
  if (assetDrag.pointerId != null && e.pointerId !== assetDrag.pointerId) return;
  e.preventDefault();
  positionAssetDragGhost(e.clientX, e.clientY);
  updateAssetDragTarget(e.clientX, e.clientY);
}

async function confirmMoveAssetToCategory(assetId, targetCatId, fromCategoryId) {
  const asset = state.assets.find((a) => a.id === assetId);
  const fromCat = state.categories.find((c) => c.id === fromCategoryId);
  const toCat = state.categories.find((c) => c.id === targetCatId);
  if (!asset || !toCat) return;

  let preview;
  try {
    preview = await API.get(
      `/api/assets/${encodeURIComponent(assetId)}/move-preview?category=${encodeURIComponent(targetCatId)}`,
    );
  } catch (err) {
    toast(err.message, { variant: "error" });
    return;
  }
  if (preview.same_category) return;

  const fromLabel = preview.from_label || fromCat?.label || preview.from_category;
  const toLabel = preview.to_label || toCat.label;
  const details = [
    t("confirm.moveAsset.detailWarning"),
    t("confirm.moveAsset.detailCaution"),
  ];
  const kindLabels = {
    source: t("confirm.moveAsset.kindSource"),
    inbox: t("confirm.moveAsset.kindInbox"),
    unity: t("confirm.moveAsset.kindUnity"),
  };
  for (const file of preview.files || []) {
    const kind = kindLabels[file.kind] || file.kind;
    if (file.exists) {
      details.push(t("confirm.moveAsset.detailMove", { kind, from: file.from, to: file.to }));
    } else {
      details.push(t("confirm.moveAsset.detailSkip", { kind }));
    }
  }

  const ok = await showConfirmDialog({
    title: t("confirm.moveAsset.title"),
    message: t("confirm.moveAsset.message", {
      name: asset.filename,
      fromLabel,
      toLabel,
    }),
    details,
    confirmText: t("confirm.moveAsset.confirm"),
    danger: true,
  });
  if (!ok) return;

  try {
    const data = await API.post(`/api/assets/${encodeURIComponent(assetId)}/move-category`, {
      category: targetCatId,
    });
    const moved = data.moved?.length || 0;
    toast(t("toast.assetMoved", { label: toLabel, n: moved }));
    const wasSource = state.categoryId === preview.from_category;
    const wasTarget = state.categoryId === targetCatId;
    if (wasSource || wasTarget) {
      await loadAssetList();
      updateMainTabsVisibility();
    }
    if (wasTarget && data.asset?.id) {
      await selectAsset(data.asset.id);
    } else if (state.assetId === assetId && wasSource) {
      state.assetId = null;
      state.assetFull = null;
      clearPreview();
      if (!categoryHasAssets()) switchTab("category");
    }
    scanStatus();
  } catch (err) {
    toast(err.message, { variant: "error" });
  }
}

async function onAssetDragPointerEnd(e) {
  clearTimeout(assetDrag.timer);
  assetDrag.timer = null;
  if (!assetDrag.active) return;
  if (assetDrag.pointerId != null && e.pointerId !== assetDrag.pointerId) return;

  const targetCatId = assetDrag.dropCatId;
  const assetId = assetDrag.assetId;
  const fromCategoryId = assetDrag.fromCategory;
  cleanupAssetDrag();
  if (targetCatId && assetId) {
    await confirmMoveAssetToCategory(assetId, targetCatId, fromCategoryId);
  }
}

function bindAssetDragToCategory() {
  const list = $("#asset-list");
  if (!list) return;

  list.addEventListener(
    "pointerdown",
    (e) => {
      if (e.button !== 0 && e.pointerType === "mouse") return;
      const row = e.target.closest(".asset-row");
      if (!row || e.target.closest(".path-chip")) return;
      const assetId = row.dataset.id;
      if (!assetId || !state.categoryId) return;

      assetDrag.pointerId = e.pointerId;
      assetDrag.startX = e.clientX;
      assetDrag.startY = e.clientY;
      assetDrag.row = row;
      clearTimeout(assetDrag.timer);
      assetDrag.timer = setTimeout(() => {
        assetDrag.timer = null;
        startAssetDrag(e, row, assetId);
      }, ASSET_DRAG_LONG_PRESS_MS);
    },
    { passive: true },
  );

  window.addEventListener("pointermove", onAssetDragPointerMove, { passive: false });
  window.addEventListener("pointerup", onAssetDragPointerEnd);
  window.addEventListener("pointercancel", () => cleanupAssetDrag());
}

function closeNavMenus() {
  closeAllFloatingMenus(["#asset-ctx-menu", "#cat-ctx-menu"]);
}

/** 现代化确认对话框；返回 true 表示用户确认 */
function showConfirmDialog({
  title,
  message,
  details = [],
  confirmText,
  cancelText,
  danger = true,
}) {
  const dlg = $("#dlg-confirm");
  const icon = $("#confirm-icon");
  const titleEl = $("#confirm-title");
  const msgEl = $("#confirm-message");
  const listEl = $("#confirm-details");
  const okBtn = $("#confirm-ok");
  const cancelBtn = $("#confirm-cancel");
  if (!dlg || !titleEl || !msgEl || !okBtn || !cancelBtn) return Promise.resolve(false);

  titleEl.textContent = title || "";
  msgEl.textContent = message || "";
  icon?.classList.toggle("danger", danger);
  okBtn.textContent = confirmText || t("dlg.confirm");
  okBtn.className = danger ? "btn danger" : "btn primary";
  cancelBtn.textContent = cancelText || t("dlg.cancel");

  if (listEl) {
    const items = details.filter(Boolean);
    listEl.innerHTML = items.map((line) => `<li>${esc(line)}</li>`).join("");
    listEl.classList.toggle("hidden", items.length === 0);
  }

  dlg.showModal();
  return new Promise((resolve) => {
    const finish = (val) => {
      dlg.close();
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      dlg.removeEventListener("cancel", onCancel);
      resolve(val);
    };
    const onOk = () => finish(true);
    const onCancel = (e) => {
      e?.preventDefault?.();
      finish(false);
    };
    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    dlg.addEventListener("cancel", onCancel);
  });
}

async function renameCategoryDialog(catId) {
  const cat = state.categories.find((c) => c.id === catId);
  if (!cat) return;
  const dlg = $("#dlg-rename-category");
  const form = $("#form-rename-category");
  const idHint = $("#rename-category-id");
  if (!dlg || !form) return;
  form.reset();
  form.label.value = cat.label;
  if (idHint) idHint.textContent = `ID: ${cat.id}`;
  dlg.showModal();
  form.onsubmit = async (e) => {
    e.preventDefault();
    const submitBtn = e.submitter || form.querySelector('[type="submit"]');
    const label = form.label.value.trim();
    if (!label || label === cat.label) {
      dlg.close();
      return;
    }
    await withBtnBusy(submitBtn, async () => {
      await API.put(`/api/categories/${catId}`, { label });
      dlg.close();
      toast(t("toast.categoryRenamed"));
      const data = await API.get("/api/categories");
      state.categories = data.categories || [];
      renderCategories();
      if (state.categoryId === catId) await loadCategoryForm();
    }).catch((err) => {
      if (err) toast(err.message);
    });
  };
}

async function deleteCategory(catId) {
  const cat = state.categories.find((c) => c.id === catId);
  if (!cat) return;
  let assetCount = 0;
  try {
    const data = await API.get(`/api/assets?category=${encodeURIComponent(catId)}`);
    assetCount = data.count ?? (data.assets?.length || 0);
  } catch {
    /* ignore */
  }
  const details = [t("confirm.deleteCategory.detailConfig")];
  if (assetCount > 0) {
    details.push(t("confirm.deleteCategory.detailAssets", { n: assetCount }));
  }
  details.push(t("confirm.deleteCategory.detailFiles"));
  const ok = await showConfirmDialog({
    title: t("confirm.deleteCategory.title"),
    message: t("confirm.deleteCategory.message", { name: cat.label, id: cat.id }),
    details,
    confirmText: t("confirm.deleteCategory.confirm"),
    danger: true,
  });
  if (!ok) return;
  try {
    await API.del(`/api/categories/${catId}`);
    toast(t("toast.categoryDeleted"));
    const wasCurrent = state.categoryId === catId;
    if (wasCurrent) {
      state.categoryId = null;
      state.assetId = null;
      state.assetFull = null;
      state.assets = [];
      state.statusMap = {};
      clearPreview();
    }
    const data = await API.get("/api/categories");
    state.categories = data.categories || [];
    renderCategories();
    if (state.categories.length) {
      await selectCategory(wasCurrent ? state.categories[0].id : state.categoryId || state.categories[0].id);
    } else {
      updateMainTabsVisibility();
      switchTab("settings");
    }
  } catch (err) {
    toast(err.message);
  }
}

let catCtxId = null;

function hideCatCtxMenu() {
  catCtxId = null;
  closeFloatingMenu($("#cat-ctx-menu"));
}

function showCatCtxMenu(x, y, catId) {
  closeNavMenus();
  hideAssetCtxMenu();
  const menu = $("#cat-ctx-menu");
  if (!menu) return;
  catCtxId = catId;
  openFloatingMenu(menu, () => {
    menu.style.left = "0px";
    menu.style.top = "0px";
    const pad = 8;
    const rect = menu.getBoundingClientRect();
    const maxX = window.innerWidth - rect.width - pad;
    const maxY = window.innerHeight - rect.height - pad;
    menu.style.left = `${Math.max(pad, Math.min(x, maxX))}px`;
    menu.style.top = `${Math.max(pad, Math.min(y, maxY))}px`;
  });
}

function bindCategoryContextMenu() {
  const list = $("#cat-list");
  const menu = $("#cat-ctx-menu");
  if (!list || !menu) return;

  list.addEventListener("contextmenu", (e) => {
    const item = e.target.closest(".cat-item");
    if (!item) return;
    const catId = item.dataset.catId;
    if (!catId) return;
    e.preventDefault();
    e.stopPropagation();
    showCatCtxMenu(e.clientX, e.clientY, catId);
  });

  menu.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-ctx]");
    if (!btn || !catCtxId) return;
    e.stopPropagation();
    const id = catCtxId;
    hideCatCtxMenu();
    const act = btn.dataset.ctx;
    if (act === "gen-category") {
      const { ids, assets } = await enabledAssetsForCategory(id);
      await runGenerate(ids, { assetRows: assets });
    } else if (act === "gen-category-export") {
      const { ids, assets } = await enabledAssetsForCategory(id);
      await runGenerate(ids, { exportAfter: true, assetRows: assets });
    } else if (act === "export-category") {
      const { ids } = await enabledAssetsForCategory(id);
      await runExport(ids);
    } else if (act === "rename") await renameCategoryDialog(id);
    else if (act === "delete") await deleteCategory(id);
  });

  document.addEventListener("click", (e) => {
    if (!e.target.closest("#cat-ctx-menu")) hideCatCtxMenu();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hideCatCtxMenu();
  });
  window.addEventListener(
    "scroll",
    () => {
      hideCatCtxMenu();
    },
    true,
  );
}

async function duplicateAsset(assetId) {
  const a = await API.post(`/api/assets/${assetId}/duplicate`);
  await loadAssetList();
  await selectAsset(a.id);
}

function hideAssetCtxMenu() {
  assetCtxIds = [];
  closeFloatingMenu($("#asset-ctx-menu"));
}

function bindAssetContextMenu() {
  const list = $("#asset-list");
  const menu = $("#asset-ctx-menu");
  if (!list || !menu) return;

  list.addEventListener("contextmenu", (e) => {
    const row = e.target.closest(".asset-row");
    if (!row) return;
    const assetId = row.dataset.id;
    if (!assetId) return;
    e.preventDefault();
    e.stopPropagation();
    showAssetCtxMenu(e.clientX, e.clientY, assetId);
  });

  menu.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-ctx]");
    if (!btn || btn.disabled || btn.classList.contains("is-disabled")) return;
    const ids = assetCtxIds.length ? [...assetCtxIds] : [];
    if (!ids.length) return;
    e.stopPropagation();
    hideAssetCtxMenu();
    const act = btn.dataset.ctx;
    const focusId = ids.includes(state.assetId) ? state.assetId : ids[0];
    if (act === "gen-one") {
      if (focusId !== state.assetId) await selectAsset(focusId);
      await runGenerate(ids, { emptyToastKey: "toast.selectAsset" });
    } else if (act === "gen-one-export") {
      if (focusId !== state.assetId) await selectAsset(focusId);
      await runGenerate(ids, { exportAfter: true, emptyToastKey: "toast.selectAsset" });
    } else if (act === "export-one") {
      if (focusId !== state.assetId) await selectAsset(focusId);
      await runExport(ids, { emptyToastKey: "toast.selectAsset" });
    } else if (act === "rename") await renameAssetDialog(ids[0]);
    else if (act === "duplicate") await duplicateAsset(ids[0]).catch((err) => err && toast(err.message));
    else if (act === "postprocess") await openPostprocess(ids[0]);
    else if (act === "remove-magenta") await removeMagentaFromAssets(ids);
    else if (act === "auto-trim") await autoTrimInboxAssets(ids);
    else if (act === "open-source") await openAssetFile(ids[0], "source");
    else if (act === "open-inbox") await openAssetFile(ids[0], "inbox");
    else if (act === "open-unity") await openAssetFile(ids[0], "unity");
    else if (act === "refresh") await refreshAssetEntries(ids);
    else if (act === "delete") await deleteAssets(ids);
  });

  document.addEventListener("click", (e) => {
    if (!e.target.closest("#asset-ctx-menu")) hideAssetCtxMenu();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hideAssetCtxMenu();
  });
  window.addEventListener(
    "scroll",
    () => {
      hideAssetCtxMenu();
    },
    true,
  );
}

async function openPath(path) {
  if (!path) return;
  try {
    await API.post("/api/open-path", { path });
  } catch (err) {
    toast(err.message);
  }
}

async function openApiPortal(provider) {
  const url = state.apiPortals?.[provider];
  if (!url) {
    toast(t("toast.apiPortalMissing"), { variant: "error" });
    return;
  }
  try {
    await API.post("/api/open-url", { url });
  } catch {
    window.open(url, "_blank", "noopener,noreferrer");
  }
}

async function openPreviewFile() {
  if (!state.assetId) {
    toast(t("toast.selectAsset"));
    return;
  }
  const key = currentPreviewSourceKey();
  try {
    const paths = await API.get(`/api/assets/${state.assetId}/paths`);
    state.paths = paths;
    const file = paths[key];
    if (!file) {
      toast(t("toast.noPath"));
      return;
    }
    await openPath(file);
  } catch (err) {
    toast(err.message);
  }
}

// ── 对话框 ─────────────────────────────────────────────

function bindDialogDismiss(dlg) {
  const form = dlg.querySelector("form");
  form?.querySelector("[data-dismiss]")?.addEventListener("click", () => dlg.close());
  dlg.addEventListener("cancel", (e) => {
    e.preventDefault();
    dlg.close();
  });
}

/** @type {{ app_version?: string, release_notes?: Record<string, string> } | null} */
let welcomeDialogData = null;

function fillWelcomeReleaseNotes(data) {
  const el = $("#welcome-release-notes");
  if (!el || !data) return;
  const lang = getLang();
  const notes =
    data.release_notes?.[lang] ||
    data.release_notes?.["zh-CN"] ||
    data.release_notes?.["en-US"] ||
    "";
  const version = data.app_version || "1.0";
  el.textContent = notes
    ? `${t("welcome.releasePrefix", { version })} · ${notes}`
    : t("welcome.releasePrefix", { version });
}

function fillWelcomeDialog(data) {
  welcomeDialogData = data;
  const badge = $("#welcome-version-badge");
  if (badge) badge.textContent = `v${data.app_version || "1.0"}`;
  fillWelcomeReleaseNotes(data);
  const skip = $("#welcome-skip-checkbox");
  if (skip) skip.checked = false;
}

async function closeWelcomeDialog(persistSkip) {
  const dlg = $("#dlg-welcome");
  if (persistSkip) {
    try {
      await API.put("/api/app/welcome/dismiss", {});
    } catch {
      /* ignore */
    }
  }
  dlg?.close();
}

async function maybeShowWelcomeDialog() {
  try {
    const data = await API.get("/api/app/welcome");
    if (!data.show) return;
    fillWelcomeDialog(data);
    const dlg = $("#dlg-welcome");
    if (!dlg) return;
    dlg.showModal();
  } catch {
    /* ignore */
  }
}

function bindWelcomeDialog() {
  const dlg = $("#dlg-welcome");
  if (!dlg) return;
  dlg.addEventListener("cancel", (e) => {
    e.preventDefault();
    void closeWelcomeDialog($("#welcome-skip-checkbox")?.checked);
  });
  $("#welcome-start")?.addEventListener("click", () => {
    void closeWelcomeDialog($("#welcome-skip-checkbox")?.checked);
  });
}

async function newCategoryDialog() {
  const dlg = $("#dlg-new-cat");
  const form = $("#form-new-cat");
  form.reset();
  await fillNewCatCheckpointSelect();
  dlg.showModal();
  form.onsubmit = async (e) => {
    e.preventDefault();
    const label = form.label.value.trim();
    if (!label) {
      toast(t("toast.categoryNameRequired"));
      return;
    }
    const submitBtn = e.submitter || form.querySelector('button[type="submit"]');
    const checkpoint = form.checkpoint?.value?.trim() || "";
    await withBtnBusy(submitBtn, async () => {
      try {
        const cat = await API.post("/api/categories", {
          label,
          id: form.id.value.trim() || undefined,
          checkpoint,
        });
        dlg.close();
        await bootstrap(false);
        await selectCategory(cat.id);
        toast(t("toast.categoryCreated"));
      } catch (err) {
        toast(err.message);
        throw err;
      }
    });
  };
}

let newAssetDlgTab = "manual";
/** @type {{ id: string, file: File, url: string, width?: number, height?: number }[]} */
let importDraftFiles = [];
let importDraftSeq = 0;
let newAssetImportBound = false;

function importAssetTargetName(file) {
  const stem = (file.name || "import").replace(/\.[^.]+$/, "").trim() || "import";
  return `${stem}.png`;
}

function resetImportDraft() {
  for (const item of importDraftFiles) {
    if (item.url) URL.revokeObjectURL(item.url);
  }
  importDraftFiles = [];
  const input = $("#import-asset-files");
  if (input) input.value = "";
  renderImportAssetList();
}

function updateNewAssetSubmitLabel() {
  const btn = $("#new-asset-submit");
  if (!btn) return;
  if (newAssetDlgTab === "import") {
    const n = importDraftFiles.length;
    btn.textContent = n > 0 ? t("dlg.importCreate", { n }) : t("dlg.create");
  } else {
    btn.textContent = t("dlg.create");
  }
}

function switchNewAssetDlgTab(tab) {
  newAssetDlgTab = tab;
  const dlg = $("#dlg-new-asset");
  if (!dlg) return;
  dlg.querySelectorAll(".dlg-tab").forEach((btn) => {
    const active = btn.dataset.dlgTab === tab;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  dlg.querySelectorAll("[data-dlg-panel]").forEach((panel) => {
    const show = panel.dataset.dlgPanel === tab;
    panel.classList.toggle("hidden", !show);
    panel.hidden = !show;
  });
  updateImportGenModeBarVisibility();
  if (tab === "import") {
    const mode = currentGenMode();
    if (mode === "redraw") {
      setImportGenMode("redraw", { syncPrompt: true });
    } else {
      setImportGenMode("txt2img", { syncPrompt: mode === "txt2img" });
    }
  } else {
    updateImportHintText();
  }
  updateNewAssetSubmitLabel();
}

function probeImportImageSize(item) {
  const img = new Image();
  img.onload = () => {
    item.width = img.naturalWidth;
    item.height = img.naturalHeight;
    renderImportAssetList();
  };
  img.onerror = () => {};
  img.src = item.url;
}

function addImportDraftFiles(fileList) {
  const existing = new Set(importDraftFiles.map((x) => x.file.name));
  for (const file of fileList) {
    if (!file || !file.type.startsWith("image/")) continue;
    if (existing.has(file.name)) continue;
    existing.add(file.name);
    const item = {
      id: `imp-${++importDraftSeq}`,
      file,
      url: URL.createObjectURL(file),
    };
    importDraftFiles.push(item);
    probeImportImageSize(item);
  }
  renderImportAssetList();
}

function removeImportDraftFile(id) {
  const idx = importDraftFiles.findIndex((x) => x.id === id);
  if (idx < 0) return;
  const [removed] = importDraftFiles.splice(idx, 1);
  if (removed?.url) URL.revokeObjectURL(removed.url);
  renderImportAssetList();
}

function renderImportAssetList() {
  const list = $("#import-asset-list");
  const countEl = $("#import-asset-count");
  const emptyEl = $("#import-asset-empty");
  if (!list) return;

  list.replaceChildren();
  for (const item of importDraftFiles) {
    const li = document.createElement("li");
    li.className = "import-asset-item";
    li.dataset.importId = item.id;

    const thumb = document.createElement("img");
    thumb.className = "import-asset-thumb";
    thumb.src = item.url;
    thumb.alt = "";
    thumb.loading = "lazy";

    const meta = document.createElement("div");
    meta.className = "import-asset-meta";

    const name = document.createElement("div");
    name.className = "import-asset-name";
    name.textContent = importAssetTargetName(item.file);
    name.title = item.file.name;

    const sub = document.createElement("div");
    sub.className = "import-asset-sub";
    const parts = [formatBytes(item.file.size)];
    if (item.width && item.height) parts.push(`${item.width}×${item.height}`);
    sub.textContent = parts.filter(Boolean).join(" · ");

    meta.append(name, sub);

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "import-asset-remove";
    removeBtn.textContent = "×";
    removeBtn.setAttribute("aria-label", t("asset.delete"));
    removeBtn.dataset.importRemove = item.id;

    li.append(thumb, meta, removeBtn);
    list.appendChild(li);
  }

  if (countEl) {
    countEl.textContent =
      importDraftFiles.length > 0
        ? t("dlg.importCount", { n: importDraftFiles.length })
        : "";
  }
  if (emptyEl) {
    emptyEl.hidden = importDraftFiles.length > 0;
  }
  updateNewAssetSubmitLabel();
}

function bindNewAssetImportUi() {
  if (newAssetImportBound) return;
  newAssetImportBound = true;

  const dlg = $("#dlg-new-asset");
  const form = $("#form-new-asset");
  const fileInput = $("#import-asset-files");
  const pickBtn = $("#import-asset-pick");
  const list = $("#import-asset-list");

  form?.querySelectorAll(".dlg-tab").forEach((tab) => {
    tab.addEventListener("click", () => switchNewAssetDlgTab(tab.dataset.dlgTab || "manual"));
  });

  pickBtn?.addEventListener("click", () => fileInput?.click());

  fileInput?.addEventListener("change", () => {
    if (fileInput.files?.length) addImportDraftFiles(fileInput.files);
    fileInput.value = "";
  });

  list?.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-import-remove]");
    if (!btn) return;
    removeImportDraftFile(btn.dataset.importRemove);
  });

  $$('input[name="import_gen_mode"]').forEach((r) => {
    r.addEventListener("change", onImportGenModeChange);
  });

  dlg?.addEventListener("close", () => {
    resetImportDraft();
    switchNewAssetDlgTab("manual");
    setFormError(form, "");
  });
}

function parseNewAssetForm(form) {
  const filename = form.filename.value.trim();
  const size = parseInt(form.size.value, 10);
  if (!filename) return { error: t("toast.filenameRequired") };
  if (!Number.isFinite(size) || size < 32 || size > 4096) {
    return { error: t("toast.invalidSize") };
  }
  return {
    filename,
    width: size,
    height: size,
    subject: form.subject.value.trim(),
  };
}

async function newAssetDialog() {
  if (!state.categoryId) {
    toast(t("toast.selectCategory"), { variant: "error" });
    return;
  }
  bindNewAssetImportUi();
  const dlg = $("#dlg-new-asset");
  const form = $("#form-new-asset");
  if (!dlg || !form) return;
  form.reset();
  resetImportDraft();
  switchNewAssetDlgTab("manual");
  setFormError(form, "");
  dlg.showModal();
  form.onsubmit = async (e) => {
    e.preventDefault();
    setFormError(form, "");
    const submitBtn = e.submitter || form.querySelector('button[type="submit"]');

    if (newAssetDlgTab === "import") {
      if (importDraftFiles.length === 0) {
        const msg = t("toast.importEmpty");
        setFormError(form, msg);
        toast(msg, { variant: "error" });
        return;
      }
      await withBtnBusy(submitBtn, async () => {
        try {
          const fd = new FormData();
          fd.append("category", state.categoryId);
          fd.append("gen_mode", currentImportGenMode());
          for (const item of importDraftFiles) {
            fd.append("files", item.file, item.file.name);
          }
          const result = await API.postForm("/api/assets/import", fd);
          dlg.close();
          resetImportDraft();
          await loadAssetList();
          updateMainTabsVisibility();
          const created = result?.created || [];
          const failed = result?.failed || [];
          if (failed.length) {
            toast(
              t("toast.assetsImportedPartial", {
                ok: result.count ?? created.length,
                fail: failed.length,
              }),
              { variant: failed.length && !created.length ? "error" : "warning" },
            );
          } else {
            toast(t("toast.assetsImported", { n: result.count ?? created.length }));
          }
          if (created[0]?.id) {
            applyGenModeToPromptPanel(created[0].gen_mode || currentImportGenMode());
            await selectAsset(created[0].id);
            switchTab("basic");
          }
        } catch (err) {
          setFormError(form, err.message);
          toast(err.message, { variant: "error" });
          throw err;
        }
      });
      return;
    }

    const parsed = parseNewAssetForm(form);
    if (parsed.error) {
      setFormError(form, parsed.error);
      toast(parsed.error, { variant: "error" });
      return;
    }
    await withBtnBusy(submitBtn, async () => {
      try {
        const asset = await API.post("/api/assets", {
          filename: parsed.filename,
          category: state.categoryId,
          width: parsed.width,
          height: parsed.height,
          subject: parsed.subject,
          enabled: false,
        });
        dlg.close();
        await loadAssetList();
        updateMainTabsVisibility();
        if (asset?.id) {
          await selectAsset(asset.id);
          switchTab("basic");
        }
        toast(t("toast.assetCreated"));
      } catch (err) {
        setFormError(form, err.message);
        toast(err.message, { variant: "error" });
        throw err;
      }
    });
  };
}

// ── 事件 ─────────────────────────────────────────────

const ASSET_PANEL_STORAGE_KEY = "artApp.assetPanelW";
const ASSET_PANEL_MIN = 176;
const ASSET_PANEL_MAX = 440;
const ASSET_PANEL_DEFAULT = 228;

function applyAssetPanelWidth(width) {
  const w = Math.min(ASSET_PANEL_MAX, Math.max(ASSET_PANEL_MIN, Math.round(width)));
  document.documentElement.style.setProperty("--asset-panel-w", `${w}px`);
  const panel = $("#panel-assets");
  if (panel) panel.style.width = `${w}px`;
  try {
    localStorage.setItem(ASSET_PANEL_STORAGE_KEY, String(w));
  } catch {
    /* ignore */
  }
  return w;
}

function readAssetPanelWidth() {
  const panel = $("#panel-assets");
  const inline = panel ? parseInt(panel.style.width, 10) : NaN;
  if (Number.isFinite(inline)) return inline;
  const raw = parseInt(localStorage.getItem(ASSET_PANEL_STORAGE_KEY) || "", 10);
  return Number.isFinite(raw) ? raw : ASSET_PANEL_DEFAULT;
}

function bindAssetPanelResize() {
  const resizer = $("#asset-panel-resizer");
  if (!resizer) return;

  applyAssetPanelWidth(readAssetPanelWidth());

  let startX = 0;
  let startW = ASSET_PANEL_DEFAULT;
  let dragging = false;

  const stopDrag = (e) => {
    if (!dragging) return;
    dragging = false;
    resizer.classList.remove("is-dragging");
    document.body.classList.remove("is-col-resizing");
    try {
      resizer.releasePointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
  };

  const onMove = (e) => {
    if (!dragging) return;
    e.preventDefault();
    applyAssetPanelWidth(startW + (e.clientX - startX));
  };

  resizer.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    dragging = true;
    startX = e.clientX;
    startW = readAssetPanelWidth();
    resizer.classList.add("is-dragging");
    document.body.classList.add("is-col-resizing");
    resizer.setPointerCapture(e.pointerId);
  });

  resizer.addEventListener("pointermove", onMove);
  resizer.addEventListener("pointerup", stopDrag);
  resizer.addEventListener("pointercancel", stopDrag);

  resizer.addEventListener("keydown", (e) => {
    const step = e.shiftKey ? 24 : 8;
    let w = readAssetPanelWidth();
    if (e.key === "ArrowLeft") w -= step;
    else if (e.key === "ArrowRight") w += step;
    else return;
    e.preventDefault();
    applyAssetPanelWidth(w);
  });
}

function bindUi() {
  bindRipple(document.getElementById("app"));
  bindPromptSegmentListeners();
  bindAssetPanelResize();
  bindAssetContextMenu();
  bindCategoryContextMenu();
  bindAssetDragToCategory();
  bindJobFloat();
  bindPreviewViewport();
  bindDialogDismiss($("#dlg-new-cat"));
  bindDialogDismiss($("#dlg-new-asset"));
  bindDialogDismiss($("#dlg-rename-asset"));
  bindDialogDismiss($("#dlg-rename-category"));
  bindDialogDismiss($("#dlg-confirm"));
  bindDialogDismiss($("#dlg-pp-subject"));
  bindWelcomeDialog();
  initPathFields(document);
  bindPathFieldSideEffects();

  $("#preview-img")?.addEventListener("error", () => {
    const asset = state.assets.find((a) => a.id === state.assetId);
    showPreviewEmpty(asset || null);
  });

  $("#asset-search")?.addEventListener("input", () => {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(loadAssetList, 150);
  });

  $("#asset-list")?.addEventListener("click", (e) => {
    const chip = e.target.closest(".path-chip");
    if (!chip) return;
    e.preventDefault();
    e.stopPropagation();
    const assetId = chip.dataset.assetId;
    const kind = chip.dataset.pathKind;
    if (assetId && kind) openAssetPathDir(assetId, kind);
  });

  $$(".seg-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".seg-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.previewSource = btn.dataset.source || "inbox";
      if (state.assetId) {
        loadPreview();
        if (state.previewInfo) renderPreviewInfo(state.previewInfo);
      }
    });
  });

  $$("#main-tabs .tab").forEach((tab) => {
    tab.addEventListener("click", () => switchTab(tab.dataset.tab));
  });

  $$(".ai-mode-tab").forEach((tab) => {
    tab.addEventListener("click", () => switchAiMode(tab.dataset.aiMode));
  });

  updateAiModeUi();
  renderAiChat([]);
  if (isAiIntroDismissed()) $("#ai-intro-section")?.classList.add("is-hidden");

  $("#form-basic")?.addEventListener("submit", saveBasic);
  $("#form-category")?.addEventListener("submit", saveCategory);
  $("#form-settings")?.addEventListener("submit", saveSettings);
  bindCloudApiFieldListeners();

  $("#workflow-preset-select")?.addEventListener("change", updateWorkflowPresetDesc);

  $$('input[name="gen_mode"]').forEach((r) => {
    r.addEventListener("change", onGenModePanelChange);
  });
  $$('input[name="cloud_gen_mode"]').forEach((r) => {
    r.addEventListener("change", () => {
      updateCloudGenModeUi();
      void saveCloudGenModeOnly();
    });
  });
  $("#cloud-strength-range")?.addEventListener("input", () => {
    syncCloudStrengthFromRange();
    void saveCloudGenModeOnly();
  });
  $("#cloud-strength")?.addEventListener("change", () => {
    syncCloudStrengthFromNumber();
    void saveCloudGenModeOnly();
  });
  $("#img2img-denoise-range")?.addEventListener("input", () => {
    syncDenoiseFromRange();
    void saveGenModeOnly();
  });
  $("#img2img-denoise")?.addEventListener("change", () => {
    syncDenoiseFromNumber();
    void saveGenModeOnly();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeNavMenus();
  });

  $("#ai-input")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendAi();
    }
  });

  $("#ai-intro-dismiss")?.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    dismissAiIntro();
  });

  $("#ai-history")?.addEventListener("click", (e) => {
    const resendBtn = e.target.closest("[data-action='ai-resend']");
    if (resendBtn) {
      e.stopPropagation();
      const retry = resendBtn.dataset.retryMessage || "";
      if (retry) void sendAiWithMessage(retry);
      return;
    }
    const failed = e.target.closest(".ai-msg.failed[data-retry-message]");
    if (!failed || e.target.closest("button")) return;
    const retry = failed.dataset.retryMessage || "";
    if (retry) fillAiInput(retry);
  });

  $("#ai-history")?.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const failed = e.target.closest(".ai-msg.failed[data-retry-message]");
    if (!failed) return;
    e.preventDefault();
    const retry = failed.dataset.retryMessage || "";
    if (retry) fillAiInput(retry);
  });

  document.addEventListener("click", () => {
    closeNavMenus();
  });

  document.getElementById("app")?.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    closeNavMenus();
    const act = btn.dataset.action;
    switch (act) {
      case "scan-status":
        await withBtnBusy(btn, scanStatus);
        break;
      case "refresh-preview":
        if (state.assetId) {
          loadPreview();
          loadPreviewInfo(state.assetId);
        }
        break;
      case "open-file":
        await openPreviewFile();
        break;
      case "open-inbox-dir":
        if (state.categoryId) {
          const d = await API.get(`/api/categories/${state.categoryId}/dir/inbox`);
          openPath(d.path);
        }
        break;
      case "open-source-dir":
        if (state.categoryId) {
          const d = await API.get(`/api/categories/${state.categoryId}/dir/source`);
          openPath(d.path);
        }
        break;
      case "postprocess":
        if (state.assetId) await openPostprocess(state.assetId);
        break;
      case "new-category":
        newCategoryDialog();
        break;
      case "new-asset":
        newAssetDialog();
        break;
      case "save-prompts":
        await savePrompts(btn);
        break;
      case "save-cloud-prompts":
        await saveCloudPrompts(btn);
        break;
      case "save-wf":
        await saveWorkflow(btn);
        break;
      case "validate-wf":
        await validateWorkflow(btn);
        break;
      case "load-default-wf":
        await loadDefaultWorkflow(btn);
        break;
      case "load-wf-preset":
        await loadWorkflowPreset(btn);
        break;
      case "ai-send":
        sendAi();
        break;
      case "ai-resend":
        if (btn.dataset.retryMessage) void sendAiWithMessage(btn.dataset.retryMessage);
        break;
      case "ai-clear":
        await clearAiChat();
        break;
      case "ai-intro-dismiss":
        dismissAiIntro();
        break;
      case "clear-logs":
        await API.del("/api/logs");
        $("#log-body").textContent = "";
        break;
      case "close-logs":
        closeLogDrawer();
        break;
      case "init-workflows":
        await withBtnBusy(btn, async () => {
          const r = await API.post("/api/workflows/init");
          toast(t("toast.workflowsInit", { n: r.created }));
        }).catch((err) => {
          if (err) toast(err.message);
        });
        break;
      case "test-cloud-api":
        await testCloudApi(btn.dataset.provider, btn);
        break;
      case "open-api-portal":
        await openApiPortal(btn.dataset.provider);
        break;
      case "test-deepseek-api":
        await testDeepseekApi(btn);
        break;
      case "autodetect-paths":
        await withBtnBusy(btn, async () => {
          const p = await API.get("/api/settings/paths/default");
          const form = $("#form-settings");
          form.project_root.value = p.project_root;
          form.art_pipeline_root.value = p.art_pipeline_root;
          if (p.log_dir && form.log_dir) form.log_dir.value = p.log_dir;
        }).catch((err) => {
          if (err) toast(err.message);
        });
        break;
      case "open-log-dir":
        await withBtnBusy(btn, async () => {
          const data = await API.get("/api/settings");
          const dir = (formSettingsLogDir() || data.log_dir_effective || "").trim();
          if (!dir) {
            toast(t("toast.logDirMissing"), { variant: "error" });
            return;
          }
          await openPath(dir);
        }).catch((err) => {
          if (err) toast(err.message);
        });
        break;
      default:
        break;
    }
  });
  window.addEventListener("message", (event) => {
    if (event.origin !== location.origin) return;
    const data = event.data;
    if (!data || data.type !== "postprocess-applied" || !data.assetId) return;
    handlePostprocessApplied(data);
  });
}

function handlePostprocessApplied(data) {
  if (!data?.assetId) return;
  if (data.width != null && data.height != null) {
    const row = state.assets.find((a) => a.id === data.assetId);
    if (row) {
      row.width = data.width;
      row.height = data.height;
      if (data.size_label) row.size_label = data.size_label;
      else row.size_label = `${data.width}×${data.height}`;
    }
    if (data.assetId === state.assetId && state.assetFull) {
      state.assetFull.width = data.width;
      state.assetFull.height = data.height;
      const form = $("#form-basic");
      if (form?.width) form.width.value = data.width;
      if (form?.height) form.height.value = data.height;
    }
    renderAssetList();
  }
  if (data.assetId === state.assetId) {
    loadPreview();
    loadPreviewInfo(data.assetId);
  }
}

function consumePostprocessAppliedNotification() {
  try {
    const raw = sessionStorage.getItem("artApp.ppApplied");
    if (!raw) return;
    sessionStorage.removeItem("artApp.ppApplied");
    handlePostprocessApplied(JSON.parse(raw));
  } catch {
    /* ignore */
  }
}

async function trySelectAssetFromUrl() {
  const id = new URLSearchParams(location.search).get("asset");
  if (!id) return false;
  try {
    const asset = await API.get(`/api/assets/${id}`);
    await selectCategory(asset.category);
    await selectAsset(id);
    return true;
  } catch {
    return false;
  }
}

async function bootstrap(pickFirst = true) {
  const useSplash = !appBooted;
  if (!useSplash) showGlobalOverlay(t("splash.reloading"));
  try {
    try {
      await initI18n();
      applyDomI18n();
      bindLangSwitcher();
      onLangChange(() => refreshI18nUi());
      initLogPanel();
    } catch (err) {
      console.error("i18n init failed", err);
    }
    await API.get("/api/health");
    const data = await API.get("/api/categories");
    state.categories = data.categories || [];
    fillCategorySelect();
    renderCategories();
    await loadCheckpoints();
    const fromUrl = await trySelectAssetFromUrl();
    if (!fromUrl && pickFirst && state.categories.length && !state.categoryId) {
      await selectCategory(state.categories[0].id);
    } else if (state.categoryId) {
      await loadAssetList();
    }
    refreshComfy();
    if (!appBooted) setInterval(refreshComfy, 30000);
    try {
      const j = await API.get("/api/jobs/status");
      if (j.busy || (j.queued_count || 0) > 0 || (j.queue || []).length > 0) {
        state.jobRunId = j.run_id ?? null;
        state.jobTrack = { active: true, sawBusy: !!j.busy, postOk: true, submittedAt: Date.now() };
        if (j.kind) resetJobProg(j.kind);
        updateJobFromApi(j);
        ensureJobPoll();
      }
    } catch {
      /* ignore */
    }
    const activeTab = $("#main-tabs .tab.active")?.dataset.tab;
    updateMainTabsVisibility();
    const visibleTab = $(`#main-tabs .tab[data-tab="${activeTab}"]:not([hidden])`);
    switchTab(visibleTab?.dataset.tab || (categoryHasAssets() ? "ai" : "category"));
    consumePostprocessAppliedNotification();
  } catch (err) {
    toast(t("toast.backendFailed", { msg: err.message }));
  } finally {
    if (useSplash) await hideSplash(document.getElementById("app-splash"));
    else hideGlobalOverlay();
    appBooted = true;
    if (useSplash) await maybeShowWelcomeDialog();
  }
}

document.body.classList.add("is-booting");
bindUi();
bootstrap();
