/**
 * 抠图算法（浏览器端，对标 tools/alpha_matte.py）。
 * 操作 RGBA 交错缓冲（Uint8ClampedArray，长度 w*h*4）。
 */

function pixelSat(r, g, b) {
  return Math.max(r, g, b) - Math.min(r, g, b);
}

function colorDist(r, g, b, br, bg, bb) {
  return Math.hypot(r - br, g - bg, b - bb);
}

function isBackgroundLike(r, g, b, br, bg, bb, colorTol, bgSat) {
  if (colorDist(r, g, b, br, bg, bb) > colorTol) return false;
  if (pixelSat(r, g, b) > Math.max(28, bgSat + 20)) return false;
  return true;
}

/** 全图同色剔除：色键底（品红等）用分通道容差，镂空碎色更稳 */
function matchesGlobalKeyColor(r, g, b, br, bg, bb, colorTol) {
  const tol = colorTol + 6;
  if (colorDist(r, g, b, br, bg, bb) <= tol) return true;
  if (pixelSat(br, bg, bb) < 72) return false;
  const chTol = tol + 6;
  return (
    Math.abs(r - br) <= chTol &&
    Math.abs(g - bg) <= chTol &&
    Math.abs(b - bb) <= chTol
  );
}

function globalKeyColorMask(data, w, h, br, bg, bb, colorTol, bgSat) {
  const satLimit = Math.max(48, bgSat + 32);
  const mask = new Uint8Array(w * h);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      if (data[i + 3] === 0) continue;
      const r = data[i];
      const g = data[i + 1];
      const b = data[i + 2];
      if (!matchesGlobalKeyColor(r, g, b, br, bg, bb, colorTol)) continue;
      if (pixelSat(r, g, b) > satLimit) continue;
      mask[y * w + x] = 1;
    }
  }
  return mask;
}

function medianRgb(samples) {
  if (!samples.length) return [127, 127, 127];
  const rs = samples.map((s) => s[0]).sort((a, b) => a - b);
  const gs = samples.map((s) => s[1]).sort((a, b) => a - b);
  const bs = samples.map((s) => s[2]).sort((a, b) => a - b);
  const m = (arr) => arr[(arr.length / 2) | 0];
  return [m(rs), m(gs), m(bs)];
}

function estimateBorderBgColor(data, w, h, borderBand = 4) {
  const band = Math.max(1, Math.min(borderBand, (h / 4) | 0, (w / 4) | 0));
  const samples = [];
  for (let x = 0; x < w; x++) {
    for (let y = 0; y < band; y++) {
      const i = (y * w + x) * 4;
      samples.push([data[i], data[i + 1], data[i + 2]]);
    }
    for (let y = h - band; y < h; y++) {
      const i = (y * w + x) * 4;
      samples.push([data[i], data[i + 1], data[i + 2]]);
    }
  }
  for (let y = band; y < h - band; y++) {
    for (let x = 0; x < band; x++) {
      const i = (y * w + x) * 4;
      samples.push([data[i], data[i + 1], data[i + 2]]);
    }
    for (let x = w - band; x < w; x++) {
      const i = (y * w + x) * 4;
      samples.push([data[i], data[i + 1], data[i + 2]]);
    }
  }
  return medianRgb(samples);
}

function bgLikeMask(data, w, h, br, bg, bb, colorTol, bgSat) {
  const satLimit = Math.max(28, bgSat + 20);
  const mask = new Uint8Array(w * h);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      const r = data[i];
      const g = data[i + 1];
      const b = data[i + 2];
      if (colorDist(r, g, b, br, bg, bb) <= colorTol && pixelSat(r, g, b) <= satLimit) {
        mask[y * w + x] = 1;
      }
    }
  }
  return mask;
}

function morphErode4(mask, w, h) {
  const out = new Uint8Array(w * h);
  for (let y = 1; y < h - 1; y++) {
    for (let x = 1; x < w - 1; x++) {
      const i = y * w + x;
      if (
        mask[i] &&
        mask[i - 1] &&
        mask[i + 1] &&
        mask[i - w] &&
        mask[i + w]
      ) {
        out[i] = 1;
      }
    }
  }
  return out;
}

function morphDilate4(mask, w, h) {
  const out = new Uint8Array(w * h);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      if (!mask[y * w + x]) continue;
      for (const [dx, dy] of [[0, 0], [-1, 0], [1, 0], [0, -1], [0, 1]]) {
        const nx = x + dx;
        const ny = y + dy;
        if (nx >= 0 && nx < w && ny >= 0 && ny < h) out[ny * w + nx] = 1;
      }
    }
  }
  return out;
}

function morphOpen4(mask, w, h, iters = 1) {
  let out = mask;
  for (let n = 0; n < iters; n++) {
    out = morphDilate4(morphErode4(out, w, h), w, h);
  }
  return out;
}

function neighborTransparentCount(alpha, outside, w, h) {
  const counts = new Uint8Array(w * h);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = y * w + x;
      const trans = outside[i] || alpha[i] === 0;
      if (!trans) continue;
      if (x > 0) counts[i - 1]++;
      if (x < w - 1) counts[i + 1]++;
      if (y > 0) counts[i - w]++;
      if (y < h - 1) counts[i + w]++;
    }
  }
  return counts;
}

function expandOutsideBgLike(data, alpha, outside, w, h, br, bg, bb, colorTol, bgSat, maxIter = 4) {
  const out = new Uint8Array(outside);
  const bgLike = bgLikeMask(data, w, h, br, bg, bb, colorTol, bgSat);
  for (let iter = 0; iter < maxIter; iter++) {
    let peeled = false;
    const neighborTrans = neighborTransparentCount(alpha, out, w, h);
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const pi = y * w + x;
        if (out[pi] || alpha[pi] === 0 || !bgLike[pi] || neighborTrans[pi] < 1) continue;
        out[pi] = 1;
        alpha[pi] = 0;
        peeled = true;
      }
    }
    if (!peeled) break;
  }
  return out;
}

function minIslandPixels(h, w) {
  return Math.max(18, Math.floor(h * w * 0.00006));
}

function removeSmallIslands(alpha, w, h, minPixels, alphaThresh = 10) {
  const fg = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i++) {
    if (alpha[i] > alphaThresh) fg[i] = 1;
  }
  if (!fg.some(Boolean)) return alpha;
  const radius = Math.max(1, Math.min(4, Math.round(Math.sqrt(Math.max(1, minPixels)) / 2)));
  let eroded = fg;
  for (let n = 0; n < radius; n++) eroded = morphErode4(eroded, w, h);
  let opened = eroded;
  for (let n = 0; n < radius; n++) opened = morphDilate4(opened, w, h);
  for (let i = 0; i < w * h; i++) {
    if (fg[i] && !opened[i]) alpha[i] = 0;
  }
  return alpha;
}

function defringeAlpha(alpha, data, w, h, br, bg, bb, colorTol, bgSat, passes = 2) {
  const satLimit = Math.max(28, bgSat + 20);
  const a = alpha;
  for (let pass = 0; pass < passes; pass++) {
    const solid = new Uint8Array(w * h);
    for (let i = 0; i < w * h; i++) {
      if (a[i] > 8) solid[i] = 1;
    }
    const outside = new Uint8Array(w * h);
    for (let i = 0; i < w * h; i++) {
      if (!solid[i]) outside[i] = 1;
    }
    const neighborTrans = neighborTransparentCount(a, outside, w, h);
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const pi = y * w + x;
        if (a[pi] <= 8 || neighborTrans[pi] === 0) continue;
        const i = pi * 4;
        const r = data[i];
        const g = data[i + 1];
        const b = data[i + 2];
        const diff = colorDist(r, g, b, br, bg, bb);
        const sat = pixelSat(r, g, b);
        if (sat > satLimit) continue;
        if (diff <= colorTol * 0.9) a[pi] = 0;
        else if (diff <= colorTol * 1.15) a[pi] = Math.min(a[pi], 48);
      }
    }
  }
  return a;
}

function featherRim(alpha, w, h, radius = 1, strength = 0.5) {
  if (radius <= 0) return alpha;
  const a = alpha;
  const floor = 255 * (1 - strength);
  let solid = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i++) solid[i] = a[i] > 127 ? 1 : 0;
  let bgMask = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i++) bgMask[i] = solid[i] ? 0 : 1;

  for (let r = 0; r < radius; r++) {
    const rim = new Uint8Array(w * h);
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const pi = y * w + x;
        if (!solid[pi]) continue;
        if (
          (x > 0 && bgMask[pi - 1]) ||
          (x < w - 1 && bgMask[pi + 1]) ||
          (y > 0 && bgMask[pi - w]) ||
          (y < h - 1 && bgMask[pi + w])
        ) {
          rim[pi] = 1;
        }
      }
    }
    let any = false;
    for (let i = 0; i < w * h; i++) {
      if (rim[i]) {
        a[i] = Math.min(a[i], floor);
        bgMask[i] = 1;
        solid[i] = 0;
        any = true;
      }
    }
    if (!any) break;
  }
  return a;
}

function refineMatte(data, alpha, outside, w, h, br, bg, bb, { color_tol, bg_sat, feather = 1, cleanup = true }) {
  let outOutside = expandOutsideBgLike(
    data,
    alpha,
    outside,
    w,
    h,
    br,
    bg,
    bb,
    color_tol,
    bg_sat,
  );
  for (let i = 0; i < w * h; i++) {
    if (outOutside[i]) alpha[i] = 0;
  }
  if (!cleanup) {
    for (let i = 0; i < w * h; i++) {
      data[i * 4 + 3] = Math.max(0, Math.min(255, alpha[i] | 0));
    }
    return alpha;
  }

  const minIsland = minIslandPixels(h, w);
  removeSmallIslands(alpha, w, h, minIsland);
  defringeAlpha(alpha, data, w, h, br, bg, bb, color_tol, bg_sat);

  const fg = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i++) {
    if (alpha[i] > 96) fg[i] = 1;
  }
  const opened = morphOpen4(fg, w, h, 1);
  for (let i = 0; i < w * h; i++) {
    if (fg[i] && !opened[i]) alpha[i] = 0;
  }
  if (feather > 0) featherRim(alpha, w, h, feather, 0.42);
  for (let i = 0; i < w * h; i++) {
    data[i * 4 + 3] = Math.max(0, Math.min(255, alpha[i] | 0));
  }
  return alpha;
}

function floodViaDilation(seed, candidates, w, h, maxIter) {
  const outside = new Uint8Array(seed);
  let remain = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i++) {
    remain[i] = candidates[i] && !outside[i] ? 1 : 0;
  }
  const limit = maxIter ?? h + w + 2;
  for (let iter = 0; iter < limit; iter++) {
    if (!remain.some(Boolean)) break;
    const grown = new Uint8Array(w * h);
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const pi = y * w + x;
        if (!outside[pi]) continue;
        for (const [dx, dy] of [[0, 0], [-1, 0], [1, 0], [0, -1], [0, 1]]) {
          const nx = x + dx;
          const ny = y + dy;
          if (nx < 0 || nx >= w || ny < 0 || ny >= h) continue;
          const ni = ny * w + nx;
          if (remain[ni]) grown[ni] = 1;
        }
      }
    }
    if (!grown.some(Boolean)) break;
    for (let i = 0; i < w * h; i++) {
      if (grown[i]) {
        outside[i] = 1;
        remain[i] = 0;
      }
    }
  }
  return outside;
}

function downscaleRgba(data, w, h, maxDim) {
  const longest = Math.max(w, h);
  if (longest <= maxDim) {
    return { data: new Uint8ClampedArray(data), w, h, scale: 1 };
  }
  const scale = maxDim / longest;
  const nw = Math.max(1, Math.round(w * scale));
  const nh = Math.max(1, Math.round(h * scale));
  const canvas = document.createElement("canvas");
  canvas.width = nw;
  canvas.height = nh;
  const srcCanvas = document.createElement("canvas");
  srcCanvas.width = w;
  srcCanvas.height = h;
  const srcCtx = srcCanvas.getContext("2d");
  srcCtx.putImageData(new ImageData(data, w, h), 0, 0);
  const ctx = canvas.getContext("2d");
  ctx.drawImage(srcCanvas, 0, 0, nw, nh);
  const small = ctx.getImageData(0, 0, nw, nh);
  return { data: small.data, w: nw, h: nh, scale };
}

function upscaleBoolMask(mask, sw, sh, w, h) {
  if (sw === w && sh === h) return mask;
  const canvas = document.createElement("canvas");
  canvas.width = sw;
  canvas.height = sh;
  const ctx = canvas.getContext("2d");
  const imgData = ctx.createImageData(sw, sh);
  for (let i = 0; i < sw * sh; i++) {
    const v = mask[i] ? 255 : 0;
    imgData.data[i * 4] = v;
    imgData.data[i * 4 + 1] = v;
    imgData.data[i * 4 + 2] = v;
    imgData.data[i * 4 + 3] = 255;
  }
  ctx.putImageData(imgData, 0, 0);
  const dst = document.createElement("canvas");
  dst.width = w;
  dst.height = h;
  const dctx = dst.getContext("2d");
  dctx.imageSmoothingEnabled = false;
  dctx.drawImage(canvas, 0, 0, w, h);
  const up = dctx.getImageData(0, 0, w, h);
  const out = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i++) {
    out[i] = up.data[i * 4] > 127 ? 1 : 0;
  }
  return out;
}

function dedupeSeedPoints(points, grid = 3) {
  const step = Math.max(1, grid | 0);
  const seen = new Set();
  const out = [];
  for (const pt of points) {
    const key = `${Math.floor(pt[0] / step)},${Math.floor(pt[1] / step)}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push([Math.round(pt[0]), Math.round(pt[1])]);
  }
  return out;
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

function collectBrushSeeds(points, offsets, w, h) {
  const seeds = [];
  const seen = new Set();
  for (const [cx, cy] of points) {
    for (const [dx, dy] of offsets) {
      const sx = cx + dx;
      const sy = cy + dy;
      if (sx < 0 || sx >= w || sy < 0 || sy >= h) continue;
      const key = `${sx},${sy}`;
      if (seen.has(key)) continue;
      seen.add(key);
      seeds.push([sx, sy]);
    }
  }
  return seeds;
}

function scaleSeeds(seeds, scale, nw, nh) {
  const out = [];
  const seen = new Set();
  for (const [x, y] of seeds) {
    const sx = Math.min(nw - 1, Math.max(0, Math.round(x * scale)));
    const sy = Math.min(nh - 1, Math.max(0, Math.round(y * scale)));
    const key = `${sx},${sy}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push([sx, sy]);
  }
  return out;
}

function floodFromSeedsDilate(data, w, h, seeds, br, bg, bb, colorTol, bgSat) {
  const seedMask = new Uint8Array(w * h);
  for (const [sx, sy] of seeds) {
    if (sx < 0 || sx >= w || sy < 0 || sy >= h) continue;
    const pi = sy * w + sx;
    if (data[pi * 4 + 3] === 0) continue;
    const i = pi * 4;
    if (!isBackgroundLike(data[i], data[i + 1], data[i + 2], br, bg, bb, colorTol, bgSat)) continue;
    seedMask[pi] = 1;
  }
  if (!seedMask.some(Boolean)) return seedMask;
  const candidates = new Uint8Array(w * h);
  const bgLike = bgLikeMask(data, w, h, br, bg, bb, colorTol, bgSat);
  for (let i = 0; i < w * h; i++) {
    candidates[i] = bgLike[i] && data[i * 4 + 3] > 0 ? 1 : 0;
  }
  return floodViaDilation(seedMask, candidates, w, h);
}

function floodFromSeeds(data, w, h, seeds, colorTol, maxDim = 768) {
  if (!seeds.length) return { outside: new Uint8Array(w * h), bg: null, bgSat: 0 };

  let bg = null;
  let bgSat = 0;
  for (const [sx, sy] of seeds) {
    if (sx < 0 || sx >= w || sy < 0 || sy >= h) continue;
    const pi = sy * w + sx;
    if (data[pi * 4 + 3] === 0) continue;
    const i = pi * 4;
    bg = [data[i], data[i + 1], data[i + 2]];
    bgSat = pixelSat(bg[0], bg[1], bg[2]);
    break;
  }
  if (!bg) return { outside: new Uint8Array(w * h), bg: null, bgSat: 0 };

  const scaled = downscaleRgba(data, w, h, maxDim);
  const workSeeds = scaleSeeds(seeds, scaled.scale === 1 ? 1 : scaled.w / w, scaled.w, scaled.h);
  const [br, bgg, bb] = bg;
  let outside = floodFromSeedsDilate(
    scaled.data,
    scaled.w,
    scaled.h,
    workSeeds,
    br,
    bgg,
    bb,
    colorTol,
    bgSat,
  );
  if (scaled.scale < 1) {
    outside = upscaleBoolMask(outside, scaled.w, scaled.h, w, h);
  }
  return { outside, bg, bgSat };
}

function clampBrushSize(brushSize) {
  return Math.max(1, Math.min(50, brushSize | 0));
}

/** 全图同色剔除（橡皮擦 + 全图同色）：全分辨率，含品红键色增强 */
export function globalSameColorMatteRgba(data, w, h, seedX, seedY, opts = {}) {
  if (!data || w <= 0 || h <= 0) return false;
  const colorTol = opts.color_tol ?? 34;
  const sx = Math.max(0, Math.min(w - 1, Math.round(seedX)));
  const sy = Math.max(0, Math.min(h - 1, Math.round(seedY)));
  const si = (sy * w + sx) * 4;
  if (data[si + 3] === 0) return false;

  const br = data[si];
  const bg = data[si + 1];
  const bb = data[si + 2];
  const bgSat = pixelSat(br, bg, bb);
  const keyMagenta = br > 160 && bb > 160 && bg < 140 && br + bb - bg > 160;

  const outside = globalKeyColorMask(data, w, h, br, bg, bb, colorTol + (keyMagenta ? 10 : 0), bgSat);

  if (keyMagenta) {
    for (let pi = 0; pi < w * h; pi++) {
      if (outside[pi]) continue;
      const i = pi * 4;
      if (data[i + 3] === 0) continue;
      const r = data[i];
      const g = data[i + 1];
      const b = data[i + 2];
      if (r > 150 && b > 150 && g < 150 && r + b - g > 140) {
        if (colorDist(r, g, b, br, bg, bb) <= colorTol + 18) outside[pi] = 1;
      }
    }
  }

  if (!outside.some(Boolean)) return false;

  const alpha = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i++) alpha[i] = data[i * 4 + 3];

  refineMatte(data, alpha, outside, w, h, br, bg, bb, {
    color_tol: colorTol,
    bg_sat: bgSat,
    feather: 0,
    cleanup: true,
  });
  return true;
}

/**
 * 对 RGBA 缓冲执行笔画抠图（原地修改 data）。
 */
export function strokeMatteRgba(data, w, h, points, opts = {}) {
  if (!points?.length || !data || w <= 0 || h <= 0) return false;

  const colorTol = opts.color_tol ?? 34;
  const brushSize = clampBrushSize(opts.brush_size ?? 1);
  const feather = opts.cleanup ? Math.max(0, opts.feather ?? 1) : 0;
  const cleanup = opts.cleanup !== false;
  const globalSame = !!opts.global_same_color;

  if (globalSame) {
    const [sx, sy] = points[0];
    return globalSameColorMatteRgba(data, w, h, sx, sy, { color_tol: colorTol });
  }

  const radius = Math.max(0, ((brushSize - 1) / 2) | 0);
  const offsets = brushDiskOffsets(radius);
  const deduped = dedupeSeedPoints(points, Math.max(2, (brushSize / 2) | 0));
  const seeds = collectBrushSeeds(deduped, offsets, w, h);

  const { outside, bg, bgSat } = floodFromSeeds(
    data,
    w,
    h,
    seeds,
    colorTol,
    opts.max_dim ?? 512,
  );
  if (!bg || !outside.some(Boolean)) return false;

  const alpha = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i++) alpha[i] = data[i * 4 + 3];

  refineMatte(data, alpha, outside, w, h, bg[0], bg[1], bg[2], {
    color_tol: colorTol,
    bg_sat: bgSat,
    feather,
    cleanup,
  });
  return true;
}

/**
 * 去除外围纯色背景（原地修改）。
 */
export function borderMatteRgba(data, w, h, opts = {}) {
  if (!data || w <= 0 || h <= 0) return false;

  const colorTol = opts.color_tol ?? 34;
  const borderBand = opts.border_band ?? 4;
  const feather = Math.max(0, opts.feather ?? 1);
  const maxDim = opts.max_dim ?? 768;

  const scaled = downscaleRgba(data, w, h, maxDim);
  const sw = scaled.w;
  const sh = scaled.h;
  const sd = scaled.data;
  const [br, bg, bb] = estimateBorderBgColor(sd, sw, sh, borderBand);
  const bgSat = pixelSat(br, bg, bb);

  const bgLike = bgLikeMask(sd, sw, sh, br, bg, bb, colorTol + 6, bgSat);
  const candidates = new Uint8Array(sw * sh);
  for (let i = 0; i < sw * sh; i++) {
    candidates[i] = bgLike[i] && sd[i * 4 + 3] > 0 ? 1 : 0;
  }

  const band = Math.max(1, Math.min(borderBand, (sh / 4) | 0, (sw / 4) | 0));
  const seedMask = new Uint8Array(sw * sh);
  for (let y = 0; y < sh; y++) {
    for (let x = 0; x < sw; x++) {
      const edge = y < band || y >= sh - band || x < band || x >= sw - band;
      if (!edge) continue;
      const pi = y * sw + x;
      if (candidates[pi]) seedMask[pi] = 1;
    }
  }
  let outside = floodViaDilation(seedMask, candidates, sw, sh);
  if (scaled.scale < 1) {
    outside = upscaleBoolMask(outside, sw, sh, w, h);
  }
  if (!outside.some(Boolean)) return false;

  const alpha = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i++) alpha[i] = data[i * 4 + 3];

  refineMatte(data, alpha, outside, w, h, br, bg, bb, {
    color_tol: colorTol,
    bg_sat: bgSat,
    feather,
    cleanup: true,
  });
  return true;
}

/**
 * 对已抠图结果收边（原地修改）。
 */
export function polishMatteRgba(data, w, h, opts = {}) {
  if (!data || w <= 0 || h <= 0) return false;

  const colorTol = opts.color_tol ?? 34;
  const feather = Math.max(0, opts.feather ?? 1);
  const alpha = new Uint8Array(w * h);
  let hasTransparent = false;
  for (let i = 0; i < w * h; i++) {
    alpha[i] = data[i * 4 + 3];
    if (alpha[i] === 0) hasTransparent = true;
  }
  if (!hasTransparent) return false;

  const [br, bg, bb] = estimateBorderBgColor(data, w, h, 4);
  const bgSat = pixelSat(br, bg, bb);
  const outside = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i++) {
    if (alpha[i] === 0) outside[i] = 1;
  }

  refineMatte(data, alpha, outside, w, h, br, bg, bb, {
    color_tol: colorTol,
    bg_sat: bgSat,
    feather,
    cleanup: true,
  });
  return true;
}

/** 预览用：对 ImageData 批量应用笔画点（可降采样尺寸）。 */
export function applyPreviewStrokePoints(imageData, points, opts = {}) {
  const w = imageData.width;
  const h = imageData.height;
  return strokeMatteRgba(imageData.data, w, h, points, {
    ...opts,
    cleanup: false,
    max_dim: Math.max(w, h),
  });
}

/** RGBA 缓冲 → PNG Blob */
export function rgbaToPngBlob(data, w, h) {
  return new Promise((resolve, reject) => {
    const canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d");
    if (!ctx) {
      reject(new Error("canvas"));
      return;
    }
    ctx.putImageData(new ImageData(data, w, h), 0, 0);
    canvas.toBlob(
      (blob) => (blob ? resolve(blob) : reject(new Error("encode"))),
      "image/png",
    );
  });
}
