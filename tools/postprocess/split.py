"""智能拆分：按 alpha 连通域将图集拆为独立图层。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import uuid

import numpy as np
from PIL import Image

from postprocess.engine import alpha_tight_bbox, render_stack
from postprocess.models import ASSET_SUBJECT_SOURCE, Layer, LayerStack, LayerTransform


def _binary_erode(mask: np.ndarray, iterations: int = 1) -> np.ndarray:
    out = mask.astype(bool)
    for _ in range(max(0, int(iterations))):
        up = np.zeros_like(out)
        up[1:, :] = out[:-1, :]
        down = np.zeros_like(out)
        down[:-1, :] = out[1:, :]
        left = np.zeros_like(out)
        left[:, 1:] = out[:, :-1]
        right = np.zeros_like(out)
        right[:, :-1] = out[:, 1:]
        out = out & up & down & left & right
    return out


def _estimate_border_bg_color(rgb: np.ndarray, border_band: int = 4) -> np.ndarray:
    h, w = rgb.shape[:2]
    band = max(1, min(border_band, h // 4, w // 4))
    edges = np.concatenate(
        [
            rgb[:band, :].reshape(-1, 3),
            rgb[-band:, :].reshape(-1, 3),
            rgb[:, :band].reshape(-1, 3),
            rgb[:, -band:].reshape(-1, 3),
        ],
        axis=0,
    )
    return np.median(edges, axis=0)


def _flood_from_border(bg_like: np.ndarray) -> np.ndarray:
    """从四边出发洪泛 bg_like 区域。"""
    h, w = bg_like.shape
    if h == 0 or w == 0:
        return np.zeros_like(bg_like, dtype=bool)
    outside = np.zeros((h, w), dtype=bool)
    stack: list[tuple[int, int]] = []
    for x in range(w):
        if bg_like[0, x]:
            stack.append((x, 0))
        if h > 1 and bg_like[h - 1, x]:
            stack.append((x, h - 1))
    for y in range(h):
        if bg_like[y, 0]:
            stack.append((0, y))
        if w > 1 and bg_like[y, w - 1]:
            stack.append((w - 1, y))
    while stack:
        x, y = stack.pop()
        if outside[y, x] or not bg_like[y, x]:
            continue
        outside[y, x] = True
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and bg_like[ny, nx] and not outside[ny, nx]:
                stack.append((nx, ny))
    return outside


def _exclude_sheet_background(rgba: np.ndarray, alpha_threshold: int) -> np.ndarray:
    """剔除不透明底图，保留前景 alpha 掩码。"""
    alpha = rgba[:, :, 3]
    rgb = rgba[:, :, :3].astype(np.float32)
    fg = alpha >= max(1, min(255, int(alpha_threshold)))

    h, w = alpha.shape
    corner_alpha = np.array([alpha[0, 0], alpha[0, w - 1], alpha[h - 1, 0], alpha[h - 1, w - 1]], dtype=np.float32)
    if float(np.median(corner_alpha)) < alpha_threshold * 0.85:
        return fg

    bg = _estimate_border_bg_color(rgb)
    color_tol = 34.0
    diff = np.linalg.norm(rgb - bg.reshape(1, 1, 3), axis=2)
    bg_like = (diff <= color_tol) & (alpha > 0)
    outside = _flood_from_border(bg_like)
    if not outside.any():
        return fg
    return fg & ~outside


def _connected_components(mask: np.ndarray, *, min_area: int = 64) -> list[tuple[int, int, int, int]]:
    """4-连通域 bbox: (x0, y0, x1, y1) 开区间右下。"""
    if mask.ndim != 2:
        return []
    h, w = mask.shape
    visited = np.zeros((h, w), dtype=bool)
    boxes: list[tuple[int, int, int, int]] = []
    min_a = max(1, int(min_area))

    for y0 in range(h):
        row_mask = mask[y0]
        row_vis = visited[y0]
        for x0 in range(w):
            if not row_mask[x0] or row_vis[x0]:
                continue
            stack: list[tuple[int, int]] = [(x0, y0)]
            visited[y0, x0] = True
            min_x = max_x = x0
            min_y = max_y = y0
            area = 0
            while stack:
                x, y = stack.pop()
                area += 1
                if x < min_x:
                    min_x = x
                if x > max_x:
                    max_x = x
                if y < min_y:
                    min_y = y
                if y > max_y:
                    max_y = y
                if y > 0:
                    ny = y - 1
                    if mask[ny, x] and not visited[ny, x]:
                        visited[ny, x] = True
                        stack.append((x, ny))
                if y + 1 < h:
                    ny = y + 1
                    if mask[ny, x] and not visited[ny, x]:
                        visited[ny, x] = True
                        stack.append((x, ny))
                if x > 0:
                    nx = x - 1
                    if mask[y, nx] and not visited[y, nx]:
                        visited[y, nx] = True
                        stack.append((nx, y))
                if x + 1 < w:
                    nx = x + 1
                    if mask[y, nx] and not visited[y, nx]:
                        visited[y, nx] = True
                        stack.append((nx, y))
            if area >= min_a:
                boxes.append((min_x, min_y, max_x + 1, max_y + 1))
    boxes.sort(key=lambda b: (b[1], b[0]))
    return boxes


def _projection_bands(proj: np.ndarray, *, min_gap: int = 2) -> list[tuple[int, int]]:
    """按投影找内容带；短间隙（≤min_gap）视为同一带。"""
    n = int(proj.shape[0])
    if n == 0:
        return []
    active = proj > 0
    bands: list[tuple[int, int]] = []
    i = 0
    min_gap = max(1, int(min_gap))
    while i < n:
        while i < n and not active[i]:
            i += 1
        if i >= n:
            break
        start = i
        while i < n:
            if active[i]:
                i += 1
                continue
            gap_start = i
            while i < n and not active[i]:
                i += 1
            if i - gap_start >= min_gap:
                i = gap_start
                break
        bands.append((start, i))
    return bands


def _split_by_projection(mask: np.ndarray, *, min_area: int = 64, min_gap: int = 2) -> list[tuple[int, int, int, int]]:
    """规则网格图集：按行列投影切分。"""
    h, w = mask.shape
    if h < 2 or w < 2:
        return []
    row_bands = _projection_bands(mask.sum(axis=1), min_gap=min_gap)
    col_bands = _projection_bands(mask.sum(axis=0), min_gap=min_gap)
    if len(row_bands) < 2 and len(col_bands) < 2:
        return []
    boxes: list[tuple[int, int, int, int]] = []
    min_a = max(1, int(min_area))
    for y0, y1 in row_bands:
        for x0, x1 in col_bands:
            sub = mask[y0:y1, x0:x1]
            if int(sub.sum()) < min_a:
                continue
            ys, xs = np.where(sub)
            if ys.size == 0:
                continue
            bx0 = int(xs.min()) + x0
            bx1 = int(xs.max()) + x0 + 1
            by0 = int(ys.min()) + y0
            by1 = int(ys.max()) + y0 + 1
            if (bx1 - bx0) * (by1 - by0) >= min_a:
                boxes.append((bx0, by0, bx1, by1))
    boxes.sort(key=lambda b: (b[1], b[0]))
    return boxes


def _mask_coverage(mask: np.ndarray) -> float:
    total = mask.size
    if total <= 0:
        return 0.0
    return float(mask.sum()) / float(total)


def _is_near_full_canvas_box(
    box: tuple[int, int, int, int],
    canvas_w: int,
    canvas_h: int,
    *,
    margin: int = 2,
) -> bool:
    x0, y0, x1, y1 = box
    return x0 <= margin and y0 <= margin and x1 >= canvas_w - margin and y1 >= canvas_h - margin


def _score_boxes(
    boxes: list[tuple[int, int, int, int]],
    mask: np.ndarray,
    canvas_w: int,
    canvas_h: int,
) -> float:
    n = len(boxes)
    if n == 0:
        return -1e9
    if n == 1 and _is_near_full_canvas_box(boxes[0], canvas_w, canvas_h):
        return -5e8
    coverage = _mask_coverage(mask)
    # 偏好 2~120 个合理切分；过多可能是噪点
    if n == 1:
        count_score = -120.0 if coverage > 0.55 else 40.0
    elif 2 <= n <= 120:
        count_score = 200.0 + min(n, 40) * 2.0
    else:
        count_score = 80.0 - (n - 120) * 3.0
    # 略惩罚极小碎片占比（由 min_area 已滤，此处轻量加权）
    areas = [(b[2] - b[0]) * (b[3] - b[1]) for b in boxes]
    median_area = float(np.median(areas)) if areas else 0.0
    small_penalty = sum(1 for a in areas if a < median_area * 0.08) * 4.0
    return count_score - small_penalty


def _find_split_boxes(
    rgba: np.ndarray,
    *,
    alpha_threshold: int = 8,
    min_area: int = 64,
) -> tuple[list[tuple[int, int, int, int]], dict[str, Any]]:
    """多策略尝试，返回最佳 bbox 列表与 meta。"""
    h, w = rgba.shape[:2]
    strategies: list[dict[str, Any]] = [
        {"alpha_threshold": alpha_threshold, "min_area": min_area, "erode": 0, "bg_remove": True, "projection": False},
        {"alpha_threshold": max(4, alpha_threshold // 2), "min_area": max(16, min_area // 2), "erode": 0, "bg_remove": True, "projection": False},
        {"alpha_threshold": alpha_threshold, "min_area": min_area, "erode": 1, "bg_remove": True, "projection": False},
        {"alpha_threshold": min(32, alpha_threshold * 2), "min_area": min_area, "erode": 1, "bg_remove": True, "projection": False},
        {"alpha_threshold": alpha_threshold, "min_area": max(16, min_area // 2), "erode": 0, "bg_remove": False, "projection": False},
        {"alpha_threshold": max(4, alpha_threshold // 2), "min_area": max(16, min_area // 2), "erode": 1, "bg_remove": True, "projection": True},
        {"alpha_threshold": alpha_threshold, "min_area": min_area, "erode": 0, "bg_remove": True, "projection": True},
    ]

    best_boxes: list[tuple[int, int, int, int]] = []
    best_meta: dict[str, Any] = {}
    best_score = -1e18

    for strat in strategies:
        ath = int(strat["alpha_threshold"])
        ma = int(strat["min_area"])
        mask = _exclude_sheet_background(rgba, ath) if strat["bg_remove"] else (rgba[:, :, 3] >= ath)
        if int(strat.get("erode") or 0) > 0:
            cc_mask = _binary_erode(mask, int(strat["erode"]))
        else:
            cc_mask = mask

        boxes = _connected_components(cc_mask, min_area=ma)
        used_projection = False
        if strat.get("projection") and len(boxes) < 2:
            proj_boxes = _split_by_projection(mask, min_area=ma, min_gap=max(2, min(w, h) // 256))
            if len(proj_boxes) > len(boxes):
                boxes = proj_boxes
                used_projection = True

        score = _score_boxes(boxes, mask, w, h)
        if score > best_score:
            best_score = score
            best_boxes = boxes
            best_meta = {
                "alpha_threshold": ath,
                "min_area": ma,
                "erode": int(strat.get("erode") or 0),
                "bg_remove": bool(strat["bg_remove"]),
                "projection": used_projection,
                "strategy_score": score,
            }

    return best_boxes, best_meta


def _canvas_offset_for_tile(
    stack: LayerStack,
    x0: int,
    y0: int,
    tw: int,
    th: int,
) -> tuple[float, float]:
    """center anchor：使 tight 图块左上角落在画布 (x0, y0)。"""
    cw, ch = stack.canvas_width, stack.canvas_height
    return float(x0 + tw / 2 - cw / 2), float(y0 + th / 2 - ch / 2)


def smart_split_subject_layer(
    stack: LayerStack,
    resolver: Any,
    *,
    layers_dir: Path,
    alpha_threshold: int = 8,
    min_area: int = 64,
    hide_subject: bool = True,
) -> tuple[list[Layer], dict[str, Any]]:
    """将主体层按连通域拆成多个图片层；返回 (新图层列表, meta)。"""
    subj = stack.subject_layer()
    if not subj or subj.type != "image":
        raise ValueError("未找到主体图片层")
    if subj.locked:
        raise ValueError("主体图层已锁定")

    solo = Image.new("RGBA", (stack.canvas_width, stack.canvas_height), (0, 0, 0, 0))
    rendered = render_stack(stack, resolver, solo_layer_id=subj.id)
    solo.paste(rendered, (0, 0), rendered)
    rgba = np.array(solo.convert("RGBA"))
    boxes, split_meta = _find_split_boxes(
        rgba,
        alpha_threshold=alpha_threshold,
        min_area=min_area,
    )

    if not boxes:
        raise ValueError(
            "未检测到可拆分的独立图标。请确认图集为透明底或已去色，且各图标之间有明显间隙。"
        )

    cw, ch = solo.width, solo.height
    if len(boxes) == 1 and _is_near_full_canvas_box(boxes[0], cw, ch):
        raise ValueError(
            "仅检测到一整块连通区域，无法拆分。可尝试：先去色/抠图去掉底色，或确保图标之间留有透明间隙。"
        )

    layers_dir.mkdir(parents=True, exist_ok=True)
    new_layers: list[Layer] = []
    used_names: dict[str, int] = {}

    for idx, (x0, y0, x1, y1) in enumerate(boxes, start=1):
        tile = solo.crop((x0, y0, x1, y1))
        tw, th = tile.size
        if tw < 1 or th < 1:
            continue
        file_id = uuid.uuid4().hex[:10]
        rel_path = f"postprocess/layers/{file_id}.png"
        out_path = layers_dir / f"{file_id}.png"
        out_path.write_bytes(_tile_png_bytes(tile))

        base = f"image{idx:02d}"
        count = used_names.get(base, 0)
        used_names[base] = count + 1
        name = base if count == 0 else f"{base}_{count + 1}"
        ox, oy = _canvas_offset_for_tile(stack, x0, y0, tw, th)
        new_layers.append(
            Layer(
                id=f"i_{file_id[:8]}",
                name=name,
                type="image",
                visible=True,
                opacity=subj.opacity,
                blend_mode=subj.blend_mode,
                blend_color=subj.blend_color,
                blend_amount=subj.blend_amount,
                blend_enabled=subj.blend_enabled,
                source=rel_path,
                transform=LayerTransform(
                    offset_x=ox,
                    offset_y=oy,
                    scale=1.0,
                    anchor="center",
                ),
                is_subject=False,
            )
        )

    if not new_layers:
        raise ValueError("拆分结果为空（连通域过小，可调低最小面积后重试）")

    meta: dict[str, Any] = {
        "count": len(new_layers),
        "regions": [{"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0} for x0, y0, x1, y1 in boxes],
        **split_meta,
    }

    if hide_subject:
        subj.visible = False
        subj.is_subject = False
        if subj.source == ASSET_SUBJECT_SOURCE:
            subj.name = f"{subj.name or '主体'}（已拆分）"

    return new_layers, meta


def _tile_png_bytes(tile: Image.Image) -> bytes:
    import io

    buf = io.BytesIO()
    tile.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
