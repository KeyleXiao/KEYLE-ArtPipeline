#!/usr/bin/env python3
"""后处理编辑器 · 图层抠图（写回 PNG）。"""

from __future__ import annotations

import io
import shutil
from pathlib import Path
from typing import Any

from alpha_matte import (
    border_matte_to_alpha,
    polish_matte_alpha,
    seed_matte_to_alpha,
    stroke_matte_to_alpha,
)
from postprocess.models import ASSET_SUBJECT_SOURCE, Layer, layer_image_source


def is_asset_source_path(path: Path | None, asset_source: Path | None) -> bool:
    if path is None or asset_source is None or not asset_source.is_file():
        return False
    try:
        return path.resolve() == asset_source.resolve()
    except OSError:
        return False


def is_under_source_tree(path: Path, art_root: Path) -> bool:
    try:
        root = (art_root / "source").resolve()
        return path.resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


def touches_source_master(
    path: Path | None,
    *,
    art_root: Path,
    asset_source: Path | None,
) -> bool:
    """写入路径是否属于 source 原图（含本资源 source 与同目录其它 source 文件）。"""
    if path is None or not path.is_file():
        return False
    if is_asset_source_path(path, asset_source):
        return True
    return is_under_source_tree(path, art_root)


def sync_asset_source_to_inbox(asset_source: Path, inbox: Path) -> bool:
    """将 source 原图复制到 inbox，便于主体 $asset 合成与「应用到 inbox」。"""
    if not asset_source.is_file():
        return False
    inbox.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(asset_source, inbox)
    return True


def normalize_edit_subject(subject_path: str | None) -> str:
    """后处理编辑目标：inbox / source / unity。"""
    key = (subject_path or "inbox").strip().lower()
    if key in ("source", "src"):
        return "source"
    if key in ("unity", "engine"):
        return "unity"
    return "inbox"


def is_subject_source_edit_mode(subject_path: str | None) -> bool:
    """后处理以 source 原图为主体编辑（$asset 读写均指向 source）。"""
    return normalize_edit_subject(subject_path) == "source"


def is_subject_unity_edit_mode(subject_path: str | None) -> bool:
    return normalize_edit_subject(subject_path) == "unity"


def is_subject_master_edit_mode(subject_path: str | None) -> bool:
    """直接编辑 source 或 unity 导出文件（非 inbox 副本）。"""
    return normalize_edit_subject(subject_path) in ("source", "unity")


def sync_inbox_if_source_newer(asset_source: Path | None, inbox: Path) -> bool:
    """apply 前兜底：source 不旧于 inbox 时同步（避免只改了 source 未写入 inbox）。"""
    if asset_source is None or not asset_source.is_file():
        return False
    if not inbox.is_file():
        return sync_asset_source_to_inbox(asset_source, inbox)
    try:
        if asset_source.stat().st_mtime <= inbox.stat().st_mtime:
            return False
    except OSError:
        return False
    return sync_asset_source_to_inbox(asset_source, inbox)


def resolve_layer_image_path(
    *,
    art_root: Path,
    layer: Layer,
    inbox_path: Path,
    asset_source: Path | None = None,
    asset_unity: Path | None = None,
    subject_path: str | None = None,
) -> Path | None:
    """返回可写的图层 PNG 路径。"""
    key = layer_image_source(layer)
    if not key:
        return None
    if key == ASSET_SUBJECT_SOURCE:
        mode = normalize_edit_subject(subject_path)
        if mode == "source" and asset_source and asset_source.is_file():
            return asset_source
        if mode == "unity" and asset_unity and asset_unity.is_file():
            return asset_unity
        if inbox_path.is_file():
            return inbox_path
        if asset_source and asset_source.is_file():
            return asset_source
        return None
    path = Path(key)
    if not path.is_absolute():
        path = art_root / key
    return path if path.is_file() else None


def apply_layer_matte(
    path: Path,
    *,
    mode: str,
    seed_x: int | None = None,
    seed_y: int | None = None,
    seed_points: list[tuple[int, int]] | None = None,
    color_tol: float = 34.0,
    step_tol: float = 16.0,
    feather: int = 0,
    brush_size: int = 1,
    finalize: bool = True,
) -> dict[str, Any]:
    from PIL import Image

    with Image.open(path) as im:
        im.load()
        rgba = im.convert("RGBA")
    refine_feather = max(0, int(feather))
    if refine_feather == 0:
        refine_feather = 1
    if mode == "border":
        out = border_matte_to_alpha(
            rgba,
            color_tol=color_tol,
            step_tol=step_tol,
            feather=refine_feather,
        )
    elif mode == "seed":
        if seed_x is None or seed_y is None:
            raise ValueError("seed 模式需要 seed_x / seed_y")
        out = seed_matte_to_alpha(
            rgba,
            int(seed_x),
            int(seed_y),
            color_tol=color_tol,
            step_tol=step_tol,
            feather=refine_feather,
        )
    elif mode == "stroke":
        pts = [(int(x), int(y)) for x, y in (seed_points or [])]
        out = rgba
        if pts:
            out = stroke_matte_to_alpha(
                rgba,
                pts,
                color_tol=color_tol,
                step_tol=step_tol,
                brush_size=brush_size,
                feather=refine_feather,
                cleanup=False,
            )
        if finalize:
            out = polish_matte_alpha(
                out,
                color_tol=color_tol,
                feather=refine_feather,
            )
        elif not pts:
            return {"width": out.width, "height": out.height, "path": str(path)}
    else:
        raise ValueError(f"未知抠图模式: {mode}")

    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=finalize)
    path.write_bytes(buf.getvalue())
    return {"width": out.width, "height": out.height, "path": str(path)}


def layer_write_info(
    *,
    art_root: Path,
    layer: Layer,
    inbox_path: Path,
    asset_source: Path | None,
    asset_unity: Path | None = None,
    subject_path: str | None = None,
) -> dict[str, Any]:
    path = resolve_layer_image_path(
        art_root=art_root,
        layer=layer,
        inbox_path=inbox_path,
        asset_source=asset_source,
        asset_unity=asset_unity,
        subject_path=subject_path,
    )
    touches = touches_source_master(path, art_root=art_root, asset_source=asset_source)
    writes_inbox = False
    if path and inbox_path.is_file():
        try:
            writes_inbox = path.resolve() == inbox_path.resolve()
        except OSError:
            writes_inbox = False
    return {
        "path": str(path) if path else "",
        "touches_source": touches,
        "is_asset_source": is_asset_source_path(path, asset_source),
        "writes_inbox": writes_inbox,
    }
