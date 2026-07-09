#!/usr/bin/env python3
"""REST + SSE 路由。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import platform
import re
import shutil
import subprocess
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from backend.deps import TOOLS_DIR, get_config_manager, reload_config_manager, sync_log_bus_from_config
from backend.runtime_paths import user_data_dir
from backend.services.log_bus import log_bus
from backend.services.pipeline_runner import pipeline_runner
from backend.services.preview import (
    PREVIEW_MAX,
    PreviewSource,
    invalidate_preview_cache,
    preview_etag,
    preview_png_bytes,
    resolve_path_for_source,
    resolve_preview_file,
    should_remove_bg,
)

router = APIRouter(prefix="/api")


# ── 序列化 ─────────────────────────────────────────────


def _asset_dict(a: Any, *, full: bool = False) -> dict[str, Any]:
    from cloud.registry import is_cloud_checkpoint

    config = get_config_manager()
    effective_ckpt = config.checkpoint_for_asset(a)
    d = {
        "id": a.id,
        "filename": a.filename,
        "category": a.category,
        "width": a.width,
        "height": a.height,
        "size_label": a.size_label(),
        "subject": a.subject,
        "enabled": a.enabled,
        "seed": a.seed or "",
        "remove_bg_mode": a.remove_bg_mode,
        "gen_mode": getattr(a, "gen_mode", "txt2img"),
        "ref_image": getattr(a, "ref_image", ""),
        "ref_image_use_source": bool(getattr(a, "ref_image_use_source", False)),
        "img2img_denoise": getattr(a, "img2img_denoise", 0.65),
        "cloud_gen_mode": getattr(a, "cloud_gen_mode", "text_to_image"),
        "cloud_prompt": getattr(a, "cloud_prompt", ""),
        "cloud_negative": getattr(a, "cloud_negative", ""),
        "cloud_strength": getattr(a, "cloud_strength", 0.65),
        "cloud_output_count": int(getattr(a, "cloud_output_count", 1) or 1),
        "cloud_ref_images_extra": getattr(a, "cloud_ref_images_extra", ""),
        "checkpoint": getattr(a, "checkpoint", "") or "",
        "checkpoint_effective": effective_ckpt,
        "is_cloud_model": is_cloud_checkpoint(effective_ckpt),
    }
    if full:
        src, _inbox, _unity = config.resolve_paths(a)
        d["source_path"] = str(src)
        d["category_checkpoint"] = config.checkpoint_for_category(a.category)
        d["positive"] = a.positive
        d["negative"] = a.negative
        d["positive_prefix"] = getattr(a, "positive_prefix", "")
        d["positive_subject"] = getattr(a, "positive_subject", "")
        d["positive_scene"] = getattr(a, "positive_scene", "")
        d["positive_light"] = getattr(a, "positive_light", "")
        d["positive_g"] = a.positive_g
        d["positive_l"] = a.positive_l
        d["workflow"] = a.workflow
    return d


def _category_dict(c: Any, *, full: bool = False) -> dict[str, Any]:
    ckpt = c.checkpoint or ""
    d = {
        "id": c.id,
        "label": c.label,
        "checkpoint": ckpt,
        "checkpoint_short": Path(ckpt).name[:24] if ckpt else "未设置",
        "source": c.source,
        "inbox": c.inbox,
        "unity": c.unity,
        "alpha_matte": c.alpha_matte,
        "positive_common": c.positive_common if full else "",
        "negative_common": c.negative_common if full else "",
    }
    if full:
        config = get_config_manager()
        d["source"] = str(config.category_source_path(c))
        d["inbox"] = str(config.category_inbox_path(c))
        d["unity"] = str(config.category_unity_path(c))
    return d


def _file_info(path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {"path": str(path), "exists": False}
    try:
        if path.is_file():
            st = path.stat()
            out["exists"] = True
            out["mtime"] = st.st_mtime
            out["size"] = st.st_size
    except OSError:
        pass
    return out


def _asset_activity(asset: Any, *, limit: int = 8) -> list[dict[str, Any]]:
    needles = {asset.id, asset.filename}
    hits: list[dict[str, Any]] = []
    for entry in reversed(log_bus.history(limit=800)):
        msg = entry.get("msg", "")
        if any(n and n in msg for n in needles):
            hits.append(entry)
            if len(hits) >= limit:
                break
    hits.reverse()
    return hits


def _asset_info_dict(config: Any, asset: Any) -> dict[str, Any]:
    src, inbox, unity = config.resolve_paths(asset)
    wf_path = config.workflow_file_for_asset(asset)
    cfg_path = config.path
    raw = config._asset_raw(asset.id) or {}
    has_pp = "postprocess" in raw and raw.get("postprocess") is not None
    return {
        "id": asset.id,
        "filename": asset.filename,
        "size_label": asset.size_label(),
        "subject": asset.subject or "",
        "seed": asset.seed or "",
        "enabled": asset.enabled,
        "files": {
            "source": _file_info(src),
            "inbox": _file_info(inbox),
            "unity": _file_info(unity),
        },
        "workflow": _file_info(wf_path),
        "config_file": _file_info(cfg_path),
        "has_postprocess": has_pp,
        "activity": _asset_activity(asset),
    }


# ── Models ─────────────────────────────────────────────


class AssetUpdateBody(BaseModel):
    filename: str | None = None
    category: str | None = None
    width: int | None = None
    height: int | None = None
    seed: str | None = None
    subject: str | None = None
    enabled: bool | None = None
    remove_bg_mode: str | None = None
    checkpoint: str | None = None
    positive: str | None = None
    negative: str | None = None
    positive_prefix: str | None = None
    positive_subject: str | None = None
    positive_scene: str | None = None
    positive_light: str | None = None
    positive_g: str | None = None
    positive_l: str | None = None
    gen_mode: str | None = None
    ref_image: str | None = None
    ref_image_use_source: bool | None = None
    img2img_denoise: float | None = None
    cloud_gen_mode: str | None = None
    cloud_prompt: str | None = None
    cloud_negative: str | None = None
    cloud_strength: float | None = None
    cloud_output_count: int | None = None
    cloud_ref_images_extra: str | None = None


class LayerMatteBody(BaseModel):
    layer_id: str
    stack: dict[str, Any] | None = None
    subject_path: str | None = None
    mode: str = "border"
    seed_x: int | None = None
    seed_y: int | None = None
    seed_points: list[list[int]] | None = None
    color_tol: float = 34.0
    step_tol: float = 16.0
    feather: int = 0
    brush_size: int = Field(default=1, ge=1, le=50)
    finalize: bool = True


class LayerRawBody(BaseModel):
    layer_id: str
    stack: dict[str, Any] | None = None
    subject_path: str | None = None


class LayerRestoreImageBody(BaseModel):
    layer_id: str
    image_b64: str
    stack: dict[str, Any] | None = None
    subject_path: str | None = None


class LayerTrimAlphaBody(BaseModel):
    layer_id: str
    stack: dict[str, Any] | None = None
    subject_path: str | None = None
    alpha_threshold: int = 1


class PostprocessPrepareBody(BaseModel):
    subject_path: str = "inbox"


class CategoryUpdateBody(BaseModel):
    label: str | None = None
    source: str | None = None
    inbox: str | None = None
    unity: str | None = None
    checkpoint: str | None = None
    positive_common: str | None = None
    negative_common: str | None = None
    alpha_matte: str | None = None


class NewCategoryBody(BaseModel):
    label: str
    id: str | None = None
    checkpoint: str = ""


class NewAssetBody(BaseModel):
    filename: str
    category: str
    width: int = 512
    height: int = 512
    subject: str = ""
    enabled: bool = False


class AssetRenameBody(BaseModel):
    filename: str
    overwrite: bool = False


class MoveAssetCategoryBody(BaseModel):
    category: str


class GenerateBody(BaseModel):
    asset_ids: list[str] = Field(default_factory=list)
    export_after: bool = False


class AiChatBody(BaseModel):
    message: str
    asset_id: str
    mode: str = "free"


class OpenPathBody(BaseModel):
    path: str


class OpenUrlBody(BaseModel):
    url: str


class PickImageFileBody(BaseModel):
    initial_dir: str | None = None


class PickSavePngBody(BaseModel):
    initial_dir: str | None = None
    default_name: str = "export.png"
    title: str | None = None


class PickOutputDirBody(BaseModel):
    initial_dir: str | None = None
    title: str | None = None
    relative_base: str | None = "art"  # art | project | none


class ValidatePathBody(BaseModel):
    path: str = ""
    kind: str = "dir"  # dir | file | any
    base: str = "absolute"  # absolute | art | project
    optional: bool = False


def _resolve_user_path(config: Any, raw: str, base: str) -> Path:
    s = (raw or "").strip()
    if not s:
        return Path()
    if "\0" in s:
        raise ValueError("路径包含非法字符")
    p = Path(s).expanduser()
    if not p.is_absolute() and (s.startswith("Users/") or s.startswith("Volumes/")):
        p = Path("/") / s
        p = p.expanduser()
    if p.is_absolute():
        return p.resolve()
    if base == "art":
        return (config.art_root() / s).resolve()
    if base == "project":
        return (config.project_root() / s).resolve()
    return p.resolve()


def _rel_to_art_root(path: Path, art_root: Path) -> str:
    try:
        return path.resolve().relative_to(art_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _pick_image_path(initial_dir: Path) -> Path | None:
    initial = str(initial_dir.resolve())
    if platform.system() == "Darwin":
        esc = initial.replace("\\", "\\\\").replace('"', '\\"')
        script = f'''
set defaultPath to POSIX file "{esc}"
try
    set picked to choose file of type {{"png", "jpg", "jpeg", "webp", "PNG", "JPG", "public.image"}} with prompt "选择图片" default location defaultPath
    return POSIX path of picked
on error number -128
    return ""
end try
'''
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        p = r.stdout.strip()
        return Path(p) if p else None
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except tk.TclError:
            pass
        path = filedialog.askopenfilename(
            title="选择图片",
            initialdir=initial,
            filetypes=[
                ("Images", "*.png;*.jpg;*.jpeg;*.webp"),
                ("PNG", "*.png"),
                ("All", "*.*"),
            ],
        )
        root.destroy()
        return Path(path) if path else None
    except Exception:
        return None


def _pick_save_png_path(initial_dir: Path, default_name: str, title: str = "保存 PNG") -> Path | None:
    initial = str(initial_dir.resolve())
    safe_name = Path(default_name).name or "export.png"
    if platform.system() == "Darwin":
        esc = initial.replace("\\", "\\\\").replace('"', '\\"')
        name_esc = safe_name.replace('"', '\\"')
        title_esc = title.replace('"', '\\"')
        script = f'''
set defaultPath to POSIX file "{esc}"
try
    set picked to choose file name with prompt "{title_esc}" default name "{name_esc}" default location defaultPath
    return POSIX path of picked
on error number -128
    return ""
end try
'''
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        p = r.stdout.strip()
        return Path(p) if p else None
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except tk.TclError:
            pass
        path = filedialog.asksaveasfilename(
            title=title,
            initialdir=initial,
            initialfile=safe_name,
            defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("All", "*.*")],
        )
        root.destroy()
        return Path(path) if path else None
    except Exception:
        return None


def _pick_output_dir(initial_dir: Path, title: str = "选择文件夹") -> Path | None:
    initial = str(initial_dir.resolve())
    if platform.system() == "Darwin":
        esc = initial.replace("\\", "\\\\").replace('"', '\\"')
        title_esc = title.replace('"', '\\"')
        script = f'''
set defaultPath to POSIX file "{esc}"
try
    set picked to choose folder with prompt "{title_esc}" default location defaultPath
    return POSIX path of picked
on error number -128
    return ""
end try
'''
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        p = r.stdout.strip()
        return Path(p) if p else None
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except tk.TclError:
            pass
        path = filedialog.askdirectory(title=title, initialdir=initial, mustexist=True)
        root.destroy()
        return Path(path) if path else None
    except Exception:
        return None


def _safe_export_stem(name: str, layer_id: str) -> str:
    stem = re.sub(r"[^\w\-\u4e00-\u9fff]+", "_", (name or layer_id).strip())
    stem = stem.strip("_") or layer_id
    return stem[:64]


def _inbox_multi_layer_dir(inbox: Path, asset: Any, asset_id: str) -> Path:
    stem = Path(asset.filename or asset_id).stem
    return inbox.parent / stem


def _export_stack_layers_to_dir(
    stack: Any,
    resolver: Any,
    output_dir: Path,
    *,
    asset_id: str = "",
    asset_filename: str = "",
    tight: bool = True,
    include_hidden: bool = False,
) -> dict[str, Any]:
    """将 stack 各可见图层导出为单独 PNG，并写入 manifest.json。"""
    from postprocess.engine import render_layer_export_png_bytes

    output_dir.mkdir(parents=True, exist_ok=True)
    exported: list[dict[str, Any]] = []
    used_names: dict[str, int] = {}
    for layer in stack.layers:
        if not include_hidden and layer.visible is False:
            continue
        try:
            data, meta = render_layer_export_png_bytes(
                stack,
                resolver,
                layer.id,
                tight=bool(tight),
            )
        except Exception as exc:
            log_bus.log(f"图层导出跳过 {layer.name}: {exc}", kind="系统")
            continue
        if not data or meta.get("width", 0) <= 0 or meta.get("height", 0) <= 0:
            continue
        base = _safe_export_stem(layer.name or "", layer.id)
        count = used_names.get(base, 0)
        used_names[base] = count + 1
        fname = f"{base}.png" if count == 0 else f"{base}_{count + 1}.png"
        out_path = output_dir / fname
        _write_bytes_atomic(out_path, data)
        exported.append(
            {
                "id": layer.id,
                "name": layer.name,
                "type": layer.type,
                "file": fname,
                "path": str(out_path.resolve()),
                "offset_x": meta["offset_x"],
                "offset_y": meta["offset_y"],
                "width": meta["width"],
                "height": meta["height"],
            }
        )

    if not exported:
        raise ValueError("没有可导出的可见图层")

    manifest = {
        "asset_id": asset_id,
        "filename": asset_filename,
        "canvas_width": stack.canvas_width,
        "canvas_height": stack.canvas_height,
        "tight_crop": bool(tight),
        "layers": exported,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "count": len(exported),
        "layers": exported,
        "manifest": str(manifest_path.resolve()),
        "dir": str(output_dir.resolve()),
    }


def _default_export_dir(config: Any, asset: Any) -> Path:
    art_root = config.art_root()
    stem = Path(asset.filename or asset.id).stem
    return art_root / "postprocess" / "exports" / stem


def _resolve_pick_initial_dir(config: Any, body_initial: str | None) -> Path:
    art_root = config.art_root()
    project_root = config.project_root()
    initial = art_root
    raw = (body_initial or "").strip()
    if not raw:
        return initial

    cand = Path(raw).expanduser()
    if cand.is_absolute():
        if cand.is_dir():
            return cand.resolve()
        if cand.parent.is_dir():
            return cand.parent.resolve()
        return initial

    for root in (art_root, project_root):
        full = (root / raw).resolve()
        if full.is_dir():
            return full
        if full.parent.is_dir():
            return full.parent
    if cand.is_dir():
        return cand.resolve()
    if cand.parent.is_dir():
        return cand.parent.resolve()
    return initial


# ── Health / config ─────────────────────────────────────────────


@router.get("/health")
def health() -> dict[str, Any]:
    from backend.services.pipeline_runner import pipeline_runner

    return {
        "status": "ok",
        "ui": "artApp-web",
        "legacy": str(TOOLS_DIR / "artTool_ui.py"),
        "job": pipeline_runner.progress_snapshot(),
    }


@router.post("/config/reload")
def config_reload() -> dict[str, str]:
    reload_config_manager()
    log_bus.log("配置已重新加载", kind="系统")
    return {"status": "reloaded"}


@router.post("/config/save")
def config_save() -> dict[str, str]:
    get_config_manager().save()
    log_bus.log("配置已保存", kind="系统")
    return {"status": "saved"}


# ── ComfyUI ─────────────────────────────────────────────


@router.get("/comfyui/status")
def comfyui_status() -> dict[str, Any]:
    from pipeline_core import PipelineCore

    ok, msg = PipelineCore(get_config_manager()).test_comfyui()
    out: dict[str, Any] = {"ok": ok, "message": msg}
    if ok:
        out["live_preview_hint"] = (
            "实时采样预览需 ComfyUI 以 --preview-method auto（或 taesd）启动"
        )
    return out


@router.get("/comfyui/checkpoints")
def list_checkpoints() -> dict[str, Any]:
    from pipeline_core import PipelineCore

    try:
        ckpts = PipelineCore(get_config_manager()).list_checkpoints()
        return {"checkpoints": ckpts, "offline": False}
    except Exception as exc:
        return {"checkpoints": [], "offline": True, "message": str(exc)}


@router.get("/cloud/models")
def cloud_models() -> dict[str, Any]:
    from cloud.registry import cloud_gen_modes, list_cloud_models, load_registry

    reg = load_registry()
    providers: dict[str, Any] = {}
    for pid, prov in (reg.get("providers") or {}).items():
        entry: dict[str, Any] = {}
        for key in ("label_zh", "label_en", "size_constraints"):
            if key in prov:
                entry[key] = prov[key]
        if entry:
            providers[pid] = entry
    return {
        "models": list_cloud_models(),
        "gen_modes": cloud_gen_modes(),
        "providers": providers,
        "defaults": reg.get("defaults") or {},
    }


@router.get("/generation/models")
def generation_models() -> dict[str, Any]:
    from cloud.registry import list_cloud_models, load_registry

    local = list_checkpoints()
    cloud = list_cloud_models()
    reg = load_registry()
    providers: dict[str, Any] = {}
    for pid, prov in (reg.get("providers") or {}).items():
        entry: dict[str, Any] = {}
        for key in ("label_zh", "label_en", "size_constraints"):
            if key in prov:
                entry[key] = prov[key]
        if entry:
            providers[pid] = entry
    return {
        "local": local,
        "cloud": {"models": cloud, "providers": providers},
    }


@router.post("/cloud/verify")
def cloud_verify(body: dict[str, Any]) -> dict[str, Any]:
    from cloud.http_util import cloud_keys_from_defaults
    from cloud.verify import verify_cloud_provider

    provider = str(body.get("provider") or "").strip().lower()
    if not provider:
        raise HTTPException(400, "provider 为空")

    config = get_config_manager()
    d = config.defaults
    incoming = body.get("keys") if isinstance(body.get("keys"), dict) else {}

    if provider == "deepseek":
        from ai_assistant import verify_deepseek

        api_key = str(
            body.get("api_key") or incoming.get("deepseek_api_key") or d.get("deepseek_api_key") or ""
        ).strip()
        model = str(body.get("model") or incoming.get("deepseek_model") or d.get("deepseek_model") or "")
        ok, message = verify_deepseek(api_key, model)
        return {"ok": ok, "message": message, "provider": "deepseek"}

    merged = cloud_keys_from_defaults(d)
    for k, v in incoming.items():
        if v is not None and str(v).strip():
            merged[str(k)] = str(v).strip()

    result = verify_cloud_provider(provider, merged)
    if not result.get("ok"):
        return result
    return result


# ── Jobs ─────────────────────────────────────────────


@router.get("/jobs/status")
def job_status() -> dict[str, Any]:
    return pipeline_runner.progress_snapshot()


@router.get("/jobs/live-preview")
def job_live_preview() -> Response:
    data, mime, _rev = pipeline_runner.live_preview_bytes()
    if not data:
        raise HTTPException(404, "当前无采样预览")
    return Response(content=data, media_type=mime, headers={"Cache-Control": "no-store"})


@router.post("/jobs/cancel")
def job_cancel() -> dict[str, str]:
    pipeline_runner.cancel()
    return {"status": "cancel_requested"}


@router.post("/generate")
def generate_assets(body: GenerateBody) -> dict[str, Any]:
    if not body.asset_ids:
        raise HTTPException(400, "asset_ids 为空")
    try:
        run_id, queue_position = pipeline_runner.generate_batch(
            body.asset_ids, export_after=body.export_after
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "status": "started" if queue_position <= 1 else "queued",
        "run_id": run_id,
        "queue_position": queue_position,
    }


@router.post("/export")
def export_assets(body: GenerateBody) -> dict[str, Any]:
    if not body.asset_ids:
        raise HTTPException(400, "asset_ids 为空")
    try:
        run_id, queue_position = pipeline_runner.export_batch(body.asset_ids)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "status": "started" if queue_position <= 1 else "queued",
        "run_id": run_id,
        "queue_position": queue_position,
    }


@router.post("/workflows/init")
def init_workflows() -> dict[str, Any]:
    from pipeline_core import PipelineCore

    n = PipelineCore(get_config_manager()).init_asset_workflows_from_default()
    log_bus.log(f"已为 {n} 个资源创建工作流 JSON", kind="系统")
    return {"created": n}


@router.get("/workflows/templates")
def list_workflow_templates(
    category: str = Query(""),
    gen_mode: str = Query("txt2img"),
) -> dict[str, Any]:
    from workflow_presets import list_presets

    presets = list_presets(category=category, gen_mode=gen_mode)
    return {"presets": presets}


@router.get("/workflows/templates/{preset_id}")
def get_workflow_template(preset_id: str) -> dict[str, str]:
    from paths import WORKFLOWS_DIR
    from workflow_presets import preset_by_id

    preset = preset_by_id(preset_id)
    if not preset:
        raise HTTPException(404, f"未知工作流预设: {preset_id}")
    p = WORKFLOWS_DIR / Path(str(preset["file"])).name
    if not p.is_file():
        raise HTTPException(404, f"模板文件不存在: {preset['file']}")
    return {
        "id": preset["id"],
        "name": preset["name"],
        "mode": str(preset.get("mode", "")),
        "description": str(preset.get("description", "")),
        "how_it_works": str(preset.get("how_it_works", "")),
        "recommended_for": str(preset.get("recommended_for", "")),
        "file": str(preset["file"]),
        "text": p.read_text(encoding="utf-8"),
    }


# ── Logs ─────────────────────────────────────────────


@router.get("/logs")
def get_logs(tab: str = Query("全部"), limit: int = Query(500, le=2000)) -> dict[str, Any]:
    return {"entries": log_bus.history(tab=tab, limit=limit)}


@router.delete("/logs")
def clear_logs() -> dict[str, str]:
    log_bus.clear()
    return {"status": "cleared"}


@router.get("/logs/stream")
async def stream_logs() -> StreamingResponse:
    async def event_gen():
        q = log_bus.subscribe()
        try:
            for entry in log_bus.history(limit=50):
                yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
            while True:
                try:
                    entry = await asyncio.wait_for(q.get(), timeout=25.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if entry is None:
                    break
                yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
        finally:
            log_bus.unsubscribe(q)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# ── Settings ─────────────────────────────────────────────


def _settings_api_portals() -> dict[str, str]:
    from cloud.registry import settings_api_portals

    return settings_api_portals()


@router.get("/settings")
def get_settings() -> dict[str, Any]:
    from paths import default_log_dir

    config = get_config_manager()
    d = config.defaults
    return {
        "project_root": str(d.get("project_root", "")),
        "art_pipeline_root": str(d.get("art_pipeline_root", "")),
        "log_dir": str(d.get("log_dir", "")),
        "log_dir_default": str(default_log_dir()),
        "log_dir_effective": str(config.log_dir()),
        "log_file": str((config.log_dir() / "studio.log")),
        "comfyui_url": str(d.get("comfyui_url", "")),
        "steps": int(d.get("steps", 35)),
        "cfg": float(d.get("cfg", 7.0)),
        "sampler": str(d.get("sampler", "euler_ancestral")),
        "scheduler": str(d.get("scheduler", "normal")),
        "seed": str(d.get("seed", "")),
        "checkpoint": str(d.get("checkpoint", "")),
        "deepseek_api_key": str(d.get("deepseek_api_key", "")),
        "deepseek_model": str(d.get("deepseek_model", "")),
        "cloud_max_concurrent": int(d.get("cloud_max_concurrent") or 3),
        "cloud_api_keys": dict(d.get("cloud_api_keys") or {}),
        "api_portals": _settings_api_portals(),
    }


@router.put("/settings")
def put_settings(body: dict[str, Any]) -> dict[str, str]:
    from ai_assistant import SUPPORTED_MODELS, resolve_model

    config = get_config_manager()
    d = config.defaults
    project = str(body.get("project_root", "")).strip()
    if project and not Path(project).expanduser().is_dir():
        raise HTTPException(400, f"Unity 项目根目录不存在: {project}")
    art = str(body.get("art_pipeline_root", "")).strip()
    if art and not Path(art).expanduser().is_dir():
        raise HTTPException(400, f"ArtPipeline 根目录不存在: {art}")
    if "log_dir" in body:
        log_dir = str(body.get("log_dir", "")).strip()
        if log_dir:
            log_path = Path(log_dir).expanduser()
            try:
                log_path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise HTTPException(400, f"无法创建日志目录: {exc}") from exc
            if not log_path.is_dir():
                raise HTTPException(400, f"日志路径不是目录: {log_dir}")
        d["log_dir"] = log_dir
    for key in (
        "project_root",
        "art_pipeline_root",
        "comfyui_url",
        "sampler",
        "scheduler",
        "seed",
        "checkpoint",
        "deepseek_api_key",
    ):
        if key in body:
            d[key] = str(body[key]).strip()
    if "steps" in body:
        d["steps"] = int(body["steps"])
    if "cfg" in body:
        d["cfg"] = float(body["cfg"])
    if "deepseek_model" in body:
        m = resolve_model(str(body["deepseek_model"]))
        if m not in SUPPORTED_MODELS:
            raise HTTPException(400, f"不支持的模型: {m}")
        d["deepseek_model"] = m
    if "cloud_max_concurrent" in body:
        d["cloud_max_concurrent"] = max(1, min(16, int(body["cloud_max_concurrent"])))
    if "cloud_api_keys" in body and isinstance(body["cloud_api_keys"], dict):
        keys = dict(d.get("cloud_api_keys") or {})
        for k, v in body["cloud_api_keys"].items():
            keys[str(k)] = str(v).strip()
        d["cloud_api_keys"] = keys
    config.save()
    reload_config_manager()
    sync_log_bus_from_config()
    log_bus.log("全局设置已保存", kind="系统")
    return {"status": "saved"}


@router.get("/app/welcome")
def get_app_welcome() -> dict[str, Any]:
    from app_release import APP_VERSION, DEFAULT_RELEASE_NOTES

    config = get_config_manager()
    app_version = str(config.data.get("app_version") or APP_VERSION).strip() or APP_VERSION
    release_notes = config.data.get("release_notes")
    if not isinstance(release_notes, dict):
        release_notes = dict(DEFAULT_RELEASE_NOTES)
    dismissed = str(config.defaults.get("welcome_dismissed_version", "")).strip()
    return {
        "app_version": app_version,
        "release_notes": release_notes,
        "show": dismissed != app_version,
    }


@router.put("/app/welcome/dismiss")
def dismiss_app_welcome() -> dict[str, str]:
    from app_release import APP_VERSION

    config = get_config_manager()
    app_version = str(config.data.get("app_version") or APP_VERSION).strip() or APP_VERSION
    config.defaults["welcome_dismissed_version"] = app_version
    config.save()
    return {"status": "ok"}


@router.get("/settings/paths/default")
def default_paths() -> dict[str, str]:
    from paths import ART_ROOT, PROJECT_ROOT, default_log_dir

    return {
        "project_root": str(PROJECT_ROOT.resolve()),
        "art_pipeline_root": str(ART_ROOT.resolve()),
        "log_dir": str(default_log_dir()),
    }


# ── Categories ─────────────────────────────────────────────


@router.get("/categories")
def list_categories(full: bool = Query(False)) -> dict[str, Any]:
    config = get_config_manager()
    return {"categories": [_category_dict(c, full=full) for c in config.categories()]}


@router.get("/categories/{cat_id}")
def get_category(cat_id: str) -> dict[str, Any]:
    cat = get_config_manager().category_by_id(cat_id)
    if not cat:
        raise HTTPException(404, f"未知分类: {cat_id}")
    return _category_dict(cat, full=True)


@router.put("/categories/{cat_id}")
def update_category(cat_id: str, body: CategoryUpdateBody) -> dict[str, str]:
    config = get_config_manager()
    cat = config.category_by_id(cat_id)
    if not cat:
        raise HTTPException(404, f"未知分类: {cat_id}")
    if body.label is not None:
        cat.label = body.label.strip()
    if body.source is not None:
        cat.source = config.normalize_category_path(body.source.strip(), under="art")
    if body.inbox is not None:
        cat.inbox = config.normalize_category_path(body.inbox.strip(), under="art")
    if body.unity is not None:
        cat.unity = config.normalize_category_path(body.unity.strip(), under="project")
    if body.checkpoint is not None:
        cat.checkpoint = body.checkpoint.strip()
    if body.positive_common is not None:
        cat.positive_common = body.positive_common
    if body.negative_common is not None:
        cat.negative_common = body.negative_common
    if body.alpha_matte is not None:
        cat.alpha_matte = body.alpha_matte
    config.update_category(cat)
    config.ensure_category_dirs(cat)
    reload_config_manager()
    log_bus.log(f"已保存分类设置: {cat.id}", kind="操作")
    return {"status": "saved"}


@router.post("/categories")
def create_category(body: NewCategoryBody) -> dict[str, Any]:
    try:
        cat = get_config_manager().add_category(
            body.label.strip(),
            body.id,
            checkpoint=body.checkpoint.strip(),
        )
        reload_config_manager()
        log_bus.log(f"新建分类: {cat.id}", kind="操作")
        return _category_dict(cat, full=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/categories/{cat_id}")
def delete_category(cat_id: str) -> dict[str, Any]:
    config = get_config_manager()
    cat = config.category_by_id(cat_id)
    if not cat:
        raise HTTPException(404, f"未知分类: {cat_id}")
    try:
        removed_assets = config.delete_category(cat_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    reload_config_manager()
    log_bus.log(f"已删除分类: {cat_id}（{removed_assets} 个资源配置）", kind="操作")
    return {"status": "deleted", "id": cat_id, "removed_assets": removed_assets}


# ── Assets ─────────────────────────────────────────────


@router.get("/assets")
def list_assets(category: str = Query(...), q: str = Query("")) -> dict[str, Any]:
    config = get_config_manager()
    if not config.category_by_id(category):
        raise HTTPException(404, f"未知分类: {category}")
    query = q.strip().lower()
    items = []
    for asset in config.assets(category=category):
        if query and query not in asset.filename.lower() and query not in asset.id.lower():
            continue
        items.append(_asset_dict(asset))
    return {"category": category, "count": len(items), "assets": items}


@router.get("/assets/{asset_id}")
def get_asset(asset_id: str) -> dict[str, Any]:
    asset = get_config_manager().asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    return _asset_dict(asset, full=True)


@router.put("/assets/{asset_id}")
def update_asset(asset_id: str, body: AssetUpdateBody) -> dict[str, Any]:
    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    if body.filename is not None:
        try:
            asset.filename = config.normalize_asset_filename(body.filename)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if body.category is not None:
        asset.category = body.category.strip()
    if body.width is not None:
        asset.width = int(body.width)
    if body.height is not None:
        asset.height = int(body.height)
    if body.seed is not None:
        asset.seed = body.seed.strip()
    if body.subject is not None:
        asset.subject = body.subject.strip()
    if body.enabled is not None:
        asset.enabled = body.enabled
    if body.remove_bg_mode is not None:
        asset.remove_bg_mode = body.remove_bg_mode
    if body.checkpoint is not None:
        asset.checkpoint = body.checkpoint.strip()
    if body.positive is not None:
        asset.positive = body.positive
    if body.negative is not None:
        asset.negative = body.negative
    if body.positive_prefix is not None:
        asset.positive_prefix = body.positive_prefix
    if body.positive_subject is not None:
        asset.positive_subject = body.positive_subject
    if body.positive_scene is not None:
        asset.positive_scene = body.positive_scene
    if body.positive_light is not None:
        asset.positive_light = body.positive_light
    if body.positive_g is not None:
        asset.positive_g = body.positive_g
    if body.positive_l is not None:
        asset.positive_l = body.positive_l
    if body.gen_mode is not None:
        mode = body.gen_mode.strip().lower()
        if mode not in ("txt2img", "img2img", "redraw"):
            raise HTTPException(400, "gen_mode 须为 txt2img、img2img 或 redraw")
        asset.gen_mode = mode
        asset.ref_image_use_source = False
        if mode == "redraw":
            asset.ref_image = ""
    if body.ref_image is not None:
        asset.ref_image = body.ref_image.strip()
    if body.ref_image_use_source is not None:
        asset.ref_image_use_source = bool(body.ref_image_use_source)
    if body.img2img_denoise is not None:
        d = float(body.img2img_denoise)
        if d < 0.01 or d > 1.0:
            raise HTTPException(400, "img2img_denoise 须在 0.01–1.0")
        asset.img2img_denoise = d
    if body.cloud_gen_mode is not None:
        mode = body.cloud_gen_mode.strip()
        if mode not in ("text_to_image", "image_to_image", "image_edit"):
            raise HTTPException(400, "cloud_gen_mode 无效")
        asset.cloud_gen_mode = mode
    if body.cloud_prompt is not None:
        asset.cloud_prompt = body.cloud_prompt
    if body.cloud_negative is not None:
        asset.cloud_negative = body.cloud_negative
    if body.cloud_strength is not None:
        s = float(body.cloud_strength)
        if s < 0.01 or s > 1.0:
            raise HTTPException(400, "cloud_strength 须在 0.01–1.0")
        asset.cloud_strength = s
    if body.cloud_output_count is not None:
        asset.cloud_output_count = max(1, min(15, int(body.cloud_output_count)))
    if body.cloud_ref_images_extra is not None:
        asset.cloud_ref_images_extra = body.cloud_ref_images_extra.strip()
    config.sync_asset_prompt_fields(asset)
    if asset.width < 32 or asset.height < 32 or asset.width > 4096 or asset.height > 4096:
        raise HTTPException(400, "尺寸须在 32–4096")
    try:
        config.update_asset(asset)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    reload_config_manager()
    log_bus.log(f"已保存资源: {asset.id}", kind="操作")
    return _asset_dict(asset, full=True)


@router.post("/assets/{asset_id}/rename")
def rename_asset(asset_id: str, body: AssetRenameBody) -> dict[str, Any]:
    from config_manager import RenameFileConflictError

    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    try:
        renamed = config.rename_asset_files(
            asset, body.filename, overwrite=body.overwrite
        )
    except RenameFileConflictError as exc:
        raise HTTPException(
            409,
            detail={
                "code": "rename_file_conflict",
                "message": str(exc),
                "filename": exc.filename,
                "conflicts": exc.conflicts,
                "can_overwrite": True,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    config._update_asset_dict(asset)
    config.save()
    reload_config_manager()
    note = "（覆盖残留文件）" if body.overwrite else ""
    log_bus.log(
        f"已重命名资源: {asset.id} → {asset.filename}（{len(renamed)} 个文件）{note}",
        kind="操作",
    )
    return {
        "asset": _asset_dict(asset, full=True),
        "renamed": renamed,
        "filename": asset.filename,
        "overwritten": body.overwrite,
    }


@router.get("/assets/{asset_id}/move-preview")
def move_asset_preview(asset_id: str, category: str = Query(...)) -> dict[str, Any]:
    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    try:
        return config.preview_move_asset_to_category(asset, category.strip())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/assets/{asset_id}/move-category")
def move_asset_category(asset_id: str, body: MoveAssetCategoryBody) -> dict[str, Any]:
    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    target = body.category.strip()
    if not target:
        raise HTTPException(400, "目标分类不能为空")
    try:
        result = config.move_asset_to_category(asset, target)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    reload_config_manager()
    asset = config.asset_by_id(asset_id)
    assert asset is not None
    log_bus.log(
        f"已移动资源: {asset.id} → 分类 {target}（{len(result.get('moved', []))} 个文件）",
        kind="操作",
    )
    return {
        "asset": _asset_dict(asset, full=True),
        "moved": result.get("moved", []),
        "skipped": result.get("skipped", []),
        "from_category": result.get("from_category"),
        "to_category": result.get("to_category"),
    }


@router.post("/assets")
def create_asset(body: NewAssetBody) -> dict[str, Any]:
    fn = body.filename.strip()
    if not fn:
        raise HTTPException(400, "文件名不能为空")
    if not fn.lower().endswith(".png"):
        fn += ".png"
    try:
        asset = get_config_manager().add_asset(
            filename=fn,
            category=body.category,
            width=body.width,
            height=body.height,
            subject=body.subject,
            enabled=body.enabled,
        )
        reload_config_manager()
        log_bus.log(f"新建资源: {asset.id}", kind="操作")
        return _asset_dict(asset, full=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _normalize_import_filename(name: str) -> str:
    stem = Path(name or "import").stem.strip() or "import"
    return f"{stem}.png"


def _import_asset_image(
    config: Any,
    *,
    category: str,
    original_name: str,
    content: bytes,
    gen_mode: str = "txt2img",
) -> Any:
    import io

    from PIL import Image

    from config_manager import GEN_MODE_REDRAW, GEN_MODE_TXT2IMG, GEN_MODES

    mode = (gen_mode or GEN_MODE_TXT2IMG).strip().lower()
    if mode not in (GEN_MODE_TXT2IMG, GEN_MODE_REDRAW):
        raise ValueError("gen_mode 须为 txt2img 或 redraw")

    if not content:
        raise ValueError("空文件")
    try:
        img = Image.open(io.BytesIO(content))
        width, height = img.size
    except Exception as exc:
        raise ValueError(f"无法读取图片: {exc}") from exc

    filename = _normalize_import_filename(original_name)
    if config.asset_by_filename(filename):
        raise ValueError(f"文件名已存在: {filename}")

    asset = config.add_asset(
        filename=filename,
        category=category,
        width=int(width),
        height=int(height),
        subject="",
        enabled=False,
    )
    asset.gen_mode = mode
    asset.ref_image_use_source = False

    buf = io.BytesIO()
    img.convert("RGBA").save(buf, format="PNG", optimize=True)
    png = buf.getvalue()
    src_path, inbox_path, _ = config.resolve_paths(asset)
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    if mode == GEN_MODE_REDRAW:
        inbox_path.write_bytes(png)
    else:
        src_path.parent.mkdir(parents=True, exist_ok=True)
        src_path.write_bytes(png)
        inbox_path.write_bytes(png)

    config.update_asset(asset)
    return asset


@router.post("/assets/import")
async def import_assets(
    category: str = Form(...),
    gen_mode: str = Form("txt2img"),
    replace_asset_ids: str = Form(""),
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    if not category.strip():
        raise HTTPException(400, "分类不能为空")
    if not files:
        raise HTTPException(400, "未选择文件")
    config = get_config_manager()
    if not config.category_by_id(category):
        raise HTTPException(404, f"未知分类: {category}")

    replace_ids = {x.strip() for x in replace_asset_ids.split(",") if x.strip()}
    created: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    trashed: list[dict[str, Any]] = []
    for upload in files:
        raw_name = Path(upload.filename or "import.png").name
        try:
            target_name = _normalize_import_filename(raw_name)
            existing = config.asset_by_filename(target_name)
            if existing:
                if existing.id not in replace_ids:
                    failed.append({"filename": raw_name, "error": f"文件名已存在: {target_name}"})
                    continue
                trashed.append(config.move_asset_to_trash(existing.id))
                replace_ids.discard(existing.id)
            content = await upload.read()
            asset = _import_asset_image(
                config,
                category=category,
                original_name=raw_name,
                content=content,
                gen_mode=gen_mode,
            )
            created.append(_asset_dict(asset, full=True))
        except ValueError as exc:
            failed.append({"filename": raw_name, "error": str(exc)})
        except OSError as exc:
            failed.append({"filename": raw_name, "error": f"写入失败: {exc}"})

    if created or trashed:
        reload_config_manager()
        if trashed:
            names = ", ".join(t["filename"] for t in trashed)
            log_bus.log(f"导入替换：{len(trashed)} 个旧资源已移入回收站（{names}）", kind="操作")
        if created:
            log_bus.log(
                f"批量导入 {len(created)} 个资源 → 分类 {category}"
                + (f"（{len(failed)} 失败）" if failed else ""),
                kind="操作",
            )
    if not created and failed:
        raise HTTPException(400, failed[0]["error"])
    return {
        "created": created,
        "failed": failed,
        "trashed": trashed,
        "count": len(created),
    }


@router.delete("/assets/{asset_id}")
def delete_asset(asset_id: str) -> dict[str, str]:
    config = get_config_manager()
    if not config.asset_by_id(asset_id):
        raise HTTPException(404, f"资源不存在: {asset_id}")
    config.delete_asset(asset_id)
    reload_config_manager()
    log_bus.log(f"已删除资源配置: {asset_id}", kind="操作")
    return {"status": "deleted"}


@router.post("/assets/{asset_id}/duplicate")
def duplicate_asset(asset_id: str) -> dict[str, Any]:
    import shutil
    from paths import WORKFLOWS_DIR

    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    base = Path(asset.filename).stem
    new_fn = f"{base}_copy.png"
    n = 1
    while config.asset_by_filename(new_fn):
        new_fn = f"{base}_copy{n}.png"
        n += 1
    clone = config.add_asset(
        filename=new_fn,
        category=asset.category,
        width=asset.width,
        height=asset.height,
        subject=asset.subject,
        positive=asset.positive,
        negative=asset.negative,
        workflow=asset.workflow,
        seed=asset.seed,
        enabled=asset.enabled,
    )
    clone.gen_mode = getattr(asset, "gen_mode", "txt2img")
    clone.ref_image = getattr(asset, "ref_image", "")
    clone.ref_image_use_source = bool(getattr(asset, "ref_image_use_source", False))
    clone.img2img_denoise = getattr(asset, "img2img_denoise", 0.65)
    clone.positive_g = asset.positive_g
    clone.positive_l = asset.positive_l
    clone.positive_prefix = getattr(asset, "positive_prefix", "")
    clone.positive_subject = getattr(asset, "positive_subject", "")
    clone.positive_scene = getattr(asset, "positive_scene", "")
    clone.positive_light = getattr(asset, "positive_light", "")
    clone.remove_bg_mode = asset.remove_bg_mode
    wf_src = config.workflow_file_for_asset(asset)
    wf_dst = WORKFLOWS_DIR / "assets" / f"{clone.id}.json"
    if wf_src.is_file():
        wf_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(wf_src, wf_dst)
        clone.workflow = f"workflows/assets/{clone.id}.json"
    config.update_asset(clone)
    reload_config_manager()
    log_bus.log(f"复制资源: {clone.id}", kind="操作")
    return _asset_dict(clone, full=True)


@router.post("/assets/status")
def assets_status(body: dict[str, Any]) -> dict[str, Any]:
    config = get_config_manager()
    ids = body.get("ids") or []
    out: dict[str, dict[str, dict[str, Any]]] = {}

    def _fingerprint(path: Path) -> str | None:
        if not path.is_file():
            return None
        try:
            digest = hashlib.md5()
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except OSError:
            return None

    def _slot(path: Path, state: str) -> dict[str, Any]:
        return {
            "state": state,
            "exists": state in ("ok", "modified", "outdated"),
            "dir": str(path.parent),
            "file": str(path),
        }

    for aid in ids:
        asset = config.asset_by_id(str(aid))
        if not asset:
            continue
        src, inbox, unity = config.resolve_paths(asset)
        src_fp = _fingerprint(src)
        in_fp = _fingerprint(inbox)
        un_fp = _fingerprint(unity)

        source_state = "ok" if src_fp else "none"

        if not in_fp:
            inbox_state = "missing"
        elif src_fp and in_fp == src_fp:
            inbox_state = "ok"
        else:
            inbox_state = "modified"

        if not un_fp:
            unity_state = "missing"
        elif in_fp and un_fp == in_fp:
            unity_state = "ok"
        else:
            unity_state = "outdated"

        out[asset.id] = {
            "source": _slot(src, source_state),
            "inbox": _slot(inbox, inbox_state),
            "unity": _slot(unity, unity_state),
        }

    return {"status": out}


@router.get("/assets/{asset_id}/preview.png")
def asset_preview(
    asset_id: str,
    source: PreviewSource = Query("inbox"),
    max: int = Query(PREVIEW_MAX, ge=64, le=2048),
) -> Response:
    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    try:
        path = resolve_preview_file(config, asset, source)
        remove_bg = should_remove_bg(config, asset)
        etag = preview_etag(path, max, remove_bg)
        data = preview_png_bytes(config, asset, source=source, max_size=max)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
    headers = {"Cache-Control": "private, max-age=86400"}
    if etag:
        headers["ETag"] = etag
    return Response(content=data, media_type="image/png", headers=headers)


@router.get("/assets/{asset_id}/paths")
def asset_paths(asset_id: str) -> dict[str, str]:
    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    src, inbox, unity = config.resolve_paths(asset)
    return {"source": str(src), "inbox": str(inbox), "unity": str(unity)}


@router.get("/assets/{asset_id}/info")
def asset_info(asset_id: str) -> dict[str, Any]:
    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    return _asset_info_dict(config, asset)


# ── Workflow ─────────────────────────────────────────────


@router.get("/assets/{asset_id}/workflow")
def get_workflow(asset_id: str) -> dict[str, Any]:
    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    path = config.workflow_file_for_asset(asset)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    return {"path": str(path), "text": text}


@router.put("/assets/{asset_id}/workflow")
def put_workflow(asset_id: str, body: dict[str, Any]) -> dict[str, str]:
    from pipeline_core import PipelineCore
    from workflow_engine import validate_workflow_json

    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    text = str(body.get("text", ""))
    data, err = validate_workflow_json(text)
    if err:
        raise HTTPException(400, err)
    assert data is not None
    clean = {k: v for k, v in data.items() if not str(k).startswith("_")}
    PipelineCore(config).save_asset_workflow(asset, clean)
    log_bus.log(f"工作流已保存: {asset.id}", kind="操作")
    return {"status": "saved"}


@router.post("/assets/{asset_id}/workflow/validate")
def validate_workflow(asset_id: str, body: dict[str, Any]) -> dict[str, str]:
    from workflow_engine import validate_workflow_json

    _, err = validate_workflow_json(str(body.get("text", "")))
    if err:
        raise HTTPException(400, err)
    return {"status": "ok"}


@router.get("/assets/{asset_id}/workflow/default")
def default_workflow(asset_id: str) -> dict[str, str]:
    from config_manager import GEN_MODES_IMG2IMG, effective_gen_mode
    from paths import WORKFLOWS_DIR
    from workflow_presets import preset_by_id, suggest_preset_id

    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    gen_mode = effective_gen_mode(asset)
    preset_id = suggest_preset_id(category=asset.category, gen_mode=gen_mode)
    preset = preset_by_id(preset_id)
    rel = str(preset["file"]) if preset else "workflows/_default_sdxl_api.json"
    if gen_mode not in GEN_MODES_IMG2IMG:
        cat = config.category_by_id(asset.category)
        if cat and cat.default_workflow:
            rel = cat.default_workflow
    p = WORKFLOWS_DIR / Path(rel).name
    if not p.is_file():
        p = WORKFLOWS_DIR / (
            "_default_sdxl_img2img_api.json" if gen_mode in GEN_MODES_IMG2IMG else "_default_sdxl_api.json"
        )
    text = p.read_text(encoding="utf-8") if p.is_file() else ""
    return {"text": text, "preset_id": preset_id}


# ── Postprocess ─────────────────────────────────────────────


def _ensure_inbox_for_postprocess(config: Any, asset: Any) -> Path:
    """后处理仅编辑 inbox；若 inbox 缺失则从 source 复制（不修改 source）。"""
    src, inbox, _unity = config.resolve_paths(asset)
    if inbox.is_file():
        return inbox
    if not src.is_file():
        raise HTTPException(400, f"无 inbox 且 source 不存在: {asset.filename}")
    inbox.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, inbox)
    log_bus.log(f"已从 source 初始化 inbox: {asset.filename}", kind="操作")
    return inbox


def _resolve_subject_path(config: Any, asset: Any, subject: str | None) -> Path | None:
    """解析主体 $asset 读取路径；subject 可为 source / inbox / unity 或绝对/相对路径。"""
    src, inbox, unity = config.resolve_paths(asset)
    key = (subject or "").strip().lower()
    if key in ("source", "src"):
        return src if src.is_file() else None
    if key in ("inbox", "in"):
        return inbox if inbox.is_file() else None
    if key in ("unity", "engine"):
        return unity if unity.is_file() else None
    if subject:
        p = Path(subject).expanduser()
        if p.is_file():
            return p
        rel = config.art_root() / subject
        if rel.is_file():
            return rel
    if inbox.is_file():
        return inbox
    if src.is_file():
        return src
    return None


def _pp_resolver(config: Any, asset: Any, subject: str | None = None) -> Any:
    from postprocess.engine import AssetImageResolver

    src, inbox, unity = config.resolve_paths(asset)
    return AssetImageResolver(
        art_root=config.art_root(),
        asset_source=src if src.is_file() else None,
        asset_inbox=inbox if inbox.is_file() else None,
        asset_unity=unity if unity.is_file() else None,
        subject_path=_resolve_subject_path(config, asset, subject),
    )


def _sync_inbox_before_apply(config: Any, asset: Any, body: dict[str, Any]) -> None:
    """apply / preview 前：source 有更新时同步到 inbox，或按请求强制同步。"""
    if body.get("skip_inbox_sync"):
        return
    from postprocess.matte import (
        is_subject_master_edit_mode,
        sync_asset_source_to_inbox,
        sync_inbox_if_source_newer,
    )

    src, inbox, _unity = config.resolve_paths(asset)
    if not src.is_file():
        return
    editing_master = is_subject_master_edit_mode(body.get("subject_path"))
    if body.get("sync_source") and not editing_master:
        sync_asset_source_to_inbox(src, inbox)
        log_bus.log(f"apply 前已同步 source → inbox: {asset.filename}", kind="操作")
        return
    if body.get("prefer_source_sync") and not editing_master:
        sync_asset_source_to_inbox(src, inbox)
        log_bus.log(f"apply 前已同步 source → inbox: {asset.filename}", kind="操作")
        return
    if sync_inbox_if_source_newer(src, inbox):
        log_bus.log(f"apply 前已同步 source → inbox: {asset.filename}", kind="操作")


def _after_layer_image_write(config: Any, asset: Any, path: Path | None) -> None:
    """图层 PNG 写入后：若改了本资源 source 原图，同步到 inbox。"""
    from backend.services.postprocess_preview import invalidate_postprocess_preview_cache
    from postprocess.matte import is_asset_source_path, sync_asset_source_to_inbox

    invalidate_postprocess_preview_cache()

    if path is None:
        return
    src, inbox, _ = config.resolve_paths(asset)
    if not is_asset_source_path(path, src if src.is_file() else None):
        return
    if sync_asset_source_to_inbox(src, inbox):
        log_bus.log(f"已同步 source → inbox: {asset.filename}", kind="操作")


def _pp_layer_write_kwargs(config: Any, asset: Any, inbox: Path, subject_path: str | None) -> dict[str, Any]:
    src, _inbox, unity = config.resolve_paths(asset)
    return {
        "art_root": config.art_root(),
        "inbox_path": inbox,
        "asset_source": src if src.is_file() else None,
        "asset_unity": unity if unity.is_file() else None,
        "subject_path": subject_path,
    }


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.write.tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    finally:
        if tmp.is_file():
            try:
                tmp.unlink()
            except OSError:
                pass


def _master_target_path(
    config: Any,
    asset: Any,
    subject_path: str | None,
) -> tuple[str, Path] | None:
    from postprocess.matte import normalize_edit_subject

    src, _inbox, unity = config.resolve_paths(asset)
    mode = normalize_edit_subject(subject_path)
    if mode == "source" and src.is_file():
        return mode, src
    if mode == "unity" and unity.is_file():
        return mode, unity
    return None


def _validate_stack_canvas_size(stack: Any) -> None:
    cw, ch = int(stack.canvas_width), int(stack.canvas_height)
    if cw < 32 or ch < 32 or cw > 4096 or ch > 4096:
        raise ValueError("画布尺寸须在 32–4096")


def _stack_canvas_differs_from_asset(asset: Any, stack: Any) -> bool:
    return int(stack.canvas_width) != int(asset.width) or int(stack.canvas_height) != int(
        asset.height
    )


def _reset_subject_layer_after_master_write(stack: Any) -> None:
    from postprocess.models import LayerTransform

    subj = stack.subject_layer()
    if not subj:
        return
    offset_x = subj.transform.offset_x
    offset_y = subj.transform.offset_y
    anchor = subj.transform.anchor
    subj.crop = None
    subj.transform = LayerTransform(
        offset_x=offset_x,
        offset_y=offset_y,
        scale=1.0,
        anchor=anchor,
    )


def _reset_subject_layer_transform_to_default(subj: Any) -> None:
    """主体层恢复默认变换（画布已与 PNG 像素 1:1 对齐时使用）。"""
    from postprocess.models import LayerTransform

    anchor = subj.transform.anchor if subj.transform else "center"
    subj.crop = None
    subj.transform = LayerTransform(anchor=anchor)


def _is_single_subject_stack(stack: Any) -> bool:
    layers = stack.layers or []
    return len(layers) == 1 and layers[0].type == "image" and bool(layers[0].is_subject)


def _write_canvas_render_to_source(
    config: Any,
    asset: Any,
    stack: Any,
    resolver: Any,
    subject_path: str | None,
) -> bytes | None:
    """source 编辑：将整幅画布合成写入 source 原图（用于修改画布尺寸）。

    返回已合成的 PNG 字节；调用方勿再 render_stack，否则主体会被二次合成放大。
    """
    from postprocess.engine import render_stack_to_png_bytes
    from postprocess.matte import is_subject_source_edit_mode, sync_asset_source_to_inbox

    if not is_subject_source_edit_mode(subject_path):
        return None
    target_info = _master_target_path(config, asset, subject_path)
    if not target_info or target_info[0] != "source":
        return None
    _target_mode, target = target_info
    try:
        data = render_stack_to_png_bytes(stack, resolver)
    except Exception as exc:
        raise ValueError(f"合成 source 失败: {exc}") from exc
    if not data:
        raise ValueError("合成 source 结果为空")
    _write_bytes_atomic(target, data)
    _reset_subject_layer_after_master_write(stack)
    src, inbox, _unity = config.resolve_paths(asset)
    if sync_asset_source_to_inbox(src, inbox):
        log_bus.log(f"画布写入 source 后已同步 inbox: {asset.filename}", kind="操作")
    log_bus.log(
        f"已按画布尺寸写入 source: {asset.filename}（{stack.canvas_width}×{stack.canvas_height}）",
        kind="操作",
    )
    return data


def _subject_had_crop(stack: Any) -> bool:
    subj = stack.subject_layer()
    return bool(subj and subj.crop)


def _clear_subject_crop(stack: Any) -> None:
    subj = stack.subject_layer()
    if subj:
        subj.crop = None


def _bake_subject_crop_to_inbox(
    config: Any,
    asset: Any,
    stack: Any,
    resolver: Any,
    inbox: Path,
) -> bool:
    """inbox 模式 apply：将主体裁切烘焙进 inbox 工作副本，避免与合成阶段重复裁切。"""
    import io

    from postprocess.engine import bake_subject_crop_pixels
    from postprocess.models import ASSET_SUBJECT_SOURCE, layer_image_source

    subj = stack.subject_layer()
    if not subj or subj.type != "image" or layer_image_source(subj) != ASSET_SUBJECT_SOURCE:
        return False
    if not subj.crop:
        return False
    baked = bake_subject_crop_pixels(subj, resolver)
    if baked is None:
        return False
    buf = io.BytesIO()
    baked.save(buf, format="PNG", optimize=True)
    _write_bytes_atomic(inbox, buf.getvalue())
    subj.crop = None
    log_bus.log(f"apply 前已烘焙裁切到 inbox: {asset.filename}", kind="操作")
    return True


def _sync_asset_dimensions_from_stack(config: Any, asset: Any, stack: Any) -> bool:
    _validate_stack_canvas_size(stack)
    cw, ch = int(stack.canvas_width), int(stack.canvas_height)
    if int(asset.width) == cw and int(asset.height) == ch:
        return False
    asset.width = cw
    asset.height = ch
    config.update_asset(asset)
    return True


def _bake_subject_crop_to_master(
    config: Any,
    asset: Any,
    stack: Any,
    resolver: Any,
    subject_path: str | None,
) -> bool:
    """source/unity 模式：将主体裁切烘焙写入原图。"""
    import io

    from postprocess.engine import bake_subject_crop_pixels
    from postprocess.matte import is_subject_master_edit_mode
    from postprocess.models import ASSET_SUBJECT_SOURCE, layer_image_source

    if not is_subject_master_edit_mode(subject_path):
        return False
    target_info = _master_target_path(config, asset, subject_path)
    if not target_info:
        return False
    mode, target = target_info
    subj = stack.subject_layer()
    if not subj or subj.type != "image" or layer_image_source(subj) != ASSET_SUBJECT_SOURCE:
        return False
    if not subj.crop:
        return False
    baked = bake_subject_crop_pixels(subj, resolver)
    if baked is None:
        return False
    buf = io.BytesIO()
    baked.save(buf, format="PNG", optimize=True)
    _write_bytes_atomic(target, buf.getvalue())
    subj.crop = None
    log_bus.log(f"已烘焙裁切到 {mode}: {asset.filename}", kind="操作")
    return True


def _bake_subject_transform_to_master(
    config: Any,
    asset: Any,
    stack: Any,
    resolver: Any,
    subject_path: str | None,
) -> bool:
    """source/unity 编辑模式：将主体 $asset 的裁切/镜像/缩放/旋转烘焙写入原图。"""
    import io

    from postprocess.engine import bake_image_layer_pixels
    from postprocess.matte import is_subject_master_edit_mode
    from postprocess.models import ASSET_SUBJECT_SOURCE, LayerTransform, layer_image_source

    if not is_subject_master_edit_mode(subject_path):
        return False
    target_info = _master_target_path(config, asset, subject_path)
    if not target_info:
        return False
    mode, target = target_info
    subj = stack.subject_layer()
    if not subj or subj.type != "image" or layer_image_source(subj) != ASSET_SUBJECT_SOURCE:
        return False
    baked = bake_image_layer_pixels(subj, resolver)
    if baked is None:
        return False
    buf = io.BytesIO()
    baked.save(buf, format="PNG", optimize=True)
    _write_bytes_atomic(target, buf.getvalue())
    _reset_subject_layer_after_master_write(stack)
    label = "source" if mode == "source" else "unity"
    log_bus.log(f"已烘焙变换到 {label}: {asset.filename}", kind="操作")
    return True


def _sync_inbox_before_render(config: Any, asset: Any, body: dict[str, Any] | None = None) -> None:
    """合成/apply 前：source 较新时刷新 inbox，避免主体层仍用旧 inbox。"""
    _sync_inbox_before_apply(config, asset, body or {})


def _resolve_postprocess_stack(
    config: Any,
    asset: Any,
    stack_body: dict[str, Any] | None = None,
) -> Any:
    """优先用请求体 stack（编辑器内存态），否则读配置，再退回默认模板。"""
    from postprocess.models import stack_from_dict

    if stack_body:
        stack = stack_from_dict(stack_body)
        if stack:
            return stack
    stack = config.get_postprocess_stack(asset.id)
    if not stack:
        stack = config.default_postprocess_stack(asset)
    return stack


def _subject_layer_has_default_layout(layer: Any) -> bool:
    xf = layer.transform
    crop = layer.crop
    return (
        abs(float(xf.scale) - 1.0) < 1e-6
        and abs(float(xf.offset_x)) < 1e-6
        and abs(float(xf.offset_y)) < 1e-6
        and abs(float(xf.rotation_deg)) < 1e-6
        and not xf.flip_h
        and not xf.flip_v
        and crop is None
    )


def _sync_stack_canvas_to_subject_image(
    config: Any,
    asset: Any,
    stack: Any,
    subject_path: str | None,
    *,
    persist: bool = False,
) -> tuple[bool, int, int]:
    """将画布对齐到正在编辑的主体 PNG 尺寸；单主体层时清除迁就旧画布的缩放/偏移。"""
    from PIL import Image

    from postprocess.matte import normalize_edit_subject, resolve_layer_image_path

    subj = stack.subject_layer()
    if not subj or subj.type != "image":
        return False, int(stack.canvas_width), int(stack.canvas_height)

    inbox = _ensure_inbox_for_postprocess(config, asset)
    subject_key = normalize_edit_subject(subject_path)
    path = resolve_layer_image_path(
        **_pp_layer_write_kwargs(config, asset, inbox, subject_key),
        layer=subj,
    )
    if not path or not path.is_file():
        return False, int(stack.canvas_width), int(stack.canvas_height)

    with Image.open(path) as im:
        iw, ih = int(im.width), int(im.height)

    crop = subj.crop
    if crop and int(getattr(crop, "w", 0) or 0) > 0 and int(getattr(crop, "h", 0) or 0) > 0:
        iw = int(crop.w)
        ih = int(crop.h)

    cw, ch = int(stack.canvas_width), int(stack.canvas_height)
    asset_w, asset_h = int(asset.width), int(asset.height)
    single_subject = _is_single_subject_stack(stack)
    default_layout = _subject_layer_has_default_layout(subj)

    changed = False
    stack.edit_subject = subject_key
    canvas_resized_by_sync = False

    if cw == iw and ch == ih:
        report_w, report_h = iw, ih
    elif (cw, ch) == (asset_w, asset_h):
        # 资源登记尺寸与 stack 一致，但 PNG 像素已变（外部换图）→ 跟随 PNG
        stack.canvas_width = iw
        stack.canvas_height = ih
        canvas_resized_by_sync = True
        changed = True
        report_w, report_h = iw, ih
    else:
        # 用户在后处理中自定义了画布尺寸（尚未或已写入 PNG）→ 保留 stack 画布
        report_w, report_h = cw, ch

    if canvas_resized_by_sync and single_subject:
        _reset_subject_layer_transform_to_default(subj)
        changed = True
    elif cw == iw and ch == ih and single_subject and not default_layout:
        _reset_subject_layer_transform_to_default(subj)
        changed = True

    if changed and persist:
        config.set_postprocess_stack(asset.id, stack)
    return changed, report_w, report_h


@router.get("/assets/{asset_id}/postprocess")
def get_postprocess(
    asset_id: str,
    subject: str = "inbox",
    skip_canvas_sync: bool = False,
) -> dict[str, Any]:
    from postprocess.matte import normalize_edit_subject
    from postprocess.models import stack_to_dict

    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    _ensure_inbox_for_postprocess(config, asset)
    subject_key = normalize_edit_subject(subject)
    stack = config.get_postprocess_stack(asset_id)
    if not stack:
        stack = config.default_postprocess_stack(asset)
    if skip_canvas_sync:
        synced, img_w, img_h = False, int(stack.canvas_width), int(stack.canvas_height)
    else:
        synced, img_w, img_h = _sync_stack_canvas_to_subject_image(
            config, asset, stack, subject_key, persist=True
        )
        if synced:
            reload_config_manager()
    return {
        "stack": stack_to_dict(stack),
        "image_size": {"width": img_w, "height": img_h},
        "canvas_synced": synced,
        "edit_subject": subject_key,
    }


@router.put("/assets/{asset_id}/postprocess")
def put_postprocess(asset_id: str, body: dict[str, Any]) -> dict[str, str]:
    from postprocess.matte import normalize_edit_subject
    from postprocess.models import stack_from_dict

    config = get_config_manager()
    if not config.asset_by_id(asset_id):
        raise HTTPException(404, f"资源不存在: {asset_id}")
    stack = stack_from_dict(body.get("stack"))
    if not stack:
        raise HTTPException(400, "无效 stack")
    try:
        _validate_stack_canvas_size(stack)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    edit_subject = body.get("edit_subject")
    if edit_subject is not None:
        stack.edit_subject = normalize_edit_subject(str(edit_subject))
    config.set_postprocess_stack(asset_id, stack)
    reload_config_manager()
    return {"status": "saved"}


@router.post("/assets/{asset_id}/postprocess/prepare")
def prepare_postprocess(asset_id: str, body: PostprocessPrepareBody) -> dict[str, Any]:
    """打开后处理前：inbox 为空时从 source 复制；校验编辑目标文件存在。"""
    from postprocess.matte import normalize_edit_subject

    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    subject = normalize_edit_subject(body.subject_path)
    src, inbox, unity = config.resolve_paths(asset)
    inbox_initialized = False

    if subject == "inbox":
        if not inbox.is_file():
            if not src.is_file():
                raise HTTPException(400, f"无 inbox 且 source 不存在: {asset.filename}")
            inbox.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, inbox)
            inbox_initialized = True
            log_bus.log(f"已从 source 初始化 inbox: {asset.filename}", kind="操作")
    elif subject == "source":
        if not src.is_file():
            raise HTTPException(400, f"source 原图不存在: {asset.filename}")
    elif subject == "unity":
        if not unity.is_file():
            raise HTTPException(400, f"游戏引擎导出文件不存在: {asset.filename}")

    stack = config.get_postprocess_stack(asset_id)
    if not stack:
        stack = config.default_postprocess_stack(asset)
    synced, img_w, img_h = _sync_stack_canvas_to_subject_image(
        config, asset, stack, subject, persist=True
    )
    if synced:
        reload_config_manager()

    return {
        "status": "ok",
        "subject_path": subject,
        "inbox_initialized": inbox_initialized,
        "canvas_synced": synced,
        "image_size": {"width": img_w, "height": img_h},
        "paths": {
            "source": str(src) if src else "",
            "inbox": str(inbox) if inbox else "",
            "unity": str(unity) if unity else "",
        },
    }


@router.post("/assets/{asset_id}/postprocess/preview")
def postprocess_preview(asset_id: str, body: dict[str, Any]) -> Response:
    """编辑器实时预览（不写入配置）。"""
    from backend.services.postprocess_preview import (
        cached_postprocess_preview_png,
        postprocess_preview_cache_key,
    )

    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    inbox = _ensure_inbox_for_postprocess(config, asset)
    _sync_inbox_before_render(config, asset, body)
    stack = _resolve_postprocess_stack(config, asset, body.get("stack"))
    if not stack:
        raise HTTPException(400, "无效 stack")
    resolver = _pp_resolver(config, asset, body.get("subject_path"))
    solo = body.get("solo_layer_id") or None
    try:
        src, _inbox, unity = config.resolve_paths(asset)
        cache_key = postprocess_preview_cache_key(
            asset_id,
            stack,
            body,
            config=config,
            asset=asset,
            inbox=inbox,
            asset_source=src if src.is_file() else None,
            asset_unity=unity if unity.is_file() else None,
        )
        data = cached_postprocess_preview_png(cache_key, stack, resolver, solo_layer_id=solo)
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
    return Response(content=data, media_type="image/png")


@router.post("/assets/{asset_id}/postprocess/bounds")
def postprocess_bounds(asset_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """图层边界（命中测试 / 选框）。"""
    from PIL import Image

    from postprocess.engine import layer_bounds, layer_frame
    from postprocess.models import layer_image_source, stack_from_dict

    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    stack = stack_from_dict(body.get("stack"))
    if not stack:
        raise HTTPException(400, "无效 stack")
    resolver = _pp_resolver(config, asset, body.get("subject_path"))
    scratch = Image.new(
        "RGBA",
        (max(stack.canvas_width, 4), max(stack.canvas_height, 4)),
        (0, 0, 0, 0),
    )
    layers_out: list[dict[str, Any]] = []
    raw_sizes: dict[str, dict[str, int]] = {}
    for layer in stack.layers:
        if layer.type == "image":
            raw = resolver.resolve(layer_image_source(layer))
            if raw:
                raw_sizes[layer.id] = {"w": raw.width, "h": raw.height}
        bounds = layer_bounds(layer, stack, resolver, scratch=scratch)
        frame = layer_frame(layer, stack, resolver, scratch=scratch)
        if bounds and frame:
            layers_out.append(
                {
                    "id": layer.id,
                    "x": frame["x"],
                    "y": frame["y"],
                    "w": frame["w"],
                    "h": frame["h"],
                    "visible": layer.visible,
                    "locked": layer.locked,
                    "type": layer.type,
                    "is_subject": layer.is_subject,
                    **({"corners": frame["corners"]} if frame.get("corners") else {}),
                    **({"pivot": frame["pivot"]} if frame.get("pivot") else {}),
                    **({"local_w": frame["local_w"]} if frame.get("local_w") else {}),
                    **({"local_h": frame["local_h"]} if frame.get("local_h") else {}),
                    **({"pivot_norm": frame["pivot_norm"]} if frame.get("pivot_norm") else {}),
                }
            )
    return {
        "canvas": {"width": stack.canvas_width, "height": stack.canvas_height},
        "layers": layers_out,
        "raw_sizes": raw_sizes,
    }


@router.post("/assets/{asset_id}/postprocess/layer-matte")
def postprocess_layer_matte(asset_id: str, body: LayerMatteBody) -> dict[str, Any]:
    from postprocess.matte import apply_layer_matte, resolve_layer_image_path

    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    inbox = _ensure_inbox_for_postprocess(config, asset)
    stack = _resolve_postprocess_stack(config, asset, body.stack)
    layer = next((l for l in stack.layers if l.id == body.layer_id), None)
    if not layer or layer.type != "image":
        raise HTTPException(400, "图层不存在或非图片层")
    if layer.locked:
        raise HTTPException(400, "图层已锁定")

    path = resolve_layer_image_path(
        **_pp_layer_write_kwargs(config, asset, inbox, body.subject_path),
        layer=layer,
    )
    if not path:
        raise HTTPException(404, "图层图片不存在")

    mode = body.mode.strip().lower()
    if mode not in ("border", "seed", "stroke"):
        raise HTTPException(400, "mode 须为 border、seed 或 stroke")
    if mode == "seed" and (body.seed_x is None or body.seed_y is None):
        raise HTTPException(400, "seed 模式需要 seed_x / seed_y")
    if mode == "stroke" and not body.seed_points and not body.finalize:
        raise HTTPException(400, "stroke 模式需要 seed_points 或 finalize")

    seed_points: list[tuple[int, int]] | None = None
    if mode == "stroke" and body.seed_points:
        seed_points = []
        for pt in body.seed_points:
            if not isinstance(pt, (list, tuple)) or len(pt) != 2:
                raise HTTPException(400, "seed_points 须为 [[x,y], ...]")
            seed_points.append((int(pt[0]), int(pt[1])))

    try:
        result = apply_layer_matte(
            path,
            mode=mode,
            seed_x=body.seed_x,
            seed_y=body.seed_y,
            seed_points=seed_points,
            color_tol=float(body.color_tol),
            step_tol=float(body.step_tol),
            feather=int(body.feather),
            brush_size=int(body.brush_size),
            finalize=bool(body.finalize),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    _after_layer_image_write(config, asset, path)
    log_bus.log(f"图层抠图 ({mode}): {layer.name} · {asset.filename}", kind="操作")
    return {"status": "ok", "mode": mode, **result}


@router.post("/assets/{asset_id}/postprocess/layer-write-info")
def postprocess_layer_write_info(asset_id: str, body: LayerRawBody) -> dict[str, Any]:
    """查询图层抠图/写回将修改的文件路径（是否触及 source 原图）。"""
    from postprocess.matte import layer_write_info

    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    inbox = _ensure_inbox_for_postprocess(config, asset)
    stack = _resolve_postprocess_stack(config, asset, body.stack)
    layer = next((l for l in stack.layers if l.id == body.layer_id), None)
    if not layer or layer.type != "image":
        raise HTTPException(404, f"图层不存在或非图片: {body.layer_id}")
    return layer_write_info(
        **_pp_layer_write_kwargs(config, asset, inbox, body.subject_path),
        layer=layer,
    )


def _postprocess_layer_raw_image(
    config: Any,
    asset: Any,
    layer_id: str,
    stack_body: dict[str, Any] | None = None,
    subject_path: str | None = None,
) -> tuple[bytes, Any]:
    import io

    from PIL import Image

    from postprocess.matte import resolve_layer_image_path

    inbox = _ensure_inbox_for_postprocess(config, asset)
    stack = _resolve_postprocess_stack(config, asset, stack_body)
    layer = next((l for l in stack.layers if l.id == layer_id), None)
    if not layer or layer.type != "image":
        raise HTTPException(404, f"图层不存在或非图片: {layer_id}")
    path = resolve_layer_image_path(
        **_pp_layer_write_kwargs(config, asset, inbox, subject_path),
        layer=layer,
    )
    if not path or not path.is_file():
        log_bus.log(f"layer-raw 图片不存在: {asset.filename} layer={layer_id}", kind="系统")
        raise HTTPException(404, "图层图片不存在")
    if path.suffix.lower() == ".png":
        try:
            data = path.read_bytes()
            if len(data) >= 8:
                return data, layer
        except OSError:
            pass
    with Image.open(path) as im:
        rgba = im.convert("RGBA")
    buf = io.BytesIO()
    rgba.save(buf, format="PNG", optimize=False, compress_level=1)
    return buf.getvalue(), layer


@router.post("/assets/{asset_id}/postprocess/layer-raw")
def layer_raw_png_post(asset_id: str, body: LayerRawBody) -> Response:
    """任意图片图层的原图 PNG（编辑器内存 stack）。"""
    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    data, _layer = _postprocess_layer_raw_image(
        config, asset, body.layer_id, body.stack, body.subject_path
    )
    return Response(content=data, media_type="image/png")


@router.get("/assets/{asset_id}/postprocess/layer-raw.png")
def layer_raw_png(asset_id: str, layer_id: str) -> Response:
    """任意图片图层的原图 PNG（兼容 GET，使用已保存/默认 stack）。"""
    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    data, _layer = _postprocess_layer_raw_image(config, asset, layer_id)
    return Response(content=data, media_type="image/png")


@router.post("/assets/{asset_id}/postprocess/layer-restore-image")
def layer_restore_image(asset_id: str, body: LayerRestoreImageBody) -> dict[str, str]:
    """写回图层 PNG（撤销/重做）。"""
    import base64
    import binascii

    from postprocess.matte import resolve_layer_image_path

    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    inbox = _ensure_inbox_for_postprocess(config, asset)
    stack = _resolve_postprocess_stack(config, asset, body.stack)
    layer = next((l for l in stack.layers if l.id == body.layer_id), None)
    if not layer or layer.type != "image":
        raise HTTPException(404, f"图层不存在或非图片: {body.layer_id}")
    if layer.locked:
        raise HTTPException(400, "图层已锁定")
    path = resolve_layer_image_path(
        **_pp_layer_write_kwargs(config, asset, inbox, body.subject_path),
        layer=layer,
    )
    if not path:
        raise HTTPException(404, "图层图片不存在")
    raw_b64 = body.image_b64.strip()
    if raw_b64.startswith("data:"):
        raw_b64 = raw_b64.split(",", 1)[-1]
    try:
        data = base64.b64decode(raw_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(400, "无效 image_b64") from exc
    if len(data) < 8:
        raise HTTPException(400, "图片数据过短")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    _after_layer_image_write(config, asset, path)
    layer.crop = None
    config.set_postprocess_stack(asset_id, stack)
    reload_config_manager()
    return {"status": "ok", "path": str(path)}


@router.post("/assets/{asset_id}/postprocess/trim-layer-alpha")
def trim_layer_alpha(asset_id: str, body: LayerTrimAlphaBody) -> dict[str, Any]:
    """自适应裁切：去除图层 PNG 透明边距并写回，调整 offset 保持画布位置。"""
    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    _ensure_inbox_for_postprocess(config, asset)
    stack = _resolve_postprocess_stack(config, asset, body.stack)
    if not stack:
        raise HTTPException(400, "无效 stack")
    layer = next((l for l in stack.layers if l.id == body.layer_id), None)
    if not layer or layer.type != "image":
        raise HTTPException(404, f"图层不存在或非图片: {body.layer_id}")
    return _apply_auto_trim_stack(
        config,
        asset,
        stack,
        layer,
        alpha_threshold=body.alpha_threshold,
        subject_path=body.subject_path,
    )


@router.post("/assets/{asset_id}/postprocess/restore-from-source")
def restore_postprocess_from_source(asset_id: str) -> dict[str, Any]:
    """用 source 原图覆盖 inbox，并重置后处理配置（source 只读，仅写 inbox）。"""
    from postprocess.models import stack_to_dict

    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    src, inbox, _unity = config.resolve_paths(asset)
    if not src.is_file():
        raise HTTPException(400, f"source 原图不存在: {src.name}")
    inbox.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, inbox)
    stack = config.default_postprocess_stack(asset)
    config.set_postprocess_stack(asset_id, stack)
    reload_config_manager()
    log_bus.log(f"已从 source 还原 inbox 并重置后处理: {asset.filename}", kind="操作")
    return {"path": str(inbox), "stack": stack_to_dict(stack)}


@router.post("/assets/{asset_id}/remove-magenta")
def remove_magenta_from_asset(asset_id: str) -> dict[str, Any]:
    """对 inbox 应用品红色键去底（HudV8 快捷操作），并重置后处理图层栈。"""
    import io

    import numpy as np
    from alpha_matte import magenta_key_to_alpha
    from PIL import Image
    from postprocess.models import stack_to_dict

    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    inbox = _ensure_inbox_for_postprocess(config, asset)
    with Image.open(inbox) as im:
        im.load()
        before = im.convert("RGBA")
        alpha_before = np.array(before)[:, :, 3]
        out = magenta_key_to_alpha(before, fuzz=34.0, feather=2)
        alpha_after = np.array(out)[:, :, 3]

    if not np.any(alpha_before != alpha_after):
        return {"changed": False, "path": str(inbox)}

    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    _write_bytes_atomic(inbox, buf.getvalue())
    invalidate_preview_cache()
    from backend.services.postprocess_preview import invalidate_postprocess_preview_cache

    invalidate_postprocess_preview_cache()

    stack = config.default_postprocess_stack(asset)
    config.set_postprocess_stack(asset_id, stack)
    reload_config_manager()
    log_bus.log(f"去除洋红: {asset.filename}", kind="操作")
    return {
        "changed": True,
        "path": str(inbox),
        "stack": stack_to_dict(stack),
    }


def _subject_image_layer(stack: Any) -> Any | None:
    for layer in stack.layers:
        if layer.type == "image" and (layer.is_subject or layer.source == "$asset"):
            return layer
    for layer in stack.layers:
        if layer.type == "image":
            return layer
    return None


def _apply_auto_trim_stack(
    config: Any,
    asset: Any,
    stack: Any,
    layer: Any,
    *,
    alpha_threshold: int = 1,
    subject_path: str | None = None,
) -> dict[str, Any]:
    """自适应裁切：裁图层透明边并缩小画布。"""
    import io

    from PIL import Image

    from postprocess.engine import layer_frame, shrink_stack_canvas_to_content, trim_layer_alpha_pixels
    from postprocess.matte import resolve_layer_image_path
    from postprocess.models import stack_to_dict

    inbox = _ensure_inbox_for_postprocess(config, asset)
    if layer.locked:
        raise HTTPException(400, "图层已锁定")

    resolver = _pp_resolver(config, asset, subject_path)
    scratch = Image.new(
        "RGBA",
        (max(stack.canvas_width, 4), max(stack.canvas_height, 4)),
        (0, 0, 0, 0),
    )
    frame_before = layer_frame(layer, stack, resolver, scratch=scratch)
    pivot_before = frame_before.get("pivot") if frame_before else None

    from postprocess.models import layer_image_source

    raw_probe = resolver.resolve(layer_image_source(layer))
    if raw_probe and layer.crop:
        c = layer.crop.clamp_to(raw_probe.width, raw_probe.height)
        if c.x == 0 and c.y == 0 and c.w == raw_probe.width and c.h == raw_probe.height:
            layer.crop = None

    trimmed, meta = trim_layer_alpha_pixels(
        layer,
        resolver,
        alpha_threshold=alpha_threshold,
    )
    layer_trim_changed = False
    if trimmed is None:
        if meta.get("empty"):
            raise HTTPException(400, "图层完全透明，无法裁切")
    else:
        path = resolve_layer_image_path(
            **_pp_layer_write_kwargs(config, asset, inbox, subject_path),
            layer=layer,
        )
        if not path:
            raise HTTPException(404, "图层图片不存在")

        buf = io.BytesIO()
        trimmed.save(buf, format="PNG", optimize=True)
        _write_bytes_atomic(path, buf.getvalue())
        _after_layer_image_write(config, asset, path)
        layer.crop = None
        layer_trim_changed = True
        resolver = _pp_resolver(config, asset, subject_path)
        frame_after = layer_frame(layer, stack, resolver, scratch=scratch)
        if pivot_before and frame_after and frame_after.get("pivot"):
            layer.transform.offset_x += pivot_before["x"] - frame_after["pivot"]["x"]
            layer.transform.offset_y += pivot_before["y"] - frame_after["pivot"]["y"]

    canvas_meta = shrink_stack_canvas_to_content(
        stack,
        resolver,
        alpha_threshold=alpha_threshold,
    )

    if not layer_trim_changed and not canvas_meta.get("changed"):
        return {"status": "unchanged", "unchanged": True}

    invalidate_preview_cache()
    config.set_postprocess_stack(asset.id, stack)
    reload_config_manager()
    if layer_trim_changed and meta.get("old_width"):
        log_msg = (
            f"自适应裁切 {layer.name or layer.id}: {meta.get('old_width')}×{meta.get('old_height')} → "
            f"{meta.get('width')}×{meta.get('height')} ({asset.filename})"
        )
    else:
        log_msg = f"自适应裁切（缩画布） {layer.name or layer.id} ({asset.filename})"
    if canvas_meta.get("changed"):
        log_msg += (
            f"；画布 {canvas_meta.get('old_canvas_width')}×{canvas_meta.get('old_canvas_height')}"
            f" → {canvas_meta.get('canvas_width')}×{canvas_meta.get('canvas_height')}"
        )
    log_bus.log(log_msg, kind="操作")
    result: dict[str, Any] = {
        "status": "ok",
        "width": meta.get("width"),
        "height": meta.get("height"),
        "old_width": meta.get("old_width"),
        "old_height": meta.get("old_height"),
        "canvas": canvas_meta,
        "stack": stack_to_dict(stack),
    }
    if layer_trim_changed:
        path = resolve_layer_image_path(
            **_pp_layer_write_kwargs(config, asset, inbox, subject_path),
            layer=layer,
        )
        if path:
            result["path"] = str(path)
    return result


@router.post("/assets/{asset_id}/auto-trim-inbox")
def auto_trim_inbox(asset_id: str) -> dict[str, Any]:
    """主界面快捷：对 inbox 主体图层做自适应裁切（透明边 + 缩画布）。"""
    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    _ensure_inbox_for_postprocess(config, asset)
    stack = _resolve_postprocess_stack(config, asset, None)
    layer = _subject_image_layer(stack)
    if not layer:
        raise HTTPException(400, "无图片图层可裁切")
    return _apply_auto_trim_stack(config, asset, stack, layer)


@router.get("/assets/{asset_id}/postprocess/subject-raw.png")
def subject_raw_png(asset_id: str, subject: str | None = None) -> Response:
    """主体原图 PNG（裁切编辑器用；subject 决定读 source / inbox / unity）。"""
    import io

    from PIL import Image

    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    if not subject:
        _ensure_inbox_for_postprocess(config, asset)
    path = _resolve_subject_path(config, asset, subject)
    if not path or not path.is_file():
        raise HTTPException(404, "无主体原图")
    if path.suffix.lower() == ".png":
        try:
            data = path.read_bytes()
            if len(data) >= 8:
                return Response(content=data, media_type="image/png")
        except OSError:
            pass
    with Image.open(path) as im:
        rgba = im.convert("RGBA")
    buf = io.BytesIO()
    rgba.save(buf, format="PNG", compress_level=1, optimize=False)
    return Response(content=buf.getvalue(), media_type="image/png")


@router.post("/assets/{asset_id}/postprocess/bake-subject-crop")
def bake_subject_crop_postprocess(asset_id: str, body: dict[str, Any] | None = None) -> dict[str, str]:
    """source/unity 模式：确认裁切后直接写入原图。"""
    from postprocess.matte import is_subject_master_edit_mode, resolve_layer_image_path

    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    body = body or {}
    if not is_subject_master_edit_mode(body.get("subject_path")):
        raise HTTPException(400, "仅 source / unity 编辑模式可直写原图")
    inbox = _ensure_inbox_for_postprocess(config, asset)
    stack = _resolve_postprocess_stack(config, asset, body.get("stack"))
    if not stack:
        raise HTTPException(400, "无效 stack")
    resolver = _pp_resolver(config, asset, body.get("subject_path"))
    subj = stack.subject_layer()
    if not subj or not subj.crop:
        raise HTTPException(400, "主体无裁切区域")
    if not _bake_subject_crop_to_master(config, asset, stack, resolver, body.get("subject_path")):
        raise HTTPException(400, "裁切写入失败")
    path = resolve_layer_image_path(
        **_pp_layer_write_kwargs(config, asset, inbox, body.get("subject_path")),
        layer=subj,
    )
    _after_layer_image_write(config, asset, path)
    if body.get("edit_subject") is not None:
        from postprocess.matte import normalize_edit_subject

        stack.edit_subject = normalize_edit_subject(str(body.get("edit_subject")))
    from postprocess.models import stack_to_dict

    config.set_postprocess_stack(asset_id, stack)
    reload_config_manager()
    return {"status": "ok", "path": str(path) if path else "", "stack": stack_to_dict(stack)}


@router.post("/assets/{asset_id}/postprocess/apply")
def apply_postprocess(
    asset_id: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from postprocess.engine import render_stack_to_png_bytes

    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    body = body or {}
    _ensure_inbox_for_postprocess(config, asset)
    _sync_inbox_before_apply(config, asset, body)
    stack = _resolve_postprocess_stack(config, asset, body.get("stack"))
    if not stack:
        raise HTTPException(400, "无效 stack")
    resolver = _pp_resolver(config, asset, body.get("subject_path"))
    _src, inbox, unity = config.resolve_paths(asset)
    from postprocess.matte import is_subject_master_edit_mode, normalize_edit_subject

    baked_master = False
    size_synced = False
    canvas_written = False
    composed_data: bytes | None = None
    had_subject_crop = _subject_had_crop(stack)
    try:
        if is_subject_master_edit_mode(body.get("subject_path")):
            if _stack_canvas_differs_from_asset(asset, stack):
                _validate_stack_canvas_size(stack)
                composed_data = _write_canvas_render_to_source(
                    config, asset, stack, resolver, body.get("subject_path")
                )
                canvas_written = composed_data is not None
                resolver = _pp_resolver(config, asset, body.get("subject_path"))
                size_synced = _sync_asset_dimensions_from_stack(config, asset, stack)
                baked_master = canvas_written
            else:
                baked_master = _bake_subject_transform_to_master(
                    config, asset, stack, resolver, body.get("subject_path")
                )
                resolver = _pp_resolver(config, asset, body.get("subject_path"))
        elif had_subject_crop:
            _bake_subject_crop_to_inbox(config, asset, stack, resolver, inbox)
            resolver = _pp_resolver(config, asset, body.get("subject_path"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        if composed_data is None:
            composed_data = render_stack_to_png_bytes(stack, resolver)
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc

    edit_key = normalize_edit_subject(body.get("subject_path") or body.get("edit_subject"))
    export_multi = bool(body.get("export_multi_layers")) and not body.get("export_unity")
    export_multi = export_multi and edit_key == "inbox"
    tight_crop = body.get("tight_crop", True) is not False

    if export_multi:
        layer_dir = _inbox_multi_layer_dir(inbox, asset, asset_id)
        try:
            export_info = _export_stack_layers_to_dir(
                stack,
                resolver,
                layer_dir,
                asset_id=asset_id,
                asset_filename=asset.filename or "",
                tight=tight_crop,
                include_hidden=False,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        data = composed_data
    else:
        if not composed_data:
            raise HTTPException(500, "合成结果为空")
        inbox.parent.mkdir(parents=True, exist_ok=True)
        tmp = inbox.with_name(f".{inbox.name}.apply.tmp")
        try:
            tmp.write_bytes(composed_data)
            tmp.replace(inbox)
        finally:
            if tmp.is_file():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        data = composed_data

    if had_subject_crop and not canvas_written:
        _clear_subject_crop(stack)
    if body.get("edit_subject") is not None:
        stack.edit_subject = normalize_edit_subject(str(body.get("edit_subject")))
    elif body.get("subject_path"):
        stack.edit_subject = normalize_edit_subject(str(body.get("subject_path")))
    config.set_postprocess_stack(asset_id, stack)
    reload_config_manager()
    mode = normalize_edit_subject(body.get("subject_path"))
    if (
        not size_synced
        and mode == "inbox"
        and _stack_canvas_differs_from_asset(asset, stack)
    ):
        size_synced = _sync_asset_dimensions_from_stack(config, asset, stack)
        reload_config_manager()
    if canvas_written and mode == "source":
        log_bus.log(
            f"后处理已写入 source（画布 {stack.canvas_width}×{stack.canvas_height}）并更新 inbox: {asset.filename}",
            kind="操作",
        )
    elif baked_master and mode == "source":
        log_bus.log(f"后处理已写入 source 并同步 inbox: {asset.filename}", kind="操作")
    elif baked_master and mode == "unity":
        log_bus.log(f"后处理已写入游戏引擎文件并同步 inbox: {asset.filename}", kind="操作")
    elif export_multi:
        log_bus.log(
            f"多图层导出至 inbox 子文件夹: {layer_dir.name}（{export_info['count']} 层）",
            kind="操作",
        )
    else:
        log_bus.log(f"后处理已写入 inbox: {asset.filename}", kind="操作")
    result: dict[str, Any] = {"path": str(inbox), "bytes": len(data) if data else 0}
    if export_multi:
        result["path"] = str(layer_dir)
        result["multi_layer_dir"] = str(layer_dir)
        result["multi_layer_count"] = export_info["count"]
        result["manifest"] = export_info["manifest"]
    if size_synced:
        result["width"] = int(asset.width)
        result["height"] = int(asset.height)
        result["size_label"] = asset.size_label()
    if baked_master:
        src, _inbox, unity = config.resolve_paths(asset)
        if mode == "source" and src.is_file():
            result["source_path"] = str(src)
        elif mode == "unity" and unity.is_file():
            result["unity_path"] = str(unity)
        result["master_baked"] = True
    if body.get("export_unity"):
        from pipeline_core import PipelineCore

        if pipeline_runner.is_busy():
            raise HTTPException(409, "任务进行中")
        pipeline = PipelineCore(config)
        ok = pipeline.export_one(asset, log=lambda m: log_bus.log(m, kind="操作"))
        if not ok:
            raise HTTPException(400, f"无法导出 Unity：inbox 不存在 ({inbox.name})")
        result["unity_path"] = str(unity)
        log_bus.log(f"已导出 Unity: {asset.filename}", kind="操作")
    return result


@router.post("/assets/{asset_id}/postprocess/smart-split")
def smart_split_postprocess(asset_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """智能拆分：按 alpha 连通域将主体图集拆为多个图层。"""
    from postprocess.models import stack_to_dict
    from postprocess.split import smart_split_subject_layer

    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    _ensure_inbox_for_postprocess(config, asset)
    _sync_inbox_before_render(config, asset, body)
    stack = _resolve_postprocess_stack(config, asset, body.get("stack"))
    if not stack:
        raise HTTPException(400, "无效 stack")
    resolver = _pp_resolver(config, asset, body.get("subject_path"))
    subj = stack.subject_layer()
    if not subj:
        raise HTTPException(400, "未找到主体图层")
    subj_idx = stack.layers.index(subj)

    art_root = config.art_root()
    layers_dir = art_root / "postprocess" / "layers"
    try:
        new_layers, meta = smart_split_subject_layer(
            stack,
            resolver,
            layers_dir=layers_dir,
            alpha_threshold=int(body.get("alpha_threshold", 8)),
            min_area=int(body.get("min_area", 32)),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    for i, layer in enumerate(new_layers):
        stack.layers.insert(subj_idx + 1 + i, layer)

    config.set_postprocess_stack(asset_id, stack)
    reload_config_manager()
    log_bus.log(
        f"智能拆分: {meta['count']} 个图层（{asset.filename}）",
        kind="操作",
    )
    return {
        "stack": stack_to_dict(stack),
        "count": meta["count"],
        "new_layer_ids": [layer.id for layer in new_layers],
        "regions": meta.get("regions", []),
    }


@router.post("/assets/{asset_id}/postprocess/merge-layers")
def merge_postprocess_layers(asset_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """将多个图层合并为一个图片图层。"""
    from postprocess.merge import merge_stack_layers
    from postprocess.models import stack_to_dict

    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    _ensure_inbox_for_postprocess(config, asset)
    _sync_inbox_before_render(config, asset, body)
    stack = _resolve_postprocess_stack(config, asset, body.get("stack"))
    if not stack:
        raise HTTPException(400, "无效 stack")
    layer_ids = body.get("layer_ids") or []
    if not isinstance(layer_ids, list) or len(layer_ids) < 2:
        raise HTTPException(400, "请至少选择两个图层")

    resolver = _pp_resolver(config, asset, body.get("subject_path"))
    art_root = config.art_root()
    layers_dir = art_root / "postprocess" / "layers"
    removed_ids = [str(lid) for lid in layer_ids if lid]
    try:
        new_layer = merge_stack_layers(
            stack,
            resolver,
            removed_ids,
            layers_dir=layers_dir,
            tight=body.get("tight", True) is not False,
            merged_name=str(body.get("merged_name") or "").strip() or None,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    config.set_postprocess_stack(asset_id, stack)
    reload_config_manager()
    log_bus.log(
        f"合并图层: {new_layer.name} ← {len(removed_ids)} 层（{asset.filename}）",
        kind="操作",
    )
    return {
        "stack": stack_to_dict(stack),
        "new_layer_id": new_layer.id,
        "removed_layer_ids": removed_ids,
        "merged_name": new_layer.name,
    }


@router.post("/assets/{asset_id}/postprocess/export-merged")
def export_postprocess_merged(asset_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """合并导出：将全部可见图层合成一张 PNG。"""
    from postprocess.engine import render_stack_to_png_bytes

    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    _ensure_inbox_for_postprocess(config, asset)
    _sync_inbox_before_render(config, asset, body)
    stack = _resolve_postprocess_stack(config, asset, body.get("stack"))
    if not stack:
        raise HTTPException(400, "无效 stack")
    resolver = _pp_resolver(config, asset, body.get("subject_path"))
    try:
        data = render_stack_to_png_bytes(stack, resolver)
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
    if not data:
        raise HTTPException(500, "合成结果为空")

    stem = Path(asset.filename or asset_id).stem
    default_name = f"{stem}_merged.png"
    output_path: Path | None = None
    if body.get("output_path"):
        output_path = Path(str(body["output_path"])).expanduser()
    elif body.get("pick_path", True):
        initial = _resolve_pick_initial_dir(config, body.get("initial_dir"))
        picked = _pick_save_png_path(
            initial,
            default_name,
            str(body.get("title") or "保存合成图"),
        )
        if not picked:
            return {"cancelled": True}
        output_path = picked
    else:
        output_path = _default_export_dir(config, asset) / default_name

    if output_path.suffix.lower() != ".png":
        output_path = output_path.with_suffix(".png")
    _write_bytes_atomic(output_path, data)
    log_bus.log(f"合并导出: {output_path.name} ({asset.filename})", kind="操作")
    art_root = config.art_root()
    return {
        "cancelled": False,
        "path": str(output_path.resolve()),
        "relative": _rel_to_art_root(output_path, art_root),
        "bytes": len(data),
        "width": stack.canvas_width,
        "height": stack.canvas_height,
    }


@router.post("/assets/{asset_id}/postprocess/export-layers")
def export_postprocess_layers(asset_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """多图层导出：各可见图层单独 PNG（默认 tight bbox），并写入 manifest.json。"""
    from postprocess.engine import render_layer_export_png_bytes

    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    _ensure_inbox_for_postprocess(config, asset)
    _sync_inbox_before_render(config, asset, body)
    stack = _resolve_postprocess_stack(config, asset, body.get("stack"))
    if not stack:
        raise HTTPException(400, "无效 stack")
    resolver = _pp_resolver(config, asset, body.get("subject_path"))

    include_hidden = bool(body.get("include_hidden"))
    tight_crop = body.get("tight_crop", True) is not False

    output_dir: Path | None = None
    if body.get("output_dir"):
        output_dir = Path(str(body["output_dir"])).expanduser()
    elif body.get("pick_dir", True):
        initial = _resolve_pick_initial_dir(config, body.get("initial_dir"))
        picked = _pick_output_dir(initial, str(body.get("title") or "选择导出文件夹"))
        if not picked:
            return {"cancelled": True}
        output_dir = picked
    else:
        output_dir = _default_export_dir(config, asset)

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(asset.filename or asset_id).stem
    subdir = output_dir / f"{stem}_layers"
    subdir.mkdir(parents=True, exist_ok=True)

    exported: list[dict[str, Any]] = []
    used_names: dict[str, int] = {}
    for layer in stack.layers:
        if not include_hidden and layer.visible is False:
            continue
        try:
            data, meta = render_layer_export_png_bytes(
                stack,
                resolver,
                layer.id,
                tight=bool(tight_crop),
            )
        except Exception as exc:
            log_bus.log(f"图层导出跳过 {layer.name}: {exc}", kind="系统")
            continue
        if not data or meta.get("width", 0) <= 0 or meta.get("height", 0) <= 0:
            continue
        base = _safe_export_stem(layer.name or "", layer.id)
        count = used_names.get(base, 0)
        used_names[base] = count + 1
        fname = f"{base}.png" if count == 0 else f"{base}_{count + 1}.png"
        out_path = subdir / fname
        _write_bytes_atomic(out_path, data)
        exported.append(
            {
                "id": layer.id,
                "name": layer.name,
                "type": layer.type,
                "file": fname,
                "path": str(out_path.resolve()),
                "offset_x": meta["offset_x"],
                "offset_y": meta["offset_y"],
                "width": meta["width"],
                "height": meta["height"],
            }
        )

    if not exported:
        raise HTTPException(400, "没有可导出的可见图层")

    manifest = {
        "asset_id": asset_id,
        "filename": asset.filename,
        "canvas_width": stack.canvas_width,
        "canvas_height": stack.canvas_height,
        "tight_crop": bool(tight_crop),
        "layers": exported,
    }
    manifest_path = subdir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    log_bus.log(
        f"多图层导出: {len(exported)} 个文件 → {subdir.name} ({asset.filename})",
        kind="操作",
    )
    art_root = config.art_root()
    return {
        "cancelled": False,
        "dir": str(subdir.resolve()),
        "relative": _rel_to_art_root(subdir, art_root),
        "count": len(exported),
        "layers": exported,
        "manifest": str(manifest_path.resolve()),
    }


@router.post("/assets/{asset_id}/export-unity")
def export_asset_unity(asset_id: str) -> dict[str, Any]:
    from pipeline_core import PipelineCore

    if pipeline_runner.is_busy():
        raise HTTPException(409, "任务进行中")
    config = get_config_manager()
    asset = config.asset_by_id(asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {asset_id}")
    _src, inbox, unity = config.resolve_paths(asset)
    pipeline = PipelineCore(config)
    ok = pipeline.export_one(asset, log=lambda m: log_bus.log(m, kind="操作"))
    if not ok:
        raise HTTPException(400, f"无法导出：inbox 不存在 ({inbox.name})")
    log_bus.log(f"已导出 Unity: {asset.filename}", kind="操作")
    return {"status": "ok", "path": str(unity)}


@router.get("/postprocess/templates")
def postprocess_templates() -> dict[str, Any]:
    from postprocess.templates import BUILTIN_TEMPLATES

    return {"templates": list(BUILTIN_TEMPLATES.keys())}


@router.get("/postprocess/templates/{template_id}")
def postprocess_template(template_id: str, width: int = 512, height: int = 512) -> dict[str, Any]:
    from postprocess.models import stack_to_dict
    from postprocess.templates import builtin_template

    stack = builtin_template(template_id, width, height)
    return {"stack": stack_to_dict(stack)}


@router.get("/postprocess/fonts")
def postprocess_fonts() -> dict[str, Any]:
    from postprocess.fonts import list_system_fonts

    return {"fonts": list_system_fonts()[:120]}


@router.post("/pick-image-file")
def pick_image_file(body: PickImageFileBody | None = None) -> dict[str, Any]:
    config = get_config_manager()
    art_root = config.art_root()
    initial = _resolve_pick_initial_dir(config, body.initial_dir if body else None)
    picked = _pick_image_path(initial)
    if not picked:
        return {"cancelled": True}
    if not picked.is_file():
        raise HTTPException(400, "所选路径不是文件")
    resolved = picked.resolve()
    return {
        "cancelled": False,
        "path": str(resolved),
        "absolute": str(resolved),
        "relative": _rel_to_art_root(resolved, art_root),
    }


@router.post("/pick-save-png")
def pick_save_png(body: PickSavePngBody | None = None) -> dict[str, Any]:
    config = get_config_manager()
    art_root = config.art_root()
    body = body or PickSavePngBody()
    initial = _resolve_pick_initial_dir(config, body.initial_dir)
    picked = _pick_save_png_path(initial, body.default_name, body.title or "保存 PNG")
    if not picked:
        return {"cancelled": True}
    return {
        "cancelled": False,
        "path": str(picked.resolve()),
        "relative": _rel_to_art_root(picked, art_root),
    }


@router.post("/pick-output-dir")
def pick_output_dir(body: PickOutputDirBody | None = None) -> dict[str, Any]:
    config = get_config_manager()
    art_root = config.art_root()
    body = body or PickOutputDirBody()
    initial = _resolve_pick_initial_dir(config, body.initial_dir)
    picked = _pick_output_dir(initial, body.title or "选择文件夹")
    if not picked:
        return {"cancelled": True}
    rel_base = (body.relative_base or "art").strip().lower()
    relative = ""
    if rel_base == "art":
        relative = _rel_to_art_root(picked, art_root)
    elif rel_base == "project":
        relative = config.rel_to_project(picked)
    return {
        "cancelled": False,
        "path": str(picked.resolve()),
        "relative": relative or str(picked.resolve()),
    }


@router.post("/validate-path")
def validate_path_endpoint(body: ValidatePathBody) -> dict[str, Any]:
    config = get_config_manager()
    raw = (body.path or "").strip()
    if not raw:
        if body.optional:
            return {"valid": True, "empty": True, "exists": False}
        return {"valid": False, "empty": True, "message": "路径不能为空"}

    kind = (body.kind or "dir").strip().lower()
    base = (body.base or "absolute").strip().lower()
    try:
        resolved = _resolve_user_path(config, raw, base)
    except (ValueError, OSError) as exc:
        return {"valid": False, "message": str(exc) or "路径格式无效"}

    exists = resolved.exists()
    if kind == "dir":
        ok = resolved.is_dir() if exists else True
        wrong_type = exists and not resolved.is_dir()
    elif kind == "file":
        ok = resolved.is_file() if exists else True
        wrong_type = exists and not resolved.is_file()
    else:
        ok = True
        wrong_type = False

    if wrong_type:
        msg = "路径存在但不是文件夹" if kind == "dir" else "路径存在但不是文件"
        return {
            "valid": False,
            "exists": True,
            "resolved": str(resolved),
            "message": msg,
        }

    return {
        "valid": ok,
        "exists": exists,
        "resolved": str(resolved),
    }


@router.post("/postprocess/upload-image")
async def upload_postprocess_image(file: UploadFile = File(...)) -> dict[str, str]:
    config = get_config_manager()
    art_root = config.art_root()
    dest_dir = art_root / "postprocess" / "layers"
    dest_dir.mkdir(parents=True, exist_ok=True)

    raw_name = Path(file.filename or "image.png").name
    suffix = Path(raw_name).suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        suffix = ".png"
    dest = dest_dir / f"{uuid.uuid4().hex[:12]}{suffix}"

    try:
        content = await file.read()
        if not content:
            raise HTTPException(400, "空文件")
        dest.write_bytes(content)
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(500, f"写入失败: {exc}") from exc

    rel = dest.relative_to(art_root.resolve()).as_posix()
    log_bus.log(f"已导入后处理图片: {rel}", kind="操作")
    return {"path": rel}


# ── AI ─────────────────────────────────────────────

AI_MODES = ("free", "prompt", "refine", "workflow", "basic", "category")


def _ai_histories_path() -> Path:
    root = user_data_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root / "ai_chat_histories.json"


def _load_ai_histories() -> dict[str, list[dict[str, Any]]]:
    path = _ai_histories_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for key, items in raw.items():
        if not isinstance(key, str) or not isinstance(items, list):
            continue
        cleaned: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = item.get("content")
            if role not in ("user", "assistant") or not isinstance(content, str):
                continue
            entry: dict[str, Any] = {"role": role, "content": content}
            if item.get("failed"):
                entry["failed"] = True
                retry = item.get("retry_message")
                if isinstance(retry, str):
                    entry["retry_message"] = retry
            cleaned.append(entry)
        if cleaned:
            out[key] = cleaned
    return out


def _save_ai_histories() -> None:
    path = _ai_histories_path()
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps(_ai_histories, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError:
        pass


_ai_histories: dict[str, list[dict[str, Any]]] = _load_ai_histories()


def _ai_append_failed_turn(hist: list[dict[str, Any]], user_text: str, error: str) -> None:
    hist.append({"role": "user", "content": user_text})
    hist.append(
        {
            "role": "assistant",
            "content": error,
            "failed": True,
            "retry_message": user_text,
        }
    )

_CAT_SETTING_KEYS = (
    "source",
    "inbox",
    "unity",
    "checkpoint",
    "alpha_matte",
    "positive_common",
    "negative_common",
)


def _ai_hist_key(asset_id: str, mode: str) -> str:
    m = mode if mode in AI_MODES else "free"
    return f"{asset_id}:{m}"


@router.get("/ai/history/{asset_id}")
def ai_history(asset_id: str, mode: str = "free") -> dict[str, Any]:
    return {"history": _ai_histories.get(_ai_hist_key(asset_id, mode), [])}


@router.delete("/ai/history/{asset_id}")
def ai_clear(asset_id: str, mode: str | None = None) -> dict[str, str]:
    if mode and mode in AI_MODES:
        _ai_histories.pop(_ai_hist_key(asset_id, mode), None)
    else:
        for m in AI_MODES:
            _ai_histories.pop(_ai_hist_key(asset_id, m), None)
        _ai_histories.pop(asset_id, None)
    _save_ai_histories()
    return {"status": "cleared"}


def _apply_ai_basic_updates(config: Any, asset: Any, updates: dict[str, Any], applied: list[str]) -> None:
    from ai_assistant import AiAssistantError, _ai_bool, _ai_update_present

    if _ai_update_present(updates.get("filename")):
        fn = str(updates["filename"]).strip()
        if not fn.lower().endswith(".png"):
            fn = f"{fn}.png" if fn else asset.filename
        asset.filename = fn
        applied.append("filename")
    if _ai_update_present(updates.get("category")):
        cat_id = str(updates["category"]).strip()
        if not config.category_by_id(cat_id):
            raise HTTPException(400, f"AI 返回未知分类: {cat_id}")
        asset.category = cat_id
        applied.append("category")
    if _ai_update_present(updates.get("width")):
        w = int(updates["width"])
        if w < 32 or w > 4096:
            raise HTTPException(400, "AI 返回 width 须在 32–4096")
        asset.width = w
        applied.append("width")
    if _ai_update_present(updates.get("height")):
        h = int(updates["height"])
        if h < 32 or h > 4096:
            raise HTTPException(400, "AI 返回 height 须在 32–4096")
        asset.height = h
        applied.append("height")
    if "seed" in updates and updates.get("seed") is not None:
        asset.seed = str(updates["seed"]).strip()
        applied.append("seed")
    if _ai_update_present(updates.get("enabled")):
        try:
            asset.enabled = _ai_bool(updates["enabled"])
        except AiAssistantError as exc:
            raise HTTPException(400, str(exc)) from exc
        applied.append("enabled")
    if _ai_update_present(updates.get("remove_bg_mode")):
        mode = str(updates["remove_bg_mode"]).strip().lower()
        if mode not in ("inherit", "remove", "keep"):
            raise HTTPException(400, "remove_bg_mode 须为 inherit / remove / keep")
        asset.remove_bg_mode = mode
        applied.append("remove_bg_mode")
    if _ai_update_present(updates.get("checkpoint")):
        asset.checkpoint = str(updates["checkpoint"]).strip()
        applied.append("checkpoint")
    if _ai_update_present(updates.get("subject")):
        asset.subject = str(updates["subject"]).strip()
        applied.append("subject")


def _apply_ai_prompt_updates(config: Any, asset: Any, updates: dict[str, Any], applied: list[str]) -> None:
    from ai_assistant import (
        collect_cloud_negative_update,
        collect_cloud_prompt_update,
        updates_intent_cloud_prompt,
        _ai_field_set,
    )
    from cloud.registry import is_cloud_checkpoint

    is_cloud = is_cloud_checkpoint(config.checkpoint_for_asset(asset))
    cloud_intent = updates_intent_cloud_prompt(updates)
    if is_cloud or cloud_intent:
        prompt = collect_cloud_prompt_update(updates)
        if prompt is not None:
            asset.cloud_prompt = prompt
            applied.append("cloud_prompt")
        negative = collect_cloud_negative_update(updates)
        if negative is not None:
            asset.cloud_negative = negative
            applied.append("cloud_negative")
        return

    for key in (
        "positive_prefix",
        "positive_subject",
        "positive_scene",
        "positive_light",
        "positive_g",
        "positive_l",
        "positive",
        "negative",
        "subject",
    ):
        if _ai_field_set(updates, key):
            setattr(asset, key, str(updates[key]).strip())
            applied.append(key)
    config.sync_asset_prompt_fields(asset)


def _apply_ai_gen_updates(asset: Any, updates: dict[str, Any], applied: list[str]) -> None:
    from ai_assistant import _ai_update_present

    if _ai_update_present(updates.get("gen_mode")):
        mode = str(updates["gen_mode"]).strip().lower()
        if mode not in ("txt2img", "img2img", "redraw"):
            raise HTTPException(400, "gen_mode 须为 txt2img / img2img / redraw")
        asset.gen_mode = mode
        asset.ref_image_use_source = False
        if mode == "redraw":
            asset.ref_image = ""
        applied.append("gen_mode")
    if _ai_update_present(updates.get("ref_image")):
        asset.ref_image = str(updates["ref_image"]).strip()
        applied.append("ref_image")
    if _ai_update_present(updates.get("img2img_denoise")):
        d = float(updates["img2img_denoise"])
        if d < 0.01 or d > 1.0:
            raise HTTPException(400, "img2img_denoise 须在 0.01–1.0")
        asset.img2img_denoise = d
        applied.append("img2img_denoise")


def _apply_ai_category_updates(cat: Any, cat_updates: dict[str, Any], applied: list[str]) -> bool:
    from ai_assistant import _ai_field_set, _ai_update_present

    if not cat or not isinstance(cat_updates, dict):
        return False
    changed = False
    for key in _CAT_SETTING_KEYS:
        if not _ai_field_set(cat_updates, key) and not _ai_update_present(cat_updates.get(key)):
            continue
        val = cat_updates[key]
        if key == "alpha_matte":
            s = str(val).strip().lower()
            if s in ("default", "border", "on", "true", "yes", "1"):
                val = "border"
            elif s in ("none", "off", "false", "no", "0"):
                val = "none"
            else:
                val = str(val).strip()
        elif key in ("positive_common", "negative_common"):
            val = str(val)
        else:
            val = str(val).strip()
        setattr(cat, key, val)
        applied.append(f"cat.{key}")
        changed = True
    return changed


def _apply_ai_workflow_update(config: Any, asset: Any, updates: dict[str, Any], applied: list[str]) -> None:
    import json

    from paths import WORKFLOWS_DIR

    wf = updates.get("workflow")
    if not wf or not isinstance(wf, dict):
        return
    wf_path = WORKFLOWS_DIR / "assets" / f"{asset.id}.json"
    wf_path.parent.mkdir(parents=True, exist_ok=True)
    wf_path.write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    asset.workflow = f"workflows/assets/{asset.id}.json"
    applied.append("workflow")


def _apply_all_ai_updates(
    config: Any,
    asset: Any,
    updates: dict[str, Any],
    applied: list[str],
) -> bool:
    """将 AI updates 中所有非 null 字段写入内存；返回分类是否有改动。"""
    _apply_ai_basic_updates(config, asset, updates, applied)
    _apply_ai_prompt_updates(config, asset, updates, applied)
    _apply_ai_gen_updates(asset, updates, applied)
    _apply_ai_workflow_update(config, asset, updates, applied)
    target_cat = config.category_by_id(asset.category)
    cat_settings = updates.get("category_settings")
    if target_cat and isinstance(cat_settings, dict):
        return _apply_ai_category_updates(target_cat, cat_settings, applied)
    return False


def _ai_context_for_asset(config: Any, asset: Any) -> str:
    from ai_assistant import build_context_message
    from cloud.registry import is_cloud_checkpoint

    cat = config.category_by_id(asset.category)
    wf_path = config.workflow_file_for_asset(asset)
    wf_summary = wf_path.name if wf_path.is_file() else "默认"
    cat_opts = ", ".join(f"{c.id}({c.label})" for c in config.categories())
    eff_ckpt = config.checkpoint_for_asset(asset)
    cloud_label = ""
    if is_cloud_checkpoint(eff_ckpt):
        from cloud.registry import get_model

        m = get_model(eff_ckpt)
        if m:
            cloud_label = str(m.get("label_zh") or m.get("label_en") or "")
    return build_context_message(
        asset_id=asset.id,
        filename=asset.filename,
        category=asset.category,
        category_label=cat.label if cat else asset.category,
        width=asset.width,
        height=asset.height,
        subject=asset.subject,
        positive=asset.positive,
        negative=asset.negative,
        positive_prefix=getattr(asset, "positive_prefix", ""),
        positive_subject=getattr(asset, "positive_subject", ""),
        positive_scene=getattr(asset, "positive_scene", ""),
        positive_light=getattr(asset, "positive_light", ""),
        positive_g=asset.positive_g,
        positive_l=asset.positive_l,
        workflow_summary=wf_summary,
        seed=asset.seed or "",
        enabled=asset.enabled,
        remove_bg_mode=asset.remove_bg_mode,
        category_options=cat_opts,
        category_source=cat.source if cat else "",
        category_inbox=cat.inbox if cat else "",
        category_unity=cat.unity if cat else "",
        category_checkpoint=cat.checkpoint if cat else "",
        category_alpha_matte=cat.alpha_matte if cat else "",
        category_positive_common=cat.positive_common if cat else "",
        category_negative_common=cat.negative_common if cat else "",
        asset_checkpoint=getattr(asset, "checkpoint", "") or "",
        effective_checkpoint=eff_ckpt,
        is_cloud_model=is_cloud_checkpoint(eff_ckpt),
        cloud_prompt=getattr(asset, "cloud_prompt", ""),
        cloud_negative=getattr(asset, "cloud_negative", ""),
        cloud_gen_mode=getattr(asset, "cloud_gen_mode", "text_to_image"),
        cloud_strength=float(getattr(asset, "cloud_strength", 0.65)),
        cloud_model_label=cloud_label,
        gen_mode=getattr(asset, "gen_mode", "txt2img"),
        ref_image=getattr(asset, "ref_image", ""),
        img2img_denoise=float(getattr(asset, "img2img_denoise", 0.65)),
    )


@router.post("/ai/verify")
def ai_verify(body: dict[str, Any]) -> dict[str, Any]:
    """兼容旧前端；与 POST /api/cloud/verify provider=deepseek 相同。"""
    return cloud_verify(
        {
            "provider": "deepseek",
            "api_key": body.get("api_key"),
            "model": body.get("model"),
            "keys": body.get("keys") if isinstance(body.get("keys"), dict) else {},
        }
    )


@router.post("/ai/chat")
def ai_chat(body: AiChatBody) -> dict[str, Any]:
    from ai_assistant import AiAssistantError, ai_mode_prefix, chat, parse_ai_response
    from cloud.registry import is_cloud_checkpoint

    config = get_config_manager()
    asset = config.asset_by_id(body.asset_id)
    if not asset:
        raise HTTPException(404, f"资源不存在: {body.asset_id}")
    d = config.defaults
    api_key = str(d.get("deepseek_api_key", ""))
    model = str(d.get("deepseek_model", ""))
    mode = body.mode if body.mode in AI_MODES else "free"
    is_cloud = is_cloud_checkpoint(config.checkpoint_for_asset(asset))
    mode_prefix = ai_mode_prefix(mode, is_cloud_model=is_cloud)
    user_text = body.message.strip()
    if not user_text:
        raise HTTPException(400, "消息不能为空")
    ctx = _ai_context_for_asset(config, asset)
    hist_key = _ai_hist_key(body.asset_id, mode)
    hist = _ai_histories.setdefault(hist_key, [])
    messages = [{"role": "system", "content": __import__("ai_assistant").SYSTEM_PROMPT}]
    messages.append({"role": "user", "content": ctx})
    for item in hist[-10:]:
        messages.append({"role": item["role"], "content": item["content"]})
    messages.append({"role": "user", "content": f"{mode_prefix}{user_text}"})
    try:
        raw = chat(messages, api_key=api_key, model=model)
        message, updates = parse_ai_response(raw)
    except AiAssistantError as exc:
        _ai_append_failed_turn(hist, user_text, str(exc))
        _save_ai_histories()
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        _ai_append_failed_turn(hist, user_text, f"AI 请求失败: {exc}")
        _save_ai_histories()
        raise HTTPException(502, f"AI 请求失败: {exc}") from exc
    hist.append({"role": "user", "content": user_text})
    hist.append({"role": "assistant", "content": message})
    _save_ai_histories()
    applied: list[str] = []
    cat_dirty = _apply_all_ai_updates(config, asset, updates, applied)
    asset_dirty = any(not k.startswith("cat.") for k in applied)
    if asset_dirty:
        config.update_asset(asset)
    if cat_dirty:
        target_cat = config.category_by_id(asset.category)
        if target_cat:
            config.update_category(target_cat)
            config.ensure_category_dirs(target_cat)
    if asset_dirty or cat_dirty:
        reload_config_manager()
        log_bus.log(
            f"AI 已自动保存资源 {asset.id}（{', '.join(applied)}）",
            kind="操作",
        )
    elif updates:
        log_bus.log(
            f"AI 未写入配置 {asset.id}（返回字段: {', '.join(updates.keys())}）",
            kind="系统",
        )
    return {
        "message": message,
        "applied": applied,
        "updates": updates,
        "saved": bool(applied),
        "is_cloud_backend": is_cloud,
    }


# ── System open ─────────────────────────────────────────────


@router.post("/open-path")
def open_path(body: OpenPathBody) -> dict[str, str]:
    p = Path(body.path).expanduser()
    if not p.exists():
        raise HTTPException(404, "路径不存在")
    subprocess.run(["open", str(p)], check=False)
    return {"status": "ok"}


@router.post("/open-url")
def open_url(body: OpenUrlBody) -> dict[str, str]:
    from cloud.registry import allowed_api_portal_urls

    url = str(body.url or "").strip()
    if not url:
        raise HTTPException(400, "url 为空")
    if url not in allowed_api_portal_urls():
        raise HTTPException(400, "不允许打开该链接")
    webbrowser.open(url, new=2)
    return {"status": "ok"}


@router.get("/categories/{cat_id}/dir/{kind}")
def category_dir(cat_id: str, kind: str) -> dict[str, str]:
    config = get_config_manager()
    cat = config.category_by_id(cat_id)
    if not cat:
        raise HTTPException(404, f"未知分类: {cat_id}")
    if kind == "source":
        p = config.category_source_path(cat)
    elif kind == "inbox":
        p = config.category_inbox_path(cat)
    elif kind == "unity":
        p = config.category_unity_path(cat)
    else:
        raise HTTPException(400, "kind 须为 source/inbox/unity")
    return {"path": str(p)}
