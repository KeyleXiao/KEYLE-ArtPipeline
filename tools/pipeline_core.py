#!/usr/bin/env python3
"""美术资源生成、导出、ComfyUI 调度核心。"""

from __future__ import annotations

import io
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from comfyui_client import ComfyUiClient, ComfyUiError, ProgressCallback, check_connection
from config_manager import Asset, ConfigManager, GEN_MODE_REDRAW, GEN_MODES_IMG2IMG, effective_gen_mode
from paths import WORKFLOWS_DIR
from workflow_engine import build_workflow, load_workflow_template

try:
    from alpha_matte import apply_alpha_matte_png
except ImportError:
    apply_alpha_matte_png = None  # type: ignore[misc, assignment]


@dataclass
class GenerateResult:
    asset_id: str
    ok: bool
    message: str
    source: Path | None = None


LogFn = Callable[[str], None]


def _log(fn: LogFn | None, msg: str) -> None:
    if fn:
        fn(msg)
    else:
        print(msg)


def _estimate_comfy_seconds(gen_w: int, gen_h: int, steps: int) -> int:
    """按本机 MPS 历史耗时估算 ComfyUI 采样秒数（512²@35步≈40s，1024²≈3.5min）。"""
    pixels = max(1, int(gen_w) * int(gen_h))
    base = 52.0 * (pixels / (512 * 512)) * (max(int(steps), 1) / 35.0)
    return max(30, int(base))


def resolve_checkpoint(requested: str, available: list[str]) -> str:
    if not available:
        raise ComfyUiError("ComfyUI 未返回任何 checkpoint")
    if requested in available:
        return requested
    req = requested.lower().replace(" ", "").replace("_", "").replace("-", "")
    for name in available:
        norm = name.lower().replace(" ", "").replace("_", "").replace("-", "")
        if req in norm or norm in req:
            return name
    for name in available:
        if "animagine" in name.lower():
            return name
    return available[0]


def _resize_png(data: bytes, width: int, height: int) -> bytes:
    from PIL import Image

    im = Image.open(io.BytesIO(data)).convert("RGBA")
    if im.size != (width, height):
        im = im.resize((width, height), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


class PipelineCore:
    def __init__(self, config: ConfigManager | None = None) -> None:
        self.config = config or ConfigManager()
        self.config.ensure_all_dirs()
        self.cancel_event = threading.Event()

    def request_cancel(self) -> None:
        self.cancel_event.set()
        try:
            url = self.config.defaults.get("comfyui_url", "http://127.0.0.1:8188")
            ComfyUiClient(url).interrupt()
        except ComfyUiError:
            pass

    def clear_cancel(self) -> None:
        self.cancel_event.clear()

    def test_comfyui(self) -> tuple[bool, str]:
        url = self.config.defaults.get("comfyui_url", "http://127.0.0.1:8188")
        return check_connection(url)

    def list_checkpoints(self) -> list[str]:
        url = self.config.defaults.get("comfyui_url", "http://127.0.0.1:8188")
        return ComfyUiClient(url).list_checkpoints()

    def generate_one(
        self,
        asset: Asset,
        *,
        seed: int | None = None,
        to_inbox: bool = True,
        log: LogFn | None = None,
        progress_cb: ProgressCallback | None = None,
    ) -> GenerateResult:
        d = self.config.defaults
        cat_overrides = d.get("category_overrides", {}).get(asset.category, {})
        url = d.get("comfyui_url", "http://127.0.0.1:8188")
        client = ComfyUiClient(url)
        ckpts = client.list_checkpoints()
        requested = self.config.checkpoint_for_asset(asset)
        if not requested.strip():
            return GenerateResult(
                asset.id,
                False,
                "未配置 checkpoint：请在「分类设置」或「基本信息」中选择模型",
            )
        ckpt = resolve_checkpoint(requested, ckpts)
        lora_name, lora_strength = self.config.lora_for_category(asset.category)
        steps = int(cat_overrides.get("steps", d.get("steps", 35)))
        cfg = float(cat_overrides.get("cfg", d.get("cfg", 7.0)))

        gen_w = int(asset.width * asset.gen_scale)
        gen_h = int(asset.height * asset.gen_scale)
        output_w = asset.width
        output_h = asset.height
        seed = self.config.resolve_seed_for_asset(asset, override=seed)
        prefix = Path(asset.filename).stem

        gen_mode = effective_gen_mode(asset)
        ref_image_name = ""
        denoise = 1.0
        if gen_mode in GEN_MODES_IMG2IMG:
            ref_path = self.config.resolve_ref_image_path(asset)
            if not ref_path:
                if gen_mode == GEN_MODE_REDRAW:
                    return GenerateResult(asset.id, False, "重绘图：inbox 与 source 参考图均不存在")
                return GenerateResult(asset.id, False, "图生图：参考图不存在或未配置路径")
            if gen_mode == GEN_MODE_REDRAW:
                gen_w, gen_h = self.config.resolve_redraw_gen_size(asset, ref_path)
                output_w, output_h = gen_w, gen_h
                src, _, _ = self.config.resolve_paths(asset)
                dim_note = (
                    f"source {self.config.rel_to_project(src)}"
                    if src.is_file()
                    else self.config.rel_to_project(ref_path)
                )
                _log(log, f"  重绘图尺寸 {gen_w}×{gen_h}（{dim_note}）")
            ref_bytes = ref_path.read_bytes()
            try:
                from PIL import Image

                with Image.open(io.BytesIO(ref_bytes)) as im:
                    ref_size = im.size
            except OSError:
                ref_size = None
            if ref_size != (gen_w, gen_h):
                ref_bytes = _resize_png(ref_bytes, gen_w, gen_h)
                _log(
                    log,
                    f"  参考图 {self.config.rel_to_project(ref_path)} "
                    f"{ref_size[0] if ref_size else '?'}×{ref_size[1] if ref_size else '?'} "
                    f"→ {gen_w}×{gen_h}",
                )
            else:
                _log(log, f"  参考图 {self.config.rel_to_project(ref_path)} ({gen_w}×{gen_h})")
            import tempfile

            tmp_ref = Path(tempfile.mktemp(suffix=".png", prefix="artpipe_ref_"))
            try:
                tmp_ref.write_bytes(ref_bytes)
                ref_image_name = client.upload_image(tmp_ref)
            finally:
                try:
                    tmp_ref.unlink(missing_ok=True)
                except OSError:
                    pass
            denoise = float(getattr(asset, "img2img_denoise", 0.65) or 0.65)
            denoise = max(0.01, min(1.0, denoise))
            denoise_subject = max(0.15, min(0.85, denoise))
            wf_path = self.config.img2img_workflow_for_asset(asset)
        else:
            wf_path = self.config.workflow_file_for_asset(asset)
            denoise = 1.0
            raw_d = float(getattr(asset, "img2img_denoise", 0.52) or 0.52)
            denoise_subject = max(0.15, min(0.85, raw_d))

        template = load_workflow_template(wf_path)
        prompts = self.config.prompts_for_generation(asset)
        workflow = build_workflow(
            template,
            positive=prompts["positive"],
            negative=prompts["negative"],
            positive_g=prompts["positive_g"],
            positive_l=prompts["positive_l"],
            positive_prefix=prompts.get("positive_prefix", ""),
            positive_subject=prompts.get("positive_subject", ""),
            positive_scene=prompts.get("positive_scene", ""),
            positive_light=prompts.get("positive_light", ""),
            positive_scene_bg=prompts.get("positive_scene_bg", prompts["positive"]),
            positive_subject_fg=prompts.get("positive_subject_fg", prompts["positive"]),
            width=gen_w,
            height=gen_h,
            seed=seed,
            checkpoint=ckpt,
            filename_prefix=prefix,
            steps=steps,
            cfg=cfg,
            sampler=str(d.get("sampler", "euler_ancestral")),
            scheduler=str(d.get("scheduler", "normal")),
            lora=lora_name,
            lora_strength=lora_strength,
            ref_image=ref_image_name,
            denoise=denoise,
            denoise_subject=denoise_subject,
        )

        mode_note = f" img2img denoise={denoise}" if gen_mode in GEN_MODES_IMG2IMG else ""
        lora_note = f" lora={lora_name}@{lora_strength}" if lora_name else ""
        expected_s = _estimate_comfy_seconds(gen_w, gen_h, steps)
        _log(
            log,
            f"生成 {asset.filename} ({gen_w}×{gen_h}→{output_w}×{output_h}) "
            f"ckpt={ckpt}{lora_note}{mode_note} seed={seed} steps={steps} cfg={cfg} "
            f"预计 ~{expected_s}s",
        )
        prompt_id = client.queue_prompt(workflow)
        last_progress_log = -1

        def _progress(info: dict) -> None:
            nonlocal last_progress_log
            if info.get("kind") == "running":
                elapsed = int(info.get("elapsed") or 0)
                if log and elapsed >= 30 and elapsed // 30 > last_progress_log // 30:
                    last_progress_log = elapsed
                    _log(log, f"  ComfyUI 生成中… {elapsed}s（预计 ~{expected_s}s）")
            if progress_cb:
                progress_cb(info)

        history = client.wait_prompt(
            prompt_id,
            progress_cb=_progress,
            steps_hint=steps,
            expected_s=expected_s,
            cancel_event=self.cancel_event,
        )
        images = client.collect_output_images(history)
        if not images:
            return GenerateResult(asset.id, False, "无输出图")

        img_meta = images[-1]
        data = client.download_image(
            img_meta["filename"],
            img_meta.get("subfolder") or "",
            img_meta.get("type") or "output",
        )
        if (gen_w, gen_h) != (output_w, output_h):
            data = _resize_png(data, output_w, output_h)

        matte_mode = self.config.alpha_matte_for_asset(asset)
        if apply_alpha_matte_png and matte_mode != "none":
            try:
                data = apply_alpha_matte_png(data, mode=matte_mode)
                _log(log, f"  alpha 抠底 ({matte_mode})")
            except Exception as exc:
                _log(log, f"  alpha 抠底跳过: {exc}")

        src_path, inbox_path, _ = self.config.resolve_paths(asset)
        if gen_mode == GEN_MODE_REDRAW:
            inbox_path.parent.mkdir(parents=True, exist_ok=True)
            inbox_path.write_bytes(data)
            _log(
                log,
                f"  inbox  → {self.config.rel_to_project(inbox_path)} (重绘图，source 未修改)",
            )
        else:
            src_path.parent.mkdir(parents=True, exist_ok=True)
            src_path.write_bytes(data)
            _log(log, f"  source → {self.config.rel_to_project(src_path)}")

            if to_inbox:
                inbox_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, inbox_path)
                _log(log, f"  inbox  → {self.config.rel_to_project(inbox_path)}")

        return GenerateResult(asset.id, True, "ok", src_path if gen_mode != GEN_MODE_REDRAW else inbox_path)

    def generate_many(
        self,
        assets: list[Asset],
        *,
        to_inbox: bool = True,
        log: LogFn | None = None,
        progress_cb: ProgressCallback | None = None,
        batch_cb: Callable[[int, int, Asset], None] | None = None,
    ) -> list[GenerateResult]:
        results: list[GenerateResult] = []
        total = len([a for a in assets if a.enabled])
        idx = 0
        for asset in assets:
            if not asset.enabled:
                _log(log, f"跳过（已禁用）{asset.filename}")
                continue
            idx += 1
            if batch_cb:
                batch_cb(idx, total, asset)
            try:
                results.append(
                    self.generate_one(asset, to_inbox=to_inbox, log=log, progress_cb=progress_cb)
                )
            except (ComfyUiError, OSError, FileNotFoundError) as exc:
                results.append(GenerateResult(asset.id, False, str(exc)))
                _log(log, f"FAIL {asset.filename}: {exc}")
        return results

    def export_one(self, asset: Asset, *, log: LogFn | None = None) -> bool:
        _, inbox_path, unity_path = self.config.resolve_paths(asset)
        if not inbox_path.is_file():
            _log(log, f"SKIP 无 inbox: {asset.filename}")
            return False
        unity_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(inbox_path, unity_path)
        _log(log, f"导出 {asset.filename} → {self.config.rel_to_project(unity_path)}")
        return True

    def export_many(self, assets: list[Asset], *, log: LogFn | None = None) -> tuple[int, int]:
        ok = 0
        for asset in assets:
            if self.export_one(asset, log=log):
                ok += 1
        _log(log, f"导出完成 {ok}/{len(assets)}")
        return ok, len(assets)

    def save_asset_workflow(self, asset: Asset, workflow_json: dict) -> Path:
        wf_path = WORKFLOWS_DIR / "assets" / f"{asset.id}.json"
        wf_path.parent.mkdir(parents=True, exist_ok=True)
        import json

        wf_path.write_text(json.dumps(workflow_json, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        asset.workflow = f"workflows/assets/{asset.id}.json"
        self.config.update_asset(asset)
        return wf_path

    def init_asset_workflows_from_default(self) -> int:
        """为每个资源复制分类默认工作流模板（若尚不存在）。"""
        count = 0
        assets_dir = WORKFLOWS_DIR / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        for asset in self.config.assets():
            cat = self.config.category_by_id(asset.category)
            rel = cat.default_workflow if cat else "workflows/_default_sdxl_api.json"
            default = WORKFLOWS_DIR / Path(rel).name if "/" in rel else WORKFLOWS_DIR / rel
            if not default.is_file():
                default = WORKFLOWS_DIR / "_default_sdxl_api.json"
            if not default.is_file():
                continue
            text = default.read_text(encoding="utf-8")
            wf_path = assets_dir / f"{asset.id}.json"
            if not wf_path.is_file():
                wf_path.write_text(text, encoding="utf-8")
                asset.workflow = f"workflows/assets/{asset.id}.json"
                self.config.update_asset(asset)
                count += 1
        return count
