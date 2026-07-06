# ArtPipeline Studio User Guide

> Online docs: <https://art.vrast.cn> (when deployed)

## Introduction

ArtPipeline Studio is a web-based art pipeline for game assets. It connects **ComfyUI / cloud API generation → source originals → inbox editing → game engine export**. Features include batch generation, layer post-processing, AI prompt assistant, and configuration management.

## Work Environment

### Requirements

| Component | Description |
|-----------|-------------|
| **ComfyUI** | Local Stable Diffusion (primary); workflows via HTTP API |
| **Cloud APIs** (optional) | Stability, Alibaba Wan, Tencent Hunyuan, Volcengine Jimeng |
| **ArtPipeline folder** | Holds source/inbox, workflow JSON, post-process overlays, and config |
| **Game engine project** | Usually Unity; export target is set under **Settings** |

### Recommended layout

```
ArtPipeline/
├── source/          # ComfyUI output (read-only for post-process)
├── inbox/           # Working copies and composited output
├── workflows/       # Per-asset workflow JSON
└── postprocess/     # Frames, backgrounds, overlays

YourGame/
└── Assets/...       # Final paths inside the engine project
```

### Starting the web app

```bash
cd ArtPipeline/artApp
python run_dev.py
```

Open the URL shown in the terminal (default `http://127.0.0.1:8765`).

## Features

### Asset pipeline

1. **Generate**: ComfyUI writes to **source**
2. **Post-process** (optional): Layers, crop, text on **inbox**; **source is never modified**
3. **Export to engine**: Copy inbox files into the engine project

**S·in·U** status chips:

- **S (source)**: green = exists, gray = not generated
- **in (inbox)**: green = matches source, yellow = post-processed, red = missing
- **U (engine)**: green = matches inbox, red = missing or needs re-export

![Main workbench — categories · assets · S·in·U · AI assistant · ComfyUI / cloud tabs](images/main-workbench.png)

### Main UI actions

| Action | Description |
|--------|-------------|
| Generate Selected / Category | Run ComfyUI for selected or all enabled assets in category |
| Generate & Export | Generate then export to engine |
| Export to Engine | Copy inbox → engine only (no regeneration) |
| Export Category | Export all assets in current category |
| Post-process… | Open layer editor on inbox |

Preview shows **post-process composite** when configured; otherwise the inbox/source image. Switch source / inbox / engine to inspect each stage.

### New asset & import

Click **+ New asset** in the asset list:

| Mode | Description |
|------|-------------|
| **Manual** | Filename, size, description — config only (disabled by default, no auto-generation) |
| **Import files** | Multi-select PNG / JPG / WebP; preview list with per-file remove; batch-create by filename into **source** and **inbox** (saved as PNG) |

Duplicate filenames are skipped. Right-click an asset to **generate / generate & export / export** one item without enabling it in the list.

![Create category](images/create-category.png)

![Category menu](images/category-context-menu.png)

![Manual new asset](images/create-asset-manual.png)

![Import external files](images/import-external-assets.png)

![Asset context menu](images/asset-context-menu.png)

### Runtime logs

Use the **Logs** FAB for live SSE output (All / Action / Generate / System).

Under **Settings → Log directory**, set where `studio.log` is written; leave empty for OS defaults:

| Platform | Default |
|----------|---------|
| macOS | `~/Library/Logs/ArtPipeline Studio/` |
| Windows | `%LOCALAPPDATA%\ArtPipeline Studio\Logs\` |
| Linux | `~/.artpipeline-studio/logs/` |

Recent lines are restored from the log file after restart. **Open folder** reveals the directory in Finder / Explorer.

![ComfyUI & checkpoint setup](images/comfyui-checkpoint-setup.png)

![Prompts & generation](images/prompt-generate.png)

![Runtime logs](images/runtime-logs.png)

### AI assistant

Modes: write prompts, refine, workflow, free chat. Configure DeepSeek API Key in **Settings**. AI uses asset category, size, and existing prompts; some modes write back to config.

### Checkpoint / model configuration

Generation models are set at **category → asset** level:

| Location | Purpose | When empty |
|----------|---------|------------|
| **Category** | Default model for assets in category | Not set — generation will prompt you |
| **Basic info** | Per-asset override | Inherit from category |
| **Settings** | ComfyUI URL, sampler params, cloud API keys, log dir, etc. | — |

The dropdown lists **local ComfyUI checkpoints** and **cloud models** (`cloud:` prefix). Cloud models work without ComfyUI online.

See [cloud generation](../../../docs/cloud-generation.md) (Chinese; English summary in `tools/cloud/registry.json`).

### Unified generation modes

| Mode | Description |
|------|-------------|
| **Text-to-Image** | Text only; no reference |
| **Image-to-Image** | Reference image + strength (denoise) |
| **Image Editing** | Edit from source original (ComfyUI uses `redraw` internally) |

- **ComfyUI**: four-segment prompts + workflow JSON on **Prompts & Workflow**
- **Cloud**: single `cloud_prompt` on **Cloud Prompts**; parallel async jobs

Platforms: **Stability AI**, **Alibaba Wan**, **Tencent Hunyuan**, **Volcengine Jimeng**. Registry: `tools/cloud/registry.json`.

### Post-process editor

- Subject layer `$asset` binds to **inbox** only
- **Restore from source**: overwrite inbox and reset layers
- **Apply to inbox**: composite and save
- **Export to engine**: save, export, return to main UI

## Best Practices

### 1. Keep source pristine

source is the AI “master”. Post-process only on **inbox**. Use **Restore from source** to start over.

### 2. Organize by category

Each category has its own paths, checkpoint, and shared prompt prefixes. Use separate categories for items, skills, avatars, etc. Override checkpoint per asset under **Basic info** when needed.

### 3. Pre-flight checks

- **ComfyUI** (local): pill **online**; checkpoint and workflow configured
- **Cloud**: API keys in **Settings**
- Category or asset has a **generation model**
- Assets enabled; ComfyUI assets need valid workflow JSON
- Prompts and dimensions match category rules

### 4. Suggested batch workflow

1. Set category prompts + per-asset subject
2. **Generate Category** → verify S / in status
3. **Post-process** assets that need polish
4. **Export Category** or **Generate & Export**
5. Refresh assets in the engine project

### 5. Version control & teamwork

- Use **Save Config** regularly
- Prefer paths relative to project root
- Backup ArtPipeline config and inbox before large changes

### 6. Troubleshooting

| Issue | Fix |
|-------|-----|
| Empty preview | Ensure source/inbox exists; refresh status |
| Engine out of date (red U) | Re-export after inbox/post-process changes |
| ComfyUI offline | Check URL and process; or use a cloud model |
| No model / cloud key | Set model under **Category** or **Basic info**; cloud keys in **Settings** |
| Export 405 | Restart `run_dev.py` |

---

See project maintenance docs and team wiki for updates.
