#!/usr/bin/env python3
"""后处理图层数据模型。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

LayerType = Literal["image", "text"]
Anchor = Literal["center", "top_left"]
BlendMode = Literal["normal", "multiply", "screen", "overlay", "soft_light", "add", "color"]

ASSET_SUBJECT_SOURCE = "$asset"


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


@dataclass
class LayerTransform:
    offset_x: float = 0.0
    offset_y: float = 0.0
    scale: float = 1.0
    anchor: Anchor = "center"
    rotation_deg: float = 0.0
    flip_h: bool = False
    flip_v: bool = False
    pivot_x: float = 0.5
    pivot_y: float = 0.5

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> LayerTransform:
        raw = raw or {}
        anchor = str(raw.get("anchor", "center"))
        if anchor not in ("center", "top_left"):
            anchor = "center"
        return cls(
            offset_x=float(raw.get("offset_x", 0)),
            offset_y=float(raw.get("offset_y", 0)),
            scale=float(raw.get("scale", 1.0)),
            anchor=anchor,  # type: ignore[arg-type]
            rotation_deg=float(raw.get("rotation_deg", 0)),
            flip_h=bool(raw.get("flip_h", False)),
            flip_v=bool(raw.get("flip_v", False)),
            pivot_x=_clamp01(raw.get("pivot_x", 0.5)),
            pivot_y=_clamp01(raw.get("pivot_y", 0.5)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "offset_x": self.offset_x,
            "offset_y": self.offset_y,
            "scale": self.scale,
            "anchor": self.anchor,
            "rotation_deg": self.rotation_deg,
            "flip_h": self.flip_h,
            "flip_v": self.flip_v,
            "pivot_x": self.pivot_x,
            "pivot_y": self.pivot_y,
        }


@dataclass
class CropRect:
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> CropRect | None:
        if not raw:
            return None
        w = int(raw.get("w", 0))
        h = int(raw.get("h", 0))
        if w <= 0 or h <= 0:
            return None
        return cls(x=int(raw.get("x", 0)), y=int(raw.get("y", 0)), w=w, h=h)

    def to_dict(self) -> dict[str, Any]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    def clamp_to(self, img_w: int, img_h: int) -> CropRect:
        x = max(0, min(self.x, img_w - 1))
        y = max(0, min(self.y, img_h - 1))
        w = max(1, min(self.w, img_w - x))
        h = max(1, min(self.h, img_h - y))
        return CropRect(x=x, y=y, w=w, h=h)


@dataclass
class TextStyle:
    content: str = ""
    font_family: str = "PingFang SC"
    font_size: int = 24
    color: str = "#FFFFFF"
    stroke_color: str = ""
    stroke_width: int = 0
    align: str = "center"

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> TextStyle:
        raw = raw or {}
        return cls(
            content=str(raw.get("content", "")),
            font_family=str(raw.get("font_family", "PingFang SC")),
            font_size=int(raw.get("font_size", 24)),
            color=str(raw.get("color", "#FFFFFF")),
            stroke_color=str(raw.get("stroke_color", "")),
            stroke_width=int(raw.get("stroke_width", 0)),
            align=str(raw.get("align", "center")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "font_family": self.font_family,
            "font_size": self.font_size,
            "color": self.color,
            "stroke_color": self.stroke_color,
            "stroke_width": self.stroke_width,
            "align": self.align,
        }


@dataclass
class Layer:
    id: str
    name: str
    type: LayerType
    visible: bool = True
    locked: bool = False
    opacity: float = 1.0
    blend_mode: str = "normal"
    blend_color: str = ""
    blend_amount: float = 1.0
    blend_enabled: bool = False
    transform: LayerTransform = field(default_factory=LayerTransform)
    source: str = ""
    crop: CropRect | None = None
    text: TextStyle | None = None
    is_subject: bool = False

    @classmethod
    def new_image(
        cls,
        name: str,
        *,
        source: str = "",
        is_subject: bool = False,
    ) -> Layer:
        return cls(
            id=_new_id(),
            name=name,
            type="image",
            source=source,
            is_subject=is_subject,
        )

    @classmethod
    def new_text(cls, name: str = "文字") -> Layer:
        return cls(
            id=_new_id(),
            name=name,
            type="text",
            text=TextStyle(),
        )

    def clone(self) -> Layer:
        return Layer(
            id=_new_id(),
            name=f"{self.name} 副本",
            type=self.type,
            visible=self.visible,
            locked=False,
            opacity=self.opacity,
            blend_mode=self.blend_mode,
            blend_color=self.blend_color,
            blend_amount=self.blend_amount,
            blend_enabled=self.blend_enabled,
            transform=LayerTransform.from_dict(self.transform.to_dict()),
            source=self.source,
            crop=CropRect.from_dict(self.crop.to_dict()) if self.crop else None,
            text=TextStyle.from_dict(self.text.to_dict()) if self.text else None,
            is_subject=False,
        )


@dataclass
class LayerStack:
    canvas_width: int
    canvas_height: int
    layers: list[Layer] = field(default_factory=list)
    edit_subject: str | None = None

    def subject_layer(self) -> Layer | None:
        for layer in self.layers:
            if layer.source == ASSET_SUBJECT_SOURCE or layer.is_subject:
                return layer
        return None

    def ensure_subject_layer(self) -> Layer:
        subj = self.subject_layer()
        if subj:
            subj.is_subject = True
            subj.source = ASSET_SUBJECT_SOURCE
            return subj
        layer = Layer.new_image("主体", source=ASSET_SUBJECT_SOURCE, is_subject=True)
        self.layers.append(layer)
        return layer


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def layer_image_source(layer: Layer) -> str:
    """解析图片层实际图源键（主体空 source 视为 $asset）。"""
    if layer.type != "image":
        return ""
    if layer.is_subject or layer.source == ASSET_SUBJECT_SOURCE:
        return ASSET_SUBJECT_SOURCE
    return layer.source


def layer_from_dict(raw: dict[str, Any]) -> Layer:
    from postprocess.blend import normalize_blend_mode

    layer_type = raw.get("type", "image")
    if layer_type not in ("image", "text"):
        layer_type = "image"
    text_raw = raw.get("text")
    is_subject = bool(raw.get("is_subject", False))
    source = str(raw.get("source", "")).strip()
    if layer_type == "image" and is_subject and not source:
        source = ASSET_SUBJECT_SOURCE
    blend_amount = _clamp01(raw.get("blend_amount", 1.0))
    blend_enabled = bool(raw.get("blend_enabled", False))
    if "blend_enabled" not in raw:
        blend_enabled = bool(str(raw.get("blend_color", "") or "").strip()) or (
            normalize_blend_mode(raw.get("blend_mode")) != "normal"
        )
    return Layer(
        id=str(raw.get("id") or _new_id()),
        name=str(raw.get("name", "图层")),
        type=layer_type,  # type: ignore[arg-type]
        visible=bool(raw.get("visible", True)),
        locked=bool(raw.get("locked", False)),
        opacity=float(raw.get("opacity", 1.0)),
        blend_mode=normalize_blend_mode(raw.get("blend_mode")),
        blend_color=str(raw.get("blend_color", "") or "").strip(),
        blend_amount=blend_amount,
        blend_enabled=blend_enabled,
        transform=LayerTransform.from_dict(raw.get("transform")),
        source=source,
        crop=CropRect.from_dict(raw.get("crop")),
        text=TextStyle.from_dict(text_raw) if layer_type == "text" else None,
        is_subject=is_subject or source == ASSET_SUBJECT_SOURCE,
    )


def layer_to_dict(layer: Layer) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": layer.id,
        "name": layer.name,
        "type": layer.type,
        "visible": layer.visible,
        "locked": layer.locked,
        "opacity": layer.opacity,
        "blend_mode": layer.blend_mode,
        "blend_color": layer.blend_color,
        "blend_amount": layer.blend_amount,
        "blend_enabled": layer.blend_enabled,
        "transform": layer.transform.to_dict(),
    }
    if layer.type == "image":
        src = layer.source
        if layer.is_subject or src == ASSET_SUBJECT_SOURCE:
            out["source"] = ASSET_SUBJECT_SOURCE
        else:
            out["source"] = src
        if layer.crop:
            out["crop"] = layer.crop.to_dict()
        if layer.is_subject or layer.source == ASSET_SUBJECT_SOURCE:
            out["is_subject"] = True
    if layer.type == "text" and layer.text:
        out["text"] = layer.text.to_dict()
    return out


def stack_from_dict(raw: dict[str, Any] | None) -> LayerStack | None:
    if not raw:
        return None
    layers = [layer_from_dict(item) for item in raw.get("layers", [])]
    edit_subject = raw.get("edit_subject")
    edit_key = str(edit_subject).strip().lower() if edit_subject else None
    if edit_key in ("in", "inbox"):
        edit_key = "inbox"
    elif edit_key in ("src", "source"):
        edit_key = "source"
    elif edit_key in ("engine", "unity"):
        edit_key = "unity"
    else:
        edit_key = None
    return LayerStack(
        canvas_width=int(raw.get("canvas", {}).get("width", raw.get("canvas_width", 512))),
        canvas_height=int(raw.get("canvas", {}).get("height", raw.get("canvas_height", 512))),
        layers=layers,
        edit_subject=edit_key,
    )


def stack_to_dict(stack: LayerStack) -> dict[str, Any]:
    out: dict[str, Any] = {
        "canvas": {"width": stack.canvas_width, "height": stack.canvas_height},
        "layers": [layer_to_dict(layer) for layer in stack.layers],
    }
    if stack.edit_subject:
        out["edit_subject"] = stack.edit_subject
    return out


def default_stack_for_canvas(width: int, height: int) -> LayerStack:
    """新建后处理：仅主体层。"""
    return LayerStack(
        canvas_width=width,
        canvas_height=height,
        layers=[
            Layer.new_image("主体", source=ASSET_SUBJECT_SOURCE, is_subject=True),
        ],
    )
