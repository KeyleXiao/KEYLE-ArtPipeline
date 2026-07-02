#!/usr/bin/env python3
"""DeepSeek AI 助手：生成提示词与工作流 JSON。"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash"
SUPPORTED_MODELS = ("deepseek-v4-flash", "deepseek-v4-pro")
_LEGACY_MODEL_MAP = {
    "deepseek-chat": "deepseek-v4-flash",
    "deepseek-reasoner": "deepseek-v4-pro",
}


def resolve_model(name: str | None) -> str:
    m = (name or "").strip()
    if not m:
        return DEFAULT_MODEL
    return _LEGACY_MODEL_MAP.get(m, m)


def verify_deepseek(api_key: str, model: str = "") -> tuple[bool, str]:
    """轻量连通性验证（max_tokens=1，不写入对话历史）。"""
    key = api_key.strip()
    if not key:
        return False, "请填写 API Key"
    resolved = resolve_model(model or DEFAULT_MODEL)
    if resolved not in SUPPORTED_MODELS:
        supported = " / ".join(SUPPORTED_MODELS)
        return False, f"不支持的模型「{resolved}」，请使用: {supported}"

    payload = {
        "model": resolved,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }
    req = urllib.request.Request(
        DEEPSEEK_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, "API Key 无效或已过期"
        return False, f"验证失败 (HTTP {exc.code})"
    except urllib.error.URLError as exc:
        return False, f"无法连接 DeepSeek: {exc}"

    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return False, str(msg)[:160]

    return True, f"已连接 · {resolved}"


SYSTEM_PROMPT = """你是 ArtPipeline 美术流水线的 AI 助手，帮助配置资源提示词与工作流 JSON。

## 生图后端与提示词页签（必读）

上下文中的 **生图后端** 决定用户实际用哪套提示词生图，写/改/清理提示词时必须对齐：

| 生图后端 | 只改这些 updates 字段 | 不要改 |
|---------|----------------------|--------|
| **云端 API** | `cloud_prompt`、`cloud_negative` | `positive_*` 四段、`positive`、`negative`、工作流 |

用户界面上的 **火山即梦 / 万相 / 混元** 等云页签 = 读写 `cloud_prompt` / `cloud_negative`，与 ComfyUI 四段页签无关。

### 清空与 null
- **清空**某字段：返回 **空字符串 `""`**（不是 null）
- **不修改**：返回 `null` 或省略该字段
| **ComfyUI 本地** | 四段 `positive_prefix/subject/scene/light`、`negative` | `cloud_prompt`、`cloud_negative` |

用户说「清理/优化/精简提示词」时：**只处理当前生图后端对应页签的字段**，另一套保持 null。

### 云端 cloud_prompt
- 单段正向 + cloud_negative；可用自然语言或英文 tag
- 清理时删重复、矛盾、过长赘述；勿拆成 positive_prefix 等四段

## ComfyUI 正向四段结构（仅本地后端）

1. **positive_prefix** 画质前缀  2. **positive_subject** 主体  3. **positive_scene** 场景  4. **positive_light** 光影
**negative** 负向。未改段设为 null。合并规则由 pipeline 自动完成。

### 分类补充（ComfyUI）
- items/skills：GII 触发词、icon 构图词；checkpoint game_icon_institute_v4_xl.safetensors
- roles：cowboy shot、dark fantasy
- backgrounds：轮盘塔罗硬币等场景词
- ui_*：border frame only, hollow transparent center, nine-slice

### 分类通用提示词 category_settings（重要）

`updates.category_settings` 写入**当前资源所属分类**的共享配置（影响该分类下全部资源生图）：

| 字段 | 作用 |
|------|------|
| **positive_common** | 生图时**前置**到每个资源正向 prompt（ComfyUI 与云端均生效） |
| **negative_common** | 生图时**追加**到每个资源负向 prompt |
| checkpoint / source / inbox / unity / alpha_matte | 分类路径与默认模型等 |

- 透明底分类（items/skills/ui_* 等）：positive_common 写 transparent background、isolated、no scenery；**勿**在单资源 positive_scene 重复
- backgrounds 等保留背景分类：positive_common 可为空或写海报风格基线，勿写 transparent background
- 用户要求写「分类通用提示词」「分类设置」时：在 `category_settings` 返回 positive_common / negative_common；未改字段 null
- **勿**把分类通用词写进单资源 cloud_prompt 或 positive_* 四段（除非用户明确要求只改当前资源）

示例：
```json
"category_settings": {
  "positive_common": "transparent background, isolated subject, ...",
  "negative_common": "solid background, scenery, ...",
  "checkpoint": null,
  "source": null
}
```

## 工作流（仅 ComfyUI；云端 workflow 始终 null）

## 回复要求
1. 只输出一个 JSON 对象，不要 markdown 代码块
2. 先根据「生图后端」选择 cloud 或 ComfyUI 字段；云端任务只改 cloud_prompt/cloud_negative
3. 未改字段设为 null；清空字段用 `""`

JSON updates 字段含：cloud_prompt, cloud_negative, positive_prefix, positive_subject, positive_scene, positive_light, positive, negative, workflow, checkpoint, category_settings, gen_mode 等（见上下文说明）。

云端写提示词示例（火山即梦 / 万相等）：
{"message":"已写入云提示词","updates":{"cloud_prompt":"masterpiece, game icon, golden dice, centered","cloud_negative":"blurry, watermark, text"}}
清空云提示词：{"message":"已清空","updates":{"cloud_prompt":"","cloud_negative":""}}
"""


class AiAssistantError(RuntimeError):
    pass


def _ai_update_present(val: Any) -> bool:
    if val is None:
        return False
    if isinstance(val, bool):
        return True
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return True
    s = str(val).strip().lower()
    return s not in ("", "null", "none")


def _ai_field_set(updates: dict[str, Any], key: str) -> bool:
    """updates 中显式提供了该字段（含空字符串表示清空）；null 表示不修改。"""
    if not isinstance(updates, dict):
        return False
    if key not in updates:
        return False
    return updates.get(key) is not None


def normalize_ai_updates(updates: dict[str, Any]) -> dict[str, Any]:
    """清洗 AI updates：字符串 null/none 视为未改；兼容误放在根级的字段。"""
    if not isinstance(updates, dict):
        return {}
    out: dict[str, Any] = {}
    for key, val in updates.items():
        if isinstance(val, str) and val.strip().lower() in ("null", "none"):
            continue
        out[key] = val
    return out


def collect_cloud_prompt_update(updates: dict[str, Any]) -> str | None:
    """从 updates 提取云正向（含清空 \"\"）；无法识别则 None。"""
    if not isinstance(updates, dict):
        return None
    if "cloud_prompt" in updates and updates["cloud_prompt"] is not None:
        return str(updates["cloud_prompt"]).strip()
    if "positive" in updates and updates["positive"] is not None:
        return str(updates["positive"]).strip()
    parts: list[str] = []
    for key in ("positive_prefix", "positive_subject", "positive_scene", "positive_light"):
        if key in updates and updates[key] is not None:
            text = str(updates[key]).strip()
            if text:
                parts.append(text)
    if parts:
        from bootstrap_config import join_prompt_segments

        return join_prompt_segments(*parts)
    return None


def collect_cloud_negative_update(updates: dict[str, Any]) -> str | None:
    if not isinstance(updates, dict):
        return None
    if "cloud_negative" in updates and updates["cloud_negative"] is not None:
        return str(updates["cloud_negative"]).strip()
    if "negative" in updates and updates["negative"] is not None:
        return str(updates["negative"]).strip()
    return None


def updates_intent_cloud_prompt(updates: dict[str, Any]) -> bool:
    """updates 是否显式包含云提示词或误写的 positive 系字段。"""
    if not isinstance(updates, dict):
        return False
    keys = (
        "cloud_prompt",
        "cloud_negative",
        "positive",
        "negative",
        "positive_prefix",
        "positive_subject",
        "positive_scene",
        "positive_light",
    )
    return any(k in updates and updates[k] is not None for k in keys)


def _ai_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    raise AiAssistantError(f"无法解析 enabled 布尔值: {val!r}")


def build_context_message(
    *,
    asset_id: str,
    filename: str,
    category: str,
    category_label: str,
    width: int,
    height: int,
    subject: str,
    positive: str,
    negative: str,
    workflow_summary: str,
    positive_prefix: str = "",
    positive_subject: str = "",
    positive_scene: str = "",
    positive_light: str = "",
    positive_g: str = "",
    positive_l: str = "",
    seed: str = "",
    enabled: bool = True,
    remove_bg_mode: str = "inherit",
    category_options: str = "",
    category_source: str = "",
    category_inbox: str = "",
    category_unity: str = "",
    category_checkpoint: str = "",
    category_alpha_matte: str = "",
    category_positive_common: str = "",
    category_negative_common: str = "",
    asset_checkpoint: str = "",
    effective_checkpoint: str = "",
    is_cloud_model: bool = False,
    cloud_prompt: str = "",
    cloud_negative: str = "",
    cloud_gen_mode: str = "text_to_image",
    cloud_strength: float = 0.65,
    cloud_model_label: str = "",
    gen_mode: str = "txt2img",
    ref_image: str = "",
    img2img_denoise: float = 0.65,
) -> str:
    backend_line = (
        "云端 API（云提示词页 · cloud_prompt / cloud_negative）"
        if is_cloud_model
        else "ComfyUI 本地（四段提示词页 · positive_prefix/subject/scene/light）"
    )
    if is_cloud_model:
        prompt_block = (
            f"【云正向 cloud_prompt · 生图实际使用】\n{cloud_prompt or '（空）'}\n"
            f"（若为空，生成时会回退到下方 ComfyUI 合并 positive）\n\n"
            f"【ComfyUI 合并 positive · 仅作参考/回退，云端任务勿改】\n{positive or '（空）'}"
        )
        neg_block = cloud_negative or negative or "（空）"
        neg_label = "云负向 cloud_negative"
    elif any(
        str(x or "").strip()
        for x in (positive_prefix, positive_subject, positive_scene, positive_light)
    ):
        prompt_block = (
            f"【画质前缀 positive_prefix】\n{positive_prefix or '（空）'}\n\n"
            f"【主核心主体 positive_subject】\n{positive_subject or '（空）'}\n\n"
            f"【环境场景 positive_scene】\n{positive_scene or '（空）'}\n\n"
            f"【光影氛围 positive_light】\n{positive_light or '（空）'}\n\n"
            f"【合并 positive】\n{positive or '（空）'}"
        )
        neg_block = negative or "（空）"
        neg_label = "负向 negative"
    elif category in ("items", "skills") and (positive_g or positive_l):
        prompt_block = (
            f"SDXL-G 边框:\n{positive_g or '（空）'}\n\n"
            f"SDXL-L 物件:\n{positive_l or '（空）'}"
        )
        neg_block = negative or "（空）"
        neg_label = "负向 negative"
    else:
        prompt_block = positive or "（空）"
        neg_block = negative or "（空）"
        neg_label = "负向 negative"

    cloud_extra = ""
    if is_cloud_model:
        cloud_extra = (
            f"- 云模型: {cloud_model_label or effective_checkpoint}\n"
            f"- 云页签提示词字段: cloud_prompt / cloud_negative（UI 如火山即梦页签）\n"
            f"- 云生成模式: {cloud_gen_mode or 'text_to_image'}\n"
            f"- 云参考强度 cloud_strength: {cloud_strength}\n"
        )

    return (
        f"当前资源上下文:\n"
        f"- id: {asset_id}\n"
        f"- 文件名: {filename}\n"
        f"- 分类: {category} ({category_label})\n"
        f"- 尺寸: {width}×{height}\n"
        f"- 说明(subject): {subject or '（空）'}\n"
        f"- seed: {seed or '（留空=全局）'}\n"
        f"- 启用: {'是' if enabled else '否'}\n"
        f"- 剔除背景: {remove_bg_mode or 'inherit'}\n"
        f"- 资源 checkpoint: {asset_checkpoint or '（跟随分类）'}\n"
        f"- 实际生效 checkpoint: {effective_checkpoint or '（未配置）'}\n"
        f"- 生图后端: {backend_line}\n"
        f"{cloud_extra}"
        f"- 可用分类 id: {category_options or category}\n"
        f"- 当前分类设置 (category_settings):\n"
        f"  · source: {category_source or '（空）'}\n"
        f"  · inbox: {category_inbox or '（空）'}\n"
        f"  · unity: {category_unity or '（空）'}\n"
        f"  · checkpoint: {category_checkpoint or '（未设置）'}\n"
        f"  · alpha_matte: {category_alpha_matte or 'border'}\n"
        f"  · positive_common: {category_positive_common or '（空）'}（生图时前置到正向）\n"
        f"  · negative_common: {category_negative_common or '（空）'}（生图时追加到负向）\n"
        f"- 生成模式: gen_mode={gen_mode or 'txt2img'}"
        f"{f', ref_image={ref_image}' if ref_image else ''}"
        f"{f', img2img_denoise={img2img_denoise}' if gen_mode in ('img2img', 'redraw') else ''}\n"
        f"- 正向 prompt:\n{prompt_block}\n"
        f"- {neg_label}:\n{neg_block}\n"
        f"- 工作流: {workflow_summary}\n"
    )


def ai_mode_prefix(mode: str, *, is_cloud_model: bool) -> str:
    """按生图后端与对话模式生成任务前缀。"""
    if mode == "prompt":
        if is_cloud_model:
            return (
                "【任务：为当前云端模型生成或重写 cloud_prompt + cloud_negative；"
                "只改云提示词页字段；勿改 ComfyUI 四段 positive_* / positive / negative / workflow；"
                "未改字段设为 null】\n"
            )
        return (
            "【任务：按四段结构生成或重写 ComfyUI 提示词：positive_prefix / positive_subject / "
            "positive_scene / positive_light + negative；勿改 cloud_prompt 与 category_settings；未改段设为 null】\n"
        )
    if mode == "refine":
        if is_cloud_model:
            return (
                "【任务：按用户要求微调或清理 cloud_prompt / cloud_negative（删减、去重、精简）；"
                "保留未提及内容；勿改 positive_* 四段；未改字段设为 null】\n"
            )
        return (
            "【任务：在四段提示词基础上按用户要求微调或清理（prefix/subject/scene/light/negative），"
            "保留未提及段的合理内容；勿改 cloud_prompt；未改段设为 null】\n"
        )
    if mode == "workflow":
        if is_cloud_model:
            return "【任务：当前为云端生图，无 ComfyUI 工作流；workflow 保持 null，可说明云端不支持工作流 JSON】\n"
        return "【任务：处理 ComfyUI 工作流 JSON；仅用户明确要求改结构时才返回 workflow】\n"
    if mode == "category":
        return (
            "【任务：填写或修改当前资源所属分类的 category_settings，重点 positive_common 与 negative_common；"
            "这是分类级通用提示词，生图时会自动前置/追加到该分类每个资源；"
            "透明底分类写抠底/孤立主体词，单资源四段或 cloud_prompt 勿重复这些词；"
            "可同时改 checkpoint/source/inbox/unity/alpha_matte；未改字段设为 null；"
            "不要改单资源 positive_* / cloud_prompt / negative / workflow / 基本信息】\n"
        )
    if mode == "basic":
        return (
            "【任务：填写或修改资源「基本信息」页（subject、filename、分类、宽×高、seed、启用、剔除背景、checkpoint）；"
            "updates 中未改字段设为 null；不要改 prompt/workflow/category_settings】\n"
        )
    # free
    if is_cloud_model:
        return (
            "【任务：根据用户意图配置当前资源；生图走云端（火山即梦等云页签）；"
            "提示词只改 updates.cloud_prompt 与 cloud_negative，清空用空字符串 \"\"；"
            "勿改 positive_* 四段与 positive/negative；可同时改基本信息、category_settings、gen_mode；"
            "未改字段设为 null】\n"
        )
    return (
        "【任务：根据用户意图配置当前资源；生图走 ComfyUI 时提示词只改四段 positive_* 与 negative，"
        "勿改 cloud_prompt；可同时改基本信息、category_settings、gen_mode/ref_image、工作流；"
        "updates 中未改字段设为 null】\n"
    )


def chat(
    messages: list[dict[str, str]],
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    timeout_s: float = 120.0,
) -> str:
    if not api_key.strip():
        raise AiAssistantError("请先在「全局设置」中填写 DeepSeek API Key")

    model = resolve_model(model)
    if model not in SUPPORTED_MODELS:
        supported = " / ".join(SUPPORTED_MODELS)
        raise AiAssistantError(f"不支持的模型「{model}」，请使用: {supported}")

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.4,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        DEEPSEEK_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key.strip()}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise AiAssistantError(f"DeepSeek HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise AiAssistantError(f"无法连接 DeepSeek: {exc}") from exc

    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AiAssistantError(f"DeepSeek 响应格式异常: {data}") from exc


_JSON_BLOCK = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def parse_ai_response(text: str) -> tuple[str, dict[str, Any]]:
    """解析 AI 回复，返回 (message, updates)。"""
    raw = text.strip()
    block = _JSON_BLOCK.search(raw)
    if block:
        raw = block.group(1).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AiAssistantError(f"AI 返回的不是有效 JSON:\n{raw[:500]}") from exc

    if not isinstance(data, dict):
        raise AiAssistantError("AI 返回的根节点必须是 JSON 对象")

    message = str(data.get("message") or "已更新")
    updates = data.get("updates")
    if updates is None:
        # 兼容 AI 把 cloud_prompt 等写在根对象
        updates = {
            k: data[k]
            for k in data
            if k not in ("message", "updates") and data[k] is not None
        }
    if not isinstance(updates, dict):
        updates = {}
    return message, normalize_ai_updates(updates)
