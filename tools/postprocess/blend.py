#!/usr/bin/env python3
"""图层混色、叠加颜色与合成模式。"""

from __future__ import annotations

import re
from typing import Any

import numpy as np
from PIL import Image

BLEND_MODES = (
    "normal",
    "multiply",
    "screen",
    "overlay",
    "soft_light",
    "add",
    "color",
)

_BLEND_ALIASES = {
    "": "normal",
    "none": "normal",
    "plus": "add",
    "linear_dodge": "add",
    "lighter": "add",
}


def normalize_blend_mode(mode: str | None) -> str:
    key = str(mode or "normal").strip().lower().replace("-", "_").replace(" ", "_")
    key = _BLEND_ALIASES.get(key, key)
    return key if key in BLEND_MODES else "normal"


def parse_hex_color(value: str, *, default_alpha: int = 255) -> tuple[int, int, int, int]:
    s = str(value or "").strip()
    if not s:
        return (255, 255, 255, 0)
    if s.startswith("#"):
        s = s[1:]
    if re.fullmatch(r"[0-9a-fA-F]{3}", s):
        s = "".join(ch * 2 for ch in s)
    if re.fullmatch(r"[0-9a-fA-F]{6}", s):
        r = int(s[0:2], 16)
        g = int(s[2:4], 16)
        b = int(s[4:6], 16)
        return (r, g, b, default_alpha)
    if re.fullmatch(r"[0-9a-fA-F]{8}", s):
        r = int(s[0:2], 16)
        g = int(s[2:4], 16)
        b = int(s[4:6], 16)
        a = int(s[6:8], 16)
        return (r, g, b, a)
    return (255, 255, 255, 0)


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _blend_pair(a: np.ndarray, b: np.ndarray, mode: str) -> np.ndarray:
    if mode == "multiply":
        return a * b
    if mode == "screen":
        return 1.0 - (1.0 - a) * (1.0 - b)
    if mode == "overlay":
        return np.where(a < 0.5, 2.0 * a * b, 1.0 - 2.0 * (1.0 - a) * (1.0 - b))
    if mode == "soft_light":
        return np.where(
            b < 0.5,
            a - (1.0 - 2.0 * b) * a * (1.0 - a),
            a + (2.0 * b - 1.0) * (np.sqrt(a) - a),
        )
    if mode == "add":
        return np.minimum(a + b, 1.0)
    if mode == "color":
        return b
    return b


def apply_layer_tint(
    im: Image.Image,
    *,
    color: str,
    amount: float,
    mode: str,
) -> Image.Image:
    """用叠加颜色处理图层像素（混色 / 加色）。"""
    amt = _clamp01(amount)
    if amt <= 0.001 or not str(color or "").strip():
        return im
    r, g, b, _ = parse_hex_color(color)

    arr = np.array(im.convert("RGBA"), dtype=np.float32)
    rgb = arr[..., :3] / 255.0
    alpha = arr[..., 3:4] / 255.0
    tint = np.array([r, g, b], dtype=np.float32) / 255.0
    mode = normalize_blend_mode(mode)

    if mode == "add":
        out_rgb = np.minimum(rgb + tint * amt, 1.0)
    elif mode == "multiply":
        mixed = rgb * tint
        out_rgb = rgb * (1.0 - amt) + mixed * amt
    elif mode == "screen":
        mixed = 1.0 - (1.0 - rgb) * (1.0 - tint)
        out_rgb = rgb * (1.0 - amt) + mixed * amt
    elif mode == "overlay":
        mixed = _blend_pair(rgb, np.broadcast_to(tint, rgb.shape), "overlay")
        out_rgb = rgb * (1.0 - amt) + mixed * amt
    elif mode == "color":
        lum = (
            0.299 * rgb[..., 0:1]
            + 0.587 * rgb[..., 1:2]
            + 0.114 * rgb[..., 2:3]
        )
        tint_rgb = np.broadcast_to(tint, rgb.shape)
        out_rgb = rgb * (1.0 - amt) + tint_rgb * amt
        out_lum = (
            0.299 * out_rgb[..., 0:1]
            + 0.587 * out_rgb[..., 1:2]
            + 0.114 * out_rgb[..., 2:3]
        )
        scale = np.where(out_lum > 1e-6, lum / out_lum, 1.0)
        out_rgb = np.clip(out_rgb * scale, 0.0, 1.0)
    else:
        tint_rgb = np.broadcast_to(tint, rgb.shape)
        out_rgb = rgb * (1.0 - amt) + tint_rgb * amt

    out = np.concatenate([out_rgb * 255.0, alpha * 255.0], axis=-1)
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), mode="RGBA")


def composite_layer(
    canvas: Image.Image,
    layer_im: Image.Image,
    x: int,
    y: int,
    *,
    blend_mode: str = "normal",
    opacity: float = 1.0,
) -> None:
    """将图层合成到画布指定位置。"""
    mode = normalize_blend_mode(blend_mode)
    opacity = _clamp01(opacity)
    if layer_im.mode != "RGBA":
        layer_im = layer_im.convert("RGBA")

    cw, ch = canvas.size
    lw, lh = layer_im.size
    if lw <= 0 or lh <= 0:
        return

    x0 = max(0, x)
    y0 = max(0, y)
    x1 = min(cw, x + lw)
    y1 = min(ch, y + lh)
    if x0 >= x1 or y0 >= y1:
        return

    sx0 = x0 - x
    sy0 = y0 - y
    sx1 = sx0 + (x1 - x0)
    sy1 = sy0 + (y1 - y0)

    src = np.array(layer_im.crop((sx0, sy0, sx1, sy1)), dtype=np.float32)
    dst = np.array(canvas.crop((x0, y0, x1, y1)).convert("RGBA"), dtype=np.float32)

    if opacity < 0.999:
        src[..., 3] *= opacity

    if mode == "normal":
        out = _alpha_over(dst, src)
    else:
        out = _blend_composite(dst, src, mode)

    patch = Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), mode="RGBA")
    canvas.paste(patch, (x0, y0))


def _alpha_over(dst: np.ndarray, src: np.ndarray) -> np.ndarray:
    sa = src[..., 3:4] / 255.0
    da = dst[..., 3:4] / 255.0
    sb = src[..., :3] / 255.0
    db = dst[..., :3] / 255.0
    out_a = sa + da * (1.0 - sa)
    out_rgb = np.where(
        out_a > 1e-6,
        (sb * sa + db * da * (1.0 - sa)) / out_a,
        0.0,
    )
    return np.concatenate([out_rgb * 255.0, np.clip(out_a, 0, 1) * 255.0], axis=-1)


def _blend_composite(dst: np.ndarray, src: np.ndarray, mode: str) -> np.ndarray:
    sa = src[..., 3:4] / 255.0
    da = dst[..., 3:4] / 255.0
    sb = src[..., :3] / 255.0
    db = dst[..., :3] / 255.0
    blended = _blend_pair(db, sb, mode)
    out_a = sa + da * (1.0 - sa)
    out_rgb = np.where(
        out_a > 1e-6,
        (blended * sa + db * da * (1.0 - sa)) / out_a,
        0.0,
    )
    return np.concatenate([out_rgb * 255.0, np.clip(out_a, 0, 1) * 255.0], axis=-1)


def layer_style_from_raw(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = raw or {}
    color = str(raw.get("blend_color", "") or "").strip()
    return {
        "blend_mode": normalize_blend_mode(raw.get("blend_mode")),
        "blend_color": color,
        "blend_amount": _clamp01(raw.get("blend_amount", 1.0)),
    }
