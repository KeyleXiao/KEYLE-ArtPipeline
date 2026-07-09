#!/usr/bin/env python3
"""单资源云生图 + 写 source/inbox（不触碰 ComfyUI）。"""

from __future__ import annotations

import io
import shutil
import threading
from pathlib import Path
from typing import Any, Callable

from cloud.base import CloudGenerateRequest, CloudProviderError, ProgressCallback
from cloud.http_util import cloud_keys_from_defaults
from cloud.providers import get_cloud_provider
from cloud.registry import (
    CLOUD_GEN_MODE_EDIT,
    CLOUD_GEN_MODE_I2I,
    DEFAULT_CLOUD_STRENGTH,
    effective_cloud_gen_mode,
    get_model,
    is_cloud_checkpoint,
    provider_for_checkpoint,
)
from config_manager import Asset, ConfigManager, GEN_MODE_REDRAW

try:
    from alpha_matte import apply_alpha_matte_png
except ImportError:
    apply_alpha_matte_png = None  # type: ignore[misc, assignment]

try:
    from pipeline_core import GenerateResult, _log, _resize_png
except ImportError:
    from dataclasses import dataclass

    @dataclass
    class GenerateResult:
        asset_id: str
        ok: bool
        message: str
        source: Path | None = None

    def _log(fn, msg: str) -> None:
        if fn:
            fn(msg)

    def _resize_png(data: bytes, width: int, height: int) -> bytes:
        from PIL import Image

        im = Image.open(io.BytesIO(data)).convert("RGBA")
        if im.size != (width, height):
            im = im.resize((width, height), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


LogFn = Callable[[str], None]


def _cloud_prompt_for_asset(config: ConfigManager, asset: Asset) -> tuple[str, str]:
    from bootstrap_config import merge_prompt_prefix, merge_prompt_suffix

    merged = config.prompts_for_generation(asset)
    prompt = str(getattr(asset, "cloud_prompt", "") or "").strip()
    negative = str(getattr(asset, "cloud_negative", "") or "").strip()
    cat = config.category_by_id(asset.category)
    pos_common = (cat.positive_common if cat else "").strip()
    neg_common = (cat.negative_common if cat else "").strip()

    if prompt:
        prompt = merge_prompt_prefix(pos_common, prompt)
    else:
        prompt = merged["positive"]

    if negative:
        negative = merge_prompt_suffix(negative, neg_common)
    else:
        negative = merged["negative"]
    return prompt, negative


def _resolve_ref_paths(config: ConfigManager, asset: Asset, mode: str) -> tuple[Path | None, Path | None]:
    ref_path: Path | None = None
    base_path: Path | None = None
    src, inbox, _ = config.resolve_paths(asset)
    if mode == CLOUD_GEN_MODE_I2I:
        ref_path = config.resolve_ref_image_path(asset)
    elif mode == CLOUD_GEN_MODE_EDIT:
        if inbox.is_file():
            base_path = inbox
        elif src.is_file():
            base_path = src
        else:
            ref = config.resolve_ref_image_path(asset)
            base_path = ref
    return ref_path, base_path


def _extra_ref_paths_for_asset(config: ConfigManager, asset: Asset, mode: str) -> list[Path]:
    if mode != CLOUD_GEN_MODE_I2I:
        return []
    raw = str(getattr(asset, "cloud_ref_images_extra", "") or "").strip()
    if not raw:
        return []
    from cloud.providers.volcengine import parse_extra_ref_paths

    return parse_extra_ref_paths(raw, config.art_root())


def generate_one_cloud(
    config: ConfigManager,
    asset: Asset,
    *,
    seed: int | None = None,
    to_inbox: bool = True,
    log: LogFn | None = None,
    progress_cb: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> GenerateResult:
    checkpoint = config.checkpoint_for_asset(asset)
    if not is_cloud_checkpoint(checkpoint):
        return GenerateResult(asset.id, False, f"非云模型 checkpoint: {checkpoint}")

    model = get_model(checkpoint)
    if not model:
        return GenerateResult(asset.id, False, f"未在注册表中找到云模型: {checkpoint}")

    provider_id = provider_for_checkpoint(checkpoint)
    mode = effective_cloud_gen_mode(asset)
    prompt, negative = _cloud_prompt_for_asset(config, asset)
    if not prompt.strip():
        return GenerateResult(asset.id, False, "云 prompt 为空，请填写云提示词")

    ref_path, base_path = _resolve_ref_paths(config, asset, mode)
    strength = float(getattr(asset, "cloud_strength", DEFAULT_CLOUD_STRENGTH) or DEFAULT_CLOUD_STRENGTH)
    strength = max(0.01, min(1.0, strength))
    output_count = max(1, min(15, int(getattr(asset, "cloud_output_count", 1) or 1)))
    extra_refs = _extra_ref_paths_for_asset(config, asset, mode)

    req = CloudGenerateRequest(
        checkpoint=checkpoint,
        provider=provider_id,
        model=model,
        mode=mode,
        prompt=prompt,
        negative=negative,
        width=int(asset.width),
        height=int(asset.height),
        seed=config.resolve_seed_for_asset(asset, override=seed) if seed is None else seed,
        strength=strength,
        ref_image_path=ref_path,
        base_image_path=base_path,
        api_keys=cloud_keys_from_defaults(config.defaults),
        extra_ref_paths=extra_refs or None,
        output_count=output_count,
    )

    prov_label = model.get("label_zh") or checkpoint
    mode_label = {"text_to_image": "文生图", "image_to_image": "图生图", "image_edit": "图像编辑"}.get(mode, mode)
    _log(log, f"云生成 {asset.filename} {prov_label} · {mode_label} {asset.width}×{asset.height}")

    def _progress(info: dict[str, Any]) -> None:
        if progress_cb:
            info.setdefault("asset_id", asset.id)
            info.setdefault("filename", asset.filename)
            progress_cb(info)

    try:
        provider = get_cloud_provider(provider_id)
        result = provider.generate(req, progress_cb=_progress, cancel_event=cancel_event)
    except CloudProviderError as exc:
        return GenerateResult(asset.id, False, str(exc))
    except Exception as exc:
        return GenerateResult(asset.id, False, str(exc))

    if not result.ok or not result.png_bytes:
        return GenerateResult(asset.id, False, result.message or "云生成失败")

    if result.message and "共" in result.message:
        _log(log, f"  {result.message}")

    data = result.png_bytes
    if (asset.width, asset.height) != (0, 0):
        try:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as im:
                if im.size != (asset.width, asset.height):
                    data = _resize_png(data, asset.width, asset.height)
        except Exception:
            data = _resize_png(data, asset.width, asset.height)

    matte_mode = config.alpha_matte_for_asset(asset)
    if apply_alpha_matte_png and matte_mode != "none":
        try:
            data = apply_alpha_matte_png(data, mode=matte_mode)
            _log(log, f"  alpha 抠底 ({matte_mode})")
        except Exception as exc:
            _log(log, f"  alpha 抠底跳过: {exc}")

    src_path, inbox_path, _ = config.resolve_paths(asset)
    is_edit_only = mode == CLOUD_GEN_MODE_EDIT
    if is_edit_only:
        inbox_path.parent.mkdir(parents=True, exist_ok=True)
        inbox_path.write_bytes(data)
        _log(log, f"  inbox  → {config.rel_to_project(inbox_path)} (图像编辑，不写 source)")
        return GenerateResult(asset.id, True, "ok", inbox_path)
    src_path.parent.mkdir(parents=True, exist_ok=True)
    src_path.write_bytes(data)
    _log(log, f"  source → {config.rel_to_project(src_path)}")
    if to_inbox:
        inbox_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, inbox_path)
        _log(log, f"  inbox  → {config.rel_to_project(inbox_path)}")
    return GenerateResult(asset.id, True, "ok", src_path)
