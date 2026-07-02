#!/usr/bin/env python3
"""ArtPipeline Studio 打包公共逻辑（macOS / Windows 共用）。"""

from __future__ import annotations

import os
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

# GHA Windows 默认 cp1252；在任意 print 之前尽量启用 UTF-8
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def configure_stdio_utf8() -> None:
    """Windows 控制台默认 cp1252，尽量设为 UTF-8（GHA 上 reconfigure 可能无效）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def safe_print(*args: object, sep: str = " ", end: str = "\n", file=None) -> None:
    """CI/Windows 控制台可能为 cp1252，避免 UnicodeEncodeError。"""
    stream = file or sys.stdout
    text = sep.join(str(a) for a in args) + end
    buf = getattr(stream, "buffer", None)
    if buf is not None:
        try:
            buf.write(text.encode("utf-8", errors="replace"))
            buf.flush()
            return
        except (OSError, ValueError, AttributeError):
            pass
    try:
        stream.write(text)
        stream.flush()
    except UnicodeEncodeError:
        fallback = text.encode("ascii", errors="replace").decode("ascii")
        stream.write(fallback)
        stream.flush()


def die(msg: str, code: int = 1) -> None:
    """打印消息后退出（避免 SystemExit 中文信息在 cp1252 下再次编码失败）。"""
    safe_print(msg, file=sys.stderr)
    raise SystemExit(code)

configure_stdio_utf8()

ARTAPP_ROOT = Path(__file__).resolve().parent
BUILD_VENV = ARTAPP_ROOT / ".build-venv"
ART_PIPELINE_ROOT = ARTAPP_ROOT.parent
TOOLS_DIR = ART_PIPELINE_ROOT / "tools"
RELEASE_DIR = ARTAPP_ROOT / "release"
DIST_DIR = RELEASE_DIR / "dist"
BUILD_DIR = RELEASE_DIR / "build"
SPEC_DIR = RELEASE_DIR

APP_NAME = "ArtPipeline Studio"
MACOS_ZIP_NAME = "ArtPipeline-Studio-macOS.zip"
ENTRY = ARTAPP_ROOT / "run_app.py"
ICON = TOOLS_DIR / "assets" / "app_icon.png"
if not ICON.is_file():
    ICON = ARTAPP_ROOT / "web" / "assets" / "app_icon.png"


def venv_python_path() -> Path:
    if sys.platform == "win32":
        return BUILD_VENV / "Scripts" / "python.exe"
    return BUILD_VENV / "bin" / "python"


def venv_pip_path() -> Path:
    if sys.platform == "win32":
        return BUILD_VENV / "Scripts" / "pip.exe"
    return BUILD_VENV / "bin" / "pip"


def build_python() -> str:
    """优先使用独立 venv，避免系统 Python 架构/依赖混用。"""
    py = venv_python_path()
    if py.is_file():
        return str(py)
    return sys.executable


def ensure_build_venv(*, recreate: bool = False) -> Path:
    """创建/更新 .build-venv 并安装打包依赖。"""
    if recreate and BUILD_VENV.exists():
        shutil.rmtree(BUILD_VENV)
    if not venv_python_path().is_file():
        safe_print(">>> Creating build venv .build-venv ...")
        subprocess.run([sys.executable, "-m", "venv", str(BUILD_VENV)], check=True)
    pip = venv_pip_path()
    subprocess.run([str(pip), "install", "-U", "pip", "wheel"], check=True)
    subprocess.run(
        [str(pip), "install", "-r", "requirements.txt", "-r", "requirements-build.txt"],
        cwd=str(ARTAPP_ROOT),
        check=True,
    )
    return BUILD_VENV


def _sep() -> str:
    return ";" if sys.platform == "win32" else ":"


def _add_data(src: Path, dest: str) -> str:
    return f"{src}{_sep()}{dest}"


def _ensure_pyinstaller(python: str | None = None) -> None:
    """确认 PyInstaller 可用（优先检查构建 venv 的 Python）。"""
    py = python or build_python()
    result = subprocess.run(
        [py, "-c", "import PyInstaller"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        die(
            "PyInstaller not found. Install with:\n"
            f"  {py} -m pip install -r requirements-build.txt\n"
            "Or run: python build_release_win.py --setup-venv"
        )


def hidden_imports() -> list[str]:
    return [
        "backend.main",
        "backend.routes",
        "backend.deps",
        "backend.runtime_paths",
        "backend.services.preview",
        "backend.services.pipeline_runner",
        "backend.services.log_bus",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "multipart",
        "PIL",
        "PIL.Image",
        "config_manager",
        "pipeline_core",
        "comfyui_client",
        "workflow_engine",
        "workflow_presets",
        "ai_assistant",
        "alpha_matte",
        "bootstrap_config",
        "postprocess.engine",
        "postprocess.models",
        "postprocess.templates",
        "postprocess.fonts",
        "cloud.registry",
        "cloud.verify",
        "cloud.http_util",
        "cloud.runner",
        "cloud.generator",
        "cloud.base",
        "cloud.tencent_maas",
        "cloud.tencent_sign",
        "cloud.providers.stability",
        "cloud.providers.dashscope",
        "cloud.providers.tencent",
        "cloud.providers.volcengine",
    ]


def pyinstaller_cmd(*, python: str, target: str) -> list[str]:
    """组装 PyInstaller 命令。target: mac | win"""
    datas = [
        _add_data(ARTAPP_ROOT / "web", "web"),
        _add_data(ARTAPP_ROOT / "backend", "backend"),
        _add_data(TOOLS_DIR, "tools"),
    ]

    cmd = [
        python,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        f"--name={APP_NAME}",
        f"--distpath={DIST_DIR}",
        f"--workpath={BUILD_DIR}",
        f"--specpath={SPEC_DIR}",
        "--windowed",
        "--collect-all=webview",
        f"--paths={TOOLS_DIR}",
        f"--paths={ARTAPP_ROOT}",
    ]

    if ICON.is_file():
        cmd.append(f"--icon={ICON}")

    for data in datas:
        cmd.extend(["--add-data", data])

    for mod in hidden_imports():
        cmd.extend(["--hidden-import", mod])

    cmd.append(str(ENTRY))

    # macOS 由 PyInstaller 自动生成 .app；Windows 生成目录 + .exe
    # UPX 在 macOS arm64 / 部分杀软环境下会导致 .app 白屏或崩溃
    cmd.insert(cmd.index("--windowed") + 1, "--noupx")
    if target == "mac":
        cmd.extend(["--osx-bundle-identifier=cn.vrast.artpipeline.studio"])

    return cmd


def _copytree_preserve_links(src: Path, dest: Path) -> None:
    """复制 .app 时保留符号链接（PyInstaller 6 依赖）。"""
    shutil.copytree(src, dest, symlinks=True)


def patch_macos_info_plist(app_path: Path) -> None:
    """
    写入 ATS，允许 WKWebView 加载本地 http://127.0.0.1。
    未配置时 pywebview 窗口可能只有深色背景（黑屏），内容区未挂上 WebView。
    """
    plist_path = app_path / "Contents" / "Info.plist"
    if not plist_path.is_file():
        return
    with plist_path.open("rb") as handle:
        plist = plistlib.load(handle)
    plist["NSAppTransportSecurity"] = {
        "NSAllowsArbitraryLoads": True,
        "NSAllowsLocalNetworking": True,
    }
    plist.setdefault("LSMinimumSystemVersion", "11.0")
    plist.setdefault("NSHighResolutionCapable", True)
    with plist_path.open("wb") as handle:
        plistlib.dump(plist, handle)


def verify_macos_app_bundle(app_path: Path) -> None:
    """打包后校验 .app 结构，避免 CI artifact 上传后用户拿到残缺包。"""
    patch_macos_info_plist(app_path)
    exe = app_path / "Contents" / "MacOS" / APP_NAME
    if not exe.is_file():
        die(f"macOS .app missing executable: {exe}")
    macos_dir = app_path / "Contents" / "MacOS"
    internal = macos_dir / "_internal"
    frameworks = app_path / "Contents" / "Frameworks"
    has_python = any(
        p.is_file() or p.is_symlink()
        for p in [
            macos_dir / "Python",
            internal / "Python",
        ]
        if p.exists()
    )
    if not has_python and frameworks.is_dir():
        has_python = any(frameworks.rglob("Python*"))
    if not has_python and internal.is_dir():
        has_python = any(internal.iterdir())
    if not has_python:
        die(
            "macOS .app missing Python runtime (_internal / Frameworks).\n"
            f"Checked: {macos_dir}, {internal}, {frameworks}"
        )


def adhoc_sign_macos_app(app_path: Path) -> None:
    if sys.platform != "darwin":
        return
    subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(app_path)],
        check=True,
    )


def package_macos_app_zip(app_path: Path) -> Path:
    """
    用 ditto 打 zip（保留符号链接）。
    GitHub upload-artifact 直接上传 .app 会破坏 symlink，导致 Python 库丢失。
    """
    verify_macos_app_bundle(app_path)
    adhoc_sign_macos_app(app_path)
    zip_path = RELEASE_DIR / MACOS_ZIP_NAME
    if zip_path.exists():
        zip_path.unlink()
    subprocess.run(
        [
            "ditto",
            "-c",
            "-k",
            "--sequesterRsrc",
            "--keepParent",
            str(app_path),
            str(zip_path),
        ],
        check=True,
    )
    safe_print(f"Packaged {zip_path} ({zip_path.stat().st_size // (1024 * 1024)} MB)")
    return zip_path


def build_pyinstaller(*, clean: bool = True, python: str | None = None, target: str) -> Path:
    py = python or build_python()
    _ensure_pyinstaller(py)
    if not TOOLS_DIR.is_dir():
        die(f"Missing tools directory: {TOOLS_DIR}")
    if not ENTRY.is_file():
        die(f"Missing entry script: {ENTRY}")

    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    if clean:
        for d in (DIST_DIR, BUILD_DIR):
            if d.exists():
                shutil.rmtree(d)

    cmd = pyinstaller_cmd(python=py, target=target)
    safe_print(">>>", " ".join(cmd))
    subprocess.run(cmd, cwd=str(ARTAPP_ROOT), check=True)

    artifact = collect_artifact(target=target)
    if target == "mac":
        package_macos_app_zip(artifact)
    write_release_readme(artifact, portable=False, target=target)
    return artifact


def collect_artifact(*, target: str) -> Path:
    """将 dist 产物复制/提升到 release/ 根下便于分发。"""
    if target == "mac":
        src = DIST_DIR / f"{APP_NAME}.app"
        dest = RELEASE_DIR / f"{APP_NAME}.app"
        if dest.exists():
            shutil.rmtree(dest)
        if src.is_dir():
            _copytree_preserve_links(src, dest)
            return dest
    elif target == "win":
        src = DIST_DIR / APP_NAME
        dest = RELEASE_DIR / APP_NAME
        if dest.exists():
            shutil.rmtree(dest)
        if src.is_dir():
            shutil.copytree(src, dest)
            return dest

    candidates = list(DIST_DIR.iterdir()) if DIST_DIR.is_dir() else []
    if not candidates:
        die(f"PyInstaller output empty: {DIST_DIR}")
    only = candidates[0]
    dest = RELEASE_DIR / only.name
    if dest.exists():
        if dest.is_dir():
            shutil.rmtree(dest)
        else:
            dest.unlink()
    if only.is_dir():
        _copytree_preserve_links(only, dest)
    else:
        shutil.copy2(only, dest)
    return dest


def _copy_bundle_sources(bundle_artapp: Path, bundle_tools: Path) -> None:
    for name in ("backend", "web", "run_app.py", "requirements.txt"):
        src = ARTAPP_ROOT / name
        dest = bundle_artapp / name
        if src.is_dir():
            shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elif src.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)

    if bundle_tools.exists():
        shutil.rmtree(bundle_tools)
    shutil.copytree(
        TOOLS_DIR,
        bundle_tools,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
    )


def build_portable_mac() -> Path:
    """无需 PyInstaller：组装 macOS .app + 内嵌 artApp/tools 源码包。"""
    app_name = f"{APP_NAME}.app"
    app_path = RELEASE_DIR / app_name
    contents = app_path / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources" / "bundle"
    bundle_artapp = resources / "artApp"
    bundle_tools = resources / "tools"

    if app_path.exists():
        shutil.rmtree(app_path)

    macos.mkdir(parents=True)
    bundle_artapp.mkdir(parents=True)
    bundle_tools.mkdir(parents=True)

    _copy_bundle_sources(bundle_artapp, bundle_tools)

    launcher = macos / APP_NAME
    launcher.write_text(
        f"""#!/bin/bash
set -euo pipefail
APP_ROOT="$(cd "$(dirname "$0")/../Resources/bundle/artApp" && pwd)"
BUNDLE_ROOT="$(cd "$(dirname "$0")/../Resources/bundle" && pwd)"
export PYTHONPATH="$APP_ROOT"
cd "$APP_ROOT"
export ARTPIPELINE_BUNDLE_ROOT="$BUNDLE_ROOT"
exec python3 run_app.py "$@"
""",
        encoding="utf-8",
    )
    launcher.chmod(0o755)

    info = contents / "Info.plist"
    info.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key><string>zh_CN</string>
  <key>CFBundleExecutable</key><string>{APP_NAME}</string>
  <key>CFBundleIdentifier</key><string>cn.vrast.artpipeline.studio</string>
  <key>CFBundleName</key><string>{APP_NAME}</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>2.0.0</string>
  <key>CFBundleVersion</key><string>2.0.0</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key>
  <dict>
    <key>NSAllowsArbitraryLoads</key><true/>
    <key>NSAllowsLocalNetworking</key><true/>
  </dict>
</dict>
</plist>
""",
        encoding="utf-8",
    )

    if ICON.is_file():
        icon_dest = contents / "Resources" / "app_icon.png"
        icon_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ICON, icon_dest)

    write_release_readme(app_path, portable=True, target="mac")
    safe_print("Portable .app ready (requires system Python 3 + pip deps)")
    return app_path


def build_portable_win() -> Path:
    """无需 PyInstaller：组装 Windows 便携目录 + 启动 bat。"""
    dest = RELEASE_DIR / APP_NAME
    bundle_root = dest / "bundle"
    bundle_artapp = bundle_root / "artApp"
    bundle_tools = bundle_root / "tools"

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    bundle_artapp.mkdir(parents=True)
    bundle_tools.mkdir(parents=True)

    _copy_bundle_sources(bundle_artapp, bundle_tools)

    launcher = dest / f"{APP_NAME}.bat"
    launcher.write_text(
        f"""@echo off
setlocal EnableExtensions
set "BUNDLE_ROOT=%~dp0bundle"
set "APP_ROOT=%BUNDLE_ROOT%\\artApp"
set "PYTHONPATH=%APP_ROOT%"
set "ARTPIPELINE_BUNDLE_ROOT=%BUNDLE_ROOT%"
cd /d "%APP_ROOT%"
where python >nul 2>nul
if errorlevel 1 (
  echo [ArtPipeline Studio] 未找到 python，请先安装 Python 3 并加入 PATH。
  pause
  exit /b 1
)
python run_app.py %*
""",
        encoding="utf-8",
    )

    if ICON.is_file():
        shutil.copy2(ICON, dest / "app_icon.png")

    write_release_readme(dest, portable=True, target="win")
    safe_print("Portable folder ready (requires system Python 3 + pip deps)")
    return dest


def write_release_readme(artifact: Path, *, portable: bool, target: str) -> None:
    readme = RELEASE_DIR / "README.md"
    system = platform.system()

    if target == "mac" and (artifact.suffix == ".app" or artifact.name.endswith(".app")):
        run_hint = f"解压 `{MACOS_ZIP_NAME}` 后双击 `{artifact.name}` 启动（勿直接运行 Contents/MacOS 内可执行文件）"
        config_hint = """```
~/Library/Application Support/ArtPipeline Studio/
├── pipeline_config.json
└── workflows/
```"""
        rebuild_cmd = "python3 build_release_mac.py"
    elif target == "win":
        exe = artifact / f"{APP_NAME}.exe"
        bat = artifact / f"{APP_NAME}.bat"
        if exe.is_file():
            run_hint = f"运行 `{artifact.name}/{APP_NAME}.exe`"
        elif bat.is_file():
            run_hint = f"双击 `{artifact.name}/{APP_NAME}.bat` 启动"
        else:
            run_hint = f"运行 `{artifact.name}` 内可执行文件"
        config_hint = """```
%LOCALAPPDATA%\\ArtPipeline Studio\\
├── pipeline_config.json
└── workflows/
```"""
        rebuild_cmd = "python build_release_win.py"
    else:
        run_hint = f"运行 `{artifact}` 内可执行文件"
        config_hint = "见应用内「全局设置」说明"
        rebuild_cmd = "python build_release.py"

    portable_note = ""
    if portable:
        if target == "mac":
            portable_note = """
> **便携版说明**：内嵌 artApp + tools 源码，启动时调用系统 `python3`。
> 首次使用前请安装依赖（路径按实际 .app 位置调整）：
> ```bash
> pip install -r "ArtPipeline Studio.app/Contents/Resources/bundle/artApp/requirements.txt"
> ```
"""
        else:
            portable_note = """
> **便携版说明**：内嵌 artApp + tools 源码，启动时调用系统 `python`。
> 首次使用前请安装依赖：
> ```bat
> pip install -r "ArtPipeline Studio\\bundle\\artApp\\requirements.txt"
> ```
"""
    else:
        portable_note = """
> **独立版**：已内嵌 Python 运行时，**无需**单独安装 Python 或 pip 依赖。

"""

    readme.write_text(
        f"""# {APP_NAME} · 发布包

构建平台: {system} {platform.machine()} · 目标: {target}
{portable_note}
## 启动

{run_hint}

首次启动会在用户目录创建配置（示例）:

{config_hint}

请在应用内「全局设置」中确认 **ArtPipeline 根** 与 **游戏引擎项目根** 指向你的实际工程路径。

## 开发模式

```bash
cd ArtPipeline/artApp
python run_dev.py      # 浏览器
python run_app.py        # 桌面壳（未打包）
```

## 重新打包

```bash
cd ArtPipeline/artApp
{rebuild_cmd}              # PyInstaller 独立版（推荐）
{rebuild_cmd} --setup-venv # 仅重建虚拟环境
{rebuild_cmd} --portable   # 备用：需系统 Python 的便携版
```

中间文件: `release/build/`、`release/dist/`、`release/*.spec`、`.build-venv/`
""",
        encoding="utf-8",
    )
    safe_print(f"Wrote {readme}")
