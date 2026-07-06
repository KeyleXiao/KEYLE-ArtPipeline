#!/usr/bin/env python3
"""ArtPipeline 美术工具可视化入口。

启动:
  python3 ArtPipeline/tools/artTool_ui.py
  或 ./ArtPipeline/tools/run_art_tool.sh
"""

from __future__ import annotations

import io
import json
import queue
import subprocess
import sys
import threading
import tkinter as tk
from collections.abc import Callable
from datetime import datetime
from functools import partial
from pathlib import Path
from tkinter import messagebox, simpledialog
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

try:
    import ttkbootstrap as ttk
    from ttkbootstrap.constants import SECONDARY

    BaseWindow = ttk.Window
    PanedWindow = ttk.Panedwindow
    HAS_TTB = True
except ImportError:
    BaseWindow = tk.Tk  # type: ignore[misc, assignment]
    from tkinter import ttk

    PanedWindow = ttk.PanedWindow
    HAS_TTB = False
    SECONDARY = ""


def labeled_frame(
    parent: Any,
    text: str,
    *,
    padding: int | tuple[int, ...] = 8,
    **kwargs: Any,
) -> tuple[Any, ttk.Frame]:
    """LabelFrame 内嵌 Frame（macOS / ttkbootstrap 不支持 LabelFrame 的 padding 参数）。"""
    outer = ttk.LabelFrame(parent, text=text, **kwargs)
    body = ttk.Frame(outer, padding=padding)
    body.pack(fill=tk.BOTH, expand=True)
    return outer, body


from ui_layout import ScrollableFrame, fit_toplevel_to_screen  # noqa: E402

from ai_assistant import (  # noqa: E402
    AiAssistantError,
    build_context_message,
    chat as ai_chat,
    parse_ai_response,
    resolve_model,
    SUPPORTED_MODELS,
    SYSTEM_PROMPT,
)
from config_manager import (  # noqa: E402
    REMOVE_BG_INHERIT,
    REMOVE_BG_KEEP,
    REMOVE_BG_REMOVE,
    Asset,
    Category,
    ConfigManager,
)
from pipeline_core import PipelineCore  # noqa: E402
from workflow_engine import validate_workflow_json  # noqa: E402

try:
    from postprocess_editor import open_postprocess_editor
    from postprocess.engine import AssetImageResolver, render_stack
    from postprocess.models import stack_from_dict
    HAS_POSTPROCESS = True
except ImportError:
    HAS_POSTPROCESS = False
    open_postprocess_editor = None  # type: ignore[misc, assignment]

try:
    from art_tool_theme import (
        ACCENT_ERR,
        ACCENT_OK,
        ACCENT_WARN,
        BG_APP,
        FG_MUTED,
        PRODUCT_NAME,
        PRODUCT_TAGLINE,
        apply_app_styles,
        load_app_icon_photos,
        style_listbox,
        style_preview_label,
        style_text_widget,
    )
except ImportError:
    PRODUCT_NAME = "ArtPipeline Studio @keyke"
    PRODUCT_TAGLINE = "游戏美术流水线"
    ACCENT_OK = ACCENT_WARN = ACCENT_ERR = FG_MUTED = "#888888"

    def apply_app_styles(*_a, **_k): ...
    def load_app_icon_photos(_root): return {}
    def style_listbox(_): ...
    def style_preview_label(_): ...
    def style_text_widget(_w, **kwargs): ...

try:
    from alpha_matte import border_matte_to_alpha
except ImportError:
    border_matte_to_alpha = None  # type: ignore[misc, assignment]

PREVIEW_SIZE = 220
UI_THEME = "darkly"
PROGRESS_IDLE = "就绪"
COMFYUI_POLL_MS = 2000


class ArtToolApp(BaseWindow):
    def __init__(self) -> None:
        if HAS_TTB:
            super().__init__(themename=UI_THEME, title=PRODUCT_NAME)
        else:
            super().__init__()
            self.title(PRODUCT_NAME)
        apply_app_styles(self, has_ttb=HAS_TTB)
        self._icon_photos = load_app_icon_photos(self)
        screen_w, screen_h = fit_toplevel_to_screen(
            self,
            prefer_w=1280,
            prefer_h=840,
            min_w=720,
            min_h=480,
        )
        self._compact_ui = screen_h < 820 or screen_w < 1100

        self.config_mgr = ConfigManager()
        self.pipeline = PipelineCore(self.config_mgr)
        self._busy = False
        self._selected_category: str | None = None
        self._cat_list_ids: list[str] = []
        self._selected_asset_id: str | None = None
        self._checkpoint_list: list[str] = []
        self._preview_images: dict[str, Any] = {}
        self._current_preview_asset: Asset | None = None
        self._preview_source_key = tk.StringVar(value="inbox")
        self._postprocess_preview_var = tk.BooleanVar(value=False)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._schedule_refresh_assets())

        self._prog_batch_idx = 0
        self._prog_batch_total = 1
        self._prog_step_pct = 0
        self._comfyui_poll_job: str | None = None
        self._ui_queue: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._log_entries: list[dict[str, str]] = []
        self._log_window: tk.Toplevel | None = None
        self._log_text_widgets: dict[str, tk.Text] = {}
        self._log_notebook: ttk.Notebook | None = None
        self._last_comfyui_status: str | None = None
        self._ai_histories: dict[str, list[dict[str, str]]] = {}
        self._ai_busy = False
        self._closing = False
        self._asset_load_seq = 0
        self._preview_load_seq = 0
        self._category_switch_seq = 0
        self._asset_status_seq = 0
        self._refresh_assets_job: str | None = None
        self._asset_tree_refreshing = False
        self._pending_category_paths: Category | None = None
        self._detail_scroll: ScrollableFrame | None = None
        self._asset_list_job: str | None = None
        self._asset_list_seq = 0
        self._list_status_var = tk.StringVar(value="")
        self._category_paths_loaded_for: str | None = None
        self._asset_rows_cache: dict[tuple[str, str], list[tuple[str, str, str, str, str, str]]] = {}
        self._prompt_panel_asset_id: str | None = None
        self._workflow_loaded_for: str | None = None

        self._build_header()
        self._build_status_bar()
        self._build_main()
        self._apply_widget_styles()
        self._refresh_categories()
        self._fetch_comfyui_status_async(refresh_checkpoints=True)
        self._poll_comfyui_status()
        self._process_ui_queue()
        self.after(200, self._log_startup_info)
        if self._compact_ui:
            self.after(250, self._collapse_log_panel)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI 构建 ─────────────────────────────────────────────

    def _build_header(self) -> None:
        """产品化顶栏：品牌 + 主操作 + 菜单。"""
        shell = ttk.Frame(self)
        shell.pack(side=tk.TOP, fill=tk.X)

        top = ttk.Frame(shell, padding=(18, 12, 18, 8))
        top.pack(fill=tk.X)

        brand = ttk.Frame(top)
        brand.pack(side=tk.LEFT, fill=tk.Y)
        title_style = "BrandTitle.TLabel" if HAS_TTB else None
        sub_style = "BrandSubtitle.TLabel" if HAS_TTB else None
        brand_row = ttk.Frame(brand)
        brand_row.pack(anchor=tk.W)
        if self._icon_photos.get("brand"):
            icon_bg = BG_APP if HAS_TTB else self.cget("bg")
            tk.Label(
                brand_row,
                image=self._icon_photos["brand"],
                borderwidth=0,
                bg=icon_bg,
            ).pack(side=tk.LEFT, padx=(0, 10))
        title_col = ttk.Frame(brand_row)
        title_col.pack(side=tk.LEFT, fill=tk.Y)
        ttk.Label(title_col, text=PRODUCT_NAME, style=title_style).pack(anchor=tk.W)
        ttk.Label(title_col, text=PRODUCT_TAGLINE, style=sub_style).pack(anchor=tk.W, pady=(2, 0))

        actions = ttk.Frame(top)
        actions.pack(side=tk.RIGHT)
        primary = {"bootstyle": "success"} if HAS_TTB else {}
        info = {"bootstyle": "info"} if HAS_TTB else {}
        secondary = {"bootstyle": "secondary"} if HAS_TTB else {}
        outline = {"bootstyle": "outline-secondary"} if HAS_TTB else {}
        ttk.Button(actions, text="生成选中", command=self._generate_selected, width=10, **primary).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(actions, text="生成本类", command=self._generate_category, width=10, **info).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(
            actions,
            text="生成并导出",
            command=self._generate_and_export_selected,
            width=11,
            **secondary,
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(actions, text="导出 Unity", command=self._export_selected, width=10, **outline).pack(
            side=tk.LEFT
        )

        menu_defs: list[tuple[str, list[tuple[str, Any] | None]]] = [
            (
                "文件",
                [
                    ("保存配置", self._save_config),
                    ("重新加载配置", self._reload_config),
                    None,
                    ("初始化缺失工作流 JSON", self._init_workflows),
                    None,
                    ("退出", self._on_close),
                ],
            ),
            ("新建", [("新建资源", self._new_asset), ("新建分类", self._new_category)]),
            (
                "导出",
                [
                    ("导出选中", self._export_selected),
                    ("导出本类", self._export_category),
                ],
            ),
            (
                "日志",
                [
                    ("查看日志", self._show_log_window),
                    ("复制全部日志", self._copy_all_logs),
                    None,
                    ("清空日志", self._clear_logs),
                ],
            ),
            (
                "帮助",
                [
                    ("打开维护文档", self._open_readme),
                    ("打开 ArtPipeline 目录", lambda: self._open_path(self.config_mgr.art_root())),
                ],
            ),
        ]

        menu_row = ttk.Frame(shell, padding=(14, 0, 14, 10))
        menu_row.pack(fill=tk.X)
        for label, items in menu_defs:
            self._add_menu_dropdown(menu_row, label, items)

        ttk.Separator(self, orient=tk.HORIZONTAL).pack(side=tk.TOP, fill=tk.X)

    def _section_label(self, parent: ttk.Frame, text: str) -> None:
        style = "Section.TLabel" if HAS_TTB else None
        ttk.Label(parent, text=text, style=style).pack(anchor=tk.W, pady=(0, 4))

    def _hint_label(self, parent: ttk.Frame, text: str, **pack_kw: Any) -> ttk.Label:
        style = "Hint.TLabel" if HAS_TTB else None
        lbl = ttk.Label(parent, text=text, style=style)
        if not HAS_TTB:
            lbl.configure(foreground=FG_MUTED)
        lbl.pack(**{"anchor": tk.W, **pack_kw})
        return lbl

    def _apply_widget_styles(self) -> None:
        style_listbox(self.cat_list)
        style_preview_label(self.preview_thumb)
        try:
            self.asset_tree.tag_configure("odd", background="#2e2e34")
            self.asset_tree.tag_configure("even", background="#26262c")
        except tk.TclError:
            pass
        for w in (
            self.positive_text,
            self.negative_text,
            self.ai_input_text,
            self.workflow_text,
            self.category_positive_common_text,
            self.category_negative_common_text,
            self.log_text,
        ):
            style_text_widget(w, mono=w in (self.workflow_text, self.positive_text, self.negative_text))
        style_text_widget(self.ai_history_text, mono=True)

    def _add_menu_dropdown(
        self,
        parent: ttk.Frame,
        label: str,
        items: list[tuple[str, Any] | None],
    ) -> None:
        menu_kwargs: dict[str, Any] = {"tearoff": 0, **self._menu_style()}
        menu = tk.Menu(parent, **menu_kwargs)
        for item in items:
            if item is None:
                menu.add_separator()
            else:
                menu.add_command(label=item[0], command=item[1])

        if HAS_TTB:
            mb = ttk.Menubutton(parent, text=label, bootstyle=SECONDARY)
            mb.pack(side=tk.LEFT, padx=(0, 4))
            mb.configure(menu=menu)
            return

        btn = ttk.Button(parent, text=label, width=7)
        btn.pack(side=tk.LEFT, padx=(0, 6))

        def show_menu() -> None:
            try:
                menu.tk_popup(btn.winfo_rootx(), btn.winfo_rooty() + btn.winfo_height())
            finally:
                menu.grab_release()

        btn.configure(command=show_menu)

    def _menu_style(self) -> dict[str, Any]:
        if not HAS_TTB:
            return {}
        return {
            "bg": "#2b2b2b",
            "fg": "#e0e0e0",
            "activebackground": "#404040",
            "activeforeground": "#ffffff",
            "borderwidth": 0,
        }

    def _on_asset_tree_context(self, event: tk.Event) -> None:
        row_id = self.asset_tree.identify_row(event.y)
        if not row_id:
            return
        if row_id not in self.asset_tree.selection():
            self.asset_tree.selection_set(row_id)
            self._on_asset_select()
        assets = self._current_assets()
        if not assets:
            return
        menu = self._create_asset_context_menu(assets)
        menu.post(event.x_root, event.y_root)

    def _create_asset_context_menu(self, assets: list[Asset]) -> tk.Menu:
        menu = tk.Menu(self, tearoff=0, **self._menu_style())
        n = len(assets)
        suffix = f" ({n} 项)" if n > 1 else ""
        primary = assets[0]

        if self._busy:
            menu.add_command(label="取消生成", command=self._cancel_generation)
            menu.add_separator()

        menu.add_command(label=f"生成{suffix}", command=partial(self._defer, self._run_generate, list(assets)))
        menu.add_command(
            label=f"生成并导出{suffix}",
            command=partial(self._defer, self._run_generate_batch, list(assets), export_after=True),
        )
        menu.add_command(label=f"导出到 Unity{suffix}", command=partial(self._defer, self._run_export, list(assets)))
        menu.add_separator()

        if n == 1:
            for kind, label in [
                ("source", "打开 source 文件"),
                ("inbox", "打开 inbox 文件"),
                ("unity", "打开 Unity 文件"),
            ]:
                menu.add_command(
                    label=label,
                    command=lambda k=kind, asset=primary: self._open_asset_file(asset, k),
                )
            menu.add_separator()
            for kind, label in [
                ("source", "打开 source 文件夹"),
                ("inbox", "打开 inbox 文件夹"),
                ("unity", "打开 Unity 文件夹"),
            ]:
                menu.add_command(
                    label=label,
                    command=lambda k=kind, asset=primary: self._open_asset_dir(asset, k),
                )
            menu.add_separator()
            menu.add_command(label="复制资源", command=lambda a=primary: self._duplicate_asset(a))

        menu.add_command(label=f"删除配置{suffix}", command=self._delete_asset)
        return menu

    def _open_asset_file(self, asset: Asset, kind: str) -> None:
        src, inbox, unity = self.config_mgr.resolve_paths(asset)
        path = {"source": src, "inbox": inbox, "unity": unity}[kind]
        if path.is_file():
            subprocess.run(["open", str(path)], check=False)
        else:
            messagebox.showinfo("提示", f"文件不存在: {path.name}")

    def _open_asset_dir(self, asset: Asset, kind: str) -> None:
        src, inbox, unity = self.config_mgr.resolve_paths(asset)
        path = {"source": src, "inbox": inbox, "unity": unity}[kind]
        self._open_path(path.parent)

    def _build_status_bar(self) -> None:
        ttk.Separator(self, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X)
        bar = ttk.Frame(self, padding=(14, 6))
        bar.pack(side=tk.BOTTOM, fill=tk.X)

        self.status_var = tk.StringVar(value="ComfyUI: 检测中…")
        self.progress_var = tk.StringVar(value=PROGRESS_IDLE)

        status_left = ttk.Frame(bar)
        status_left.pack(side=tk.LEFT, fill=tk.Y)
        dot_bg = "#232328" if HAS_TTB else "#f0f0f0"
        self._comfy_dot = tk.Canvas(status_left, width=10, height=10, highlightthickness=0, bd=0, bg=dot_bg)
        self._comfy_dot.pack(side=tk.LEFT, padx=(0, 8))
        self._comfy_dot_id = self._comfy_dot.create_oval(1, 1, 9, 9, fill=ACCENT_WARN, outline="")

        status_style = "Status.TLabel" if HAS_TTB else None
        ttk.Label(status_left, textvariable=self.status_var, style=status_style).pack(side=tk.LEFT)

        prog_style = "Status.TLabel" if HAS_TTB else None
        self.progress_label = ttk.Label(
            bar,
            textvariable=self.progress_var,
            style=prog_style,
            width=20,
            anchor=tk.E,
        )
        self.progress_label.pack(side=tk.RIGHT, padx=(8, 0))
        self._set_progress_text(PROGRESS_IDLE)

    def _defer(self, fn: Callable[..., None], *args: Any, **kwargs: Any) -> None:
        """菜单项回调延迟到主循环（macOS 右键菜单须等菜单关闭后再执行）。"""
        self.after(0, lambda: fn(*args, **kwargs))

    def _safe_page_refresh(self) -> None:
        if self._closing or not self.winfo_exists():
            return
        scroll = getattr(self, "_detail_scroll", None)
        if scroll is not None:
            scroll.refresh()

    def _collapse_log_panel(self) -> None:
        if self._log_body.winfo_ismapped():
            self._toggle_log()

    def _build_main(self) -> None:
        body = ttk.Frame(self)
        body.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

        paned = PanedWindow(body, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        # 左侧：分类 + 资源 + 预览（固定区域，不随右侧详情滚动）
        left = ttk.Frame(paned)
        paned.add(left, weight=1)

        self._section_label(left, "分类")
        self.cat_list = tk.Listbox(left, height=3 if self._compact_ui else 4, exportselection=False)
        self.cat_list.pack(fill=tk.X, pady=(0, 8))
        self.cat_list.bind("<<ListboxSelect>>", self._on_category_select)
        self.cat_list.bind("<Button-1>", self._on_category_press, add="+")

        self._section_label(left, "资源")
        self._hint_label(left, "右键操作 · Ctrl/Shift 多选", pady=(0, 4))
        search_row = ttk.Frame(left)
        search_row.pack(fill=tk.X, pady=2)
        ttk.Label(search_row, text="筛选:").pack(side=tk.LEFT)
        ttk.Entry(search_row, textvariable=self.search_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)

        tree_h = 8 if self._compact_ui else 12
        cols = ("filename", "size", "inbox", "unity")
        self.asset_tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="extended", height=tree_h)
        self.asset_tree.heading("filename", text="文件名")
        self.asset_tree.heading("size", text="W×H")
        self.asset_tree.heading("inbox", text="in")
        self.asset_tree.heading("unity", text="U")
        self.asset_tree.column("filename", width=120)
        self.asset_tree.column("size", width=42, anchor=tk.CENTER)
        self.asset_tree.column("inbox", width=28, anchor=tk.CENTER)
        self.asset_tree.column("unity", width=28, anchor=tk.CENTER)
        self.asset_tree.pack(fill=tk.X, pady=2)
        list_status_style = "Muted.TLabel" if HAS_TTB else None
        ttk.Label(left, textvariable=self._list_status_var, style=list_status_style).pack(anchor=tk.W, pady=(0, 2))
        self.asset_tree.bind("<<TreeviewSelect>>", self._on_asset_select)
        for seq in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):
            self.asset_tree.bind(seq, self._on_asset_tree_context)

        preview_outer, preview_frame = labeled_frame(left, "预览", padding=10)
        preview_outer.pack(fill=tk.X, pady=(8, 4))
        self.asset_meta_var = tk.StringVar(value="")
        meta_style = "Muted.TLabel" if HAS_TTB else None
        ttk.Label(preview_frame, textvariable=self.asset_meta_var, style=meta_style).pack(anchor=tk.W)

        thumb_bg = "#2b2b2b" if HAS_TTB else "#e8e8e8"
        thumb_fg = "#888888" if HAS_TTB else "#666666"
        sq = ttk.Frame(preview_frame, width=PREVIEW_SIZE, height=PREVIEW_SIZE)
        sq.pack(pady=(6, 4))
        sq.pack_propagate(False)
        self.preview_thumb = tk.Label(
            sq,
            text="无图片",
            anchor=tk.CENTER,
            bg=thumb_bg,
            fg=thumb_fg,
            cursor="hand2",
        )
        self.preview_thumb.pack(fill=tk.BOTH, expand=True)
        self.preview_thumb.bind("<Button-1>", lambda _e: self._open_preview_file())

        ttk.Label(preview_frame, text="点击缩略图打开原图", foreground="#888").pack(anchor=tk.W)

        src_row = ttk.Frame(preview_frame)
        src_row.pack(fill=tk.X, pady=(6, 2))
        for key, title in [("inbox", "inbox"), ("source", "source"), ("unity", "Unity")]:
            rb_kwargs: dict[str, Any] = {
                "text": title,
                "variable": self._preview_source_key,
                "value": key,
                "command": self._refresh_preview,
            }
            if HAS_TTB:
                rb_kwargs["bootstyle"] = "toolbutton"
            ttk.Radiobutton(src_row, **rb_kwargs).pack(side=tk.LEFT, padx=(0, 4))

        preview_btns = ttk.Frame(preview_frame)
        preview_btns.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(preview_btns, text="刷新预览", command=self._refresh_preview).pack(side=tk.LEFT)
        if HAS_POSTPROCESS:
            ttk.Checkbutton(
                preview_btns,
                text="后处理预览",
                variable=self._postprocess_preview_var,
                command=self._refresh_preview,
            ).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Button(preview_btns, text="后处理…", command=self._open_postprocess_editor).pack(
                side=tk.RIGHT
            )

        btn_row = ttk.Frame(left)
        btn_row.pack(fill=tk.X, pady=(6, 0))
        del_kw = {"bootstyle": "danger-outline"} if HAS_TTB else {}
        ttk.Button(btn_row, text="删除", command=self._delete_asset, **del_kw).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="刷新列表", command=lambda: self._refresh_assets(scan_files=True)).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(btn_row, text="inbox", command=self._open_inbox, **({"bootstyle": "link"} if HAS_TTB else {})).pack(
            side=tk.RIGHT, padx=2
        )
        ttk.Button(btn_row, text="source", command=self._open_source, **({"bootstyle": "link"} if HAS_TTB else {})).pack(
            side=tk.RIGHT, padx=2
        )
        ttk.Button(btn_row, text="Unity", command=self._open_unity, **({"bootstyle": "link"} if HAS_TTB else {})).pack(
            side=tk.RIGHT, padx=2
        )

        # 右侧：详情（小屏时仅右侧可滚动，避免点分类触发整页 layout）
        right = ttk.Frame(paned)
        paned.add(right, weight=3)

        if self._compact_ui:
            self._detail_scroll = ScrollableFrame(right)
            self._detail_scroll.pack(fill=tk.BOTH, expand=True)
            detail_host = self._detail_scroll.interior
            self._detail_scroll.install_global_wheel()
        else:
            self._detail_scroll = None
            detail_host = right

        self.notebook = ttk.Notebook(detail_host)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_notebook_tab_changed)

        # Tab 1: 基本信息
        tab_basic = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab_basic, text="基本信息")

        form = ttk.Frame(tab_basic)
        form.pack(fill=tk.X)
        self.fields: dict[str, Any] = {}
        row = 0
        for name in ("ID", "文件名"):
            ttk.Label(form, text=f"{name}:").grid(row=row, column=0, sticky=tk.W, pady=3)
            self.fields[name] = ttk.Entry(form, width=50)
            self.fields[name].grid(row=row, column=1, sticky=tk.W)
            row += 1

        ttk.Label(form, text="分类:").grid(row=row, column=0, sticky=tk.W, pady=3)
        self.fields["分类"] = ttk.Combobox(form, values=[], state="readonly", width=48)
        self.fields["分类"].grid(row=row, column=1, sticky=tk.W)
        row += 1

        ttk.Label(form, text="尺寸 (W×H):").grid(row=row, column=0, sticky=tk.W, pady=3)
        size_inputs = ttk.Frame(form)
        size_inputs.grid(row=row, column=1, sticky=tk.W)
        ttk.Label(size_inputs, text="W").pack(side=tk.LEFT)
        self.fields["宽度"] = ttk.Entry(size_inputs, width=8)
        self.fields["宽度"].pack(side=tk.LEFT, padx=(2, 8))
        ttk.Label(size_inputs, text="H").pack(side=tk.LEFT)
        self.fields["高度"] = ttk.Entry(size_inputs, width=8)
        self.fields["高度"].pack(side=tk.LEFT, padx=2)
        row += 1

        ttk.Label(form, text="Seed:").grid(row=row, column=0, sticky=tk.W, pady=3)
        seed_row = ttk.Frame(form)
        seed_row.grid(row=row, column=1, sticky=tk.W)
        self.fields["Seed"] = ttk.Entry(seed_row, width=16)
        self.fields["Seed"].pack(side=tk.LEFT)
        ttk.Label(seed_row, text="留空=使用全局", foreground="#888").pack(side=tk.LEFT, padx=(8, 0))
        row += 1

        ttk.Label(form, text="说明:").grid(row=row, column=0, sticky=tk.W, pady=3)
        self.fields["说明"] = ttk.Entry(form, width=50)
        self.fields["说明"].grid(row=row, column=1, sticky=tk.W)
        row += 1

        ttk.Label(form, text="启用:").grid(row=row, column=0, sticky=tk.W, pady=3)
        self.fields["启用"] = tk.BooleanVar(value=True)
        enable_row = ttk.Frame(form)
        enable_row.grid(row=row, column=1, sticky=tk.W)
        ttk.Checkbutton(enable_row, variable=self.fields["启用"]).pack(side=tk.LEFT)
        ttk.Label(
            enable_row,
            text="（参与批量/本类 ComfyUI 生成；取消勾选则跳过，仍可单独导出到 Unity）",
            foreground="#888",
        ).pack(side=tk.LEFT, padx=(8, 0))
        row += 1

        ttk.Label(form, text="剔除背景:").grid(row=row, column=0, sticky=tk.NW, pady=3)
        self.fields["剔除背景模式"] = tk.StringVar(value=REMOVE_BG_INHERIT)
        remove_bg_row = ttk.Frame(form)
        remove_bg_row.grid(row=row, column=1, sticky=tk.W)
        rb_kw: dict[str, Any] = {"bootstyle": "toolbutton"} if HAS_TTB else {}
        for val, text in (
            (REMOVE_BG_INHERIT, "默认设置（跟随分类）"),
            (REMOVE_BG_REMOVE, "剔除纯色背景"),
            (REMOVE_BG_KEEP, "不剔除纯色背景"),
        ):
            ttk.Radiobutton(
                remove_bg_row,
                text=text,
                variable=self.fields["剔除背景模式"],
                value=val,
                command=self._on_remove_bg_mode_change,
                **rb_kw,
            ).pack(anchor=tk.W, pady=1)
        self._remove_bg_hint_var = tk.StringVar(value="")
        ttk.Label(remove_bg_row, textvariable=self._remove_bg_hint_var, foreground="#888").pack(
            anchor=tk.W, pady=(4, 0)
        )
        row += 1

        ttk.Button(tab_basic, text="保存基本信息", command=self._save_basic).pack(anchor=tk.W, pady=8)

        path_outer, path_frame = labeled_frame(tab_basic, "分类设置（路径 · Checkpoint · 通用提示词）", padding=8)
        path_outer.pack(fill=tk.X, pady=8)
        for key, label in [("source", "source"), ("inbox", "inbox"), ("unity", "Unity")]:
            ttk.Label(path_frame, text=f"{label}:").pack(anchor=tk.W)
            e = ttk.Entry(path_frame, width=70)
            e.pack(fill=tk.X, pady=2)
            self.fields[f"path_{key}"] = e
        ckpt_row = ttk.Frame(path_frame)
        ckpt_row.pack(fill=tk.X, pady=(6, 2))
        ttk.Label(ckpt_row, text="Checkpoint (本分类):").pack(side=tk.LEFT)
        self.category_ckpt_combo = ttk.Combobox(ckpt_row, width=52, values=[])
        self.category_ckpt_combo.pack(side=tk.LEFT, padx=4, fill=tk.X, expand=True)
        ttk.Button(ckpt_row, text="刷新列表", command=self._refresh_checkpoints).pack(side=tk.LEFT, padx=2)
        ttk.Label(path_frame, text="留空则使用「全局设置」中的默认 checkpoint", foreground="#666").pack(anchor=tk.W)
        matte_row = ttk.Frame(path_frame)
        matte_row.pack(fill=tk.X, pady=(8, 2))
        self.fields["分类剔除背景"] = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            matte_row,
            text="生成与预览时默认剔除纯色背景（本分类）",
            variable=self.fields["分类剔除背景"],
            command=self._on_category_remove_bg_change,
        ).pack(side=tk.LEFT)
        ttk.Label(
            path_frame,
            text="资源选「默认设置」时跟随此项；单资源可覆盖为强制剔除或不剔除",
            foreground="#666",
        ).pack(anchor=tk.W)
        ttk.Label(
            path_frame,
            text="通用提示词：生成时自动追加到本分类每个资源的正向/负向 prompt 末尾（如透明底）",
            foreground="#666",
        ).pack(anchor=tk.W, pady=(8, 2))
        ttk.Label(path_frame, text="分类通用正向:").pack(anchor=tk.W)
        self.category_positive_common_text = tk.Text(path_frame, height=2, wrap=tk.WORD)
        self.category_positive_common_text.pack(fill=tk.X, pady=2)
        ttk.Label(path_frame, text="分类通用负向:").pack(anchor=tk.W)
        self.category_negative_common_text = tk.Text(path_frame, height=2, wrap=tk.WORD)
        self.category_negative_common_text.pack(fill=tk.X, pady=2)
        ttk.Button(path_frame, text="保存分类设置", command=self._save_category_paths).pack(anchor=tk.W, pady=4)

        # Tab 2: 提示词与工作流 + AI
        tab_pw = ttk.Frame(self.notebook, padding=4)
        self.notebook.add(tab_pw, text="提示词与工作流")
        vpaned = PanedWindow(tab_pw, orient=tk.VERTICAL)
        vpaned.pack(fill=tk.X, anchor=tk.NW)

        prompt_outer, prompt_frame = labeled_frame(vpaned, "提示词", padding=6)
        vpaned.add(prompt_outer, weight=2)
        ttk.Label(prompt_frame, text="正向 prompt").pack(anchor=tk.W)
        self.positive_text = tk.Text(prompt_frame, height=5, wrap=tk.WORD)
        self.positive_text.pack(fill=tk.BOTH, expand=True, pady=2)
        ttk.Label(prompt_frame, text="负向 prompt").pack(anchor=tk.W)
        self.negative_text = tk.Text(prompt_frame, height=3, wrap=tk.WORD)
        self.negative_text.pack(fill=tk.BOTH, expand=True, pady=2)
        prompt_btns = ttk.Frame(prompt_frame)
        prompt_btns.pack(fill=tk.X, pady=2)
        ttk.Button(prompt_btns, text="保存提示词", command=self._save_prompts).pack(side=tk.LEFT)
        ttk.Button(prompt_btns, text="复制正向", command=lambda: self._copy_text(self.positive_text)).pack(side=tk.LEFT, padx=4)
        ttk.Button(prompt_btns, text="复制负向", command=lambda: self._copy_text(self.negative_text)).pack(side=tk.LEFT, padx=4)
        ttk.Button(prompt_btns, text="复制资源", command=self._duplicate_asset).pack(side=tk.RIGHT)

        ai_outer, ai_frame = labeled_frame(vpaned, "AI 助手 (DeepSeek)", padding=6)
        vpaned.add(ai_outer, weight=3)
        ttk.Label(
            ai_frame,
            text="描述想要的图标：先写常见生活物体（如放大镜、药片、扑克牌），再写特效（如紫光、毒雾）。AI 将按规范生成提示词并填入上方。",
            foreground="#888",
            wraplength=720,
        ).pack(anchor=tk.W, pady=(0, 4))
        self.ai_history_text = tk.Text(ai_frame, height=8, wrap=tk.WORD, state=tk.DISABLED, font=("Menlo", 10))
        self.ai_history_text.pack(fill=tk.BOTH, expand=True, pady=2)
        self.ai_history_text.tag_configure("user", foreground="#7ec8ff")
        self.ai_history_text.tag_configure("assistant", foreground="#a8e6a3")
        self.ai_history_text.tag_configure("system", foreground="#888888")
        ai_input_row = ttk.Frame(ai_frame)
        ai_input_row.pack(fill=tk.X, pady=2)
        self.ai_input_text = tk.Text(ai_input_row, height=3, wrap=tk.WORD)
        self.ai_input_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.ai_input_text.bind("<Control-Return>", lambda _e: (self._send_ai_message(), "break"))
        ai_btn_col = ttk.Frame(ai_input_row)
        ai_btn_col.pack(side=tk.RIGHT, padx=(6, 0))
        self.ai_send_btn = ttk.Button(ai_btn_col, text="发送", command=self._send_ai_message, width=8)
        self.ai_send_btn.pack(pady=2)
        ttk.Button(ai_btn_col, text="清空对话", command=self._clear_ai_chat, width=8).pack(pady=2)
        ttk.Label(ai_frame, text="Ctrl+Enter 发送 · API Key 在「全局设置」", foreground="#666").pack(anchor=tk.W)

        wf_outer, wf_frame = labeled_frame(vpaned, "工作流 JSON", padding=6)
        vpaned.add(wf_outer, weight=3)
        hint = (
            "ComfyUI API Format。占位符: {{POSITIVE}} {{NEGATIVE}} {{WIDTH}} {{HEIGHT}} {{SEED}} "
            "{{CHECKPOINT}} {{FILENAME_PREFIX}} {{STEPS}} {{CFG}} {{SAMPLER}} {{SCHEDULER}}"
        )
        ttk.Label(wf_frame, text=hint, justify=tk.LEFT, foreground="#888").pack(anchor=tk.W)
        self.workflow_text = tk.Text(wf_frame, height=10, wrap=tk.NONE, font=("Menlo", 10))
        self.workflow_text.pack(fill=tk.BOTH, expand=True, pady=4)
        wf_btns = ttk.Frame(wf_frame)
        wf_btns.pack(fill=tk.X)
        ttk.Button(wf_btns, text="从默认模板加载", command=self._load_default_workflow).pack(side=tk.LEFT)
        ttk.Button(wf_btns, text="校验 JSON", command=self._validate_workflow).pack(side=tk.LEFT, padx=4)
        ttk.Button(wf_btns, text="保存工作流", command=self._save_workflow).pack(side=tk.LEFT, padx=4)

        # Tab 4: 全局设置
        tab_settings = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab_settings, text="全局设置")
        self.settings_fields: dict[str, Any] = {}
        row = 0

        ttk.Label(tab_settings, text="Unity 项目根目录:").grid(row=row, column=0, sticky=tk.W, pady=4)
        root_row = ttk.Frame(tab_settings)
        root_row.grid(row=row, column=1, sticky=tk.W)
        self.settings_fields["project_root"] = ttk.Entry(root_row, width=56)
        self.settings_fields["project_root"].pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(root_row, text="浏览…", command=self._browse_project_root).pack(side=tk.LEFT, padx=(6, 0))
        row += 1

        ttk.Label(tab_settings, text="ArtPipeline 根目录:").grid(row=row, column=0, sticky=tk.W, pady=4)
        art_row = ttk.Frame(tab_settings)
        art_row.grid(row=row, column=1, sticky=tk.W)
        self.settings_fields["art_pipeline_root"] = ttk.Entry(art_row, width=56)
        self.settings_fields["art_pipeline_root"].pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(art_row, text="浏览…", command=self._browse_art_root).pack(side=tk.LEFT, padx=(6, 0))
        row += 1

        path_btn_row = ttk.Frame(tab_settings)
        path_btn_row.grid(row=row, column=1, sticky=tk.W, pady=(0, 4))
        ttk.Button(path_btn_row, text="自动检测当前路径", command=self._autodetect_root_paths).pack(side=tk.LEFT)
        row += 1

        ttk.Label(
            tab_settings,
            text="分类里的 source/inbox 相对 ArtPipeline 根目录；unity 相对 Unity 项目根目录。",
            foreground="#666",
        ).grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=(0, 8))
        row += 1

        for key, label in [
            ("comfyui_url", "ComfyUI URL"),
            ("steps", "Steps"),
            ("cfg", "CFG"),
            ("sampler", "Sampler"),
            ("scheduler", "Scheduler"),
            ("seed", "Seed"),
        ]:
            ttk.Label(tab_settings, text=f"{label}:").grid(row=row, column=0, sticky=tk.W, pady=4)
            e = ttk.Entry(tab_settings, width=50)
            e.grid(row=row, column=1, sticky=tk.W)
            self.settings_fields[key] = e
            row += 1

        ttk.Label(
            tab_settings,
            text="全局 Seed 留空则随机；资源可单独设置 Seed，优先使用资源自身的值。",
            foreground="#666",
        ).grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=(0, 4))
        row += 1

        ttk.Label(tab_settings, text="默认 Checkpoint:").grid(row=row, column=0, sticky=tk.W, pady=4)
        ckpt_global = ttk.Frame(tab_settings)
        ckpt_global.grid(row=row, column=1, sticky=tk.W)
        self.global_ckpt_combo = ttk.Combobox(ckpt_global, width=44, values=[])
        self.global_ckpt_combo.pack(side=tk.LEFT)
        ttk.Button(ckpt_global, text="刷新", command=self._refresh_checkpoints).pack(side=tk.LEFT, padx=4)
        row += 1

        ttk.Label(
            tab_settings,
            text="全局 checkpoint 作为各分类的回退；分类可单独指定不同模型。",
            foreground="#666",
        ).grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=(0, 8))
        row += 1

        ttk.Label(tab_settings, text="DeepSeek API Key:").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.settings_fields["deepseek_api_key"] = tk.Entry(tab_settings, width=50, show="•")
        self.settings_fields["deepseek_api_key"].grid(row=row, column=1, sticky=tk.W)
        row += 1

        ttk.Label(tab_settings, text="DeepSeek 模型:").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.settings_fields["deepseek_model"] = ttk.Combobox(
            tab_settings,
            width=47,
            values=list(SUPPORTED_MODELS),
            state="readonly",
        )
        self.settings_fields["deepseek_model"].grid(row=row, column=1, sticky=tk.W)
        row += 1

        ttk.Label(
            tab_settings,
            text="支持 deepseek-v4-flash（快）/ deepseek-v4-pro（强）；留空则用 flash。",
            foreground="#666",
        ).grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=(0, 4))
        row += 1

        ttk.Label(
            tab_settings,
            text="AI 助手用于生成提示词与工作流；Key 仅存于本地 pipeline_config.json。",
            foreground="#666",
        ).grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=(0, 8))
        row += 1

        ttk.Button(tab_settings, text="保存全局设置", command=self._save_settings).grid(
            row=row, column=0, columnspan=2, sticky=tk.W, pady=4
        )
        self._load_settings_fields()
        self._build_log(parent=body)
        if self._detail_scroll is not None:
            self.after(300, self._safe_page_refresh)

    def _build_log(self, parent: tk.Misc | None = None) -> None:
        host = parent if parent is not None else self
        wrap = ttk.Frame(host, padding=(0, 8, 0, 0))
        wrap.pack(fill=tk.X)

        hdr = ttk.Frame(wrap)
        hdr.pack(fill=tk.X, pady=(0, 4))
        self._section_label(hdr, "运行日志")
        link_kw = {"bootstyle": "link"} if HAS_TTB else {}
        self._log_toggle_btn = ttk.Button(hdr, text="收起", width=6, command=self._toggle_log, **link_kw)
        self._log_toggle_btn.pack(side=tk.RIGHT)

        self._log_body = ttk.Frame(wrap)
        self._log_body.pack(fill=tk.BOTH, expand=False)
        self.log_text = tk.Text(self._log_body, height=5, wrap=tk.WORD, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _toggle_log(self) -> None:
        if self._log_body.winfo_ismapped():
            self._log_body.pack_forget()
            self._log_toggle_btn.configure(text="展开")
        else:
            self._log_body.pack(fill=tk.BOTH, expand=False)
            self._log_toggle_btn.configure(text="收起")

    def _process_ui_queue(self) -> None:
        """每帧最多处理少量 UI 回调，避免后台线程把主线程长时间占满。"""
        processed = 0
        max_per_tick = 6
        while processed < max_per_tick:
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as exc:
                err = f"UI 回调错误: {exc}"
                print(err, file=sys.stderr)
                try:
                    self._log(err, kind="系统")
                except tk.TclError:
                    pass
            processed += 1
        if self.winfo_exists() and not self._closing:
            self.after(16, self._process_ui_queue)

    def _call_ui(self, fn: Callable[[], None]) -> None:
        """从工作线程安全地调度 UI 更新（macOS 上不可直接 after）。"""
        if self._closing:
            return
        self._ui_queue.put(fn)

    def _set_progress_text(self, text: str) -> None:
        self.progress_var.set(text)
        try:
            self.progress_label.configure(text=text)
        except tk.TclError:
            pass

    def _update_progress_label(self) -> None:
        if not self._busy:
            return
        total = max(self._prog_batch_total, 1)
        idx = max(self._prog_batch_idx, 1)
        overall = int(((idx - 1) + self._prog_step_pct / 100) / total * 100)
        overall = min(100, max(0, overall))
        if total > 1:
            self._set_progress_text(f"{self._prog_batch_idx}/{total} · {overall}%")
        else:
            self._set_progress_text(f"{overall}%")

    def _reset_progress_label(self) -> None:
        self._set_progress_text(PROGRESS_IDLE)

    def _on_progress_update(self, info: dict) -> None:
        kind = info.get("kind", "")
        if kind == "progress":
            val = int(info.get("value", 0))
            mx = max(int(info.get("max", 1)), 1)
            self._prog_step_pct = min(100, int(val / mx * 100))
            self._update_progress_label()
        elif kind == "batch":
            self._prog_batch_idx = int(info.get("index", 1))
            self._prog_batch_total = max(int(info.get("total", 1)), 1)
            self._prog_step_pct = 0
            self._set_progress_text("准备中…")
        elif kind in ("queue", "running") and self._busy:
            msg = info.get("message", "")
            if msg and self._prog_step_pct <= 0:
                self._set_progress_text(msg)
            elif msg:
                self._update_progress_label()
        elif kind == "executing" and self._busy:
            msg = info.get("message", "生成中")
            if self._prog_step_pct > 0:
                self._set_progress_text(f"{msg} · {self._prog_step_pct}%")
            else:
                self._set_progress_text(msg)
        elif kind == "status" and self._busy:
            msg = info.get("message", "")
            if msg and self._prog_step_pct <= 0:
                self._set_progress_text(msg if len(msg) <= 40 else "准备中…")

    def _make_progress_callback(self):
        return lambda info: self._call_ui(lambda i=info: self._on_progress_update(i))

    def _infer_log_kind(self, msg: str) -> str:
        if any(
            k in msg
            for k in (
                "生成",
                "FAIL ",
                "source →",
                "inbox →",
                "seed=",
                "──",
                "导出 ",
                "任务结束",
                "已取消",
                "ComfyUI interrupt",
                "无输出图",
            )
        ):
            return "生成"
        if any(k in msg for k in ("ComfyUI", "checkpoint", "UI 回调", "工具已启动", "配置文件:", "扫描")):
            return "系统"
        return "操作"

    def _log(self, msg: str, *, kind: str | None = None) -> None:
        kind = kind or self._infer_log_kind(msg)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [{kind}] {msg}"
        self._log_entries.append({"ts": ts, "kind": kind, "msg": msg, "line": line})

        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, line + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)
        self._refresh_log_window_text()

    def _log_lines_for_filter(self, tab: str) -> list[str]:
        if tab == "全部":
            return [e["line"] for e in self._log_entries]
        return [e["line"] for e in self._log_entries if e["kind"] == tab]

    def _refresh_log_window_text(self) -> None:
        if not self._log_text_widgets:
            return
        for tab, widget in self._log_text_widgets.items():
            content = "\n".join(self._log_lines_for_filter(tab))
            if content:
                content += "\n"
            widget.configure(state=tk.NORMAL)
            widget.delete("1.0", tk.END)
            widget.insert("1.0", content)
            widget.see(tk.END)
            widget.configure(state=tk.DISABLED)

    def _show_log_window(self) -> None:
        if self._log_window and self._log_window.winfo_exists():
            self._log_window.lift()
            self._log_window.focus_force()
            self._refresh_log_window_text()
            return

        win = tk.Toplevel(self)
        win.title("日志查看")
        win.geometry("820x540")
        win.minsize(640, 360)
        self._log_window = win

        hint = ttk.Label(
            win,
            text="操作 / 生成 / 系统分类日志，便于排查问题。可复制后发给开发者。",
            foreground="#888",
        )
        hint.pack(anchor=tk.W, padx=10, pady=(8, 4))

        self._log_notebook = ttk.Notebook(win)
        self._log_notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        log_font = ("Menlo", 11) if sys.platform == "darwin" else ("Consolas", 10)
        self._log_text_widgets = {}
        for tab_name in ("全部", "操作", "生成", "系统"):
            tab = ttk.Frame(self._log_notebook)
            self._log_notebook.add(tab, text=tab_name)
            text = tk.Text(tab, wrap=tk.WORD, font=log_font, state=tk.DISABLED)
            scroll = ttk.Scrollbar(tab, orient=tk.VERTICAL, command=text.yview)
            text.configure(yscrollcommand=scroll.set)
            text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            scroll.pack(side=tk.RIGHT, fill=tk.Y)
            self._log_text_widgets[tab_name] = text

        btn_row = ttk.Frame(win, padding=(8, 4))
        btn_row.pack(fill=tk.X, padx=4, pady=(0, 8))
        ttk.Button(btn_row, text="复制全部", command=self._copy_all_logs).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="复制当前页", command=self._copy_current_log_tab).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_row, text="刷新", command=self._refresh_log_window_text).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_row, text="清空", command=self._clear_logs).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_row, text="关闭", command=self._on_log_window_close).pack(side=tk.RIGHT)

        win.protocol("WM_DELETE_WINDOW", self._on_log_window_close)
        self._refresh_log_window_text()

    def _on_log_window_close(self) -> None:
        if self._log_window and self._log_window.winfo_exists():
            self._log_window.destroy()
        self._log_window = None
        self._log_text_widgets = {}
        self._log_notebook = None

    def _copy_all_logs(self) -> None:
        text = "\n".join(e["line"] for e in self._log_entries)
        if not text:
            messagebox.showinfo("提示", "暂无日志")
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self._log("已复制全部日志到剪贴板", kind="操作")

    def _copy_current_log_tab(self) -> None:
        if not self._log_notebook:
            self._copy_all_logs()
            return
        tab_idx = self._log_notebook.index(self._log_notebook.select())
        tabs = ("全部", "操作", "生成", "系统")
        tab = tabs[tab_idx] if tab_idx < len(tabs) else "全部"
        text = "\n".join(self._log_lines_for_filter(tab))
        if not text:
            messagebox.showinfo("提示", f"「{tab}」暂无日志")
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self._log(f"已复制「{tab}」日志到剪贴板", kind="操作")

    def _clear_logs(self) -> None:
        if not self._log_entries:
            return
        if not messagebox.askyesno("确认", "清空所有日志？"):
            return
        self._log_entries.clear()
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)
        self._refresh_log_window_text()
        self._log("日志已清空", kind="系统")

    def _log_startup_info(self) -> None:
        d = self.config_mgr.defaults
        self._log("ArtPipeline 工具已启动", kind="系统")
        self._log(f"配置文件: {self.config_mgr.path}", kind="系统")
        self._log(f"项目根: {self.config_mgr.project_root()}", kind="系统")
        self._log(f"ArtPipeline 根: {self.config_mgr.art_root()}", kind="系统")
        self._log(f"默认 checkpoint: {d.get('checkpoint', '')}", kind="系统")
        n_assets = len(self.config_mgr.assets())
        self._log(f"已加载 {len(self.config_mgr.categories())} 个分类、{n_assets} 个资源", kind="系统")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        if busy:
            self.pipeline.clear_cancel()
            self._prog_batch_idx = 1
            self._prog_batch_total = 1
            self._prog_step_pct = 0
            self._set_progress_text("准备中…")
        else:
            self._reset_progress_label()

    def _run_async(self, fn, *, on_done=None) -> None:
        if self._busy:
            messagebox.showwarning("忙碌", "请等待当前任务完成")
            return

        def worker() -> None:
            try:
                fn()
            finally:
                self._call_ui(lambda: self._set_busy(False))
                if on_done:
                    self._call_ui(on_done)

        self._set_busy(True)
        threading.Thread(target=worker, daemon=True).start()

    def _apply_comfyui_status(self, ok: bool, msg: str) -> None:
        if not self.winfo_exists():
            return
        prefix = "已连接" if ok else "未连接"
        self.status_var.set(f"ComfyUI · {prefix} · {msg}")
        try:
            self._comfy_dot.itemconfig(self._comfy_dot_id, fill=ACCENT_OK if ok else ACCENT_ERR)
        except (tk.TclError, AttributeError):
            pass
        status_key = f"{'ok' if ok else 'fail'}:{msg}"
        if status_key != self._last_comfyui_status:
            self._last_comfyui_status = status_key
            self._log(f"ComfyUI {'已连接' if ok else '未连接'} · {msg}", kind="系统")
            if not ok:
                self._log("生成任务将无法提交，请确认 ComfyUI 已启动", kind="系统")

    def _fetch_comfyui_status_async(self, *, refresh_checkpoints: bool = False) -> None:
        def task() -> None:
            ckpts: list[str] = []
            try:
                ok, msg = self.pipeline.test_comfyui()
                if ok and refresh_checkpoints:
                    ckpts = self.pipeline.list_checkpoints()
            except Exception as exc:
                ok, msg = False, str(exc)

            def apply() -> None:
                self._apply_comfyui_status(ok, msg)
                if refresh_checkpoints and ok:
                    self._apply_checkpoint_list(ckpts, quiet=True)

            self._call_ui(apply)

        threading.Thread(target=task, daemon=True).start()

    def _poll_comfyui_status(self) -> None:
        if self._closing or not self.winfo_exists():
            return
        self._fetch_comfyui_status_async(refresh_checkpoints=False)
        self._comfyui_poll_job = self.after(COMFYUI_POLL_MS, self._poll_comfyui_status)

    def _refresh_comfyui_status(self) -> None:
        """立即检测 ComfyUI 连接（不刷新 checkpoint 下拉，避免打断用户选择）。"""
        self._fetch_comfyui_status_async(refresh_checkpoints=False)

    def _stop_comfyui_poll(self) -> None:
        if self._comfyui_poll_job:
            self.after_cancel(self._comfyui_poll_job)
            self._comfyui_poll_job = None

    def _refresh_checkpoints(self, *, quiet: bool = False) -> None:
        def task() -> None:
            try:
                ckpts = self.pipeline.list_checkpoints()
                self._call_ui(lambda: self._apply_checkpoint_list(ckpts, quiet=quiet))
            except Exception as exc:
                if not quiet:
                    err_msg = str(exc)
                    self._call_ui(lambda e=err_msg: self._log(f"扫描 checkpoint 失败: {e}"))

        if not quiet:
            self._log("正在扫描 ComfyUI checkpoint…")
        threading.Thread(target=task, daemon=True).start()

    def _apply_checkpoint_list(self, ckpts: list[str], *, quiet: bool = False) -> None:
        prev = set(self._checkpoint_list)
        self._checkpoint_list = ckpts
        for combo in (self.global_ckpt_combo, self.category_ckpt_combo):
            combo["values"] = ckpts
        if not quiet and set(ckpts) != prev:
            self._log(f"已发现 {len(ckpts)} 个 checkpoint", kind="系统")
        if not ckpts:
            return
        default = self.config_mgr.defaults.get("checkpoint", "")
        if default and default in ckpts:
            self.global_ckpt_combo.set(default)
        elif default:
            self.global_ckpt_combo.set(default)
        else:
            self.global_ckpt_combo.set(ckpts[0])
        if self._selected_category:
            cat = self.config_mgr.category_by_id(self._selected_category)
            if cat and cat.checkpoint:
                self.category_ckpt_combo.set(cat.checkpoint)

    def _parse_seed_text(self, text: str, *, label: str) -> str | None:
        s = text.strip()
        if not s:
            return ""
        try:
            int(s)
            return s
        except ValueError:
            messagebox.showerror("Seed 无效", f"{label} 须为整数或留空")
            return None

    def _seed_input(self) -> str:
        entry = self.settings_fields.get("seed")
        if entry is not None:
            return entry.get().strip()
        return str(self.config_mgr.defaults.get("seed", "")).strip()

    def _parse_seed(self) -> int | None:
        s = self._seed_input()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            messagebox.showerror("Seed 无效", "全局设置中的 Seed 须为整数或留空")
            return None

    def _flush_ui(self) -> None:
        """先刷新待绘制区域，避免重活阻塞点击反馈。"""
        try:
            self.update_idletasks()
        except tk.TclError:
            pass

    def _defer_ui(self, fn: Callable[[], None], *, ms: int = 1) -> None:
        try:
            self.after(ms, fn)
        except tk.TclError:
            pass

    def _cancel_asset_list_job(self) -> None:
        job = self._asset_list_job
        self._asset_list_job = None
        if job is None:
            return
        try:
            self.after_cancel(job)
        except tk.TclError:
            pass

    def _clear_preview_placeholder(self, text: str = "点击资源查看预览") -> None:
        thumb_bg = "#2b2b2b" if HAS_TTB else "#e8e8e8"
        thumb_fg = "#888888" if HAS_TTB else "#666666"
        self._preview_images.pop("thumb", None)
        self._current_preview_asset = None
        try:
            self.preview_thumb.configure(image="", text=text, bg=thumb_bg, fg=thumb_fg)
        except tk.TclError:
            pass

    def _invalidate_asset_rows_cache(self) -> None:
        self._asset_rows_cache.clear()

    def _assets_rows_for_category(self, cat_id: str, query: str) -> list[tuple[str, str, str, str, str, str]]:
        key = (cat_id, query)
        cached = self._asset_rows_cache.get(key)
        if cached is not None:
            return cached
        rows: list[tuple[str, str, str, str, str, str]] = []
        for i, asset in enumerate(self.config_mgr.assets(category=cat_id)):
            if query and query not in asset.filename.lower() and query not in asset.id.lower():
                continue
            rows.append(
                (
                    asset.id,
                    asset.filename,
                    asset.size_label(),
                    "·",
                    "·",
                    "even" if i % 2 == 0 else "odd",
                )
            )
        self._asset_rows_cache[key] = rows
        return rows

    def _parse_asset_seed_field(self) -> str | None:
        return self._parse_seed_text(self.fields["Seed"].get(), label="资源 Seed")

    def _cat_id_from_list_index(self, idx: int) -> str | None:
        if idx < 0 or idx >= len(self._cat_list_ids):
            return None
        return self._cat_list_ids[idx]

    def _refresh_categories(self) -> None:
        self.cat_list.delete(0, tk.END)
        self._cat_list_ids = []
        cats = self.config_mgr.categories()
        for c in cats:
            ckpt_short = Path(c.checkpoint).name[:18] if c.checkpoint else "默认"
            self.cat_list.insert(tk.END, f"{c.label} ({c.id}) · {ckpt_short}")
            self._cat_list_ids.append(c.id)
        cat_ids = [c.id for c in cats]
        self.fields["分类"]["values"] = cat_ids
        if cats and not self._selected_category:
            self.cat_list.selection_set(0)
            self._selected_category = cats[0].id
            self._defer_ui(lambda: self._refresh_assets(), ms=1)
            self._defer_ui(lambda: self._load_category_paths(cats[0]), ms=50)

    def _schedule_refresh_assets(self) -> None:
        job = self._refresh_assets_job
        if job is not None:
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
        try:
            self._refresh_assets_job = self.after(120, self._run_scheduled_refresh_assets)
        except tk.TclError:
            pass

    def _run_scheduled_refresh_assets(self) -> None:
        self._refresh_assets_job = None
        if not self._closing:
            self._refresh_assets()

    def _refresh_assets(self, *, scan_files: bool = False) -> None:
        self._cancel_asset_list_job()
        self._asset_list_seq += 1
        seq = self._asset_list_seq
        cat_id = self._selected_category
        if not cat_id:
            return
        q = self.search_var.get().strip().lower()
        rows = self._assets_rows_for_category(cat_id, q)
        if scan_files:
            rows = [
                (aid, fn, sz, "…", "…", tag)
                for aid, fn, sz, _in, _u, tag in rows
            ]

        scroll = self._detail_scroll

        def fill_tree(batch_start: int = 0) -> None:
            if seq != self._asset_list_seq or self._closing or cat_id != self._selected_category:
                return
            batch_size = 25
            end = min(batch_start + batch_size, len(rows))
            if scroll is not None:
                scroll.suspend_refresh()
            self._asset_tree_refreshing = True
            try:
                if batch_start == 0:
                    self.asset_tree.delete(*self.asset_tree.get_children())
                for j in range(batch_start, end):
                    asset_id, filename, size_l, in_col, u_col, tag = rows[j]
                    self.asset_tree.insert(
                        "",
                        tk.END,
                        iid=asset_id,
                        tags=(tag,),
                        values=(filename, size_l, in_col, u_col),
                    )
            finally:
                self._asset_tree_refreshing = False
                if scroll is not None:
                    scroll.resume_refresh()
            if end < len(rows):
                self._asset_list_job = self.after(1, lambda e=end: fill_tree(e))
            else:
                self._list_status_var.set(f"{len(rows)} 个资源 · 点击加载预览")
                if scan_files and rows:
                    status_rows: list[tuple[str, Path, Path]] = []
                    for asset_id, *_rest in rows:
                        asset = self.config_mgr.asset_by_id(asset_id)
                        if asset:
                            _src, inbox, unity = self.config_mgr.resolve_paths(asset)
                            status_rows.append((asset_id, inbox, unity))
                    self._schedule_asset_status_refresh(status_rows)

        self._list_status_var.set("加载列表…")
        self._defer_ui(lambda: fill_tree(0), ms=1)

    def _schedule_asset_status_refresh(self, rows: list[tuple[str, Path, Path]]) -> None:
        self._asset_status_seq += 1
        seq = self._asset_status_seq
        cat_id = self._selected_category

        def worker() -> None:
            statuses: list[tuple[str, str, str]] = []
            for asset_id, inbox, unity in rows:
                in_ok = "✓" if inbox.is_file() else "—"
                u_ok = "✓" if unity.is_file() else "—"
                statuses.append((asset_id, in_ok, u_ok))
            self._call_ui(lambda: self._apply_asset_statuses(seq, cat_id, statuses))

        threading.Thread(target=worker, daemon=True, name="asset-status").start()

    def _apply_asset_statuses(
        self,
        seq: int,
        cat_id: str | None,
        statuses: list[tuple[str, str, str]],
    ) -> None:
        if seq != self._asset_status_seq or self._closing or cat_id != self._selected_category:
            return
        self._asset_tree_refreshing = True
        try:
            for asset_id, in_ok, u_ok in statuses:
                try:
                    if not self.asset_tree.exists(asset_id):
                        continue
                    vals = list(self.asset_tree.item(asset_id, "values"))
                    if len(vals) >= 4:
                        vals[2] = in_ok
                        vals[3] = u_ok
                        self.asset_tree.item(asset_id, values=vals)
                except tk.TclError:
                    pass
        finally:
            self._asset_tree_refreshing = False

    def _current_assets(self) -> list[Asset]:
        sel = self.asset_tree.selection()
        if sel:
            return [a for aid in sel if (a := self.config_mgr.asset_by_id(aid))]
        if self._selected_asset_id:
            a = self.config_mgr.asset_by_id(self._selected_asset_id)
            return [a] if a else []
        return []

    # ── 事件 ─────────────────────────────────────────────

    def _on_category_press(self, evt: tk.Event) -> None:
        """按下瞬间反馈（不等待 <<ListboxSelect>> 与后续重活）。"""
        try:
            idx = self.cat_list.nearest(evt.y)
            if idx < 0:
                return
            cat_id = self._cat_id_from_list_index(idx)
            if not cat_id:
                return
            self._list_status_var.set(f"→ {cat_id}")
        except (tk.TclError, IndexError):
            pass

    def _on_category_select(self, _evt=None) -> None:
        idx = self.cat_list.curselection()
        if not idx:
            return
        cat_id = self._cat_id_from_list_index(idx[0])
        if not cat_id:
            return
        if cat_id == self._selected_category:
            return
        self._selected_category = cat_id
        self._category_switch_seq += 1
        seq = self._category_switch_seq
        self._preview_load_seq += 1
        self._asset_load_seq += 1
        self._selected_asset_id = None
        self._asset_status_seq += 1
        self._list_status_var.set(f"→ {cat_id} · 切换中…")
        self.asset_meta_var.set("")

        def apply() -> None:
            if seq != self._category_switch_seq or self._closing:
                return
            self._clear_preview_placeholder("点击资源查看预览")
            cat = self.config_mgr.category_by_id(cat_id)
            if cat and self._is_basic_tab_active():
                self._defer_ui(lambda c=cat: self._load_category_paths(c), ms=80)
            elif cat:
                self._pending_category_paths = cat
            self._refresh_assets()

        self._defer_ui(apply, ms=1)

    def _on_asset_select(self, _evt=None) -> None:
        if self._asset_tree_refreshing:
            return
        sel = self.asset_tree.selection()
        if not sel:
            return
        asset_id = sel[0]
        asset = self.config_mgr.asset_by_id(asset_id)
        if not asset:
            return

        self._selected_asset_id = asset_id
        self._asset_load_seq += 1
        seq = self._asset_load_seq
        self._preview_load_seq += 1
        preview_seq = self._preview_load_seq

        ckpt = self.config_mgr.checkpoint_for_category(asset.category)
        ckpt_name = Path(ckpt).name if ckpt else "?"
        self.asset_meta_var.set(
            f"{asset.filename} · {asset.size_label()} · ckpt={ckpt_name} · 加载中…"
        )
        self._show_preview_loading()
        self._flush_ui()

        def load_form() -> None:
            if seq != self._asset_load_seq or self._closing:
                return
            self._current_preview_asset = asset
            self._load_asset_form_basic(asset)
            self.asset_meta_var.set(f"{asset.filename} · {asset.size_label()} · ckpt={ckpt_name}")
            if self._is_prompt_tab_active():
                self._defer_ui(lambda: self._load_asset_prompt_panel(asset), ms=1)

        def load_preview() -> None:
            if preview_seq != self._preview_load_seq or self._closing or self._selected_asset_id != asset_id:
                return
            self._schedule_preview_update(asset, preview_seq)

        self._defer_ui(load_form, ms=1)
        self._defer_ui(load_preview, ms=8)

    def _is_prompt_tab_active(self) -> bool:
        try:
            return self.notebook.index(self.notebook.select()) == 1
        except tk.TclError:
            return False

    def _is_basic_tab_active(self) -> bool:
        try:
            return self.notebook.index(self.notebook.select()) == 0
        except tk.TclError:
            return True

    def _on_notebook_tab_changed(self, _evt=None) -> None:
        if self._is_basic_tab_active() and self._pending_category_paths is not None:
            cat = self._pending_category_paths
            self._pending_category_paths = None
            self._defer_ui(lambda: self._load_category_paths(cat), ms=1)
        if not self._is_prompt_tab_active():
            return
        asset = self.config_mgr.asset_by_id(self._selected_asset_id) if self._selected_asset_id else None
        if asset:
            self._defer_ui(lambda: self._load_asset_prompt_panel(asset), ms=1)

    def _show_preview_loading(self) -> None:
        thumb_bg = "#2b2b2b" if HAS_TTB else "#e8e8e8"
        thumb_fg = "#888888" if HAS_TTB else "#666666"
        self._preview_images.pop("thumb", None)
        try:
            self.preview_thumb.configure(image="", text="加载中…", bg=thumb_bg, fg=thumb_fg)
        except tk.TclError:
            pass

    def _schedule_preview_update(self, asset: Asset, seq: int) -> None:
        params = self._preview_job_params(asset)
        if params is None:
            self._apply_preview_result(
                None,
                seq,
                asset.id,
                error=f"无 {self._preview_source_key.get()} 路径",
            )
            return

        def worker() -> None:
            try:
                rgba = self._build_preview_rgba_from_params(params)
            except Exception as exc:
                self._call_ui(lambda: self._apply_preview_result(None, seq, asset.id, error=str(exc)))
                return
            self._call_ui(lambda: self._apply_preview_result(rgba, seq, asset.id))

        threading.Thread(target=worker, daemon=True, name="preview-load").start()

    @staticmethod
    def _first_existing_path(*candidates: str) -> Path | None:
        for raw in candidates:
            if not raw:
                continue
            path = Path(raw)
            try:
                if path.is_file():
                    return path
            except OSError:
                continue
        return None

    def _preview_job_params(self, asset: Asset) -> dict[str, Any] | None:
        remove_bg = self._preview_remove_bg_for(asset)
        src, inbox, unity = self.config_mgr.resolve_paths(asset)
        path_candidates = [str(p) for p in (self._preview_path(asset), inbox, src, unity) if p is not None]
        if self._postprocess_preview_var.get() and HAS_POSTPROCESS:
            stack = self.config_mgr.get_postprocess_stack(asset.id)
            if stack and stack.layers:
                from postprocess.models import stack_to_dict

                preview_path = self._preview_path(asset)
                return {
                    "mode": "postprocess",
                    "stack_dict": stack_to_dict(stack),
                    "art_root": str(self.config_mgr.art_root()),
                    "src": str(src),
                    "inbox": str(inbox),
                    "unity": str(unity),
                    "preferred_subject": str(preview_path) if preview_path else "",
                    "path_candidates": path_candidates,
                    "remove_bg": remove_bg,
                }
        preview_path = self._preview_path(asset)
        if preview_path is None:
            return None
        return {
            "mode": "file",
            "path": str(preview_path),
            "path_candidates": path_candidates,
            "remove_bg": remove_bg,
        }

    def _preview_remove_bg_for(self, asset: Asset) -> bool:
        mode = asset.remove_bg_mode
        if self.fields.get("剔除背景模式") and self._selected_asset_id == asset.id:
            mode = self.fields["剔除背景模式"].get()
        if mode == REMOVE_BG_REMOVE:
            return True
        if mode == REMOVE_BG_KEEP:
            return False
        cat_id = asset.category
        if cat_id == self._selected_category and self.fields.get("分类剔除背景") is not None:
            return bool(self.fields["分类剔除背景"].get())
        return self.config_mgr.category_remove_bg_default(cat_id)

    @staticmethod
    def _build_preview_rgba_from_params(params: dict[str, Any]) -> Any:
        from PIL import Image

        if params.get("mode") == "postprocess":
            from postprocess.engine import AssetImageResolver, render_stack
            from postprocess.models import stack_from_dict

            stack = stack_from_dict(params["stack_dict"])
            if not stack:
                raise ValueError("后处理配方无效")
            src = Path(params["src"])
            inbox = Path(params["inbox"])
            unity = Path(params["unity"])
            preferred = params.get("preferred_subject", "")
            candidates = params.get("path_candidates") or []
            subject = ArtToolApp._first_existing_path(preferred, *candidates)
            resolver = AssetImageResolver(
                art_root=Path(params["art_root"]),
                asset_source=src if src.is_file() else None,
                asset_inbox=inbox if inbox.is_file() else None,
                asset_unity=unity if unity.is_file() else None,
                subject_path=subject,
            )
            im = render_stack(stack, resolver)
        else:
            path = ArtToolApp._first_existing_path(
                params.get("path", ""),
                *(params.get("path_candidates") or []),
            )
            if path is None:
                raise FileNotFoundError("图片文件不存在")
            with Image.open(path) as im_raw:
                im_raw.load()
                im = im_raw.convert("RGBA")
        max_edge = PREVIEW_SIZE * 2
        w, h = im.size
        if max(w, h) > max_edge:
            scale = max_edge / max(w, h)
            im = im.resize(
                (max(1, int(w * scale)), max(1, int(h * scale))),
                Image.Resampling.LANCZOS,
            )
        if params.get("remove_bg") and border_matte_to_alpha is not None:
            im = border_matte_to_alpha(im)
        return im

    def _apply_preview_result(
        self,
        rgba: Any | None,
        seq: int,
        asset_id: str,
        *,
        error: str = "",
    ) -> None:
        if seq != self._preview_load_seq or self._closing or self._selected_asset_id != asset_id:
            return
        thumb_bg = "#2b2b2b" if HAS_TTB else "#e8e8e8"
        thumb_fg = "#888888" if HAS_TTB else "#666666"
        if rgba is None:
            msg = (error or "无预览")[:40]
            self._preview_images.pop("thumb", None)
            try:
                self.preview_thumb.configure(image="", text=msg, bg=thumb_bg, fg=thumb_fg)
            except tk.TclError:
                pass
            return
        try:
            remove_bg = self._preview_remove_bg()
            use_grid = remove_bg or self._image_has_transparency(rgba)
            photo = self._make_square_thumbnail(rgba, checkerboard=use_grid)
            self._preview_images["thumb"] = photo
            self.preview_thumb.configure(image=photo, text="", bg=thumb_bg)
        except ImportError:
            self.preview_thumb.configure(image="", text="需要 Pillow", bg=thumb_bg, fg=thumb_fg)
        except Exception as exc:
            self.preview_thumb.configure(image="", text=str(exc)[:40], bg=thumb_bg, fg=thumb_fg)

    def _parse_wh(self) -> tuple[int, int] | None:
        try:
            w = int(self.fields["宽度"].get().strip() or 512)
            h = int(self.fields["高度"].get().strip() or 512)
            if w < 32 or h < 32 or w > 4096 or h > 4096:
                raise ValueError("out of range")
            return w, h
        except ValueError:
            messagebox.showerror("尺寸无效", "宽度和高度须为 32–4096 的整数")
            return None

    def _apply_form_to_asset(self, asset: Asset) -> None:
        wh = self._parse_wh()
        if wh is None:
            raise ValueError("invalid size")
        asset.filename = self.fields["文件名"].get().strip()
        asset.category = self.fields["分类"].get()
        asset.width, asset.height = wh
        seed_s = self._parse_asset_seed_field()
        if seed_s is None:
            raise ValueError("invalid seed")
        asset.seed = seed_s
        asset.subject = self.fields["说明"].get().strip()
        asset.enabled = self.fields["启用"].get()
        self._apply_prompt_fields_to_asset(asset)

    def _apply_prompt_fields_to_asset(self, asset: Asset) -> None:
        raw_pos = self.positive_text.get("1.0", tk.END).strip()
        asset.negative = self.negative_text.get("1.0", tk.END).strip()
        if asset.category in ("items", "skills"):
            g, l = self._split_item_prompt_text(raw_pos)
            if g:
                asset.positive_g = g
            if l:
                asset.positive_l = l
            asset.positive = f"{asset.positive_g} {asset.positive_l}".strip()
        else:
            asset.positive = raw_pos

    @staticmethod
    def _split_item_prompt_text(text: str) -> tuple[str, str]:
        marker_g = "=== SDXL-G 边框构图 ==="
        marker_l = "=== SDXL-L 物件主体 ==="
        if marker_g in text and marker_l in text:
            _, rest = text.split(marker_g, 1)
            g_part, l_part = rest.split(marker_l, 1)
            return g_part.strip(), l_part.strip()
        return "", text.strip()

    def _sync_assets_before_run(self, assets: list[Asset]) -> list[Asset] | None:
        """生成/导出前同步当前表单与提示词到配置。"""
        synced: list[Asset] = []
        for a in assets:
            if a.id == self._selected_asset_id:
                try:
                    self._apply_form_to_asset(a)
                except ValueError:
                    return None
                self.config_mgr.update_asset(a)
                refreshed = self.config_mgr.asset_by_id(a.id)
                synced.append(refreshed or a)
            else:
                synced.append(a)
        return synced

    def _validate_seeds_for_batch(self, assets: list[Asset]) -> bool:
        if self._seed_input() and self._parse_seed() is None:
            return False
        for a in assets:
            s = (a.seed or "").strip()
            if not s:
                continue
            try:
                int(s)
            except ValueError:
                messagebox.showerror("Seed 无效", f"{a.filename} 的 Seed 须为整数或留空")
                return False
        return True

    def _on_asset_generated(self, asset_id: str) -> None:
        asset = self.config_mgr.asset_by_id(asset_id)
        if not asset:
            return
        self._preview_source_key.set("inbox")
        self._current_preview_asset = asset
        self._selected_asset_id = asset_id
        self._update_preview(asset)
        self.update_idletasks()

    def _open_postprocess_editor(self) -> None:
        if not HAS_POSTPROCESS or open_postprocess_editor is None:
            messagebox.showwarning("后处理", "未找到 postprocess 模块")
            return
        asset = self._current_preview_asset
        if self._selected_asset_id:
            selected = self.config_mgr.asset_by_id(self._selected_asset_id)
            if selected:
                asset = selected
        if not asset and self._selected_asset_id:
            asset = self.config_mgr.asset_by_id(self._selected_asset_id)
        if not asset:
            messagebox.showinfo("后处理", "请先选中一张资源")
            return
        subject_path = self._preview_path(asset)
        if subject_path is None or not subject_path.is_file():
            src, inbox, unity = self.config_mgr.resolve_paths(asset)
            subject_path = next((p for p in (inbox, src, unity) if p.is_file()), None)
            preview_key = "auto"
        else:
            preview_key = self._preview_source_key.get()
        if subject_path is None:
            messagebox.showinfo("后处理", f"未找到 {asset.filename} 的图片文件（source / inbox / Unity）")
            return

        def open_editor() -> None:
            open_postprocess_editor(
                self,
                self.config_mgr,
                asset,
                subject_path=subject_path,
                subject_label=preview_key,
                on_applied=lambda: self._on_postprocess_applied(asset.id),
            )

        self._defer_ui(open_editor, ms=1)

    def _on_postprocess_applied(self, asset_id: str) -> None:
        self._postprocess_preview_var.set(True)
        self._preview_source_key.set("inbox")
        asset = self.config_mgr.asset_by_id(asset_id)
        if asset:
            self._current_preview_asset = asset
            self._update_preview(asset)

    def _refresh_preview(self) -> None:
        if self._current_preview_asset:
            asset = self._current_preview_asset
            self._preview_load_seq += 1
            seq = self._preview_load_seq
            self._show_preview_loading()
            self._schedule_preview_update(asset, seq)

    def _update_preview(self, asset: Asset) -> None:
        """刷新预览（异步）。"""
        self._preview_load_seq += 1
        seq = self._preview_load_seq
        self._show_preview_loading()
        self._schedule_preview_update(asset, seq)

    def _preview_path(self, asset: Asset | None = None) -> Path | None:
        asset = asset or self._current_preview_asset
        if not asset:
            return None
        key = self._preview_source_key.get()
        src, inbox, unity = self.config_mgr.resolve_paths(asset)
        return {"source": src, "inbox": inbox, "unity": unity}.get(key, inbox)

    @staticmethod
    def _image_has_transparency(im: Any) -> bool:
        if im.mode != "RGBA":
            return False
        lo, _hi = im.getchannel("A").getextrema()
        return lo < 255

    def _checkerboard_rgba(self, width: int, height: int, *, cell: int = 10) -> Any:
        from PIL import Image

        light = (72, 72, 72, 255) if HAS_TTB else (210, 210, 210, 255)
        dark = (48, 48, 48, 255) if HAS_TTB else (170, 170, 170, 255)
        bg = Image.new("RGBA", (width, height), light)
        px = bg.load()
        for y in range(height):
            for x in range(width):
                if ((x // cell) + (y // cell)) % 2:
                    px[x, y] = dark
        return bg

    def _make_square_thumbnail(self, im: Any, *, checkerboard: bool = False) -> Any:
        from PIL import Image, ImageTk

        im.thumbnail((PREVIEW_SIZE, PREVIEW_SIZE), Image.Resampling.LANCZOS)
        use_grid = checkerboard or self._image_has_transparency(im)
        if use_grid:
            square = self._checkerboard_rgba(PREVIEW_SIZE, PREVIEW_SIZE)
        else:
            bg = (43, 43, 43, 255) if HAS_TTB else (232, 232, 232, 255)
            square = Image.new("RGBA", (PREVIEW_SIZE, PREVIEW_SIZE), bg)
        x = (PREVIEW_SIZE - im.width) // 2
        y = (PREVIEW_SIZE - im.height) // 2
        square.paste(im, (x, y), im)
        return ImageTk.PhotoImage(square)

    def _preview_remove_bg(self) -> bool:
        asset = self._current_preview_asset
        if not asset:
            return False
        mode = asset.remove_bg_mode
        if self.fields.get("剔除背景模式") and self._selected_asset_id == asset.id:
            mode = self.fields["剔除背景模式"].get()
        if mode == REMOVE_BG_REMOVE:
            return True
        if mode == REMOVE_BG_KEEP:
            return False
        cat_id = asset.category
        if cat_id == self._selected_category and self.fields.get("分类剔除背景") is not None:
            return bool(self.fields["分类剔除背景"].get())
        return self.config_mgr.category_remove_bg_default(cat_id)

    def _update_remove_bg_hint(self, *, asset: Asset | None = None, cat: Category | None = None) -> None:
        cat = cat or (self.config_mgr.category_by_id(self._selected_category) if self._selected_category else None)
        if cat is None and asset:
            cat = self.config_mgr.category_by_id(asset.category)
        if cat is None:
            self._remove_bg_hint_var.set("")
            return
        if cat.id == self._selected_category and self.fields.get("分类剔除背景") is not None:
            default_on = bool(self.fields["分类剔除背景"].get())
        else:
            default_on = self.config_mgr.category_remove_bg_default(cat.id)
        default_text = "剔除" if default_on else "不剔除"
        if asset and self.fields.get("剔除背景模式"):
            mode = self.fields["剔除背景模式"].get()
            if mode == REMOVE_BG_INHERIT:
                self._remove_bg_hint_var.set(f"当前分类默认：{default_text}纯色背景")
            elif mode == REMOVE_BG_REMOVE:
                self._remove_bg_hint_var.set("已覆盖：强制剔除纯色背景")
            else:
                self._remove_bg_hint_var.set("已覆盖：保留纯色背景")
        else:
            self._remove_bg_hint_var.set(f"本分类默认：{default_text}纯色背景")

    def _load_preview_rgba(self, path: Path) -> Any:
        from PIL import Image

        with Image.open(path) as im:
            im.load()
            rgba = im.convert("RGBA")
        max_edge = PREVIEW_SIZE * 2
        w, h = rgba.size
        if max(w, h) > max_edge:
            scale = max_edge / max(w, h)
            rgba = rgba.resize(
                (max(1, int(w * scale)), max(1, int(h * scale))),
                Image.Resampling.LANCZOS,
            )
        if self._preview_remove_bg() and border_matte_to_alpha is not None:
            rgba = border_matte_to_alpha(rgba)
        return rgba

    def _on_remove_bg_mode_change(self) -> None:
        if not self._selected_asset_id:
            self.fields["剔除背景模式"].set(REMOVE_BG_INHERIT)
            return
        asset = self.config_mgr.asset_by_id(self._selected_asset_id)
        if not asset:
            self.fields["剔除背景模式"].set(REMOVE_BG_INHERIT)
            return
        mode = self.fields["剔除背景模式"].get()
        asset.remove_bg_mode = mode
        self.config_mgr.update_asset(asset)
        self._update_remove_bg_hint(asset=asset)

        if mode != REMOVE_BG_REMOVE:
            self._refresh_preview()
            return
        if border_matte_to_alpha is None:
            self.fields["剔除背景模式"].set(REMOVE_BG_KEEP)
            asset.remove_bg_mode = REMOVE_BG_KEEP
            self.config_mgr.update_asset(asset)
            messagebox.showerror("错误", "需要安装 numpy：pip install numpy")
            return
        path = self._preview_path()
        if path is None or not path.is_file():
            messagebox.showinfo("提示", "当前预览文件不存在")
            self._refresh_preview()
            return
        self._refresh_preview()
        self._offer_matte_replace(path)

    def _on_category_remove_bg_change(self) -> None:
        self._update_remove_bg_hint()
        asset = self.config_mgr.asset_by_id(self._selected_asset_id) if self._selected_asset_id else None
        if asset and self.fields["剔除背景模式"].get() == REMOVE_BG_INHERIT:
            self._refresh_preview()

    def _offer_matte_replace(self, path: Path) -> None:
        try:
            rgba = self._load_preview_rgba(path)
            buf = io.BytesIO()
            rgba.save(buf, format="PNG", optimize=True)
            try:
                rel = self.config_mgr.rel_to_project(path)
            except ValueError:
                rel = path
            if messagebox.askyesno(
                "剔除背景",
                f"已剔除外侧与边框相近的纯色底，主体颜色保留。\n\n是否用抠底结果替换原图？\n{rel}",
            ):
                path.write_bytes(buf.getvalue())
                self._log(f"已替换抠底图: {rel}")
                self._refresh_assets()
        except Exception as exc:
            messagebox.showerror("剔除背景失败", str(exc))
            self.fields["剔除背景模式"].set(REMOVE_BG_KEEP)
            if self._selected_asset_id:
                asset = self.config_mgr.asset_by_id(self._selected_asset_id)
                if asset:
                    asset.remove_bg_mode = REMOVE_BG_KEEP
                    self.config_mgr.update_asset(asset)
        self._refresh_preview()

    def _open_preview_file(self) -> None:
        if not self._current_preview_asset:
            return
        path = self._preview_path()
        if path and path.is_file():
            subprocess.run(["open", str(path)], check=False)
        else:
            messagebox.showinfo("提示", "文件不存在")

    def _copy_text(self, widget: tk.Text) -> None:
        text = widget.get("1.0", tk.END).strip()
        self.clipboard_clear()
        self.clipboard_append(text)
        self._log("已复制到剪贴板")

    def _duplicate_asset(self, asset: Asset | None = None) -> None:
        if asset is None:
            asset = self.config_mgr.asset_by_id(self._selected_asset_id) if self._selected_asset_id else None
        if not asset:
            return
        new_name = simpledialog.askstring("复制资源", "新文件名:", initialvalue=f"{asset.id}_copy.png")
        if not new_name:
            return
        if not new_name.endswith(".png"):
            new_name += ".png"
        try:
            new_asset = self.config_mgr.add_asset(
                filename=new_name,
                category=asset.category,
                width=asset.width,
                height=asset.height,
                subject=asset.subject + " (copy)",
                positive=asset.positive,
                negative=asset.negative,
                workflow=asset.workflow,
                seed=asset.seed,
            )
            wf_src = self.config_mgr.workflow_file_for_asset(asset)
            if wf_src.is_file():
                import shutil
                wf_dst = self.config_mgr.workflow_file_for_asset(new_asset)
                wf_dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(wf_src, wf_dst)
            self._refresh_assets()
            self.asset_tree.selection_set(new_asset.id)
            self._on_asset_select()
            self._log(f"已复制资源 → {new_asset.id}")
        except ValueError as exc:
            messagebox.showerror("错误", str(exc))

    def _cancel_generation(self) -> None:
        if not self._busy:
            return
        self.pipeline.request_cancel()
        self.progress_var.set("取消中…")
        self._set_progress_text("取消中…")
        self._log("已请求取消（ComfyUI interrupt）")

    def _load_category_paths(self, cat: Category) -> None:
        if self._category_paths_loaded_for == cat.id:
            return
        self._category_paths_loaded_for = cat.id
        self.fields["path_source"].delete(0, tk.END)
        self.fields["path_source"].insert(0, cat.source)
        self.fields["path_inbox"].delete(0, tk.END)
        self.fields["path_inbox"].insert(0, cat.inbox)
        self.fields["path_unity"].delete(0, tk.END)
        self.fields["path_unity"].insert(0, cat.unity)
        self.category_ckpt_combo.set(cat.checkpoint or "")
        self.fields["分类剔除背景"].set(cat.alpha_matte.strip().lower() != "none")
        self._update_remove_bg_hint(cat=cat)

        def load_prompts() -> None:
            if self._category_paths_loaded_for != cat.id:
                return
            self.category_positive_common_text.delete("1.0", tk.END)
            self.category_positive_common_text.insert("1.0", cat.positive_common or "")
            self.category_negative_common_text.delete("1.0", tk.END)
            self.category_negative_common_text.insert("1.0", cat.negative_common or "")

        self._defer_ui(load_prompts, ms=120)

    def _load_asset_form_basic(self, asset: Asset) -> None:
        self.fields["ID"].delete(0, tk.END)
        self.fields["ID"].insert(0, asset.id)
        self.fields["文件名"].delete(0, tk.END)
        self.fields["文件名"].insert(0, asset.filename)
        self.fields["分类"].set(asset.category)
        self.fields["宽度"].delete(0, tk.END)
        self.fields["宽度"].insert(0, str(asset.width))
        self.fields["高度"].delete(0, tk.END)
        self.fields["高度"].insert(0, str(asset.height))
        self.fields["Seed"].delete(0, tk.END)
        self.fields["Seed"].insert(0, asset.seed or "")
        self.fields["说明"].delete(0, tk.END)
        self.fields["说明"].insert(0, asset.subject)
        self.fields["启用"].set(asset.enabled)
        self.fields["剔除背景模式"].set(asset.remove_bg_mode)
        self._update_remove_bg_hint(asset=asset)

    def _load_asset_prompt_panel(self, asset: Asset) -> None:
        if self._prompt_panel_asset_id == asset.id:
            return
        self._prompt_panel_asset_id = asset.id
        self.positive_text.delete("1.0", tk.END)
        if asset.category in ("items", "skills") and asset.positive_g and asset.positive_l:
            self.positive_text.insert(
                "1.0",
                f"=== SDXL-G 边框构图 ===\n{asset.positive_g}\n\n"
                f"=== SDXL-L 物件主体 ===\n{asset.positive_l}",
            )
        else:
            self.positive_text.insert("1.0", asset.positive)
        self.negative_text.delete("1.0", tk.END)
        self.negative_text.insert("1.0", asset.negative)
        self._ensure_workflow_loaded(asset)
        self._render_ai_history(asset.id)

    def _ensure_workflow_loaded(self, asset: Asset) -> None:
        if self._workflow_loaded_for == asset.id:
            return
        self._workflow_loaded_for = asset.id
        self.workflow_text.delete("1.0", tk.END)
        wf_path = self.config_mgr.workflow_file_for_asset(asset)
        if wf_path.is_file():
            self.workflow_text.insert("1.0", wf_path.read_text(encoding="utf-8"))

    def _load_asset_into_form(self, asset: Asset) -> None:
        self._load_asset_form_basic(asset)
        self._load_asset_prompt_panel(asset)

    def _load_ai_history_for_asset(self, asset_id: str) -> None:
        self._render_ai_history(asset_id)

    def _render_ai_history(self, asset_id: str | None = None) -> None:
        aid = asset_id or self._selected_asset_id
        self.ai_history_text.configure(state=tk.NORMAL)
        self.ai_history_text.delete("1.0", tk.END)
        if not aid:
            self.ai_history_text.insert(tk.END, "（请先选择资源）\n", "system")
        else:
            history = self._ai_histories.get(aid, [])
            if not history:
                self.ai_history_text.insert(
                    tk.END,
                    "（暂无对话。描述想要的图标，例如：「做一个手持骰子的赌徒角色」）\n",
                    "system",
                )
            else:
                for item in history:
                    role = item.get("role", "assistant")
                    tag = "user" if role == "user" else "assistant"
                    prefix = "你: " if role == "user" else "AI: "
                    self.ai_history_text.insert(tk.END, prefix, tag)
                    self.ai_history_text.insert(tk.END, item.get("content", "") + "\n\n", tag)
        self.ai_history_text.configure(state=tk.DISABLED)
        self.ai_history_text.see(tk.END)

    def _clear_ai_chat(self) -> None:
        if not self._selected_asset_id:
            return
        if self._ai_histories.get(self._selected_asset_id) and not messagebox.askyesno(
            "清空对话", "确定清空当前资源的 AI 对话历史？"
        ):
            return
        self._ai_histories.pop(self._selected_asset_id, None)
        self._render_ai_history()
        self._log("已清空 AI 对话", kind="系统")

    def _set_ai_busy(self, busy: bool) -> None:
        self._ai_busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        try:
            self.ai_send_btn.configure(state=state)
        except tk.TclError:
            pass

    def _build_ai_messages(self, asset: Asset, user_text: str) -> list[dict[str, str]]:
        cat = self.config_mgr.category_by_id(asset.category)
        cat_label = cat.label if cat else asset.category
        wf_text = self.workflow_text.get("1.0", tk.END).strip()
        wf_summary = "已自定义工作流" if wf_text else "使用默认 SDXL 模板"
        context = build_context_message(
            asset_id=asset.id,
            filename=asset.filename,
            category=asset.category,
            category_label=cat_label,
            width=asset.width,
            height=asset.height,
            subject=asset.subject,
            positive=self.positive_text.get("1.0", tk.END).strip(),
            negative=self.negative_text.get("1.0", tk.END).strip(),
            workflow_summary=wf_summary,
            positive_g=asset.positive_g,
            positive_l=asset.positive_l,
        )
        messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        history = self._ai_histories.get(asset.id, [])
        if not history:
            messages.append({"role": "user", "content": f"{context}\n\n用户需求:\n{user_text}"})
        else:
            for item in history:
                messages.append({"role": item["role"], "content": item["content"]})
            messages.append({"role": "user", "content": user_text})
        return messages

    def _apply_ai_updates(self, updates: dict[str, Any]) -> list[str]:
        changed: list[str] = []
        asset = self.config_mgr.asset_by_id(self._selected_asset_id) if self._selected_asset_id else None

        for key, label in (
            ("positive_g", "边框(G)"),
            ("positive_l", "物件(L)"),
        ):
            val = updates.get(key)
            if val and str(val).lower() not in ("null", "none", ""):
                if asset:
                    setattr(asset, key, str(val).strip())
                changed.append(label)

        positive = updates.get("positive")
        if positive and str(positive).lower() not in ("null", "none", ""):
            self.positive_text.delete("1.0", tk.END)
            if asset and asset.category in ("items", "skills") and asset.positive_g and asset.positive_l:
                self.positive_text.insert(
                    "1.0",
                    f"=== SDXL-G 边框构图 ===\n{asset.positive_g}\n\n"
                    f"=== SDXL-L 物件主体 ===\n{asset.positive_l}",
                )
            else:
                self.positive_text.insert("1.0", str(positive).strip())
            if asset:
                asset.positive = str(positive).strip()
            changed.append("提示词(正向)")
        elif asset and asset.category in ("items", "skills") and asset.positive_g and asset.positive_l:
            self.positive_text.delete("1.0", tk.END)
            self.positive_text.insert(
                "1.0",
                f"=== SDXL-G 边框构图 ===\n{asset.positive_g}\n\n"
                f"=== SDXL-L 物件主体 ===\n{asset.positive_l}",
            )
            asset.positive = f"{asset.positive_g} {asset.positive_l}"
            if changed:
                changed.append("提示词(正向)")

        negative = updates.get("negative")
        if negative and str(negative).lower() not in ("null", "none", ""):
            self.negative_text.delete("1.0", tk.END)
            self.negative_text.insert("1.0", str(negative).strip())
            changed.append("提示词(负向)")

        subject = updates.get("subject")
        if subject and str(subject).lower() not in ("null", "none", ""):
            self.fields["说明"].delete(0, tk.END)
            self.fields["说明"].insert(0, str(subject).strip())
            changed.append("说明")

        workflow = updates.get("workflow")
        if workflow and workflow is not None and str(workflow).lower() not in ("null", "none", ""):
            if isinstance(workflow, str):
                wf_text = workflow.strip()
            else:
                wf_text = json.dumps(workflow, indent=2, ensure_ascii=False)
            _, err = validate_workflow_json(wf_text)
            if err:
                raise ValueError(f"AI 返回的工作流无效: {err}")
            self.workflow_text.delete("1.0", tk.END)
            self.workflow_text.insert("1.0", wf_text)
            changed.append("工作流")

        if "提示词(正向)" in changed or "提示词(负向)" in changed:
            self._save_prompts()
        if "工作流" in changed:
            self._save_workflow()
        if "说明" in changed:
            self._save_basic()
        return changed

    def _send_ai_message(self) -> None:
        if self._ai_busy:
            return
        if not self._selected_asset_id:
            messagebox.showinfo("提示", "请先选择一个资源")
            return
        asset = self.config_mgr.asset_by_id(self._selected_asset_id)
        if not asset:
            return
        user_text = self.ai_input_text.get("1.0", tk.END).strip()
        if not user_text:
            return
        key_entry = self.settings_fields.get("deepseek_api_key")
        api_key = key_entry.get().strip() if key_entry else ""
        if not api_key:
            api_key = str(self.config_mgr.defaults.get("deepseek_api_key", "")).strip()
        if not api_key:
            messagebox.showwarning("缺少 API Key", "请先在「全局设置」中填写 DeepSeek API Key 并保存")
            return
        model_entry = self.settings_fields.get("deepseek_model")
        raw_model = model_entry.get().strip() if model_entry else ""
        if not raw_model:
            raw_model = str(self.config_mgr.defaults.get("deepseek_model", "")).strip()
        model = resolve_model(raw_model)

        self.ai_input_text.delete("1.0", tk.END)
        self._set_ai_busy(True)
        self._log(f"AI 请求: {user_text[:60]}{'…' if len(user_text) > 60 else ''}", kind="系统")

        def worker() -> None:
            try:
                messages = self._build_ai_messages(asset, user_text)
                reply_raw = ai_chat(messages, api_key=api_key, model=model)
                message, updates = parse_ai_response(reply_raw)
                self._call_ui(
                    lambda aid=asset.id, ut=user_text, msg=message, upd=updates: self._on_ai_response(
                        aid, ut, msg, upd
                    )
                )
            except (AiAssistantError, ValueError) as exc:
                err_msg = str(exc)
                self._call_ui(lambda msg=err_msg: self._on_ai_error(msg))
            finally:
                self._call_ui(lambda: self._set_ai_busy(False))

        threading.Thread(target=worker, daemon=True).start()

    def _on_ai_response(
        self, asset_id: str, user_text: str, message: str, updates: dict[str, Any]
    ) -> None:
        history = self._ai_histories.setdefault(asset_id, [])
        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": message})
        self._render_ai_history(asset_id)
        try:
            changed = self._apply_ai_updates(updates)
            if changed:
                self._log(f"AI 已更新: {', '.join(changed)}", kind="系统")
            self._log(f"AI: {message}", kind="系统")
        except ValueError as exc:
            messagebox.showerror("AI 更新失败", str(exc))
            self._log(f"AI 更新失败: {exc}", kind="系统")

    def _on_ai_error(self, err: str) -> None:
        messagebox.showerror("AI 错误", err)
        self._log(f"AI 错误: {err}", kind="系统")

    def _autodetect_root_paths(self) -> None:
        from paths import ART_ROOT, PROJECT_ROOT

        for key, val in (
            ("project_root", str(PROJECT_ROOT.resolve())),
            ("art_pipeline_root", str(ART_ROOT.resolve())),
        ):
            entry = self.settings_fields.get(key)
            if entry is None:
                continue
            entry.delete(0, tk.END)
            entry.insert(0, val)
        self._log("已填入当前脚本推断的路径（保存后生效）", kind="系统")

    def _browse_project_root(self) -> None:
        from tkinter import filedialog

        picked = filedialog.askdirectory(title="选择 Unity 项目根目录")
        if not picked:
            return
        entry = self.settings_fields.get("project_root")
        if entry is not None:
            entry.delete(0, tk.END)
            entry.insert(0, picked)

    def _browse_art_root(self) -> None:
        from tkinter import filedialog

        picked = filedialog.askdirectory(title="选择 ArtPipeline 根目录")
        if not picked:
            return
        entry = self.settings_fields.get("art_pipeline_root")
        if entry is not None:
            entry.delete(0, tk.END)
            entry.insert(0, picked)

    def _load_settings_fields(self) -> None:
        d = self.config_mgr.defaults
        for key, entry in self.settings_fields.items():
            val = d.get(key, "")
            if key == "deepseek_model":
                val = resolve_model(str(val))
            if isinstance(entry, ttk.Combobox):
                entry.set(str(val))
            else:
                entry.delete(0, tk.END)
                entry.insert(0, str(val))
        self.global_ckpt_combo.set(str(d.get("checkpoint", "")))

    # ── 保存操作 ─────────────────────────────────────────────

    def _save_config(self) -> None:
        self.config_mgr.save()
        self._log("配置已保存")

    def _reload_config(self) -> None:
        self.config_mgr.load()
        self._invalidate_asset_rows_cache()
        self._category_paths_loaded_for = None
        self._refresh_categories()
        self._load_settings_fields()
        if self._selected_category:
            cat = self.config_mgr.category_by_id(self._selected_category)
            if cat:
                self._load_category_paths(cat)
        self._refresh_checkpoints()
        self._log("配置已重新加载")

    def _save_basic(self) -> None:
        if not self._selected_asset_id:
            return
        asset = self.config_mgr.asset_by_id(self._selected_asset_id)
        if not asset:
            return
        wh = self._parse_wh()
        if wh is None:
            return
        asset.filename = self.fields["文件名"].get().strip()
        asset.category = self.fields["分类"].get()
        asset.width, asset.height = wh
        seed_s = self._parse_asset_seed_field()
        if seed_s is None:
            return
        asset.seed = seed_s
        asset.subject = self.fields["说明"].get().strip()
        asset.enabled = self.fields["启用"].get()
        asset.remove_bg_mode = self.fields["剔除背景模式"].get()
        self.config_mgr.update_asset(asset)
        self._invalidate_asset_rows_cache()
        self._refresh_assets()
        self._log(f"已保存基本信息: {asset.id}")

    def _save_prompts(self) -> None:
        if not self._selected_asset_id:
            return
        asset = self.config_mgr.asset_by_id(self._selected_asset_id)
        if not asset:
            return
        self._apply_prompt_fields_to_asset(asset)
        self.config_mgr.update_asset(asset)
        self._log(f"已保存提示词: {asset.id}")

    def _save_category_paths(self) -> None:
        if not self._selected_category:
            return
        cat = self.config_mgr.category_by_id(self._selected_category)
        if not cat:
            return
        cat.source = self.config_mgr.normalize_category_path(
            self.fields["path_source"].get().strip(), under="art"
        )
        cat.inbox = self.config_mgr.normalize_category_path(
            self.fields["path_inbox"].get().strip(), under="art"
        )
        cat.unity = self.config_mgr.normalize_category_path(
            self.fields["path_unity"].get().strip(), under="project"
        )
        cat.checkpoint = self.category_ckpt_combo.get().strip()
        cat.positive_common = self.category_positive_common_text.get("1.0", tk.END).strip()
        cat.negative_common = self.category_negative_common_text.get("1.0", tk.END).strip()
        cat.alpha_matte = "border" if self.fields["分类剔除背景"].get() else "none"
        self.config_mgr.update_category(cat)
        self.config_mgr.ensure_category_dirs(cat)
        self._category_paths_loaded_for = None
        self._invalidate_asset_rows_cache()
        self._refresh_categories()
        self._update_remove_bg_hint(cat=cat)
        asset = self.config_mgr.asset_by_id(self._selected_asset_id) if self._selected_asset_id else None
        if asset and asset.remove_bg_mode == REMOVE_BG_INHERIT:
            self._refresh_preview()
        self._log(f"已保存分类设置: {cat.id} · ckpt={cat.checkpoint or '默认'}")

    def _maybe_save_category_paths(self) -> None:
        """分类路径表单已加载且用户可能未点保存时，批量操作前先写入配置。"""
        if not self._selected_category:
            return
        if self._category_paths_loaded_for != self._selected_category:
            return
        self._save_category_paths()

    def _save_settings(self) -> None:
        d = self.config_mgr.defaults
        project_raw = self.settings_fields["project_root"].get().strip()
        art_raw = self.settings_fields["art_pipeline_root"].get().strip()
        if not project_raw:
            messagebox.showerror("路径错误", "请填写 Unity 项目根目录（绝对路径）")
            return
        project_path = Path(project_raw).expanduser()
        if not project_path.is_dir():
            messagebox.showerror("路径错误", f"Unity 项目根目录不存在:\n{project_path}")
            return
        if art_raw:
            art_path = Path(art_raw).expanduser()
            if not art_path.is_dir():
                messagebox.showerror("路径错误", f"ArtPipeline 根目录不存在:\n{art_path}")
                return
        for key, entry in self.settings_fields.items():
            val = entry.get().strip()
            if key in ("steps",):
                d[key] = int(val)
            elif key in ("cfg",):
                d[key] = float(val)
            elif key == "deepseek_model":
                d[key] = resolve_model(val)
            else:
                d[key] = val
        d["checkpoint"] = self.global_ckpt_combo.get().strip()
        self.config_mgr.save()
        self._refresh_comfyui_status()
        self._log(f"全局设置已保存 · 项目根 {self.config_mgr.project_root()}", kind="系统")

    def _save_workflow(self) -> None:
        if not self._selected_asset_id:
            return
        asset = self.config_mgr.asset_by_id(self._selected_asset_id)
        if not asset:
            return
        text = self.workflow_text.get("1.0", tk.END).strip()
        data, err = validate_workflow_json(text)
        if err:
            messagebox.showerror("JSON 错误", err)
            return
        assert data is not None
        clean = {k: v for k, v in data.items() if not str(k).startswith("_")}
        self.pipeline.save_asset_workflow(asset, clean)
        self._log(f"工作流已保存: workflows/assets/{asset.id}.json")

    def _validate_workflow(self) -> None:
        text = self.workflow_text.get("1.0", tk.END).strip()
        _, err = validate_workflow_json(text)
        if err:
            messagebox.showerror("校验失败", err)
        else:
            messagebox.showinfo("校验通过", "工作流 JSON 格式有效")

    def _load_default_workflow(self) -> None:
        from paths import WORKFLOWS_DIR

        rel = "workflows/_default_sdxl_api.json"
        if self._selected_asset_id:
            asset = self.config_mgr.asset_by_id(self._selected_asset_id)
            if asset:
                cat = self.config_mgr.category_by_id(asset.category)
                if cat and cat.default_workflow:
                    rel = cat.default_workflow
        p = WORKFLOWS_DIR / Path(rel).name
        if not p.is_file():
            p = WORKFLOWS_DIR / "_default_sdxl_api.json"
        if p.is_file():
            self.workflow_text.delete("1.0", tk.END)
            self.workflow_text.insert("1.0", p.read_text(encoding="utf-8"))

    # ── 新建 / 删除 ─────────────────────────────────────────────

    def _new_category(self) -> None:
        label = simpledialog.askstring("新建分类", "分类名称（如 特效 / fx）:")
        if not label:
            return
        cat_id = simpledialog.askstring("新建分类", "分类 ID（英文，留空自动生成）:") or None
        try:
            cat = self.config_mgr.add_category(label, cat_id)
            self._log(f"新建分类: {cat.id} → {cat.source}")
            self._refresh_categories()
        except ValueError as exc:
            messagebox.showerror("错误", str(exc))

    def _new_asset(self) -> None:
        if not self._selected_category:
            messagebox.showinfo("提示", "请先选择分类")
            return
        filename = simpledialog.askstring("新建资源", "文件名（如 role_new.png）:")
        if not filename:
            return
        if not filename.endswith(".png"):
            filename += ".png"
        size = simpledialog.askinteger("新建资源", "导出尺寸（像素）:", initialvalue=512, minvalue=32, maxvalue=2048)
        subject = simpledialog.askstring("新建资源", "说明（可选）:") or ""
        if size is None:
            return
        try:
            asset = self.config_mgr.add_asset(
                filename=filename,
                category=self._selected_category,
                width=size or 512,
                height=size or 512,
                subject=subject,
            )
            self._invalidate_asset_rows_cache()
            self._refresh_assets()
            self.asset_tree.selection_set(asset.id)
            self._on_asset_select()
            self._log(f"新建资源: {asset.id}")
        except ValueError as exc:
            messagebox.showerror("错误", str(exc))

    def _delete_asset(self) -> None:
        assets = self._current_assets()
        if not assets:
            return
        if not messagebox.askyesno("确认", f"删除 {len(assets)} 个资源配置？（不删磁盘图片）"):
            return
        for a in assets:
            self.config_mgr.delete_asset(a.id)
        self._selected_asset_id = None
        self._invalidate_asset_rows_cache()
        self._refresh_assets()
        self._log("已删除选中资源配置")

    def _init_workflows(self) -> None:
        n = self.pipeline.init_asset_workflows_from_default()
        self._log(f"已为 {n} 个资源创建工作流 JSON")

    # ── 生成 / 导出 ─────────────────────────────────────────────

    def _generate_selected(self) -> None:
        assets = self._current_assets()
        if not assets:
            messagebox.showinfo("提示", "请先选择资源")
            return
        self._run_generate(assets)

    def _generate_category(self) -> None:
        if not self._selected_category:
            return
        self._maybe_save_category_paths()
        assets = self.config_mgr.assets(category=self._selected_category)
        self._run_generate(assets)

    def _run_generate_batch(
        self,
        assets: list[Asset],
        *,
        export_after: bool = False,
    ) -> None:
        if self._seed_input() and self._parse_seed() is None:
            return
        synced = self._sync_assets_before_run(assets)
        if synced is None:
            self._log("生成未启动：请检查尺寸 / Seed 等表单字段")
            return
        if not self._validate_seeds_for_batch(synced):
            return
        assets = synced
        enabled = [a for a in assets if a.enabled]
        if not enabled:
            messagebox.showinfo("提示", "没有可生成的资源（可能已禁用）")
            return

        names = ", ".join(a.filename for a in enabled[:5])
        if len(enabled) > 5:
            names += f" …等 {len(enabled)} 张"
        self._log(f"准备生成: {names}", kind="生成")

        progress_cb = self._make_progress_callback()

        def task() -> None:
            total = len(enabled)
            self._call_ui(lambda: self._log(f"── 开始 {'生成并导出' if export_after else '生成'} {total} 张 ──"))
            self._call_ui(
                lambda: self._on_progress_update(
                    {"kind": "batch", "index": 1, "total": total, "filename": enabled[0].filename}
                )
            )
            for i, a in enumerate(enabled, start=1):
                if self.pipeline.cancel_event.is_set():
                    break
                self._call_ui(
                    lambda ii=i, tt=total, fn=a.filename: self._on_progress_update(
                        {"kind": "batch", "index": ii, "total": tt, "filename": fn}
                    )
                )
                try:
                    result = self.pipeline.generate_one(
                        a,
                        to_inbox=True,
                        log=lambda m, fn=a.filename: self._call_ui(lambda msg=m: self._log(msg, kind="生成")),
                        progress_cb=progress_cb,
                    )
                    if export_after and result.ok:
                        self.pipeline.export_one(
                            a, log=lambda m: self._call_ui(lambda msg=m: self._log(msg, kind="生成"))
                        )
                    if result.ok:
                        self._call_ui(lambda aid=a.id: self._on_asset_generated(aid))
                    else:
                        self._call_ui(lambda fn=a.filename, msg=result.message: self._log(f"FAIL {fn}: {msg}"))
                except Exception as exc:
                    if self.pipeline.cancel_event.is_set():
                        self._call_ui(lambda: self._log("── 已取消 ──"))
                        break
                    self._call_ui(lambda e=exc, fn=a.filename: self._log(f"FAIL {fn}: {e}"))
            self._call_ui(lambda: self._log("── 任务结束 ──"))
            self._call_ui(self._refresh_assets)
            if enabled and self._selected_asset_id:
                self._call_ui(lambda aid=self._selected_asset_id: self._on_asset_generated(aid))

        self._run_async(task)

    def _generate_and_export_selected(self) -> None:
        assets = self._current_assets()
        if not assets:
            messagebox.showinfo("提示", "请先选择资源")
            return
        self._run_generate_batch(assets, export_after=True)

    def _run_generate(self, assets: list[Asset]) -> None:
        self._run_generate_batch(assets, export_after=False)

    def _export_selected(self) -> None:
        assets = self._current_assets()
        if not assets:
            messagebox.showinfo("提示", "请先选择资源")
            return
        self._run_export(assets)

    def _export_category(self) -> None:
        if not self._selected_category:
            messagebox.showinfo("提示", "请先选择分类")
            return
        self._maybe_save_category_paths()
        assets = self.config_mgr.assets(category=self._selected_category)
        if not assets:
            messagebox.showinfo("提示", "本类无资源")
            return
        self._run_export(assets)

    def _run_export(self, assets: list[Asset]) -> None:
        def task() -> None:
            self.pipeline.export_many(
                assets, log=lambda m: self._call_ui(lambda msg=m: self._log(msg, kind="生成"))
            )
            self._call_ui(self._refresh_assets)

        self._run_async(task)

    # ── 杂项 ─────────────────────────────────────────────

    def _open_source(self) -> None:
        assets = self._current_assets()
        if not assets:
            return
        cat = self.config_mgr.category_by_id(assets[0].category)
        if cat:
            self._open_path(self.config_mgr.category_source_path(cat))

    def _open_inbox(self) -> None:
        assets = self._current_assets()
        if not assets:
            if self._selected_category:
                cat = self.config_mgr.category_by_id(self._selected_category)
                if cat:
                    self._open_path(self.config_mgr.category_inbox_path(cat))
            return
        cat = self.config_mgr.category_by_id(assets[0].category)
        if cat:
            self._open_path(self.config_mgr.category_inbox_path(cat))

    def _open_unity(self) -> None:
        assets = self._current_assets()
        if not assets:
            if self._selected_category:
                cat = self.config_mgr.category_by_id(self._selected_category)
                if cat:
                    self._open_path(self.config_mgr.category_unity_path(cat))
            return
        cat = self.config_mgr.category_by_id(assets[0].category)
        if cat:
            self._open_path(self.config_mgr.category_unity_path(cat))

    def _open_path(self, path: Path) -> None:
        subprocess.run(["open", str(path)], check=False)

    def _open_readme(self) -> None:
        readme = TOOLS_DIR / "README.md"
        if readme.is_file():
            subprocess.run(["open", str(readme)], check=False)

    def _on_close(self) -> None:
        if self._busy:
            if not messagebox.askyesno("确认", "任务进行中，确定退出？"):
                return
        self._closing = True
        self._preview_load_seq += 1
        self._asset_load_seq += 1
        job = self._refresh_assets_job
        if job is not None:
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
        self._cancel_asset_list_job()
        self._stop_comfyui_poll()
        try:
            scroll = getattr(self, "_detail_scroll", None)
            if scroll is not None:
                scroll.uninstall_global_wheel()
        except tk.TclError:
            pass
        try:
            self._on_log_window_close()
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


def main() -> None:
    # 首次启动：生成配置 + 工作流副本
    mgr = ConfigManager()
    mgr.ensure_all_dirs()
    PipelineCore(mgr).init_asset_workflows_from_default()
    if not HAS_TTB:
        print("提示: pip install ttkbootstrap 可启用现代深色主题", file=sys.stderr)
    app = ArtToolApp()
    app.mainloop()


if __name__ == "__main__":
    main()
