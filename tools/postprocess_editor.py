#!/usr/bin/env python3
"""PS 式后处理编辑器（图层、拖拽、裁切、文字）。"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk
from typing import Any, Callable

from config_manager import Asset, ConfigManager
from postprocess.engine import (
    AssetImageResolver,
    Bounds,
    hit_test,
    layer_bounds,
    render_stack,
    stack_checkerboard,
)
from postprocess.fonts import FALLBACK_FAMILIES, list_system_fonts
from postprocess.models import (
    ASSET_SUBJECT_SOURCE,
    CropRect,
    Layer,
    LayerStack,
    TextStyle,
    stack_from_dict,
    stack_to_dict,
)
from postprocess.templates import BUILTIN_TEMPLATES, builtin_template, save_template
from ui_layout import ScrollableFrame, fit_toplevel_to_screen

try:
    import ttkbootstrap as ttkb

    HAS_TTB = True
except ImportError:
    HAS_TTB = False

VIEW_MIN_ZOOM = 0.5
VIEW_MAX_ZOOM = 8.0
DEFAULT_VIEW_ZOOM = 1.0


def open_postprocess_editor(
    parent: tk.Misc,
    config_mgr: ConfigManager,
    asset: Asset,
    *,
    subject_path: Path | None = None,
    subject_label: str = "",
    on_applied: Callable[[], None] | None = None,
) -> None:
    PostprocessEditorWindow(
        parent,
        config_mgr,
        asset,
        subject_path=subject_path,
        subject_label=subject_label,
        on_applied=on_applied,
    )


class PostprocessEditorWindow(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        config_mgr: ConfigManager,
        asset: Asset,
        *,
        subject_path: Path | None = None,
        subject_label: str = "",
        on_applied: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.config_mgr = config_mgr
        self.asset = asset
        if subject_path is not None:
            try:
                self._subject_path = subject_path.expanduser().resolve()
            except OSError:
                self._subject_path = subject_path
        else:
            self._subject_path = None
        self._subject_label = subject_label.strip()
        self.on_applied = on_applied
        self.title(f"后处理 · {asset.filename}")

        self._stack = self._load_initial_stack()
        subj = self._stack.ensure_subject_layer()
        self._selected_id = subj.id
        self._solo_id: str | None = None
        self._view_zoom = DEFAULT_VIEW_ZOOM
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._canvas_photo: Any = None
        self._drag_layer_id: str | None = None
        self._drag_last: tuple[float, float] | None = None
        self._crop_mode = False
        self._crop_drag_start: tuple[float, float] | None = None
        self._crop_preview: CropRect | None = None
        self._font_list: list[str] = list(FALLBACK_FAMILIES)
        self._props_guard = False
        self._redraw_job: str | None = None
        self._dragging_layer = False

        self._build_ui()
        fit_toplevel_to_screen(self, prefer_w=1180, prefer_h=760, min_w=720, min_h=520)
        self._page_scroll.install_global_wheel()
        self.after_idle(self._deferred_editor_init)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Escape>", lambda _e: self._exit_crop_mode())
        self.bind("<Return>", lambda _e: self._commit_crop())
        self.bind("c", lambda _e: self._toggle_crop_mode())
        for seq, handler in (
            ("<Up>", lambda e: self._nudge_selected_layer(0, -1, e)),
            ("<Down>", lambda e: self._nudge_selected_layer(0, 1, e)),
            ("<Left>", lambda e: self._nudge_selected_layer(-1, 0, e)),
            ("<Right>", lambda e: self._nudge_selected_layer(1, 0, e)),
            ("<Home>", lambda _e: self._reset_layer_offset()),
            ("<Control-0>", lambda _e: self._reset_layer_scale()),
        ):
            self.bind(seq, handler)
            self._canvas.bind(seq, handler)
        self._layer_list.bind("<Alt-Button-1>", self._on_layer_solo_click)
        self._layer_list.bind("<Double-Button-1>", self._on_layer_double_click)

    def _safe_page_refresh(self) -> None:
        try:
            if self.winfo_exists():
                self._page_scroll.refresh()
        except tk.TclError:
            pass

    def _align_canvas_to_subject_if_needed(self) -> bool:
        """主体 PNG 与画布尺寸不一致时，对齐画布（默认变换）；单主体层时清除错误缩放/偏移。"""
        from postprocess.models import layer_image_source

        subj = self._stack.subject_layer()
        if not subj or subj.type != "image":
            return False
        layers = self._stack.layers or []
        single_subject = len(layers) == 1 and subj.is_subject
        xf = subj.transform
        default_layout = (
            abs(float(xf.scale) - 1.0) < 1e-6
            and abs(float(xf.offset_x)) < 1e-6
            and abs(float(xf.offset_y)) < 1e-6
            and abs(float(xf.rotation_deg)) < 1e-6
            and not xf.flip_h
            and not xf.flip_v
            and not subj.crop
        )
        if not default_layout and not single_subject:
            return False
        raw = self._resolver().resolve(layer_image_source(subj))
        if not raw:
            return False
        rw, rh = raw.size
        changed = False
        if rw != self._stack.canvas_width or rh != self._stack.canvas_height:
            self._stack.canvas_width = rw
            self._stack.canvas_height = rh
            changed = True
        if changed or (single_subject and not default_layout):
            anchor = subj.transform.anchor
            subj.crop = None
            from postprocess.models import LayerTransform

            subj.transform = LayerTransform(anchor=anchor)
            changed = True
        return changed

    def _deferred_editor_init(self) -> None:
        try:
            self._font_list = list_system_fonts()
            if hasattr(self, "_font_combo"):
                self._font_combo.configure(values=self._font_list[:80])
        except tk.TclError:
            pass
        self._align_canvas_to_subject_if_needed()
        self._refresh_all()
        self.after(300, self._safe_page_refresh)

    def _load_initial_stack(self) -> LayerStack:
        existing = self.config_mgr.get_postprocess_stack(self.asset.id)
        if not existing or not existing.layers:
            existing = self.config_mgr.default_postprocess_stack(self.asset)
        existing.ensure_subject_layer()
        for layer in existing.layers:
            if layer.is_subject or layer.source == ASSET_SUBJECT_SOURCE:
                layer.source = ASSET_SUBJECT_SOURCE
                layer.is_subject = True
                layer.visible = True
        return existing

    def _resolver(self) -> AssetImageResolver:
        src, inbox, unity = self.config_mgr.resolve_paths(self.asset)
        return AssetImageResolver(
            art_root=self.config_mgr.art_root(),
            asset_source=src if src.is_file() else None,
            asset_inbox=inbox if inbox.is_file() else None,
            asset_unity=unity if unity.is_file() else None,
            subject_path=self._subject_path,
        )

    def _subject_path_label(self) -> str:
        if self._subject_label:
            return self._subject_label
        if self._subject_path and self._subject_path.is_file():
            try:
                return self._subject_path.name
            except OSError:
                pass
        return "inbox / source"

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self, padding=(8, 6))
        toolbar.pack(fill=tk.X)
        title = f"{self.asset.filename}  ·  {self.asset.width}×{self.asset.height}"
        title += f"  ·  主体: {self._subject_path_label()}"
        ttk.Label(toolbar, text=title).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="−", width=3, command=lambda: self._zoom_by(0.85)).pack(side=tk.RIGHT, padx=2)
        self._zoom_var = tk.StringVar(value=f"{int(self._view_zoom * 100)}%")
        ttk.Label(toolbar, textvariable=self._zoom_var, width=6).pack(side=tk.RIGHT)
        ttk.Button(toolbar, text="+", width=3, command=lambda: self._zoom_by(1.18)).pack(side=tk.RIGHT, padx=2)
        ttk.Button(toolbar, text="适应", command=self._zoom_fit).pack(side=tk.RIGHT, padx=(8, 2))
        self._solo_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(toolbar, text="Solo", variable=self._solo_var, command=self._on_solo_toggle).pack(
            side=tk.RIGHT, padx=8
        )
        self._crop_btn = ttk.Button(toolbar, text="裁切 (C)", command=self._toggle_crop_mode)
        self._crop_btn.pack(side=tk.RIGHT, padx=4)

        footer = ttk.Frame(self, padding=(8, 6))
        footer.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(footer, text="存为分类模板…", command=self._save_as_template).pack(side=tk.LEFT)
        ttk.Button(footer, text="取消", command=self._on_close).pack(side=tk.RIGHT, padx=4)
        ttk.Button(footer, text="应用 → inbox", command=self._apply_to_inbox).pack(side=tk.RIGHT)

        self._page_scroll = ScrollableFrame(self)
        self._page_scroll.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        page = self._page_scroll.interior

        body = ttk.Panedwindow(page, orient=tk.HORIZONTAL)
        body.pack(fill=tk.X, anchor=tk.NW)

        left = ttk.Frame(body)
        body.add(left, weight=3)

        self._canvas = tk.Canvas(left, bg="#2b2b2b", highlightthickness=0, cursor="crosshair", height=420)
        self._canvas.pack(fill=tk.BOTH, expand=True)
        self._page_scroll._wheel_exempt = (left, self._canvas)
        self._canvas.bind("<Configure>", lambda _e: self._schedule_redraw())
        self._canvas.bind("<ButtonPress-1>", self._on_canvas_press)
        self._canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self._canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
        self._canvas.bind("<MouseWheel>", self._on_wheel)
        self._canvas.bind("<Button-4>", lambda _e: self._zoom_by(1.1))
        self._canvas.bind("<Button-5>", lambda _e: self._zoom_by(0.9))

        self._status_var = tk.StringVar(value="")
        ttk.Label(left, textvariable=self._status_var).pack(fill=tk.X, pady=(4, 0))

        right = ttk.Frame(body)
        body.add(right, weight=1)

        layer_box = ttk.LabelFrame(right, text="图层（上 = 前景）", padding=6)
        layer_box.pack(fill=tk.X, pady=(0, 4))

        layer_btns = ttk.Frame(layer_box)
        layer_btns.pack(fill=tk.X, pady=(0, 4))
        ttk.Button(layer_btns, text="+ 图片", command=self._add_image_layer).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(layer_btns, text="+ 文字", command=self._add_text_layer).pack(side=tk.LEFT)

        self._layer_list = tk.Listbox(layer_box, height=8, exportselection=False)
        self._layer_list.pack(fill=tk.X, pady=2)
        self._layer_list.bind("<<ListboxSelect>>", self._on_layer_select)

        row_btns = ttk.Frame(layer_box)
        row_btns.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(row_btns, text="↑", width=3, command=self._move_layer_up).pack(side=tk.LEFT, padx=1)
        ttk.Button(row_btns, text="↓", width=3, command=self._move_layer_down).pack(side=tk.LEFT, padx=1)
        ttk.Button(row_btns, text="👁", width=3, command=self._toggle_visible).pack(side=tk.LEFT, padx=1)
        ttk.Button(row_btns, text="🔒", width=3, command=self._toggle_locked).pack(side=tk.LEFT, padx=1)
        ttk.Button(row_btns, text="复制", command=self._duplicate_layer).pack(side=tk.LEFT, padx=4)
        ttk.Button(row_btns, text="删除", command=self._delete_layer).pack(side=tk.RIGHT)

        props = ttk.LabelFrame(right, text="属性", padding=6)
        props.pack(fill=tk.X, pady=(8, 0))

        self._prop_name = tk.StringVar()
        self._prop_source = tk.StringVar()
        self._prop_offset_x = tk.DoubleVar()
        self._prop_offset_y = tk.DoubleVar()
        self._prop_scale = tk.DoubleVar(value=1.0)
        self._prop_opacity = tk.DoubleVar(value=1.0)
        self._prop_text = tk.StringVar()
        self._prop_font = tk.StringVar()
        self._prop_font_size = tk.IntVar(value=24)
        self._prop_color = tk.StringVar(value="#FFFFFF")

        ttk.Label(props, text="名称").grid(row=0, column=0, sticky=tk.W, pady=2)
        ttk.Entry(props, textvariable=self._prop_name).grid(row=0, column=1, sticky=tk.EW, pady=2)
        self._type_label = ttk.Label(props, text="类型: -")
        self._type_label.grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=2)

        self._image_row = ttk.Frame(props)
        self._image_row.grid(row=2, column=0, columnspan=2, sticky=tk.EW)
        ttk.Label(self._image_row, text="图源").pack(side=tk.LEFT)
        src_entry = ttk.Entry(self._image_row, textvariable=self._prop_source)
        src_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        ttk.Button(self._image_row, text="…", width=3, command=self._browse_source).pack(side=tk.LEFT)

        self._text_row = ttk.Frame(props)
        self._text_row.grid(row=3, column=0, columnspan=2, sticky=tk.EW)
        ttk.Label(self._text_row, text="内容").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(self._text_row, textvariable=self._prop_text).grid(row=0, column=1, sticky=tk.EW, pady=2)
        ttk.Label(self._text_row, text="字体").grid(row=1, column=0, sticky=tk.W)
        self._font_combo = ttk.Combobox(
            self._text_row,
            textvariable=self._prop_font,
            values=self._font_list[:80],
            state="normal",
        )
        self._font_combo.grid(row=1, column=1, sticky=tk.EW, pady=2)
        ttk.Label(self._text_row, text="字号").grid(row=2, column=0, sticky=tk.W)
        ttk.Spinbox(self._text_row, from_=8, to=256, textvariable=self._prop_font_size, width=8).grid(
            row=2, column=1, sticky=tk.W, pady=2
        )
        ttk.Label(self._text_row, text="颜色").grid(row=3, column=0, sticky=tk.W)
        color_row = ttk.Frame(self._text_row)
        color_row.grid(row=3, column=1, sticky=tk.EW)
        ttk.Entry(color_row, textvariable=self._prop_color, width=10).pack(side=tk.LEFT)
        ttk.Button(color_row, text="选色", command=self._pick_color).pack(side=tk.LEFT, padx=4)
        self._text_row.columnconfigure(1, weight=1)

        ttk.Label(props, text="位置 X").grid(row=4, column=0, sticky=tk.W, pady=2)
        pos_row = ttk.Frame(props)
        pos_row.grid(row=4, column=1, sticky=tk.EW, pady=2)
        ttk.Spinbox(pos_row, from_=-2048, to=2048, textvariable=self._prop_offset_x, width=7).pack(
            side=tk.LEFT
        )
        ttk.Label(pos_row, text="Y").pack(side=tk.LEFT, padx=(6, 2))
        ttk.Spinbox(pos_row, from_=-2048, to=2048, textvariable=self._prop_offset_y, width=7).pack(
            side=tk.LEFT
        )
        ttk.Button(pos_row, text="归位 0,0", command=self._reset_layer_offset, width=9).pack(
            side=tk.LEFT, padx=(8, 0)
        )

        quick_row = ttk.Frame(props)
        quick_row.grid(row=5, column=0, columnspan=2, sticky=tk.EW, pady=(0, 4))
        ttk.Button(quick_row, text="X→0", width=5, command=self._center_layer_x).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(quick_row, text="Y→0", width=5, command=self._center_layer_y).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(quick_row, text="缩放 100%", command=self._reset_layer_scale).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(quick_row, text="重置变换", command=self._reset_layer_transform).pack(side=tk.LEFT)

        ttk.Label(props, text="缩放").grid(row=6, column=0, sticky=tk.W, pady=2)
        scale_row = ttk.Frame(props)
        scale_row.grid(row=6, column=1, sticky=tk.EW, pady=2)
        ttk.Scale(scale_row, from_=0.05, to=3.0, variable=self._prop_scale, command=lambda _v: self._apply_props()).pack(
            fill=tk.X, expand=True, side=tk.LEFT
        )
        self._scale_pct_var = tk.StringVar(value="100%")
        ttk.Label(scale_row, textvariable=self._scale_pct_var, width=5).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(props, text="不透明度").grid(row=7, column=0, sticky=tk.W, pady=2)
        ttk.Scale(props, from_=0.0, to=1.0, variable=self._prop_opacity, command=lambda _v: self._apply_props()).grid(
            row=7, column=1, sticky=tk.EW, pady=2
        )
        hint_style = "Muted.TLabel" if HAS_TTB else None
        ttk.Label(
            props,
            text="拖拽移动 · 方向键微调(Shift×10) · Home 归位 · Ctrl+0 缩放100%",
            style=hint_style,
            wraplength=260,
        ).grid(row=8, column=0, columnspan=2, sticky=tk.W, pady=(4, 0))
        props.columnconfigure(1, weight=1)

        for var in (
            self._prop_name,
            self._prop_source,
            self._prop_text,
            self._prop_font,
            self._prop_font_size,
            self._prop_color,
        ):
            var.trace_add("write", lambda *_: self._apply_props())

        for var in (self._prop_offset_x, self._prop_offset_y):
            var.trace_add("write", lambda *_: self._apply_offset_props())

        tmpl_row = ttk.Frame(right)
        tmpl_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(tmpl_row, text="模板").pack(side=tk.LEFT)
        self._template_var = tk.StringVar(value="minimal")
        ttk.Combobox(
            tmpl_row,
            textvariable=self._template_var,
            values=list(BUILTIN_TEMPLATES.keys()),
            state="readonly",
            width=14,
        ).pack(side=tk.LEFT, padx=4)
        ttk.Button(tmpl_row, text="加载", command=self._load_template).pack(side=tk.LEFT)

    def _selected_layer(self) -> Layer | None:
        if not self._selected_id:
            return None
        for layer in self._stack.layers:
            if layer.id == self._selected_id:
                return layer
        return None

    def _layer_display_index(self, layer_id: str) -> int:
        """UI 列表索引：0 = 最前景。"""
        ids = [layer.id for layer in reversed(self._stack.layers)]
        return ids.index(layer_id)

    def _layer_from_display_index(self, ui_index: int) -> Layer | None:
        layers = list(reversed(self._stack.layers))
        if 0 <= ui_index < len(layers):
            return layers[ui_index]
        return None

    def _refresh_layer_list(self) -> None:
        self._layer_list.delete(0, tk.END)
        for layer in reversed(self._stack.layers):
            vis = "👁" if layer.visible else "○"
            lock = "🔒" if layer.locked else " "
            if layer.type == "text":
                icon = "📝"
            elif layer.source == ASSET_SUBJECT_SOURCE:
                icon = "★"
            else:
                icon = "🖼"
            self._layer_list.insert(tk.END, f"{vis}{lock} {icon} {layer.name}")
        if self._selected_id:
            try:
                idx = self._layer_display_index(self._selected_id)
                self._layer_list.selection_set(idx)
                self._layer_list.see(idx)
            except ValueError:
                pass

    def _sync_prop_vars(self, layer: Layer) -> None:
        self._props_guard = True
        try:
            self._prop_name.set(layer.name)
            self._prop_offset_x.set(layer.transform.offset_x)
            self._prop_offset_y.set(layer.transform.offset_y)
            self._prop_scale.set(layer.transform.scale)
            self._prop_opacity.set(layer.opacity)
            self._scale_pct_var.set(f"{int(round(layer.transform.scale * 100))}%")
            if layer.type == "image":
                if layer.is_subject or layer.source == ASSET_SUBJECT_SOURCE:
                    self._prop_source.set(ASSET_SUBJECT_SOURCE)
                else:
                    self._prop_source.set(layer.source)
            elif layer.text is not None:
                self._prop_text.set(layer.text.content)
                self._prop_font.set(layer.text.font_family)
                self._prop_font_size.set(layer.text.font_size)
                self._prop_color.set(layer.text.color)
        finally:
            self._props_guard = False

    def _refresh_props(self) -> None:
        layer = self._selected_layer()
        if not layer:
            self._type_label.configure(text="类型: -")
            return
        self._type_label.configure(text=f"类型: {'文字' if layer.type == 'text' else '图片'}")
        self._sync_prop_vars(layer)
        if layer.type == "image":
            self._image_row.grid()
            self._text_row.grid_remove()
        else:
            self._image_row.grid_remove()
            self._text_row.grid()
        can_crop = layer.type == "image" and (
            layer.source == ASSET_SUBJECT_SOURCE or layer.is_subject
        )
        self._crop_btn.state(["!disabled"] if can_crop else ["disabled"])

    def _refresh_all(self) -> None:
        self._refresh_layer_list()
        self._refresh_props()
        self._redraw_canvas()

    def _apply_props(self) -> None:
        if self._props_guard:
            return
        layer = self._selected_layer()
        if not layer:
            return
        try:
            layer.name = self._prop_name.get().strip() or layer.name
            layer.transform.offset_x = float(self._prop_offset_x.get())
            layer.transform.offset_y = float(self._prop_offset_y.get())
            layer.transform.scale = float(self._prop_scale.get())
            layer.opacity = float(self._prop_opacity.get())
            self._scale_pct_var.set(f"{int(round(layer.transform.scale * 100))}%")
            if layer.type == "image":
                new_src = self._prop_source.get().strip()
                if layer.is_subject or layer.source == ASSET_SUBJECT_SOURCE:
                    layer.source = ASSET_SUBJECT_SOURCE
                else:
                    layer.source = new_src
            elif layer.text is not None:
                layer.text.content = self._prop_text.get()
                layer.text.font_family = self._prop_font.get().strip() or "PingFang SC"
                layer.text.font_size = int(self._prop_font_size.get())
                layer.text.color = self._prop_color.get().strip() or "#FFFFFF"
        except (tk.TclError, ValueError):
            return
        self._refresh_layer_list()
        self._schedule_redraw()

    def _apply_offset_props(self) -> None:
        if self._props_guard or self._dragging_layer:
            return
        layer = self._selected_layer()
        if not layer:
            return
        try:
            layer.transform.offset_x = float(self._prop_offset_x.get())
            layer.transform.offset_y = float(self._prop_offset_y.get())
        except (tk.TclError, ValueError):
            return
        self._schedule_redraw()

    def _set_layer_offset(self, layer: Layer, x: float, y: float) -> None:
        layer.transform.offset_x = x
        layer.transform.offset_y = y
        self._props_guard = True
        try:
            self._prop_offset_x.set(x)
            self._prop_offset_y.set(y)
        finally:
            self._props_guard = False
        self._schedule_redraw()

    def _reset_layer_offset(self) -> None:
        layer = self._selected_layer()
        if not layer or layer.locked:
            return
        self._set_layer_offset(layer, 0.0, 0.0)
        self._status_var.set(f"{layer.name} · 已归位 (0, 0)")

    def _center_layer_x(self) -> None:
        layer = self._selected_layer()
        if not layer or layer.locked:
            return
        self._set_layer_offset(layer, 0.0, layer.transform.offset_y)
        self._status_var.set(f"{layer.name} · X 已归零")

    def _center_layer_y(self) -> None:
        layer = self._selected_layer()
        if not layer or layer.locked:
            return
        self._set_layer_offset(layer, layer.transform.offset_x, 0.0)
        self._status_var.set(f"{layer.name} · Y 已归零")

    def _reset_layer_scale(self) -> None:
        layer = self._selected_layer()
        if not layer or layer.locked:
            return
        layer.transform.scale = 1.0
        self._props_guard = True
        try:
            self._prop_scale.set(1.0)
            self._scale_pct_var.set("100%")
        finally:
            self._props_guard = False
        self._schedule_redraw()
        self._status_var.set(f"{layer.name} · 缩放 100%")

    def _reset_layer_transform(self) -> None:
        layer = self._selected_layer()
        if not layer or layer.locked:
            return
        layer.transform.scale = 1.0
        layer.opacity = 1.0
        self._set_layer_offset(layer, 0.0, 0.0)
        self._props_guard = True
        try:
            self._prop_scale.set(1.0)
            self._prop_opacity.set(1.0)
            self._scale_pct_var.set("100%")
        finally:
            self._props_guard = False
        self._schedule_redraw()
        self._status_var.set(f"{layer.name} · 已重置位置/缩放/不透明度")

    def _nudge_selected_layer(self, dx: int, dy: int, evt: tk.Event) -> str | None:
        if self._crop_mode:
            return None
        layer = self._selected_layer()
        if not layer or layer.locked or not layer.visible:
            return None
        step = 10 if evt.state & 0x0001 else 1
        self._set_layer_offset(
            layer,
            layer.transform.offset_x + dx * step,
            layer.transform.offset_y + dy * step,
        )
        return "break"

    def _on_layer_double_click(self, _evt: tk.Event) -> None:
        sel = self._layer_list.curselection()
        if not sel:
            return
        layer = self._layer_from_display_index(sel[0])
        if not layer:
            return
        self._selected_id = layer.id
        self._refresh_props()
        self._zoom_fit()
        self._status_var.set(f"已选中 · {layer.name}")

    def _on_layer_solo_click(self, evt: tk.Event) -> None:
        idx = self._layer_list.nearest(evt.y)
        layer = self._layer_from_display_index(idx)
        if not layer:
            return
        self._selected_id = layer.id
        self._solo_var.set(True)
        self._solo_id = layer.id
        self._refresh_all()
        return "break"

    def _on_layer_select(self, _evt=None) -> None:
        sel = self._layer_list.curselection()
        if not sel:
            return
        layer = self._layer_from_display_index(sel[0])
        if layer:
            self._selected_id = layer.id
            self._refresh_props()
            self._redraw_canvas()

    def _add_image_layer(self) -> None:
        layer = Layer.new_image("图片")
        self._stack.layers.append(layer)
        self._selected_id = layer.id
        self._refresh_all()

    def _add_text_layer(self) -> None:
        layer = Layer.new_text()
        self._stack.layers.append(layer)
        self._selected_id = layer.id
        self._refresh_all()

    def _move_layer_up(self) -> None:
        layer = self._selected_layer()
        if not layer:
            return
        idx = self._stack.layers.index(layer)
        if idx >= len(self._stack.layers) - 1:
            return
        self._stack.layers[idx], self._stack.layers[idx + 1] = (
            self._stack.layers[idx + 1],
            self._stack.layers[idx],
        )
        self._refresh_all()

    def _move_layer_down(self) -> None:
        layer = self._selected_layer()
        if not layer:
            return
        idx = self._stack.layers.index(layer)
        if idx <= 0:
            return
        self._stack.layers[idx], self._stack.layers[idx - 1] = (
            self._stack.layers[idx - 1],
            self._stack.layers[idx],
        )
        self._refresh_all()

    def _toggle_visible(self) -> None:
        layer = self._selected_layer()
        if not layer:
            return
        layer.visible = not layer.visible
        self._refresh_all()

    def _toggle_locked(self) -> None:
        layer = self._selected_layer()
        if not layer:
            return
        layer.locked = not layer.locked
        self._refresh_all()

    def _duplicate_layer(self) -> None:
        layer = self._selected_layer()
        if not layer:
            return
        clone = layer.clone()
        idx = self._stack.layers.index(layer)
        self._stack.layers.insert(idx + 1, clone)
        self._selected_id = clone.id
        self._refresh_all()

    def _delete_layer(self) -> None:
        layer = self._selected_layer()
        if not layer:
            return
        if layer.source == ASSET_SUBJECT_SOURCE or layer.is_subject:
            messagebox.showwarning("后处理", "不能删除主体层（$asset）", parent=self)
            return
        if not messagebox.askyesno("删除图层", f"删除「{layer.name}」？", parent=self):
            return
        self._stack.layers = [item for item in self._stack.layers if item.id != layer.id]
        self._selected_id = self._stack.layers[-1].id if self._stack.layers else None
        self._refresh_all()

    def _browse_source(self) -> None:
        layer = self._selected_layer()
        if not layer or layer.type != "image":
            return
        path = filedialog.askopenfilename(
            parent=self,
            title="选择图片",
            initialdir=str(self.config_mgr.art_root()),
            filetypes=[("PNG", "*.png"), ("All", "*.*")],
        )
        if not path:
            return
        try:
            rel = Path(path).resolve().relative_to(self.config_mgr.art_root())
            layer.source = rel.as_posix()
        except ValueError:
            layer.source = path
        self._prop_source.set(layer.source)
        self._redraw_canvas()

    def _pick_color(self) -> None:
        color = colorchooser.askcolor(parent=self, title="文字颜色")
        if color and color[1]:
            self._prop_color.set(color[1])

    def _load_template(self) -> None:
        tid = self._template_var.get()
        if not messagebox.askyesno("加载模板", f"加载模板「{tid}」？当前层栈将被替换。", parent=self):
            return
        stack = builtin_template(tid, self.asset.width, self.asset.height)
        old_subject = self._stack.subject_layer()
        self._stack = stack
        if old_subject and old_subject.crop:
            subj = self._stack.ensure_subject_layer()
            subj.crop = old_subject.crop
            subj.transform = old_subject.transform
        self._selected_id = self._stack.layers[-1].id if self._stack.layers else None
        self._refresh_all()

    def _save_as_template(self) -> None:
        from tkinter import simpledialog

        tid = simpledialog.askstring("存为模板", "模板 ID（英文）:", parent=self)
        if not tid:
            return
        save_template(self.config_mgr.data, tid.strip(), self._stack)
        cat = self.config_mgr.category_by_id(self.asset.category)
        if cat:
            for raw in self.config_mgr.data.get("categories", []):
                if raw.get("id") == cat.id:
                    raw["postprocess_template"] = tid.strip()
                    break
        self.config_mgr.save()
        messagebox.showinfo("模板", f"已保存模板 {tid}", parent=self)

    def _on_solo_toggle(self) -> None:
        if self._solo_var.get() and self._selected_id:
            self._solo_id = self._selected_id
        else:
            self._solo_id = None
        self._redraw_canvas()

    def _toggle_crop_mode(self) -> None:
        if self._crop_mode:
            self._exit_crop_mode()
        else:
            self._enter_crop_mode()

    def _enter_crop_mode(self) -> None:
        layer = self._selected_layer()
        if not layer or layer.type != "image":
            return
        if layer.source != ASSET_SUBJECT_SOURCE and not layer.is_subject:
            return
        self._crop_mode = True
        self._crop_btn.configure(text="完成裁切")
        resolver = self._resolver()
        from postprocess.models import layer_image_source

        raw = resolver.resolve(layer_image_source(layer))
        if raw and layer.crop:
            self._crop_preview = layer.crop.clamp_to(raw.width, raw.height)
        elif raw:
            side = min(raw.width, raw.height)
            self._crop_preview = CropRect(
                x=(raw.width - side) // 2,
                y=(raw.height - side) // 2,
                w=side,
                h=side,
            )
        self._status_var.set("裁切模式：拖拽框选 · Enter 确认 · Esc 取消")
        self._redraw_canvas()

    def _exit_crop_mode(self) -> None:
        self._crop_mode = False
        self._crop_preview = None
        self._crop_drag_start = None
        self._crop_btn.configure(text="裁切 (C)")
        self._status_var.set("")
        self._redraw_canvas()

    def _commit_crop(self) -> None:
        if not self._crop_mode:
            return
        layer = self._selected_layer()
        if layer and self._crop_preview:
            layer.crop = self._crop_preview
        self._exit_crop_mode()

    def _canvas_to_doc(self, cx: float, cy: float) -> tuple[float, float]:
        ox, oy = self._canvas_offset()
        return (cx - ox) / self._view_zoom, (cy - oy) / self._view_zoom

    def _canvas_offset(self) -> tuple[float, float]:
        cw = max(self._canvas.winfo_width(), 1)
        ch = max(self._canvas.winfo_height(), 1)
        doc_w = self._stack.canvas_width * self._view_zoom
        doc_h = self._stack.canvas_height * self._view_zoom
        ox = (cw - doc_w) / 2 + self._pan_x
        oy = (ch - doc_h) / 2 + self._pan_y
        return ox, oy

    def _on_canvas_press(self, evt: tk.Event) -> None:
        self._canvas.focus_set()
        doc_x, doc_y = self._canvas_to_doc(evt.x, evt.y)
        if self._crop_mode:
            self._crop_drag_start = (doc_x, doc_y)
            self._crop_preview = CropRect(x=int(doc_x), y=int(doc_y), w=1, h=1)
            try:
                self._canvas.grab_set()
            except tk.TclError:
                pass
            return
        layer = hit_test(self._stack, self._resolver(), doc_x, doc_y)
        if layer:
            self._selected_id = layer.id
            if not layer.locked and layer.visible:
                self._drag_layer_id = layer.id
                self._drag_last = (doc_x, doc_y)
                self._dragging_layer = True
                try:
                    self._canvas.grab_set()
                except tk.TclError:
                    pass
            self._refresh_layer_list()
            self._refresh_props()
        self._redraw_canvas()

    def _on_canvas_drag(self, evt: tk.Event) -> None:
        doc_x, doc_y = self._canvas_to_doc(evt.x, evt.y)
        if self._crop_mode and self._crop_drag_start and self._crop_preview:
            x0, y0 = self._crop_drag_start
            x1, y1 = doc_x, doc_y
            x = int(min(x0, x1))
            y = int(min(y0, y1))
            w = max(1, int(abs(x1 - x0)))
            h = max(1, int(abs(y1 - y0)))
            if evt.state & 0x0001:
                side = max(w, h)
                w = h = side
            layer = self._selected_layer()
            resolver = self._resolver()
            if layer:
                from postprocess.models import layer_image_source

                raw = resolver.resolve(layer_image_source(layer))
                if raw:
                    c = CropRect(x=x, y=y, w=w, h=h).clamp_to(raw.width, raw.height)
                    self._crop_preview = c
                    self._status_var.set(f"裁切 {c.w}×{c.h} px @ ({c.x},{c.y})")
            self._schedule_redraw(immediate=True)
            return
        if not self._drag_layer_id or not self._drag_last:
            return
        layer = self._selected_layer()
        if not layer or layer.id != self._drag_layer_id or layer.locked or not layer.visible:
            return
        lx, ly = self._drag_last
        dx, dy = doc_x - lx, doc_y - ly
        if dx == 0 and dy == 0:
            return
        layer.transform.offset_x += dx
        layer.transform.offset_y += dy
        self._drag_last = (doc_x, doc_y)
        self._props_guard = True
        try:
            self._prop_offset_x.set(layer.transform.offset_x)
            self._prop_offset_y.set(layer.transform.offset_y)
        finally:
            self._props_guard = False
        self._schedule_redraw(immediate=True)

    def _on_canvas_release(self, _evt: tk.Event) -> None:
        self._dragging_layer = False
        self._drag_layer_id = None
        self._drag_last = None
        try:
            self._canvas.grab_release()
        except tk.TclError:
            pass

    def _on_wheel(self, evt: tk.Event) -> str:
        delta = evt.delta
        if delta > 0:
            self._zoom_by(1.1)
        elif delta < 0:
            self._zoom_by(0.9)
        return "break"

    def _zoom_by(self, factor: float) -> None:
        self._view_zoom = max(VIEW_MIN_ZOOM, min(VIEW_MAX_ZOOM, self._view_zoom * factor))
        self._zoom_var.set(f"{int(self._view_zoom * 100)}%")
        self._redraw_canvas()

    def _zoom_fit(self) -> None:
        cw = max(self._canvas.winfo_width(), 1)
        ch = max(self._canvas.winfo_height(), 1)
        zx = cw / max(self._stack.canvas_width, 1)
        zy = ch / max(self._stack.canvas_height, 1)
        self._view_zoom = max(VIEW_MIN_ZOOM, min(VIEW_MAX_ZOOM, min(zx, zy) * 0.9))
        self._zoom_var.set(f"{int(self._view_zoom * 100)}%")
        self._pan_x = self._pan_y = 0
        self._redraw_canvas()

    def _schedule_redraw(self, *, immediate: bool = False) -> None:
        if immediate:
            job = self._redraw_job
            self._redraw_job = None
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
            self._redraw_canvas()
            return
        if self._redraw_job is not None:
            return
        try:
            self._redraw_job = self.after(16, self._run_scheduled_redraw)
        except tk.TclError:
            pass

    def _run_scheduled_redraw(self) -> None:
        self._redraw_job = None
        try:
            if self.winfo_exists():
                self._redraw_canvas()
        except tk.TclError:
            pass

    def _redraw_canvas(self) -> None:
        from PIL import Image, ImageDraw, ImageTk

        cw = max(self._canvas.winfo_width(), 1)
        ch = max(self._canvas.winfo_height(), 1)
        self._canvas.delete("all")

        solo = self._solo_id if self._solo_var.get() else None
        doc = render_stack(self._stack, self._resolver(), solo_layer_id=solo)
        bg = stack_checkerboard(self._stack.canvas_width, self._stack.canvas_height)
        bg.alpha_composite(doc)
        doc = bg

        if self._crop_mode and self._crop_preview and self._selected_layer():
            draw = ImageDraw.Draw(doc)
            c = self._crop_preview
            draw.rectangle([c.x, c.y, c.x + c.w - 1, c.y + c.h - 1], outline=(0, 200, 255, 255), width=2)

        zoom = self._view_zoom
        out_w = max(1, int(doc.width * zoom))
        out_h = max(1, int(doc.height * zoom))
        scaled = doc.resize((out_w, out_h), Image.Resampling.NEAREST)
        self._canvas_photo = ImageTk.PhotoImage(scaled)
        ox, oy = self._canvas_offset()
        self._canvas.create_image(ox, oy, anchor=tk.NW, image=self._canvas_photo)

        resolver = self._resolver()
        scratch = doc.copy()
        for layer in self._stack.layers:
            if solo and layer.id != solo:
                continue
            if not layer.visible:
                continue
            bounds = layer_bounds(layer, self._stack, resolver, scratch=scratch)
            if not bounds:
                continue
            x0 = ox + bounds.x * zoom
            y0 = oy + bounds.y * zoom
            x1 = ox + bounds.x2 * zoom
            y1 = oy + bounds.y2 * zoom
            color = "#00aaff" if layer.id == self._selected_id else "#888888"
            self._canvas.create_rectangle(x0, y0, x1, y1, outline=color, dash=(4, 2))
        if not self._crop_mode:
            layer = self._selected_layer()
            if layer:
                self._status_var.set(
                    f"{layer.name} · offset ({layer.transform.offset_x:.0f}, {layer.transform.offset_y:.0f})"
                    f" · scale {layer.transform.scale:.2f}"
                )

    def _persist_stack(self) -> None:
        self.config_mgr.set_postprocess_stack(self.asset.id, self._stack)

    def _apply_to_inbox(self) -> None:
        self._apply_props()
        _src, inbox, _unity = self.config_mgr.resolve_paths(self.asset)
        try:
            from postprocess.engine import render_stack_to_png_bytes

            data = render_stack_to_png_bytes(self._stack, self._resolver())
            inbox.parent.mkdir(parents=True, exist_ok=True)
            inbox.write_bytes(data)
            self._persist_stack()
            messagebox.showinfo("后处理", f"已写入 inbox:\n{inbox}", parent=self)
            if self.on_applied:
                self.on_applied()
        except Exception as exc:
            messagebox.showerror("后处理失败", str(exc), parent=self)

    def _on_close(self) -> None:
        job = self._redraw_job
        self._redraw_job = None
        if job is not None:
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
        try:
            self._canvas.grab_release()
        except tk.TclError:
            pass
        try:
            self._apply_props()
            self._persist_stack()
        except tk.TclError:
            pass
        try:
            if hasattr(self, "_page_scroll"):
                self._page_scroll.uninstall_global_wheel()
        except tk.TclError:
            pass
        try:
            self.withdraw()
        except tk.TclError:
            pass
        try:
            self.destroy()
        except tk.TclError:
            pass
