#!/usr/bin/env python3
"""将 AI 图外侧纯色/灰底转为透明（不依赖 rembg）。"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

_NEIGHBOR8_DY = np.array([-1, -1, -1, 0, 0, 1, 1, 1], dtype=np.int16)
_NEIGHBOR8_DX = np.array([-1, 0, 1, -1, 1, -1, 0, 1], dtype=np.int16)


def _color_dist(a: np.ndarray, b: np.ndarray) -> float:
    d = a.astype(np.float32) - b.astype(np.float32)
    return float(np.sqrt(np.dot(d, d)))


def _pixel_saturation(rgb: np.ndarray) -> float:
    return float(np.max(rgb) - np.min(rgb))


def _estimate_border_bg_color(rgb: np.ndarray, *, border_band: int) -> np.ndarray:
    """从四边采样估计背景色（中位数，抗角落主体干扰）。"""
    h, w = rgb.shape[:2]
    band = max(1, min(border_band, h // 4, w // 4))
    samples: list[np.ndarray] = []
    for x in range(w):
        for y in range(band):
            samples.append(rgb[y, x])
        for y in range(h - band, h):
            samples.append(rgb[y, x])
    for y in range(band, h - band):
        for x in range(band):
            samples.append(rgb[y, x])
        for x in range(w - band, w):
            samples.append(rgb[y, x])
    if not samples:
        return np.array([127.0, 127.0, 127.0], dtype=np.float32)
    return np.median(np.stack(samples, axis=0), axis=0).astype(np.float32)


def _is_background_like(
    px: np.ndarray,
    bg: np.ndarray,
    *,
    color_tol: float,
    bg_sat: float,
) -> bool:
    if _color_dist(px, bg) > color_tol:
        return False
    px_sat = _pixel_saturation(px)
    if px_sat > max(28.0, bg_sat + 20.0):
        return False
    return True


def _bg_like_mask(
    rgb: np.ndarray,
    bg: np.ndarray,
    *,
    color_tol: float,
    bg_sat: float,
) -> np.ndarray:
    diff = np.linalg.norm(rgb - bg.reshape(1, 1, 3), axis=2)
    sat = np.max(rgb, axis=2) - np.min(rgb, axis=2)
    return (diff <= color_tol) & (sat <= max(28.0, bg_sat + 20.0))


def _morph_erode4(mask: np.ndarray) -> np.ndarray:
    padded = np.pad(mask, 1, mode="constant", constant_values=False)
    return (
        padded[:-2, 1:-1]
        & padded[2:, 1:-1]
        & padded[1:-1, :-2]
        & padded[1:-1, 2:]
        & padded[1:-1, 1:-1]
    )


def _morph_dilate4(mask: np.ndarray) -> np.ndarray:
    padded = np.pad(mask, 1, mode="constant", constant_values=False)
    return (
        padded[:-2, 1:-1]
        | padded[2:, 1:-1]
        | padded[1:-1, :-2]
        | padded[1:-1, 2:]
        | padded[1:-1, 1:-1]
    )


def _morph_open4(mask: np.ndarray, *, iters: int = 1) -> np.ndarray:
    out = mask
    for _ in range(max(0, iters)):
        out = _morph_dilate4(_morph_erode4(out))
    return out


def _neighbor_transparent_count(alpha: np.ndarray, outside: np.ndarray) -> np.ndarray:
    trans = outside | (alpha == 0)
    padded = np.pad(trans, 1, mode="constant", constant_values=True)
    return (
        padded[:-2, 1:-1].astype(np.int16)
        + padded[2:, 1:-1]
        + padded[1:-1, :-2]
        + padded[1:-1, 2:]
    )


def _expand_outside_bg_like(
    rgb: np.ndarray,
    alpha: np.ndarray,
    outside: np.ndarray,
    bg: np.ndarray,
    *,
    color_tol: float,
    bg_sat: float,
    max_iter: int = 4,
) -> np.ndarray:
    out = outside.copy()
    bg_like = _bg_like_mask(rgb, bg, color_tol=color_tol, bg_sat=bg_sat)
    for _ in range(max_iter):
        opaque = (alpha > 0) & ~out
        if not opaque.any():
            break
        neighbor_trans = _neighbor_transparent_count(alpha, out)
        peel = opaque & bg_like & (neighbor_trans >= 1)
        if not peel.any():
            break
        out |= peel
    return out


def _remove_small_islands(alpha: np.ndarray, min_pixels: int, *, alpha_thresh: int = 10) -> np.ndarray:
    """形态学开运算剔除小块碎点（全向量化，避免 Python 连通域扫描）。"""
    fg = alpha > alpha_thresh
    if not fg.any():
        return alpha
    radius = max(1, min(4, int(round(np.sqrt(max(1, min_pixels)) / 2))))
    eroded = fg
    for _ in range(radius):
        eroded = _morph_erode4(eroded)
    opened = eroded
    for _ in range(radius):
        opened = _morph_dilate4(opened)
    removed = fg & ~opened
    if not removed.any():
        return alpha
    out = alpha.copy()
    out[removed] = 0
    return out


def _defringe_alpha(
    alpha: np.ndarray,
    rgb: np.ndarray,
    bg: np.ndarray,
    *,
    color_tol: float,
    bg_sat: float,
    passes: int = 2,
) -> np.ndarray:
    a = alpha.astype(np.float32)
    sat_limit = max(28.0, bg_sat + 20.0)
    for _ in range(max(0, passes)):
        solid = a > 8
        neighbor_trans = _neighbor_transparent_count(a.astype(np.uint8), ~solid)
        edge = solid & (neighbor_trans > 0)
        if not edge.any():
            break
        ys, xs = np.where(edge)
        px = rgb[ys, xs]
        diff = np.linalg.norm(px - bg.reshape(1, 3), axis=1)
        sat = np.max(px, axis=1) - np.min(px, axis=1)
        sat_ok = sat <= sat_limit
        hard = (diff <= color_tol * 0.9) & sat_ok
        soft = (diff <= color_tol * 1.15) & sat_ok & ~hard
        if hard.any():
            a[ys[hard], xs[hard]] = 0
        if soft.any():
            a[ys[soft], xs[soft]] = np.minimum(a[ys[soft], xs[soft]], 48.0)
    return a


def _feather_rim(alpha: np.ndarray, *, radius: int = 1, strength: float = 0.5) -> np.ndarray:
    if radius <= 0:
        return alpha
    a = alpha.astype(np.float32)
    solid = a > 127
    bg_mask = ~solid
    for _ in range(radius):
        neighbor_bg = np.zeros_like(bg_mask)
        neighbor_bg[1:, :] |= bg_mask[:-1, :]
        neighbor_bg[:-1, :] |= bg_mask[1:, :]
        neighbor_bg[:, 1:] |= bg_mask[:, :-1]
        neighbor_bg[:, :-1] |= bg_mask[:, 1:]
        rim = neighbor_bg & solid
        if not rim.any():
            break
        floor = 255.0 * (1.0 - strength)
        a[rim] = np.minimum(a[rim], floor)
        bg_mask |= rim
        solid &= ~rim
    return a


def _min_island_pixels(h: int, w: int) -> int:
    return max(18, int(h * w * 0.00006))


def _refine_matte(
    arr: np.ndarray,
    rgb: np.ndarray,
    outside: np.ndarray,
    bg: np.ndarray,
    *,
    color_tol: float,
    bg_sat: float,
    feather: int = 1,
    cleanup: bool = True,
) -> np.ndarray:
    out = arr.copy()
    if not outside.any():
        return out

    alpha = out[:, :, 3].astype(np.float32)
    outside = _expand_outside_bg_like(
        rgb, alpha.astype(np.uint8), outside, bg, color_tol=color_tol, bg_sat=bg_sat
    )
    alpha[outside] = 0

    if not cleanup:
        out[:, :, 3] = alpha.astype(np.uint8)
        return out

    h, w = arr.shape[:2]
    min_island = _min_island_pixels(h, w)
    alpha = _remove_small_islands(alpha, min_island)
    alpha = _defringe_alpha(alpha, rgb, bg, color_tol=color_tol, bg_sat=bg_sat)

    fg = alpha > 96
    if fg.any():
        spikes = fg & ~_morph_open4(fg, iters=1)
        alpha[spikes] = 0

    if feather > 0:
        alpha = _feather_rim(alpha, radius=feather, strength=0.42)

    out[:, :, 3] = np.clip(alpha, 0, 255).astype(np.uint8)
    return out


def polish_matte_alpha(
    im: Image.Image,
    *,
    color_tol: float = 34.0,
    feather: int = 1,
) -> Image.Image:
    """对已抠图结果做一次性收边（笔画结束时调用）。"""
    arr = np.array(im.convert("RGBA"))
    h, w = arr.shape[:2]
    if h == 0 or w == 0:
        return im.convert("RGBA")
    rgb = arr[:, :, :3].astype(np.float32)
    alpha_u8 = arr[:, :, 3]
    if not (alpha_u8 == 0).any():
        return im.convert("RGBA")
    bg = _estimate_border_bg_color(rgb, border_band=4)
    bg_sat = _pixel_saturation(bg)
    outside = alpha_u8 == 0
    out = _refine_matte(
        arr,
        rgb,
        outside,
        bg,
        color_tol=color_tol,
        bg_sat=bg_sat,
        feather=max(0, int(feather)),
        cleanup=True,
    )
    return Image.fromarray(out, mode="RGBA")


def _dedupe_seed_points(points: list[tuple[int, int]], *, grid: int = 3) -> list[tuple[int, int]]:
    if not points:
        return []
    step = max(1, int(grid))
    seen: set[tuple[int, int]] = set()
    out: list[tuple[int, int]] = []
    for x, y in points:
        key = (int(x) // step, int(y) // step)
        if key in seen:
            continue
        seen.add(key)
        out.append((int(x), int(y)))
    return out


def _upscale_bool_mask(mask: np.ndarray, h: int, w: int) -> np.ndarray:
    if mask.shape[0] == h and mask.shape[1] == w:
        return mask
    up = np.array(
        Image.fromarray((mask.astype(np.uint8) * 255), mode="L").resize(
            (w, h), Image.Resampling.NEAREST
        )
    )
    return up > 127


def _downscale_for_flood(
    arr: np.ndarray,
    rgb: np.ndarray,
    seeds: list[tuple[int, int]],
    *,
    max_dim: int,
) -> tuple[np.ndarray, np.ndarray, list[tuple[int, int]], float]:
    h, w = arr.shape[:2]
    longest = max(h, w)
    if longest <= max_dim:
        return arr, rgb, seeds, 1.0
    scale = max_dim / float(longest)
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    small_arr = np.asarray(
        Image.fromarray(arr, mode="RGBA").resize((nw, nh), Image.Resampling.NEAREST)
    )
    small_rgb = small_arr[:, :, :3].astype(np.float32)
    small_seeds: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for x, y in seeds:
        sx = min(nw - 1, max(0, int(round(x * scale))))
        sy = min(nh - 1, max(0, int(round(y * scale))))
        key = (sx, sy)
        if key in seen:
            continue
        seen.add(key)
        small_seeds.append(key)
    return small_arr, small_rgb, small_seeds, scale


def _flood_via_dilation(
    seed: np.ndarray,
    candidates: np.ndarray,
    *,
    max_iter: int | None = None,
) -> np.ndarray:
    outside = seed.copy()
    if not outside.any() or not candidates.any():
        return outside
    h, w = outside.shape
    if max_iter is None:
        max_iter = h + w + 2
    remain = candidates & ~outside
    for _ in range(max_iter):
        if not remain.any():
            break
        grown = _morph_dilate4(outside) & remain
        if not grown.any():
            break
        outside |= grown
        remain &= ~grown
    return outside


def _flood_from_seeds_dilate(
    arr: np.ndarray,
    rgb: np.ndarray,
    seeds: list[tuple[int, int]],
    *,
    color_tol: float,
    bg_sat: float,
    bg: np.ndarray,
) -> np.ndarray:
    h, w = arr.shape[:2]
    seed_mask = np.zeros((h, w), dtype=bool)
    for sx, sy in seeds:
        if not (0 <= sx < w and 0 <= sy < h):
            continue
        if arr[sy, sx, 3] == 0:
            continue
        if not _is_background_like(rgb[sy, sx], bg, color_tol=color_tol, bg_sat=bg_sat):
            continue
        seed_mask[sy, sx] = True
    if not seed_mask.any():
        return seed_mask
    candidates = _bg_like_mask(rgb, bg, color_tol=color_tol, bg_sat=bg_sat) & (arr[:, :, 3] > 0)
    return _flood_via_dilation(seed_mask, candidates)


def _flood_from_seeds(
    arr: np.ndarray,
    rgb: np.ndarray,
    seeds: list[tuple[int, int]],
    *,
    color_tol: float,
    step_tol: float,
    max_dim: int = 768,
) -> tuple[np.ndarray, np.ndarray | None, float]:
    """多种子泛洪：大图先降采样，再用向量化膨胀泛洪。"""
    del step_tol  # 膨胀路径以 bg_like 为准，换取实时性能
    if not seeds:
        return np.zeros(arr.shape[:2], dtype=bool), None, 0.0

    h, w = arr.shape[:2]
    bg: np.ndarray | None = None
    bg_sat = 0.0
    for sx, sy in seeds:
        if not (0 <= sx < w and 0 <= sy < h):
            continue
        if arr[sy, sx, 3] == 0:
            continue
        bg = rgb[sy, sx].copy()
        bg_sat = _pixel_saturation(bg)
        break
    if bg is None:
        return np.zeros((h, w), dtype=bool), None, 0.0

    work_arr, work_rgb, work_seeds, scale = _downscale_for_flood(arr, rgb, seeds, max_dim=max_dim)
    outside = _flood_from_seeds_dilate(
        work_arr,
        work_rgb,
        work_seeds,
        color_tol=color_tol,
        bg_sat=bg_sat,
        bg=bg,
    )
    if scale < 1.0:
        outside = _upscale_bool_mask(outside, h, w)
    return outside, bg, bg_sat


def _collect_brush_seeds(
    points: list[tuple[int, int]],
    offsets: list[tuple[int, int]],
    *,
    h: int,
    w: int,
) -> list[tuple[int, int]]:
    seeds: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for cx, cy in points:
        for dx, dy in offsets:
            sx = int(cx + dx)
            sy = int(cy + dy)
            if not (0 <= sx < w and 0 <= sy < h):
                continue
            key = (sx, sy)
            if key in seen:
                continue
            seen.add(key)
            seeds.append(key)
    return seeds


def _flood_transparent_mask(
    arr: np.ndarray,
    rgb: np.ndarray,
    seed_x: int,
    seed_y: int,
    *,
    color_tol: float,
    step_tol: float,
) -> np.ndarray:
    outside, _, _ = _flood_from_seeds(
        arr,
        rgb,
        [(int(seed_x), int(seed_y))],
        color_tol=color_tol,
        step_tol=step_tol,
    )
    return outside


def _flood_neighbors(y: int, x: int, h: int, w: int) -> list[tuple[int, int]]:
    nbs: list[tuple[int, int]] = []
    for dy, dx in zip(_NEIGHBOR8_DY, _NEIGHBOR8_DX, strict=True):
        ny = y + int(dy)
        nx = x + int(dx)
        if 0 <= ny < h and 0 <= nx < w:
            nbs.append((ny, nx))
    return nbs


def border_matte_to_alpha(
    im: Image.Image,
    *,
    color_tol: float = 34.0,
    step_tol: float = 16.0,
    border_band: int = 4,
    feather: int = 1,
    cleanup: bool = True,
) -> Image.Image:
    del step_tol
    arr = np.array(im.convert("RGBA"))
    h, w = arr.shape[:2]
    work_arr, work_rgb, _, scale = _downscale_for_flood(
        arr,
        arr[:, :, :3].astype(np.float32),
        [(0, 0)],
        max_dim=768,
    )
    wh, ww = work_arr.shape[:2]
    rgb = work_arr[:, :, :3].astype(np.float32)
    bg = _estimate_border_bg_color(rgb, border_band=border_band)
    bg_sat = _pixel_saturation(bg)

    bg_like = _bg_like_mask(rgb, bg, color_tol=color_tol + 6.0, bg_sat=bg_sat) & (work_arr[:, :, 3] > 0)
    seed_mask = np.zeros((wh, ww), dtype=bool)
    band = max(1, min(border_band, wh // 4, ww // 4))
    seed_mask[:band, :] |= bg_like[:band, :]
    seed_mask[-band:, :] |= bg_like[-band:, :]
    seed_mask[:, :band] |= bg_like[:, :band]
    seed_mask[:, -band:] |= bg_like[:, -band:]

    outside = _flood_via_dilation(seed_mask, bg_like)
    if scale < 1.0:
        outside = _upscale_bool_mask(outside, h, w)
        rgb = arr[:, :, :3].astype(np.float32)
    else:
        rgb = work_rgb

    if not outside.any():
        return im.convert("RGBA")

    out = _refine_matte(
        arr,
        rgb,
        outside,
        bg,
        color_tol=color_tol,
        bg_sat=bg_sat,
        feather=max(0, int(feather)),
        cleanup=cleanup,
    )
    return Image.fromarray(out, mode="RGBA")


def seed_matte_to_alpha(
    im: Image.Image,
    seed_x: int,
    seed_y: int,
    *,
    color_tol: float = 34.0,
    step_tol: float = 16.0,
    feather: int = 1,
    cleanup: bool = True,
) -> Image.Image:
    arr = np.array(im.convert("RGBA"))
    h, w = arr.shape[:2]
    if h == 0 or w == 0:
        return im.convert("RGBA")

    rgb = arr[:, :, :3].astype(np.float32)
    sx = int(np.clip(seed_x, 0, w - 1))
    sy = int(np.clip(seed_y, 0, h - 1))
    bg = rgb[sy, sx].copy()
    bg_sat = _pixel_saturation(bg)
    outside = _flood_transparent_mask(
        arr, rgb, seed_x, seed_y, color_tol=color_tol, step_tol=step_tol
    )
    if not outside.any():
        return im.convert("RGBA")

    out = _refine_matte(
        arr,
        rgb,
        outside,
        bg,
        color_tol=color_tol,
        bg_sat=bg_sat,
        feather=max(0, int(feather)),
        cleanup=cleanup,
    )
    return Image.fromarray(out, mode="RGBA")


_BRUSH_OFFSET_CACHE: dict[int, list[tuple[int, int]]] = {}


def _brush_disk_offsets(radius: int) -> list[tuple[int, int]]:
    radius = max(0, int(radius))
    if radius == 0:
        return [(0, 0)]
    cached = _BRUSH_OFFSET_CACHE.get(radius)
    if cached is not None:
        return cached
    r2 = radius * radius
    offsets: list[tuple[int, int]] = []
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx * dx + dy * dy <= r2:
                offsets.append((dx, dy))
    _BRUSH_OFFSET_CACHE[radius] = offsets
    return offsets


def _clamp_brush_size(brush_size: int) -> int:
    return max(1, min(50, int(brush_size)))


def stroke_matte_to_alpha(
    im: Image.Image,
    points: list[tuple[int, int]],
    *,
    color_tol: float = 34.0,
    step_tol: float = 16.0,
    brush_size: int = 1,
    feather: int = 1,
    cleanup: bool = False,
) -> Image.Image:
    """沿笔画多点连续泛洪剔除（橡皮擦式点选）。"""
    if not points:
        return im.convert("RGBA")

    arr = np.array(im.convert("RGBA"))
    h, w = arr.shape[:2]
    if h == 0 or w == 0:
        return im.convert("RGBA")

    brush_size = _clamp_brush_size(brush_size)
    radius = max(0, (brush_size - 1) // 2)
    offsets = _brush_disk_offsets(radius)
    deduped = _dedupe_seed_points(points, grid=max(2, brush_size // 2))

    rgb = arr[:, :, :3].astype(np.float32)
    seeds = _collect_brush_seeds(deduped, offsets, h=h, w=w)
    outside, bg, bg_sat = _flood_from_seeds(
        arr, rgb, seeds, color_tol=color_tol, step_tol=step_tol, max_dim=512
    )

    if not outside.any() or bg is None:
        return im.convert("RGBA")

    out = _refine_matte(
        arr,
        rgb,
        outside,
        bg,
        color_tol=color_tol,
        bg_sat=bg_sat,
        feather=max(0, int(feather)) if cleanup else 0,
        cleanup=cleanup,
    )
    return Image.fromarray(out, mode="RGBA")


def _magenta_score(rgb: np.ndarray) -> np.ndarray:
    return np.minimum(rgb[..., 0], rgb[..., 2]) - rgb[..., 1]


def _despill_magenta_rgb(rgb: np.ndarray, strength: float = 1.0) -> np.ndarray:
    out = rgb.copy()
    score = _magenta_score(out)
    mask = score > 6
    if not mask.any():
        return out
    reduce = score * 0.94 * strength
    out[..., 0] = np.where(mask, np.maximum(0.0, out[..., 0] - reduce), out[..., 0])
    out[..., 2] = np.where(mask, np.maximum(0.0, out[..., 2] - reduce), out[..., 2])
    cap = out[..., 1] + np.maximum(10.0, score * 0.18)
    out[..., 0] = np.where(mask, np.minimum(out[..., 0], cap), out[..., 0])
    out[..., 2] = np.where(mask, np.minimum(out[..., 2], cap), out[..., 2])
    return out


def _spill_alpha_cut(alpha: np.ndarray, score: np.ndarray, key_score: float, strength: float = 1.0) -> np.ndarray:
    ratio = np.zeros_like(score)
    if key_score > 0:
        ratio = np.maximum(0.0, score / key_score)
    cut = np.minimum(1.0, ratio * 1.35 * strength)
    cut = np.where(score <= 8, 0.0, cut)
    cut = np.where(ratio <= 0.06, 0.0, cut)
    return np.round(alpha * (1.0 - cut))


def chroma_key_to_alpha(
    im: Image.Image,
    *,
    key_rgb: tuple[int, int, int] = (255, 0, 255),
    fuzz: float = 22.0,
    feather: int = 2,
) -> Image.Image:
    """色键去底：全图剔除与键色相近的像素（含镂空），fuzz 为 0–100 百分比。"""
    arr = np.array(im.convert("RGBA"))
    h, w = arr.shape[:2]
    if h == 0 or w == 0:
        return im.convert("RGBA")

    kr, kg, kb = (int(key_rgb[0]), int(key_rgb[1]), int(key_rgb[2]))
    rgb = arr[:, :, :3].astype(np.float32)
    alpha = arr[:, :, 3].astype(np.float32)

    max_dist = float(255.0 * np.sqrt(3.0))
    threshold = (max(0.0, min(100.0, fuzz)) / 100.0) * max_dist * 1.12
    soft = max(6.0, threshold * 0.42)
    magenta_key = kr > 200 and kb > 200 and kg < 90
    key_score = float(min(kr, kb) - kg) if magenta_key else 0.0
    spill_min = max(8.0, 22.0 - fuzz * 0.08)

    diff = np.linalg.norm(rgb - np.array([kr, kg, kb], dtype=np.float32), axis=2)
    if magenta_key:
        score = _magenta_score(rgb)
        mag_dist = np.abs(score - key_score) * 1.6 + np.abs(rgb[:, :, 0] - rgb[:, :, 2]) * 0.12
        diff = np.minimum(diff, mag_dist)

    opaque = alpha > 0
    hard = opaque & (diff <= threshold - soft)
    soft_band = opaque & (diff > threshold - soft) & (diff < threshold)
    alpha[hard] = 0.0
    if soft_band.any():
        t = (diff[soft_band] - (threshold - soft)) / soft
        alpha[soft_band] = alpha[soft_band] * t

    if magenta_key:
        score = _magenta_score(rgb)
        spill_mask = (alpha > 0) & (score > spill_min)
        if spill_mask.any():
            alpha[spill_mask] = _spill_alpha_cut(alpha[spill_mask], score[spill_mask], key_score, 0.85)
            rgb = _despill_magenta_rgb(rgb, 0.75)

        pass_count = max(2, int(feather) + 1)
        for p in range(pass_count):
            a_u8 = np.clip(alpha, 0, 255).astype(np.uint8)
            trans = a_u8 < 16
            neighbor = np.zeros_like(trans)
            neighbor[1:, :] |= trans[:-1, :]
            neighbor[:-1, :] |= trans[1:, :]
            neighbor[:, 1:] |= trans[:, :-1]
            neighbor[:, :-1] |= trans[:, 1:]
            if p > 0:
                neighbor[1:, 1:] |= trans[:-1, :-1]
                neighbor[:-1, :-1] |= trans[1:, 1:]
                neighbor[1:, :-1] |= trans[:-1, 1:]
                neighbor[:-1, 1:] |= trans[1:, :-1]
            edge = (a_u8 >= 4) & neighbor
            score = _magenta_score(rgb)
            strength = np.where(edge, 1.15, 0.72)
            spill_thr = np.where(edge, spill_min * 0.55, spill_min * 0.85)
            dmask = (a_u8 >= 4) & (score > spill_thr)
            if dmask.any():
                rgb[dmask] = _despill_magenta_rgb(rgb[dmask], strength[dmask])
            edge_spill = edge & (score > spill_min * 0.45)
            if edge_spill.any():
                na = _spill_alpha_cut(alpha[edge_spill], score[edge_spill], key_score, 1.1)
                ys, xs = np.where(edge_spill)
                hard_e = (score[edge_spill] > spill_min * 1.35) | (na < 12)
                if hard_e.any():
                    alpha[ys[hard_e], xs[hard_e]] = 0.0
                soft_e = ~hard_e
                if soft_e.any():
                    alpha[ys[soft_e], xs[soft_e]] = np.minimum(alpha[ys[soft_e], xs[soft_e]], na[soft_e])
                    cap = score[ys[soft_e], xs[soft_e]] > spill_min
                    if cap.any():
                        idx = np.where(soft_e)[0]
                        cap_idx = idx[cap]
                        alpha[ys[cap_idx], xs[cap_idx]] = np.minimum(
                            alpha[ys[cap_idx], xs[cap_idx]], 48.0
                        )
    elif feather > 0 and (alpha == 0).any():
        soft_thr = threshold * 0.85
        for _ in range(min(2, int(feather))):
            a_u8 = np.clip(alpha, 0, 255).astype(np.uint8)
            trans = a_u8 < 16
            neighbor = np.zeros_like(trans)
            neighbor[1:, :] |= trans[:-1, :]
            neighbor[:-1, :] |= trans[1:, :]
            neighbor[:, 1:] |= trans[:, :-1]
            neighbor[:, :-1] |= trans[:, 1:]
            edge = (a_u8 >= 8) & neighbor & (diff <= soft_thr)
            if not edge.any():
                break
            ys, xs = np.where(edge)
            px = rgb[ys, xs]
            d = np.linalg.norm(px - np.array([kr, kg, kb], dtype=np.float32), axis=1)
            hard_e = d < threshold * 0.55
            alpha[ys[hard_e], xs[hard_e]] = 0.0
            soft_e = ~hard_e
            if soft_e.any():
                alpha[ys[soft_e], xs[soft_e]] = np.minimum(
                    alpha[ys[soft_e], xs[soft_e]], 48.0
                )

    out = arr.copy()
    out[:, :, :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    out[:, :, 3] = np.clip(alpha, 0, 255).astype(np.uint8)
    return Image.fromarray(out, mode="RGBA")


def magenta_key_to_alpha(
    im: Image.Image,
    *,
    fuzz: float = 22.0,
    feather: int = 2,
) -> Image.Image:
    return chroma_key_to_alpha(im, key_rgb=(255, 0, 255), fuzz=fuzz, feather=feather)


def apply_alpha_matte_png(data: bytes, *, mode: str = "border") -> bytes:
    if mode == "none":
        return data
    im = Image.open(io.BytesIO(data))
    mode_l = mode.strip().lower()
    if mode_l == "border":
        im = border_matte_to_alpha(im)
    elif mode_l in ("magenta", "chroma"):
        im = magenta_key_to_alpha(im)
    else:
        im = im.convert("RGBA")
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
