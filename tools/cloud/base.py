#!/usr/bin/env python3
"""云生图请求/响应与 Provider 协议。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

ProgressCallback = Callable[[dict[str, Any]], None]


class CloudProviderError(RuntimeError):
    pass


@dataclass
class CloudGenerateRequest:
    checkpoint: str
    provider: str
    model: dict[str, Any]
    mode: str
    prompt: str
    negative: str
    width: int
    height: int
    seed: int | None
    strength: float
    ref_image_path: Path | None
    base_image_path: Path | None
    api_keys: dict[str, str]
    extra_ref_paths: list[Path] | None = None
    output_count: int = 1


@dataclass
class CloudGenerateResult:
    ok: bool
    message: str
    png_bytes: bytes | None = None
    extra_png_bytes: list[bytes] | None = None


class CloudProvider:
    provider_id: str = ""

    def generate(
        self,
        req: CloudGenerateRequest,
        *,
        progress_cb: ProgressCallback | None = None,
        cancel_event: Any | None = None,
    ) -> CloudGenerateResult:
        raise NotImplementedError
