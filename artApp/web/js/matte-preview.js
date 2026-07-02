/**
 * 抠图会话：本地预览（降采样）+ 全分辨率缓冲 + 纯前端写回。
 */
import {
  borderMatteRgba,
  globalSameColorMatteRgba,
  polishMatteRgba,
  rgbaToPngBlob,
  strokeMatteRgba,
} from "./matte-engine.js";
import { chromaKeyRgba } from "./chroma-key.js";

const WORK_MAX = 480;

export const mattePreviewState = {
  layerId: null,
  rawW: 0,
  rawH: 0,
  workW: 0,
  workH: 0,
  workScale: 1,
  canvas: null,
  ctx: null,
  imageData: null,
  ready: false,
  loading: false,
};

export const matteFullState = {
  layerId: null,
  rawW: 0,
  rawH: 0,
  data: null,
  loading: false,
};

const matteUndoPast = [];
const matteUndoFuture = [];

function captureMatteSnapshot() {
  const full = matteFullState;
  if (!full.data) return null;
  return {
    rawW: full.rawW,
    rawH: full.rawH,
    full: new Uint8ClampedArray(full.data),
  };
}

function restoreMatteSnapshot(snap) {
  if (!snap?.full) return false;
  matteFullState.rawW = snap.rawW;
  matteFullState.rawH = snap.rawH;
  matteFullState.data = new Uint8ClampedArray(snap.full);
  return rebuildPreviewFromFullData(matteFullState.data, snap.rawW, snap.rawH);
}

export function clearMatteUndoStacks() {
  matteUndoPast.length = 0;
  matteUndoFuture.length = 0;
}

export function canUndoMatteEdit() {
  return matteUndoPast.length > 0;
}

export function canRedoMatteEdit() {
  return matteUndoFuture.length > 0;
}

/** 笔画开始前压入快照，供抠图会话内撤销 */
export function pushMatteUndoSnapshot() {
  const snap = captureMatteSnapshot();
  if (!snap) return;
  matteUndoPast.push(snap);
  if (matteUndoPast.length > 40) matteUndoPast.shift();
  matteUndoFuture.length = 0;
}

export function discardLastMatteUndoSnapshot() {
  if (matteUndoPast.length) matteUndoPast.pop();
}

export function undoMatteEdit() {
  if (!matteUndoPast.length) return false;
  const current = captureMatteSnapshot();
  if (current) matteUndoFuture.push(current);
  const snap = matteUndoPast.pop();
  return restoreMatteSnapshot(snap);
}

export function redoMatteEdit() {
  if (!matteUndoFuture.length) return false;
  const current = captureMatteSnapshot();
  if (current) matteUndoPast.push(current);
  const snap = matteUndoFuture.pop();
  return restoreMatteSnapshot(snap);
}

function pixelSat(r, g, b) {
  return Math.max(r, g, b) - Math.min(r, g, b);
}

function colorDist(r, g, b, br, bg, bb) {
  return Math.hypot(r - br, g - bg, b - bb);
}

function isBgLike(r, g, b, a, br, bg, bb, colorTol, satLimit) {
  if (a === 0) return false;
  return colorDist(r, g, b, br, bg, bb) <= colorTol && pixelSat(r, g, b) <= satLimit;
}

function floodEraseAt(imageData, w, h, sx, sy, colorTol) {
  const data = imageData.data;
  const si = (sy * w + sx) * 4;
  if (data[si + 3] === 0) return false;
  const br = data[si];
  const bg = data[si + 1];
  const bb = data[si + 2];
  const satLimit = Math.max(28, pixelSat(br, bg, bb) + 20);
  const visited = new Uint8Array(w * h);
  const stack = [sx, sy];
  let changed = false;

  while (stack.length) {
    const y = stack.pop();
    const x = stack.pop();
    const pi = y * w + x;
    if (visited[pi]) continue;
    visited[pi] = 1;
    const i = pi * 4;
    if (data[i + 3] === 0) continue;
    const r = data[i];
    const g = data[i + 1];
    const b = data[i + 2];
    if (!isBgLike(r, g, b, data[i + 3], br, bg, bb, colorTol, satLimit)) continue;
    data[i + 3] = 0;
    changed = true;
    for (let dy = -1; dy <= 1; dy++) {
      for (let dx = -1; dx <= 1; dx++) {
        if (!dx && !dy) continue;
        const nx = x + dx;
        const ny = y + dy;
        if (nx < 0 || nx >= w || ny < 0 || ny >= h) continue;
        stack.push(nx, ny);
      }
    }
  }
  return changed;
}

const _brushCache = new Map();

function brushDiskOffsets(radius) {
  const r = Math.max(0, Math.floor(radius));
  if (_brushCache.has(r)) return _brushCache.get(r);
  const offsets = r === 0 ? [[0, 0]] : [];
  if (r > 0) {
    const r2 = r * r;
    for (let dy = -r; dy <= r; dy++) {
      for (let dx = -r; dx <= r; dx++) {
        if (dx * dx + dy * dy <= r2) offsets.push([dx, dy]);
      }
    }
  }
  _brushCache.set(r, offsets);
  return offsets;
}

function rawToWork(rawX, rawY) {
  const scale = mattePreviewState.workScale;
  return [
    Math.max(0, Math.min(mattePreviewState.workW - 1, Math.round(rawX * scale))),
    Math.max(0, Math.min(mattePreviewState.workH - 1, Math.round(rawY * scale))),
  ];
}

export function rebuildPreviewFromFullData(fullData, rawW, rawH) {
  const scale = Math.min(1, WORK_MAX / Math.max(rawW, rawH, 1));
  const workW = Math.max(1, Math.round(rawW * scale));
  const workH = Math.max(1, Math.round(rawH * scale));

  const canvas = document.createElement("canvas");
  canvas.width = workW;
  canvas.height = workH;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  if (!ctx) return false;

  const src = document.createElement("canvas");
  src.width = rawW;
  src.height = rawH;
  src.getContext("2d").putImageData(new ImageData(fullData, rawW, rawH), 0, 0);
  ctx.drawImage(src, 0, 0, workW, workH);

  mattePreviewState.rawW = rawW;
  mattePreviewState.rawH = rawH;
  mattePreviewState.workW = workW;
  mattePreviewState.workH = workH;
  mattePreviewState.workScale = workW / rawW;
  mattePreviewState.canvas = canvas;
  mattePreviewState.ctx = ctx;
  mattePreviewState.imageData = ctx.getImageData(0, 0, workW, workH);
  mattePreviewState.ready = true;
  return true;
}

export function applyMattePointsToPreview(
  rawPoints,
  { color_tol: colorTol, brush_size: brushSize, global_same_color: globalSame },
) {
  const st = mattePreviewState;
  const full = matteFullState;
  if (!st.ready || !rawPoints?.length) return false;

  if (globalSame && full.data && full.layerId === st.layerId) {
    const [sx, sy] = rawPoints[0];
    if (!globalSameColorMatteRgba(full.data, full.rawW, full.rawH, sx, sy, { color_tol: colorTol })) {
      return false;
    }
    return rebuildPreviewFromFullData(full.data, full.rawW, full.rawH);
  }

  if (!st.imageData) return false;
  const { workW: w, workH: h, imageData } = st;
  const scale = st.workScale || 1;
  const workBrush = Math.max(1, brushSize * scale);
  const radius = Math.max(0, (workBrush - 1) / 2);
  const offsets = brushDiskOffsets(Math.round(radius));
  const seen = new Set();
  let changed = false;

  for (const pt of rawPoints) {
    const rawX = Math.round(pt[0]);
    const rawY = Math.round(pt[1]);
    for (const [dx, dy] of offsets) {
      const [wx, wy] = rawToWork(rawX + dx, rawY + dy);
      const key = wx + wy * w;
      if (seen.has(key)) continue;
      seen.add(key);
      if (floodEraseAt(imageData, w, h, wx, wy, colorTol)) changed = true;
    }
  }

  if (changed && st.ctx) {
    st.ctx.putImageData(imageData, 0, 0);
  }
  return changed;
}

/**
 * 将笔画同步到全分辨率缓冲（与预览 flood 算法一致），再重建预览。
 * 避免 syncMatteFullFromPreview 把降采样预览放大导致发糊。
 */
export function applyMatteStrokeToFullData(
  rawPoints,
  { color_tol: colorTol, brush_size: brushSize, global_same_color: globalSame },
) {
  const full = matteFullState;
  const pv = mattePreviewState;
  if (!full.data || !rawPoints?.length) return false;

  if (globalSame) {
    const [sx, sy] = rawPoints[0];
    if (!globalSameColorMatteRgba(full.data, full.rawW, full.rawH, sx, sy, { color_tol: colorTol })) {
      return false;
    }
    if (pv.layerId === full.layerId) {
      rebuildPreviewFromFullData(full.data, full.rawW, full.rawH);
    }
    return true;
  }

  const w = full.rawW;
  const h = full.rawH;
  const brushRadius = Math.max(0, Math.round((brushSize - 1) / 2));
  const offsets = brushDiskOffsets(brushRadius);
  const seen = new Set();
  let changed = false;
  const imageData = { data: full.data, width: w, height: h };

  for (const pt of rawPoints) {
    const rawX = Math.round(pt[0]);
    const rawY = Math.round(pt[1]);
    for (const [dx, dy] of offsets) {
      const fx = Math.max(0, Math.min(w - 1, rawX + dx));
      const fy = Math.max(0, Math.min(h - 1, rawY + dy));
      const key = fx + fy * w;
      if (seen.has(key)) continue;
      seen.add(key);
      if (floodEraseAt(imageData, w, h, fx, fy, colorTol)) changed = true;
    }
  }

  if (changed && pv.layerId === full.layerId && pv.ready) {
    rebuildPreviewFromFullData(full.data, full.rawW, full.rawH);
  }
  return changed;
}

async function decodeBlobToRgba(blob) {
  let bmp;
  if (typeof createImageBitmap === "function") {
    bmp = await createImageBitmap(blob);
  } else {
    bmp = await new Promise((resolve, reject) => {
      const img = new Image();
      const url = URL.createObjectURL(blob);
      img.onload = () => {
        URL.revokeObjectURL(url);
        resolve(img);
      };
      img.onerror = () => {
        URL.revokeObjectURL(url);
        reject(new Error("decode"));
      };
      img.src = url;
    });
  }
  const rawW = bmp.width;
  const rawH = bmp.height;
  const canvas = document.createElement("canvas");
  canvas.width = rawW;
  canvas.height = rawH;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  if (!ctx) {
    if (bmp.close) bmp.close();
    throw new Error("canvas");
  }
  ctx.drawImage(bmp, 0, 0);
  if (bmp.close) bmp.close();
  const imageData = ctx.getImageData(0, 0, rawW, rawH);
  return { data: new Uint8ClampedArray(imageData.data), rawW, rawH };
}

export async function loadMattePreview(layerId, fetchBlob) {
  if (mattePreviewState.loading && mattePreviewState.layerId === layerId) {
    return mattePreviewState.ready;
  }
  if (mattePreviewState.ready && mattePreviewState.layerId === layerId) {
    return true;
  }

  mattePreviewState.loading = true;
  mattePreviewState.ready = false;
  mattePreviewState.layerId = layerId;

  try {
    const blob = await fetchBlob(layerId);
    if (!blob) return false;

    const { data, rawW, rawH } = await decodeBlobToRgba(blob);
    matteFullState.layerId = layerId;
    matteFullState.rawW = rawW;
    matteFullState.rawH = rawH;
    matteFullState.data = data;

    return rebuildPreviewFromFullData(data, rawW, rawH);
  } catch {
    clearMattePreview();
    return false;
  } finally {
    mattePreviewState.loading = false;
  }
}

export async function ensureMatteFull(layerId, fetchBlob) {
  if (matteFullState.layerId === layerId && matteFullState.data) {
    return true;
  }
  if (matteFullState.loading) {
    while (matteFullState.loading) {
      await new Promise((r) => setTimeout(r, 16));
    }
    return matteFullState.layerId === layerId && !!matteFullState.data;
  }

  matteFullState.loading = true;
  try {
    if (mattePreviewState.layerId === layerId && matteFullState.data) {
      return true;
    }
    const blob = await fetchBlob(layerId);
    if (!blob) return false;
    const { data, rawW, rawH } = await decodeBlobToRgba(blob);
    matteFullState.layerId = layerId;
    matteFullState.rawW = rawW;
    matteFullState.rawH = rawH;
    matteFullState.data = data;
    if (mattePreviewState.layerId === layerId) {
      rebuildPreviewFromFullData(data, rawW, rawH);
    }
    return true;
  } catch {
    return false;
  } finally {
    matteFullState.loading = false;
  }
}

export function applyMattePointsToFull(
  rawPoints,
  { color_tol: colorTol, brush_size: brushSize, global_same_color: globalSame },
) {
  const st = matteFullState;
  if (!st.data || !rawPoints?.length) return false;
  if (globalSame) {
    const [sx, sy] = rawPoints[0];
    return globalSameColorMatteRgba(st.data, st.rawW, st.rawH, sx, sy, { color_tol: colorTol });
  }
  return strokeMatteRgba(st.data, st.rawW, st.rawH, rawPoints, {
    color_tol: colorTol,
    brush_size: brushSize,
    cleanup: false,
    max_dim: 512,
  });
}

/** 将当前预览 canvas 同步到全分辨率缓冲（退出写回前保证所见即所得） */
export function syncMatteFullFromPreview() {
  const pv = mattePreviewState;
  const full = matteFullState;
  if (!pv.ready || !pv.canvas || !full.data || pv.layerId !== full.layerId) return false;

  const src = document.createElement("canvas");
  src.width = full.rawW;
  src.height = full.rawH;
  const sctx = src.getContext("2d", { willReadFrequently: true });
  if (!sctx) return false;
  sctx.drawImage(pv.canvas, 0, 0, pv.workW, pv.workH, 0, 0, full.rawW, full.rawH);
  const imgData = sctx.getImageData(0, 0, full.rawW, full.rawH);
  full.data.set(imgData.data);
  return true;
}
/** 退出抠图模式前：全分辨率收边（不写回预览 canvas，避免覆盖拖动中的效果） */
export function finalizeMatteSession(settings = {}) {
  const st = matteFullState;
  if (!st.data) return false;
  return polishMatteRgba(st.data, st.rawW, st.rawH, {
    color_tol: settings.color_tol ?? 34,
    feather: 1,
  });
}

export function commitBorderMatte(settings) {
  const st = matteFullState;
  if (!st.data) return false;
  const ok = borderMatteRgba(st.data, st.rawW, st.rawH, {
    ...settings,
    feather: 1,
  });
  if (ok && mattePreviewState.layerId === st.layerId) {
    rebuildPreviewFromFullData(st.data, st.rawW, st.rawH);
  }
  return ok;
}

export function commitChromaKey(settings) {
  const st = matteFullState;
  if (!st.data) return false;
  const ok = chromaKeyRgba(st.data, st.rawW, st.rawH, {
    key_hex: settings.key_hex,
    fuzz: settings.fuzz,
    feather: settings.feather,
  });
  if (ok && mattePreviewState.layerId === st.layerId) {
    rebuildPreviewFromFullData(st.data, st.rawW, st.rawH);
  }
  return ok;
}

export async function matteFullToBlob() {
  const st = matteFullState;
  if (!st.data) return null;
  return rgbaToPngBlob(st.data, st.rawW, st.rawH);
}

export function invalidateMatteFull() {
  matteFullState.layerId = null;
  matteFullState.rawW = 0;
  matteFullState.rawH = 0;
  matteFullState.data = null;
}

export function clearMattePreview() {
  clearMatteUndoStacks();
  mattePreviewState.layerId = null;
  mattePreviewState.rawW = 0;
  mattePreviewState.rawH = 0;
  mattePreviewState.workW = 0;
  mattePreviewState.workH = 0;
  mattePreviewState.workScale = 1;
  mattePreviewState.canvas = null;
  mattePreviewState.ctx = null;
  mattePreviewState.imageData = null;
  mattePreviewState.ready = false;
  mattePreviewState.loading = false;
  invalidateMatteFull();
}

export function drawMattePreview(ctx, ox, oy, z, bounds, layer) {
  const st = mattePreviewState;
  if (!st.ready || !st.canvas || !bounds) return;

  const x0 = ox + bounds.x * z;
  const y0 = oy + bounds.y * z;
  const sw = bounds.w * z;
  const sh = bounds.h * z;

  const crop = layer?.crop;
  let srcX = 0;
  let srcY = 0;
  let srcW = st.workW;
  let srcH = st.workH;
  if (crop?.w > 0 && crop?.h > 0) {
    srcX = Math.round(crop.x * st.workScale);
    srcY = Math.round(crop.y * st.workScale);
    srcW = Math.max(1, Math.round(crop.w * st.workScale));
    srcH = Math.max(1, Math.round(crop.h * st.workScale));
  }

  ctx.save();
  ctx.globalCompositeOperation = "source-over";
  ctx.globalAlpha = 1;
  if (bounds.corners?.length === 4) {
    const [c0, c1, c2, c3] = bounds.corners;
    ctx.beginPath();
    ctx.moveTo(ox + c0[0] * z, oy + c0[1] * z);
    ctx.lineTo(ox + c1[0] * z, oy + c1[1] * z);
    ctx.lineTo(ox + c2[0] * z, oy + c2[1] * z);
    ctx.lineTo(ox + c3[0] * z, oy + c3[1] * z);
    ctx.closePath();
    ctx.clip();
  }
  ctx.drawImage(st.canvas, srcX, srcY, srcW, srcH, x0, y0, sw, sh);
  ctx.restore();
}
