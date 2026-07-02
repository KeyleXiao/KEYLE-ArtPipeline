#!/usr/bin/env python3
"""
将 ArtPipeline 源码同步到 GitHub 仓库目录，并过滤敏感配置。

用法:
  cd ArtPipeline/artApp
  python3 sync_to_github.py
  python3 sync_to_github.py --dest ~/ArtPipeline-Studio
  python3 sync_to_github.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ARTAPP_ROOT = Path(__file__).resolve().parent
PIPELINE_ROOT = ARTAPP_ROOT.parent
ARTAPP_SITE = PIPELINE_ROOT / "artAppSite"
DEFAULT_DEST = Path.home() / "ArtPipeline-Studio"

# 官网截图 → GitHub docs/images/（英文名便于 README 引用）
SCREENSHOT_MAP = {
    "主界面功能预览.png": "main-workbench.png",
    "提示词与工作流截图.png": "prompts-workflow.png",
    "生成的图片可以继续重新绘制截图.png": "img2img-redraw.png",
    "后处理功能.png": "postprocess-editor.png",
    "创建本地资源分类.png": "create-category.png",
    "分类功能菜单.png": "category-context-menu.png",
    "创建本地资源.png": "create-asset-manual.png",
    "外部资源导入界面.png": "import-external-assets.png",
    "资源页签功能菜单.png": "asset-context-menu.png",
    "通过ai生成图片需要的配置.png": "comfyui-checkpoint-setup.png",
    "提示词生成贴图.png": "prompt-generate.png",
    "查看本地日志.png": "runtime-logs.png",
}

# 同步的顶层条目（相对 ArtPipeline 根）
SYNC_TOP_LEVEL = (
    "artApp",
    "tools",
    "docs",
    "manifest",
    "overlays",
    "comfyui",
    ".github",
    "README.md",
)

GITHUB_GITIGNORE = """# 构建与本地环境
artApp/release/
artApp/.build-venv/
**/__pycache__/
**/*.pyc
.DS_Store
.sync_staging/
.sync_backup_meta/

# 敏感配置（复制 pipeline_config.example.json 为 pipeline_config.json 后本地填写）
tools/pipeline_config.json

# 美术资源（体积大，本地生成）
source/**/*.png
source/**/*.webp
source/**/*.jpg
source/**/*.jpeg
inbox/**/*.png
inbox/**/*.webp
inbox/**/*.jpg
inbox/**/*.jpeg

# 个人资源工作流副本
tools/workflows/assets/*
!tools/workflows/assets/.gitkeep

# 项目开发中的资源清单（本地测试分类，不入库）
manifest/hud_v8.yaml

.env
.env.*
"""

# manifest/ 中仅本地开发使用、不应进入公开仓库的文件
MANIFEST_LOCAL_ONLY = (
    "hud_v8.yaml",
)

# icons.yaml 中从此类行起视为「开发中分类」并裁剪（如 HudV8 测试图目录）
ICONS_DEV_SECTION_MARKERS = (
    re.compile(r"^#\s*HudV8\b", re.IGNORECASE),
    re.compile(r"^hud_v8_", re.IGNORECASE),
)

RSYNC_EXCLUDES = [
    ".git",
    ".DS_Store",
    "__pycache__",
    "*.pyc",
    "release/",           # artApp/release 打包产物
    ".build-venv/",       # artApp/.build-venv
    "source/",
    "inbox/",
    "workflows/assets/",  # tools/workflows/assets 个人工作流
    "pipeline_config.json",  # 本地主配置，不入库
    *MANIFEST_LOCAL_ONLY,  # 开发中 manifest，不入库
]

SANITIZE_DEFAULT_KEYS = (
    "deepseek_api_key",
    "project_root",
    "art_pipeline_root",
    "log_dir",
)

SK_PATTERN = re.compile(r"sk-[A-Za-z0-9]{8,}")
HOME_PATH_PATTERN = re.compile(r"(/Users/[^/\"\\]+|/home/[^/\"\\]+|C:\\\\Users\\\\[^\\\\\"]+)")


def sanitize_pipeline_config(data: dict) -> dict:
    """移除 API 密钥与个人绝对路径；保留分类/模板结构。"""
    out = json.loads(json.dumps(data))
    defaults = out.setdefault("defaults", {})
    for key in SANITIZE_DEFAULT_KEYS:
        if key in defaults:
            defaults[key] = ""
    # 兜底：扫描 defaults 内疑似密钥字符串
    for key, val in list(defaults.items()):
        if isinstance(val, str) and SK_PATTERN.search(val):
            defaults[key] = ""
    return _sanitize_values_recursive(out)


def _sanitize_values_recursive(obj: object) -> object:
    """递归清除字符串中的 sk-* 密钥与用户 home 绝对路径。"""
    if isinstance(obj, dict):
        return {k: _sanitize_values_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_values_recursive(v) for v in obj]
    if isinstance(obj, str):
        if SK_PATTERN.search(obj):
            return ""
        if HOME_PATH_PATTERN.search(obj):
            return ""
    return obj


def build_public_example_config(tools_dir: Path) -> dict:
    """生成可公开的 example 配置（通用模板，非个人本地副本）。"""
    tools_path = str(tools_dir.resolve())
    if tools_path not in sys.path:
        sys.path.insert(0, tools_path)
    try:
        from bootstrap_config import build_default_config

        return sanitize_pipeline_config(build_default_config())
    except Exception as exc:
        print(f"  ⚠ bootstrap_config 失败 ({exc})，回退为最小模板")
        return {
            "version": 1,
            "defaults": {
                "comfyui_url": "http://127.0.0.1:8188",
                "checkpoint": "animagineXL_v3.safetensors",
                "steps": 35,
                "cfg": 7.0,
                "sampler": "euler_ancestral",
                "scheduler": "normal",
                "deepseek_api_key": "",
                "deepseek_model": "deepseek-v4-flash",
                "project_root": "",
                "art_pipeline_root": "",
                "log_dir": "",
            },
            "categories": [],
            "assets": [],
        }


def write_public_example_config(dest: Path) -> None:
    example = dest / "tools" / "pipeline_config.example.json"
    data = build_public_example_config(dest / "tools")
    example.parent.mkdir(parents=True, exist_ok=True)
    example.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_sanitized_config(src: Path, dest: Path) -> None:
    if not src.is_file():
        return
    data = json.loads(src.read_text(encoding="utf-8"))
    clean = sanitize_pipeline_config(data)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def rsync_copy(src: Path, dest: Path, *, dry_run: bool) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    cmd = ["rsync", "-a", "--delete"]
    if dry_run:
        cmd.append("--dry-run")
    for pattern in RSYNC_EXCLUDES:
        cmd.extend(["--exclude", pattern])
    cmd.extend([f"{src}/", f"{dest}/"])
    print(">>>", " ".join(cmd))
    subprocess.run(cmd, check=True)


def sanitize_manifest(dest: Path, *, dry_run: bool) -> None:
    """移除项目开发中的 manifest（如 HudV8 测试图目录清单）。"""
    manifest_dir = dest / "manifest"
    if not manifest_dir.is_dir():
        return

    for name in MANIFEST_LOCAL_ONLY:
        path = manifest_dir / name
        if not path.exists():
            continue
        if dry_run:
            print(f"  [dry-run] 删除本地清单 manifest/{name}")
            continue
        path.unlink()
        print(f"  ✓ 已删除 manifest/{name}（项目开发清单，不入库）")

    icons = manifest_dir / "icons.yaml"
    if not icons.is_file():
        return
    lines = icons.read_text(encoding="utf-8").splitlines(keepends=True)
    cut = len(lines)
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if any(pat.match(stripped) for pat in ICONS_DEV_SECTION_MARKERS):
            cut = i
            break
    if cut >= len(lines):
        return
    if dry_run:
        print(f"  [dry-run] 裁剪 manifest/icons.yaml（自第 {cut + 1} 行起为开发分类）")
        return
    icons.write_text("".join(lines[:cut]).rstrip() + "\n", encoding="utf-8")
    print("  ✓ 已裁剪 manifest/icons.yaml（移除 HudV8 等开发中分类）")


def sync_screenshots(dest: Path, *, dry_run: bool) -> None:
    """从 artAppSite 复制功能截图到 docs/images/ 与 artApp/web/docs/images/。"""
    src_dir = ARTAPP_SITE / "assets" / "screenshots"
    if not src_dir.is_dir():
        src_dir = ARTAPP_SITE
    dest_dirs = [
        dest / "docs" / "images",
        dest / "artApp" / "web" / "docs" / "images",
    ]
    copied = 0
    for src_name, dest_name in SCREENSHOT_MAP.items():
        src = src_dir / src_name
        if not src.is_file():
            src = ARTAPP_SITE / src_name
        if not src.is_file():
            continue
        if dry_run:
            print(f"  [dry-run] 截图 {src_name} → docs/images/{dest_name}")
            copied += 1
            continue
        for dest_dir in dest_dirs:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest_dir / dest_name)
        copied += 1
    if copied:
        print(f"  ✓ 已同步 {copied} 张功能截图 → docs/images/ 与 artApp/web/docs/images/")
    elif not dry_run:
        print("  ⚠ 未找到 artAppSite 截图，跳过 docs/images/")


def post_process(dest: Path, *, dry_run: bool) -> None:
    cfg = dest / "tools" / "pipeline_config.json"
    example = dest / "tools" / "pipeline_config.example.json"
    if not dry_run:
        write_public_example_config(dest)
        print("  ✓ 已生成 tools/pipeline_config.example.json（通用 bootstrap 模板）")
        if cfg.is_file():
            cfg.unlink()
            print("  ✓ 已删除 tools/pipeline_config.json（本地配置不入库）")

    assets_dir = dest / "tools" / "workflows" / "assets"
    if not dry_run:
        if assets_dir.exists():
            shutil.rmtree(assets_dir)
        assets_dir.mkdir(parents=True, exist_ok=True)
        (assets_dir / ".gitkeep").write_text("", encoding="utf-8")

    stray_release = dest / "artApp" / "release"
    if stray_release.is_dir():
        shutil.rmtree(stray_release)
        print("  ✓ 已删除 artApp/release/")

    stray_site = dest / "artAppSite"
    if stray_site.exists():
        if stray_site.is_dir():
            shutil.rmtree(stray_site)
        else:
            stray_site.unlink()
        print("  ✓ 已删除 artAppSite/（官网单独发布，不入库）")

    (dest / ".gitignore").write_text(GITHUB_GITIGNORE, encoding="utf-8")
    print("  ✓ 已写入 .gitignore")

    sanitize_manifest(dest, dry_run=dry_run)
    sync_screenshots(dest, dry_run=dry_run)


def preserve_repo_meta(dest: Path, backup: Path) -> None:
    """暂存 Git 仓库元数据，避免 rsync --delete 误伤。"""
    backup.mkdir(parents=True, exist_ok=True)
    for name in (".git", "LICENSE", "README.md"):
        src = dest / name
        if src.exists():
            dst = backup / name
            if dst.exists():
                if dst.is_dir():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)


def restore_repo_meta(dest: Path, backup: Path) -> None:
    if not backup.is_dir():
        return
    for item in backup.iterdir():
        target = dest / item.name
        if item.name == "README.md":
            # 合并：保留仓库 README，追加 ArtPipeline README 作为参考
            pipeline_readme = dest / "README.md"
            if pipeline_readme.is_file() and item.is_file():
                merged = item.read_text(encoding="utf-8").rstrip()
                body = pipeline_readme.read_text(encoding="utf-8").strip()
                if body and body not in merged:
                    merged += "\n\n---\n\n<!-- ArtPipeline 项目说明 -->\n\n" + body
                target.write_text(merged + "\n", encoding="utf-8")
                continue
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)
    shutil.rmtree(backup)


def sync(*, dest: Path, dry_run: bool) -> None:
    if not PIPELINE_ROOT.is_dir():
        raise SystemExit(f"找不到 ArtPipeline 根目录: {PIPELINE_ROOT}")

    print(f"源: {PIPELINE_ROOT}")
    print(f"目标: {dest}")

    backup = dest / ".sync_backup_meta"
    if not dry_run:
        preserve_repo_meta(dest, backup)

    staging = dest / ".sync_staging"
    if staging.exists() and not dry_run:
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    for name in SYNC_TOP_LEVEL:
        src = PIPELINE_ROOT / name
        if not src.exists():
            print(f"  跳过（不存在）: {name}")
            continue
        dst = staging / name
        print(f"\n同步 {name} …")
        if src.is_dir():
            rsync_copy(src, dst, dry_run=dry_run)
        elif not dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        else:
            print(f"  复制文件 {name}")

    if dry_run:
        print("\n[dry-run] 未写入目标仓库")
        return

    # 将 staging 内容合并到 dest（保留 .git）
    for item in staging.iterdir():
        target = dest / item.name
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)
    shutil.rmtree(staging)

    post_process(dest, dry_run=False)
    restore_repo_meta(dest, backup)

    print(f"\n✓ 已同步到 {dest}")
    print("  请检查 git status，确认无密钥与本地路径后再 commit。")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync ArtPipeline to GitHub repo with sanitization")
    parser.add_argument(
        "--dest",
        type=Path,
        default=DEFAULT_DEST,
        help=f"Git 仓库路径（默认 {DEFAULT_DEST}）",
    )
    parser.add_argument("--dry-run", action="store_true", help="仅预览 rsync，不写入")
    args = parser.parse_args()

    dest = args.dest.expanduser().resolve()
    if not (dest / ".git").is_dir() and not args.dry_run:
        print(f"警告: {dest} 不是 Git 仓库（无 .git），仍将写入文件。", file=sys.stderr)

    sync(dest=dest, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
