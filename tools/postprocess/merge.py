"""合并多个图层为单个图片图层。"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import uuid

from PIL import Image

from postprocess.engine import render_layers_subset
from postprocess.models import Layer, LayerStack, LayerTransform
from postprocess.split import _canvas_offset_for_tile


def _png_bytes(im: Image.Image) -> bytes:
    buf = BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def merge_stack_layers(
    stack: LayerStack,
    resolver,
    layer_ids: list[str],
    *,
    layers_dir: Path,
    tight: bool = True,
    merged_name: str | None = None,
) -> Layer:
    """将多个图层合并为一个图片图层，并就地更新 stack.layers。"""
    id_set = {str(lid) for lid in layer_ids if lid}
    selected = [layer for layer in stack.layers if layer.id in id_set]
    if len(selected) < 2:
        raise ValueError("请至少选择两个图层")
    if any(layer.locked for layer in selected):
        raise ValueError("包含已锁定图层，无法合并")

    indices = [stack.layers.index(layer) for layer in selected]
    insert_at = min(indices)

    merged_im = render_layers_subset(stack, resolver, id_set)
    offset_x = 0.0
    offset_y = 0.0

    if tight:
        bbox = merged_im.getbbox()
        if bbox:
            x0, y0, x1, y1 = bbox
            merged_im = merged_im.crop(bbox)
            tw, th = merged_im.size
            offset_x, offset_y = _canvas_offset_for_tile(stack, x0, y0, tw, th)
    elif merged_im.getbbox() is None:
        raise ValueError("合并结果为空")

    layers_dir.mkdir(parents=True, exist_ok=True)
    file_id = uuid.uuid4().hex[:10]
    rel_path = f"postprocess/layers/{file_id}.png"
    (layers_dir / f"{file_id}.png").write_bytes(_png_bytes(merged_im))

    base_name = (selected[0].name or selected[0].id or "layer").strip()
    name = (merged_name or f"{base_name}_merged")[:80] or "merged"

    new_layer = Layer(
        id=f"i_{file_id[:8]}",
        name=name,
        type="image",
        visible=True,
        opacity=1.0,
        blend_mode="normal",
        blend_color="",
        blend_amount=1.0,
        blend_enabled=False,
        source=rel_path,
        transform=LayerTransform(offset_x=offset_x, offset_y=offset_y),
        is_subject=False,
    )

    stack.layers = [layer for layer in stack.layers if layer.id not in id_set]
    stack.layers.insert(insert_at, new_layer)
    return new_layer
