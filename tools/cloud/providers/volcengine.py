#!/usr/bin/env python3
"""火山方舟 · Seedream / 即梦。"""

from __future__ import annotations

import base64
import json
import math
from pathlib import Path

from cloud.base import CloudGenerateRequest, CloudGenerateResult, CloudProvider, CloudProviderError
from cloud.http_util import http_json, image_to_data_url, run_with_progress_heartbeat
from cloud.registry import CLOUD_GEN_MODE_EDIT, CLOUD_GEN_MODE_I2I, CLOUD_GEN_MODE_TEXT

_DEFAULT_BASE = "https://ark.cn-beijing.volces.com/api/v3"
# Seedream 4 像素下限见 registry providers.volcengine.size_constraints
_MAX_REF_IMAGES = 10


def _min_pixels() -> int:
    from cloud.registry import provider_meta

    sc = provider_meta("volcengine").get("size_constraints") or {}
    return max(1, int(sc.get("min_pixels") or 3_686_400))


def _api_size(width: int, height: int) -> tuple[int, int]:
    min_px = _min_pixels()
    w = max(1, int(width))
    h = max(1, int(height))
    if w * h >= min_px:
        return w, h
    scale = math.sqrt(min_px / (w * h)) * 1.02
    nw = max(w, int(math.ceil(w * scale)))
    nh = max(h, int(math.ceil(h * scale)))
    # 取整后可能仍略低于下限，再补一次
    if nw * nh < min_px:
        bump = math.sqrt(min_px / (nw * nh)) * 1.01
        nw = max(nw, int(math.ceil(nw * bump)))
        nh = max(nh, int(math.ceil(nh * bump)))
    return nw, nh


def _collect_ref_data_urls(req: CloudGenerateRequest) -> list[str]:
    urls: list[str] = []
    primary = req.ref_image_path if req.mode == CLOUD_GEN_MODE_I2I else (req.base_image_path or req.ref_image_path)
    if primary and primary.is_file():
        urls.append(image_to_data_url(primary))
    for path in req.extra_ref_paths or []:
        if path.is_file():
            urls.append(image_to_data_url(path))
        if len(urls) >= _MAX_REF_IMAGES:
            break
    return urls


def _decode_image_items(items: list[dict]) -> list[bytes]:
    out: list[bytes] = []
    for item in items:
        b64 = item.get("b64_json") or item.get("b64_image")
        if b64:
            out.append(base64.b64decode(b64))
            continue
        url = item.get("url")
        if url:
            from cloud.http_util import download_bytes

            out.append(download_bytes(str(url)))
    return out


class VolcengineProvider(CloudProvider):
    provider_id = "volcengine"

    def generate(self, req: CloudGenerateRequest, *, progress_cb=None, cancel_event=None) -> CloudGenerateResult:
        key = req.api_keys.get("volcengine", "")
        if not key:
            return CloudGenerateResult(False, "未配置火山方舟 API Key")
        model = req.api_keys.get("volcengine_endpoint") or str(req.model.get("api_model") or "doubao-seedream-4-0")
        if not model:
            return CloudGenerateResult(False, "未配置 Seedream 模型 ID（volcengine_endpoint）")

        api_w, api_h = _api_size(req.width, req.height)
        output_count = max(1, min(15, int(req.output_count or 1)))
        scale = max(0.0, min(1.0, float(req.strength or 0.5)))

        body: dict = {
            "model": model,
            "prompt": req.prompt,
            "size": f"{api_w}x{api_h}",
            "response_format": "b64_json",
            "watermark": False,
            "scale": round(scale, 2),
            "force_single": output_count <= 1,
        }
        if req.negative:
            body["negative_prompt"] = req.negative
        if req.seed is not None:
            body["seed"] = int(req.seed)

        if req.mode in (CLOUD_GEN_MODE_I2I, CLOUD_GEN_MODE_EDIT):
            ref_urls = _collect_ref_data_urls(req)
            if not ref_urls:
                return CloudGenerateResult(False, "图生图/图像编辑：参考图或底图不存在")
            body["image"] = ref_urls[0] if len(ref_urls) == 1 else ref_urls
            body["sequential_image_generation"] = "disabled"
        elif output_count > 1:
            body["sequential_image_generation"] = "auto"
            body["sequential_image_generation_options"] = {"max_images": output_count}
        else:
            body["sequential_image_generation"] = "disabled"

        if output_count > 1 and req.mode in (CLOUD_GEN_MODE_I2I, CLOUD_GEN_MODE_EDIT):
            body["sequential_image_generation"] = "auto"
            body["sequential_image_generation_options"] = {"max_images": output_count}

        label = {
            CLOUD_GEN_MODE_TEXT: "文生图",
            CLOUD_GEN_MODE_I2I: "图生图",
            CLOUD_GEN_MODE_EDIT: "图像编辑",
        }.get(req.mode, req.mode)
        if progress_cb:
            progress_cb(
                {
                    "kind": "cloud_task",
                    "status": "SUBMITTING",
                    "pct": 10,
                    "message": f"即梦 · {label} · 提交中",
                }
            )

        def _request() -> dict:
            return http_json(
                f"{_DEFAULT_BASE}/images/generations",
                method="POST",
                headers={"Authorization": f"Bearer {key}"},
                body=body,
                timeout=300.0,
            )

        data = run_with_progress_heartbeat(
            _request,
            progress_cb=progress_cb,
            cancel_event=cancel_event,
            message=f"即梦 · {label} · 生成中",
            start_pct=18,
            max_pct=90,
        )
        items = data.get("data") or []
        if not items:
            err = data.get("error") or data
            raise CloudProviderError(f"即梦无输出: {json.dumps(err, ensure_ascii=False)[:200]}")
        pngs = _decode_image_items(items)
        if not pngs:
            raise CloudProviderError("即梦结果无图像数据")
        msg = "ok"
        if len(pngs) > 1:
            msg = f"ok · 共 {len(pngs)} 张，已写入首张至 inbox"
        if progress_cb:
            progress_cb({"kind": "cloud_task", "status": "SUCCEEDED", "pct": 100, "message": "即梦 · 完成"})
        return CloudGenerateResult(
            True,
            msg,
            png_bytes=pngs[0],
            extra_png_bytes=pngs[1:] if len(pngs) > 1 else None,
        )


def parse_extra_ref_paths(raw: str, art_root: Path) -> list[Path]:
    paths: list[Path] = []
    for line in str(raw or "").replace(",", "\n").split("\n"):
        p = line.strip()
        if not p:
            continue
        path = Path(p).expanduser()
        if not path.is_absolute():
            path = (art_root / p).resolve()
        else:
            path = path.resolve()
        if path.is_file():
            paths.append(path)
        if len(paths) >= _MAX_REF_IMAGES - 1:
            break
    return paths
