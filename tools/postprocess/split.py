"""智能拆分：按 alpha 连通域将图集拆为独立图层。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import uuid

import numpy as np
from PIL import Image

from postprocess.engine import alpha_tight_bbox, render_stack
from postprocess.models import ASSET_SUBJECT_SOURCE, Layer, LayerStack, LayerTransform


def _find_alpha_components(
    rgba: np.ndarray,
    *,
    alpha_threshold: int = 8,
    min_area: int = 64,
) -> list[tuple[int, int, int, int]]:
    """返回各连通域 bbox: (x0, y0, x1, y1)，右下为开区间。"""
    if rgba.ndim != 3 or rgba.shape[2] < 4:
        return []
    h, w = rgba.shape[:2]
    alpha = rgba[:, :, 3]
    mask = alpha >= max(1, min(255, int(alpha_threshold)))
    visited = np.zeros((h, w), dtype=bool)
    boxes: list[tuple[int, int, int, int]] = []

    for y0 in range(h):
        for x0 in range(w):
            if not mask[y0, x0] or visited[y0, x0]:
                continue
            stack: list[tuple[int, int]] = [(x0, y0)]
            visited[y0, x0] = True
            min_x = max_x = x0
            min_y = max_y = y0
            area = 0
            while stack:
                x, y = stack.pop()
                area += 1
                min_x = min(min_x, x)
                max_x = max(max_x, x)
                min_y = min(min_y, y)
                max_y = max(max_y, y)
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    nx, ny = x + dx, y + dy
                    if nx < 0 or ny < 0 or nx >= w or ny >= h:
                        continue
                    if mask[ny, nx] and not visited[ny, nx]:
                        visited[ny, nx] = True
                        stack.append((nx, ny))
            if area >= max(1, int(min_area)):
                boxes.append((min_x, min_y, max_x + 1, max_y + 1))
    boxes.sort(key=lambda b: (b[1], b[0]))
    return boxes


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
    boxes = _find_alpha_components(rgba, alpha_threshold=alpha_threshold, min_area=min_area)
    if not boxes:
        raise ValueError("未检测到可拆分的独立图标（请先去色或检查透明度）")
    if len(boxes) == 1:
        bbox = alpha_tight_bbox(solo)
        if bbox and bbox[2] - bbox[0] >= solo.width - 2 and bbox[3] - bbox[1] >= solo.height - 2:
            raise ValueError("仅检测到一整块区域，无需拆分")

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
        raise ValueError("拆分结果为空")

    meta: dict[str, Any] = {
        "count": len(new_layers),
        "regions": [{"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0} for x0, y0, x1, y1 in boxes],
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
