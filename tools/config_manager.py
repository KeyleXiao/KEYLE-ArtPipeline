#!/usr/bin/env python3
"""pipeline_config.json 读写与分类/资源管理。"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from paths import ART_ROOT, CONFIG_FILE, INBOX_ROOT, PROJECT_ROOT, SOURCE_ROOT, WORKFLOWS_DIR

try:
    from cloud.registry import DEFAULT_CLOUD_STRENGTH, parse_cloud_gen_mode
except ImportError:
    DEFAULT_CLOUD_STRENGTH = 0.65

    def parse_cloud_gen_mode(raw: str | None) -> str:
        mode = str(raw or "text_to_image").strip()
        return mode if mode in ("text_to_image", "image_to_image", "image_edit") else "text_to_image"


class RenameFileConflictError(ValueError):
    """重命名目标路径已有文件（常见于删除资源后磁盘 PNG 未删）。"""

    def __init__(self, conflicts: list[dict[str, str]], filename: str) -> None:
        self.conflicts = conflicts
        self.filename = filename
        kinds = ", ".join(c["kind"] for c in conflicts)
        super().__init__(f"目标文件已存在: {filename}（{kinds}）")


try:
    from bootstrap_config import (
        CATEGORY_NEGATIVE_COMMON,
        CATEGORY_POSITIVE_COMMON,
        NO_TRANSPARENT_CATEGORIES,
        join_prompt_segments,
        merge_prompt_prefix,
        merge_prompt_suffix,
    )
except ImportError:
    CATEGORY_POSITIVE_COMMON = (
        "(transparent background:1.4), (alpha channel:1.25), isolated subject, clean cutout sticker, "
        "simple flat background, single color backdrop, no scenery, floating object"
    )
    CATEGORY_NEGATIVE_COMMON = (
        "(solid background:1.35), (opaque background:1.25), complex background, detailed background, "
        "white background, black background, grey background, gradient background, colored backdrop, "
        "scenery, environment, floor, ground, wall, room, studio backdrop, vignette, cast shadow on ground"
    )
    NO_TRANSPARENT_CATEGORIES = frozenset({"backgrounds"})

    def join_prompt_segments(*parts: str) -> str:
        cleaned: list[str] = []
        for part in parts:
            text = str(part or "").strip().rstrip(",")
            if text:
                cleaned.append(text)
        return ", ".join(cleaned)

    def merge_prompt_prefix(prefix: str, body: str) -> str:
        prefix = prefix.strip()
        body = body.strip()
        if not prefix:
            return body
        if not body:
            return prefix
        return f"{prefix}, {body}"

    def merge_prompt_suffix(base: str, suffix: str) -> str:
        base = base.strip()
        suffix = suffix.strip()
        if not suffix:
            return base
        if not base:
            return suffix
        return f"{base}, {suffix}"

SAFE_ID = re.compile(r"[^a-z0-9_]+")

REMOVE_BG_INHERIT = "inherit"
REMOVE_BG_REMOVE = "remove"
REMOVE_BG_KEEP = "keep"
REMOVE_BG_MODES = frozenset({REMOVE_BG_INHERIT, REMOVE_BG_REMOVE, REMOVE_BG_KEEP})

GEN_MODE_TXT2IMG = "txt2img"
GEN_MODE_IMG2IMG = "img2img"
GEN_MODE_REDRAW = "redraw"
GEN_MODES = frozenset({GEN_MODE_TXT2IMG, GEN_MODE_IMG2IMG, GEN_MODE_REDRAW})
GEN_MODES_IMG2IMG = frozenset({GEN_MODE_IMG2IMG, GEN_MODE_REDRAW})
DEFAULT_IMG2IMG_DENOISE = 0.65
DEFAULT_IMG2IMG_WORKFLOW = "workflows/_default_sdxl_img2img_api.json"


def parse_remove_bg_mode(raw: dict[str, Any]) -> str:
    """解析资源抠图模式（兼容旧版 remove_bg 布尔字段）。"""
    if "remove_bg_mode" in raw:
        v = str(raw["remove_bg_mode"]).strip().lower()
        if v in ("default", "inherit", ""):
            return REMOVE_BG_INHERIT
        if v in ("remove", "on", "true", "yes"):
            return REMOVE_BG_REMOVE
        if v in ("keep", "off", "false", "no"):
            return REMOVE_BG_KEEP
        return REMOVE_BG_INHERIT
    if "remove_bg" in raw:
        return REMOVE_BG_REMOVE if raw["remove_bg"] else REMOVE_BG_KEEP
    return REMOVE_BG_INHERIT


def parse_gen_mode(raw: dict[str, Any]) -> str:
    v = str(raw.get("gen_mode", GEN_MODE_TXT2IMG)).strip().lower()
    if v == GEN_MODE_IMG2IMG and raw.get("ref_image_use_source"):
        return GEN_MODE_REDRAW
    return v if v in GEN_MODES else GEN_MODE_TXT2IMG


def effective_gen_mode(asset: Asset) -> str:
    """运行时生成模式（兼容 img2img + ref_image_use_source → redraw）。"""
    mode = getattr(asset, "gen_mode", GEN_MODE_TXT2IMG) or GEN_MODE_TXT2IMG
    if mode == GEN_MODE_IMG2IMG and getattr(asset, "ref_image_use_source", False):
        return GEN_MODE_REDRAW
    return mode if mode in GEN_MODES else GEN_MODE_TXT2IMG


@dataclass
class Category:
    id: str
    label: str
    source: str
    inbox: str
    unity: str
    default_workflow: str = "workflows/_default_sdxl_api.json"
    checkpoint: str = ""  # 分类级 checkpoint；空表示未设置
    lora: str = ""
    lora_strength: float = 0.7
    positive_common: str = ""
    negative_common: str = ""
    alpha_matte: str = "none"


@dataclass
class Asset:
    id: str
    filename: str
    category: str
    width: int = 512
    height: int = 512
    subject: str = ""
    positive: str = ""
    negative: str = ""
    positive_prefix: str = ""
    positive_subject: str = ""
    positive_scene: str = ""
    positive_light: str = ""
    positive_g: str = ""
    positive_l: str = ""
    workflow: str = ""
    enabled: bool = True
    gen_scale: float = 2.0
    seed: str = ""
    checkpoint: str = ""  # 空则继承分类 checkpoint
    remove_bg_mode: str = REMOVE_BG_INHERIT
    gen_mode: str = GEN_MODE_TXT2IMG
    ref_image: str = ""
    ref_image_use_source: bool = False
    img2img_denoise: float = DEFAULT_IMG2IMG_DENOISE
    cloud_gen_mode: str = "text_to_image"
    cloud_prompt: str = ""
    cloud_negative: str = ""
    cloud_strength: float = DEFAULT_CLOUD_STRENGTH
    cloud_output_count: int = 1
    cloud_ref_images_extra: str = ""

    @property
    def size(self) -> int:
        """兼容旧逻辑：取宽高中较大边。"""
        return max(self.width, self.height)

    def size_label(self) -> str:
        return f"{self.width}×{self.height}"

    def workflow_path(self) -> Path | None:
        if not self.workflow:
            return None
        p = WORKFLOWS_DIR.parent / self.workflow
        if self.workflow.startswith("workflows/"):
            p = WORKFLOWS_DIR.parent / self.workflow
        p = Path(self.workflow) if Path(self.workflow).is_absolute() else WORKFLOWS_DIR.parent / self.workflow
        return p


class ConfigManager:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or CONFIG_FILE
        self.data: dict[str, Any] = {}
        self._assets_cache: list[Asset] | None = None
        self._asset_by_id: dict[str, Asset] | None = None
        self._categories_cache: list[Category] | None = None
        self._category_by_id: dict[str, Category] | None = None
        self.load()

    def _invalidate_caches(self) -> None:
        self._assets_cache = None
        self._asset_by_id = None
        self._categories_cache = None
        self._category_by_id = None

    def load(self) -> None:
        if not self.path.is_file():
            self.data = _default_config()
        else:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        self._invalidate_caches()
        self._ensure_app_meta()
        self._ensure_root_paths_in_config()

    def _ensure_app_meta(self) -> None:
        """补全 app_version / release_notes（欢迎弹窗与版本说明）。"""
        from app_release import APP_VERSION, DEFAULT_RELEASE_NOTES

        dirty = False
        if not str(self.data.get("app_version", "")).strip():
            self.data["app_version"] = APP_VERSION
            dirty = True
        rn = self.data.get("release_notes")
        if not isinstance(rn, dict) or not str(rn.get("zh-CN", "")).strip():
            self.data["release_notes"] = dict(DEFAULT_RELEASE_NOTES)
            dirty = True
        if dirty:
            self.save()

    def _ensure_root_paths_in_config(self) -> None:
        """首次加载时写入推断的项目根路径（便于分类相对路径解析）。"""
        d = self.defaults
        dirty = False
        if not str(d.get("project_root", "")).strip():
            d["project_root"] = str(PROJECT_ROOT.resolve())
            dirty = True
        if not str(d.get("art_pipeline_root", "")).strip():
            d["art_pipeline_root"] = str(ART_ROOT.resolve())
            dirty = True
        if dirty:
            self.save()

    def project_root(self) -> Path:
        raw = str(self.defaults.get("project_root", "")).strip()
        if raw:
            return Path(raw).expanduser().resolve()
        return PROJECT_ROOT.resolve()

    def art_root(self) -> Path:
        raw = str(self.defaults.get("art_pipeline_root", "")).strip()
        if raw:
            return Path(raw).expanduser().resolve()
        sibling = self.project_root() / "ArtPipeline"
        if sibling.is_dir():
            return sibling.resolve()
        return ART_ROOT.resolve()

    def log_dir(self) -> Path:
        raw = str(self.defaults.get("log_dir", "")).strip()
        if raw:
            return Path(raw).expanduser().resolve()
        from paths import default_log_dir

        return default_log_dir()

    @staticmethod
    def _join_under(root: Path, rel: str) -> Path:
        s = (rel or "").strip().replace("\\", "/")
        if not s:
            return root.resolve()
        p = Path(s).expanduser()
        if p.is_absolute():
            return p.resolve()
        # 旧版 strip("/") 会把 /Users/... 存成 Users/...，导出时误拼到项目根下
        if s.startswith("Users/") or s.startswith("Volumes/"):
            legacy = Path("/") / s
            return legacy.expanduser().resolve()
        rel_part = s.strip("/")
        if not rel_part:
            return root.resolve()
        return (root / rel_part).resolve()

    def normalize_category_path(self, raw: str, *, under: str) -> str:
        """将分类路径规范为绝对路径（source/inbox→art 根，unity→项目根）。"""
        s = (raw or "").strip()
        if not s:
            return s
        root = self.art_root() if under == "art" else self.project_root()
        return str(self._join_under(root, s))

    def category_source_path(self, cat: Category) -> Path:
        return self._join_under(self.art_root(), cat.source)

    def category_inbox_path(self, cat: Category) -> Path:
        return self._join_under(self.art_root(), cat.inbox)

    def category_unity_path(self, cat: Category) -> Path:
        return self._join_under(self.project_root(), cat.unity)

    def rel_to_project(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.project_root()))
        except ValueError:
            return str(path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self._invalidate_caches()

    @property
    def defaults(self) -> dict[str, Any]:
        return self.data.setdefault("defaults", {})

    def categories(self) -> list[Category]:
        if self._categories_cache is not None:
            return self._categories_cache
        out: list[Category] = []
        for raw in self.data.get("categories", []):
            raw.setdefault("checkpoint", "")
            out.append(
                Category(
                    id=raw["id"],
                    label=raw["label"],
                    source=raw["source"],
                    inbox=raw["inbox"],
                    unity=raw["unity"],
                    default_workflow=raw.get("default_workflow", "workflows/_default_sdxl_api.json"),
                    checkpoint=raw.get("checkpoint", ""),
                    lora=str(raw.get("lora", "")),
                    lora_strength=float(raw.get("lora_strength", 0.7)),
                    positive_common=str(
                        raw.get("positive_common", CATEGORY_POSITIVE_COMMON)
                    ),
                    negative_common=str(
                        raw.get("negative_common", CATEGORY_NEGATIVE_COMMON)
                    ),
                    alpha_matte=str(raw.get("alpha_matte", "none")),
                )
            )
        self._categories_cache = out
        self._category_by_id = {c.id: c for c in out}
        return out

    def category_by_id(self, cat_id: str) -> Category | None:
        if self._category_by_id is None:
            self.categories()
        assert self._category_by_id is not None
        return self._category_by_id.get(cat_id)

    def _all_assets(self) -> list[Asset]:
        if self._assets_cache is not None:
            return self._assets_cache
        out: list[Asset] = []
        for raw in self.data.get("assets", []):
            legacy = int(raw.get("size", 512))
            a = Asset(
                id=raw["id"],
                filename=raw["filename"],
                category=raw["category"],
                width=int(raw.get("width", legacy)),
                height=int(raw.get("height", legacy)),
                subject=raw.get("subject", ""),
                positive=raw.get("positive", ""),
                negative=raw.get("negative", ""),
                positive_prefix=raw.get("positive_prefix", ""),
                positive_subject=raw.get("positive_subject", ""),
                positive_scene=raw.get("positive_scene", ""),
                positive_light=raw.get("positive_light", ""),
                positive_g=raw.get("positive_g", ""),
                positive_l=raw.get("positive_l", ""),
                workflow=raw.get("workflow", ""),
                enabled=bool(raw.get("enabled", True)),
                gen_scale=float(
                    raw.get(
                        "gen_scale",
                        2.0 if raw.get("category") in ("roles", "items") else 1.0,
                    )
                ),
                seed=str(raw.get("seed", "")),
                checkpoint=str(raw.get("checkpoint", "")),
                remove_bg_mode=parse_remove_bg_mode(raw),
                gen_mode=parse_gen_mode(raw),
                ref_image=str(raw.get("ref_image", "")),
                ref_image_use_source=bool(raw.get("ref_image_use_source", False)),
                img2img_denoise=float(raw.get("img2img_denoise", DEFAULT_IMG2IMG_DENOISE)),
                cloud_gen_mode=parse_cloud_gen_mode(raw.get("cloud_gen_mode")),
                cloud_prompt=str(raw.get("cloud_prompt", "")),
                cloud_negative=str(raw.get("cloud_negative", "")),
                cloud_strength=float(raw.get("cloud_strength", DEFAULT_CLOUD_STRENGTH)),
                cloud_output_count=max(1, min(15, int(raw.get("cloud_output_count", 1) or 1))),
                cloud_ref_images_extra=str(raw.get("cloud_ref_images_extra", "")),
            )
            out.append(a)
        self._assets_cache = out
        self._asset_by_id = {a.id: a for a in out}
        return out

    def assets(self, *, category: str | None = None, enabled_only: bool = False) -> list[Asset]:
        out = self._all_assets()
        if category:
            out = [a for a in out if a.category == category]
        if enabled_only:
            out = [a for a in out if a.enabled]
        return out

    def asset_by_id(self, asset_id: str) -> Asset | None:
        if self._asset_by_id is None:
            self._all_assets()
        assert self._asset_by_id is not None
        return self._asset_by_id.get(asset_id)

    def asset_by_filename(self, filename: str) -> Asset | None:
        for a in self._all_assets():
            if a.filename == filename:
                return a
        return None

    def ensure_category_dirs(self, cat: Category) -> None:
        self.category_source_path(cat).mkdir(parents=True, exist_ok=True)
        self.category_inbox_path(cat).mkdir(parents=True, exist_ok=True)
        self.category_unity_path(cat).mkdir(parents=True, exist_ok=True)

    def ensure_all_dirs(self) -> None:
        for cat in self.categories():
            self.ensure_category_dirs(cat)

    def add_category(self, label: str, cat_id: str | None = None, *, checkpoint: str = "") -> Category:
        cat_id = cat_id or SAFE_ID.sub("_", label.lower()).strip("_") or str(uuid.uuid4())[:8]
        if self.category_by_id(cat_id):
            raise ValueError(f"分类已存在: {cat_id}")
        ckpt = checkpoint.strip() or str(self.defaults.get("checkpoint", ""))
        cat = Category(
            id=cat_id,
            label=label,
            source=f"source/{cat_id}",
            inbox=f"inbox/{cat_id}",
            unity=f"Assets/Art/UI/Icons/{cat_id.title()}",
            checkpoint=ckpt,
            positive_common=CATEGORY_POSITIVE_COMMON,
            negative_common=CATEGORY_NEGATIVE_COMMON,
            alpha_matte="none",
        )
        self.data.setdefault("categories", []).append(cat.__dict__)
        self.ensure_category_dirs(cat)
        self.save()
        return cat

    def _allocate_asset_id(self, filename: str, category: str) -> str:
        """由文件名推导资源 ID；纯中文等无法推导时自动生成唯一 ID。"""
        stem = Path(filename).stem
        base = SAFE_ID.sub("_", stem.lower()).strip("_")
        if not base:
            cat = SAFE_ID.sub("_", category.lower()).strip("_") or "asset"
            return f"{cat}_{uuid.uuid4().hex[:8]}"
        if not self.asset_by_id(base):
            return base
        raise ValueError(f"资源已存在: {base}")

    def add_asset(
        self,
        *,
        filename: str,
        category: str,
        width: int = 512,
        height: int = 512,
        subject: str = "",
        positive: str = "",
        negative: str = "",
        workflow: str = "",
        seed: str = "",
        enabled: bool = True,
    ) -> Asset:
        if not self.category_by_id(category):
            raise ValueError(f"未知分类: {category}")
        asset_id = self._allocate_asset_id(filename, category)
        cat = self.category_by_id(category)
        wf = workflow or cat.default_workflow
        asset = Asset(
            id=asset_id,
            filename=filename,
            category=category,
            width=width,
            height=height,
            subject=subject,
            positive=positive,
            negative=negative,
            workflow=wf,
            seed=seed,
            enabled=enabled,
        )
        self.data.setdefault("assets", []).append(asset.__dict__)
        wf_path = WORKFLOWS_DIR / "assets" / f"{asset_id}.json"
        if not wf_path.is_file() and cat.default_workflow:
            src = WORKFLOWS_DIR / Path(cat.default_workflow).name
            if src.is_file():
                wf_path.parent.mkdir(parents=True, exist_ok=True)
                wf_path.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
                asset.workflow = f"workflows/assets/{asset_id}.json"
                self._update_asset_dict(asset)
        self.save()
        return asset

    def update_asset(self, asset: Asset) -> None:
        old_raw = self._asset_raw(asset.id)
        if old_raw:
            old_fn = str(old_raw.get("filename", ""))
            if old_fn and asset.filename != old_fn:
                self.rename_asset_files(asset, asset.filename)
        self._update_asset_dict(asset)
        self.save()

    def _update_asset_dict(self, asset: Asset) -> None:
        payload = {
            "id": asset.id,
            "filename": asset.filename,
            "category": asset.category,
            "width": asset.width,
            "height": asset.height,
            "subject": asset.subject,
            "positive": asset.positive,
            "negative": asset.negative,
            "positive_prefix": asset.positive_prefix,
            "positive_subject": asset.positive_subject,
            "positive_scene": asset.positive_scene,
            "positive_light": asset.positive_light,
            "positive_g": asset.positive_g,
            "positive_l": asset.positive_l,
            "workflow": asset.workflow,
            "enabled": asset.enabled,
            "gen_scale": asset.gen_scale,
            "seed": asset.seed,
            "checkpoint": asset.checkpoint,
            "remove_bg_mode": asset.remove_bg_mode,
            "gen_mode": asset.gen_mode,
            "ref_image": asset.ref_image,
            "ref_image_use_source": asset.ref_image_use_source,
            "img2img_denoise": asset.img2img_denoise,
            "cloud_gen_mode": asset.cloud_gen_mode,
            "cloud_prompt": asset.cloud_prompt,
            "cloud_negative": asset.cloud_negative,
            "cloud_strength": asset.cloud_strength,
            "cloud_output_count": int(getattr(asset, "cloud_output_count", 1) or 1),
            "cloud_ref_images_extra": getattr(asset, "cloud_ref_images_extra", ""),
        }
        for i, raw in enumerate(self.data.get("assets", [])):
            if raw.get("id") == asset.id:
                if "postprocess" in raw:
                    payload["postprocess"] = raw["postprocess"]
                self.data["assets"][i] = payload
                return
        raise KeyError(asset.id)

    def resolve_seed_for_asset(self, asset: Asset, *, override: int | None = None) -> int:
        """优先级：CLI/API 指定 > 资源 seed > 全局 seed > 随机。"""
        import random

        if override is not None:
            return override
        asset_s = (asset.seed or "").strip()
        if asset_s:
            return int(asset_s)
        global_s = str(self.defaults.get("seed", "")).strip()
        if global_s:
            return int(global_s)
        return random.randint(1, 2**31 - 1)

    def checkpoint_for_category(self, cat_id: str) -> str:
        cat = self.category_by_id(cat_id)
        if cat and cat.checkpoint.strip():
            return cat.checkpoint.strip()
        fallback = str(self.defaults.get("checkpoint", "")).strip()
        return fallback

    def checkpoint_for_asset(self, asset: Asset) -> str:
        if asset.checkpoint.strip():
            return asset.checkpoint.strip()
        return self.checkpoint_for_category(asset.category)

    def lora_for_category(self, cat_id: str) -> tuple[str, float]:
        cat = self.category_by_id(cat_id)
        if cat and cat.lora.strip():
            return cat.lora.strip(), float(cat.lora_strength)
        return "", 0.0

    def category_remove_bg_default(self, cat_id: str) -> bool:
        """分类是否默认剔除纯色背景。"""
        return self.alpha_matte_for_category(cat_id) != "none"

    def should_remove_bg(self, asset: Asset, cat: Category | None = None) -> bool:
        """预览/生成时是否对该资源做纯色底抠图。"""
        mode = asset.remove_bg_mode
        if mode == REMOVE_BG_REMOVE:
            return True
        if mode == REMOVE_BG_KEEP:
            return False
        cat = cat or self.category_by_id(asset.category)
        if cat:
            return cat.alpha_matte.strip().lower() != "none"
        return asset.category not in NO_TRANSPARENT_CATEGORIES

    def alpha_matte_for_category(self, cat_id: str) -> str:
        cat = self.category_by_id(cat_id)
        if cat and cat.alpha_matte.strip():
            return cat.alpha_matte.strip()
        return "none" if cat_id in NO_TRANSPARENT_CATEGORIES else "border"

    def alpha_matte_for_asset(self, asset: Asset) -> str:
        """生成管线使用的抠图模式；不抠图时返回 none。"""
        if not self.should_remove_bg(asset):
            return "none"
        mode = self.alpha_matte_for_category(asset.category)
        return mode if mode != "none" else "border"

    def asset_has_prompt_segments(self, asset: Asset) -> bool:
        return any(
            str(getattr(asset, key, "") or "").strip()
            for key in ("positive_prefix", "positive_subject", "positive_scene", "positive_light")
        )

    def compose_positive_body(self, asset: Asset) -> str:
        if self.asset_has_prompt_segments(asset):
            return join_prompt_segments(
                asset.positive_prefix,
                asset.positive_subject,
                asset.positive_scene,
                asset.positive_light,
            )
        return (asset.positive or "").strip()

    def compose_sdxl_g_body(self, asset: Asset) -> str:
        if self.asset_has_prompt_segments(asset):
            explicit = (asset.positive_g or "").strip()
            if explicit:
                return explicit
            return join_prompt_segments(
                asset.positive_prefix,
                asset.positive_subject,
                asset.positive_scene,
            )
        return (asset.positive_g or asset.positive or "").strip()

    def compose_sdxl_l_body(self, asset: Asset) -> str:
        if self.asset_has_prompt_segments(asset):
            explicit = (asset.positive_l or "").strip()
            if explicit:
                return explicit
            return join_prompt_segments(
                asset.positive_subject,
                asset.positive_scene,
                asset.positive_light,
            )
        return (asset.positive_l or asset.positive or "").strip()

    def sync_asset_prompt_fields(self, asset: Asset) -> None:
        """根据分段同步 positive 与 SDXL G/L（有分段时）。"""
        if not self.asset_has_prompt_segments(asset):
            return
        asset.positive = self.compose_positive_body(asset)
        asset.positive_g = join_prompt_segments(
            asset.positive_prefix,
            asset.positive_subject,
            asset.positive_scene,
        )
        asset.positive_l = join_prompt_segments(
            asset.positive_subject,
            asset.positive_scene,
            asset.positive_light,
        )

    def prompts_for_generation(self, asset: Asset) -> dict[str, str]:
        """合并资源 prompt 与分类通用 prompt（生成时使用）。"""
        cat = self.category_by_id(asset.category)
        pos_common = (cat.positive_common if cat else "").strip()
        neg_common = (cat.negative_common if cat else "").strip()
        body = self.compose_positive_body(asset)
        g_body = self.compose_sdxl_g_body(asset)
        l_body = self.compose_sdxl_l_body(asset)
        scene_bg = join_prompt_segments(
            asset.positive_prefix,
            asset.positive_scene,
            asset.positive_light,
        )
        subject_fg = join_prompt_segments(
            asset.positive_prefix,
            asset.positive_subject,
        )
        if not scene_bg.strip():
            scene_bg = body
        if not subject_fg.strip():
            subject_fg = body
        positive = merge_prompt_prefix(pos_common, body)
        positive_g = merge_prompt_prefix(pos_common, g_body)
        positive_l = merge_prompt_prefix(pos_common, l_body)
        positive_scene_bg = merge_prompt_prefix(pos_common, scene_bg)
        positive_subject_fg = merge_prompt_prefix(pos_common, subject_fg)
        negative = merge_prompt_suffix(asset.negative, neg_common)
        return {
            "positive": positive,
            "positive_g": positive_g,
            "positive_l": positive_l,
            "negative": negative,
            "positive_prefix": asset.positive_prefix.strip(),
            "positive_subject": asset.positive_subject.strip(),
            "positive_scene": asset.positive_scene.strip(),
            "positive_light": asset.positive_light.strip(),
            "positive_scene_bg": positive_scene_bg,
            "positive_subject_fg": positive_subject_fg,
        }

    def update_category(self, cat: Category) -> None:
        for i, raw in enumerate(self.data.get("categories", [])):
            if raw.get("id") == cat.id:
                self.data["categories"][i] = cat.__dict__
                self.save()
                return
        raise KeyError(cat.id)

    def delete_asset(self, asset_id: str) -> None:
        self.data["assets"] = [a for a in self.data.get("assets", []) if a.get("id") != asset_id]
        self.save()

    def trash_dir(self) -> Path:
        return self.art_root() / "trash"

    def move_asset_to_trash(self, asset_id: str) -> dict[str, Any]:
        """将资源配置移除，并把 source / inbox / unity PNG 与工作流移入 art/trash。"""
        asset = self.asset_by_id(asset_id)
        if not asset:
            raise ValueError(f"资源不存在: {asset_id}")
        cat = self.category_by_id(asset.category)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest_dir = self.trash_dir() / f"{stamp}_{asset.id}"
        dest_dir.mkdir(parents=True, exist_ok=True)

        moved_files: list[str] = []
        src, inbox, unity = self.resolve_paths(asset)
        for kind, path in (("source", src), ("inbox", inbox), ("unity", unity)):
            if path.is_file():
                dest = dest_dir / f"{kind}_{path.name}"
                shutil.move(str(path), str(dest))
                moved_files.append(str(dest))

        wf = asset.workflow_path()
        if wf and wf.is_file():
            dest = dest_dir / wf.name
            shutil.move(str(wf), str(dest))
            moved_files.append(str(dest))

        meta = {
            "trashed_at": stamp,
            "asset": asdict(asset),
            "category_label": cat.label if cat else asset.category,
            "moved_files": moved_files,
        }
        (dest_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        asset_snapshot = {"id": asset.id, "filename": asset.filename}
        self.delete_asset(asset_id)
        return {
            **asset_snapshot,
            "trash_dir": str(dest_dir),
            "moved_files": moved_files,
        }

    def delete_category(self, cat_id: str) -> int:
        if not self.category_by_id(cat_id):
            raise ValueError(f"未知分类: {cat_id}")
        assets = self.data.get("assets", [])
        removed = sum(1 for a in assets if a.get("category") == cat_id)
        self.data["assets"] = [a for a in assets if a.get("category") != cat_id]
        self.data["categories"] = [c for c in self.data.get("categories", []) if c.get("id") != cat_id]
        self._invalidate_caches()
        self.save()
        return removed

    def resolve_paths(self, asset: Asset) -> tuple[Path, Path, Path]:
        cat = self.category_by_id(asset.category)
        if not cat:
            raise ValueError(f"资源 {asset.id} 的分类不存在")
        return (
            self.category_source_path(cat) / asset.filename,
            self.category_inbox_path(cat) / asset.filename,
            self.category_unity_path(cat) / asset.filename,
        )

    @staticmethod
    def normalize_asset_filename(name: str) -> str:
        fn = (name or "").strip()
        if not fn:
            raise ValueError("文件名不能为空")
        if any(c in fn for c in ("/", "\\", "\0")):
            raise ValueError("文件名不能包含路径分隔符")
        if not fn.lower().endswith(".png"):
            fn = f"{fn}.png"
        return fn

    @staticmethod
    def _rename_path_conflict(old_p: Path, new_p: Path) -> bool:
        """目标路径被占用且与源路径不是同一文件。"""
        if not new_p.exists():
            return False
        try:
            return old_p.resolve() != new_p.resolve()
        except OSError:
            return True

    def _rename_asset_pairs(self, asset: Asset, new_filename: str) -> tuple[tuple, ...]:
        cat = self.category_by_id(asset.category)
        if not cat:
            raise ValueError(f"资源 {asset.id} 的分类不存在")
        src_old, inbox_old, unity_old = self.resolve_paths(asset)
        src_new = self.category_source_path(cat) / new_filename
        inbox_new = self.category_inbox_path(cat) / new_filename
        unity_new = self.category_unity_path(cat) / new_filename
        return (
            ("source", src_old, src_new),
            ("inbox", inbox_old, inbox_new),
            ("unity", unity_old, unity_new),
        )

    def rename_asset_file_conflicts(
        self, asset: Asset, new_filename: str
    ) -> list[dict[str, str]]:
        """列出重命名时会被占用的 source / inbox / unity 目标路径。"""
        new_filename = self.normalize_asset_filename(new_filename)
        if new_filename == asset.filename:
            return []
        pairs = self._rename_asset_pairs(asset, new_filename)
        conflicts: list[dict[str, str]] = []
        for kind, old_p, new_p in pairs:
            if self._rename_path_conflict(old_p, new_p):
                conflicts.append({"kind": kind, "path": str(new_p)})
        return conflicts

    def rename_asset_files(
        self, asset: Asset, new_filename: str, *, overwrite: bool = False
    ) -> list[str]:
        """重命名 source / inbox / unity 下已存在的 PNG；更新 asset.filename。"""
        new_filename = self.normalize_asset_filename(new_filename)
        old_filename = asset.filename
        if new_filename == old_filename:
            return []

        other = self.asset_by_filename(new_filename)
        if other and other.id != asset.id:
            raise ValueError(f"文件名已被占用: {new_filename}")

        pairs = self._rename_asset_pairs(asset, new_filename)
        conflicts = self.rename_asset_file_conflicts(asset, new_filename)
        if conflicts and not overwrite:
            raise RenameFileConflictError(conflicts, new_filename)

        renamed: list[str] = []
        done: list[tuple[Path, Path]] = []
        try:
            for _kind, old_p, new_p in pairs:
                if self._rename_path_conflict(old_p, new_p):
                    if overwrite:
                        new_p.unlink(missing_ok=True)
                if not old_p.is_file():
                    continue
                try:
                    if old_p.resolve() == new_p.resolve():
                        continue
                except OSError:
                    pass
                new_p.parent.mkdir(parents=True, exist_ok=True)
                old_p.rename(new_p)
                done.append((new_p, old_p))
                renamed.append(str(new_p))
        except OSError as exc:
            for new_p, old_p in reversed(done):
                try:
                    if new_p.is_file() and not old_p.exists():
                        new_p.rename(old_p)
                except OSError:
                    pass
            raise ValueError(f"重命名失败: {exc}") from exc

        asset.filename = new_filename
        return renamed

    def _move_asset_plan(self, asset: Asset, new_cat_id: str) -> dict[str, Any]:
        new_cat = self.category_by_id(new_cat_id)
        if not new_cat:
            raise ValueError(f"未知分类: {new_cat_id}")
        old_cat = self.category_by_id(asset.category)
        if not old_cat:
            raise ValueError(f"资源 {asset.id} 的分类不存在")
        if old_cat.id == new_cat.id:
            return {
                "same_category": True,
                "from_category": old_cat.id,
                "to_category": new_cat.id,
                "from_label": old_cat.label,
                "to_label": new_cat.label,
                "filename": asset.filename,
                "files": [],
            }
        for other in self.assets(category=new_cat.id):
            if other.filename == asset.filename and other.id != asset.id:
                raise ValueError(f"目标分类已有同名资源: {asset.filename}")

        filename = asset.filename
        pairs = (
            ("source", self.category_source_path(old_cat) / filename, self.category_source_path(new_cat) / filename),
            ("inbox", self.category_inbox_path(old_cat) / filename, self.category_inbox_path(new_cat) / filename),
            ("unity", self.category_unity_path(old_cat) / filename, self.category_unity_path(new_cat) / filename),
        )
        files: list[dict[str, Any]] = []
        for kind, old_p, new_p in pairs:
            exists = old_p.is_file()
            conflict = new_p.exists() and (not exists or old_p.resolve() != new_p.resolve())
            files.append(
                {
                    "kind": kind,
                    "from": str(old_p),
                    "to": str(new_p),
                    "exists": exists,
                    "conflict": conflict,
                }
            )
        conflicts = [f for f in files if f["conflict"]]
        if conflicts:
            kind = conflicts[0]["kind"]
            raise ValueError(f"{kind} 目标文件已存在: {filename}")
        return {
            "same_category": False,
            "from_category": old_cat.id,
            "to_category": new_cat.id,
            "from_label": old_cat.label,
            "to_label": new_cat.label,
            "filename": filename,
            "files": files,
        }

    def preview_move_asset_to_category(self, asset: Asset, new_cat_id: str) -> dict[str, Any]:
        return self._move_asset_plan(asset, new_cat_id)

    def move_asset_to_category(self, asset: Asset, new_cat_id: str) -> dict[str, Any]:
        plan = self._move_asset_plan(asset, new_cat_id)
        if plan["same_category"]:
            return {"moved": [], "skipped": [], **plan}

        moved: list[str] = []
        skipped: list[str] = []
        done: list[tuple[Path, Path]] = []
        try:
            for entry in plan["files"]:
                old_p = Path(entry["from"])
                new_p = Path(entry["to"])
                if not entry["exists"]:
                    skipped.append(entry["kind"])
                    continue
                try:
                    if old_p.resolve() == new_p.resolve():
                        skipped.append(entry["kind"])
                        continue
                except OSError:
                    pass
                new_p.parent.mkdir(parents=True, exist_ok=True)
                old_p.rename(new_p)
                done.append((new_p, old_p))
                moved.append(str(new_p))
        except OSError as exc:
            for new_p, old_p in reversed(done):
                try:
                    if new_p.is_file() and not old_p.exists():
                        new_p.rename(old_p)
                except OSError:
                    pass
            raise ValueError(f"移动文件失败: {exc}") from exc

        asset.category = new_cat_id
        self._update_asset_dict(asset)
        self.save()
        return {"moved": moved, "skipped": skipped, **plan}

    def workflow_file_for_asset(self, asset: Asset) -> Path:
        cat = self.category_by_id(asset.category)
        rel = asset.workflow or (cat.default_workflow if cat else "workflows/_default_sdxl_api.json")
        p = WORKFLOWS_DIR.parent / rel
        if not p.is_file():
            p = WORKFLOWS_DIR / Path(rel).name
        return p

    def img2img_workflow_for_asset(self, asset: Asset) -> Path:
        wf_path = self.workflow_file_for_asset(asset)
        if wf_path.is_file():
            try:
                if "{{REF_IMAGE}}" in wf_path.read_text(encoding="utf-8"):
                    return wf_path
            except OSError:
                pass
        p = WORKFLOWS_DIR / Path(DEFAULT_IMG2IMG_WORKFLOW).name
        if p.is_file():
            return p
        return WORKFLOWS_DIR / "_default_sdxl_img2img_api.json"

    def resolve_ref_image_path(self, asset: Asset) -> Path | None:
        mode = effective_gen_mode(asset)
        src, inbox, _unity = self.resolve_paths(asset)
        if mode == GEN_MODE_REDRAW:
            # 重绘：优先 inbox（后处理/导入的工作副本），无 inbox 再回退 source
            for path in (inbox, src):
                try:
                    if path.is_file():
                        return path.resolve()
                except OSError:
                    pass
            return None
        if getattr(asset, "ref_image_use_source", False):
            try:
                if src.is_file():
                    return src.resolve()
            except OSError:
                pass
            try:
                if inbox.is_file():
                    return inbox.resolve()
            except OSError:
                pass
            return None
        raw = (asset.ref_image or "").strip()
        if not raw:
            return None
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = self.art_root() / raw
        p = p.resolve()
        return p if p.is_file() else None

    def resolve_redraw_gen_size(self, asset: Asset, ref_path: Path | None = None) -> tuple[int, int]:
        """重绘图 ComfyUI 尺寸：优先 source 原图像素；无 source 时用当前参考图。"""
        src, _, _unity = self.resolve_paths(asset)
        dim_path = src if src.is_file() else ref_path
        if dim_path is None or not dim_path.is_file():
            return int(asset.width), int(asset.height)
        try:
            from PIL import Image

            with Image.open(dim_path) as im:
                w, h = im.size
                return int(w), int(h)
        except OSError:
            return int(asset.width), int(asset.height)

    def _asset_raw(self, asset_id: str) -> dict[str, Any] | None:
        for raw in self.data.get("assets", []):
            if raw.get("id") == asset_id:
                return raw
        return None

    def get_postprocess_stack(self, asset_id: str) -> "LayerStack | None":
        from postprocess.models import stack_from_dict

        raw = self._asset_raw(asset_id)
        if not raw:
            return None
        return stack_from_dict(raw.get("postprocess"))

    def set_postprocess_stack(self, asset_id: str, stack: "LayerStack") -> None:
        from postprocess.models import stack_to_dict

        raw = self._asset_raw(asset_id)
        if not raw:
            raise KeyError(asset_id)
        raw["postprocess"] = stack_to_dict(stack)
        self.save()

    def default_postprocess_stack(self, asset: Asset) -> "LayerStack":
        from postprocess.templates import resolve_template

        return resolve_template(
            self.data,
            template_id=None,
            category_id=asset.category,
            width=asset.width,
            height=asset.height,
        )


def _default_config() -> dict[str, Any]:
    """首次启动时的默认配置（从现有 manifest 迁移）。"""
    from bootstrap_config import build_default_config

    return build_default_config()
