/**
 * 矩阵裁切：单图层、前端像素处理，确认后写回 layer-restore-image。
 */
import { rgbaToPngBlob } from "./matte-engine.js";

const MIN_CELL = 8;

export { rgbaToPngBlob };

export async function decodeBlobToRgba(blob) {
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
      img.onerror = reject;
      img.src = url;
    });
  }
  const canvas = document.createElement("canvas");
  canvas.width = bmp.width;
  canvas.height = bmp.height;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(bmp, 0, 0);
  if (bmp.close) bmp.close();
  const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
  return {
    data: new Uint8ClampedArray(imageData.data),
    w: canvas.width,
    h: canvas.height,
  };
}

/** 应用 layer.crop 后得到实际编辑缓冲 */
export function clampRect(x, y, w, h, maxW, maxH) {
  let nx = Math.round(x);
  let ny = Math.round(y);
  let nw = Math.max(1, Math.round(w));
  let nh = Math.max(1, Math.round(h));
  if (nx < 0) {
    nw += nx;
    nx = 0;
  }
  if (ny < 0) {
    nh += ny;
    ny = 0;
  }
  if (nx + nw > maxW) nw = maxW - nx;
  if (ny + nh > maxH) nh = maxH - ny;
  if (nw < 1) nw = 1;
  if (nh < 1) nh = 1;
  return { x: nx, y: ny, w: nw, h: nh };
}

export function copyRgbaRegion(data, imgW, x, y, rw, rh) {
  const out = new Uint8ClampedArray(rw * rh * 4);
  for (let row = 0; row < rh; row++) {
    const srcOff = ((y + row) * imgW + x) * 4;
    out.set(data.subarray(srcOff, srcOff + rw * 4), row * rw * 4);
  }
  return out;
}

export function clearRgbaRegion(data, imgW, x, y, rw, rh) {
  for (let row = 0; row < rh; row++) {
    for (let col = 0; col < rw; col++) {
      const i = ((y + row) * imgW + (x + col)) * 4;
      data[i] = 0;
      data[i + 1] = 0;
      data[i + 2] = 0;
      data[i + 3] = 0;
    }
  }
}

export function pasteRgbaRegion(data, imgW, imgH, patch, px, py) {
  const pw = patch.w;
  const ph = patch.h;
  for (let row = 0; row < ph; row++) {
    const dy = py + row;
    if (dy < 0 || dy >= imgH) continue;
    for (let col = 0; col < pw; col++) {
      const dx = px + col;
      if (dx < 0 || dx >= imgW) continue;
      const si = (row * pw + col) * 4;
      if (patch.data[si + 3] === 0) continue;
      const di = (dy * imgW + dx) * 4;
      data[di] = patch.data[si];
      data[di + 1] = patch.data[si + 1];
      data[di + 2] = patch.data[si + 2];
      data[di + 3] = patch.data[si + 3];
    }
  }
}

export function applyLayerCropToRgba(data, rawW, rawH, crop) {
  if (!crop?.w || !crop?.h) {
    return { data: new Uint8ClampedArray(data), w: rawW, h: rawH };
  }
  const x = Math.max(0, Math.min(crop.x, rawW - 1));
  const y = Math.max(0, Math.min(crop.y, rawH - 1));
  const w = Math.max(1, Math.min(crop.w, rawW - x));
  const h = Math.max(1, Math.min(crop.h, rawH - y));
  const out = new Uint8ClampedArray(w * h * 4);
  for (let row = 0; row < h; row++) {
    const srcOff = ((y + row) * rawW + x) * 4;
    const dstOff = row * w * 4;
    out.set(data.subarray(srcOff, srcOff + w * 4), dstOff);
  }
  return { data: out, w, h };
}

export function defaultGridLines(_w, _h) {
  return { hLines: [], vLines: [] };
}

function sortUniqueLines(lines, max) {
  const seen = new Set();
  const out = [];
  for (const v of [...lines].sort((a, b) => a - b)) {
    const n = Math.round(v);
    if (n <= 0 || n >= max || seen.has(n)) continue;
    seen.add(n);
    out.push(n);
  }
  return out;
}

export function normalizeGridLines(hLines, vLines, w, h) {
  return {
    hLines: sortUniqueLines(hLines, h),
    vLines: sortUniqueLines(vLines, w),
  };
}

export function gridSegments(size, innerLines) {
  const pts = [0, ...innerLines, size];
  const segs = [];
  for (let i = 0; i < pts.length - 1; i++) {
    segs.push({ start: pts[i], end: pts[i + 1], len: pts[i + 1] - pts[i] });
  }
  return segs;
}

export function cellKey(r, c) {
  return `${r},${c}`;
}

export function parseCellKey(key) {
  const [r, c] = key.split(",").map(Number);
  return { r, c };
}

function copyRect(src, sw, sx, sy, rw, rh, dst, dw, dx, dy) {
  for (let row = 0; row < rh; row++) {
    const srcOff = ((sy + row) * sw + sx) * 4;
    const dstOff = ((dy + row) * dw + dx) * 4;
    dst.set(src.subarray(srcOff, srcOff + rw * 4), dstOff);
  }
}

function clearRect(data, w, x, y, rw, rh) {
  for (let row = 0; row < rh; row++) {
    for (let col = 0; col < rw; col++) {
      const i = ((y + row) * w + (x + col)) * 4;
      data[i + 3] = 0;
    }
  }
}

/**
 * @param {Uint8ClampedArray} src
 * @param {Set<string>} removedCells keys "r,c"
 */
export function applyMatrixCrop(src, w, h, { hLines, vLines, removedCells, autoStitch }) {
  const { hLines: hL, vLines: vL } = normalizeGridLines(hLines, vLines, w, h);
  const rowSegs = gridSegments(h, hL);
  const colSegs = gridSegments(w, vL);
  const rows = rowSegs.length;
  const cols = colSegs.length;

  const isRemoved = (r, c) => removedCells.has(cellKey(r, c));

  if (!autoStitch) {
    const out = new Uint8ClampedArray(src);
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        if (!isRemoved(r, c)) continue;
        const rs = rowSegs[r];
        const cs = colSegs[c];
        clearRect(out, w, cs.start, rs.start, cs.len, rs.len);
      }
    }
    return { data: out, w, h };
  }

  const colDrop = Array.from({ length: cols }, (_, c) => {
    for (let r = 0; r < rows; r++) {
      if (!isRemoved(r, c)) return false;
    }
    return true;
  });
  const rowDrop = Array.from({ length: rows }, (_, r) => {
    for (let c = 0; c < cols; c++) {
      if (!isRemoved(r, c)) return false;
    }
    return true;
  });

  const keptCols = colSegs.filter((_, i) => !colDrop[i]);
  const keptRows = rowSegs.filter((_, i) => !rowDrop[i]);
  const outW = keptCols.reduce((s, seg) => s + seg.len, 0);
  const outH = keptRows.reduce((s, seg) => s + seg.len, 0);
  const out = new Uint8ClampedArray(outW * outH * 4);

  let dy = 0;
  for (let r = 0; r < rows; r++) {
    if (rowDrop[r]) continue;
    const rs = rowSegs[r];
    let dx = 0;
    for (let c = 0; c < cols; c++) {
      if (colDrop[c]) continue;
      const cs = colSegs[c];
      if (isRemoved(r, c)) {
        clearRect(out, outW, dx, dy, cs.len, rs.len);
      } else {
        copyRect(src, w, cs.start, rs.start, cs.len, rs.len, out, outW, dx, dy);
      }
      dx += cs.len;
    }
    dy += rs.len;
  }

  return { data: out, w: outW, h: outH };
}

export function previewMatrixSize(w, h, { hLines, vLines, removedCells, autoStitch }) {
  const result = applyMatrixCrop(
    new Uint8ClampedArray(w * h * 4),
    w,
    h,
    { hLines, vLines, removedCells, autoStitch },
  );
  return { w: result.w, h: result.h };
}

export function clampLineMove(lines, index, value, max, minCell = MIN_CELL) {
  const sorted = sortUniqueLines(lines, max);
  const prev = index === 0 ? 0 : sorted[index - 1];
  const next = index === sorted.length - 1 ? max : sorted[index + 1];
  const lo = prev + minCell;
  const hi = next - minCell;
  if (lo > hi) return sorted[index];
  return Math.max(lo, Math.min(hi, Math.round(value)));
}

export function insertGridLine(lines, max) {
  const sorted = sortUniqueLines(lines, max);
  const segs = gridSegments(max, sorted);
  let best = segs[0];
  for (const seg of segs) {
    if (seg.len > best.len) best = seg;
  }
  if (!best || best.len < MIN_CELL * 2) return sorted;
  const pos = Math.round(best.start + best.len / 2);
  return sortUniqueLines([...sorted, pos], max);
}

export function removeGridLine(lines, index, max) {
  const sorted = sortUniqueLines(lines, max);
  if (index < 0 || index >= sorted.length) return sorted;
  sorted.splice(index, 1);
  return sorted;
}

export function hitTestGridLine(cx, cy, rx, ry, scale, hLines, vLines) {
  const hit = 8 / Math.max(scale, 0.001);
  for (let i = 0; i < hLines.length; i++) {
    if (Math.abs(ry - hLines[i]) <= hit) return { type: "h", index: i };
  }
  for (let i = 0; i < vLines.length; i++) {
    if (Math.abs(rx - vLines[i]) <= hit) return { type: "v", index: i };
  }
  return null;
}

export const MATRIX_LINE_DELETE_BTN_R = 9;

/** 矩阵裁切：线条末端的删除按钮位置（画布像素坐标） */
export function matrixLineDeleteHandles(scale, hLines, vLines, canvasW, canvasH) {
  const margin = 6;
  const handles = [];
  for (let i = 0; i < hLines.length; i++) {
    handles.push({
      type: "h",
      index: i,
      cx: canvasW - margin - MATRIX_LINE_DELETE_BTN_R,
      cy: hLines[i] * scale,
    });
  }
  for (let i = 0; i < vLines.length; i++) {
    handles.push({
      type: "v",
      index: i,
      cx: vLines[i] * scale,
      cy: canvasH - margin - MATRIX_LINE_DELETE_BTN_R,
    });
  }
  return handles;
}

export function hitTestMatrixLineDelete(cx, cy, scale, hLines, vLines, canvasW, canvasH) {
  const r = MATRIX_LINE_DELETE_BTN_R + 3;
  for (const h of matrixLineDeleteHandles(scale, hLines, vLines, canvasW, canvasH)) {
    const dx = cx - h.cx;
    const dy = cy - h.cy;
    if (dx * dx + dy * dy <= r * r) return { type: h.type, index: h.index };
  }
  return null;
}

export function drawMatrixLineDeleteHandle(ctx, cx, cy, { hovered = false } = {}) {
  const r = MATRIX_LINE_DELETE_BTN_R;
  ctx.save();
  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.fillStyle = hovered ? "rgba(220, 38, 38, 0.92)" : "rgba(15, 17, 24, 0.86)";
  ctx.fill();
  ctx.strokeStyle = hovered ? "#fca5a5" : "rgba(248, 113, 113, 0.92)";
  ctx.lineWidth = 1.5;
  ctx.stroke();
  const arm = 4.2;
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

export function cellAtPoint(rx, ry, w, h, hLines, vLines) {
  const { hLines: hL, vLines: vL } = normalizeGridLines(hLines, vLines, w, h);
  const rowSegs = gridSegments(h, hL);
  const colSegs = gridSegments(w, vL);
  let r = -1;
  let c = -1;
  for (let i = 0; i < rowSegs.length; i++) {
    const seg = rowSegs[i];
    if (ry >= seg.start && ry < seg.end) {
      r = i;
      break;
    }
  }
  for (let i = 0; i < colSegs.length; i++) {
    const seg = colSegs[i];
    if (rx >= seg.start && rx < seg.end) {
      c = i;
      break;
    }
  }
  if (r < 0 || c < 0) return null;
  return { r, c };
}
