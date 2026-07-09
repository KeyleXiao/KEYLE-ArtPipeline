/**
 * 后处理编辑器 · Web（对标 postprocess_editor.py）
 * 服务端 PIL 渲染 + 浏览器交互（拖拽 / 裁切 / 图层管理）
 */
import { API } from "./api.js";
import { icon, layerTypeIcon } from "./icons.js";
import {
  initI18n,
  t,
  applyDomI18n,
  bindLangSwitcher,
  onLangChange,
} from "./i18n.js";
import {
  bindRipple,
  closeFloatingMenu,
  hideGlobalOverlay,
  openFloatingMenu,
  showGlobalOverlay,
  withBtnBusy,
} from "./effects.js";
import { appendLog, initLogPanel } from "./log-panel.js";
import { initPathFields } from "./path-input.js";
import {
  applyMattePointsToPreview,
  applyMatteStrokeToFullData,
  canRedoMatteEdit,
  canUndoMatteEdit,
  clearMattePreview,
  commitBorderMatte,
  commitChromaKey,
  drawMattePreview,
  discardLastMatteUndoSnapshot,
  ensureMatteFull,
  invalidateMatteFull,
  loadMattePreview,
  matteFullState,
  matteFullToBlob,
  mattePreviewState,
  pushMatteUndoSnapshot,
  redoMatteEdit,
  undoMatteEdit,
} from "./matte-preview.js";
import {
  applyLayerCropToRgba,
  applyMatrixCrop,
  cellAtPoint,
  cellKey,
  clampLineMove,
  clampRect,
  clearRgbaRegion,
  copyRgbaRegion,
  decodeBlobToRgba,
  defaultGridLines,
  hitTestGridLine,
  hitTestMatrixLineDelete,
  drawMatrixLineDeleteHandle,
  matrixLineDeleteHandles,
  insertGridLine,
  normalizeGridLines,
  pasteRgbaRegion,
  previewMatrixSize,
  removeGridLine,
  rgbaToPngBlob,
} from "./crop-matrix.js";
import { CHROMA_PRESET_MAGENTA, parseKeyHex } from "./chroma-key.js";

const params = new URLSearchParams(location.search);
const assetId = params.get("asset");
/** 主界面打开时传入：source | inbox | unity，决定主体 $asset 读取哪张图 */
const subjectMode = (params.get("subject") || "inbox").trim().toLowerCase();

const $ = (s) => document.querySelector(s);

const view = {
  zoom: 1,
  panX: 0,
  panY: 0,
  minZoom: 0.25,
  maxZoom: 6,
};

let stack = null;
let assetInfo = null;
let assetPaths = null;
let selectedId = null;
/** @type {string[]} */
let selectedLayerIds = [];
let lastLayerClickId = null;
/** @type {string[]} */
let layerCtxIds = [];
let soloId = null;
let boundsData = { layers: [], canvas: { width: 512, height: 512 }, raw_sizes: {} };
let previewTimer = null;
let previewReq = 0;
let previewAbort = null;
let previewBlobUrl = null;
let boundsTimer = null;
let boundsReq = 0;

/** 图层 stack 自动持久化（关闭窗口后再次进入仍保留图层数与配置） */
let stackPersistEnabled = false;
let stackPersistTimer = null;
let stackPersistBusy = false;
let stackPersistDirty = false;

let drag = null;
/** @type {{ mode: "crop"|"viewport", startX: number, startY: number, scrollLeft?: number, scrollTop?: number, panX?: number, panY?: number } | null} */
let canvasPan = null;
let cropMode = false;
let matteMode = false;
/** @type {{ layerId: string, points: number[][], lastX: number, lastY: number } | null} */
let matteStroke = null;
let matteStrokeActive = false;
let matteEntering = false;
let matteCursorDoc = null;
let matteTargetLayerId = null;
let matteCursorRaf = 0;
let matteLocalRaf = 0;
/** @type {number[][]} */
let mattePendingPoints = [];
let matteStrokeNeedsUndoSnap = false;
let keyPresetMode = "magenta";
let mattePreviewLoading = false;
let matteUsingLocalComposite = false;
/** 抠图期间服务端预览应隐藏目标图层（防 schedulePreview 竞态重影） */
let matteHideTargetInPreview = false;
/** 本地抠图有未写回改动 */
let matteDirty = false;
let matteExiting = false;

/** 旋转拖拽：前端 canvas 预览，松手后再请求服务端合成 */
const rotationPreviewState = {
  active: false,
  layerId: null,
  img: null,
  blobUrl: null,
  bgReady: false,
  loading: false,
};
let rotationEditPointer = 0;

/** 与 engine._normalized_rotation_deg 一致：归一化到 [-180, 180] */
function normalizeRotationDeg(angle) {
  let a = Number(angle) || 0;
  a = ((a % 360) + 360) % 360;
  if (a > 180) a -= 360;
  return a;
}

/** 文档坐标（Y 向下）下与 PIL rotate(-deg) / canvas rotate(+deg) 一致的弧度 */
function rotationCanvasRad(deg) {
  return (normalizeRotationDeg(deg) * Math.PI) / 180;
}

/** 将图层局部角点变换到画布文档坐标（与后端 _rotate_point(rad=-deg) 一致） */
function mapRotatedLocalToDoc(lx, ly, px, py, ax, ay, deg) {
  const rad = rotationCanvasRad(deg);
  const dx = lx - px;
  const dy = ly - py;
  return [ax + dx * Math.cos(rad) + dy * Math.sin(rad), ay - dx * Math.sin(rad) + dy * Math.cos(rad)];
}

function rotatedLayerDocCorners(sw, sh, px, py, ax, ay, deg) {
  const angle = normalizeRotationDeg(deg);
  if (Math.abs(angle) < 0.01) {
    return [
      [ax - px, ay - py],
      [ax - px + sw, ay - py],
      [ax - px + sw, ay - py + sh],
      [ax - px, ay - py + sh],
    ];
  }
  return [
    [0, 0],
    [sw, 0],
    [sw, sh],
    [0, sh],
  ].map(([lx, ly]) => mapRotatedLocalToDoc(lx, ly, px, py, ax, ay, angle));
}
/** @type {{ offsetX: number, offsetY: number } | null} */
let matteBannerDrag = null;
const matteBannerPos = { x: null, y: null };
const MATTE_STROKE_STEP_PX = 4;
let cropPreview = null;
let cropDrag = null;
let cropRawImg = null;
let cropRawSize = { w: 0, h: 0 };
/** @type {"rect"|"matrix"|"free"} */
let cropSubMode = "rect";
let cropLoading = false;
let cropEntering = false;
let cropSubModeLoading = false;
let cropLoadSeq = 0;
/** 图层 PNG 缓存世代；自适应裁切等写回后递增，避免悬停预取写入过期 blob */
let cropRawBlobEpoch = 0;
/** @type {Map<string, { epoch: number, blob: Blob }>} */
const cropRawBlobCache = new Map();
/** @type {Map<string, Promise<Blob>>} */
const cropRawBlobInflight = new Map();
/** @type {AbortController | null} */
let cropLayerFetchAbort = null;
/** @type {{ data: Uint8ClampedArray, w: number, h: number, layerId: string } | null} */
let freeCropWork = null;
let freeCropBitmap = null;
/** @type {{ x: number, y: number, w: number, h: number } | null} */
let freeCropSelection = null;
/** @type {{ data: Uint8ClampedArray, w: number, h: number } | null} */
let freeCropClipboard = null;
let freeCropDirty = false;
/** @type {{ anchor: { x: number, y: number }, square: boolean } | null} */
let freeCropDrag = null;
let cropDrawRaf = 0;
let cropPointerCaptureId = null;
/** @type {{ data: Uint8ClampedArray, w: number, h: number, layerId: string } | null} */
let matrixWork = null;
let matrixHLines = [];
let matrixVLines = [];
/** @type {Set<string>} */
let matrixRemoved = new Set();
/** @type {{ type: "h"|"v", index: number } | null} */
let matrixDrag = null;
/** @type {{ type: "h"|"v", index: number } | null} */
let matrixSelectedLine = null;
/** @type {{ type: "h"|"v", index: number } | null} */
let matrixDeleteHover = null;
let matrixRectRawImg = null;

/** 裁切画布缩放：在「适应容器」基础上的倍率 */
let cropViewZoom = 1;

const CROP_HANDLE = 9;
const CROP_CLOSE_BTN_R = 10;
const HISTORY_MAX = 10;
const PP_BLEND_MODES = ["normal", "multiply", "screen", "overlay", "soft_light", "add", "color"];
const BLEND_MODE_DESC_KEYS = {
  normal: "pp.blendMode.normal.desc",
  multiply: "pp.blendMode.multiply.desc",
  screen: "pp.blendMode.screen.desc",
  overlay: "pp.blendMode.overlay.desc",
  soft_light: "pp.blendMode.soft_light.desc",
  add: "pp.blendMode.add.desc",
  color: "pp.blendMode.color.desc",
};

const ppHistory = {
  past: [],
  future: [],
  recording: false,
  busy: false,
};
let propsHistoryArmed = true;

function cloneStackData(s) {
  return JSON.parse(JSON.stringify(s));
}

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== "string") {
        reject(new Error("read failed"));
        return;
      }
      resolve(result.includes(",") ? result.split(",")[1] : result);
    };
    reader.onerror = () => reject(reader.error || new Error("read failed"));
    reader.readAsDataURL(blob);
  });
}

async function fetchLayerRawBase64(layerId) {
  applyPropsFromForm();
  try {
    const res = await fetch(
      `/api/assets/${encodeURIComponent(assetId)}/postprocess/layer-raw`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(previewBody({ layer_id: layerId })),
      },
    );
    if (!res.ok) return null;
    return blobToBase64(await res.blob());
  } catch {
    return null;
  }
}

async function createHistoryEntry({ includeImages = false } = {}) {
  applyPropsFromForm();
  const entry = {
    stack: cloneStackData(stack),
    selectedId,
    soloId,
    images: {},
  };
  if (!includeImages) return entry;
  for (const layer of stack.layers || []) {
    if (layer.type !== "image") continue;
    const b64 = await fetchLayerRawBase64(layer.id);
    if (b64) entry.images[layer.id] = b64;
  }
  return entry;
}

async function restoreHistoryEntry(entry) {
  ppHistory.recording = true;
  try {
    stack = cloneStackData(entry.stack);
    selectedId = entry.selectedId ?? selectedId;
    soloId = entry.soloId ?? null;
    const solo = $("#pp-solo");
    if (solo) solo.checked = !!soloId;
    for (const [layerId, b64] of Object.entries(entry.images || {})) {
      if (!b64) continue;
      await API.post(`/api/assets/${encodeURIComponent(assetId)}/postprocess/layer-restore-image`, {
        ...previewBody(),
        layer_id: layerId,
        image_b64: b64,
      });
    }
    renderLayers();
    fillProps();
    await fetchBounds();
    await refreshPreview({ skipInboxSync: true });
    drawOverlay();
  } finally {
    ppHistory.recording = false;
    scheduleStackPersist({ structural: true });
  }
}

function updateHistoryButtons() {
  const undoBtn = $("#pp-undo");
  const redoBtn = $("#pp-redo");
  const blocked = ppHistory.recording || ppHistory.busy;
  const matteLocked = isMatteSessionLocked();
  if (undoBtn) {
    undoBtn.disabled = blocked || (matteLocked ? !canUndoMatteEdit() : ppHistory.past.length === 0);
  }
  if (redoBtn) {
    redoBtn.disabled = blocked || (matteLocked ? !canRedoMatteEdit() : ppHistory.future.length === 0);
  }
}

function resetHistory() {
  ppHistory.past = [];
  ppHistory.future = [];
  propsHistoryArmed = true;
  updateHistoryButtons();
}

async function pushHistoryBefore({ includeImages = false } = {}) {
  if (ppHistory.recording) return false;
  while (ppHistory.busy) {
    await new Promise((r) => setTimeout(r, 16));
  }
  ppHistory.busy = true;
  try {
    const entry = await createHistoryEntry({ includeImages });
    ppHistory.past.push(entry);
    if (ppHistory.past.length > HISTORY_MAX) ppHistory.past.shift();
    ppHistory.future = [];
    updateHistoryButtons();
    return true;
  } finally {
    ppHistory.busy = false;
  }
}

/** 抠图类操作前：确保当前图层 PNG 已写入撤销栈 */
async function pushMatteHistoryBefore(layer) {
  if (!layer?.id || layer.type !== "image") return false;
  const b64 = await fetchLayerRawBase64(layer.id);
  if (!b64) return false;
  const ok = await pushHistoryBefore({ includeImages: false });
  if (!ok) return false;
  const entry = ppHistory.past[ppHistory.past.length - 1];
  if (!entry) return false;
  entry.images = { ...(entry.images || {}), [layer.id]: b64 };
  updateHistoryButtons();
  return true;
}

function beginPropsHistoryBatch() {
  if (ppHistory.recording || !propsHistoryArmed) return;
  propsHistoryArmed = false;
  pushHistoryBefore({ includeImages: false }).finally(() => {
    window.setTimeout(() => {
      propsHistoryArmed = true;
    }, 1500);
  });
}

async function undoHistory() {
  if (ppHistory.recording || ppHistory.busy) return;
  if (!ppHistory.past.length) {
    setStatus(t("pp.historyNothing"));
    return;
  }
  ppHistory.busy = true;
  updateHistoryButtons();
  try {
    const current = await createHistoryEntry({ includeImages: true });
    ppHistory.future.push(current);
    const prev = ppHistory.past.pop();
    await restoreHistoryEntry(prev);
    setStatus(t("pp.historyRestored"));
  } catch (err) {
    setStatus(err.message);
  } finally {
    ppHistory.busy = false;
    updateHistoryButtons();
  }
}

async function redoHistory() {
  if (ppHistory.recording || ppHistory.busy || !ppHistory.future.length) return;
  ppHistory.busy = true;
  updateHistoryButtons();
  try {
    const current = await createHistoryEntry({ includeImages: true });
    ppHistory.past.push(current);
    if (ppHistory.past.length > HISTORY_MAX) ppHistory.past.shift();
    const next = ppHistory.future.pop();
    await restoreHistoryEntry(next);
    setStatus(t("pp.historyRestored"));
  } catch (err) {
    setStatus(err.message);
  } finally {
    ppHistory.busy = false;
    updateHistoryButtons();
  }
}

function syncMatteDirtyFromUndo() {
  matteDirty = canUndoMatteEdit();
}

function undoMatteSessionEdit() {
  if (!undoMatteEdit()) return false;
  resetMatteStroke();
  syncMatteDirtyFromUndo();
  drawOverlay();
  updateHistoryButtons();
  return true;
}

function redoMatteSessionEdit() {
  if (!redoMatteEdit()) return false;
  resetMatteStroke();
  syncMatteDirtyFromUndo();
  drawOverlay();
  updateHistoryButtons();
  return true;
}

function bindHistoryControls() {
  $("#pp-undo")?.addEventListener("click", () => {
    if (isMatteSessionLocked()) {
      undoMatteSessionEdit();
      return;
    }
    undoHistory();
  });
  $("#pp-redo")?.addEventListener("click", () => {
    if (isMatteSessionLocked()) {
      redoMatteSessionEdit();
      return;
    }
    redoHistory();
  });
}

function setStatus(msg) {
  $("#pp-status").textContent = msg;
}

function selectedLayer() {
  return stack?.layers?.find((l) => l.id === selectedId) || null;
}

function subjectLayer() {
  return stack?.layers?.find((l) => l.is_subject || l.source === "$asset") || null;
}

function canvasSize() {
  return {
    w: stack?.canvas?.width || stack?.canvas_width || assetInfo?.width || 512,
    h: stack?.canvas?.height || stack?.canvas_height || assetInfo?.height || 512,
  };
}

function syncBoundsCanvasFromStack() {
  const { w, h } = canvasSize();
  if (!boundsData?.canvas) boundsData = { layers: [], canvas: { width: w, height: h }, raw_sizes: {} };
  else {
    boundsData.canvas.width = w;
    boundsData.canvas.height = h;
  }
}

/** 文档坐标尺寸（与 stack / bounds 一致，预览图按此尺寸铺放） */
function documentSize() {
  const bw = boundsData?.canvas?.width;
  const bh = boundsData?.canvas?.height;
  if (bw > 0 && bh > 0) return { w: bw, h: bh };
  return canvasSize();
}

function isDefaultSubjectLayout(layer) {
  if (!layer) return false;
  const xf = layer.transform || {};
  return (
    Math.abs((xf.scale ?? 1) - 1) < 1e-6 &&
    Math.abs(xf.offset_x ?? 0) < 1e-6 &&
    Math.abs(xf.offset_y ?? 0) < 1e-6 &&
    Math.abs(xf.rotation_deg ?? 0) < 1e-6 &&
    !xf.flip_h &&
    !xf.flip_v &&
    !layer.crop
  );
}

function isNativeViewport() {
  return Math.abs(view.zoom - 1) < 0.001 && view.panX === 0 && view.panY === 0;
}

/** 当前文档在视口中的像素尺寸 */
function viewportDocSize() {
  const { w, h } = documentSize();
  return { docW: w * view.zoom, docH: h * view.zoom };
}

/** 文档大于可视区域时需要外层滚动（贴左上），否则在视口内居中 */
function viewportNeedsOuterScroll() {
  const wrap = $("#pp-viewport-wrap");
  const { docW, docH } = viewportDocSize();
  if (!wrap) return false;
  const pad = 20;
  const availW = Math.max(1, wrap.clientWidth - pad);
  const availH = Math.max(1, wrap.clientHeight - pad);
  return docW > availW || docH > availH;
}

function viewportOffset() {
  const vp = $("#pp-viewport");
  const { docW, docH } = viewportDocSize();
  if (viewportNeedsOuterScroll()) {
    return { ox: view.panX, oy: view.panY, docW, docH };
  }
  return {
    ox: (vp.clientWidth - docW) / 2 + view.panX,
    oy: (vp.clientHeight - docH) / 2 + view.panY,
    docW,
    docH,
  };
}

function waitPreviewImageLoaded(img) {
  if (!img) return Promise.resolve();
  if (img.complete && img.naturalWidth > 0) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const onLoad = () => {
      cleanup();
      resolve();
    };
    const onError = () => {
      cleanup();
      reject(new Error("preview load"));
    };
    const cleanup = () => {
      img.removeEventListener("load", onLoad);
      img.removeEventListener("error", onError);
    };
    img.addEventListener("load", onLoad);
    img.addEventListener("error", onError);
  });
}

function clampCanvasDim(n, fallback = 512) {
  const v = Math.round(Number(n));
  if (!Number.isFinite(v)) return fallback;
  return Math.min(4096, Math.max(32, v));
}

function setStackCanvasSize(w, h) {
  if (!stack) return canvasSize();
  const cw = clampCanvasDim(w);
  const ch = clampCanvasDim(h);
  stack.canvas_width = cw;
  stack.canvas_height = ch;
  stack.canvas = { width: cw, height: ch };
  return { w: cw, h: ch };
}

/** @type {{ w: number, h: number }} */
let canvasResizeBaseline = { w: 512, h: 512 };
/** @type {object | null} */
let canvasResizeSnapshot = null;

function isCanvasSizeField(el) {
  return el?.id === "pp-resize-w" || el?.id === "pp-resize-h";
}

function ensureCanvasSizeInputsEnabled() {
  const wIn = $("#pp-resize-w");
  const hIn = $("#pp-resize-h");
  for (const el of [wIn, hIn]) {
    if (!el) continue;
    el.disabled = false;
    el.readOnly = false;
  }
}

function parseCanvasFieldValue(raw) {
  const s = String(raw ?? "").trim();
  if (!s || !/^\d+$/.test(s)) return null;
  const n = Number(s);
  if (!Number.isFinite(n)) return null;
  return n;
}

function resetCanvasResizeSession() {
  const { w, h } = canvasSize();
  canvasResizeBaseline = { w, h };
  canvasResizeSnapshot = cloneStackData(stack);
  fillCanvasResizeInputs();
  updateCanvasSizeCompareUI();
}

function updateCanvasSizeCompareUI() {
  const cur = $("#pp-canvas-size-current");
  const tgt = $("#pp-canvas-size-target");
  if (cur) cur.textContent = `${canvasResizeBaseline.w} × ${canvasResizeBaseline.h}`;
  if (!tgt) return;
  const draft = readCanvasResizeInputs({ commit: false });
  if (draft) {
    tgt.textContent = `${draft.w} × ${draft.h}`;
    return;
  }
  tgt.textContent = `${canvasResizeBaseline.w} × ${canvasResizeBaseline.h}`;
}

function fillCanvasResizeInputs() {
  const wIn = $("#pp-resize-w");
  const hIn = $("#pp-resize-h");
  if (!wIn || !hIn) return;
  if (document.activeElement === wIn || document.activeElement === hIn) return;
  const { w, h } = canvasSize();
  wIn.value = String(w);
  hIn.value = String(h);
  ensureCanvasSizeInputsEnabled();
  updateCanvasSizeCompareUI();
}

function readCanvasResizeInputs({ commit = true } = {}) {
  const wIn = $("#pp-resize-w");
  const hIn = $("#pp-resize-h");
  if (!wIn || !hIn) return null;
  const rawW = parseCanvasFieldValue(wIn.value);
  const rawH = parseCanvasFieldValue(hIn.value);
  if (rawW == null || rawH == null) return null;
  if (!commit) return { w: rawW, h: rawH };
  return setStackCanvasSize(rawW, rawH);
}

function ensureCanvasResizeSnapshot() {
  if (!canvasResizeSnapshot && stack) {
    canvasResizeSnapshot = cloneStackData(stack);
  }
}

function resizeStackCanvasData(stackObj, newW, newH, { scaleContent = false } = {}) {
  if (!stackObj) return false;
  const oldW = stackObj.canvas_width || stackObj.canvas?.width || 512;
  const oldH = stackObj.canvas_height || stackObj.canvas?.height || 512;
  const nw = clampCanvasDim(newW, oldW);
  const nh = clampCanvasDim(newH, oldH);
  if (nw === oldW && nh === oldH && !scaleContent) return false;

  if (scaleContent && oldW > 0 && oldH > 0 && (nw !== oldW || nh !== oldH)) {
    const sx = nw / oldW;
    const sy = nh / oldH;
    const contentScale = Math.min(sx, sy);
    for (const layer of stackObj.layers || []) {
      if (!layer?.transform) continue;
      layer.transform.offset_x = (layer.transform.offset_x || 0) * sx;
      layer.transform.offset_y = (layer.transform.offset_y || 0) * sy;
      layer.transform.scale = (layer.transform.scale || 1) * contentScale;
      if (layer.type === "text" && layer.text) {
        layer.text.font_size = Math.max(8, Math.round((layer.text.font_size || 24) * contentScale));
      }
    }
  }

  stackObj.canvas_width = nw;
  stackObj.canvas_height = nh;
  stackObj.canvas = { width: nw, height: nh };
  return nw !== oldW || nh !== oldH || scaleContent;
}

function applyCanvasResizePreview() {
  const draft = readCanvasResizeInputs({ commit: false });
  if (!draft || !stack) return false;
  ensureCanvasResizeSnapshot();
  if (!canvasResizeSnapshot) return false;

  const scaleContent = !!$("#pp-resize-scale-content")?.checked;
  const targetW = clampCanvasDim(draft.w, canvasResizeBaseline.w);
  const targetH = clampCanvasDim(draft.h, canvasResizeBaseline.h);
  const baseW = canvasResizeBaseline.w;
  const baseH = canvasResizeBaseline.h;

  for (const layer of stack.layers || []) {
    const baseLayer = (canvasResizeSnapshot.layers || []).find((l) => l.id === layer.id);
    if (!layer?.transform || !baseLayer?.transform) continue;
    if (scaleContent && baseW > 0 && baseH > 0) {
      const sx = targetW / baseW;
      const sy = targetH / baseH;
      const contentScale = Math.min(sx, sy);
      layer.transform.offset_x = (baseLayer.transform.offset_x || 0) * sx;
      layer.transform.offset_y = (baseLayer.transform.offset_y || 0) * sy;
      layer.transform.scale = (baseLayer.transform.scale || 1) * contentScale;
      if (layer.type === "text" && layer.text) {
        const baseSize = baseLayer.text?.font_size || layer.text.font_size || 24;
        layer.text.font_size = Math.max(8, Math.round(baseSize * contentScale));
      }
    } else {
      layer.transform.offset_x = baseLayer.transform.offset_x || 0;
      layer.transform.offset_y = baseLayer.transform.offset_y || 0;
      layer.transform.scale = baseLayer.transform.scale || 1;
      if (layer.type === "text" && layer.text && baseLayer.text?.font_size != null) {
        layer.text.font_size = baseLayer.text.font_size;
      }
    }
  }

  stack.canvas_width = targetW;
  stack.canvas_height = targetH;
  stack.canvas = { width: targetW, height: targetH };
  syncBoundsCanvasFromStack();
  updateCanvasSizeCompareUI();
  updatePostprocessMeta();
  scheduleStackPersist({ structural: true });
  schedulePreview(120);
  return true;
}

function commitCanvasResizeFromInputs() {
  const draft = readCanvasResizeInputs({ commit: false });
  if (!draft) throw new Error(t("pp.canvasSizeInvalid"));
  if (
    draft.w < 32 ||
    draft.w > 4096 ||
    draft.h < 32 ||
    draft.h > 4096
  ) {
    throw new Error(t("pp.canvasSizeInvalid"));
  }
  applyCanvasResizePreview();
  resetCanvasResizeSession();
}

function bindCanvasResizeControls() {
  const wIn = $("#pp-resize-w");
  const hIn = $("#pp-resize-h");
  const scaleChk = $("#pp-resize-scale-content");
  const resetBtn = $("#pp-resize-reset");
  const acc = $("#pp-acc-canvas-size");
  if (!wIn || !hIn) return;
  ensureCanvasSizeInputsEnabled();

  let canvasHistTimer = null;
  let previewDebounce = null;

  const queueCanvasResizeHistory = () => {
    if (canvasHistTimer) clearTimeout(canvasHistTimer);
    canvasHistTimer = setTimeout(() => {
      canvasHistTimer = null;
      pushHistoryBefore({ includeImages: false }).catch(() => {});
    }, 500);
  };

  const onResizeInput = () => {
    updateCanvasSizeCompareUI();
    clearTimeout(previewDebounce);
    previewDebounce = setTimeout(() => {
      previewDebounce = null;
      applyCanvasResizePreview();
      queueCanvasResizeHistory();
    }, 280);
  };

  const onResizeCommit = () => {
    clearTimeout(previewDebounce);
    previewDebounce = null;
    if (!applyCanvasResizePreview()) fillCanvasResizeInputs();
    else queueCanvasResizeHistory();
  };

  for (const el of [wIn, hIn]) {
    el.addEventListener("input", onResizeInput);
    el.addEventListener("change", onResizeCommit);
    el.addEventListener("keydown", (e) => e.stopPropagation());
    el.addEventListener("wheel", (e) => e.stopPropagation(), { passive: true });
  }

  scaleChk?.addEventListener("change", () => {
    applyCanvasResizePreview();
    queueCanvasResizeHistory();
  });

  resetBtn?.addEventListener("click", () => {
    resetCanvasResizeSession();
    applyCanvasResizePreview();
    setStatus(t("pp.canvasSizeResetDone"));
  });

  acc?.addEventListener("toggle", () => {
    if (acc.open) resetCanvasResizeSession();
  });

  resetCanvasResizeSession();
}

function previewBody(extra = {}) {
  return {
    stack,
    solo_layer_id: soloId || undefined,
    subject_path: subjectMode || "inbox",
    edit_subject: subjectMode || "inbox",
    ...extra,
  };
}

function postprocessSaveBody(extra = {}) {
  return { stack, edit_subject: subjectMode || "inbox", ...extra };
}

function stackLayerCount() {
  return stack?.layers?.length ?? 0;
}

function ppSessionStorageKey() {
  return `artApp.ppSession.${assetId}.${subjectMode || "inbox"}`;
}

function persistSessionState() {
  try {
    sessionStorage.setItem(
      ppSessionStorageKey(),
      JSON.stringify({
        selectedId,
        selectedLayerIds,
        layerIds: (stack.layers || []).map((l) => l.id),
      }),
    );
  } catch {
    /* ignore */
  }
}

function restoreSessionSelection() {
  try {
    const raw = sessionStorage.getItem(ppSessionStorageKey());
    if (!raw) return;
    const state = JSON.parse(raw);
    const currentIds = (stack.layers || []).map((l) => l.id);
    if (!state?.layerIds?.length || state.layerIds.length !== currentIds.length) return;
    if (!state.layerIds.every((id, i) => id === currentIds[i])) return;
    if (state.selectedId && currentIds.includes(state.selectedId)) {
      selectedId = state.selectedId;
    }
    if (Array.isArray(state.selectedLayerIds)) {
      const restored = state.selectedLayerIds.filter((id) => currentIds.includes(id));
      if (restored.length) selectedLayerIds = restored;
    } else if (selectedId) {
      selectedLayerIds = [selectedId];
    }
  } catch {
    /* ignore */
  }
}

function scheduleStackPersist({ delay = 1500, structural = false } = {}) {
  if (!stackPersistEnabled || !assetId || isMatteSessionLocked() || ppHistory.recording) return;
  stackPersistDirty = true;
  clearTimeout(stackPersistTimer);
  stackPersistTimer = setTimeout(() => {
    void persistStackQuietly();
  }, structural ? 400 : delay);
}

function flushStackPersistSync() {
  if (!stackPersistDirty || !assetId || stackPersistBusy) return;
  try {
    applyPropsFromForm();
    const body = JSON.stringify(postprocessSaveBody());
    fetch(`/api/assets/${encodeURIComponent(assetId)}/postprocess`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body,
      keepalive: true,
    }).catch(() => {});
    stackPersistDirty = false;
    persistSessionState();
  } catch {
    /* ignore */
  }
}

async function persistStackNow() {
  if (!assetId || !stack) return;
  applyPropsFromForm();
  await API.put(`/api/assets/${encodeURIComponent(assetId)}/postprocess`, postprocessSaveBody());
  stackPersistDirty = false;
  persistSessionState();
}

async function persistStackQuietly() {
  if (!stackPersistDirty || !assetId || stackPersistBusy || ppHistory.recording || isMatteSessionLocked()) {
    return;
  }
  stackPersistBusy = true;
  try {
    applyPropsFromForm();
    await API.put(`/api/assets/${assetId}/postprocess`, postprocessSaveBody());
    stackPersistDirty = false;
    persistSessionState();
    updatePostprocessMeta();
    setStatus(t("pp.autoSaved", { layers: stackLayerCount() }));
  } catch (err) {
    matteLog(`配置自动保存失败: ${err.message}`, "系统");
  } finally {
    stackPersistBusy = false;
  }
}

function markStackStructuralChange() {
  scheduleStackPersist({ structural: true });
  updatePostprocessMeta();
  if (stack && !isCanvasSizeField(document.activeElement)) {
    resetCanvasResizeSession();
  }
}

function defaultTextStyle() {
  return {
    content: t("pp.defaultTextContent"),
    font_family: "PingFang SC",
    font_size: 40,
    color: "#FFFFFF",
    stroke_color: "#000000",
    stroke_width: 2,
    align: "center",
  };
}

function defaultTransform() {
  return {
    offset_x: 0,
    offset_y: 0,
    scale: 1,
    anchor: "center",
    rotation_deg: 0,
    flip_h: false,
    flip_v: false,
    pivot_x: 0.5,
    pivot_y: 0.5,
  };
}

function ensureFontOption(family) {
  const sel = $("#pp-fonts");
  if (!sel || !family) return;
  if ([...sel.options].some((o) => o.value === family)) return;
  const o = document.createElement("option");
  o.value = family;
  o.textContent = family;
  sel.appendChild(o);
}

function layerListSubtitle(layer) {
  if (layer.type === "text" && layer.text) {
    const snippet = (layer.text.content || "").trim().slice(0, 14) || t("pp.layerText");
    const font = (layer.text.font_family || "").trim();
    return font ? `${snippet} · ${font}` : snippet;
  }
  return `${layer.type}${layer.is_subject ? t("layer.subject") : ""}`;
}

function refreshActiveLayerRow() {
  const layer = selectedLayer();
  if (!layer) return;
  const row = document.querySelector(".layer-item.active");
  if (!row) {
    renderLayers();
    return;
  }
  const title = row.querySelector(".layer-title");
  const sub = row.querySelector(".layer-sub");
  if (title) title.textContent = layer.name || layer.id;
  if (sub) sub.textContent = layerListSubtitle(layer);
}

function isMatteSessionLocked() {
  return matteMode && !matteExiting;
}

function onPropsFormChange() {
  if (isMatteSessionLocked()) return;
  beginPropsHistoryBatch();
  applyTransformLive({ previewMs: 32, bounds: true });
  if (!rotationPreviewState.active) scheduleStackPersist();
}

function isRotationLivePreview() {
  return rotationPreviewState.active;
}

function cleanupRotationPreviewAssets() {
  rotationPreviewState.active = false;
  rotationPreviewState.layerId = null;
  rotationPreviewState.bgReady = false;
  rotationPreviewState.loading = false;
  if (rotationPreviewState.blobUrl) {
    URL.revokeObjectURL(rotationPreviewState.blobUrl);
    rotationPreviewState.blobUrl = null;
  }
  rotationPreviewState.img = null;
}

function updateRotationPreviewLocal() {
  const layer = selectedLayer();
  if (!layer) return;
  applyPropsFromForm();
  if (layer.type === "image") synthesizeMatteLayerBounds(layer);
  drawOverlay();
}

async function beginRotationPreview() {
  if (rotationPreviewState.loading) return;
  const layer = selectedLayer();
  if (!layer || layer.locked || matteMode) return;

  rotationPreviewState.active = true;
  rotationPreviewState.layerId = layer.id;

  if (layer.type !== "image") return;

  if (rotationPreviewState.img && rotationPreviewState.layerId === layer.id && rotationPreviewState.bgReady) {
    updateRotationPreviewLocal();
    return;
  }

  rotationPreviewState.loading = true;
  try {
    if (!rotationPreviewState.img) {
      const blob = await matteFetchRawBlob(layer.id);
      if (blob) {
        rotationPreviewState.blobUrl = URL.createObjectURL(blob);
        const img = new Image();
        await new Promise((resolve, reject) => {
          img.onload = resolve;
          img.onerror = reject;
          img.src = rotationPreviewState.blobUrl;
        });
        rotationPreviewState.img = img;
      }
    }
    if (!rotationPreviewState.bgReady) {
      await refreshPreview({ skipInboxSync: true, hideRotationLayerId: layer.id });
      rotationPreviewState.bgReady = true;
    }
    updateRotationPreviewLocal();
  } catch {
    /* 本地预览失败时仍可在松手后走服务端 */
  } finally {
    rotationPreviewState.loading = false;
  }
}

function armRotationPreview() {
  rotationEditPointer += 1;
  if (rotationEditPointer === 1) void beginRotationPreview();
}

function releaseRotationPreview() {
  if (rotationEditPointer <= 0) return;
  rotationEditPointer -= 1;
  if (rotationEditPointer === 0) void commitRotationPreview();
}

async function commitRotationPreview() {
  if (!rotationPreviewState.active && rotationEditPointer <= 0) return;
  rotationEditPointer = 0;
  const wasActive = rotationPreviewState.active;
  cleanupRotationPreviewAssets();
  if (!wasActive) return;
  applyPropsFromForm();
  scheduleBoundsRefresh(0);
  await refreshPreview({ skipInboxSync: true });
  scheduleStackPersist({ delay: 400 });
}

function drawRotationLayerPreview(ctx, ox, oy, z, layer, bounds) {
  const img = rotationPreviewState.img;
  if (!img || rotationPreviewState.layerId !== layer.id) return;

  const xf = layer.transform || {};
  const opacity = Math.max(0, Math.min(1, layer.opacity ?? 1));
  const crop = layer.crop;
  let srcX = 0;
  let srcY = 0;
  let srcW = img.naturalWidth || img.width;
  let srcH = img.naturalHeight || img.height;
  if (crop?.w > 0 && crop?.h > 0) {
    srcX = crop.x;
    srcY = crop.y;
    srcW = crop.w;
    srcH = crop.h;
  }

  const sw = bounds.local_w || bounds.w;
  const sh = bounds.local_h || bounds.h;
  const pivot = bounds.pivot;
  if (!pivot || !sw || !sh) return;

  const angleRad = rotationCanvasRad(xf.rotation_deg || 0);
  const pxNorm = xf.pivot_x ?? 0.5;
  const pyNorm = xf.pivot_y ?? 0.5;
  const drawW = sw * z;
  const drawH = sh * z;

  ctx.save();
  ctx.globalAlpha = opacity;
  ctx.imageSmoothingEnabled = view.zoom < 2;
  ctx.translate(ox + pivot.x * z, oy + pivot.y * z);
  ctx.rotate(angleRad);
  if (xf.flip_h) ctx.scale(-1, 1);
  if (xf.flip_v) ctx.scale(1, -1);
  const dx = -pxNorm * drawW;
  const dy = -pyNorm * drawH;
  ctx.drawImage(img, srcX, srcY, srcW, srcH, dx, dy, drawW, drawH);
  ctx.strokeStyle = "rgba(56, 189, 248, 0.9)";
  ctx.lineWidth = Math.max(1.5, 2 / z);
  ctx.setLineDash([6 / z, 3 / z]);
  ctx.strokeRect(dx + 0.5 / z, dy + 0.5 / z, drawW - 1 / z, drawH - 1 / z);
  ctx.setLineDash([]);
  ctx.restore();
}

function bindRotationPreviewControls() {
  const form = $("#pp-props-form");
  form?.rotation_slider?.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 && e.pointerType === "mouse") return;
    armRotationPreview();
  });
  form?.rotation?.addEventListener("focusin", () => armRotationPreview());
  form?.rotation?.addEventListener("blur", () => releaseRotationPreview());
  form?.rotation?.addEventListener("change", () => releaseRotationPreview());
  window.addEventListener("pointerup", () => releaseRotationPreview());
  window.addEventListener("pointercancel", () => releaseRotationPreview());
}

/** 将表单/按钮的变换写入 stack，并触发边界与预览刷新 */
function applyTransformLive({ previewMs = 16, bounds = true, forceServerPreview = false } = {}) {
  applyPropsFromForm();
  refreshActiveLayerRow();
  if (bounds) {
    if (isRotationLivePreview() && !forceServerPreview) {
      updateRotationPreviewLocal();
    } else {
      scheduleBoundsRefresh(0);
    }
  }
  if (!isRotationLivePreview() || forceServerPreview) {
    schedulePreview(previewMs);
  }
}

function patchBoundsOffset(layerId, dx, dy) {
  if (!dx && !dy) return;
  for (const b of boundsData.layers || []) {
    if (b.id !== layerId) continue;
    b.x = (b.x ?? 0) + dx;
    b.y = (b.y ?? 0) + dy;
    if (b.corners?.length) {
      b.corners = b.corners.map(([x, y]) => [x + dx, y + dy]);
    }
    if (b.pivot) {
      b.pivot.x += dx;
      b.pivot.y += dy;
    }
    break;
  }
  drawOverlay();
}

function scheduleBoundsRefresh(delay = 0) {
  clearTimeout(boundsTimer);
  boundsTimer = setTimeout(refreshBoundsOverlay, Math.max(0, delay));
}

async function refreshBoundsOverlay() {
  applyPropsFromForm();
  const reqId = ++boundsReq;
  try {
    await fetchBounds();
    if (reqId !== boundsReq) return;
    layoutPreview();
    drawOverlay();
  } catch {
    /* ignore */
  }
}

function applyPropsFromForm() {
  const layer = selectedLayer();
  if (!layer) return;
  const form = $("#pp-props-form");
  layer.name = form.name.value.trim() || layer.name;
  if (form.opacity_slider) {
    const pct = Math.min(100, Math.max(0, parseInt(form.opacity_slider.value, 10) || 0));
    layer.opacity = pct / 100;
    form.opacity.value = pct === 100 ? 1 : pct / 100;
  } else {
    const o = parseFloat(form.opacity.value);
    layer.opacity = Number.isFinite(o) ? Math.min(1, Math.max(0, o)) : 1;
  }
  const blendMode = String(form.blend_mode?.value || "normal");
  layer.blend_mode = PP_BLEND_MODES.includes(blendMode) ? blendMode : "normal";
  layer.blend_enabled = !!form.blend_enabled?.checked;
  layer.blend_color = normalizeBlendColorText(form.blend_color_text?.value || form.blend_color?.value || "");
  if (form.blend_amount_slider) {
    const pct = Math.min(100, Math.max(0, parseInt(form.blend_amount_slider.value, 10) || 0));
    layer.blend_amount = pct / 100;
    form.blend_amount.value = pct === 100 ? 1 : pct / 100;
  } else {
    const a = parseFloat(form.blend_amount?.value);
    layer.blend_amount = Number.isFinite(a) ? Math.min(1, Math.max(0, a)) : 1;
  }
  layer.transform = { ...defaultTransform(), ...(layer.transform || {}) };
  layer.transform.offset_x = parseFloat(form.offset_x.value) || 0;
  layer.transform.offset_y = parseFloat(form.offset_y.value) || 0;
  if (form.scale_slider) {
    const pct = parseInt(form.scale_slider.value, 10) || 100;
    layer.transform.scale = pct / 100;
    form.scale_pct.value = pct;
    const tag = $("#scale-tag");
    if (tag) tag.textContent = `${pct}%`;
  } else {
    layer.transform.scale = (parseFloat(form.scale_pct.value) || 100) / 100;
  }
  if (form.rotation_slider) {
    const rot = normalizeRotationDeg(parseInt(form.rotation_slider.value, 10) || 0);
    layer.transform.rotation_deg = rot;
    form.rotation.value = rot;
    const rotTag = $("#rotation-tag");
    if (rotTag) rotTag.textContent = `${rot}°`;
  } else {
    layer.transform.rotation_deg = normalizeRotationDeg(parseFloat(form.rotation?.value) || 0);
  }
  const pxPct = parseFloat(form.pivot_x_pct?.value);
  const pyPct = parseFloat(form.pivot_y_pct?.value);
  if (Number.isFinite(pxPct)) layer.transform.pivot_x = Math.min(1, Math.max(0, pxPct / 100));
  if (Number.isFinite(pyPct)) layer.transform.pivot_y = Math.min(1, Math.max(0, pyPct / 100));
  if (layer.type === "image") {
    layer.source = form.source?.value?.trim() ?? layer.source;
    if (layer.is_subject) layer.source = "$asset";
    const cx = parseInt(form.crop_x?.value, 10);
    const cy = parseInt(form.crop_y?.value, 10);
    const cw = parseInt(form.crop_w?.value, 10);
    const ch = parseInt(form.crop_h?.value, 10);
    if (cw > 0 && ch > 0 && !Number.isNaN(cx) && !Number.isNaN(cy)) {
      layer.crop = { x: cx, y: cy, w: cw, h: ch };
    } else {
      delete layer.crop;
    }
  }
  if (layer.type === "text") {
    layer.text = layer.text || defaultTextStyle();
    layer.text.content = form.text_content.value;
    layer.text.font_size = parseInt(form.font_size.value, 10) || 24;
    layer.text.color = form.text_color.value || "#FFFFFF";
    const family = form.font_family?.value?.trim();
    if (family) layer.text.font_family = family;
  }
}

function normalizeBlendColorText(value) {
  const s = String(value || "").trim();
  if (!s) return "";
  if (/^#[0-9a-fA-F]{6}$/.test(s)) return s.toLowerCase();
  if (/^#[0-9a-fA-F]{3}$/.test(s)) {
    const h = s.slice(1);
    return `#${h[0]}${h[0]}${h[1]}${h[1]}${h[2]}${h[2]}`.toLowerCase();
  }
  if (/^[0-9a-fA-F]{6}$/.test(s)) return `#${s.toLowerCase()}`;
  return "";
}

function syncBlendColorInputs(form, hex) {
  const color = normalizeBlendColorText(hex);
  if (form.blend_color) form.blend_color.value = color || "#ffffff";
  if (form.blend_color_text) form.blend_color_text.value = color;
}

function updateBlendModeDesc() {
  const form = $("#pp-props-form");
  const sel = form?.blend_mode;
  const el = $("#pp-blend-mode-desc");
  if (!el || !sel) return;
  const key = BLEND_MODE_DESC_KEYS[sel.value];
  el.textContent = key ? t(key) : "";
}

function updateBlendUi() {
  const form = $("#pp-props-form");
  const enabled = !!form?.blend_enabled?.checked;
  const tintBody = $("#pp-blend-tint-body");
  const acc = $("#pp-acc-blend");
  if (tintBody) tintBody.classList.toggle("is-disabled", !enabled);
  if (acc) acc.classList.toggle("pp-acc--active", enabled);
  tintBody?.querySelectorAll("input,select,button").forEach((el) => {
    if (el.name === "blend_enabled" || el.closest(".pp-switch")) return;
    el.disabled = !enabled;
  });
}

function normalizeKeyHex(text) {
  const s = String(text || "").trim();
  if (!s) return "";
  const withHash = s.startsWith("#") ? s : `#${s}`;
  return parseKeyHex(withHash) ? withHash.toUpperCase() : "";
}

function syncKeyColorInputs(hex) {
  const color = normalizeKeyHex(hex) || "#FF00FF";
  const picker = $("#pp-key-color");
  const text = $("#pp-key-color-text");
  if (picker) picker.value = color.toLowerCase();
  if (text) text.value = color;
}

function readKeySettings() {
  const fuzz = parseInt($("#pp-key-fuzz-num")?.value || $("#pp-key-fuzz")?.value || "15", 10);
  const feather = parseInt($("#pp-key-feather-num")?.value || $("#pp-key-feather")?.value || "1", 10);
  let keyHex = "#FF00FF";
  if (keyPresetMode === "custom") {
    keyHex = normalizeKeyHex($("#pp-key-color-text")?.value || $("#pp-key-color")?.value) || "#FF00FF";
  }
  return {
    key_hex: keyHex,
    fuzz: Math.max(5, Math.min(45, fuzz || 15)),
    feather: Math.max(0, Math.min(3, Number.isNaN(feather) ? 1 : feather)),
  };
}

function setKeyPresetMode(mode) {
  keyPresetMode = mode === "custom" ? "custom" : "magenta";
  const magentaBtn = $("#pp-key-preset-magenta");
  const customBtn = $("#pp-key-preset-custom");
  const colorField = $("#pp-key-color-field");
  magentaBtn?.classList.toggle("active", keyPresetMode === "magenta");
  magentaBtn?.classList.toggle("ghost", keyPresetMode !== "magenta");
  customBtn?.classList.toggle("active", keyPresetMode === "custom");
  customBtn?.classList.toggle("ghost", keyPresetMode !== "custom");
  if (colorField) {
    colorField.hidden = keyPresetMode !== "custom";
    colorField.toggleAttribute("hidden", keyPresetMode !== "custom");
  }
  if (keyPresetMode === "magenta") {
    syncKeyColorInputs(CHROMA_PRESET_MAGENTA.key_hex);
    const fuzz = $("#pp-key-fuzz");
    const fuzzNum = $("#pp-key-fuzz-num");
    const feather = $("#pp-key-feather");
    const featherNum = $("#pp-key-feather-num");
    if (fuzz) fuzz.value = String(CHROMA_PRESET_MAGENTA.fuzz);
    if (fuzzNum) fuzzNum.value = String(CHROMA_PRESET_MAGENTA.fuzz);
    if (feather) feather.value = String(CHROMA_PRESET_MAGENTA.feather);
    if (featherNum) featherNum.value = String(CHROMA_PRESET_MAGENTA.feather);
  }
}

function syncKeyFuzzFromRange() {
  const range = $("#pp-key-fuzz");
  const num = $("#pp-key-fuzz-num");
  if (range && num) num.value = range.value;
}

function syncKeyFuzzFromNumber() {
  const range = $("#pp-key-fuzz");
  const num = $("#pp-key-fuzz-num");
  if (!range || !num) return;
  let v = parseInt(num.value, 10);
  if (Number.isNaN(v)) v = 15;
  v = Math.max(5, Math.min(45, v));
  num.value = v;
  range.value = v;
}

function syncKeyFeatherFromRange() {
  const range = $("#pp-key-feather");
  const num = $("#pp-key-feather-num");
  if (range && num) num.value = range.value;
}

function syncKeyFeatherFromNumber() {
  const range = $("#pp-key-feather");
  const num = $("#pp-key-feather-num");
  if (!range || !num) return;
  let v = parseInt(num.value, 10);
  if (Number.isNaN(v)) v = 1;
  v = Math.max(0, Math.min(3, v));
  num.value = v;
  range.value = v;
}

async function applyChromaKey(btn) {
  const layer = selectedLayer();
  if (!layer || layer.type !== "image") {
    setStatus(t("pp.matteNeedImage"));
    return;
  }
  if (layer.locked) return;
  const settings = readKeySettings();
  await withBtnBusy(btn || $("#pp-key-apply"), async () => {
    if (!(await loadMatteForLayer(layer))) return;
    if (!(await pushMatteHistoryBefore(layer))) {
      setStatus(t("pp.historySnapshotFailed"));
      return;
    }
    if (!commitChromaKey(settings)) {
      setStatus(t("pp.keyNoChange"));
      matteLog("去色：未检测到可剔除的键色像素", "操作");
      return;
    }
    setStatus(t("pp.matteSaving"));
    await restoreMatteLayerImage(layer);
    clearMattePreview();
    matteTargetLayerId = null;
    matteHideTargetInPreview = false;
    matteDirty = false;
    await fetchBounds();
    await refreshPreview({ skipInboxSync: true });
    drawOverlay();
    setStatus(t("pp.keyDone"));
    matteLog(`去色 ${settings.key_hex} fuzz=${settings.fuzz}%: ${layer.name || layer.id}`, "操作");
  }).catch((err) => {
    if (err) {
      setStatus(err.message);
      matteLog(`去色失败: ${err.message}`, "系统");
    }
  });
}

function bindKeyControls() {
  $("#pp-key-preset-magenta")?.addEventListener("click", () => setKeyPresetMode("magenta"));
  $("#pp-key-preset-custom")?.addEventListener("click", () => setKeyPresetMode("custom"));
  $("#pp-key-fuzz")?.addEventListener("input", syncKeyFuzzFromRange);
  $("#pp-key-fuzz-num")?.addEventListener("change", syncKeyFuzzFromNumber);
  $("#pp-key-feather")?.addEventListener("input", syncKeyFeatherFromRange);
  $("#pp-key-feather-num")?.addEventListener("change", syncKeyFeatherFromNumber);
  $("#pp-key-color")?.addEventListener("input", () => {
    const hex = normalizeKeyHex($("#pp-key-color")?.value);
    if (hex) syncKeyColorInputs(hex);
  });
  $("#pp-key-color-text")?.addEventListener("input", () => {
    const hex = normalizeKeyHex($("#pp-key-color-text")?.value);
    if (hex && $("#pp-key-color")) $("#pp-key-color").value = hex.toLowerCase();
  });
  $("#pp-key-apply")?.addEventListener("click", (e) => {
    e.preventDefault();
    void applyChromaKey(e.currentTarget);
  });
  setKeyPresetMode("magenta");
}

function fillProps() {
  const layer = selectedLayer();
  const form = $("#pp-props-form");
  const imgFs = $("#pp-image-fields");
  const txtFs = $("#pp-text-fields");
  const cropFs = $("#pp-crop-fields");
  if (!layer) {
    form.querySelectorAll("input,textarea,select").forEach((el) => {
      if (el.name && !isCanvasSizeField(el)) el.disabled = true;
    });
    ensureCanvasSizeInputsEnabled();
    const subjectSplit = $("#pp-subject-split");
    if (subjectSplit) subjectSplit.hidden = true;
    const smartSplitBtn = $("#pp-smart-split");
    if (smartSplitBtn) smartSplitBtn.disabled = true;
    updateCropToggleState(null);
    return;
  }
  form.querySelectorAll("input,textarea,select").forEach((el) => {
    if (!isCanvasSizeField(el)) el.disabled = false;
  });
  ensureCanvasSizeInputsEnabled();
  const isImg = layer.type === "image";
  const isText = layer.type === "text";
  form.name.value = layer.name || "";
  const opPct = Math.min(100, Math.max(0, Math.round((layer.opacity ?? 1) * 100)));
  form.opacity.value = opPct === 100 ? 1 : opPct / 100;
  if (form.opacity_slider) form.opacity_slider.value = opPct;
  const blendMode = PP_BLEND_MODES.includes(layer.blend_mode) ? layer.blend_mode : "normal";
  if (form.blend_mode) form.blend_mode.value = blendMode;
  if (form.blend_enabled) form.blend_enabled.checked = !!layer.blend_enabled;
  syncBlendColorInputs(form, layer.blend_color || "");
  const blendPct = Math.min(100, Math.max(0, Math.round((layer.blend_amount ?? 1) * 100)));
  if (form.blend_amount) form.blend_amount.value = blendPct === 100 ? 1 : blendPct / 100;
  if (form.blend_amount_slider) form.blend_amount_slider.value = blendPct;
  const blendAcc = $("#pp-acc-blend");
  updateBlendUi();
  updateBlendModeDesc();
  if (blendAcc && (layer.blend_enabled || isImg)) blendAcc.open = true;
  const xf = layer.transform || {};
  const fmtOffset = (v) => {
    const n = Number(v);
    if (!Number.isFinite(n)) return 0;
    return Math.abs(n) < 1e-6 ? 0 : Math.round(n * 100) / 100;
  };
  form.offset_x.value = fmtOffset(xf.offset_x);
  form.offset_y.value = fmtOffset(xf.offset_y);
  const scalePct = Math.round((xf.scale ?? 1) * 100);
  form.scale_pct.value = scalePct;
  if (form.scale_slider) form.scale_slider.value = Math.min(300, Math.max(5, scalePct));
  const tag = $("#scale-tag");
  if (tag) tag.textContent = `${scalePct}%`;
  const rot = Math.round(xf.rotation_deg ?? 0);
  if (form.rotation) form.rotation.value = rot;
  if (form.rotation_slider) form.rotation_slider.value = Math.min(180, Math.max(-180, rot));
  const rotTag = $("#rotation-tag");
  if (rotTag) rotTag.textContent = `${rot}°`;
  const pxPct = Math.round((xf.pivot_x ?? 0.5) * 100);
  const pyPct = Math.round((xf.pivot_y ?? 0.5) * 100);
  if (form.pivot_x_pct) form.pivot_x_pct.value = pxPct;
  if (form.pivot_y_pct) form.pivot_y_pct.value = pyPct;
  syncFlipButtons(xf);
  syncPivotGrid(pxPct, pyPct);

  if (blendAcc) blendAcc.hidden = isText;
  imgFs.hidden = !isImg;
  const matteAcc = $("#pp-acc-matte");
  if (matteAcc) matteAcc.hidden = !isImg;
  txtFs.hidden = !isText;
  cropFs.hidden = !isImg;
  if (isImg && form.source) {
    form.source.value = layer.is_subject ? "$asset" : layer.source || "";
    form.source.readOnly = layer.is_subject;
  }
  if (isImg && layer.crop) {
    form.crop_x.value = layer.crop.x ?? 0;
    form.crop_y.value = layer.crop.y ?? 0;
    form.crop_w.value = layer.crop.w ?? 0;
    form.crop_h.value = layer.crop.h ?? 0;
  } else if (form.crop_x) {
    form.crop_x.value = form.crop_y.value = form.crop_w.value = form.crop_h.value = "";
  }
  if (isText) {
    layer.text = layer.text || defaultTextStyle();
    ensureFontOption(layer.text.font_family);
    form.text_content.value = layer.text.content || "";
    form.font_size.value = layer.text.font_size || 40;
    form.text_color.value = layer.text.color || "#ffffff";
    form.font_family.value = layer.text.font_family || "PingFang SC";
  }
  updateCropToggleState(layer);
  const autoCropBtn = $("#pp-auto-crop");
  if (autoCropBtn) autoCropBtn.disabled = !isImg || !!layer.locked;
  const matteToggle = $("#pp-matte-toggle");
  if (matteToggle) matteToggle.disabled = !isImg || !!layer.locked;
  const matteBorder = $("#pp-matte-border");
  const matteWand = $("#pp-matte-wand");
  if (matteBorder) matteBorder.disabled = !isImg || !!layer.locked || cropMode;
  if (matteWand) matteWand.disabled = !isImg || !!layer.locked || cropMode;
  const keyApply = $("#pp-key-apply");
  const keySection = $("#pp-blend-key-section");
  if (keyApply) keyApply.disabled = !isImg || !!layer.locked;
  keySection?.querySelectorAll("input,button").forEach((el) => {
    if (el.id === "pp-key-apply") return;
    el.disabled = !isImg || !!layer.locked;
  });
  const smartSplitBtn = $("#pp-smart-split");
  const subjectSplit = $("#pp-subject-split");
  const subj = subjectLayer();
  const showSplit = !!(subj && layer.is_subject && layer.id === subj.id);
  if (subjectSplit) subjectSplit.hidden = !showSplit;
  if (smartSplitBtn) smartSplitBtn.disabled = !showSplit || !!layer.locked;
  const multiChk = $("#pp-export-multi-layers");
  if (multiChk) multiChk.disabled = subjectMode !== "inbox";
  if (cropMode) updateCropModeUi();
  if (isMatteSessionLocked()) syncMatteEditLock();
}

function syncFlipButtons(xf = selectedLayer()?.transform || {}) {
  $("#pp-flip-h")?.classList.toggle("active", !!xf.flip_h);
  $("#pp-flip-v")?.classList.toggle("active", !!xf.flip_v);
}

function syncPivotGrid(pxPct = 50, pyPct = 50) {
  const grid = $("#pp-pivot-grid");
  if (!grid) return;
  const tol = 8;
  grid.querySelectorAll(".pp-pivot-cell").forEach((btn) => {
    const [x, y] = (btn.dataset.pivot || "0.5,0.5").split(",").map(parseFloat);
    const match = Math.abs(x * 100 - pxPct) <= tol && Math.abs(y * 100 - pyPct) <= tol;
    btn.classList.toggle("active", match);
  });
}

function pointInPolygon(px, py, corners) {
  if (!corners?.length) return false;
  let inside = false;
  for (let i = 0, j = corners.length - 1; i < corners.length; j = i++) {
    const [xi, yi] = corners[i];
    const [xj, yj] = corners[j];
    if ((yi > py) !== (yj > py) && px < ((xj - xi) * (py - yi)) / (yj - yi + 1e-9) + xi) {
      inside = !inside;
    }
  }
  return inside;
}

function unrotateDocPoint(docX, docY, pivot, deg) {
  const norm = normalizeRotationDeg(deg);
  if (!pivot || Math.abs(norm) < 0.001) return { x: docX, y: docY };
  const rad = (norm * Math.PI) / 180;
  const dx = docX - pivot.x;
  const dy = docY - pivot.y;
  return {
    x: pivot.x + dx * Math.cos(rad) - dy * Math.sin(rad),
    y: pivot.y + dx * Math.sin(rad) + dy * Math.cos(rad),
  };
}

function layerActionBar(layer) {
  const isSubject = layer.is_subject;
  return `
    <div class="layer-actions">
      <button type="button" class="icon-btn" data-act="up" title="${escapeHtml(t("pp.moveUp"))}">${icon("chevronUp")}</button>
      <button type="button" class="icon-btn" data-act="down" title="${escapeHtml(t("pp.moveDown"))}">${icon("chevronDown")}</button>
      <button type="button" class="icon-btn" data-act="dup" title="${escapeHtml(t("pp.duplicate"))}" ${isSubject ? "disabled" : ""}>${icon("copy")}</button>
      <button type="button" class="icon-btn danger" data-act="del" title="${escapeHtml(t("pp.delete"))}" ${isSubject ? "disabled" : ""}>${icon("trash")}</button>
    </div>`;
}

function layerListOrder() {
  return [...(stack?.layers || [])].reverse().map((l) => l.id);
}

function effectiveSelectedLayerIds() {
  const ids = (stack?.layers || []).map((l) => l.id);
  const sel = selectedLayerIds.filter((id) => ids.includes(id));
  if (sel.length) return sel;
  return selectedId && ids.includes(selectedId) ? [selectedId] : [];
}

function isLayerRowSelected(layerId) {
  if (selectedLayerIds.length) return selectedLayerIds.includes(layerId);
  return layerId === selectedId;
}

function toggleLayerInSelection(layerId) {
  const ordered = layerListOrder();
  const base =
    selectedLayerIds.length > 0 ? selectedLayerIds : selectedId ? [selectedId] : [];
  const set = new Set(base);
  if (set.has(layerId)) {
    set.delete(layerId);
    if (!set.size) set.add(layerId);
  } else {
    set.add(layerId);
  }
  return ordered.filter((id) => set.has(id));
}

function handleLayerListClick(layerId, ev) {
  const ordered = layerListOrder();
  const idx = ordered.indexOf(layerId);
  if (idx < 0) return;

  const shift = ev.shiftKey;
  const toggle = ev.metaKey || ev.ctrlKey;

  if (shift && lastLayerClickId) {
    const anchor = ordered.indexOf(lastLayerClickId);
    if (anchor >= 0) {
      const lo = Math.min(anchor, idx);
      const hi = Math.max(anchor, idx);
      const range = ordered.slice(lo, hi + 1);
      if (toggle) {
        const set = new Set(
          selectedLayerIds.length ? selectedLayerIds : [selectedId].filter(Boolean),
        );
        for (const id of range) set.add(id);
        selectedLayerIds = ordered.filter((id) => set.has(id));
      } else {
        selectedLayerIds = range;
      }
      selectedId = layerId;
      lastLayerClickId = layerId;
      renderLayers();
      fillProps();
      drawOverlay();
      return;
    }
  }

  if (toggle) {
    selectedLayerIds = toggleLayerInSelection(layerId);
    selectedId = layerId;
    lastLayerClickId = layerId;
    renderLayers();
    fillProps();
    drawOverlay();
    return;
  }

  selectLayer(layerId);
  lastLayerClickId = layerId;
}

function renderLayers() {
  const box = $("#pp-layer-list");
  if (!box) return;
  const layers = stack?.layers || [];
  box.dataset.dense = layers.length > 10 ? "1" : "";
  box.innerHTML = "";
  for (const layer of [...layers].reverse()) {
    const isPrimary = layer.id === selectedId;
    const inSelection = isLayerRowSelected(layer.id);
    const visible = layer.visible !== false;
    const locked = !!layer.locked;
    const row = document.createElement("div");
    row.className =
      "layer-item" +
      (isPrimary ? " active" : "") +
      (inSelection && !isPrimary ? " multi-selected" : "") +
      (soloId === layer.id ? " solo" : "");
    if (!visible) row.classList.add("layer-hidden");
    if (locked) row.classList.add("layer-locked");

    row.innerHTML = `
      <div class="layer-row">
        <button type="button" class="layer-main" data-id="${layer.id}">
          <span class="layer-type-icon">${layerTypeIcon(layer)}</span>
          <span class="layer-name">
            <span class="layer-title">${escapeHtml(layer.name || layer.id)}</span>
            <span class="layer-sub">${escapeHtml(layerListSubtitle(layer))}</span>
          </span>
        </button>
        <div class="layer-quick">
          <button type="button" class="icon-btn ${visible ? "" : "off"}" data-act="vis" data-id="${layer.id}" title="${escapeHtml(visible ? t("pp.hide") : t("pp.show"))}">${icon(visible ? "eye" : "eyeOff")}</button>
          <button type="button" class="icon-btn ${locked ? "on" : ""}" data-act="lock" data-id="${layer.id}" title="${escapeHtml(locked ? t("pp.unlock") : t("pp.lock"))}">${icon(locked ? "lock" : "unlock")}</button>
        </div>
      </div>
      ${isPrimary ? layerActionBar(layer) : ""}`;

    row.querySelector(".layer-main")?.addEventListener("click", (e) => {
      if (e.altKey) {
        soloId = layer.id;
        $("#pp-solo").checked = true;
        schedulePreview();
        drawOverlay();
      }
      handleLayerListClick(layer.id, e);
    });
    row.querySelector(".layer-main")?.addEventListener("dblclick", (e) => {
      e.preventDefault();
      selectLayer(layer.id);
      zoomFit();
    });

    row.querySelectorAll(".icon-btn[data-act]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        handleLayerAction(btn.dataset.act, layer.id);
      });
    });

    box.appendChild(row);
  }
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function handleLayerAction(act, layerId) {
  if (isMatteSessionLocked()) return;
  const layer = stack.layers.find((l) => l.id === layerId);
  if (!layer) return;
  if (act === "vis") {
    pushHistoryBefore({ includeImages: false }).then(() => {
      layer.visible = !(layer.visible !== false);
      renderLayers();
      schedulePreview();
      markStackStructuralChange();
    });
    return;
  }
  if (act === "lock") {
    layer.locked = !layer.locked;
    renderLayers();
    fillProps();
    return;
  }
  if (layerId !== selectedId) selectLayer(layerId);
  if (act === "up") moveLayer(1);
  else if (act === "down") moveLayer(-1);
  else if (act === "dup") duplicateLayer();
  else if (act === "del") deleteLayer();
}

function bindRangeSync() {
  const form = $("#pp-props-form");
  form.scale_slider?.addEventListener("input", () => {
    form.scale_pct.value = form.scale_slider.value;
    const tag = $("#scale-tag");
    if (tag) tag.textContent = `${form.scale_slider.value}%`;
  });
  form.scale_pct?.addEventListener("input", () => {
    let v = parseInt(form.scale_pct.value, 10) || 100;
    v = Math.min(800, Math.max(1, v));
    form.scale_slider.value = Math.min(300, v);
    const tag = $("#scale-tag");
    if (tag) tag.textContent = `${v}%`;
  });
  form.opacity_slider?.addEventListener("input", () => {
    const pct = Math.min(100, Math.max(0, parseInt(form.opacity_slider.value, 10) || 0));
    form.opacity.value = pct === 100 ? 1 : pct / 100;
  });
  form.opacity?.addEventListener("input", () => {
    const o = parseFloat(form.opacity.value);
    const pct = Number.isFinite(o) ? Math.round(Math.min(1, Math.max(0, o)) * 100) : 100;
    if (form.opacity_slider) form.opacity_slider.value = pct;
  });
  form.querySelector("#pp-blend-tint-section .pp-switch")?.addEventListener("click", (e) => e.stopPropagation());
  form.blend_enabled?.addEventListener("change", () => {
    if (form.blend_enabled.checked) {
      const acc = $("#pp-acc-blend");
      if (acc) acc.open = true;
    }
    updateBlendUi();
    applyTransformLive({ previewMs: 32, bounds: false });
  });
  form.blend_mode?.addEventListener("change", () => {
    updateBlendModeDesc();
    applyTransformLive({ previewMs: 32, bounds: false });
  });
  form.blend_color?.addEventListener("input", () => {
    const hex = normalizeBlendColorText(form.blend_color.value);
    if (form.blend_color_text) form.blend_color_text.value = hex;
    applyTransformLive({ previewMs: 32, bounds: false });
  });
  form.blend_color_text?.addEventListener("input", () => {
    const hex = normalizeBlendColorText(form.blend_color_text.value);
    if (hex && form.blend_color) form.blend_color.value = hex;
    applyTransformLive({ previewMs: 32, bounds: false });
  });
  $("#pp-blend-color-clear")?.addEventListener("click", () => {
    syncBlendColorInputs(form, "");
    applyTransformLive({ previewMs: 32, bounds: false });
  });
  form.blend_amount_slider?.addEventListener("input", () => {
    const pct = Math.min(100, Math.max(0, parseInt(form.blend_amount_slider.value, 10) || 0));
    form.blend_amount.value = pct === 100 ? 1 : pct / 100;
  });
  form.blend_amount?.addEventListener("input", () => {
    const a = parseFloat(form.blend_amount.value);
    const pct = Math.min(100, Math.max(0, Math.round((Number.isFinite(a) ? a : 1) * 100)));
    if (form.blend_amount_slider) form.blend_amount_slider.value = pct;
  });
  form.rotation_slider?.addEventListener("input", () => {
    form.rotation.value = form.rotation_slider.value;
    const rotTag = $("#rotation-tag");
    if (rotTag) rotTag.textContent = `${form.rotation_slider.value}°`;
  });
  form.rotation?.addEventListener("input", () => {
    let v = parseInt(form.rotation.value, 10) || 0;
    v = Math.min(180, Math.max(-180, v));
    form.rotation.value = v;
    if (form.rotation_slider) form.rotation_slider.value = v;
    const rotTag = $("#rotation-tag");
    if (rotTag) rotTag.textContent = `${v}°`;
  });
  form.pivot_x_pct?.addEventListener("input", () => {
    syncPivotGrid(parseFloat(form.pivot_x_pct.value) || 0, parseFloat(form.pivot_y_pct?.value) || 50);
  });
  form.pivot_y_pct?.addEventListener("input", () => {
    syncPivotGrid(parseFloat(form.pivot_x_pct?.value) || 50, parseFloat(form.pivot_y_pct.value) || 0);
  });
}

function bindTransformControls() {
  $("#pp-flip-h")?.addEventListener("click", async () => {
    const layer = selectedLayer();
    if (!layer) return;
    await pushHistoryBefore({ includeImages: false });
    layer.transform = { ...defaultTransform(), ...(layer.transform || {}) };
    layer.transform.flip_h = !layer.transform.flip_h;
    syncFlipButtons(layer.transform);
    fillProps();
    applyTransformLive({ previewMs: 0, bounds: true });
  });
  $("#pp-flip-v")?.addEventListener("click", async () => {
    const layer = selectedLayer();
    if (!layer) return;
    await pushHistoryBefore({ includeImages: false });
    layer.transform = { ...defaultTransform(), ...(layer.transform || {}) };
    layer.transform.flip_v = !layer.transform.flip_v;
    syncFlipButtons(layer.transform);
    fillProps();
    applyTransformLive({ previewMs: 0, bounds: true });
  });
  $("#pp-rotation-reset")?.addEventListener("click", async () => {
    const layer = selectedLayer();
    if (!layer?.transform) return;
    await pushHistoryBefore({ includeImages: false });
    layer.transform.rotation_deg = 0;
    fillProps();
    if (rotationPreviewState.active) {
      updateRotationPreviewLocal();
      return;
    }
    applyTransformLive({ previewMs: 0, bounds: true, forceServerPreview: true });
  });
  $("#pp-pivot-grid")?.addEventListener("click", async (e) => {
    const btn = e.target.closest(".pp-pivot-cell");
    if (!btn) return;
    const layer = selectedLayer();
    if (!layer) return;
    const [x, y] = (btn.dataset.pivot || "0.5,0.5").split(",").map(parseFloat);
    await pushHistoryBefore({ includeImages: false });
    layer.transform = layer.transform || defaultTransform();
    layer.transform.pivot_x = x;
    layer.transform.pivot_y = y;
    fillProps();
    applyTransformLive({ previewMs: 0, bounds: true });
  });
  $("#pp-acc-rotation")?.addEventListener("toggle", () => drawOverlay());
}

function initToolbarIcons() {
  const imgBtn = $("#pp-add-image")?.querySelector(".btn-icon");
  const txtBtn = $("#pp-add-text")?.querySelector(".btn-icon");
  if (imgBtn) imgBtn.innerHTML = icon("image", 14);
  if (txtBtn) txtBtn.innerHTML = icon("type", 14);
}

function selectLayer(id) {
  if (isMatteSessionLocked() && id !== matteTargetLayerId) return;
  if (rotationPreviewState.active && id !== rotationPreviewState.layerId) {
    void commitRotationPreview();
  }
  selectedId = id;
  selectedLayerIds = [id];
  renderLayers();
  fillProps();
  schedulePreview();
  drawOverlay();
}

let ppFileInput = null;

function ensureFileInput() {
  if (ppFileInput) return ppFileInput;
  ppFileInput = document.createElement("input");
  ppFileInput.type = "file";
  ppFileInput.accept = "image/png,image/jpeg,image/webp,image/gif,image/*";
  ppFileInput.hidden = true;
  document.body.appendChild(ppFileInput);
  return ppFileInput;
}

function pickImageViaInput() {
  return new Promise((resolve) => {
    const input = ensureFileInput();
    const onChange = async () => {
      input.removeEventListener("change", onChange);
      const file = input.files?.[0];
      input.value = "";
      if (!file) {
        resolve(null);
        return;
      }
      try {
        const fd = new FormData();
        fd.append("file", file);
        const res = await fetch("/api/postprocess/upload-image", {
          method: "POST",
          body: fd,
          headers: { Accept: "application/json" },
        });
        if (!res.ok) {
          const msg = (await res.text()).slice(0, 200) || res.statusText;
          throw new Error(msg);
        }
        const data = await res.json();
        resolve(data.path || null);
      } catch (err) {
        setStatus(err.message || t("pp.pickImageFailed"));
        resolve(null);
      }
    };
    input.addEventListener("change", onChange);
    input.click();
  });
}

function pickImageInitialDir() {
  const inbox = assetPaths?.inbox;
  if (!inbox) return undefined;
  const i = inbox.lastIndexOf("/");
  return i > 0 ? inbox.slice(0, i) : undefined;
}

async function pickImageFile() {
  try {
    const r = await API.post("/api/pick-image-file", {
      initial_dir: pickImageInitialDir(),
    });
    if (r.cancelled) return null;
    return r.path || r.absolute || null;
  } catch {
    return pickImageViaInput();
  }
}

function layerNameFromPath(path) {
  const base = path.split("/").pop() || "";
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(0, dot) : base || t("pp.layerImage");
}

async function onPostprocessSourcePicked() {
  const layer = selectedLayer();
  const srcInput = $("#pp-props-form input[name=source]");
  if (!layer || layer.type !== "image" || layer.is_subject || !srcInput) return;
  await pushHistoryBefore({ includeImages: true });
  applyPropsFromForm();
  if (!layer.name || layer.name === t("pp.layerImage")) {
    layer.name = layerNameFromPath(layer.source);
  }
  renderLayers();
  fillProps();
  schedulePreview();
  markStackStructuralChange();
}

function bindPostprocessPathFields() {
  initPathFields($("#pp-props-form"));
  $("#pp-props-form input[name=source]")?.addEventListener("pathpicked", () => {
    void onPostprocessSourcePicked();
  });
}

function schedulePreview(delay = 48) {
  clearTimeout(previewTimer);
  if (matteMode || matteStrokeActive || matteEntering || matteExiting || isRotationLivePreview()) {
    return;
  }
  previewTimer = setTimeout(refreshPreview, Math.max(0, delay));
}

function revokePreviewBlob() {
  if (previewBlobUrl) {
    URL.revokeObjectURL(previewBlobUrl);
    previewBlobUrl = null;
  }
}

function showPreviewEmpty(message = null) {
  const msg = message ?? t("pp.noPreview");
  const img = $("#pp-preview");
  const ph = $("#pp-preview-ph");
  revokePreviewBlob();
  img.hidden = true;
  img.removeAttribute("src");
  if (ph) {
    ph.hidden = false;
    ph.textContent = msg;
  }
}

function showPreviewLoading(message = null) {
  const msg = message ?? t("pp.loadingPreview");
  const img = $("#pp-preview");
  const ph = $("#pp-preview-ph");
  const hasImage = previewBlobUrl && !img.hidden;
  if (!hasImage) {
    revokePreviewBlob();
    img.hidden = true;
    img.removeAttribute("src");
    if (ph) {
      ph.hidden = false;
      ph.textContent = msg;
    }
  } else if (ph) {
    ph.hidden = true;
  }
}

async function setPreviewBlob(blob) {
  const img = $("#pp-preview");
  const ph = $("#pp-preview-ph");
  if (!blob.type.startsWith("image/")) {
    showPreviewEmpty(t("pp.noPreview"));
    return false;
  }
  revokePreviewBlob();
  previewBlobUrl = URL.createObjectURL(blob);
  img.src = previewBlobUrl;
  img.hidden = false;
  if (ph) ph.hidden = true;
  try {
    await waitPreviewImageLoaded(img);
    return true;
  } catch {
    showPreviewEmpty(t("pp.noPreview"));
    return false;
  }
}

async function fetchBounds(body = previewBody()) {
  const prev = boundsData;
  try {
    const data = await API.post(`/api/assets/${assetId}/postprocess/bounds`, body);
    if (data?.layers?.length || data?.raw_sizes) {
      boundsData = data;
    }
  } catch {
    if (!prev?.layers?.length) {
      boundsData = { layers: [], canvas: canvasSize(), raw_sizes: {} };
    }
  }
  return boundsData;
}

/** 抠图命中：保证目标图层在 boundsData 中（含 raw 尺寸） */
function matteBoundsBody(layerId) {
  const body = previewBody();
  const stackClone = cloneStackData(stack);
  const layer = stackClone.layers?.find((l) => l.id === layerId);
  if (layer) layer.visible = true;
  return { ...body, stack: stackClone };
}

function matteLayerRawSize(layer) {
  const fromBounds = boundsData.raw_sizes?.[layer.id];
  if (fromBounds?.w > 0 && fromBounds?.h > 0) return fromBounds;
  if (matteFullState.layerId === layer.id && matteFullState.rawW > 0 && matteFullState.rawH > 0) {
    return { w: matteFullState.rawW, h: matteFullState.rawH };
  }
  if (mattePreviewState.layerId === layer.id && mattePreviewState.rawW > 0 && mattePreviewState.rawH > 0) {
    return { w: mattePreviewState.rawW, h: mattePreviewState.rawH };
  }
  return null;
}

function synthesizeMatteLayerBounds(layer) {
  const raw = matteLayerRawSize(layer);
  if (!raw?.w || !raw?.h) return null;

  if (!boundsData.raw_sizes) boundsData.raw_sizes = {};
  boundsData.raw_sizes[layer.id] = { w: raw.w, h: raw.h };

  const cw = canvasSize().w;
  const ch = canvasSize().h;
  const xf = layer.transform || {};
  const crop = layer.crop;
  const srcW = crop?.w > 0 ? crop.w : raw.w;
  const srcH = crop?.h > 0 ? crop.h : raw.h;
  const scale = Math.max(0.01, xf.scale ?? 1);
  const sw = Math.max(1, Math.round(srcW * scale));
  const sh = Math.max(1, Math.round(srcH * scale));
  const anchor = xf.anchor || "center";
  const ax = (anchor === "top_left" ? 0 : cw / 2) + (xf.offset_x ?? 0);
  const ay = (anchor === "top_left" ? 0 : ch / 2) + (xf.offset_y ?? 0);
  const px = (xf.pivot_x ?? 0.5) * sw;
  const py = (xf.pivot_y ?? 0.5) * sh;
  const angle = normalizeRotationDeg(xf.rotation_deg ?? 0);
  const entry = {
    id: layer.id,
    x: Math.round(ax - px),
    y: Math.round(ay - py),
    w: sw,
    h: sh,
    local_w: sw,
    local_h: sh,
    pivot: { x: ax, y: ay },
    pivot_norm: { x: xf.pivot_x ?? 0.5, y: xf.pivot_y ?? 0.5 },
    visible: true,
    locked: !!layer.locked,
    type: layer.type,
    is_subject: !!layer.is_subject,
  };
  if (Math.abs(angle) > 0.01) {
    entry.corners = rotatedLayerDocCorners(sw, sh, px, py, ax, ay, angle);
  }
  if (!boundsData.layers) boundsData.layers = [];
  const idx = boundsData.layers.findIndex((b) => b.id === layer.id);
  if (idx >= 0) boundsData.layers[idx] = { ...boundsData.layers[idx], ...entry };
  else boundsData.layers.push(entry);
  return entry;
}

async function ensureMatteLayerBounds(layer) {
  if (!layer?.id) return null;
  let b = (boundsData.layers || []).find((x) => x.id === layer.id);
  let raw = matteLayerRawSize(layer);
  if (b && raw?.w && raw?.h) return b;
  await fetchBounds(matteBoundsBody(layer.id));
  b = (boundsData.layers || []).find((x) => x.id === layer.id);
  raw = matteLayerRawSize(layer);
  if (b && raw?.w && raw?.h) return b;
  return synthesizeMatteLayerBounds(layer);
}

/** apply 后从服务端拉取最新 stack（烘焙写入 source 会重置 scale/crop） */
async function reloadStackFromServer() {
  const data = await API.get(`/api/assets/${assetId}/postprocess`);
  if (!data?.stack) return;
  stack = data.stack;
  const subj = subjectLayer();
  if (subj?.id) selectedId = subj.id;
  else if (!stack.layers?.some((l) => l.id === selectedId)) {
    selectedId = stack.layers?.[0]?.id || selectedId;
  }
}

function syncPreviewAfterApply(result = {}) {
  if (result.width != null && result.height != null) {
    assetInfo.width = result.width;
    assetInfo.height = result.height;
    if (result.size_label) assetInfo.size_label = result.size_label;
    fillCanvasResizeInputs();
    updatePostprocessMeta();
  }
  syncViewportToDocument();
}

function shouldHideMatteLayerInPreview(hideMatteLayer) {
  if (!matteTargetLayerId) return false;
  if (hideMatteLayer === false) return false;
  if (hideMatteLayer === true) return true;
  return matteMode || matteHideTargetInPreview || matteStrokeActive;
}

async function refreshPreview({ skipInboxSync = false, hideMatteLayer, hideRotationLayerId } = {}) {
  clearTimeout(previewTimer);
  if (!isCanvasSizeField(document.activeElement)) {
    applyPropsFromForm();
  }
  const reqId = ++previewReq;
  if (previewAbort) previewAbort.abort();
  previewAbort = new AbortController();
  const { signal } = previewAbort;
  const matteBgOnly = shouldHideMatteLayerInPreview(hideMatteLayer);
  const rotationBgOnly = !!hideRotationLayerId;
  if (!matteBgOnly && !rotationBgOnly) {
    showPreviewLoading(t("pp.rendering"));
    setStatus(t("pp.rendering"));
  }
  let boundsPromise = null;
  try {
    const body = previewBody();
    if (skipInboxSync) body.skip_inbox_sync = true;
    let previewReqBody = body;
    if (matteBgOnly && matteTargetLayerId) {
      const stackClone = cloneStackData(stack);
      const hideLayer = stackClone.layers.find((l) => l.id === matteTargetLayerId);
      if (hideLayer) hideLayer.visible = false;
      previewReqBody = { ...body, stack: stackClone };
    } else if (hideRotationLayerId) {
      const stackClone = cloneStackData(stack);
      const hideLayer = stackClone.layers.find((l) => l.id === hideRotationLayerId);
      if (hideLayer) hideLayer.visible = false;
      previewReqBody = { ...body, stack: stackClone };
    }
    boundsPromise = fetchBounds(
      matteBgOnly && matteTargetLayerId ? matteBoundsBody(matteTargetLayerId) : body,
    );
    const res = await fetch(`/api/assets/${encodeURIComponent(assetId)}/postprocess/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(previewReqBody),
      signal,
    });
    if (signal.aborted || reqId !== previewReq) return;

    if (!res.ok) {
      showPreviewEmpty(t("pp.noPreviewFile"));
      setStatus(t("pp.noPreviewFile"));
      return;
    }
    const blob = await res.blob();
    if (signal.aborted || reqId !== previewReq) return;

    const ok = await setPreviewBlob(blob);
    if (signal.aborted || reqId !== previewReq) return;
    if (!ok) {
      setStatus(t("pp.noPreview"));
      return;
    }
    syncBoundsCanvasFromStack();
    boundsReq += 1;
    if (signal.aborted || reqId !== previewReq) return;
    if (boundsPromise) {
      await boundsPromise;
      if (signal.aborted || reqId !== previewReq) return;
    }
    if (matteMode && matteTargetLayerId) {
      const matteLayer = matteTargetLayer();
      if (matteLayer) synthesizeMatteLayerBounds(matteLayer);
    }
    layoutPreview();
    drawOverlay();
    setStatus(t("pp.ready"));
  } catch (err) {
    if (err?.name === "AbortError" || reqId !== previewReq) return;
    showPreviewEmpty(t("pp.noPreview"));
    setStatus(t("pp.previewFailed", { msg: err.message }));
  } finally {
    if (reqId === previewReq) previewAbort = null;
  }
}

function canvasToDoc(cx, cy) {
  const vp = $("#pp-viewport");
  const rect = vp.getBoundingClientRect();
  const { ox, oy } = viewportOffset();
  return { x: (cx - rect.left - ox) / view.zoom, y: (cy - rect.top - oy) / view.zoom };
}

function layoutPreview() {
  const img = $("#pp-preview");
  if (img.hidden || !previewBlobUrl) return;
  const vp = $("#pp-viewport");
  const wrap = $("#pp-viewport-wrap");
  const { docW, docH } = viewportDocSize();
  const scroll = viewportNeedsOuterScroll();

  if (scroll) {
    vp.style.flex = "0 0 auto";
    vp.style.width = `${Math.max(docW, 1)}px`;
    vp.style.height = `${Math.max(docH, 1)}px`;
    vp.style.minHeight = "0";
    wrap?.classList.remove("pp-viewport-wrap--center");
    img.style.left = "0";
    img.style.top = "0";
    img.style.width = `${docW}px`;
    img.style.height = `${docH}px`;
  } else {
    vp.style.flex = "1 1 auto";
    vp.style.width = "100%";
    vp.style.height = "100%";
    vp.style.minHeight = "280px";
    wrap?.classList.add("pp-viewport-wrap--center");
    const { ox, oy } = viewportOffset();
    img.style.width = `${docW}px`;
    img.style.height = `${docH}px`;
    img.style.left = `${ox}px`;
    img.style.top = `${oy}px`;
  }

  const crisp = isNativeViewport();
  img.style.imageRendering = crisp ? "pixelated" : "auto";
  const canvas = $("#pp-overlay");
  canvas.width = vp.clientWidth;
  canvas.height = vp.clientHeight;
  updateZoomLabel();
}

function shouldShowPivotIndicator(layer) {
  if (!layer || layer.type !== "image") return false;
  return !!$("#pp-acc-rotation")?.open;
}

function drawOverlay() {
  const canvas = $("#pp-overlay");
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const { ox, oy } = viewportOffset();
  const z = view.zoom;
  for (const b of boundsData.layers || []) {
    if (soloId && b.id !== soloId) continue;
    const isRotationTarget =
      rotationPreviewState.active && b.id === rotationPreviewState.layerId;
    const selected = b.id === selectedId;
    if (!isRotationTarget) {
      ctx.strokeStyle = selected ? "#38bdf8" : "#666";
      ctx.fillStyle = selected ? "rgba(56, 189, 248, 0.08)" : "rgba(255,255,255,0.02)";
      ctx.setLineDash(selected ? [] : [4, 3]);
      ctx.lineWidth = selected ? 2 : 1.5;
      if (b.corners?.length === 4) {
        ctx.beginPath();
        b.corners.forEach(([x, y], i) => {
          const sx = ox + x * z;
          const sy = oy + y * z;
          if (i === 0) ctx.moveTo(sx, sy);
          else ctx.lineTo(sx, sy);
        });
        ctx.closePath();
        ctx.fill();
        ctx.stroke();
      } else {
        const x0 = ox + b.x * z;
        const y0 = oy + b.y * z;
        const x1 = ox + (b.x + b.w) * z;
        const y1 = oy + (b.y + b.h) * z;
        ctx.strokeRect(x0, y0, x1 - x0, y1 - y0);
      }
    }
    if (selected && b.pivot && shouldShowPivotIndicator(selectedLayer())) {
      const px = ox + b.pivot.x * z;
      const py = oy + b.pivot.y * z;
      ctx.setLineDash([]);
      ctx.strokeStyle = "#fbbf24";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(px - 7, py);
      ctx.lineTo(px + 7, py);
      ctx.moveTo(px, py - 7);
      ctx.lineTo(px, py + 7);
      ctx.stroke();
      ctx.fillStyle = "#fbbf24";
      ctx.beginPath();
      ctx.arc(px, py, 3.5, 0, Math.PI * 2);
      ctx.fill();
    }
  }
  const showMatteLayer =
    mattePreviewState.ready &&
    mattePreviewState.layerId === matteTargetLayerId &&
    (matteMode || matteStrokeActive);
  if (showMatteLayer) {
    const layer = matteTargetLayer();
    const bounds = layer ? (boundsData.layers || []).find((b) => b.id === layer.id) : null;
    if (bounds) {
      drawMattePreview(ctx, ox, oy, z, bounds, layer);
    }
  }
  if (rotationPreviewState.active && rotationPreviewState.img) {
    const layer = selectedLayer();
    const bounds = layer ? (boundsData.layers || []).find((b) => b.id === layer.id) : null;
    if (layer?.type === "image" && bounds && layer.id === rotationPreviewState.layerId) {
      drawRotationLayerPreview(ctx, ox, oy, z, layer, bounds);
    }
  }
  drawMatteBrushCursor(ctx);
  ctx.setLineDash([]);
}

function hitTestLayer(docX, docY) {
  const layers = [...(boundsData.layers || [])].reverse();
  for (const b of layers) {
    if (!b.visible || b.locked) continue;
    if (b.corners?.length === 4) {
      if (pointInPolygon(docX, docY, b.corners)) {
        return stack.layers.find((l) => l.id === b.id);
      }
      continue;
    }
    if (docX >= b.x && docX < b.x + b.w && docY >= b.y && docY < b.y + b.h) {
      return stack.layers.find((l) => l.id === b.id);
    }
  }
  return null;
}

function zoomBy(factor) {
  if (cropMode) {
    cropZoomBy(factor);
    return;
  }
  view.zoom = Math.max(view.minZoom, Math.min(view.maxZoom, view.zoom * factor));
  layoutPreview();
  drawOverlay();
  updateZoomLabel();
}

function zoom100() {
  if (cropMode) {
    const size = activeCropRawSize();
    const fit = cropFitScale(size.w, size.h);
    cropViewZoom = fit > 0 ? 1 / fit : 1;
    scheduleCropCanvasLayout();
    return;
  }
  syncViewportToDocument();
}

function zoomFit() {
  if (cropMode) {
    cropViewZoom = 1;
    const wrap = $("#pp-crop-canvas")?.parentElement;
    if (wrap) {
      wrap.scrollLeft = 0;
      wrap.scrollTop = 0;
    }
    scheduleCropCanvasLayout();
    return;
  }
  const vp = $("#pp-viewport");
  const { w, h } = documentSize();
  const zx = vp.clientWidth / Math.max(w, 1);
  const zy = vp.clientHeight / Math.max(h, 1);
  view.zoom = Math.max(view.minZoom, Math.min(view.maxZoom, Math.min(zx, zy) * 0.92));
  view.panX = view.panY = 0;
  layoutPreview();
  drawOverlay();
  updateZoomLabel();
}

function updateZoomLabel() {
  const label = $("#pp-zoom-label");
  if (!label) return;
  if (cropMode) {
    const size = activeCropRawSize();
    const scale = getCropCanvasScale();
    const pct = size.w > 0 ? Math.round(scale * 100) : 100;
    label.textContent = `${pct}%`;
    return;
  }
  label.textContent = `${Math.round(view.zoom * 100)}%`;
}

function cropWrapEl() {
  return $("#pp-crop-canvas")?.parentElement;
}

function beginCanvasPan(e) {
  if (e.button !== 2) return false;
  if (cropMode && (cropLoading || cropSubModeLoading)) return false;

  e.preventDefault();

  if (cropMode) {
    const wrap = cropWrapEl();
    if (!wrap) return false;
    canvasPan = {
      mode: "crop",
      startX: e.clientX,
      startY: e.clientY,
      scrollLeft: wrap.scrollLeft,
      scrollTop: wrap.scrollTop,
    };
    wrap.classList.add("is-grabbing");
    return true;
  }

  if (isMatteUiTarget(e.target)) return false;

  canvasPan = {
    mode: "viewport",
    startX: e.clientX,
    startY: e.clientY,
    panX: view.panX,
    panY: view.panY,
  };
  $("#pp-viewport")?.classList.add("is-grabbing");
  return true;
}

function moveCanvasPan(e) {
  if (!canvasPan) return;
  const dx = e.clientX - canvasPan.startX;
  const dy = e.clientY - canvasPan.startY;
  if (canvasPan.mode === "crop") {
    const wrap = cropWrapEl();
    if (wrap) {
      wrap.scrollLeft = canvasPan.scrollLeft - dx;
      wrap.scrollTop = canvasPan.scrollTop - dy;
    }
    return;
  }
  view.panX = canvasPan.panX + dx;
  view.panY = canvasPan.panY + dy;
  layoutPreview();
  drawOverlay();
}

function endCanvasPan() {
  if (!canvasPan) return;
  cropWrapEl()?.classList.remove("is-grabbing");
  $("#pp-viewport")?.classList.remove("is-grabbing");
  canvasPan = null;
}

function preventCanvasContextMenu(e) {
  if (cropMode) {
    e.preventDefault();
    return;
  }
  if (e.target.closest("#pp-viewport, #pp-crop-canvas, .pp-crop-wrap")) {
    e.preventDefault();
  }
}

function cropFitScale(rawW, rawH) {
  const canvas = $("#pp-crop-canvas");
  const wrap = canvas?.parentElement;
  const wrapW = Math.max(wrap?.clientWidth || 0, 280);
  const wrapH = Math.max(wrap?.clientHeight || 0, 220);
  const maxW = Math.max(wrapW - 24, 200);
  const maxH = Math.max(wrapH - 40, 180);
  if (!rawW || !rawH) return 1;
  return Math.min(maxW / rawW, maxH / rawH);
}

function cropZoomBy(factor, e) {
  const canvas = $("#pp-crop-canvas");
  const wrap = canvas?.parentElement;
  const size = activeCropRawSize();
  if (!canvas || !size.w) return;

  const prevScale = getCropCanvasScale();
  let scrollLeft = wrap?.scrollLeft ?? 0;
  let scrollTop = wrap?.scrollTop ?? 0;
  let dispX = null;
  let dispY = null;

  if (e && wrap) {
    const rect = canvas.getBoundingClientRect();
    dispX = e.clientX - rect.left;
    dispY = e.clientY - rect.top;
    scrollLeft = wrap.scrollLeft;
    scrollTop = wrap.scrollTop;
  }

  cropViewZoom = Math.max(0.25, Math.min(8, cropViewZoom * factor));
  layoutCropCanvas(size.w, size.h);
  drawCropCanvas();
  updateZoomLabel();

  if (dispX != null && dispY != null && wrap && prevScale > 0) {
    const newScale = getCropCanvasScale();
    const ratio = newScale / prevScale;
    wrap.scrollLeft = (scrollLeft + dispX) * ratio - dispX;
    wrap.scrollTop = (scrollTop + dispY) * ratio - dispY;
  }
}

function syncViewportToDocument() {
  view.zoom = 1;
  view.panX = 0;
  view.panY = 0;
  const relayout = () => {
    layoutPreview();
    drawOverlay();
  };
  relayout();
  requestAnimationFrame(relayout);
}

function matteLog(msg, kind = "系统") {
  appendLog({ ts: new Date().toLocaleTimeString(), kind, msg: `[后处理] ${msg}` });
}

function cropLog(msg, kind = "系统") {
  appendLog({ ts: new Date().toLocaleTimeString(), kind, msg: `[裁切] ${msg}` });
}

function cropLoadDiag(label, extra = {}) {
  const parts = [
    `loadSeq=${extra.loadSeq ?? "?"}`,
    `cropLoadSeq=${cropLoadSeq}`,
    `cropMode=${cropMode}`,
    `cropLoading=${cropLoading}`,
    `cropEntering=${cropEntering}`,
    `cropSubModeLoading=${cropSubModeLoading}`,
    `rawImg=${!!cropRawImg}`,
    `epoch=${cropRawBlobEpoch}`,
  ];
  cropLog(`${label} · ${parts.join(" · ")}`, "系统");
}

function withTimeout(promise, ms, message) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(message)), ms);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (err) => {
        clearTimeout(timer);
        reject(err);
      },
    );
  });
}

async function blobToImageBitmap(blob, timeoutMs = 30000) {
  if (!blob || blob.size < 8) throw new Error(t("pp.noPreviewFile"));

  const decodeViaImage = () =>
    withTimeout(
      new Promise((resolve, reject) => {
        const img = new Image();
        const url = URL.createObjectURL(blob);
        img.onload = () => {
          URL.revokeObjectURL(url);
          resolve(img);
        };
        img.onerror = () => {
          URL.revokeObjectURL(url);
          reject(new Error(t("pp.noPreviewFile")));
        };
        img.src = url;
      }),
      timeoutMs,
      t("pp.cropTimeout"),
    );

  cropLog(`解码图层 PNG (${blob.size} bytes, type=${blob.type || "?"})…`, "系统");

  if (typeof createImageBitmap !== "function") {
    const img = await decodeViaImage();
    cropLog(`Image 解码完成 ${img.width}×${img.height}`, "系统");
    return img;
  }

  try {
    const bmp = await withTimeout(createImageBitmap(blob), timeoutMs, t("pp.cropTimeout"));
    cropLog(`createImageBitmap 完成 ${bmp.width}×${bmp.height}`, "系统");
    return bmp;
  } catch (err) {
    cropLog(`createImageBitmap 失败，回退 Image 解码: ${err.message}`, "系统");
    const img = await decodeViaImage();
    cropLog(`Image 回退解码完成 ${img.width}×${img.height}`, "系统");
    return img;
  }
}

function abortCropEnterIfStale(loadSeq, reason = "") {
  if (loadSeq === cropLoadSeq && cropMode) return false;
  const suffix = reason ? ` (${reason})` : "";
  cropLog(`进入裁切：加载已取消${suffix}`, "系统");
  cropLoadDiag("取消时状态", { loadSeq });
  if (loadSeq === cropLoadSeq) {
    cropLoading = false;
    cropEntering = false;
    updateCropModeUi();
  }
  return true;
}

function clearCropEnterLoading(loadSeq, reason = "") {
  if (loadSeq != null && loadSeq !== cropLoadSeq) return;
  cropLoading = false;
  if (reason) cropLog(reason, "系统");
  updateCropModeUi();
}

function setCropPanelMessage(msg) {
  const info = $("#pp-crop-info");
  if (info) info.textContent = msg || "";
}

function flashCropFeedback(msg) {
  setStatus(msg);
  setCropPanelMessage(msg);
  cropLog(msg, "操作");
  const btn = $("#pp-crop-toggle");
  btn?.classList.add("pp-crop-attention");
  setTimeout(() => btn?.classList.remove("pp-crop-attention"), 1200);
}

function updateCropToggleState(layer = selectedLayer()) {
  const btn = $("#pp-crop-toggle");
  if (!btn) return;
  const can = !!(layer && layer.type === "image" && !layer.locked);
  btn.disabled = false;
  btn.classList.toggle("is-disabled", !can && !cropMode);
  btn.setAttribute("aria-disabled", can || cropMode ? "false" : "true");
}

function resetStaleCropState() {
  const stage = cropStageEl();
  const panel = $("#pp-crop-panel");
  const domCropActive =
    !!stage?.classList.contains("is-crop-mode") ||
    (!!panel && !panel.hidden && !panel.classList.contains("hidden"));
  if (cropMode && !domCropActive) {
    cropLog("裁切状态不同步，已重置", "系统");
    cropMode = false;
    cropLoading = false;
    cropEntering = false;
    cropPreview = null;
  } else if (!cropMode && domCropActive) {
    cropLog("裁切界面残留，已关闭", "系统");
    setCropStageVisible(false);
    cropLoading = false;
    cropEntering = false;
  }
}

function requestCropMode() {
  resetStaleCropState();
  if (cropMode) {
    if (cropLoading || cropEntering) {
      cropLog("裁切加载未完成，强制重试", "操作");
      exitCropMode();
      void enterCropMode();
      return;
    }
    if (!cropRawImg) {
      cropLog("裁切加载失败，重试进入", "操作");
      exitCropMode();
      void enterCropMode();
    }
    return;
  }
  if (cropEntering) {
    flashCropFeedback(t("pp.cropLoading"));
    return;
  }
  const layer = selectedLayer();
  if (!layer || layer.type !== "image") {
    flashCropFeedback(t("pp.cropNeedImage"));
    return;
  }
  if (layer.locked) {
    flashCropFeedback(t("pp.layerLocked"));
    return;
  }
  void enterCropMode();
}

function invalidateCropLayerCache(layerId) {
  cropRawBlobEpoch += 1;
  cropRawBlobCache.clear();
  cropRawBlobInflight.clear();
  cropLog(`图层 PNG 缓存失效: layer=${layerId || "?"} epoch=${cropRawBlobEpoch}`, "系统");
  if (!layerId) return;
  if (matteFullState.layerId !== layerId || !matteFullState.data) return;
  invalidateMatteFull();
  if (mattePreviewState.layerId === layerId) {
    mattePreviewState.ready = false;
    mattePreviewState.layerId = null;
    mattePreviewState.canvas = null;
    mattePreviewState.ctx = null;
    mattePreviewState.imageData = null;
  }
}

async function matteFetchRawBlob(layerId, { skipFormSync = false, signal } = {}) {
  if (!skipFormSync) applyPropsFromForm();
  const body = previewBody({ layer_id: layerId });
  const t0 = performance.now();
  cropLog(`请求 layer-raw: layer=${layerId}`, "系统");
  try {
    const res = await fetch(`/api/assets/${encodeURIComponent(assetId)}/postprocess/layer-raw`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
      signal,
    });
    if (!res.ok) {
      const detail = (await res.text()).slice(0, 200) || res.statusText;
      cropLog(`layer-raw 失败 (${res.status}): ${detail}`, "系统");
      throw new Error(`${t("pp.noPreviewFile")} (${res.status}: ${detail})`);
    }
    const blob = await res.blob();
    cropLog(
      `layer-raw 完成: ${blob.size} bytes (${Math.round(performance.now() - t0)}ms)`,
      "系统",
    );
    return blob;
  } catch (err) {
    if (err?.name === "AbortError") {
      cropLog(`layer-raw 已中止 (${Math.round(performance.now() - t0)}ms)`, "系统");
      throw err;
    }
    cropLog(`layer-raw 异常 (${Math.round(performance.now() - t0)}ms): ${err.message}`, "系统");
    throw err instanceof Error ? err : new Error(String(err));
  }
}

async function loadMatteForLayer(layer) {
  if (!layer?.id || layer.type !== "image") return false;
  const ok = await ensureMattePreview(layer);
  if (!ok) {
    setStatus(t("pp.mattePreviewFailed"));
    matteLog(`抠图缓冲加载失败: ${layer.name || layer.id}`, "系统");
    return false;
  }
  await ensureMatteLayerBounds(layer);
  synthesizeMatteLayerBounds(layer);
  return true;
}

async function ensureMattePreview(layer) {
  if (!layer?.id || layer.type !== "image") return false;
  if (mattePreviewState.ready && mattePreviewState.layerId === layer.id) return true;
  if (mattePreviewLoading && mattePreviewState.layerId === layer.id) {
    while (mattePreviewLoading) {
      await new Promise((r) => setTimeout(r, 16));
    }
    return mattePreviewState.ready && mattePreviewState.layerId === layer.id;
  }
  mattePreviewLoading = true;
  try {
    return await loadMattePreview(layer.id, matteFetchRawBlob);
  } finally {
    mattePreviewLoading = false;
  }
}

function scheduleMatteLocalApply() {
  if (matteLocalRaf) return;
  matteLocalRaf = requestAnimationFrame(() => {
    matteLocalRaf = 0;
    flushMatteLocalPreview();
  });
}

function flushMatteLocalPreview() {
  if (!mattePendingPoints.length) return;
  const batch = mattePendingPoints.splice(0);
  const settings = readMatteSettings();
  if (applyMattePointsToPreview(batch, settings)) {
    if (!matteStrokeNeedsUndoSnap) {
      pushMatteUndoSnapshot();
      matteStrokeNeedsUndoSnap = true;
    }
    matteDirty = true;
    drawOverlay();
  }
}

function cancelMatteLocalRaf() {
  if (matteLocalRaf) {
    cancelAnimationFrame(matteLocalRaf);
    matteLocalRaf = 0;
  }
  mattePendingPoints = [];
}

function readMatteSettings() {
  const tol = parseInt($("#pp-matte-tol-num")?.value || $("#pp-matte-tol")?.value || "34", 10);
  return {
    color_tol: Math.max(8, Math.min(80, tol || 34)),
    step_tol: 16,
    feather: 0,
    brush_size: readMatteBrushSize(),
  };
}

function readMatteBrushSize() {
  const v = parseInt($("#pp-matte-brush-num")?.value || $("#pp-matte-brush")?.value || "1", 10);
  return Math.max(1, Math.min(50, Number.isNaN(v) ? 1 : v));
}

function syncMatteBrushFromRange() {
  const range = $("#pp-matte-brush");
  const num = $("#pp-matte-brush-num");
  if (range && num) num.value = range.value;
  drawOverlay();
}

function syncMatteBrushFromNumber() {
  const range = $("#pp-matte-brush");
  const num = $("#pp-matte-brush-num");
  if (!range || !num) return;
  let v = parseInt(num.value, 10);
  if (Number.isNaN(v)) v = 1;
  v = Math.max(1, Math.min(50, v));
  num.value = v;
  range.value = v;
  drawOverlay();
}

function matteTargetLayer() {
  if (matteTargetLayerId) {
    const layer = stack.layers.find((l) => l.id === matteTargetLayerId);
    if (layer) return layer;
  }
  return selectedLayer();
}

function matteBrushRadiusScreen(layer, brushSize) {
  const b = (boundsData.layers || []).find((x) => x.id === layer.id);
  const raw = boundsData.raw_sizes?.[layer.id];
  const z = view.zoom;
  if (!b || !raw?.w) return Math.max(3, (brushSize / 2) * z);
  const crop = layer.crop;
  const srcW = crop?.w > 0 ? crop.w : raw.w;
  const srcH = crop?.h > 0 ? crop.h : raw.h;
  const localW = b.local_w || b.w;
  const localH = b.local_h || b.h;
  const docPerRawX = localW / Math.max(srcW, 1);
  const docPerRawY = localH / Math.max(srcH, 1);
  const docPerRaw = (docPerRawX + docPerRawY) / 2;
  const radiusDoc = (brushSize / 2) * docPerRaw;
  return Math.max(3, radiusDoc * z);
}

function scheduleMatteCursorRedraw(docX, docY) {
  matteCursorDoc = { x: docX, y: docY };
  if (matteCursorRaf) return;
  matteCursorRaf = requestAnimationFrame(() => {
    matteCursorRaf = 0;
    drawOverlay();
  });
}

function drawMatteBrushCursor(ctx) {
  if (!(matteMode || matteStrokeActive) || !matteCursorDoc) return;
  const layer = matteTargetLayer();
  if (!layer || layer.type !== "image") return;

  const brushSize = readMatteBrushSize();
  const r = matteBrushRadiusScreen(layer, brushSize);
  const { ox, oy } = viewportOffset();
  const z = view.zoom;
  const sx = ox + matteCursorDoc.x * z;
  const sy = oy + matteCursorDoc.y * z;

  ctx.save();
  ctx.fillStyle = "rgba(34, 211, 238, 0.14)";
  ctx.beginPath();
  ctx.arc(sx, sy, r, 0, Math.PI * 2);
  ctx.fill();
  ctx.strokeStyle = "rgba(255, 255, 255, 0.92)";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.arc(sx, sy, r, 0, Math.PI * 2);
  ctx.stroke();
  ctx.strokeStyle = "rgba(34, 211, 238, 0.95)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.arc(sx, sy, r, 0, Math.PI * 2);
  ctx.stroke();
  ctx.restore();
}

function syncMatteToleranceFromRange() {
  const range = $("#pp-matte-tol");
  const num = $("#pp-matte-tol-num");
  if (range && num) num.value = range.value;
}

function syncMatteToleranceFromNumber() {
  const range = $("#pp-matte-tol");
  const num = $("#pp-matte-tol-num");
  if (!range || !num) return;
  let v = parseInt(num.value, 10);
  if (Number.isNaN(v)) v = 34;
  v = Math.max(8, Math.min(80, v));
  num.value = v;
  range.value = v;
}

function docToRawPixelDirect(layer, docX, docY) {
  const raw = matteLayerRawSize(layer);
  if (!raw?.w || !raw?.h) return null;
  const { w: cw, h: ch } = canvasSize();
  const xf = layer.transform || {};
  const crop = layer.crop;
  if (Math.abs((xf.rotation_deg ?? 0)) > 0.01) return null;
  if (xf.flip_h || xf.flip_v) return null;
  const scale = Math.max(0.01, xf.scale ?? 1);
  const srcW = crop?.w > 0 ? crop.w : raw.w;
  const srcH = crop?.h > 0 ? crop.h : raw.h;
  const sw = Math.max(1, Math.round(srcW * scale));
  const sh = Math.max(1, Math.round(srcH * scale));
  const anchor = xf.anchor || "center";
  const ax = (anchor === "top_left" ? 0 : cw / 2) + (xf.offset_x ?? 0);
  const ay = (anchor === "top_left" ? 0 : ch / 2) + (xf.offset_y ?? 0);
  const px = (xf.pivot_x ?? 0.5) * sw;
  const py = (xf.pivot_y ?? 0.5) * sh;
  const x0 = ax - px;
  const y0 = ay - py;
  if (docX < x0 || docX > x0 + sw || docY < y0 || docY > y0 + sh) return null;
  const relX = (docX - x0) / sw;
  const relY = (docY - y0) / sh;
  let rawX;
  let rawY;
  if (crop?.w > 0 && crop?.h > 0) {
    rawX = crop.x + relX * crop.w;
    rawY = crop.y + relY * crop.h;
  } else {
    rawX = relX * raw.w;
    rawY = relY * raw.h;
  }
  return {
    x: Math.max(0, Math.min(raw.w - 1, Math.round(rawX))),
    y: Math.max(0, Math.min(raw.h - 1, Math.round(rawY))),
  };
}

function docToRawPixelFromBoundsRect(layer, docX, docY) {
  const b = (boundsData.layers || []).find((x) => x.id === layer.id);
  const raw = matteLayerRawSize(layer);
  if (!b || !raw?.w || !raw?.h) return null;
  const bw = Math.max(1, b.w || 1);
  const bh = Math.max(1, b.h || 1);
  if (docX < b.x || docX > b.x + bw || docY < b.y || docY > b.y + bh) return null;
  const relX = (docX - b.x) / bw;
  const relY = (docY - b.y) / bh;
  const crop = layer.crop;
  let rawX;
  let rawY;
  if (crop?.w > 0 && crop?.h > 0) {
    rawX = crop.x + relX * crop.w;
    rawY = crop.y + relY * crop.h;
  } else {
    rawX = relX * raw.w;
    rawY = relY * raw.h;
  }
  return {
    x: Math.max(0, Math.min(raw.w - 1, Math.round(rawX))),
    y: Math.max(0, Math.min(raw.h - 1, Math.round(rawY))),
  };
}

function docToRawPixelCanvasFallback(layer, docX, docY) {
  if (!layer?.is_subject) return null;
  const raw = matteLayerRawSize(layer);
  if (!raw?.w || !raw?.h) return null;
  const { w: cw, h: ch } = canvasSize();
  if (cw <= 0 || ch <= 0) return null;
  if (docX < 0 || docX > cw || docY < 0 || docY > ch) return null;
  return {
    x: Math.max(0, Math.min(raw.w - 1, Math.round((docX / cw) * raw.w))),
    y: Math.max(0, Math.min(raw.h - 1, Math.round((docY / ch) * raw.h))),
  };
}

function docToRawPixel(layer, docX, docY) {
  const direct = docToRawPixelDirect(layer, docX, docY);
  if (direct) return direct;
  const fromBounds = docToRawPixelFromBoundsRect(layer, docX, docY);
  if (fromBounds) return fromBounds;
  const b = (boundsData.layers || []).find((x) => x.id === layer.id);
  const rawMeta = matteLayerRawSize(layer);
  if (!b || !rawMeta?.w || !rawMeta?.h) {
    return docToRawPixelCanvasFallback(layer, docX, docY);
  }
  const raw = { w: rawMeta.w, h: rawMeta.h };
  const xf = layer.transform || {};
  const angle = normalizeRotationDeg(xf.rotation_deg || 0);
  let sampleX = docX;
  let sampleY = docY;
  if (b.corners?.length === 4 && b.pivot && Math.abs(angle) > 0.01) {
    if (!pointInPolygon(docX, docY, b.corners)) {
      return docToRawPixelCanvasFallback(layer, docX, docY);
    }
    const flat = unrotateDocPoint(docX, docY, b.pivot, angle);
    sampleX = flat.x;
    sampleY = flat.y;
  } else if (docX < b.x || docX > b.x + b.w || docY < b.y || docY > b.y + b.h) {
    return docToRawPixelCanvasFallback(layer, docX, docY);
  }
  const localW = b.local_w || b.w;
  const localH = b.local_h || b.h;
  const pivotNorm = b.pivot_norm || { x: xf.pivot_x ?? 0.5, y: xf.pivot_y ?? 0.5 };
  let topLeftX;
  let topLeftY;
  if (b.pivot && localW && localH) {
    topLeftX = b.pivot.x - pivotNorm.x * localW;
    topLeftY = b.pivot.y - pivotNorm.y * localH;
  } else {
    topLeftX = b.x;
    topLeftY = b.y;
  }
  const relX = (sampleX - topLeftX) / localW;
  const relY = (sampleY - topLeftY) / localH;
  if (relX < 0 || relX > 1 || relY < 0 || relY > 1) {
    return docToRawPixelCanvasFallback(layer, docX, docY);
  }
  const crop = layer.crop;
  let rawX;
  let rawY;
  if (crop?.w > 0 && crop?.h > 0) {
    rawX = crop.x + relX * crop.w;
    rawY = crop.y + relY * crop.h;
  } else {
    rawX = relX * raw.w;
    rawY = relY * raw.h;
  }
  return {
    x: Math.max(0, Math.min(raw.w - 1, Math.round(rawX))),
    y: Math.max(0, Math.min(raw.h - 1, Math.round(rawY))),
  };
}

async function confirmLayerSourceEdit(layer) {
  if (!layer || layer.type !== "image") return true;
  applyPropsFromForm();
  try {
    const info = await API.post(
      `/api/assets/${encodeURIComponent(assetId)}/postprocess/layer-write-info`,
      previewBody({ layer_id: layer.id }),
    );
    if (!info.touches_source) return true;
    const name = (info.path || "").split("/").pop() || layer.name;
    return confirm(t("pp.confirmEditSource", { name }));
  } catch {
    return true;
  }
}

function commitMatteStrokePointsToFull(strokePoints) {
  if (!strokePoints?.length) return false;
  return applyMatteStrokeToFullData(strokePoints, readMatteSettings());
}

function flushMatteStrokeToLocal() {
  cancelMatteLocalRaf();
  flushMatteLocalPreview();
  const hadChange = matteStrokeNeedsUndoSnap;
  const strokePoints = hadChange && matteStroke?.points?.length ? [...matteStroke.points] : null;
  matteStrokeActive = false;
  matteStroke = null;
  matteStrokeNeedsUndoSnap = false;
  if (strokePoints && commitMatteStrokePointsToFull(strokePoints)) {
    matteDirty = true;
  }
}

async function restoreMatteLayerImage(layer) {
  applyPropsFromForm();
  const blob = await matteFullToBlob();
  if (!blob) throw new Error(t("pp.mattePreviewFailed"));
  const image_b64 = await blobToBase64(blob);
  await API.post(`/api/assets/${encodeURIComponent(assetId)}/postprocess/layer-restore-image`, {
    ...previewBody(),
    layer_id: layer.id,
    image_b64,
  });
}

async function applyBorderMatte(btn) {
  const layer = selectedLayer();
  if (!layer || layer.type !== "image") {
    setStatus(t("pp.matteNeedImage"));
    return;
  }
  if (layer.locked) return;
  const settings = readMatteSettings();
  await withBtnBusy(btn || $("#pp-matte-border"), async () => {
    if (!(await loadMatteForLayer(layer))) return;
    if (!(await pushMatteHistoryBefore(layer))) {
      setStatus(t("pp.historySnapshotFailed"));
      return;
    }
    if (!commitBorderMatte(settings)) {
      setStatus(t("pp.matteNoChange"));
      matteLog("去除外围纯色：未检测到可剔除的纯色边缘", "操作");
      return;
    }
    setStatus(t("pp.matteSaving"));
    await restoreMatteLayerImage(layer);
    clearMattePreview();
    matteTargetLayerId = null;
    matteHideTargetInPreview = false;
    matteDirty = false;
    await fetchBounds();
    await refreshPreview({ skipInboxSync: true });
    drawOverlay();
    setStatus(t("pp.matteBorderDone"));
    matteLog(`去除外围纯色: ${layer.name || layer.id}`, "操作");
  }).catch((err) => {
    if (err) {
      setStatus(err.message);
      matteLog(`去除外围纯色失败: ${err.message}`, "系统");
    }
  });
}

function beginMatteStroke(layer, docX, docY) {
  if (!layer || layer.type !== "image" || layer.locked) return false;
  if (!matteLayerRawSize(layer)) return false;
  if (!(boundsData.layers || []).find((x) => x.id === layer.id)) {
    synthesizeMatteLayerBounds(layer);
  }
  cancelMatteLocalRaf();
  mattePendingPoints = [];
  matteStrokeNeedsUndoSnap = false;
  const px = docToRawPixel(layer, docX, docY);
  if (!px) {
    matteLog(
      `笔画未命中 (${Math.round(docX)},${Math.round(docY)}) raw=${matteLayerRawSize(layer)?.w || 0}×${matteLayerRawSize(layer)?.h || 0}`,
      "系统",
    );
    return false;
  }
  matteStroke = {
    layerId: layer.id,
    points: [[px.x, px.y]],
    lastX: px.x,
    lastY: px.y,
  };
  mattePendingPoints.push([px.x, px.y]);
  scheduleMatteLocalApply();
  return true;
}

function appendMatteRawPoints(x, y) {
  if (!matteStroke) return;
  const x0 = matteStroke.lastX;
  const y0 = matteStroke.lastY;
  const dx = x - x0;
  const dy = y - y0;
  const dist = Math.hypot(dx, dy);
  if (dist < 0.5) return;
  const step = Math.max(MATTE_STROKE_STEP_PX, Math.ceil(readMatteBrushSize() / 2));
  const n = Math.max(1, Math.ceil(dist / step));
  for (let i = 1; i <= n; i++) {
    const t = i / n;
    const pt = [Math.round(x0 + dx * t), Math.round(y0 + dy * t)];
    matteStroke.points.push(pt);
    mattePendingPoints.push(pt);
  }
  matteStroke.lastX = x;
  matteStroke.lastY = y;
  scheduleMatteLocalApply();
}

function extendMatteStroke(layer, docX, docY) {
  if (!matteStroke || matteStroke.layerId !== layer.id) return;
  const px = docToRawPixel(layer, docX, docY);
  if (!px) return;
  appendMatteRawPoints(px.x, px.y);
}

function endMatteStroke() {
  cancelMatteLocalRaf();
  flushMatteLocalPreview();
  const hadChange = matteStrokeNeedsUndoSnap;
  const strokePoints = hadChange && matteStroke?.points?.length ? [...matteStroke.points] : null;
  matteStrokeActive = false;
  matteStroke = null;
  matteStrokeNeedsUndoSnap = false;
  drawOverlay();
  if (!hadChange) {
    setStatus(t("pp.matteModeHint"));
    return;
  }
  if (strokePoints && commitMatteStrokePointsToFull(strokePoints)) {
    matteDirty = true;
    updateHistoryButtons();
  }
  drawOverlay();
  setStatus(t("pp.matteModeHint"));
}

function resetMatteBannerPosition() {
  matteBannerPos.x = null;
  matteBannerPos.y = null;
  applyMatteBannerPosition();
}

function clampMatteBannerPosition(x, y) {
  const banner = $("#pp-matte-banner");
  const vp = $("#pp-viewport");
  if (!banner || !vp) return { x, y };
  const pad = 4;
  const maxX = Math.max(pad, vp.clientWidth - banner.offsetWidth - pad);
  const maxY = Math.max(pad, vp.clientHeight - banner.offsetHeight - pad);
  return {
    x: Math.max(pad, Math.min(maxX, x)),
    y: Math.max(pad, Math.min(maxY, y)),
  };
}

function applyMatteBannerPosition() {
  const banner = $("#pp-matte-banner");
  if (!banner) return;
  if (matteBannerPos.x == null || matteBannerPos.y == null) {
    banner.style.left = "50%";
    banner.style.top = "10px";
    banner.style.transform = "translateX(-50%)";
    banner.classList.remove("is-positioned");
    return;
  }
  const { x, y } = clampMatteBannerPosition(matteBannerPos.x, matteBannerPos.y);
  matteBannerPos.x = x;
  matteBannerPos.y = y;
  banner.style.left = `${x}px`;
  banner.style.top = `${y}px`;
  banner.style.transform = "none";
  banner.classList.add("is-positioned");
}

function beginMatteBannerDrag(e) {
  if (e.button !== 0) return;
  if (e.target instanceof Element && e.target.closest("#pp-matte-exit")) return;
  const banner = $("#pp-matte-banner");
  const vp = $("#pp-viewport");
  if (!banner || !vp || banner.hidden) return;
  e.preventDefault();
  e.stopPropagation();

  const bannerRect = banner.getBoundingClientRect();
  const vpRect = vp.getBoundingClientRect();
  if (matteBannerPos.x == null || matteBannerPos.y == null) {
    matteBannerPos.x = bannerRect.left - vpRect.left;
    matteBannerPos.y = bannerRect.top - vpRect.top;
    applyMatteBannerPosition();
  }

  matteBannerDrag = {
    offsetX: e.clientX - bannerRect.left,
    offsetY: e.clientY - bannerRect.top,
  };
  banner.classList.add("is-dragging");
}

function moveMatteBannerDrag(e) {
  if (!matteBannerDrag) return;
  const banner = $("#pp-matte-banner");
  const vp = $("#pp-viewport");
  if (!banner || !vp) return;
  const vpRect = vp.getBoundingClientRect();
  const next = clampMatteBannerPosition(
    e.clientX - vpRect.left - matteBannerDrag.offsetX,
    e.clientY - vpRect.top - matteBannerDrag.offsetY,
  );
  matteBannerPos.x = next.x;
  matteBannerPos.y = next.y;
  applyMatteBannerPosition();
}

function endMatteBannerDrag() {
  if (!matteBannerDrag) return;
  matteBannerDrag = null;
  $("#pp-matte-banner")?.classList.remove("is-dragging");
}

function bindMatteBannerDrag() {
  const banner = $("#pp-matte-banner");
  if (!banner || banner.dataset.dragBound === "1") return;
  banner.dataset.dragBound = "1";
  banner.addEventListener("mousedown", beginMatteBannerDrag);
  banner.addEventListener("dblclick", (e) => {
    if (e.target instanceof Element && e.target.closest("#pp-matte-exit")) return;
    e.preventDefault();
    e.stopPropagation();
    resetMatteBannerPosition();
  });
  window.addEventListener("mousemove", moveMatteBannerDrag);
  window.addEventListener("mouseup", endMatteBannerDrag);
  window.addEventListener("resize", () => {
    if (matteBannerPos.x != null) applyMatteBannerPosition();
  });
}

function resetMatteStroke() {
  matteStrokeActive = false;
  cancelMatteLocalRaf();
  matteStroke = null;
}

function syncMatteEditLock() {
  const locked = isMatteSessionLocked();
  const app = document.getElementById("pp-app");
  app?.classList.toggle("pp-matte-edit-lock", locked);

  for (const sel of [".pp-layers", ".pp-foot", ".pp-head .pp-actions"]) {
    const el = document.querySelector(sel);
    if (el) el.toggleAttribute("inert", locked);
  }

  const form = $("#pp-props-form");
  if (form) {
    form.querySelectorAll(".pp-acc").forEach((acc) => {
      const matteAcc = acc.id === "pp-acc-matte";
      acc.toggleAttribute("inert", locked && !matteAcc);
      acc.querySelectorAll("input, button, select, textarea").forEach((el) => {
        if (locked && !matteAcc) el.disabled = true;
        else if (!locked && el.id !== "pp-matte-toggle") el.disabled = false;
      });
    });
  }

  const layer = selectedLayer();
  const isImg = layer?.type === "image";
  const matteToggle = $("#pp-matte-toggle");
  if (matteToggle) matteToggle.disabled = locked ? false : !isImg || !!layer?.locked;
  const matteBorder = $("#pp-matte-border");
  const matteWand = $("#pp-matte-wand");
  if (matteBorder) matteBorder.disabled = locked ? false : !isImg || !!layer?.locked;
  if (matteWand) matteWand.disabled = locked ? false : !isImg || !!layer?.locked;
}

function updateCropModeUi() {
  const toolsGroup = document.querySelector(".pp-toolbar-group-tools");
  if (toolsGroup) {
    toolsGroup.hidden = cropMode;
    toolsGroup.toggleAttribute("hidden", cropMode);
  }
  const matteToggle = $("#pp-matte-toggle");
  if (matteToggle) {
    matteToggle.hidden = cropMode;
    matteToggle.toggleAttribute("hidden", cropMode);
  }
  const cropBtn = $("#pp-crop-toggle");
  if (cropBtn) {
    cropBtn.hidden = cropMode;
    cropBtn.toggleAttribute("hidden", cropMode);
    cropBtn.classList.toggle("is-busy", cropEntering);
    if (!cropMode) {
      cropBtn.textContent = t("pp.crop");
      cropBtn.classList.remove("active");
      cropBtn.title = t("pp.cropHint");
      updateCropToggleState();
    }
  }
}

function updateMatteModeUi() {
  const vp = $("#pp-viewport");
  const banner = $("#pp-matte-banner");
  const toggle = $("#pp-matte-toggle");
  const wandBtn = $("#pp-matte-wand");
  const inMatte = matteMode || matteStrokeActive;
  vp?.classList.toggle("pp-matte-mode", inMatte);
  if (banner) {
    banner.classList.toggle("hidden", !inMatte);
    banner.toggleAttribute("hidden", !inMatte);
    if (inMatte) applyMatteBannerPosition();
  }
  if (toggle) {
    toggle.hidden = cropMode;
    toggle.toggleAttribute("hidden", cropMode);
    toggle.classList.toggle("active", inMatte);
    toggle.textContent = inMatte ? t("pp.matteDone") : t("pp.matteWandShort");
    toggle.title = inMatte ? t("pp.matteWandHint") : t("pp.matteWandHint");
  }
  if (wandBtn) wandBtn.textContent = inMatte ? t("pp.matteDone") : t("pp.matteWand");
  syncMatteEditLock();
  updateHistoryButtons();
}

async function enterMatteMode() {
  if (matteMode || matteEntering || matteExiting) return;
  if (cropMode) exitCropMode();
  await exitMatteMode();
  const layer = selectedLayer();
  if (!layer || layer.type !== "image") {
    setStatus(t("pp.matteNeedImage"));
    return;
  }
  if (layer.locked) {
    setStatus(t("pp.matteNeedImage"));
    return;
  }
  matteEntering = true;
  try {
    if (!(await confirmLayerSourceEdit(layer))) {
      setStatus(t("pp.matteCancelled"));
      return;
    }
    matteTargetLayerId = layer.id;
    matteMode = true;
    matteDirty = false;
    matteHideTargetInPreview = true;
    updateMatteModeUi();
    setStatus(t("pp.matteLoadingPreview"));
    matteLog(`进入橡皮擦抠图: ${layer.name || layer.id}`, "操作");
    if (!(await loadMatteForLayer(layer))) {
      matteHideTargetInPreview = false;
      matteMode = false;
      matteTargetLayerId = null;
      clearMattePreview();
      updateMatteModeUi();
      return;
    }
    void pushMatteHistoryBefore(layer);
    await refreshPreview({ hideMatteLayer: true });
    matteUsingLocalComposite = true;
    drawOverlay();
    setStatus(t("pp.matteModeHint"));
  } finally {
    matteEntering = false;
  }
}

async function exitMatteMode(opts = {}) {
  const { skipPreviewRefresh = false } = opts;
  if (matteExiting) return;

  flushMatteStrokeToLocal();

  const wasMatte = matteMode;
  const layer = matteTargetLayer();
  let dirty = matteDirty;

  if (!wasMatte) return;

  matteMode = false;
  matteExiting = true;
  updateMatteModeUi();

  try {
    if (dirty && layer) {
      setStatus(t("pp.matteSaving"));
      await restoreMatteLayerImage(layer);
    }
  } catch (err) {
    matteMode = true;
    matteExiting = false;
    updateMatteModeUi();
    setStatus(err.message);
    return;
  }

  matteDirty = false;
  matteTargetLayerId = null;
  matteUsingLocalComposite = false;
  matteHideTargetInPreview = false;
  matteCursorDoc = null;
  if (matteCursorRaf) {
    cancelAnimationFrame(matteCursorRaf);
    matteCursorRaf = 0;
  }
  clearMattePreview();
  endMatteBannerDrag();
  resetMatteBannerPosition();
  updateMatteModeUi();

  if (!skipPreviewRefresh) {
    await fetchBounds();
    await refreshPreview({ skipInboxSync: true });
    drawOverlay();
    setStatus(dirty ? t("pp.matteExitSaved") : t("pp.ready"));
  }
  matteExiting = false;
  if (dirty) scheduleStackPersist({ structural: true });
}

function isEscapeKey(e) {
  return e.key === "Escape" || e.code === "Escape" || e.key === "Esc";
}

function isMatteUiTarget(el) {
  if (!(el instanceof Element)) return false;
  return !!el.closest(
    "#pp-matte-banner, .pp-canvas-toolbar, .pp-layers, .pp-props, .pp-foot, .pp-head, .pp-crop-panel",
  );
}

function isViewportMatteTarget(e) {
  if (cropMode || !matteMode) return false;
  if (isMatteUiTarget(e.target)) return false;
  if (e.target instanceof Element && e.target.closest("button, input, textarea, select, a, label")) {
    return false;
  }
  return true;
}

function handleGlobalEscape(e) {
  if (!isEscapeKey(e)) return;
  if (cropMode) {
    cancelCropMode();
    e.preventDefault();
    e.stopPropagation();
    return;
  }
  if (matteMode || matteStrokeActive) {
    void exitMatteMode();
    e.preventDefault();
    e.stopPropagation();
  }
}

function bindMatteExitControl(el, handler) {
  if (!el) return;
  el.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    handler(e);
  });
}

function toggleMatteMode() {
  if (matteMode || matteStrokeActive) void exitMatteMode();
  else void enterMatteMode();
}

function bindMatteControls() {
  $("#pp-matte-tol")?.addEventListener("input", syncMatteToleranceFromRange);
  $("#pp-matte-tol-num")?.addEventListener("change", syncMatteToleranceFromNumber);
  $("#pp-matte-brush")?.addEventListener("input", syncMatteBrushFromRange);
  $("#pp-matte-brush-num")?.addEventListener("change", syncMatteBrushFromNumber);
  $("#pp-matte-border")?.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    void applyBorderMatte(e.currentTarget);
  });
  bindMatteExitControl($("#pp-matte-wand"), () => toggleMatteMode());
  bindMatteExitControl($("#pp-matte-toggle"), () => toggleMatteMode());
  bindMatteExitControl($("#pp-matte-exit"), () => void exitMatteMode());
  bindMatteBannerDrag();
}

function bindCropControls() {
  const root = $("#pp-app") || document;
  root.addEventListener("click", (e) => {
    const cropBtn = e.target.closest("#pp-crop-toggle");
    if (!cropBtn) return;
    e.preventDefault();
    requestCropMode();
  });
  $("#pp-crop-toggle")?.addEventListener("mouseenter", () => {
    const layer = selectedLayer();
    if (layer?.type === "image" && !layer.locked && !cropMode) {
      void fetchCropLayerBlob(layer).catch(() => {});
    }
  });
}

function bindEscapeHandler() {
  const opts = { capture: true };
  window.addEventListener("keydown", handleGlobalEscape, opts);
  document.addEventListener("keydown", handleGlobalEscape, opts);
}

// ── 裁切（矩形取景 + 矩阵裁切 + 自由裁切） ─────────────────

async function fetchCropLayerBlob(layer, { signal } = {}) {
  const reqEpoch = cropRawBlobEpoch;
  const id = layer.id;
  const cached = cropRawBlobCache.get(id);
  if (cached && cached.epoch === cropRawBlobEpoch) {
    cropLog(`命中图层缓存: ${id} (${cached.blob.size} bytes)`, "系统");
    return cached.blob;
  }

  const inflightKey = `${id}:${cropRawBlobEpoch}`;
  const pending = cropRawBlobInflight.get(inflightKey);
  if (pending) {
    cropLog(`复用进行中的图层请求: ${layer.name || id} · epoch=${reqEpoch}`, "系统");
    return pending;
  }

  const task = (async () => {
    const t0 = performance.now();
    cropLog(
      `拉取裁切图层: ${layer.name || id} · layer-raw · epoch=${reqEpoch} · subject=${isSubjectLayer(layer)}`,
      "系统",
    );
    try {
      const blob = await matteFetchRawBlob(id, { skipFormSync: true, signal });
      if (!blob || blob.size < 8) throw new Error(t("pp.noPreviewFile"));
      if (reqEpoch === cropRawBlobEpoch) {
        cropRawBlobCache.set(id, { epoch: cropRawBlobEpoch, blob });
      }
      cropLog(`layer-raw 总耗时 ${Math.round(performance.now() - t0)}ms`, "系统");
      return blob;
    } finally {
      if (cropRawBlobInflight.get(inflightKey) === task) {
        cropRawBlobInflight.delete(inflightKey);
      }
    }
  })();

  cropRawBlobInflight.set(inflightKey, task);
  return task;
}

async function loadCropRgbaWork(layer) {
  if (!layer?.id || layer.type !== "image") throw new Error(t("pp.cropNeedImage"));
  if (freeCropWork?.layerId === layer.id) {
    return { data: freeCropWork.data, w: freeCropWork.w, h: freeCropWork.h };
  }
  if (matrixWork?.layerId === layer.id) {
    return { data: matrixWork.data, w: matrixWork.w, h: matrixWork.h };
  }
  const blob = await fetchCropLayerBlob(layer);
  const decoded = await decodeBlobToRgba(blob);
  return applyLayerCropToRgba(decoded.data, decoded.w, decoded.h, layer.crop);
}

function drawCropLoadingCanvas() {
  const canvas = $("#pp-crop-canvas");
  if (!canvas) return;
  const wrap = canvas.parentElement;
  const w = Math.max((wrap?.clientWidth || 0) - 24, 280);
  const h = Math.max((wrap?.clientHeight || 0) - 40, 220);
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#14161e";
  ctx.fillRect(0, 0, w, h);
  ctx.fillStyle = "#9ca3af";
  ctx.font = "600 14px Inter, system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(t("pp.cropLoading"), w / 2, h / 2);
}

function drawCropErrorCanvas(message) {
  const canvas = $("#pp-crop-canvas");
  if (!canvas) return;
  const wrap = canvas.parentElement;
  const w = Math.max((wrap?.clientWidth || 0) - 24, 280);
  const h = Math.max((wrap?.clientHeight || 0) - 40, 220);
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#14161e";
  ctx.fillRect(0, 0, w, h);
  ctx.fillStyle = "#f87171";
  ctx.font = "600 14px Inter, system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  const lines = String(message || t("pp.cropFailed")).split(/\n/);
  const lineH = 20;
  const startY = h / 2 - ((lines.length - 1) * lineH) / 2;
  lines.forEach((line, i) => ctx.fillText(line, w / 2, startY + i * lineH));
}

function resetFreeCropState() {
  freeCropWork = null;
  freeCropSelection = null;
  freeCropClipboard = null;
  freeCropDirty = false;
  freeCropDrag = null;
  if (freeCropBitmap) {
    freeCropBitmap.close?.();
    freeCropBitmap = null;
  }
}

function freeCropPatchCanvasOffset(px, py, pw, ph) {
  const { w: cw, h: ch } = documentSize();
  const bounds = boundsData.layers?.find((b) => b.id === freeCropWork?.layerId);
  if (!bounds || !freeCropWork) {
    return {
      offset_x: px + pw / 2 - cw / 2,
      offset_y: py + ph / 2 - ch / 2,
    };
  }
  const scaleX = bounds.w / Math.max(1, freeCropWork.w);
  const scaleY = bounds.h / Math.max(1, freeCropWork.h);
  const docX = bounds.x + px * scaleX;
  const docY = bounds.y + py * scaleY;
  const docW = pw * scaleX;
  const docH = ph * scaleY;
  return {
    offset_x: docX + docW / 2 - cw / 2,
    offset_y: docY + docH / 2 - ch / 2,
  };
}

async function uploadFreeCropPatchPng(data, w, h) {
  const blob = await rgbaToPngBlob(data, w, h);
  const fd = new FormData();
  fd.append("file", new File([blob], "patch.png", { type: "image/png" }));
  const res = await fetch("/api/postprocess/upload-image", {
    method: "POST",
    body: fd,
    headers: { Accept: "application/json" },
  });
  if (!res.ok) {
    const msg = (await res.text()).slice(0, 120) || res.statusText;
    throw new Error(msg);
  }
  const payload = await res.json();
  return payload.path;
}

async function addFreeCropPatchAsLayer(data, w, h, px, py, { nameKey = "pp.freeCropPasteLayerName" } = {}) {
  const baseLayer = stack.layers.find((l) => l.id === freeCropWork?.layerId);
  if (!baseLayer || !freeCropWork) return null;
  const path = await uploadFreeCropPatchPng(data, w, h);
  const { offset_x, offset_y } = freeCropPatchCanvasOffset(px, py, w, h);
  const id = `i_${Math.random().toString(36).slice(2, 8)}`;
  const newLayer = {
    id,
    name: t(nameKey),
    type: "image",
    visible: true,
    opacity: 1,
    blend_mode: "normal",
    blend_color: "",
    blend_amount: 1,
    blend_enabled: false,
    source: path,
    transform: { ...defaultTransform(), offset_x, offset_y },
  };
  const idx = stack.layers.findIndex((l) => l.id === baseLayer.id);
  stack.layers.splice(idx + 1, 0, newLayer);
  selectedId = id;
  selectedLayerIds = [id];
  renderLayers();
  fillProps();
  markStackStructuralChange();
  await fetchBounds();
  schedulePreview();
  return newLayer;
}

function drawCropCloseButton(ctx, cx, cy) {
  ctx.save();
  ctx.beginPath();
  ctx.arc(cx, cy, CROP_CLOSE_BTN_R, 0, Math.PI * 2);
  ctx.fillStyle = "rgba(15, 17, 24, 0.92)";
  ctx.fill();
  ctx.strokeStyle = "#f87171";
  ctx.lineWidth = 1.5;
  ctx.stroke();
  const arm = 4;
  ctx.strokeStyle = "#fff";
  ctx.lineWidth = 2;
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.moveTo(cx - arm, cy - arm);
  ctx.lineTo(cx + arm, cy + arm);
  ctx.moveTo(cx + arm, cy - arm);
  ctx.lineTo(cx - arm, cy + arm);
  ctx.stroke();
  ctx.restore();
}

function cropCloseButtonCanvasPos() {
  const r = cropRectCanvas();
  if (!r || r.w < 16 || r.h < 16) return null;
  return { cx: r.x2 - 6, cy: r.y2 - 6 };
}

function hitTestCropCloseButton(cx, cy) {
  const pos = cropCloseButtonCanvasPos();
  if (!pos) return false;
  const dx = cx - pos.cx;
  const dy = cy - pos.cy;
  const hit = CROP_CLOSE_BTN_R + 3;
  return dx * dx + dy * dy <= hit * hit;
}

function clearRectCropSelection() {
  cropPreview = null;
  cropDrag = null;
  drawCropCanvas();
  updateCropInfo();
  setStatus(t("pp.cropSelectionCleared"));
}

async function rebuildFreeCropBitmap() {
  if (!freeCropWork) return;
  if (freeCropBitmap) freeCropBitmap.close?.();
  const c = document.createElement("canvas");
  c.width = freeCropWork.w;
  c.height = freeCropWork.h;
  c.getContext("2d").putImageData(
    new ImageData(freeCropWork.data, freeCropWork.w, freeCropWork.h),
    0,
    0,
  );
  freeCropBitmap = await createImageBitmap(c);
}

async function loadFreeCropWork(layer) {
  const cropped = await loadCropRgbaWork(layer);
  freeCropWork = {
    data: new Uint8ClampedArray(cropped.data),
    w: cropped.w,
    h: cropped.h,
    layerId: layer.id,
  };
  freeCropSelection = null;
  freeCropDirty = false;
  await rebuildFreeCropBitmap();
}

function clampFreeSelection(sel) {
  if (!freeCropWork || !sel) return null;
  return clampRect(sel.x, sel.y, sel.w, sel.h, freeCropWork.w, freeCropWork.h);
}

function validFreeCropSelection() {
  const sel = clampFreeSelection(freeCropSelection);
  if (!sel || sel.w < 2 || sel.h < 2) return null;
  return sel;
}

function markFreeCropDirty() {
  freeCropDirty = true;
}

async function refreshFreeCropView() {
  await rebuildFreeCropBitmap();
  drawCropCanvas();
  updateCropInfo();
}

function freeCropCopySelection() {
  const sel = validFreeCropSelection();
  if (!sel || !freeCropWork) {
    setStatus(t("pp.freeCropNeedSelection"));
    return false;
  }
  freeCropClipboard = {
    data: copyRgbaRegion(freeCropWork.data, freeCropWork.w, sel.x, sel.y, sel.w, sel.h),
    w: sel.w,
    h: sel.h,
  };
  setStatus(t("pp.freeCropCopy"));
  return true;
}

async function freeCropCutSelection() {
  const sel = validFreeCropSelection();
  if (!sel || !freeCropWork) {
    setStatus(t("pp.freeCropNeedSelection"));
    return false;
  }
  await pushHistoryBefore({ includeImages: true });
  const patch = copyRgbaRegion(freeCropWork.data, freeCropWork.w, sel.x, sel.y, sel.w, sel.h);
  freeCropClipboard = { data: new Uint8ClampedArray(patch), w: sel.w, h: sel.h };
  clearRgbaRegion(freeCropWork.data, freeCropWork.w, sel.x, sel.y, sel.w, sel.h);
  markFreeCropDirty();
  await refreshFreeCropView();
  await addFreeCropPatchAsLayer(patch, sel.w, sel.h, sel.x, sel.y, {
    nameKey: "pp.freeCropCutLayerName",
  });
  setStatus(t("pp.freeCropCutLayer"));
  return true;
}

async function freeCropClearSelectionPixels() {
  const sel = validFreeCropSelection();
  if (!sel || !freeCropWork) {
    setStatus(t("pp.freeCropNeedSelection"));
    return false;
  }
  clearRgbaRegion(freeCropWork.data, freeCropWork.w, sel.x, sel.y, sel.w, sel.h);
  markFreeCropDirty();
  await refreshFreeCropView();
  return true;
}

async function freeCropPasteClipboard() {
  if (!freeCropClipboard?.data || !freeCropWork) {
    setStatus(t("pp.freeCropClipboardEmpty"));
    return false;
  }
  await pushHistoryBefore({ includeImages: true });
  let px = Math.round((freeCropWork.w - freeCropClipboard.w) / 2);
  let py = Math.round((freeCropWork.h - freeCropClipboard.h) / 2);
  const sel = validFreeCropSelection();
  if (sel) {
    px = sel.x;
    py = sel.y;
  }
  await addFreeCropPatchAsLayer(
    freeCropClipboard.data,
    freeCropClipboard.w,
    freeCropClipboard.h,
    px,
    py,
    { nameKey: "pp.freeCropPasteLayerName" },
  );
  drawCropCanvas();
  updateCropInfo();
  setStatus(t("pp.freeCropPasteLayer"));
  return true;
}

async function freeCropDeleteTarget() {
  if (selectedId && freeCropWork && selectedId !== freeCropWork.layerId) {
    const layer = stack.layers.find((l) => l.id === selectedId);
    if (layer?.type === "image" && !layer.is_subject && !layer.locked) {
      await deleteLayer();
      return true;
    }
  }
  return freeCropClearSelectionPixels();
}

async function commitFreeCrop() {
  const layer = selectedLayer();
  if (!layer || !freeCropWork || freeCropWork.layerId !== layer.id) return;
  if (!freeCropDirty) {
    setStatus(t("pp.freeCropNothing"));
    exitCropMode();
    return;
  }
  if (!(await confirmLayerSourceEdit(layer))) {
    setStatus(t("pp.cropCancelled"));
    return;
  }
  await pushHistoryBefore({ includeImages: true });
  const outW = freeCropWork.w;
  const outH = freeCropWork.h;
  const outData = new Uint8ClampedArray(freeCropWork.data);
  const blob = await rgbaToPngBlob(outData, outW, outH);
  const image_b64 = await blobToBase64(blob);
  await API.post(`/api/assets/${encodeURIComponent(assetId)}/postprocess/layer-restore-image`, {
    ...previewBody(),
    layer_id: layer.id,
    image_b64,
  });
  delete layer.crop;
  invalidateCropLayerCache(layer.id);
  await persistStackNow();
  fillProps();
  await fetchBounds();
  await refreshPreview({ skipInboxSync: true });
  exitCropMode();
  drawOverlay();
  scheduleStackPersist({ structural: true });
  cropLog(`自由裁切写回: ${layer.name || layer.id} → ${outW}×${outH}`, "操作");
  setStatus(t("pp.freeCropDone"));
}

function isSubjectLayer(layer) {
  return !!(layer?.is_subject || layer?.source === "$asset");
}

function cropStageEl() {
  return $("#pp-stage") || document.querySelector(".pp-stage");
}

function setCropStageVisible(active) {
  const stage = cropStageEl();
  const wrap = $("#pp-viewport-wrap");
  const panel = $("#pp-crop-panel");
  stage?.classList.toggle("is-crop-mode", active);
  if (wrap) {
    wrap.classList.toggle("hidden", active);
    wrap.toggleAttribute("hidden", active);
  }
  if (panel) {
    panel.classList.toggle("hidden", !active);
    panel.toggleAttribute("hidden", !active);
  }
}

function updateCropTabsUi() {
  const rectTab = $("#pp-crop-tab-rect");
  const matrixTab = $("#pp-crop-tab-matrix");
  const freeTab = $("#pp-crop-tab-free");
  const rectUi = $("#pp-crop-rect-ui");
  const matrixUi = $("#pp-crop-matrix-ui");
  const freeUi = $("#pp-crop-free-ui");
  const title = $("#pp-crop-title");

  if (rectTab) {
    rectTab.hidden = false;
    rectTab.toggleAttribute("hidden", false);
    rectTab.classList.toggle("active", cropSubMode === "rect");
  }
  if (matrixTab) {
    matrixTab.hidden = false;
    matrixTab.toggleAttribute("hidden", false);
    matrixTab.classList.toggle("active", cropSubMode === "matrix");
  }
  if (freeTab) {
    freeTab.hidden = false;
    freeTab.toggleAttribute("hidden", false);
    freeTab.classList.toggle("active", cropSubMode === "free");
  }
  if (rectUi) {
    const show = cropSubMode === "rect";
    rectUi.classList.toggle("hidden", !show);
    rectUi.toggleAttribute("hidden", !show);
  }
  if (matrixUi) {
    const show = cropSubMode === "matrix";
    matrixUi.classList.toggle("hidden", !show);
    matrixUi.toggleAttribute("hidden", !show);
  }
  if (freeUi) {
    const show = cropSubMode === "free";
    freeUi.classList.toggle("hidden", !show);
    freeUi.toggleAttribute("hidden", !show);
  }
  if (title) {
    if (cropSubMode === "matrix") title.textContent = t("pp.cropTabMatrix");
    else if (cropSubMode === "free") title.textContent = t("pp.cropTabFree");
    else title.textContent = t("pp.cropTitle");
  }
}

function switchCropSubMode(mode) {
  void switchCropSubModeAsync(mode);
}

async function switchCropSubModeAsync(mode) {
  const layer = selectedLayer();
  if (!layer || layer.type !== "image") return;
  if (mode === cropSubMode && !cropSubModeLoading) return;
  cropSubModeLoading = true;
  cropLog(`切换裁切子模式 → ${mode}`, "操作");
  drawCropCanvas();
  try {
    if (mode === "rect") {
      try {
        if (!cropRawImg || cropRawSize.w <= 0) await loadRectCropWork(layer);
      } catch (err) {
        cropLog(`矩形裁切加载失败: ${err.message}`, "系统");
        setStatus(err.message);
        drawCropErrorCanvas(err.message);
        return;
      }
    } else if (mode === "matrix") {
      cropLog(`切换矩阵裁切: ${layer.name || layer.id}`, "操作");
      if (!(await ensureMatrixWork(layer))) return;
    } else if (mode === "free") {
      try {
        if (!freeCropWork || freeCropWork.layerId !== layer.id) await loadFreeCropWork(layer);
      } catch (err) {
        cropLog(`自由裁切加载失败: ${err.message}`, "系统");
        setStatus(err.message);
        return;
      }
    }
    cropSubMode = mode;
    cropDrag = null;
    matrixDrag = null;
    freeCropDrag = null;
    updateCropTabsUi();
    scheduleCropCanvasLayout();
  } finally {
    cropSubModeLoading = false;
    updateCropModeUi();
  }
}

async function loadMatrixWork(layer) {
  const t0 = performance.now();
  const cropped = await loadCropRgbaWork(layer);
  matrixWork = {
    data: new Uint8ClampedArray(cropped.data),
    w: cropped.w,
    h: cropped.h,
    layerId: layer.id,
  };
  const def = defaultGridLines(cropped.w, cropped.h);
  matrixHLines = def.hLines;
  matrixVLines = def.vLines;
  matrixRemoved = new Set();
  matrixSelectedLine = null;

  if (matrixRectRawImg) {
    matrixRectRawImg.close?.();
    matrixRectRawImg = null;
  }
  const c = document.createElement("canvas");
  c.width = cropped.w;
  c.height = cropped.h;
  c.getContext("2d").putImageData(new ImageData(cropped.data, cropped.w, cropped.h), 0, 0);
  matrixRectRawImg = await createImageBitmap(c);
  cropLog(
    `矩阵缓冲就绪: ${layer.name || layer.id} ${cropped.w}×${cropped.h} (${Math.round(performance.now() - t0)}ms)`,
    "系统",
  );
}

async function ensureMatrixWork(layer) {
  if (!layer || layer.type !== "image") return false;
  if (matrixWork?.layerId === layer.id && matrixRectRawImg) return true;
  try {
    setStatus(t("pp.cropLoading"));
    await loadMatrixWork(layer);
    return true;
  } catch (err) {
    cropLog(`矩阵缓冲加载失败: ${err.message}`, "系统");
    setStatus(err.message || t("pp.noPreviewFile"));
    return false;
  }
}

async function loadRectCropWork(layer, { signal, loadSeq } = {}) {
  const t0 = performance.now();
  cropLog(`loadRectCropWork 开始: ${layer.name || layer.id}`, "系统");
  let blob;
  try {
    blob = await withTimeout(
      fetchCropLayerBlob(layer, { signal }),
      60000,
      t("pp.cropTimeout"),
    );
  } catch (err) {
    cropLog(
      `拉取图层 PNG 失败 (${Math.round(performance.now() - t0)}ms): ${err?.message || err}`,
      "系统",
    );
    throw err;
  }
  cropLog(`PNG 已就绪 (${Math.round(performance.now() - t0)}ms, ${blob.size} bytes)`, "系统");
  if (loadSeq != null && abortCropEnterIfStale(loadSeq, "PNG 后")) return;

  if (cropRawImg) cropRawImg.close?.();
  let bitmap;
  try {
    bitmap = await blobToImageBitmap(blob);
  } catch (err) {
    cropLog(
      `解码 PNG 失败 (${Math.round(performance.now() - t0)}ms): ${err?.message || err}`,
      "系统",
    );
    throw err;
  }
  if (loadSeq != null && abortCropEnterIfStale(loadSeq, "解码后")) {
    bitmap.close?.();
    return;
  }
  cropRawImg = bitmap;
  cropRawSize = { w: cropRawImg.width, h: cropRawImg.height };
  const liveLayer = selectedLayer();
  const crop = liveLayer?.id === layer.id ? liveLayer.crop : layer.crop;
  if (crop?.w > 0 && crop?.h > 0) {
    cropPreview = { ...crop };
  } else {
    cropPreview = null;
  }
  cropLog(
    `loadRectCropWork 完成 ${cropRawSize.w}×${cropRawSize.h} (${Math.round(performance.now() - t0)}ms)`,
    "系统",
  );
}

function openCropPropsPanel() {
  const cropAcc = $("#pp-crop-fields");
  if (cropAcc) cropAcc.open = true;
}

function scheduleCropCanvasLayout() {
  requestAnimationFrame(() => {
    drawCropCanvas();
    updateCropInfo();
    updateZoomLabel();
    requestAnimationFrame(drawCropCanvas);
  });
}

function layoutCropCanvas(rawW, rawH) {
  const canvas = $("#pp-crop-canvas");
  if (!canvas || !rawW || !rawH) return 0;
  const fit = cropFitScale(rawW, rawH);
  const scale = Math.max(0.05, Math.min(8, fit * cropViewZoom));
  canvas.width = Math.max(1, Math.round(rawW * scale));
  canvas.height = Math.max(1, Math.round(rawH * scale));
  canvas.style.width = `${canvas.width}px`;
  canvas.style.height = `${canvas.height}px`;
  return scale;
}

async function enterCropMode() {
  resetStaleCropState();
  if (cropEntering) {
    flashCropFeedback(t("pp.cropLoading"));
    return;
  }
  if (cropMode) {
    if (!cropRawImg && !cropLoading && !cropEntering) {
      cropLog("裁切加载失败，重试进入", "操作");
      exitCropMode();
      return enterCropMode();
    }
    return;
  }
  const layer = selectedLayer();
  if (!layer || layer.type !== "image") {
    flashCropFeedback(t("pp.cropNeedImage"));
    return;
  }
  if (layer.locked) {
    flashCropFeedback(t("pp.layerLocked"));
    return;
  }

  const loadSeq = ++cropLoadSeq;
  cropSubMode = "rect";
  cropMode = true;
  cropLoading = true;
  cropEntering = true;
  cropViewZoom = 1;
  cropDrag = null;
  matrixDrag = null;
  freeCropDrag = null;
  cropPreview = null;
  cropRawImg = null;

  setCropStageVisible(true);
  updateCropModeUi();
  openCropPropsPanel();
  updateCropTabsUi();
  drawCropLoadingCanvas();
  setCropPanelMessage(t("pp.cropLoading"));

  const stitch = $("#pp-matrix-auto-stitch");
  if (stitch) stitch.checked = true;
  setStatus(t("pp.cropLoading"));
  cropLog(`进入裁切: ${layer.name || layer.id} · subject=${isSubjectLayer(layer)}`, "操作");
  cropLoadDiag("enterCropMode 开始", { loadSeq });

  cropLayerFetchAbort?.abort();
  const loadAbort = new AbortController();
  cropLayerFetchAbort = loadAbort;

  const t0 = performance.now();
  const loadWatchdog = setTimeout(() => {
    if (loadSeq !== cropLoadSeq || !cropLoading) return;
    cropLoadDiag("加载超过 8s 仍在进行", { loadSeq });
  }, 8000);

  try {
    if (matteMode || matteStrokeActive) {
      cropLog("进入裁切前先退出抠图模式", "系统");
      await exitMatteMode({ skipPreviewRefresh: true });
      if (abortCropEnterIfStale(loadSeq, "退出抠图后")) return;
      setStatus(t("pp.cropLoading"));
      setCropPanelMessage(t("pp.cropLoading"));
      drawCropLoadingCanvas();
    }
    if (abortCropEnterIfStale(loadSeq, "加载前")) return;
    await loadRectCropWork(layer, { signal: loadAbort.signal, loadSeq });
    if (abortCropEnterIfStale(loadSeq, "loadRectCropWork 后")) return;
    if (!cropRawImg) {
      throw new Error(t("pp.noPreviewFile"));
    }
    cropLoading = false;
    updateCropModeUi();
    scheduleCropCanvasLayout();
    setCropPanelMessage("");
    cropLog(
      `矩形裁切就绪: ${cropRawSize.w}×${cropRawSize.h} (${Math.round(performance.now() - t0)}ms)`,
      "系统",
    );
    setStatus(
      isSubjectLayer(layer) ? t("pp.cropModeEnterSubject") : t("pp.cropModeEnter"),
    );
  } catch (err) {
    if (err?.name === "AbortError") {
      cropLog(`进入裁切已中止 (${Math.round(performance.now() - t0)}ms)`, "系统");
      clearCropEnterLoading(loadSeq, "AbortError 清理 cropLoading");
      if (abortCropEnterIfStale(loadSeq, "AbortError")) return;
      return;
    }
    if (abortCropEnterIfStale(loadSeq, "catch 前")) return;
    cropLoading = false;
    const msg = err?.message || String(err);
    cropLog(`进入裁切失败 (${Math.round(performance.now() - t0)}ms): ${msg}`, "系统");
    cropLoadDiag("失败时状态", { loadSeq });
    drawCropErrorCanvas(msg);
    setCropPanelMessage(msg);
    setStatus(`${t("pp.cropFailed")}: ${msg}`);
    updateCropModeUi();
  } finally {
    clearTimeout(loadWatchdog);
    if (cropLayerFetchAbort === loadAbort) cropLayerFetchAbort = null;
    if (loadSeq === cropLoadSeq) cropEntering = false;
    if (loadSeq === cropLoadSeq) updateCropModeUi();
    if (loadSeq === cropLoadSeq && cropLoading) {
      cropLoadDiag("finally 仍 loading", { loadSeq });
    }
  }
}

function resetMatrixCropState() {
  matrixWork = null;
  matrixHLines = [];
  matrixVLines = [];
  matrixRemoved = new Set();
  matrixDrag = null;
  matrixSelectedLine = null;
  matrixDeleteHover = null;
  if (matrixRectRawImg) {
    matrixRectRawImg.close?.();
    matrixRectRawImg = null;
  }
}

function exitCropMode() {
  cropLog("退出裁切模式", "操作");
  cropLoadDiag("exitCropMode");
  cropLoadSeq += 1;
  cropLayerFetchAbort?.abort();
  cropLayerFetchAbort = null;
  cropMode = false;
  cropLoading = false;
  cropEntering = false;
  cropSubModeLoading = false;
  cropViewZoom = 1;
  cropPreview = null;
  cropDrag = null;
  cropSubMode = "rect";
  cropViewZoom = 1;
  endCanvasPan();
  resetMatrixCropState();
  resetFreeCropState();
  setCropStageVisible(false);
  setCropPanelMessage("");
  updateCropModeUi();
  if (cropRawImg) {
    cropRawImg.close?.();
    cropRawImg = null;
  }
  layoutPreview();
  drawOverlay();
}

function activeCropRawSize() {
  if (cropSubMode === "free" && freeCropWork) {
    return { w: freeCropWork.w, h: freeCropWork.h };
  }
  if (cropSubMode === "matrix" && matrixWork) {
    return { w: matrixWork.w, h: matrixWork.h };
  }
  return cropRawSize;
}

function getCropCanvasScale() {
  const canvas = $("#pp-crop-canvas");
  const size = activeCropRawSize();
  if (!size.w || !canvas.width) return 1;
  return canvas.width / size.w;
}

function cropEventPos(e, { clampToRaw = false } = {}) {
  const canvas = $("#pp-crop-canvas");
  const rect = canvas.getBoundingClientRect();
  const sx = rect.width > 0 ? canvas.width / rect.width : 1;
  const sy = rect.height > 0 ? canvas.height / rect.height : 1;
  const cx = (e.clientX - rect.left) * sx;
  const cy = (e.clientY - rect.top) * sy;
  const scale = getCropCanvasScale();
  let rx = cx / scale;
  let ry = cy / scale;
  if (clampToRaw) {
    const size = activeCropRawSize();
    if (size.w > 0) rx = Math.max(0, Math.min(size.w, rx));
    if (size.h > 0) ry = Math.max(0, Math.min(size.h, ry));
  }
  return { cx, cy, rx, ry, scale };
}

function scheduleCropDrawDuringDrag() {
  if (cropDrawRaf) return;
  cropDrawRaf = requestAnimationFrame(() => {
    cropDrawRaf = 0;
    drawCropCanvas();
    updateCropInfo();
  });
}

function beginCropPointerCapture(canvas, e) {
  if (!canvas || e.button !== 0) return;
  try {
    canvas.setPointerCapture(e.pointerId);
    cropPointerCaptureId = e.pointerId;
  } catch {
    cropPointerCaptureId = null;
  }
}

function endCropPointerCapture(canvas, e) {
  if (!canvas) return;
  if (cropPointerCaptureId != null && canvas.hasPointerCapture?.(cropPointerCaptureId)) {
    try {
      canvas.releasePointerCapture(cropPointerCaptureId);
    } catch {
      /* ignore */
    }
  }
  cropPointerCaptureId = null;
}

function finishCropDrag(e) {
  const cropCanvas = $("#pp-crop-canvas");
  if (cropSubMode === "rect" && cropDrag?.mode === "create") {
    if (!cropPreview || cropPreview.w < 4 || cropPreview.h < 4) {
      cropPreview = null;
    }
  }
  cropDrag = null;
  matrixDrag = null;
  freeCropDrag = null;
  endCropPointerCapture(cropCanvas, e);
  if (cropDrawRaf) {
    cancelAnimationFrame(cropDrawRaf);
    cropDrawRaf = 0;
  }
  drawCropCanvas();
  updateCropInfo();
}

function updateFreeCropSelectionFromPointer(e) {
  if (!freeCropDrag || !freeCropWork) return;
  const { rx, ry } = cropEventPos(e, { clampToRaw: true });
  let x = Math.min(freeCropDrag.anchor.x, rx);
  let y = Math.min(freeCropDrag.anchor.y, ry);
  let w = Math.max(1, Math.abs(rx - freeCropDrag.anchor.x));
  let h = Math.max(1, Math.abs(ry - freeCropDrag.anchor.y));
  if (e.shiftKey || freeCropDrag.square) {
    const side = Math.max(w, h);
    w = h = side;
    if (rx < freeCropDrag.anchor.x) x = freeCropDrag.anchor.x - side;
    if (ry < freeCropDrag.anchor.y) y = freeCropDrag.anchor.y - side;
  }
  freeCropSelection = clampRect(x, y, w, h, freeCropWork.w, freeCropWork.h);
  scheduleCropDrawDuringDrag();
}

function handleCropPointerMove(e) {
  const cropCanvas = $("#pp-crop-canvas");
  if (canvasPan?.mode === "crop") {
    moveCanvasPan(e);
    return;
  }
  if (!cropMode || cropLoading || cropSubModeLoading) return;
  const { cx, cy, rx, ry } = cropEventPos(e, { clampToRaw: !!freeCropDrag });

  if (cropSubMode === "free") {
    if (freeCropDrag) {
      updateFreeCropSelectionFromPointer(e);
    } else {
      cropCanvas.style.cursor = "crosshair";
    }
    return;
  }

  if (cropSubMode === "matrix") {
    if (matrixDrag && matrixWork) {
      const raw = cropEventPos(e, { clampToRaw: true });
      if (matrixDrag.type === "h") {
        const sorted = normalizeGridLines(matrixHLines, matrixVLines, matrixWork.w, matrixWork.h)
          .hLines;
        const next = [...sorted];
        next[matrixDrag.index] = clampLineMove(next, matrixDrag.index, raw.ry, matrixWork.h);
        matrixHLines = next;
      } else {
        const sorted = normalizeGridLines(matrixHLines, matrixVLines, matrixWork.w, matrixWork.h)
          .vLines;
        const next = [...sorted];
        next[matrixDrag.index] = clampLineMove(next, matrixDrag.index, raw.rx, matrixWork.w);
        matrixVLines = next;
      }
      onMatrixGridChanged();
      scheduleCropDrawDuringDrag();
    } else if (matrixWork) {
      const scale = getCropCanvasScale();
      const { hLines, vLines } = normalizeGridLines(
        matrixHLines,
        matrixVLines,
        matrixWork.w,
        matrixWork.h,
      );
      const prevHover = matrixDeleteHover;
      const delHit = hitTestMatrixLineDelete(
        cx,
        cy,
        scale,
        hLines,
        vLines,
        cropCanvas.width,
        cropCanvas.height,
      );
      matrixDeleteHover = delHit;
      if (delHit) {
        cropCanvas.style.cursor = "pointer";
        if (matrixDeleteHoverChanged(prevHover, delHit)) drawCropCanvas();
        return;
      }
      if (matrixDeleteHoverChanged(prevHover, null)) drawCropCanvas();
      const lineHit = hitTestGridLine(cx, cy, rx, ry, scale, hLines, vLines);
      if (lineHit?.type === "h") cropCanvas.style.cursor = "ns-resize";
      else if (lineHit?.type === "v") cropCanvas.style.cursor = "ew-resize";
      else cropCanvas.style.cursor = "crosshair";
    }
    return;
  }

  if (cropDrag) {
    const raw = cropEventPos(e, { clampToRaw: true });
    if (cropDrag.mode === "create") {
      cropPreview = applyCropCreate(cropDrag.anchor, raw.rx, raw.ry, e.shiftKey || cropDrag.square);
    } else if (cropDrag.mode === "move") {
      const dx = raw.rx - cropDrag.start.x;
      const dy = raw.ry - cropDrag.start.y;
      cropPreview = clampCrop({
        x: cropDrag.orig.x + dx,
        y: cropDrag.orig.y + dy,
        w: cropDrag.orig.w,
        h: cropDrag.orig.h,
      });
    } else if (cropDrag.mode === "resize") {
      cropPreview = applyCropResize(
        cropDrag.orig,
        cropDrag.handle,
        raw.rx,
        raw.ry,
        e.shiftKey || cropDrag.square,
      );
    }
    scheduleCropDrawDuringDrag();
  } else {
    cropCanvas.style.cursor = cropCursorForHit(hitTestCrop(cx, cy));
  }
}

function clampCrop(c) {
  const rw = cropRawSize.w;
  const rh = cropRawSize.h;
  let x = Math.round(c.x);
  let y = Math.round(c.y);
  let w = Math.round(c.w);
  let h = Math.round(c.h);
  if (w < 1) w = 1;
  if (h < 1) h = 1;
  if (x < 0) {
    w += x;
    x = 0;
  }
  if (y < 0) {
    h += y;
    y = 0;
  }
  if (x + w > rw) w = rw - x;
  if (y + h > rh) h = rh - y;
  if (w < 1) w = 1;
  if (h < 1) h = 1;
  return { x, y, w, h };
}

function cropRectCanvas() {
  if (!cropPreview) return null;
  const s = getCropCanvasScale();
  return {
    x: cropPreview.x * s,
    y: cropPreview.y * s,
    w: cropPreview.w * s,
    h: cropPreview.h * s,
    x2: (cropPreview.x + cropPreview.w) * s,
    y2: (cropPreview.y + cropPreview.h) * s,
  };
}

function hitTestCrop(cx, cy) {
  if (hitTestCropCloseButton(cx, cy)) return { type: "close" };
  const r = cropRectCanvas();
  if (!r || r.w < 2 || r.h < 2) return { type: "create" };
  const H = CROP_HANDLE;
  const near = (a, b) => Math.abs(a - b) <= H;
  const onCorner = (hx, hy) => near(cx, hx) && near(cy, hy);

  if (onCorner(r.x, r.y)) return { type: "resize", handle: "nw" };
  if (onCorner(r.x2, r.y)) return { type: "resize", handle: "ne" };
  if (onCorner(r.x, r.y2)) return { type: "resize", handle: "sw" };
  if (onCorner(r.x2, r.y2)) return { type: "resize", handle: "se" };

  if (near(cx, r.x) && cy >= r.y && cy <= r.y2) return { type: "resize", handle: "w" };
  if (near(cx, r.x2) && cy >= r.y && cy <= r.y2) return { type: "resize", handle: "e" };
  if (near(cy, r.y) && cx >= r.x && cx <= r.x2) return { type: "resize", handle: "n" };
  if (near(cy, r.y2) && cx >= r.x && cx <= r.x2) return { type: "resize", handle: "s" };

  if (cx >= r.x && cx <= r.x2 && cy >= r.y && cy <= r.y2) return { type: "move" };
  return { type: "create" };
}

function cropCursorForHit(hit) {
  if (hit.type === "close") return "pointer";
  if (hit.type === "move") return "move";
  if (hit.type === "resize") {
    const map = {
      nw: "nwse-resize",
      se: "nwse-resize",
      ne: "nesw-resize",
      sw: "nesw-resize",
      n: "ns-resize",
      s: "ns-resize",
      e: "ew-resize",
      w: "ew-resize",
    };
    return map[hit.handle] || "crosshair";
  }
  return "crosshair";
}

function applyCropResize(orig, handle, rx, ry, square) {
  let x = orig.x;
  let y = orig.y;
  let x2 = orig.x + orig.w;
  let y2 = orig.y + orig.h;

  if (handle.includes("w")) x = rx;
  if (handle.includes("e")) x2 = rx;
  if (handle.includes("n")) y = ry;
  if (handle.includes("s")) y2 = ry;

  if (square) {
    const w = x2 - x;
    const h = y2 - y;
    const side = Math.max(Math.abs(w), Math.abs(h));
    const sx = w < 0 ? -1 : 1;
    const sy = h < 0 ? -1 : 1;
    if (handle.includes("w")) x = x2 - sx * side;
    else x2 = x + sx * side;
    if (handle.includes("n")) y = y2 - sy * side;
    else y2 = y + sy * side;
  }

  let nx = Math.min(x, x2);
  let ny = Math.min(y, y2);
  let nw = Math.max(1, Math.abs(x2 - x));
  let nh = Math.max(1, Math.abs(y2 - y));
  return clampCrop({ x: nx, y: ny, w: nw, h: nh });
}

function applyCropCreate(anchor, rx, ry, square) {
  let x = Math.min(anchor.x, rx);
  let y = Math.min(anchor.y, ry);
  let w = Math.max(1, Math.abs(rx - anchor.x));
  let h = Math.max(1, Math.abs(ry - anchor.y));
  if (square) {
    const side = Math.max(w, h);
    w = h = side;
    if (rx < anchor.x) x = anchor.x - side;
    if (ry < anchor.y) y = anchor.y - side;
  }
  return clampCrop({ x: Math.round(x), y: Math.round(y), w: Math.round(w), h: Math.round(h) });
}

function drawCropCanvas() {
  if (cropLoading || cropSubModeLoading) {
    drawCropLoadingCanvas();
    return;
  }
  if (cropSubMode === "free") {
    drawFreeCropCanvas();
    return;
  }
  if (cropSubMode === "matrix") {
    drawMatrixCropCanvas();
    return;
  }
  const canvas = $("#pp-crop-canvas");
  if (!cropRawImg) return;
  const scale = layoutCropCanvas(cropRawSize.w, cropRawSize.h);
  if (!scale) return;
  const ctx = canvas.getContext("2d");
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(cropRawImg, 0, 0, canvas.width, canvas.height);
  if (!cropPreview || cropPreview.w < 1 || cropPreview.h < 1) return;

  const c = cropPreview;
  const sx = c.x * scale;
  const sy = c.y * scale;
  const sw = c.w * scale;
  const sh = c.h * scale;
  ctx.fillStyle = "rgba(0,0,0,0.48)";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.clearRect(sx, sy, sw, sh);
  ctx.drawImage(cropRawImg, c.x, c.y, c.w, c.h, sx, sy, sw, sh);
  ctx.strokeStyle = "#00c8ff";
  ctx.lineWidth = 2;
  ctx.setLineDash([6, 3]);
  ctx.strokeRect(sx + 0.5, sy + 0.5, sw - 1, sh - 1);
  ctx.setLineDash([]);

  const hs = 7;
  ctx.fillStyle = "#fff";
  ctx.strokeStyle = "#00c8ff";
  ctx.lineWidth = 1.5;
  const handles = [
    [sx, sy],
    [sx + sw, sy],
    [sx, sy + sh],
    [sx + sw, sy + sh],
    [sx + sw / 2, sy],
    [sx + sw / 2, sy + sh],
    [sx, sy + sh / 2],
    [sx + sw, sy + sh / 2],
  ];
  for (const [hx, hy] of handles) {
    ctx.fillRect(hx - hs / 2, hy - hs / 2, hs, hs);
    ctx.strokeRect(hx - hs / 2 + 0.5, hy - hs / 2 + 0.5, hs - 1, hs - 1);
  }

  const label = `${c.w} × ${c.h}`;
  ctx.font = "600 12px Inter, system-ui, sans-serif";
  const tw = ctx.measureText(label).width + 16;
  const lx = sx + sw / 2 - tw / 2;
  const ly = Math.max(4, sy - 26);
  ctx.fillStyle = "rgba(15, 17, 24, 0.9)";
  ctx.fillRect(lx, ly, tw, 20);
  ctx.fillStyle = "#e8eaef";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(label, sx + sw / 2, ly + 10);

  const closePos = cropCloseButtonCanvasPos();
  if (closePos) drawCropCloseButton(ctx, closePos.cx, closePos.cy);
}

function drawFreeCropCanvas() {
  const bmp = freeCropBitmap;
  if (!bmp || !freeCropWork) return;
  const { w: rawW, h: rawH } = freeCropWork;
  const scale = layoutCropCanvas(rawW, rawH);
  if (!scale) return;
  const canvas = $("#pp-crop-canvas");
  const ctx = canvas.getContext("2d");
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(bmp, 0, 0, canvas.width, canvas.height);

  const sel = clampFreeSelection(freeCropSelection);
  if (sel && sel.w >= 1 && sel.h >= 1) {
    const sx = sel.x * scale;
    const sy = sel.y * scale;
    const sw = sel.w * scale;
    const sh = sel.h * scale;
    ctx.strokeStyle = "#00c8ff";
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 3]);
    ctx.strokeRect(sx + 0.5, sy + 0.5, sw - 1, sh - 1);
    ctx.setLineDash([]);
    ctx.fillStyle = "rgba(0, 200, 255, 0.08)";
    ctx.fillRect(sx, sy, sw, sh);
    const label = `${sel.w} × ${sel.h}`;
    ctx.font = "600 12px Inter, system-ui, sans-serif";
    const tw = ctx.measureText(label).width + 16;
    const lx = sx + sw / 2 - tw / 2;
    const ly = Math.max(4, sy - 26);
    ctx.fillStyle = "rgba(15, 17, 24, 0.9)";
    ctx.fillRect(lx, ly, tw, 20);
    ctx.fillStyle = "#e8eaef";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(label, sx + sw / 2, ly + 10);
  }
}

function drawMatrixCropCanvas() {
  const bmp = matrixRectRawImg;
  if (!bmp || !matrixWork) return;
  const { w: rawW, h: rawH } = matrixWork;
  const scale = layoutCropCanvas(rawW, rawH);
  if (!scale) return;
  const canvas = $("#pp-crop-canvas");
  const ctx = canvas.getContext("2d");
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(bmp, 0, 0, canvas.width, canvas.height);

  const { hLines, vLines } = normalizeGridLines(matrixHLines, matrixVLines, rawW, rawH);
  const xs = [0, ...vLines, rawW];
  const ys = [0, ...hLines, rawH];

  ctx.save();
  for (let r = 0; r < ys.length - 1; r++) {
    for (let c = 0; c < xs.length - 1; c++) {
      if (!matrixRemoved.has(cellKey(r, c))) continue;
      const x = xs[c] * scale;
      const y = ys[r] * scale;
      const w = (xs[c + 1] - xs[c]) * scale;
      const h = (ys[r + 1] - ys[r]) * scale;
      ctx.fillStyle = "rgba(239, 68, 68, 0.42)";
      ctx.fillRect(x, y, w, h);
      ctx.strokeStyle = "rgba(248, 113, 113, 0.9)";
      ctx.lineWidth = 1.5;
      ctx.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1);
    }
  }
  ctx.restore();

  ctx.strokeStyle = "rgba(56, 189, 248, 0.95)";
  ctx.lineWidth = 1.5;
  ctx.setLineDash([5, 4]);
  for (let i = 0; i < hLines.length; i++) {
    const y = hLines[i];
    const selected = matrixSelectedLine?.type === "h" && matrixSelectedLine.index === i;
    ctx.strokeStyle = selected ? "rgba(251, 191, 36, 0.98)" : "rgba(56, 189, 248, 0.95)";
    ctx.lineWidth = selected ? 2.5 : 1.5;
    const sy = y * scale;
    ctx.beginPath();
    ctx.moveTo(0, sy);
    ctx.lineTo(canvas.width, sy);
    ctx.stroke();
  }
  for (let i = 0; i < vLines.length; i++) {
    const x = vLines[i];
    const selected = matrixSelectedLine?.type === "v" && matrixSelectedLine.index === i;
    ctx.strokeStyle = selected ? "rgba(251, 191, 36, 0.98)" : "rgba(56, 189, 248, 0.95)";
    ctx.lineWidth = selected ? 2.5 : 1.5;
    const sx = x * scale;
    ctx.beginPath();
    ctx.moveTo(sx, 0);
    ctx.lineTo(sx, canvas.height);
    ctx.stroke();
  }
  ctx.setLineDash([]);

  const handles = matrixLineDeleteHandles(scale, hLines, vLines, canvas.width, canvas.height);
  for (const handle of handles) {
    const hovered =
      matrixDeleteHover?.type === handle.type && matrixDeleteHover?.index === handle.index;
    drawMatrixLineDeleteHandle(ctx, handle.cx, handle.cy, { hovered });
  }

  const autoStitch = $("#pp-matrix-auto-stitch")?.checked !== false;
  const out = previewMatrixSize(rawW, rawH, {
    hLines,
    vLines,
    removedCells: matrixRemoved,
    autoStitch,
  });
  const label = `${out.w} × ${out.h}`;
  ctx.font = "600 12px Inter, system-ui, sans-serif";
  const tw = ctx.measureText(label).width + 16;
  const lx = canvas.width / 2 - tw / 2;
  const ly = 8;
  ctx.fillStyle = "rgba(15, 17, 24, 0.9)";
  ctx.fillRect(lx, ly, tw, 20);
  ctx.fillStyle = "#e8eaef";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(label, canvas.width / 2, ly + 10);
}

function syncCropToForm() {
  if (!cropPreview) return;
  const form = $("#pp-props-form");
  if (!form.crop_x) return;
  form.crop_x.value = cropPreview.x;
  form.crop_y.value = cropPreview.y;
  form.crop_w.value = cropPreview.w;
  form.crop_h.value = cropPreview.h;
}

function updateCropInfo() {
  if (cropSubMode === "free" && freeCropWork) {
    const sel = validFreeCropSelection();
    let text = `${freeCropWork.w} × ${freeCropWork.h} px`;
    if (sel) {
      text = t("pp.freeCropPreview", { w: sel.w, h: sel.h, x: sel.x, y: sel.y });
    }
    $("#pp-crop-info").textContent = text;
    const badge = $("#pp-crop-size");
    if (badge) badge.textContent = `${freeCropWork.w} × ${freeCropWork.h} px`;
    return;
  }
  if (cropSubMode === "matrix" && matrixWork) {
    const autoStitch = $("#pp-matrix-auto-stitch")?.checked !== false;
    const { hLines, vLines } = normalizeGridLines(
      matrixHLines,
      matrixVLines,
      matrixWork.w,
      matrixWork.h,
    );
    const out = previewMatrixSize(matrixWork.w, matrixWork.h, {
      hLines,
      vLines,
      removedCells: matrixRemoved,
      autoStitch,
    });
    const text = t("pp.matrixCropPreview", { w: out.w, h: out.h, n: matrixRemoved.size });
    $("#pp-crop-info").textContent = text;
    const badge = $("#pp-crop-size");
    if (badge) badge.textContent = `${out.w} × ${out.h} px`;
    updateMatrixToolbarState();
    return;
  }
  if (cropSubMode === "rect" && cropRawSize.w) {
    const badge = $("#pp-crop-size");
    if (badge) badge.textContent = `${cropRawSize.w} × ${cropRawSize.h} px`;
    if (!cropPreview) {
      $("#pp-crop-info").textContent = t("pp.cropDragToSelect");
      return;
    }
  }
  if (!cropPreview) return;
  const c = cropPreview;
  const text = `${c.w} × ${c.h} px · 位置 (${c.x}, ${c.y})`;
  $("#pp-crop-info").textContent = text;
  const badge = $("#pp-crop-size");
  if (badge) badge.textContent = `${c.w} × ${c.h} px`;
  syncCropToForm();
}

async function commitMatrixCrop() {
  const layer = selectedLayer();
  if (!layer || !matrixWork || matrixWork.layerId !== layer.id) return;
  if (matrixRemoved.size === 0) {
    setStatus(t("pp.matrixCropNothing"));
    return;
  }
  if (!(await confirmLayerSourceEdit(layer))) {
    setStatus(t("pp.cropCancelled"));
    return;
  }

  const autoStitch = $("#pp-matrix-auto-stitch")?.checked !== false;
  const { hLines, vLines } = normalizeGridLines(
    matrixHLines,
    matrixVLines,
    matrixWork.w,
    matrixWork.h,
  );
  const result = applyMatrixCrop(matrixWork.data, matrixWork.w, matrixWork.h, {
    hLines,
    vLines,
    removedCells: matrixRemoved,
    autoStitch,
  });

  await pushHistoryBefore({ includeImages: true });
  const blob = await rgbaToPngBlob(result.data, result.w, result.h);
  const image_b64 = await blobToBase64(blob);
  await API.post(`/api/assets/${encodeURIComponent(assetId)}/postprocess/layer-restore-image`, {
    ...previewBody(),
    layer_id: layer.id,
    image_b64,
  });
  delete layer.crop;
  invalidateCropLayerCache(layer.id);
  await persistStackNow();
  fillProps();
  await fetchBounds();
  await refreshPreview({ skipInboxSync: true });
  exitCropMode();
  drawOverlay();
  scheduleStackPersist({ structural: true });
  cropLog(
    `矩阵裁切写回: ${layer.name || layer.id} ${matrixWork.w}×${matrixWork.h} → ${result.w}×${result.h}` +
      ` · 标记 ${matrixRemoved.size} 格 · 自动合并=${autoStitch ? "开" : "关"}`,
    "操作",
  );
  setStatus(t("pp.matrixCropDone"));
}

function isFullBleedCrop(crop, rawSize = cropRawSize) {
  if (!crop || !rawSize?.w || !rawSize?.h) return false;
  return crop.x <= 0 && crop.y <= 0 && crop.w >= rawSize.w && crop.h >= rawSize.h;
}

async function commitCrop(triggerBtn) {
  if (!cropMode) return;

  await withBtnBusy(triggerBtn || $("#pp-crop-commit"), async () => {
    try {
      if (cropSubMode === "matrix") {
        await commitMatrixCrop();
        return;
      }
      if (cropSubMode === "free") {
        await commitFreeCrop();
        return;
      }

      const layer = selectedLayer();
      if (!layer || layer.type !== "image") {
        setStatus(t("pp.cropNeedImage"));
        return;
      }
      if (!cropPreview || cropPreview.w < 1 || cropPreview.h < 1) {
        setStatus(t("pp.cropInvalidRegion"));
        return;
      }
      if (!cropRawSize.w || !cropRawSize.h) {
        setStatus(t("pp.noPreviewFile"));
        return;
      }

      const nextCrop = clampCrop(cropPreview);
      const fullBleed = isFullBleedCrop(nextCrop);
      const bakeToMaster =
        !fullBleed &&
        (subjectMode === "source" || subjectMode === "unity") &&
        (layer.is_subject || layer.source === "$asset");

      syncCropToForm();
      setStatus(t("pp.cropApplying"));
      await pushHistoryBefore({ includeImages: bakeToMaster });

      if (fullBleed) {
        delete layer.crop;
      } else {
        layer.crop = nextCrop;
      }

      if (bakeToMaster) {
        applyPropsFromForm();
        const r = await API.post(
          `/api/assets/${encodeURIComponent(assetId)}/postprocess/bake-subject-crop`,
          previewBody(),
        );
        if (r?.stack) {
          stack = r.stack;
          const subj = subjectLayer();
          if (subj?.id) selectedId = subj.id;
        } else {
          delete layer.crop;
        }
        setStatus(t("pp.cropBakedToMaster"));
      } else {
        setStatus(fullBleed ? t("pp.cropFullImage") : t("pp.cropDone"));
      }

      fillProps();
      await fetchBounds();
      await refreshPreview({ skipInboxSync: true });
      exitCropMode();
      drawOverlay();
      scheduleStackPersist({ structural: true });
    } catch (err) {
      const msg = err?.message || String(err);
      setStatus(msg);
      cropLog(`裁切失败: ${msg}`, "系统");
    }
  });
}

function cancelCropMode() {
  cropLog("取消裁切", "操作");
  exitCropMode();
  setStatus(t("pp.cropCancelled"));
}

function onMatrixGridChanged() {
  matrixRemoved.clear();
  matrixSelectedLine = null;
  matrixDeleteHover = null;
}

function removeMatrixGridLine(type) {
  if (!matrixWork) return false;
  const normalized = normalizeGridLines(matrixHLines, matrixVLines, matrixWork.w, matrixWork.h);
  const lines = type === "h" ? normalized.hLines : normalized.vLines;
  if (!lines.length) return false;
  let index = lines.length - 1;
  if (matrixSelectedLine?.type === type && matrixSelectedLine.index < lines.length) {
    index = matrixSelectedLine.index;
  }
  return deleteMatrixGridLineAt(type, index);
}

function updateMatrixToolbarState() {
  /* 矩阵裁切删除改由线条末端 × 按钮完成 */
}

function matrixDeleteHoverChanged(a, b) {
  return a?.type !== b?.type || a?.index !== b?.index;
}

function deleteMatrixGridLineAt(type, index) {
  if (!matrixWork) return false;
  const normalized = normalizeGridLines(matrixHLines, matrixVLines, matrixWork.w, matrixWork.h);
  const lines = type === "h" ? normalized.hLines : normalized.vLines;
  if (index < 0 || index >= lines.length) return false;
  if (type === "h") {
    matrixHLines = removeGridLine(matrixHLines, index, matrixWork.h);
  } else {
    matrixVLines = removeGridLine(matrixVLines, index, matrixWork.w);
  }
  onMatrixGridChanged();
  drawCropCanvas();
  updateCropInfo();
  return true;
}

function bindCropCanvas() {
  const cropCanvas = $("#pp-crop-canvas");
  const cropWrap = cropCanvas?.parentElement;
  if (typeof ResizeObserver !== "undefined" && cropWrap) {
    new ResizeObserver(() => {
      if (cropMode) scheduleCropCanvasLayout();
    }).observe(cropWrap);
  }

  if (!cropCanvas) return;

  const onCropWheel = (e) => {
    if (!cropMode || cropLoading || cropSubModeLoading) return;
    e.preventDefault();
    cropZoomBy(e.deltaY < 0 ? 1.12 : 0.89, e);
  };
  cropWrap?.addEventListener("wheel", onCropWheel, { passive: false });
  cropCanvas.addEventListener("wheel", onCropWheel, { passive: false });

  cropWrap?.addEventListener("mousedown", (e) => {
    if (e.button !== 2) return;
    beginCanvasPan(e);
  });
  cropWrap?.addEventListener("contextmenu", preventCanvasContextMenu);
  cropCanvas.addEventListener("contextmenu", preventCanvasContextMenu);

  cropCanvas.addEventListener("pointerdown", (e) => {
    if (!cropMode || e.button !== 0 || cropLoading || cropSubModeLoading) return;
    e.preventDefault();
    const { cx, cy, rx, ry } = cropEventPos(e, { clampToRaw: true });
    let captureDrag = false;

    if (cropSubMode === "free") {
      if (!freeCropWork) return;
      freeCropDrag = { anchor: { x: rx, y: ry }, square: e.shiftKey };
      freeCropSelection = clampFreeSelection({ x: rx, y: ry, w: 1, h: 1 });
      captureDrag = true;
      drawCropCanvas();
      updateCropInfo();
    } else if (cropSubMode === "matrix") {
      if (!matrixWork) return;
      const scale = getCropCanvasScale();
      const canvas = cropCanvas;
      const { hLines, vLines } = normalizeGridLines(
        matrixHLines,
        matrixVLines,
        matrixWork.w,
        matrixWork.h,
      );
      const delHit = hitTestMatrixLineDelete(
        cx,
        cy,
        scale,
        hLines,
        vLines,
        canvas.width,
        canvas.height,
      );
      if (delHit) {
        deleteMatrixGridLineAt(delHit.type, delHit.index);
        return;
      }
      const lineHit = hitTestGridLine(cx, cy, rx, ry, scale, hLines, vLines);
      if (lineHit) {
        matrixSelectedLine = { type: lineHit.type, index: lineHit.index };
        matrixDrag = lineHit;
        captureDrag = true;
        drawCropCanvas();
      } else {
        matrixSelectedLine = null;
        const cell = cellAtPoint(rx, ry, matrixWork.w, matrixWork.h, matrixHLines, matrixVLines);
        if (!cell) return;
        const key = cellKey(cell.r, cell.c);
        if (matrixRemoved.has(key)) matrixRemoved.delete(key);
        else matrixRemoved.add(key);
        drawCropCanvas();
        updateCropInfo();
      }
    } else {
      const hit = hitTestCrop(cx, cy);
      if (hit.type === "close") {
        clearRectCropSelection();
        return;
      }
      if (hit.type === "create") {
        cropDrag = { mode: "create", anchor: { x: rx, y: ry }, square: e.shiftKey };
      } else if (hit.type === "move") {
        cropDrag = { mode: "move", start: { x: rx, y: ry }, orig: { ...cropPreview } };
      } else {
        cropDrag = {
          mode: "resize",
          handle: hit.handle,
          orig: { ...cropPreview },
          square: e.shiftKey,
        };
      }
      captureDrag = true;
      drawCropCanvas();
      updateCropInfo();
    }

    if (captureDrag) beginCropPointerCapture(cropCanvas, e);
  });

  cropCanvas.addEventListener("pointermove", (e) => {
    handleCropPointerMove(e);
  });

  cropCanvas.addEventListener("pointerup", (e) => {
    if (!cropMode || e.button !== 0) return;
    finishCropDrag(e);
  });

  cropCanvas.addEventListener("pointercancel", (e) => {
    finishCropDrag(e);
  });

  cropCanvas.addEventListener("lostpointercapture", () => {
    cropPointerCaptureId = null;
  });

  cropCanvas.addEventListener("mouseleave", () => {
    if (!cropDrag && !matrixDrag && !freeCropDrag) cropCanvas.style.cursor = "crosshair";
    if (matrixDeleteHover) {
      matrixDeleteHover = null;
      if (cropSubMode === "matrix") drawCropCanvas();
    }
  });

  window.addEventListener("pointerup", (e) => {
    if (!cropMode || e.button !== 0) return;
    if (!cropDrag && !matrixDrag && !freeCropDrag) return;
    finishCropDrag(e);
  });
}

function bindCropMatrixControls() {
  $("#pp-crop-tab-rect")?.addEventListener("click", () => switchCropSubMode("rect"));
  $("#pp-crop-tab-matrix")?.addEventListener("click", () => switchCropSubMode("matrix"));
  $("#pp-crop-tab-free")?.addEventListener("click", () => switchCropSubMode("free"));
  $("#pp-free-copy")?.addEventListener("click", () => freeCropCopySelection());
  $("#pp-free-cut")?.addEventListener("click", () => void freeCropCutSelection());
  $("#pp-free-paste")?.addEventListener("click", () => void freeCropPasteClipboard());
  $("#pp-free-clear")?.addEventListener("click", () => void freeCropDeleteTarget());
  $("#pp-matrix-add-h")?.addEventListener("click", () => {
    if (!matrixWork) return;
    matrixHLines = insertGridLine(matrixHLines, matrixWork.h);
    onMatrixGridChanged();
    drawCropCanvas();
    updateCropInfo();
  });
  $("#pp-matrix-add-v")?.addEventListener("click", () => {
    if (!matrixWork) return;
    matrixVLines = insertGridLine(matrixVLines, matrixWork.w);
    onMatrixGridChanged();
    drawCropCanvas();
    updateCropInfo();
  });
  $("#pp-matrix-auto-stitch")?.addEventListener("change", () => {
    drawCropCanvas();
    updateCropInfo();
  });
}

// ── 图层操作 ─────────────────────────────────────────────

async function addTextLayer() {
  await pushHistoryBefore({ includeImages: true });
  const id = "t_" + Math.random().toString(36).slice(2, 8);
  const textStyle = defaultTextStyle();
  ensureFontOption(textStyle.font_family);
  stack.layers.push({
    id,
    name: t("pp.layerText"),
    type: "text",
    visible: true,
    opacity: 1,
    blend_mode: "normal",
    blend_color: "",
    blend_amount: 1,
    blend_enabled: false,
    transform: defaultTransform(),
    text: textStyle,
  });
  selectLayer(id);
  markStackStructuralChange();
}

function addImageLayer() {
  pickImageFile().then(async (path) => {
    if (!path) return;
    await pushHistoryBefore({ includeImages: true });
    const id = "i_" + Math.random().toString(36).slice(2, 8);
    stack.layers.push({
      id,
      name: layerNameFromPath(path),
      type: "image",
      visible: true,
      opacity: 1,
      blend_mode: "normal",
      blend_color: "",
      blend_amount: 1,
      blend_enabled: false,
      source: path,
      transform: defaultTransform(),
    });
    selectLayer(id);
    markStackStructuralChange();
  });
}

async function moveLayer(delta) {
  const layer = selectedLayer();
  if (!layer) return;
  const idx = stack.layers.indexOf(layer);
  const ni = idx + delta;
  if (ni < 0 || ni >= stack.layers.length) return;
  await pushHistoryBefore({ includeImages: true });
  [stack.layers[idx], stack.layers[ni]] = [stack.layers[ni], stack.layers[idx]];
  renderLayers();
  schedulePreview();
  markStackStructuralChange();
}

async function duplicateLayer() {
  const layer = selectedLayer();
  if (!layer || layer.is_subject) return;
  await pushHistoryBefore({ includeImages: true });
  const clone = JSON.parse(JSON.stringify(layer));
  clone.id = (layer.type === "text" ? "t_" : "i_") + Math.random().toString(36).slice(2, 8);
  clone.name = `${layer.name}${t("pp.layerCopy")}`;
  clone.is_subject = false;
  stack.layers.push(clone);
  selectLayer(clone.id);
  markStackStructuralChange();
}

async function mergeSelectedLayers(btn) {
  if (isMatteSessionLocked()) return;
  const picked = layerCtxIds.length ? [...layerCtxIds] : effectiveSelectedLayerIds();
  if (picked.length < 2) {
    setStatus(t("pp.mergeLayersNeedTwo"));
    return;
  }
  const idSet = new Set(picked);
  const ids = (stack.layers || []).filter((l) => idSet.has(l.id)).map((l) => l.id);
  if (ids.length < 2) {
    setStatus(t("pp.mergeLayersNeedTwo"));
    return;
  }
  if (ids.some((id) => stack.layers.find((l) => l.id === id)?.locked)) {
    setStatus(t("pp.mergeLayersLocked"));
    return;
  }
  await withBtnBusy(btn, async () => {
    if (rotationPreviewState.active) await commitRotationPreview();
    applyPropsFromForm();
    await API.put(`/api/assets/${encodeURIComponent(assetId)}/postprocess`, postprocessSaveBody());
    const r = await API.post(
      `/api/assets/${encodeURIComponent(assetId)}/postprocess/merge-layers`,
      previewBody({ layer_ids: ids }),
    );
    if (r.stack) stack = r.stack;
    selectedId = r.new_layer_id || ids[0];
    selectedLayerIds = selectedId ? [selectedId] : [];
    soloId = null;
    if ($("#pp-solo")) $("#pp-solo").checked = false;
    renderLayers();
    fillProps();
    await fetchBounds();
    schedulePreview();
    markStackStructuralChange();
    const name = r.merged_name || "";
    setStatus(t("pp.mergeLayersDone", { name }));
    cropLog(t("pp.mergeLayersDone", { name }), "操作");
  }).catch((err) => {
    if (err) setStatus(err.message);
  });
}

function hideLayerCtxMenu() {
  layerCtxIds = [];
  closeFloatingMenu($("#pp-layer-ctx-menu"));
}

function updateLayerCtxMenu(ids) {
  const menu = $("#pp-layer-ctx-menu");
  if (!menu) return;
  const mergeBtn = menu.querySelector('[data-ctx="merge"]');
  const n = ids.length;
  if (!mergeBtn) return;
  mergeBtn.textContent = t("pp.mergeLayers", { n });
  const disabled = n < 2;
  mergeBtn.disabled = disabled;
  mergeBtn.classList.toggle("is-disabled", disabled);
  mergeBtn.setAttribute("aria-disabled", disabled ? "true" : "false");
}

function showLayerCtxMenu(x, y, layerId) {
  const menu = $("#pp-layer-ctx-menu");
  if (!menu) return;
  if (!isLayerRowSelected(layerId)) {
    selectedLayerIds = [layerId];
    selectedId = layerId;
    renderLayers();
  }
  layerCtxIds = effectiveSelectedLayerIds();
  updateLayerCtxMenu(layerCtxIds);
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

function bindLayerContextMenu() {
  const list = $("#pp-layer-list");
  const menu = $("#pp-layer-ctx-menu");
  if (!list || !menu) return;

  list.addEventListener("contextmenu", (e) => {
    const main = e.target.closest(".layer-main");
    if (!main) return;
    const layerId = main.dataset.id;
    if (!layerId) return;
    e.preventDefault();
    e.stopPropagation();
    showLayerCtxMenu(e.clientX, e.clientY, layerId);
  });

  menu.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-ctx]");
    if (!btn || btn.disabled || btn.classList.contains("is-disabled")) return;
    e.stopPropagation();
    const act = btn.dataset.ctx;
    const trigger = btn;
    hideLayerCtxMenu();
    if (act === "merge") await mergeSelectedLayers(trigger);
  });

  document.addEventListener("click", (e) => {
    if (!e.target.closest("#pp-layer-ctx-menu")) hideLayerCtxMenu();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hideLayerCtxMenu();
  });
}

function adjustTextFontSize(delta) {
  const layer = selectedLayer();
  if (!layer || layer.type !== "text" || layer.locked) return false;
  beginPropsHistoryBatch();
  layer.text = layer.text || {};
  layer.text.font_size = Math.min(256, Math.max(8, (layer.text.font_size || 24) + delta));
  fillProps();
  applyTransformLive({ previewMs: 16, bounds: true });
  return true;
}

function focusTextContent() {
  const layer = selectedLayer();
  if (layer?.type !== "text") return false;
  const ta = $("#pp-props-form")?.elements?.text_content;
  if (!ta) return false;
  ta.focus();
  ta.select?.();
  return true;
}

function isPlusKey(e) {
  return e.key === "+" || e.key === "=" || e.code === "NumpAdd" || e.code === "Equal";
}

function isMinusKey(e) {
  return e.key === "-" || e.key === "_" || e.code === "NumpSubtract" || e.code === "Minus";
}

async function deleteLayer(opts = {}) {
  const { silent = false } = opts;
  const layer = selectedLayer();
  if (!layer || layer.is_subject || layer.locked) return;
  if (!silent && !confirm(t("pp.confirmDeleteLayer", { name: layer.name }))) return;
  await pushHistoryBefore({ includeImages: true });
  stack.layers = stack.layers.filter((l) => l.id !== layer.id);
  selectedId = stack.layers[stack.layers.length - 1]?.id || subjectLayer()?.id;
  selectedLayerIds = selectedId ? [selectedId] : [];
  renderLayers();
  fillProps();
  schedulePreview();
  markStackStructuralChange();
}

async function saveStack() {
  if (!assetId || isMatteSessionLocked()) return;
  try {
    applyPropsFromForm();
    await API.put(`/api/assets/${assetId}/postprocess`, postprocessSaveBody());
    stackPersistDirty = false;
    clearTimeout(stackPersistTimer);
    persistSessionState();
    setStatus(t("pp.saved"));
  } catch (err) {
    setStatus(err.message);
  }
}

async function applyInbox(btn) {
  if (rotationPreviewState.active) await commitRotationPreview();
  await withBtnBusy(btn || $("#pp-apply"), async () => {
    if (cropMode && cropSubMode === "rect" && cropPreview) {
      const layer = selectedLayer();
      if (layer) layer.crop = clampCrop(cropPreview);
      syncCropToForm();
    }
    applyPropsFromForm();
    if (subjectMode === "inbox" || subjectMode === "source") {
      commitCanvasResizeFromInputs();
    }
    await API.put(`/api/assets/${assetId}/postprocess`, postprocessSaveBody());
    stackPersistDirty = false;
    clearTimeout(stackPersistTimer);
    persistSessionState();
    const exportMulti = $("#pp-export-multi-layers")?.checked;
    const tightCrop = $("#pp-export-tight")?.checked !== false;
    const r = await API.post(
      `/api/assets/${encodeURIComponent(assetId)}/postprocess/apply`,
      previewBody({
        export_multi_layers: exportMulti || undefined,
        tight_crop: tightCrop,
      }),
    );
    try {
      window.opener?.postMessage?.(
        {
          type: "postprocess-applied",
          assetId,
          width: r.width,
          height: r.height,
          size_label: r.size_label,
        },
        location.origin,
      );
    } catch {
      /* ignore */
    }
    try {
      sessionStorage.setItem(
        "artApp.ppApplied",
        JSON.stringify({
          type: "postprocess-applied",
          assetId,
          width: r.width,
          height: r.height,
          size_label: r.size_label,
        }),
      );
    } catch {
      /* ignore */
    }
    location.href = `/?asset=${encodeURIComponent(assetId)}`;
  }).catch((err) => {
    if (err) setStatus(err.message);
  });
}

async function restoreFromSource(btn) {
  if (!confirm(t("pp.confirmRestore"))) return;
  await withBtnBusy(btn || $("#pp-restore-source"), async () => {
    setStatus(t("pp.restoring"));
    const r = await API.post(`/api/assets/${encodeURIComponent(assetId)}/postprocess/restore-from-source`);
    stack = r.stack;
    const subj = subjectLayer();
    selectedId = subj?.id || stack.layers?.[0]?.id;
    soloId = null;
    if ($("#pp-solo")) $("#pp-solo").checked = false;
    exitCropMode?.();
    renderLayers();
    fillProps();
    schedulePreview();
    resetHistory();
    markStackStructuralChange();
    setStatus(t("pp.restored", { file: r.path?.split("/").pop() || "inbox" }));
  }).catch((err) => {
    if (err) setStatus(err.message);
  });
}

async function exportUnityAndReturn() {
  const btn = $("#pp-export-unity");
  await withBtnBusy(btn, async () => {
    applyPropsFromForm();
    setStatus(t("pp.exportSaving"));
    await API.put(`/api/assets/${assetId}/postprocess`, postprocessSaveBody());
    await API.post(`/api/assets/${encodeURIComponent(assetId)}/postprocess/apply`, {
      ...previewBody(),
      export_unity: true,
    });
    location.href = `/?asset=${encodeURIComponent(assetId)}`;
  }).catch((err) => {
    if (err) setStatus(t("pp.exportFailed", { msg: err.message }));
  });
}

function multiExportStorageKey() {
  return `artApp.ppMultiExport.${assetId}`;
}

function loadMultiExportPref() {
  try {
    return sessionStorage.getItem(multiExportStorageKey()) === "1";
  } catch {
    return false;
  }
}

function saveMultiExportPref(checked) {
  try {
    sessionStorage.setItem(multiExportStorageKey(), checked ? "1" : "0");
  } catch {
    /* ignore */
  }
}

async function smartSplitSubject(btn) {
  if (rotationPreviewState.active) await commitRotationPreview();
  await withBtnBusy(btn, async () => {
    if (isMatteSessionLocked()) throw new Error(t("pp.smartSplitBlockedMatte"));
    applyPropsFromForm();
    await API.put(`/api/assets/${assetId}/postprocess`, postprocessSaveBody());
    const r = await API.post(
      `/api/assets/${encodeURIComponent(assetId)}/postprocess/smart-split`,
      previewBody({ min_area: 32, alpha_threshold: 8 }),
    );
    if (r.stack) stack = r.stack;
    const firstNew = r.new_layer_ids?.[0];
    if (firstNew) selectedId = firstNew;
    soloId = null;
    if ($("#pp-solo")) $("#pp-solo").checked = false;
    renderLayers();
    fillProps();
    schedulePreview();
    markStackStructuralChange();
    setStatus(t("pp.smartSplitDone", { n: r.count || 0 }));
    matteLog(t("pp.smartSplitDone", { n: r.count || 0 }), "操作");
  }).catch((err) => {
    if (err) setStatus(err.message);
  });
}

function bindSubjectControls() {
  const multiChk = $("#pp-export-multi-layers");
  if (multiChk) {
    multiChk.checked = loadMultiExportPref();
    multiChk.addEventListener("change", () => {
      saveMultiExportPref(multiChk.checked);
      updatePostprocessActions();
    });
  }
  $("#pp-smart-split")?.addEventListener("click", (e) => smartSplitSubject(e.currentTarget));
}

async function resetTransform(full = false, partial = {}) {
  const l = selectedLayer();
  if (!l) return;
  await pushHistoryBefore({ includeImages: false });
  l.transform = l.transform || defaultTransform();
  if (full) {
    l.transform.offset_x = 0;
    l.transform.offset_y = 0;
    l.transform.scale = 1;
    l.transform.rotation_deg = 0;
    l.transform.flip_h = false;
    l.transform.flip_v = false;
    l.transform.pivot_x = 0.5;
    l.transform.pivot_y = 0.5;
    l.opacity = 1;
  } else if (partial.xy) {
    l.transform.offset_x = 0;
    l.transform.offset_y = 0;
  } else if (partial.x) {
    l.transform.offset_x = 0;
  } else if (partial.y) {
    l.transform.offset_y = 0;
  } else if (partial.scale) {
    l.transform.scale = 1;
  } else {
    l.transform.offset_x = 0;
    l.transform.offset_y = 0;
    l.transform.scale = 1;
    l.transform.rotation_deg = 0;
    l.transform.flip_h = false;
    l.transform.flip_v = false;
    l.transform.pivot_x = 0.5;
    l.transform.pivot_y = 0.5;
  }
  fillProps();
  applyTransformLive({ previewMs: 0, bounds: true });
}

async function clearLayerCrop() {
  const l = selectedLayer();
  if (!l?.crop) return;
  await pushHistoryBefore({ includeImages: false });
  delete l.crop;
  fillProps();
  applyTransformLive({ previewMs: 32, bounds: true });
}

async function autoTrimLayerAlpha(btn) {
  const layer = selectedLayer();
  if (!layer || layer.type !== "image") {
    setStatus(t("pp.cropNeedImage"));
    return;
  }
  if (layer.locked) return;
  if (cropMode) {
    setStatus(t("pp.autoCropExitCropMode"));
    return;
  }
  if (cropEntering || cropLoading) {
    cropLog("自适应裁切前清理残留裁切加载状态", "系统");
    exitCropMode();
  }
  await withBtnBusy(btn || $("#pp-auto-crop"), async () => {
    if (rotationPreviewState.active) await commitRotationPreview();
    await pushHistoryBefore({ includeImages: true });
    applyPropsFromForm();
    invalidateCropLayerCache(layer.id);
    await persistStackNow();
    setStatus(t("pp.autoCropApplying"));
    cropLog(`自适应裁切开始: ${layer.name || layer.id}`, "操作");
    const r = await API.post(
      `/api/assets/${encodeURIComponent(assetId)}/postprocess/trim-layer-alpha`,
      { ...previewBody(), layer_id: layer.id },
    );
    if (r.unchanged) {
      cropLog("自适应裁切: 无变化", "操作");
      setStatus(t("pp.autoCropUnchanged"));
      return;
    }
    if (r.stack) stack = r.stack;
    exitCropMode();
    invalidateCropLayerCache(layer.id);
    cropLog(
      `自适应裁切完成: ${r.width}×${r.height} canvas=${r.canvas?.canvas_width}×${r.canvas?.canvas_height}`,
      "操作",
    );
    fillProps();
    fillCanvasResizeInputs();
    updatePostprocessMeta();
    await fetchBounds();
    await refreshPreview({ skipInboxSync: true });
    markStackStructuralChange();
    if (r.canvas?.changed) {
      setStatus(
        t("pp.autoCropDoneWithCanvas", {
          w: r.width,
          h: r.height,
          ow: r.old_width,
          oh: r.old_height,
          cw: r.canvas.canvas_width,
          ch: r.canvas.canvas_height,
          ocw: r.canvas.old_canvas_width,
          och: r.canvas.old_canvas_height,
        }),
      );
      matteLog(
        t("pp.autoCropDoneWithCanvas", {
          w: r.width,
          h: r.height,
          ow: r.old_width,
          oh: r.old_height,
          cw: r.canvas.canvas_width,
          ch: r.canvas.canvas_height,
          ocw: r.canvas.old_canvas_width,
          och: r.canvas.old_canvas_height,
        }),
        "操作",
      );
    } else {
      setStatus(
        t("pp.autoCropDone", {
          w: r.width,
          h: r.height,
          ow: r.old_width,
          oh: r.old_height,
        }),
      );
      matteLog(
        t("pp.autoCropDone", { w: r.width, h: r.height, ow: r.old_width, oh: r.old_height }),
        "操作",
      );
    }
  }).catch((err) => {
    if (err) setStatus(err.message);
  });
}

// ── 指针事件 ─────────────────────────────────────────────

function bindViewport() {
  const vp = $("#pp-viewport");
  const wrap = $("#pp-viewport-wrap");

  if (typeof ResizeObserver !== "undefined") {
    const onResize = () => {
      if (!previewBlobUrl) return;
      layoutPreview();
      drawOverlay();
    };
    new ResizeObserver(onResize).observe(vp);
    if (wrap) new ResizeObserver(onResize).observe(wrap);
  }

  vp.addEventListener("wheel", (e) => {
    e.preventDefault();
    zoomBy(e.deltaY < 0 ? 1.1 : 0.9);
  }, { passive: false });

  vp.addEventListener("contextmenu", preventCanvasContextMenu);

  vp.addEventListener("mouseleave", () => {
    if (!matteMode) return;
    matteCursorDoc = null;
    if (matteCursorRaf) {
      cancelAnimationFrame(matteCursorRaf);
      matteCursorRaf = 0;
    }
    drawOverlay();
  });

  vp.addEventListener("mousemove", (e) => {
    if (!matteMode || cropMode) return;
    const doc = canvasToDoc(e.clientX, e.clientY);
    scheduleMatteCursorRedraw(doc.x, doc.y);
  });

  vp.addEventListener("mousedown", (e) => {
    if (e.button === 2) {
      beginCanvasPan(e);
      return;
    }
    if (cropMode) return;
    if (isMatteUiTarget(e.target)) return;

    const doc = canvasToDoc(e.clientX, e.clientY);

    if (isViewportMatteTarget(e) && e.button === 0) {
      e.preventDefault();
      const target = matteTargetLayer();
      if (!target || target.type !== "image" || target.locked) return;
      synthesizeMatteLayerBounds(target);
      matteStrokeActive = true;
      if (!beginMatteStroke(target, doc.x, doc.y)) {
        matteStrokeActive = false;
        setStatus(t("pp.matteMiss"));
        return;
      }
      drawOverlay();
      return;
    }

    const layer = hitTestLayer(doc.x, doc.y);

    if (isMatteSessionLocked()) return;

    if (layer) {
      selectedId = layer.id;
      renderLayers();
      fillProps();
      if (!layer.locked && layer.visible !== false) {
        pushHistoryBefore({ includeImages: false });
        drag = { id: layer.id, lastX: doc.x, lastY: doc.y };
      }
      drawOverlay();
    }
  });

  vp.addEventListener("dblclick", (e) => {
    if (cropMode) return;
    const doc = canvasToDoc(e.clientX, e.clientY);
    const layer = hitTestLayer(doc.x, doc.y);
    if (layer?.type === "text") {
      selectLayer(layer.id);
      focusTextContent();
      e.preventDefault();
    }
  });

  window.addEventListener("mousemove", (e) => {
    if (canvasPan) {
      moveCanvasPan(e);
      return;
    }
    if (cropMode) return;
    if (matteMode && matteStrokeActive && (e.buttons & 1)) {
      const doc = canvasToDoc(e.clientX, e.clientY);
      const layer = matteTargetLayer();
      if (layer?.type === "image" && !layer.locked) {
        extendMatteStroke(layer, doc.x, doc.y);
      }
      return;
    }
    if (!drag) return;
    const doc = canvasToDoc(e.clientX, e.clientY);
    const layer = stack.layers.find((l) => l.id === drag.id);
    if (!layer?.transform) return;
    const dx = doc.x - drag.lastX;
    const dy = doc.y - drag.lastY;
    if (dx === 0 && dy === 0) return;
    layer.transform.offset_x += dx;
    layer.transform.offset_y += dy;
    drag.lastX = doc.x;
    drag.lastY = doc.y;
    fillProps();
    patchBoundsOffset(layer.id, dx, dy);
    applyTransformLive({ previewMs: 16, bounds: false });
  });

  const finishPointerStroke = () => {
    endCanvasPan();
    if (matteStrokeActive) {
      endMatteStroke();
    }
    if (drag) {
      drag = null;
      if (!matteMode && !matteStrokeActive) refreshPreview();
    }
  };

  window.addEventListener("mouseup", finishPointerStroke);
  window.addEventListener("pointerup", finishPointerStroke);
  window.addEventListener("pointercancel", finishPointerStroke);

  bindCropCanvas();
  bindCropMatrixControls();

  window.addEventListener("resize", () => {
    layoutPreview();
    drawOverlay();
    if (cropMode) drawCropCanvas();
  });
}

function bindKeys() {
  document.addEventListener("keydown", (e) => {
    if (isCanvasSizeField(e.target)) return;
    const inField = e.target.matches("input,textarea,select");
    const mod = e.ctrlKey || e.metaKey;

    if (!inField && !mod && (e.key === "c" || e.key === "C")) {
      e.preventDefault();
      requestCropMode();
      return;
    }
    if (!inField && !mod && (e.key === "m" || e.key === "M")) {
      e.preventDefault();
      toggleMatteMode();
      return;
    }

    if (cropMode) {
      if (!inField && isPlusKey(e)) {
        e.preventDefault();
        cropZoomBy(e.shiftKey ? 1.35 : 1.18);
        return;
      }
      if (!inField && isMinusKey(e)) {
        e.preventDefault();
        cropZoomBy(e.shiftKey ? 0.74 : 0.85);
        return;
      }
      if (!inField && mod && e.key === "1") {
        e.preventDefault();
        zoom100();
        return;
      }
      if (!inField && mod && e.key === "0") {
        e.preventDefault();
        zoomFit();
        return;
      }
      if (isEscapeKey(e)) {
        cancelCropMode();
        e.preventDefault();
      } else if (e.key === "Enter") {
        void commitCrop($("#pp-crop-commit"));
        e.preventDefault();
      } else if (cropSubMode === "matrix") {
        if (e.key === "Delete" || e.key === "Backspace") {
          e.preventDefault();
          if (matrixSelectedLine) {
            removeMatrixGridLine(matrixSelectedLine.type);
          } else if (matrixRemoved.size) {
            matrixRemoved.clear();
            drawCropCanvas();
            updateCropInfo();
          }
        }
      } else if (cropSubMode === "free") {
        if (mod && (e.key === "c" || e.key === "C")) {
          e.preventDefault();
          freeCropCopySelection();
        } else if (mod && (e.key === "x" || e.key === "X")) {
          e.preventDefault();
          void freeCropCutSelection();
        } else if (mod && (e.key === "v" || e.key === "V")) {
          e.preventDefault();
          void freeCropPasteClipboard();
        } else if (e.key === "Delete" || e.key === "Backspace") {
          e.preventDefault();
          void freeCropDeleteTarget();
        }
      }
      return;
    }

    if (matteMode || matteStrokeActive) {
      if (mod && e.key === "z" && !e.shiftKey) {
        e.preventDefault();
        undoMatteSessionEdit();
        return;
      }
      if (mod && ((e.key === "z" && e.shiftKey) || e.key === "y" || e.key === "Y")) {
        e.preventDefault();
        redoMatteSessionEdit();
        return;
      }
      if (isEscapeKey(e)) {
        void exitMatteMode();
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      if (e.key === "Enter") {
        void exitMatteMode();
        e.preventDefault();
        return;
      }
      return;
    }

    if (mod && (e.key === "s" || e.key === "S")) {
      e.preventDefault();
      saveStack().catch((err) => setStatus(err.message));
      return;
    }

    if (mod && e.key === "z" && !e.shiftKey) {
      e.preventDefault();
      undoHistory();
      return;
    }
    if (mod && ((e.key === "z" && e.shiftKey) || e.key === "y" || e.key === "Y")) {
      e.preventDefault();
      redoHistory();
      return;
    }

    if (!inField && mod && isPlusKey(e)) {
      e.preventDefault();
      zoomBy(1.18);
      return;
    }
    if (!inField && mod && isMinusKey(e)) {
      e.preventDefault();
      zoomBy(0.85);
      return;
    }
    if (!inField && mod && e.key === "1") {
      e.preventDefault();
      zoomFit();
      return;
    }

    if (!inField && (e.key === "Delete" || e.key === "Backspace")) {
      const layer = selectedLayer();
      if (layer && !layer.is_subject && !layer.locked) {
        e.preventDefault();
        deleteLayer({ silent: true });
      }
      return;
    }

    if (!inField && isEscapeKey(e)) {
      e.preventDefault();
      soloId = null;
      const solo = $("#pp-solo");
      if (solo) solo.checked = false;
      const subj = subjectLayer();
      if (subj) selectLayer(subj.id);
      else {
        selectedId = null;
        renderLayers();
        fillProps();
        drawOverlay();
      }
      return;
    }

    if (!inField && mod && (e.key === "d" || e.key === "D")) {
      e.preventDefault();
      duplicateLayer();
      return;
    }

    if (!inField && isPlusKey(e)) {
      e.preventDefault();
      const layer = selectedLayer();
      if (layer?.type === "text" && !layer.locked) {
        adjustTextFontSize(e.shiftKey ? 10 : 2);
      } else {
        zoomBy(1.18);
      }
      return;
    }
    if (!inField && isMinusKey(e)) {
      e.preventDefault();
      const layer = selectedLayer();
      if (layer?.type === "text" && !layer.locked) {
        adjustTextFontSize(e.shiftKey ? -10 : -2);
      } else {
        zoomBy(0.85);
      }
      return;
    }

    if (!inField && e.key === "Enter") {
      const layer = selectedLayer();
      if (layer?.type === "text") {
        e.preventDefault();
        focusTextContent();
        return;
      }
    }

    if (inField) return;

    const l = selectedLayer();
    if (!l?.transform || l.locked) return;
    const step = e.shiftKey ? 10 : 1;
    if (e.key === "ArrowLeft") {
      beginPropsHistoryBatch();
      l.transform.offset_x -= step;
      fillProps();
      patchBoundsOffset(l.id, -step, 0);
      applyTransformLive({ previewMs: 16, bounds: false });
      e.preventDefault();
    } else if (e.key === "ArrowRight") {
      l.transform.offset_x += step;
      fillProps();
      patchBoundsOffset(l.id, step, 0);
      applyTransformLive({ previewMs: 16, bounds: false });
      e.preventDefault();
    } else if (e.key === "ArrowUp") {
      l.transform.offset_y -= step;
      fillProps();
      patchBoundsOffset(l.id, 0, -step);
      applyTransformLive({ previewMs: 16, bounds: false });
      e.preventDefault();
    } else if (e.key === "ArrowDown") {
      l.transform.offset_y += step;
      fillProps();
      patchBoundsOffset(l.id, 0, step);
      applyTransformLive({ previewMs: 16, bounds: false });
      e.preventDefault();
    } else if (e.key === "Home") {
      l.transform.offset_x = 0;
      l.transform.offset_y = 0;
      fillProps();
      applyTransformLive({ previewMs: 16, bounds: true });
      e.preventDefault();
    } else if (e.key === "0" && mod) {
      l.transform.scale = 1;
      fillProps();
      applyTransformLive({ previewMs: 16, bounds: true });
      e.preventDefault();
    } else if ((e.key === "[" || e.key === "【") && l.type === "text") {
      adjustTextFontSize(e.shiftKey ? -10 : -2);
      e.preventDefault();
    } else if ((e.key === "]" || e.key === "】") && l.type === "text") {
      adjustTextFontSize(e.shiftKey ? 10 : 2);
      e.preventDefault();
    }
  });
}

function updatePostprocessMeta() {
  if (!assetInfo) return;
  const inboxName = assetPaths?.inbox?.split("/").pop() || "inbox";
  const sourceName = assetPaths?.source?.split("/").pop() || "source";
  const unityName = assetPaths?.unity?.split("/").pop() || "engine";
  $("#pp-title").textContent = t("pp.titleAsset", { name: assetInfo.filename });
  const { w: canvasW, h: canvasH } = canvasSize();
  let metaKey = "pp.meta";
  let metaArgs = { w: canvasW, h: canvasH, inbox: inboxName, layers: stackLayerCount() };
  if (subjectMode === "source") {
    metaKey = "pp.metaSourceEdit";
    metaArgs = { w: canvasW, h: canvasH, source: sourceName, layers: stackLayerCount() };
  } else if (subjectMode === "unity") {
    metaKey = "pp.metaUnityEdit";
    metaArgs = { w: assetInfo.width, h: assetInfo.height, unity: unityName, layers: stackLayerCount() };
  }
  $("#pp-meta").textContent = t(metaKey, metaArgs);
  updateMasterEditWarning();
  updatePostprocessActions();
}

function updatePostprocessActions() {
  const applyBtn = $("#pp-apply");
  if (applyBtn) {
    const exportMulti = $("#pp-export-multi-layers")?.checked;
    let key = "pp.applyInbox";
    if (subjectMode === "source") key = "pp.applySource";
    else if (subjectMode === "unity") key = "pp.applyUnity";
    else if (exportMulti) key = "pp.applyMultiLayers";
    applyBtn.textContent = t(key);
    applyBtn.title =
      subjectMode === "source"
        ? t("pp.canvasSizeHint")
        : exportMulti
          ? t("pp.applyMultiLayersHint")
          : "";
  }
  const restoreBtn = $("#pp-restore-source");
  if (restoreBtn) {
    restoreBtn.hidden = subjectMode === "source" || subjectMode === "unity";
  }
}

function updateMasterEditWarning() {
  const el = $("#pp-master-warn");
  if (!el) return;
  if (subjectMode === "source") {
    el.textContent = t("pp.sourceEditWarning");
    el.hidden = false;
  } else if (subjectMode === "unity") {
    el.textContent = t("pp.unityEditWarning");
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

function consumePostprocessSkipCanvasSync(asset, subject) {
  try {
    const raw = sessionStorage.getItem("artApp.ppSkipCanvasSync");
    sessionStorage.removeItem("artApp.ppSkipCanvasSync");
    if (!raw) return false;
    const data = JSON.parse(raw);
    return data.assetId === asset && data.subject === subject;
  } catch {
    return false;
  }
}

async function bootstrap() {
  const subjectQ = encodeURIComponent(subjectMode || "inbox");
  const skipCanvasSync = consumePostprocessSkipCanvasSync(assetId, subjectMode || "inbox");
  const ppUrl = `/api/assets/${assetId}/postprocess?subject=${subjectQ}${
    skipCanvasSync ? "&skip_canvas_sync=1" : ""
  }`;
  const [assetInfoRes, assetPathsRes, pp, fontsRes] = await Promise.all([
    API.get(`/api/assets/${assetId}`),
    API.get(`/api/assets/${assetId}/paths`),
    API.get(ppUrl),
    API.get("/api/postprocess/fonts").catch(() => null),
  ]);
  assetInfo = assetInfoRes;
  assetPaths = assetPathsRes;
  updatePostprocessMeta();

  stack = pp.stack;
  syncBoundsCanvasFromStack();
  restoreSessionSelection();
  const subj = stack.layers?.find((l) => l.is_subject);
  if (!stack.layers?.some((l) => l.id === selectedId)) {
    selectedId = subj?.id || stack.layers?.[0]?.id;
  }
  if (selectedId && !selectedLayerIds.length) {
    selectedLayerIds = [selectedId];
  }
  await fetchBounds();

  if (fontsRes?.fonts) {
    for (const f of fontsRes.fonts) {
      const o = document.createElement("option");
      o.value = f;
      o.textContent = f;
      $("#pp-fonts").appendChild(o);
    }
  }

  renderLayers();
  fillProps();
  bindViewport();
  bindKeys();
  bindRangeSync();
  bindTransformControls();
  bindRotationPreviewControls();
  bindMatteControls();
  bindKeyControls();
  bindEscapeHandler();
  bindHistoryControls();
  bindCanvasResizeControls();
  bindSubjectControls();
  bindPostprocessPathFields();
  resetHistory();
  initToolbarIcons();
  updatePostprocessActions();
  updateCropModeUi();
  updateMatteModeUi();

  $("#pp-preview")?.addEventListener("error", () => {
    showPreviewEmpty(t("pp.noPreviewFile"));
    setStatus(t("pp.previewFailed", { msg: "load" }));
  });

  $("#pp-props-form").addEventListener("input", onPropsFormChange);
  $("#pp-props-form").addEventListener("change", onPropsFormChange);
  bindRipple(document.getElementById("pp-app"));
  bindCropControls();
  bindLayerContextMenu();

  $("#pp-export-unity").addEventListener("click", (e) => exportUnityAndReturn());
  $("#pp-apply").addEventListener("click", (e) => applyInbox(e.currentTarget));
  $("#pp-restore-source").addEventListener("click", (e) => restoreFromSource(e.currentTarget));
  $("#pp-add-text").addEventListener("click", addTextLayer);
  $("#pp-add-image").addEventListener("click", () => addImageLayer());
  $("#pp-offset-reset").addEventListener("click", () => resetTransform(false, { xy: true }));
  $("#pp-center").addEventListener("click", () => resetTransform(false, { xy: true }));
  $("#pp-x0").addEventListener("click", () => resetTransform(false, { x: true }));
  $("#pp-y0").addEventListener("click", () => resetTransform(false, { y: true }));
  $("#pp-scale100").addEventListener("click", () => resetTransform(false, { scale: true }));
  $("#pp-reset-xform").addEventListener("click", () => resetTransform(true));
  $("#pp-clear-crop").addEventListener("click", () => clearLayerCrop());
  $("#pp-auto-crop").addEventListener("click", (e) =>
    autoTrimLayerAlpha(e.currentTarget).catch((err) => {
      if (err) setStatus(err.message);
    }),
  );
  $("#pp-zoom-in").addEventListener("click", () => zoomBy(1.18));
  $("#pp-zoom-out").addEventListener("click", () => zoomBy(0.85));
  $("#pp-zoom-fit").addEventListener("click", zoomFit);
  $("#pp-solo").addEventListener("change", (e) => {
    soloId = e.target.checked ? selectedId : null;
    schedulePreview();
  });
  $("#pp-crop-commit").addEventListener("click", (e) => {
    e.preventDefault();
    void commitCrop(e.currentTarget);
  });
  $("#pp-crop-cancel").addEventListener("click", cancelCropMode);

  syncViewportToDocument();

  stackPersistEnabled = true;
  window.addEventListener("beforeunload", flushStackPersistSync);
  window.addEventListener("pagehide", flushStackPersistSync);
}

function mainReturnUrl() {
  if (assetId) return `/?asset=${encodeURIComponent(assetId)}`;
  try {
    return sessionStorage.getItem("artApp.ppReturn") || "/";
  } catch {
    return "/";
  }
}

function bindPostprocessBackLink() {
  const backBtn = document.querySelector(".pp-head-back");
  if (!backBtn) return;
  const href = mainReturnUrl();
  backBtn.setAttribute("href", href);
  backBtn.addEventListener("click", (e) => {
    e.preventDefault();
    const go = () => location.assign(href);
    if (rotationPreviewState.active) {
      void commitRotationPreview().finally(() => {
        void persistStackQuietly().finally(go);
      });
      return;
    }
    void persistStackQuietly().finally(go);
  });
}

async function start() {
  bindPostprocessBackLink();
  showGlobalOverlay(t("splash.reloading"));
  try {
    await initI18n();
    applyDomI18n();
    bindLangSwitcher();
    onLangChange(() => {
      applyDomI18n();
      updatePostprocessMeta();
      updatePostprocessActions();
      renderLayers();
      updateBlendModeDesc();
      updateCropModeUi();
      updateMatteModeUi();
    });
    initLogPanel();
    if (!assetId) {
      document.body.innerHTML = `<p class="hint">${t("pp.missingAsset")}</p>`;
      return;
    }
    await bootstrap();
    $("#pp-app")?.classList.add("is-ready");
    hideGlobalOverlay();
    document.body.classList.remove("is-booting");
    await refreshPreview({ skipInboxSync: true });
  } finally {
    hideGlobalOverlay();
    document.body.classList.remove("is-booting");
  }
}

start().catch((e) => setStatus(e.message));
