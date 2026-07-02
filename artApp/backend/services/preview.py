#!/usr/bin/env python3
"""预览图生成（后台线程安全，纯 PIL）。"""

from __future__ import annotations

import io
from collections import OrderedDict
from pathlib import Path
from typing import Any, Literal

from config_manager import Asset, ConfigManager

PreviewSource = Literal["inbox", "source", "unity"]

PREVIEW_MAX = 440
_PREVIEW_CACHE_MAX = 64


class _PreviewCache:
    def __init__(self, max_items: int = _PREVIEW_CACHE_MAX) -> None:
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


_PREVIEW_CACHE = _PreviewCache()


def invalidate_preview_cache() -> None:
    _PREVIEW_CACHE._data.clear()


def resolve_path_for_source(
    config: ConfigManager,
    asset: Asset,
    source: PreviewSource,
) -> Path | None:
    src, inbox, unity = config.resolve_paths(asset)
    return {"source": src, "inbox": inbox, "unity": unity}.get(source)


def resolve_preview_file(
    config: ConfigManager,
    asset: Asset,
    source: PreviewSource,
) -> Path:
    """按预览来源解析文件；source/unity 不存在时不回退到其他路径。"""
    src, inbox, unity = config.resolve_paths(asset)
    primary = resolve_path_for_source(config, asset, source)
    if primary is not None:
        try:
            if primary.is_file():
                return primary
        except OSError:
            pass
    if source == "inbox":
        fallback = first_existing_path(inbox, src, unity)
        if fallback is not None:
            return fallback
    raise FileNotFoundError(f"无 {source} 文件")


def first_existing_path(*candidates: Path | None) -> Path | None:
    for path in candidates:
        if path is None:
            continue
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def category_remove_bg_default(config: ConfigManager, cat_id: str) -> bool:
    cat = config.category_by_id(cat_id)
    if not cat:
        return False
    return cat.alpha_matte.strip().lower() not in ("", "none")


def should_remove_bg(config: ConfigManager, asset: Asset) -> bool:
    from config_manager import REMOVE_BG_INHERIT, REMOVE_BG_KEEP, REMOVE_BG_REMOVE

    mode = asset.remove_bg_mode
    if mode == REMOVE_BG_REMOVE:
        return True
    if mode == REMOVE_BG_KEEP:
        return False
    return category_remove_bg_default(config, asset.category)


def _preview_cache_key(path: Path, mtime_ns: int, max_size: int, remove_bg: bool) -> tuple:
    return (str(path), mtime_ns, int(max_size), bool(remove_bg))


def _load_resized_rgba(path: Path, cap: int) -> Any:
    """尽量在解码阶段缩小，避免大图全尺寸解码。"""
    from PIL import Image

    cap = max(64, min(int(cap), 2048))
    with Image.open(path) as raw:
        w, h = raw.size
        if max(w, h) > cap:
            scale = cap / max(w, h)
            tw = max(1, int(w * scale))
            th = max(1, int(h * scale))
            try:
                raw.draft(raw.mode, (tw, th))
            except Exception:
                pass
            raw.load()
            im = raw.convert("RGBA")
            if max(im.size) > cap:
                im.thumbnail((tw, th), Image.Resampling.BILINEAR)
        else:
            raw.load()
            im = raw.convert("RGBA")
    return im


def build_preview_rgba(
    config: ConfigManager,
    asset: Asset,
    *,
    source: PreviewSource = "inbox",
    max_size: int = PREVIEW_MAX,
) -> Any:
    path = resolve_preview_file(config, asset, source)
    cap = max(64, min(int(max_size), 2048))
    im = _load_resized_rgba(path, cap)

    if should_remove_bg(config, asset):
        try:
            from alpha_matte import border_matte_to_alpha

            im = border_matte_to_alpha(im)
        except ImportError:
            pass

    return im


def preview_png_bytes(
    config: ConfigManager,
    asset: Asset,
    *,
    source: PreviewSource = "inbox",
    max_size: int = PREVIEW_MAX,
) -> bytes:
    path = resolve_preview_file(config, asset, source=source)
    cap = max(64, min(int(max_size), 2048))
    remove_bg = should_remove_bg(config, asset)

    try:
        st = path.stat()
        mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
    except OSError as exc:
        raise FileNotFoundError(f"无法读取: {path}") from exc

    cache_key = _preview_cache_key(path, mtime_ns, cap, remove_bg)
    cached = _PREVIEW_CACHE.get(cache_key)
    if cached is not None:
        return cached

    if not remove_bg and path.suffix.lower() == ".png":
        try:
            from PIL import Image

            with Image.open(path) as probe:
                w, h = probe.size
            if max(w, h) <= cap:
                data = path.read_bytes()
                if len(data) >= 8:
                    _PREVIEW_CACHE.set(cache_key, data)
                    return data
        except OSError:
            pass

    im = build_preview_rgba(config, asset, source=source, max_size=max_size)
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=False, compress_level=1)
    data = buf.getvalue()
    _PREVIEW_CACHE.set(cache_key, data)
    return data


def preview_etag(path: Path, max_size: int, remove_bg: bool) -> str | None:
    try:
        st = path.stat()
        mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
    except OSError:
        return None
    return f'"{mtime_ns:x}-{int(max_size)}-{int(remove_bg)}"'
