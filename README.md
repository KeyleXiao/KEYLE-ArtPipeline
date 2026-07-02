# KEYLE· ArtPipeline Studio

[![Build Release](https://github.com/KeyleXiao/ArtPipeline-Studio-/actions/workflows/build-release.yml/badge.svg?branch=main)](https://github.com/KeyleXiao/ArtPipeline-Studio-/actions/workflows/build-release.yml)
[MIT License](LICENSE) · [Desktop Latest Release](https://github.com/KeyleXiao/ArtPipeline-Studio-/releases/latest)

> 产品主页：[art.vrast.cn](https://art.vrast.cn) · 使用文档：[art.vrast.cn/docs.html](https://art.vrast.cn/docs.html)

---

## 这是什么？

**ArtPipeline Studio** 是一款面向 Unity 等游戏引擎的 **AI 美术资源生产与管理工具**。

你用 ComfyUI 或云端 API 批量出图之后，往往会遇到这些问题：几百张 PNG 散落在文件夹里、prompt 和工作流对不上文件名、不知道哪张已经进引擎、改后处理又怕覆盖 AI 原图。Studio 把 **生成 → 保管 → 编辑 → 导出** 串成一条可追踪的流水线，让每一张图标、头像、道具都有配置、有状态、有去向。

```
ComfyUI 或云端 API 生图 → source 原图（只读母版）→ inbox 后处理 → 游戏引擎目录
```

<p align="center">
  <img src="docs/images/main-workbench.png" alt="主工作台：分类、资源列表、预览与 AI 助手" width="920" />
</p>

<p align="center"><sub>主工作台 — 分类 · 资源 · S·in·U 状态 · AI 助手 · ComfyUI / 云模型页签</sub></p>

---

## 能做到什么程度？

| 阶段 | Studio 帮你做什么 |
|------|-------------------|
| **策划与配置** | 按分类（角色 / 道具 / UI…）管理资源清单；每张图独立保存 prompt、Checkpoint、工作流 JSON、尺寸与说明 |
| **AI 生图** | **ComfyUI 本地**（工作流、LoRA、四段 SDXL Prompt、内置模板）或 **云端 API**（Stability / 万相 / 混元 / 即梦，无需 GPU）；支持 **文生图 / 图生图 / 图像编辑** 三种模式 |
| **质量与效率** | 分类 / 本类 / 多选批量生成；生成时可选 **背景剔除**；右键 **去除洋红 / 自适应裁切**；DeepSeek **AI 助手** 写提示词与工作流 |
| **后处理** | PS 式图层栈：矩形 / 矩阵 / 自由裁切、橡皮擦抠图、品红去色、混色、智能拆分图集、撤销重做 — 只改 **inbox**，不破坏 **source** |
| **进引擎** | 单张 / 本类 / 生成并导出；**S·in·U** 三态块实时告诉你：原图有没有、inbox 有没有后处理、引擎目录是否过期 |

**运行形态**：Web 开发版（浏览器 `127.0.0.1:8765`）+ macOS / Windows **桌面独立版**（内嵌 Python，GitHub Actions 自动构建）。界面 **中 / EN** 双语。

**不做什么**：Studio 不是 ComfyUI 本体（需自行安装 ComfyUI 或使用纯云端）；也不是通用 DCC，专注 **2D 游戏 UI / 图标类资源** 的批量生产与入库。

---

## 为什么要用它管理 AI 生成资源？

直接用 ComfyUI 或云 API 出图，通常只能得到「文件夹 + 散图」。项目规模稍大就会出现：

- **对不上号**：`icon_sword.png` 对应哪条 prompt、哪个 workflow、哪次 seed？
- **阶段混乱**：后处理、裁切、加框改的是母版还是交付版？改错了无法回退。
- **不知道进度**：100 张图标哪些已生成、哪些已抠底、哪些已进 Unity？
- **批量痛苦**：换模型、换分类前缀、整类重跑，缺少统一入口。

Studio 的设计思路是 **分阶段、可追踪、可批量**：

1. **source 只读** — AI 原图写入 `source/`，后处理只在 `inbox/` 进行；随时「从 source 还原」。
2. **一图一配置** — 文件名、分类、Checkpoint、四段 prompt / 云 prompt、工作流 JSON 全部落在 `pipeline_config.json`，可搜索、可复制、可 AI 辅助填写。
3. **S·in·U 三态** — 列表上一眼看到 source / inbox / 引擎 是否就绪、是否过期（见下表）。
4. **分类即产线** — 每个分类独立路径、默认模型、通用 prompt 前缀；头像、道具、技能各走各的模板，仍可单张 override。
5. **本地 + 云端混用** — 分类 A 用 ComfyUI，分类 B 用万相；无 GPU 也能跑云端批量。

| 块 | 绿 | 黄 | 红 / 灰 |
|----|----|----|---------|
| **S** source | 已生成 | — | 尚未生成 |
| **in** inbox | 与 source 一致 | 已后处理 | 缺失 |
| **U** 引擎 | 与 inbox 一致 | — | 需重新导出 |

> 适合：**独立开发者或小团队** 用 AI 持续产出游戏 UI 资源，并希望「出图—精修—进引擎」有章可循，而不是每次手动复制粘贴。

---

## 功能一览

| 能力 | 说明 |
|------|------|
| 分类与资源库 | 多分类、搜索、**多选批量**、复制资源；**S·in·U** 三态路径追踪；长按跨分类迁移 |
| **ComfyUI + 云端** | 分类 / 资源两级 Checkpoint；本地模型 + `cloud:` 云端模型可混用 |
| **云端生图** | Stability · 万相 · 混元（TokenHub）· 即梦；测试连接 + 可配置并发 |
| 三种生成模式 | **文生图 / 图生图 / 图像编辑**（ComfyUI 与云端统一命名） |
| ComfyUI 工作流 | 四段 SDXL Prompt（前缀 / 主体 / 场景 / 光影）；内置模板（单次 / 双通道 / GII / LoRA 等） |
| 批量生图 | 生成选中 / 本类 / 生成并导出；浮动任务队列；右键单张生图（无需「启用」） |
| 背景剔除 | 分类默认抠底 + 单资源覆盖；右键 **去除洋红 / 自适应裁切** |
| **导入外部资源** | 多选 PNG/JPG/WebP；标准导入或 **重绘导入**（仅写 inbox） |
| 后处理编辑器 | 图层栈、矩阵 / 自由裁切、抠图、品红去色、混色、**智能拆分**、撤销重做 |
| AI 助手 | 自由对话 / 写提示词 / 优化 / 工作流 / 写基本信息 / 分类设置（DeepSeek） |
| 运行日志 | 抽屉实时 SSE；可配置目录，写入 `studio.log` |
| 预览面板 | 缩放、inbox / source / 引擎切换、文件信息与最近动态 |
| 中 / EN | 界面双语 |

### 云端支持的模型（Checkpoint 下拉）

| 区域 | Provider | Studio 示例 |
|------|----------|-------------|
| 海外 | Stability AI | `cloud:stability/core` · `cloud:stability/sd3` |
| 国内 | 阿里云万相 | `cloud:dashscope/wan2.6-t2i` |
| 国内 | 腾讯混元 | `cloud:tencent/hunyuan-3.0`（TokenHub API Key） |
| 国内 | 火山即梦 | `cloud:volcengine/seedream-4` |

详见 [docs/cloud-generation.md](docs/cloud-generation.md) 与 [在线文档 · 云端 API](https://art.vrast.cn/docs.html#云端-api-生图)。

---

## 界面截图

### 主工作台

分类与资源库、AI 助手、inbox 预览与 **S·in·U** 三态。按 checkpoint 自动切换 ComfyUI / 云模型页签。

<p align="center">
  <img src="docs/images/main-workbench.png" alt="主界面" width="880" />
</p>

### 提示词与工作流 / 云 prompt

ComfyUI：subject、正/负向、SDXL **G / L** 分层、workflow JSON。云端：单段 prompt + 文生图 / 图生图 / 图像编辑。

<p align="center">
  <img src="docs/images/prompts-workflow.png" alt="提示词与工作流" width="880" />
</p>

### 三种生成模式 / 图生图

文生图、图生图、图像编辑 — ComfyUI 与云端统一命名。基于 inbox / source 继续迭代，适合头像与道具微调。

<p align="center">
  <img src="docs/images/img2img-redraw.png" alt="三种生成模式" width="880" />
</p>

### 后处理编辑器

PS 式图层栈：裁切 / 抠图 / 混色 / 智能拆分；仅修改 inbox，source 只读。

<p align="center">
  <img src="docs/images/postprocess-editor.png" alt="后处理编辑器" width="880" />
</p>

> 更多界面与功能截图见 [产品官网](https://art.vrast.cn#showcase) · [图集](https://art.vrast.cn#gallery)。

### 上手说明（图文）

<p align="center"><sub>新建分类 · 分类菜单 · 手动/导入资源 · 资源右键 · 生图配置 · 出图 · 运行日志</sub></p>

| | | |
|:---:|:---:|:---:|
| <img src="docs/images/create-category.png" width="280" alt="新建分类" /><br />新建分类 | <img src="docs/images/category-context-menu.png" width="280" alt="分类菜单" /><br />分类右键 | <img src="docs/images/create-asset-manual.png" width="280" alt="手动新建" /><br />手动新建 |
| <img src="docs/images/import-external-assets.png" width="280" alt="导入外部" /><br />导入外部 | <img src="docs/images/asset-context-menu.png" width="280" alt="资源右键" /><br />资源右键 | <img src="docs/images/comfyui-checkpoint-setup.png" width="280" alt="生图配置" /><br />ComfyUI / 云端配置 |
| <img src="docs/images/prompt-generate.png" width="280" alt="提示词出图" /><br />提示词出图 | <img src="docs/images/runtime-logs.png" width="280" alt="运行日志" /><br />运行日志 | |

官网 [#guide](https://art.vrast.cn/index.html#guide) 与 [在线文档](https://art.vrast.cn/docs.html) 含 ComfyUI 安装指引与各平台 API 配置说明。

---

## 快速开始

### 环境要求

| 组件 | 说明 |
|------|------|
| Python 3.10+ | Web / 开发模式 |
| **ComfyUI**（可选） | 本地生图；[官方下载](https://www.comfy.org/download) · [文档](https://docs.comfy.org/) |
| **云端 API**（可选） | Stability / 万相 / 混元 / 即梦；至少配置一种生图方式即可 |
| 游戏项目 | 通常为 Unity；导出路径在「全局设置」配置 |

### 1. 安装依赖

```bash
git clone git@github.com:KeyleXiao/ArtPipeline-Studio-.git
cd ArtPipeline-Studio-
cd artApp
pip install -r requirements.txt
```

### 2. 初始化配置

```bash
cp ../tools/pipeline_config.example.json ../tools/pipeline_config.json
```

在应用 **「全局设置」** 中填写：

| 配置项 | 用途 |
|--------|------|
| **ComfyUI URL** | 本地生图（默认 `http://127.0.0.1:8188`） |
| **云端生图 API Key** | Stability / DashScope / 混元 / 即梦（各平台可单独测试连接） |
| **ArtPipeline 根目录** | 含 `source/`、`inbox/`、`workflows/` |
| **游戏项目根目录** | Unity 等导出目标 |
| **DeepSeek API Key** | AI 助手（与生图 API 独立） |
| **运行日志目录** | 可选；默认写入 `studio.log` |

在 **「分类设置」** 选择 **Checkpoint**（本地或 `cloud:` 模型）；单资源可在 **「基本信息」** 覆盖。

> `pipeline_config.json` 含个人路径与密钥，**勿提交 Git**（已在 `.gitignore`）。

目录约定见 [docs/目录说明.md](docs/目录说明.md)；云生图见 [docs/cloud-generation.md](docs/cloud-generation.md)。

### 3. 启动 Web 版

```bash
python run_dev.py
```

浏览器打开 **http://127.0.0.1:8765**。

### 4. 推荐工作流

1. 配置分类通用 prompt + 各资源 subject（ComfyUI 四段或云端单段）
2. ComfyUI 在线 **或** 云端 Key 测试通过 → **生成本类**
3. 需精修进 **后处理**
4. **导出本类** 或 **生成并导出**
5. 在引擎内验证资源

完整操作说明：[art.vrast.cn/docs.html](https://art.vrast.cn/docs.html) · 仓库内 [artApp/web/docs/zh-CN.md](artApp/web/docs/zh-CN.md)

---

## 仓库结构

```
ArtPipeline-Studio/
├── artApp/                 # Web / 桌面壳（FastAPI + 前端）
│   ├── run_dev.py          # 浏览器开发入口
│   ├── run_app.py          # 桌面 pywebview 入口
│   ├── build_release_*.py  # macOS / Windows 打包
│   └── web/                # 静态 UI、应用内文档
├── tools/                  # 配置、ComfyUI 客户端、云生图、后处理
│   ├── pipeline_config.example.json
│   ├── cloud/              # 云 provider 注册表与实现
│   └── workflows/          # ComfyUI 工作流模板
├── docs/                   # 规范与说明
│   ├── cloud-generation.md # 云生图说明
│   ├── 目录说明.md
│   └── images/             # README 用截图
├── comfyui/                # ComfyUI 工作流参考
├── manifest/               # 资源清单
└── overlays/               # 后处理叠加素材
```

运行时工作目录（需自行创建）：

```
your-art-workspace/
├── source/                 # AI 原图（ComfyUI 或云端；只读）
├── inbox/                  # 后处理与合成输出
└── workflows/              # 各资源 ComfyUI workflow JSON
```

---

## 桌面独立版

| 平台 | 命令 | 产物 |
|------|------|------|
| macOS | `python3 build_release_mac.py` | `artApp/release/ArtPipeline Studio.app` |
| Windows | `python build_release_win.py` | `artApp/release/ArtPipeline Studio/` |

首次启动配置：`~/Library/Application Support/ArtPipeline Studio/`（macOS）或 `%LOCALAPPDATA%\ArtPipeline Studio\`（Windows）。

### 下载（Latest Release）

| 平台 | 直接下载 |
|------|----------|
| **macOS** | [ArtPipeline-Studio-macOS.zip](https://github.com/KeyleXiao/ArtPipeline-Studio-/releases/latest/download/ArtPipeline-Studio-macOS.zip) |
| **Windows** | [ArtPipeline-Studio-Windows.zip](https://github.com/KeyleXiao/ArtPipeline-Studio-/releases/latest/download/ArtPipeline-Studio-Windows.zip) |

[查看所有版本 · Releases](https://github.com/KeyleXiao/ArtPipeline-Studio-/releases/latest)

push 到 `main` 且 CI 成功后自动更新 **Latest Release**（tag `latest-desktop`）。

**macOS 提示**：解压后双击 `.app`；若被拦截请 **右键 → 打开**。勿在终端直接运行 zip 内 `Contents/MacOS/...`。

---

## 推荐模型（ComfyUI 本地）

| 用途 | Checkpoint | 说明 |
|------|------------|------|
| 角色、道具、UI 图标 | **animagineXL_v3.safetensors** | 二次元卡牌风 |

云端模型见 Checkpoint 下拉「云端 · 国外 / 国内」分组。详见 [docs/animagine-xl.md](docs/animagine-xl.md)。

---

## 文档

| 文档 | 位置 |
|------|------|
| 官网与截图 | [art.vrast.cn](https://art.vrast.cn) |
| 在线使用文档（ComfyUI / 云 API 配置） | [art.vrast.cn/docs.html](https://art.vrast.cn/docs.html) |
| 云生图说明 | [docs/cloud-generation.md](docs/cloud-generation.md) |
| 应用内 Markdown | [artApp/web/docs/zh-CN.md](artApp/web/docs/zh-CN.md) |
| 近期更新 | [docs/更新日志.md](docs/更新日志.md) |
| artApp 开发 | [artApp/README.md](artApp/README.md) |
| 工具与 CLI | [tools/README.md](tools/README.md) |
| 目录规范 | [docs/目录说明.md](docs/目录说明.md) |

---

## 旧版 Tk / 命令行

仍可使用 Tk 界面或 CLI（与 Web 共用 `pipeline_config.json`）：

```bash
python tools/artTool_ui.py
python tools/cli.py --list
```

Web 版已覆盖主流程；Tk 可作为边缘功能回退。

---

## 参与与同步（维护者）

从本地完整工程同步到本仓库（脱敏密钥、排除 `release/` 与美术 PNG）：

```bash
cd /path/to/ArtPipeline/artApp
python3 sync_to_github.py --dest ~/ArtPipeline-Studio
```

---

## License

见 [LICENSE](LICENSE)。

---

**KEYLE · ArtPipeline Studio** — 从 AI 出图到引擎资源，一条流水线

联系：[keyle_xiao@hotmail.com](mailto:keyle_xiao@hotmail.com)

---

<!-- 以下为 ArtPipeline 工作区目录说明（与 Studio 配套使用） -->

# 附录：美术资源工作区（ArtPipeline 目录）

项目根目录下的 **AI 美术工作区**：生成原图、待入库文件、文档与 ComfyUI 工作流集中放这里，再部署到 `Assets/Resources/`。

## 目录一览

```
ArtPipeline/
├── README.md                 # 本文件
├── docs/                     # 规范与操作说明
├── manifest/                 # 资源清单（文件名、尺寸、prompt）
├── source/                   # ComfyUI 原始输出（按分类，可多版本）
│   ├── roles/
│   ├── items/
│   └── ui/
├── inbox/                    # 选定待入库（文件名必须与 Unity 一致）
│   ├── roles/
│   ├── items/
│   └── ui/
└── comfyui/                  # ComfyUI API 工作流
    └── workflows/
```

## 推荐模型

| 用途 | Checkpoint | 说明 |
|------|------------|------|
| 角色头像、道具插画 | **animagineXL_v3.safetensors** | 二次元卡牌风，与本项目 HUD 气质接近 |

详见 [docs/animagine-xl.md](docs/animagine-xl.md)。

## 一键生成（脚本 / GUI）

**推荐 GUI：**

```bash
python3 ArtPipeline/tools/artTool_ui.py
```

**命令行：**

```bash
python3 ArtPipeline/tools/cli.py --list
python3 ArtPipeline/tools/cli.py --category roles --to-inbox --deploy
# 旧路径仍可用
python3 Assets/Scripts/Tools/generate_icons_comfyui.py --kind role --to-inbox --deploy
```

配置与维护见 [tools/README.md](tools/README.md)、[tools/维护指南.md](tools/维护指南.md)。

## 工作流（ComfyUI → Unity）

1. 在 ComfyUI 用 **animagineXL_v3** 按清单生成，保存到 `source/<分类>/`（可保留多版 `role_warrior_v2.png` 等）。
2. 满意的一张 **复制/重命名** 为清单中的正式文件名，放入 `inbox/<分类>/`。
3. 入库 Unity Resources（直接复制，无裁切/抠图后处理）：

```bash
python3 Assets/Scripts/Tools/generate_icons_comfyui.py --kind role --to-inbox --deploy
# 或生成全部并入库
python3 Assets/Scripts/Tools/generate_icons_comfyui.py --all --to-inbox --deploy
```

旧版后处理脚本（裁切、去背景等）已移至项目根 `DeprecatedScripts/`。

## 与代码的对应关系

| inbox 路径 | Unity 目标 | 加载 |
|------------|------------|------|
| `inbox/roles/role_*.png` | `Assets/Resources/UI/Icons/Roles/` | `GameUiIconResources.GetRoleSprite` |
| `inbox/items/item_*.png` | `Assets/Resources/UI/Icons/Items/` | `GetItemSprite` |
| `inbox/ui/hp_heart_*.png` 等 | `Assets/Resources/UI/Icons/UI/` | `HpHeartFull` 等 |

## 关联文档

- [docs/目录说明.md](docs/目录说明.md)
- [docs/animagine-xl.md](docs/animagine-xl.md)
- [docs/cloud-generation.md](docs/cloud-generation.md) — 云生图（Stability / 万相 / 混元 / 即梦）
- `Assets/Scripts/表现优化文档.md` §7–§9（美术方向与分辨率）
- `Assets/Resources/UI/Icons/README.md`（Unity 导入设置）

---

<!-- ArtPipeline 项目说明 -->

# 美术资源流水线（ArtPipeline）

项目根目录下的 **AI 美术工作区**：生成原图、待入库文件、文档与 ComfyUI 工作流集中放这里，再部署到 `Assets/Resources/`。

## 目录一览

```
ArtPipeline/
├── README.md                 # 本文件
├── docs/                     # 规范与操作说明
├── manifest/                 # 资源清单（文件名、尺寸、prompt）
├── source/                   # ComfyUI 原始输出（按分类，可多版本）
│   ├── roles/
│   ├── items/
│   └── ui/
├── inbox/                    # 选定待入库（文件名必须与 Unity 一致）
│   ├── roles/
│   ├── items/
│   └── ui/
└── comfyui/                  # ComfyUI API 工作流
    └── workflows/
```

## 推荐模型

| 用途 | Checkpoint | 说明 |
|------|------------|------|
| 角色头像、道具插画 | **animagineXL_v3.safetensors** | 二次元卡牌风，与本项目 HUD 气质接近 |

详见 [docs/animagine-xl.md](docs/animagine-xl.md)。

## 一键生成（脚本 / GUI）

**推荐 GUI：**

```bash
python3 ArtPipeline/tools/artTool_ui.py
```

**命令行：**

```bash
python3 ArtPipeline/tools/cli.py --list
python3 ArtPipeline/tools/cli.py --category roles --to-inbox --deploy
# 旧路径仍可用
python3 Assets/Scripts/Tools/generate_icons_comfyui.py --kind role --to-inbox --deploy
```

配置与维护见 [tools/README.md](tools/README.md)、[tools/维护指南.md](tools/维护指南.md)。

## 工作流（ComfyUI → Unity）

1. 在 ComfyUI 用 **animagineXL_v3** 按清单生成，保存到 `source/<分类>/`（可保留多版 `role_warrior_v2.png` 等）。
2. 满意的一张 **复制/重命名** 为清单中的正式文件名，放入 `inbox/<分类>/`。
3. 入库 Unity Resources（直接复制，无裁切/抠图后处理）：

```bash
python3 Assets/Scripts/Tools/generate_icons_comfyui.py --kind role --to-inbox --deploy
# 或生成全部并入库
python3 Assets/Scripts/Tools/generate_icons_comfyui.py --all --to-inbox --deploy
```

旧版后处理脚本（裁切、去背景等）已移至项目根 `DeprecatedScripts/`。

## 与代码的对应关系

| inbox 路径 | Unity 目标 | 加载 |
|------------|------------|------|
| `inbox/roles/role_*.png` | `Assets/Resources/UI/Icons/Roles/` | `GameUiIconResources.GetRoleSprite` |
| `inbox/items/item_*.png` | `Assets/Resources/UI/Icons/Items/` | `GetItemSprite` |
| `inbox/ui/hp_heart_*.png` 等 | `Assets/Resources/UI/Icons/UI/` | `HpHeartFull` 等 |

## 关联文档

- [docs/目录说明.md](docs/目录说明.md)
- [docs/animagine-xl.md](docs/animagine-xl.md)
- [docs/cloud-generation.md](docs/cloud-generation.md) — 云生图（Stability / 万相 / 混元 / 即梦）
- `Assets/Scripts/表现优化文档.md` §7–§9（美术方向与分辨率）
- `Assets/Resources/UI/Icons/README.md`（Unity 导入设置）
