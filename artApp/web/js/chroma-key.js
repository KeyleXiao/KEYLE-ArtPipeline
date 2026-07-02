/**
 * 色键去底（对标 tools/alpha_matte.chroma_key_to_alpha / ImageMagick -fuzz）。
 * 操作 RGBA 交错缓冲；全图生效，含镂空细缝。
 */

function parseHexRgb(hex) {
  const s = String(hex || "")
    .trim()
    .replace(/^#/, "");
  if (!/^[0-9a-fA-F]{6}$/.test(s)) return null;
  return [
    parseInt(s.slice(0, 2), 16),
    parseInt(s.slice(2, 4), 16),
    parseInt(s.slice(4, 6), 16),
  ];
}

function isMagentaKey(kr, kg, kb) {
  return kr > 200 && kb > 200 && kg < 90;
}

function clampByte(v) {
  return v < 0 ? 0 : v > 255 ? 255 : v | 0;
}

function magentaScore(r, g, b) {
  return Math.min(r, b) - g;
}

function keyDistance(r, g, b, kr, kg, kb, magentaKey, keyScore) {
  let dist = Math.hypot(r - kr, g - kg, b - kb);
  if (magentaKey) {
    const score = magentaScore(r, g, b);
    const magDist = Math.abs(score - keyScore) * 1.6 + Math.abs(r - b) * 0.12;
    dist = Math.min(dist, magDist);
  }
  return dist;
}

function buildAlphaMap(data, w, h) {
  const alpha = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i++) alpha[i] = data[i * 4 + 3];
  return alpha;
}

function neighborTransparentCount(alpha, w, h, x, y, radius = 1) {
  let count = 0;
  for (let dy = -radius; dy <= radius; dy++) {
    for (let dx = -radius; dx <= radius; dx++) {
      if (!dx && !dy) continue;
      const nx = x + dx;
      const ny = y + dy;
      if (nx < 0 || ny < 0 || nx >= w || ny >= h) {
        count++;
        continue;
      }
      if (alpha[ny * w + nx] < 16) count++;
    }
  }
  return count;
}

function despillMagentaRgb(r, g, b, strength = 1) {
  const score = magentaScore(r, g, b);
  if (score <= 6) return [r, g, b];
  const reduce = score * 0.94 * strength;
  let nr = Math.max(0, r - reduce);
  let nb = Math.max(0, b - reduce);
  const cap = g + Math.max(10, score * 0.18);
  nr = Math.min(nr, cap);
  nb = Math.min(nb, cap);
  return [clampByte(nr), g, clampByte(nb)];
}

function spillAlphaCut(a0, score, keyScore, strength = 1) {
  if (score <= 8 || keyScore <= 0) return a0;
  const ratio = score / keyScore;
  if (ratio <= 0.06) return a0;
  const cut = Math.min(1, ratio * 1.35 * strength);
  return Math.round(a0 * (1 - cut));
}

/**
 * @param {Uint8ClampedArray} data
 * @param {number} fuzz 0–100，与 ImageMagick -fuzz 百分比类似
 */
export function chromaKeyRgba(data, w, h, opts = {}) {
  if (!data || w <= 0 || h <= 0) return false;

  let kr = opts.key_r ?? 255;
  let kg = opts.key_g ?? 0;
  let kb = opts.key_b ?? 255;
  if (opts.key_hex) {
    const parsed = parseHexRgb(opts.key_hex);
    if (parsed) [kr, kg, kb] = parsed;
  }

  const fuzz = Math.max(0, Math.min(100, opts.fuzz ?? 22));
  const maxDist = Math.hypot(255, 255, 255);
  const threshold = (fuzz / 100) * maxDist * 1.12;
  const soft = Math.max(6, threshold * 0.42);
  const magentaKey = isMagentaKey(kr, kg, kb);
  const keyScore = magentaKey ? Math.min(kr, kb) - kg : 0;
  const spillMin = Math.max(8, 22 - fuzz * 0.08);

  let changed = false;
  for (let pi = 0; pi < w * h; pi++) {
    const i = pi * 4;
    const a0 = data[i + 3];
    if (a0 === 0) continue;

    const r = data[i];
    const g = data[i + 1];
    const b = data[i + 2];
    const dist = keyDistance(r, g, b, kr, kg, kb, magentaKey, keyScore);

    let na = a0;
    if (dist < threshold) {
      if (dist <= threshold - soft) {
        na = 0;
      } else {
        const t = (dist - (threshold - soft)) / soft;
        na = Math.round(a0 * t);
      }
    }

    if (magentaKey && na > 0) {
      const score = magentaScore(r, g, b);
      if (score > spillMin) {
        na = spillAlphaCut(na, score, keyScore, 0.85);
        const [nr, ng, nb] = despillMagentaRgb(r, g, b, 0.75);
        if (nr !== r || nb !== b) {
          data[i] = nr;
          data[i + 1] = ng;
          data[i + 2] = nb;
          changed = true;
        }
      }
    }

    if (na !== a0) {
      data[i + 3] = na;
      changed = true;
    }
  }

  const feather = Math.max(0, Math.min(4, opts.feather ?? 2));
  if (magentaKey) {
    if (despillMagentaEdges(data, w, h, keyScore, spillMin, feather)) changed = true;
  } else if (changed && feather > 0) {
    defringeChroma(data, w, h, kr, kg, kb, threshold, magentaKey, keyScore, feather);
  }

  return changed;
}

function despillMagentaEdges(data, w, h, keyScore, spillMin, passes) {
  let changed = false;
  const passCount = Math.max(2, passes + 1);
  for (let pass = 0; pass < passCount; pass++) {
    const alpha = buildAlphaMap(data, w, h);
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const pi = y * w + x;
        if (alpha[pi] < 4) continue;
        const i = pi * 4;
        let r = data[i];
        let g = data[i + 1];
        let b = data[i + 2];
        const score = magentaScore(r, g, b);
        const nearEdge = neighborTransparentCount(alpha, w, h, x, y, pass === 0 ? 1 : 2) > 0;
        const strength = nearEdge ? 1.15 : 0.72;

        if (score > spillMin * (nearEdge ? 0.55 : 0.85)) {
          const [nr, ng, nb] = despillMagentaRgb(r, g, b, strength);
          if (nr !== r || nb !== b) {
            data[i] = nr;
            data[i + 1] = ng;
            data[i + 2] = nb;
            changed = true;
            r = nr;
            b = nb;
          }
        }

        if (!nearEdge) continue;
        const score2 = magentaScore(r, g, b);
        if (score2 <= spillMin * 0.45) continue;

        const na = spillAlphaCut(data[i + 3], score2, keyScore, 1.1);
        if (score2 > spillMin * 1.35 || na < 12) {
          data[i + 3] = 0;
          changed = true;
        } else if (na < data[i + 3]) {
          data[i + 3] = na;
          changed = true;
        } else if (score2 > spillMin && data[i + 3] > 48) {
          data[i + 3] = Math.min(data[i + 3], 48);
          changed = true;
        }
      }
    }
  }
  return changed;
}

function defringeChroma(data, w, h, kr, kg, kb, threshold, magentaKey, keyScore, passes = 2) {
  const softThr = threshold * 0.85;
  for (let pass = 0; pass < passes; pass++) {
    const alpha = buildAlphaMap(data, w, h);
    for (let y = 1; y < h - 1; y++) {
      for (let x = 1; x < w - 1; x++) {
        const pi = y * w + x;
        if (alpha[pi] < 8) continue;
        const i = pi * 4;
        const r = data[i];
        const g = data[i + 1];
        const b = data[i + 2];
        const dist = keyDistance(r, g, b, kr, kg, kb, magentaKey, keyScore);
        if (dist > softThr) continue;
        if (!neighborTransparentCount(alpha, w, h, x, y, 1)) continue;
        data[i + 3] = dist < threshold * 0.55 ? 0 : Math.min(data[i + 3], 48);
      }
    }
  }
}

export const CHROMA_PRESET_MAGENTA = {
  key_hex: "#FF00FF",
  fuzz: 22,
  feather: 2,
};

export function parseKeyHex(hex) {
  return parseHexRgb(hex);
}
