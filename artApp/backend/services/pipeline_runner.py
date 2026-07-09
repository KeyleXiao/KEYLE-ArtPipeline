#!/usr/bin/env python3
"""后台生成 / 导出任务（串行队列）。"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from backend.deps import get_config_manager, reload_config_manager
from backend.services.log_bus import log_bus


@dataclass
class JobState:
    busy: bool = False
    kind: str = ""
    run_id: int = 0
    progress: dict[str, Any] = field(default_factory=dict)
    cancel_requested: bool = False
    live_preview: bytes = field(default_factory=bytes)
    live_preview_mime: str = "image/jpeg"
    live_preview_rev: int = 0


@dataclass
class JobRequest:
    run_id: int
    kind: str
    asset_ids: list[str]
    export_after: bool = False


class PipelineRunner:
    def __init__(self) -> None:
        self.state = JobState()
        self._lock = threading.Lock()
        self._queue: deque[JobRequest] = deque()
        self._worker_lock = threading.Lock()
        self._worker_active = False

    def is_busy(self) -> bool:
        with self._lock:
            return self.state.busy

    def queue_size(self) -> int:
        with self._lock:
            return len(self._queue) + (1 if self.state.busy else 0)

    def progress_snapshot(self) -> dict[str, Any]:
        with self._lock:
            queue_items = [
                {
                    "run_id": req.run_id,
                    "kind": req.kind,
                    "label": self._label_for(req.asset_ids),
                    "count": len(req.asset_ids),
                }
                for req in self._queue
            ]
            snap = {
                "busy": self.state.busy,
                "kind": self.state.kind,
                "run_id": self.state.run_id,
                "progress": dict(self.state.progress),
                "queue": queue_items,
                "queued_count": len(queue_items) + (1 if self.state.busy else 0),
            }
            if "cloud_tasks" in self.state.progress:
                snap["cloud_tasks"] = list(self.state.progress.get("cloud_tasks") or [])
            return snap

    def live_preview_bytes(self) -> tuple[bytes, str, int]:
        with self._lock:
            return self.state.live_preview, self.state.live_preview_mime, self.state.live_preview_rev

    def _clear_live_preview(self) -> None:
        self.state.live_preview = b""
        self.state.live_preview_mime = "image/jpeg"
        self.state.live_preview_rev = 0

    def cancel(self) -> None:
        from pipeline_core import PipelineCore

        with self._lock:
            self.state.cancel_requested = True
            cleared = len(self._queue)
            self._queue.clear()
        try:
            PipelineCore(get_config_manager()).request_cancel()
        except Exception:
            pass
        if cleared:
            log_bus.log(f"已请求取消，并清空排队 {cleared} 个任务", kind="操作")
        else:
            log_bus.log("已请求取消（ComfyUI interrupt）", kind="操作")

    def _label_for(self, asset_ids: list[str]) -> str:
        config = get_config_manager()
        if len(asset_ids) == 1:
            asset = config.asset_by_id(asset_ids[0])
            if asset:
                return asset.filename
            return asset_ids[0]
        return f"{len(asset_ids)} 项"

    def _set_busy(self, busy: bool, kind: str = "") -> None:
        with self._lock:
            self.state.busy = busy
            self.state.kind = kind if busy else ""
            if busy:
                self.state.progress = {}
                self._clear_live_preview()
            if not busy:
                self.state.cancel_requested = False
                self._clear_live_preview()

    def _set_progress(self, info: dict[str, Any]) -> None:
        with self._lock:
            if info.get("kind") == "preview":
                raw = info.get("image_bytes")
                if isinstance(raw, (bytes, bytearray)) and raw:
                    self.state.live_preview = bytes(raw)
                    self.state.live_preview_mime = str(info.get("mime") or "image/jpeg")
                    self.state.live_preview_rev += 1
                prev = dict(self.state.progress)
                merged = dict(prev)
                merged["preview_rev"] = self.state.live_preview_rev
                merged["preview_available"] = self.state.live_preview_rev > 0
                self.state.progress = merged
                return
            if info.get("kind") == "batch":
                self.state.progress = dict(info)
                return
            if info.get("kind") == "cloud_batch":
                prev = dict(self.state.progress)
                merged = {
                    "kind": "cloud_batch",
                    "cloud_tasks": info.get("cloud_tasks") or [],
                    "overall_pct": info.get("overall_pct"),
                    "filename": info.get("filename") or prev.get("filename"),
                    "index": prev.get("index"),
                    "total": prev.get("total"),
                }
                self.state.progress = merged
                return
            prev = dict(self.state.progress)
            merged = dict(info)
            for key in ("index", "total", "filename", "cloud_tasks"):
                if key in prev and key not in merged:
                    merged[key] = prev[key]
            self.state.progress = merged

    def _next_run_id(self) -> int:
        with self._lock:
            self.state.run_id += 1
            return self.state.run_id

    def _enqueue(self, kind: str, asset_ids: list[str], *, export_after: bool = False) -> tuple[int, int]:
        run_id = self._next_run_id()
        req = JobRequest(
            run_id=run_id,
            kind=kind,
            asset_ids=list(asset_ids),
            export_after=export_after,
        )
        with self._lock:
            self._queue.append(req)
            position = len(self._queue) + (1 if self.state.busy else 0)
        if position > 1:
            log_bus.log(
                f"已加入队列 #{run_id} · {self._label_for(asset_ids)}（第 {position} 位）",
                kind="操作",
            )
        self._ensure_worker()
        return run_id, position

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker_active:
                return
            self._worker_active = True

        def worker() -> None:
            try:
                while True:
                    with self._lock:
                        if not self._queue:
                            break
                        req = self._queue.popleft()
                        self.state.run_id = req.run_id
                        self.state.busy = True
                        self.state.kind = req.kind
                        self.state.cancel_requested = False
                        self.state.progress = {}
                        self._clear_live_preview()
                    self._set_progress({"kind": "status", "message": "任务启动中…"})
                    try:
                        if req.kind == "generate":
                            self._run_generate(req)
                        elif req.kind == "export":
                            self._run_export(req)
                    except Exception as exc:
                        log_bus.log(f"任务 #{req.run_id} 异常: {exc}", kind="生成")
                    finally:
                        with self._lock:
                            self.state.cancel_requested = False
            finally:
                self._set_busy(False)
                with self._worker_lock:
                    self._worker_active = False
                with self._lock:
                    if self._queue:
                        self._ensure_worker()

        threading.Thread(target=worker, daemon=True, name="artapp-job").start()

    def generate_batch(self, asset_ids: list[str], *, export_after: bool = False) -> tuple[int, int]:
        return self._enqueue("generate", asset_ids, export_after=export_after)

    def export_batch(self, asset_ids: list[str]) -> tuple[int, int]:
        return self._enqueue("export", asset_ids)

    def _run_generate(self, req: JobRequest) -> None:
        from cloud.runner import partition_assets_by_backend, run_cloud_batch
        from pipeline_core import PipelineCore

        config = reload_config_manager()
        pipeline = PipelineCore(config)
        pipeline.clear_cancel()

        cloud_assets, comfy_assets = partition_assets_by_backend(config, req.asset_ids)
        if not cloud_assets and not comfy_assets:
            missing = [aid for aid in req.asset_ids if not config.asset_by_id(aid)]
            if missing:
                log_bus.log(f"未找到资源: {', '.join(missing)}", kind="生成")
            else:
                log_bus.log("没有可生成的资源", kind="生成")
            return

        if comfy_assets:
            ok, comfy_msg = pipeline.test_comfyui()
            if not ok:
                if not cloud_assets:
                    log_bus.log(f"ComfyUI 不可用，已取消生成: {comfy_msg}", kind="生成")
                    self._set_progress({"kind": "status", "message": comfy_msg})
                    return
                log_bus.log(f"ComfyUI 离线，跳过 {len(comfy_assets)} 个本地资源: {comfy_msg}", kind="生成")
                comfy_assets = []

        all_assets = cloud_assets + comfy_assets
        disabled = [a for a in all_assets if not a.enabled]
        if disabled:
            log_bus.log(
                f"以下资源未勾选「启用」，仍按指定生成: "
                f"{', '.join(a.filename for a in disabled)}",
                kind="生成",
            )
        total = len(req.asset_ids)
        log_bus.log(
            f"── 开始 {'生成并导出' if req.export_after else '生成'} {total} 张 "
            f"(云 {len(cloud_assets)} · ComfyUI {len(comfy_assets)}) ──",
            kind="生成",
        )

        def progress_cb(info: dict) -> None:
            self._set_progress(info)

        cancel_event = pipeline.cancel_event

        if cloud_assets:
            self._set_progress(
                {
                    "kind": "batch",
                    "index": 0,
                    "total": total,
                    "filename": cloud_assets[0].filename,
                    "cloud_tasks": [],
                }
            )
            results = run_cloud_batch(
                config,
                cloud_assets,
                to_inbox=True,
                export_after=req.export_after,
                log=lambda m: log_bus.log(m, kind="生成"),
                progress_cb=progress_cb,
                cancel_event=cancel_event,
            )
            ok_n = sum(1 for r in results if getattr(r, "ok", False))
            fail_n = len(results) - ok_n
            if fail_n and ok_n:
                log_bus.log(f"云生成完成: 成功 {ok_n}，失败 {fail_n}", kind="生成")
            elif fail_n and not ok_n:
                log_bus.log(f"云生成全部失败 ({fail_n})", kind="生成")

        if cancel_event.is_set():
            log_bus.log("── 已取消 ──", kind="生成")
            log_bus.log("── 任务结束 ──", kind="生成")
            return

        comfy_index_base = len(cloud_assets)
        for i, asset in enumerate(comfy_assets, start=1):
            if cancel_event.is_set():
                log_bus.log("── 已取消 ──", kind="生成")
                break
            self._set_progress(
                {
                    "kind": "batch",
                    "index": comfy_index_base + i,
                    "total": total,
                    "filename": asset.filename,
                }
            )
            try:
                result = pipeline.generate_one(
                    asset,
                    to_inbox=True,
                    log=lambda m: log_bus.log(m, kind="生成"),
                    progress_cb=progress_cb,
                )
                if req.export_after and result.ok:
                    pipeline.export_one(asset, log=lambda m: log_bus.log(m, kind="生成"))
                if not result.ok:
                    log_bus.log(f"FAIL {asset.filename}: {result.message}", kind="生成")
            except Exception as exc:
                if cancel_event.is_set():
                    log_bus.log("── 已取消 ──", kind="生成")
                    break
                log_bus.log(f"FAIL {asset.filename}: {exc}", kind="生成")
        log_bus.log("── 任务结束 ──", kind="生成")

    def _run_export(self, req: JobRequest) -> None:
        from pipeline_core import PipelineCore

        config = reload_config_manager()
        pipeline = PipelineCore(config)
        assets = [a for aid in req.asset_ids if (a := config.asset_by_id(aid))]
        if not assets:
            log_bus.log("未找到指定资源", kind="生成")
            return
        ok, fail = pipeline.export_many(assets, log=lambda m: log_bus.log(m, kind="生成"))
        log_bus.log(f"导出完成: 成功 {ok}，失败 {fail}", kind="生成")


pipeline_runner = PipelineRunner()
