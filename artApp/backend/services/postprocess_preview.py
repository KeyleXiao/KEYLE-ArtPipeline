#!/usr/bin/env python3
"""后处理编辑器预览 PNG 缓存与编码。"""

from __future__ import annotations

import hashlib
import io
import json
from collections import OrderedDict
from typing import Any

_PP_PREVIEW_CACHE_MAX = 32


class _PostprocessPreviewCache:
    def __init__(self, max_items: int = _PP_PREVIEW_CACHE_MAX) -> None:
        self._max = max_items
        self._data: OrderedDict[tuple, bytes] = OrderedDict()

    def get(self, key: tuple) -> bytes | None:
        hit = self._data.get(key)
        if hit is None:
            return None
        self._data.move_to_end(key)
        return hit

    def set(self, key: tuple, value: bytes) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()


_PP_PREVIEW_CACHE = _PostprocessPreviewCache()


def invalidate_postprocess_preview_cache() -> None:
    _PP_PREVIEW_CACHE.clear()


def _path_mtime_ns(path: Any) -> int:
    try:
        st = path.stat()
        return int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
    except OSError:
        return 0


def postprocess_preview_cache_key(
    asset_id: str,
    stack: Any,
    body: dict[str, Any],
    *,
    config: Any,
    asset: Any,
    inbox: Any,
    asset_source: Any = None,
    asset_unity: Any = None,
) -> tuple:
    """栈 JSON + 图层文件 mtime，用于命中预览缓存。"""
    from postprocess.matte import resolve_layer_image_path
    from postprocess.models import layer_image_source, stack_to_dict

    subject_path = body.get("subject_path")
    solo = body.get("solo_layer_id") or ""
    stack_digest = hashlib.sha256(
        json.dumps(stack_to_dict(stack), sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:20]

    layer_mt: list[tuple[str, str, int]] = []
    for layer in stack.layers:
        if layer.type != "image":
            continue
        key = layer_image_source(layer)
        if not key:
            continue
        path = resolve_layer_image_path(
            art_root=config.art_root(),
            layer=layer,
            inbox_path=inbox,
            asset_source=asset_source,
            asset_unity=asset_unity,
            subject_path=subject_path,
        )
        if path and path.is_file():
            layer_mt.append((layer.id, str(path), _path_mtime_ns(path)))

    return (
        asset_id,
        str(subject_path or "inbox"),
        str(solo),
        int(stack.canvas_width),
        int(stack.canvas_height),
        stack_digest,
        tuple(layer_mt),
    )


def render_postprocess_preview_png(
    stack: Any,
    resolver: Any,
    *,
    solo_layer_id: str | None = None,
) -> bytes:
    from postprocess.engine import render_stack, stack_checkerboard

    doc = render_stack(stack, resolver, solo_layer_id=solo_layer_id)
    bg = stack_checkerboard(stack.canvas_width, stack.canvas_height)
    bg.alpha_composite(doc)
    buf = io.BytesIO()
    bg.save(buf, format="PNG", optimize=False, compress_level=1)
    return buf.getvalue()


def cached_postprocess_preview_png(
    cache_key: tuple,
    stack: Any,
    resolver: Any,
    *,
    solo_layer_id: str | None = None,
) -> bytes:
    cached = _PP_PREVIEW_CACHE.get(cache_key)
    if cached is not None:
        return cached
    data = render_postprocess_preview_png(stack, resolver, solo_layer_id=solo_layer_id)
    _PP_PREVIEW_CACHE.set(cache_key, data)
    return data
