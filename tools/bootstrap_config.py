#!/usr/bin/env python3
"""生成 pipeline_config.json 初始内容。"""

from __future__ import annotations

PREFIX = "masterpiece, best quality, very aesthetic, absurdres, "
BASE_NEG = (
    "lowres, bad anatomy, bad hands, text, error, watermark, signature, blurry, "
    "checkerboard, transparent background grid, white background, letterbox, "
    "cropped, worst quality, low quality, jpeg artifacts, abstract, logo, emblem only"
)

ROLE_COMP = (
    "square image, 1:1 aspect ratio, from waist up, cowboy shot, upper body, "
    "complete face visible, detailed eyes, detailed face, both arms visible, "
    "both hands visible, shoulders fully visible, chest visible, "
    "subject fills 90 percent of frame, large character, zoomed in, centered, "
    "looking at viewer, detailed shading, painterly, dark fantasy, demon roulette, game character icon, "
    "ornate details, intricate patterns, engraved gold trim"
)

ROLE_NEG_EXTRA = (
    ", head only, face only, missing face, empty hood, floating head, missing arms, "
    "no arms, missing hands, incomplete body, tiny character, wide shot, full body, chibi"
)

TRAITOR_NEG = ", eyes, nose, mouth, lips, smile, teeth, facial features"

# 分类级通用提示词（生成时 **前置** 到每个资源 prompt，SDXL 对句首权重更敏感）
CATEGORY_POSITIVE_COMMON = (
    "(transparent background:1.4), (alpha channel:1.25), isolated subject, clean cutout sticker, "
    "simple flat background, single color backdrop, no scenery, floating object"
)
CATEGORY_NEGATIVE_COMMON = (
    "(solid background:1.35), (opaque background:1.25), complex background, detailed background, "
    "white background, black background, grey background, gradient background, colored backdrop, "
    "scenery, environment, floor, ground, wall, room, studio backdrop, vignette, cast shadow on ground"
)

# 全屏海报等需要保留背景的分类
NO_TRANSPARENT_CATEGORIES = frozenset({"backgrounds"})


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


def join_prompt_segments(*parts: str) -> str:
    """拼接正向分段（画质/主体/场景/光影），忽略空段。"""
    cleaned: list[str] = []
    for part in parts:
        text = str(part or "").strip().rstrip(",")
        if text:
            cleaned.append(text)
    return ", ".join(cleaned)

# Game Icon Institute V4_XL 风格触发词（Civitai #47800）
# gmic_(3dicon)=欧美3D游戏icon | gmic icon_(xieshi)=写实 | gmic_(2dXIANTIAO)=2D线条
GII_STYLE_TRIGGER = "gmic_(3dicon)"

ITEM_BASE_NEG = (
    "lowres, text, error, watermark, signature, blurry, worst quality, low quality, "
    "jpeg artifacts, human, face, character, person, "
    "abstract symbol, unrecognizable object, "
    "curtain, wallpaper, architecture, ui background, "
    "tiny object, zoomed out, lots of empty space, white background, "
    "anime, manga, photorealistic photo"
)

ITEM_NEG_EXTRA = (
    ", multiple objects, wrong item, cluttered scene, item name text, letters, numbers, "
    "icon grid, sprite sheet, multiple icons, collage"
)

# ---------------------------------------------------------------------------
# 图标 prompt 写法：常见生活物体（real）+ 游戏特效（effects）
# - real：人人能认出的日常实物，写清材质/形状/数量（放大镜、药片、奶茶杯、扑克牌…）
# - effects：奇幻/状态/光效，表达道具玩法，勿替代主体
# - 避免 abstract symbol、emblem only、mystical orb 等纯抽象描述
# ---------------------------------------------------------------------------

# real = 现实可识别物体；effects = 奇幻特效；subject = UI 中文说明（物体 · 特效）
ITEMS: dict[str, dict[str, str]] = {
    "item_spyglass.png": {
        "subject": "窥视镜 · 放大镜 + 紫蓝魔法光",
        "real": "brass magnifying glass with round glass lens and short handle",
        "effects": "purple and blue mystical glow on lens, soft magical sparkle, warm copper gold metal shine",
    },
    "item_eject_drink.png": {
        "subject": "退弹饮 · 绿色药瓶 + 弹壳弹出",
        "real": "dark green glass medicine bottle with narrow neck and cork stopper",
        "effects": "brass bullet cartridge popping out of bottle mouth, green potion glow inside, small eject motion sparkle",
    },
    "item_rage_potion.png": {
        "subject": "狂暴剂 · 锯齿药瓶 + 沸腾红液火星",
        "real": "jagged cracked glass potion bottle",
        "effects": "boiling dark red liquid inside, orange fire sparks rising from neck, glowing red heat aura, bubbling foam",
    },
    "item_amulet.png": {
        "subject": "护身符 · 金吊坠 + 暗红宝石",
        "real": "gold pendant necklace with oval medallion and chain loop",
        "effects": "dark ruby red gem in center, soft holy golden protective shimmer, engraved rune glow",
    },
    "item_milk_tea.png": {
        "subject": "奶茶 · 珍珠奶茶杯 + 金边",
        "real": "ceramic bubble tea cup with wide straw hole and visible tapioca pearls inside",
        "effects": "warm milk tea brown liquid, black pearl shine, subtle steam wisps, gold rim highlight",
    },
    "item_expired_pill.png": {
        "subject": "过期药片 · 白色药片 + 泡罩 + 发黑变色",
        "real": "single round white medicine pill tablet in silver aluminum blister pack, one plastic cavity opened",
        "effects": (
            "expired pill turned dark grey and black at edges, yellow-brown discoloration spots, "
            "chalky stale surface, fine crack lines, faint sickly green toxic haze"
        ),
    },
    "item_steal_hand.png": {
        "subject": "妙手空空 · 黑手套 + 金筹码",
        "real": "single black leather glove hand clutching gold poker chips",
        "effects": "dark purple shadow aura on cuff, gold chip glint sparkle, sly motion trail",
    },
    "item_inverter.png": {
        "subject": "置换器 · 左轮弹巢 + 红灰子弹",
        "real": "revolver cylinder drum seen from front showing two bullet chambers",
        "effects": "red live bullet glowing in one slot, gray blank bullet in other slot, brass flip arrow indicator, metallic gleam",
    },
}


def build_real_effects_body(spec: dict[str, str]) -> str:
    """合并「常见生活物体 + 特效」主体段。"""
    return f"{spec['real']}, {spec['effects']}"


def build_item_prompts(spec: dict[str, str]) -> dict[str, str]:
    """道具 prompt：GII 触发词 + 常见生活物体 + 特效。"""
    obj = build_real_effects_body(spec)
    pos_g = (
        f"{GII_STYLE_TRIGGER}, {obj}, recognizable everyday object, game item icon, single object, centered, "
        "fills canvas, western game art, cutout sticker"
    )
    pos_l = (
        f"{GII_STYLE_TRIGGER}, {obj}, recognizable everyday object, "
        "detailed game prop icon, high quality, no humans"
    )
    neg = ITEM_BASE_NEG + ITEM_NEG_EXTRA
    return {
        "positive_g": pos_g,
        "positive_l": pos_l,
        "positive": f"{pos_g} {pos_l}",
        "negative": neg,
    }


ROLES = {
    "role_dealer.png": (
        "1girl, solo, beautiful mature woman, casino dealer, croupier, complete face, "
        "purple and gold tailcoat, white opera gloves, both hands holding poker chips and cards"
    ),
    "role_magician.png": (
        "1girl, solo, stage magician, complete face, top hat, magic wand, both arms visible, "
        "purple magic particles, floating bullet shells"
    ),
    "role_gambler.png": (
        "1boy, solo, gambler, roguish smirk, complete face, dice in hand, both hands visible, "
        "leather vest, spyglass monocle, golden luck aura"
    ),
    "role_warrior.png": (
        "1boy, solo, knight warrior, complete face, gold pauldrons, breastplate, "
        "both hands gripping sword hilt, protective shield aura"
    ),
    "role_mercenary.png": (
        "1boy, solo, mercenary, eyepatch, complete face, both arms visible, revolver, "
        "red rage potion vial, red aura particles"
    ),
    "role_doctor.png": (
        "1girl, solo, doctor apothecary, complete face, dark medical robe, "
        "both hands holding syringe and glowing potion vial, toxic green mist"
    ),
    "role_traitor.png": (
        "1boy, solo, hooded figure, smooth blank oval face skin under hood, no eyes no nose no mouth, "
        "both forearms crossed over chest, dark purple cloak, gold trim"
    ),
}

# UI 状态标志：subject = 中文说明（玩法 · 主体 + 特效）；real / effects 用于 ComfyUI prompt
UI_STATUS: dict[str, dict[str, str]] = {
    "hp_heart_full.png": {
        "subject": "当前生命 · 实心红心 + 金边",
        "real": "classic plump heart shape symbol, filled solid heart icon",
        "effects": "bright crimson red fill, thin gold metallic rim highlight, subtle warm life glow",
    },
    "hp_heart_empty.png": {
        "subject": "已失去生命槽 · 空心灰心",
        "real": "simple heart outline shape, hollow empty center, thin stroke only",
        "effects": "dark gray silver edge, dim inactive look, no inner fill",
    },
    "hp_heart_dead.png": {
        "subject": "淘汰玩家 · 碎裂灰心",
        "real": "heart shape with crack lines and broken chipped pieces",
        "effects": "faded ash gray tone, fractured edges, dead lifeless matte surface",
    },
    "badge_lock.png": {
        "subject": "本回合锁枪 · 黄铜挂锁",
        "real": "small brass padlock with closed shackle, side view on dark round chip badge",
        "effects": "soft purple restriction glow, polished metal lock shine",
    },
    "badge_skull.png": {
        "subject": "玩家淘汰 · 白色骷髅头",
        "real": "small white human skull front view on dark circular badge medal",
        "effects": "faint red death mist, cracked dark rim, eliminated player mark",
    },
    "badge_traitor.png": {
        "subject": "背叛者身份 · 兜帽剪影",
        "real": "small hooded cloak silhouette head, smooth blank face under hood on dark gold medal",
        "effects": "purple ominous shadow glow, secret traitor trim, hidden identity mark",
    },
}

UI_FRAMES = {
    "card_frame.png": {
        "subject": "玩家金框 · 九宫格卡框",
        "desc": "ornate gold purple rectangular portrait card border frame, baroque demon casino style, thick decorative corners",
    },
    "card_frame_silver.png": {
        "subject": "玩家银框 · 九宫格卡框",
        "desc": "ornate silver gray rectangular portrait card border frame, cool metallic trim, same layout as gold frame",
    },
}

UI_BUTTONS = {
    "btn_primary.png": {
        "subject": "主按钮 · 紫金边框",
        "desc": "rounded rectangle game UI button border frame only, rich purple and gold ornate rim, hollow transparent center, no center fill, primary action style",
    },
    "btn_secondary.png": {
        "subject": "次按钮 · 暗灰边框",
        "desc": "rounded rectangle game UI button border frame only, dark charcoal gray silver rim, hollow transparent center, no center fill, secondary action style",
    },
    "btn_danger.png": {
        "subject": "危险按钮 · 暗红边框",
        "desc": "rounded rectangle game UI button border frame only, deep crimson red black gold rim, hollow transparent center, no center fill, danger shoot action style",
    },
}

UI_COMBAT = {
    "revolver.png": {
        "subject": "左轮手枪 · 侧视",
        "real": "classic revolver handgun side profile pointing right, dark steel metal body, wooden grip",
        "effects": "subtle muzzle highlight, polished metal gleam, game prop icon",
    },
    "bullet.png": {
        "subject": "子弹 · 侧视",
        "real": "brass bullet cartridge side view, copper gold casing, pointed tip facing right",
        "effects": "metallic shine, small motion streak optional",
    },
}

SKILLS = {
    "skill_gambler_luck.png": {
        "subject": "好运连连 · 赌徒被动",
        "real": "pair of golden dice with lucky clover charm",
        "effects": "sparkling gold luck aura, soft green fortune glow",
    },
    "skill_dealer_prophet.png": {
        "subject": "先知 · 扑克牌 + 放大镜",
        "real": "fan of five ordinary playing cards, ace of spades on top, small brass pocket magnifying glass on center card",
        "effects": "soft purple glow around magnifying lens, faint card edge highlight",
    },
    "skill_magician_trajectory.png": {
        "subject": "弹道操控 · 子弹 + 魔杖",
        "real": "brass bullet cartridge floating beside slim wooden magic wand with white tip",
        "effects": "curved purple magic spark trail bending bullet path, direction flip sparkle at wand tip",
    },
}

BACKGROUNDS = {
    "startup_poster.png": (
        "no humans, vertical mobile game splash poster, demon roulette casino theme, "
        "dark purple and gold atmosphere, revolver and roulette wheel motifs, "
        "dramatic lighting, ornate gothic frame edges, title safe center area"
    ),
}

HP_SUFFIX = "single symbol centered, game ui icon, bold readable silhouette"

UI_SYMBOL_NEG = (
    BASE_NEG + ", human, face, character, person, full scene, landscape, filled panel, inner illustration"
)

UI_SLICE_NEG = UI_SYMBOL_NEG + ", solid center fill, portrait photo, character inside frame"

COMBAT_NEG = (
    "lowres, text, error, watermark, signature, blurry, worst quality, low quality, "
    "human, face, hands holding gun, multiple guns, scene, background room"
)


def build_ui_symbol_prompt(tags: str) -> dict[str, str]:
    return {
        "positive": f"{PREFIX}{tags}, {HP_SUFFIX}",
        "negative": UI_SYMBOL_NEG,
    }


def build_ui_status_prompts(spec: dict[str, str], *, heart_symbol: bool = False) -> dict[str, str]:
    obj = build_real_effects_body(spec)
    shape_hint = "clear readable heart ui symbol" if heart_symbol else "recognizable everyday object"
    pos = (
        f"{PREFIX}no humans, {obj}, {shape_hint}, "
        f"small game status badge icon, centered, {HP_SUFFIX}"
    )
    return {"positive": pos, "negative": UI_SYMBOL_NEG}


def build_ui_badge_prompts(spec: dict[str, str]) -> dict[str, str]:
    """兼容旧名。"""
    return build_ui_status_prompts(spec)


def build_ui_slice_prompt(desc: str) -> dict[str, str]:
    pos = (
        f"{PREFIX}no humans, {desc}, game UI nine-slice border frame, "
        "hollow transparent center, empty middle, dark fantasy demon roulette, "
        "symmetrical, single ui asset, clean alpha edges"
    )
    return {"positive": pos, "negative": UI_SLICE_NEG}


def build_ui_combat_prompts(spec: dict[str, str]) -> dict[str, str]:
    obj = build_real_effects_body(spec)
    pos = (
        f"{PREFIX}no humans, {obj}, recognizable everyday object, "
        f"centered, game combat prop icon, {HP_SUFFIX}"
    )
    return {"positive": pos, "negative": COMBAT_NEG}


def build_skill_prompts(spec: dict[str, str]) -> dict[str, str]:
    obj = build_real_effects_body(spec)
    pos_g = (
        f"{GII_STYLE_TRIGGER}, {obj}, recognizable everyday object, game skill icon, single symbol, centered, "
        "fills canvas, western dark fantasy game art"
    )
    pos_l = (
        f"{GII_STYLE_TRIGGER}, {obj}, recognizable everyday object, "
        "detailed skill ability icon, high quality, no humans"
    )
    neg = ITEM_BASE_NEG + ITEM_NEG_EXTRA
    return {
        "positive_g": pos_g,
        "positive_l": pos_l,
        "positive": f"{pos_g} {pos_l}",
        "negative": neg,
    }


def _category(
    cat_id: str,
    label: str,
    folder: str,
    unity: str,
    *,
    workflow: str = "workflows/_default_sdxl_api.json",
    checkpoint: str = "animagineXL_v3.safetensors",
    lora: str = "",
    lora_strength: float = 0.0,
) -> dict:
    transparent = cat_id not in NO_TRANSPARENT_CATEGORIES
    return {
        "id": cat_id,
        "label": label,
        "source": f"source/{folder}",
        "inbox": f"inbox/{folder}",
        "unity": unity,
        "default_workflow": workflow,
        "checkpoint": checkpoint,
        "lora": lora,
        "lora_strength": lora_strength,
        "positive_common": CATEGORY_POSITIVE_COMMON if transparent else "",
        "negative_common": CATEGORY_NEGATIVE_COMMON if transparent else "",
        "alpha_matte": "none",
    }


def _asset(
    filename: str,
    category: str,
    *,
    subject: str,
    width: int,
    height: int,
    gen_scale: float = 1.0,
    prompts: dict[str, str] | None = None,
    positive: str = "",
    negative: str = "",
    remove_bg_mode: str | None = None,
    remove_bg: bool | None = None,
) -> dict:
    stem = filename.replace(".png", "")
    if remove_bg_mode is None and remove_bg is not None:
        remove_bg_mode = "remove" if remove_bg else "keep"
    row: dict = {
        "id": stem,
        "filename": filename,
        "category": category,
        "width": width,
        "height": height,
        "subject": subject,
        "workflow": f"workflows/assets/{stem}.json",
        "enabled": True,
        "gen_scale": gen_scale,
        "seed": "",
        "remove_bg_mode": remove_bg_mode or "inherit",
    }
    if prompts:
        row.update(prompts)
    else:
        row["positive"] = positive
        row["negative"] = negative
    return row


def build_default_config() -> dict:
    categories = [
        _category(
            "roles",
            "角色头像",
            "roles",
            "Assets/Art/UI/Icons/Roles",
        ),
        _category(
            "items",
            "道具 icon",
            "items",
            "Assets/Art/UI/Icons/Items",
            workflow="workflows/_item_icon_gii_api.json",
            checkpoint="game_icon_institute_v4_xl.safetensors",
        ),
        _category(
            "skills",
            "技能 icon",
            "skills",
            "Assets/Art/UI/Icons/Skills",
            workflow="workflows/_item_icon_gii_api.json",
            checkpoint="game_icon_institute_v4_xl.safetensors",
        ),
        _category(
            "ui_status",
            "UI 状态标志",
            "ui_status",
            "Assets/Art/UI/Icons/UI/Status",
        ),
        _category(
            "ui_frames",
            "UI 边框",
            "ui_frames",
            "Assets/Art/UI/Icons/UI/Frames",
        ),
        _category(
            "ui_buttons",
            "UI 按钮",
            "ui_buttons",
            "Assets/Art/UI/Icons/UI/Buttons",
        ),
        _category(
            "ui_combat",
            "开枪贴图",
            "ui_combat",
            "Assets/Art/UI/Icons/UI/Combat",
        ),
        _category(
            "backgrounds",
            "背景海报",
            "backgrounds",
            "Assets/Art/UI",
            workflow="workflows/_sdxl_split_g_l_api.json",
        ),
    ]

    assets: list[dict] = []

    for fn, tags in ROLES.items():
        neg = BASE_NEG + ROLE_NEG_EXTRA
        if "traitor" in fn:
            neg += TRAITOR_NEG
        assets.append(
            _asset(
                fn,
                "roles",
                subject=tags[:60],
                width=512,
                height=512,
                gen_scale=2.0,
                positive=f"{PREFIX}{tags}, {ROLE_COMP}",
                negative=neg,
            )
        )

    for fn, spec in ITEMS.items():
        assets.append(
            _asset(
                fn,
                "items",
                subject=spec["subject"],
                width=256,
                height=256,
                gen_scale=2.0,
                prompts=build_item_prompts(spec),
            )
        )

    for fn, spec in SKILLS.items():
        assets.append(
            _asset(
                fn,
                "skills",
                subject=spec["subject"],
                width=128,
                height=128,
                gen_scale=2.0,
                prompts=build_skill_prompts(spec),
            )
        )

    for fn, spec in UI_STATUS.items():
        heart = fn.startswith("hp_heart")
        assets.append(
            _asset(
                fn,
                "ui_status",
                subject=spec["subject"],
                width=128,
                height=128,
                gen_scale=1.0,
                prompts=build_ui_status_prompts(spec, heart_symbol=heart),
            )
        )

    for fn, spec in UI_FRAMES.items():
        assets.append(
            _asset(
                fn,
                "ui_frames",
                subject=spec["subject"],
                width=128,
                height=128,
                gen_scale=2.0,
                prompts=build_ui_slice_prompt(spec["desc"]),
            )
        )

    for fn, spec in UI_BUTTONS.items():
        assets.append(
            _asset(
                fn,
                "ui_buttons",
                subject=spec["subject"],
                width=128,
                height=128,
                gen_scale=2.0,
                prompts=build_ui_slice_prompt(spec["desc"]),
            )
        )

    for fn, spec in UI_COMBAT.items():
        assets.append(
            _asset(
                fn,
                "ui_combat",
                subject=spec["subject"],
                width=256,
                height=256,
                gen_scale=2.0,
                prompts=build_ui_combat_prompts(spec),
            )
        )

    for fn, tags in BACKGROUNDS.items():
        prompts = build_ui_symbol_prompt(tags)
        assets.append(
            _asset(
                fn,
                "backgrounds",
                subject="启动海报 · 竖屏暗黑赌场",
                width=512,
                height=896,
                gen_scale=1.0,
                prompts=prompts,
            )
        )

    from app_release import APP_VERSION, DEFAULT_RELEASE_NOTES

    return {
        "version": 1,
        "app_version": APP_VERSION,
        "release_notes": dict(DEFAULT_RELEASE_NOTES),
        "defaults": {
            "project_root": "",
            "art_pipeline_root": "",
            "welcome_dismissed_version": "",
            "comfyui_url": "http://127.0.0.1:8188",
            "checkpoint": "animagineXL_v3.safetensors",
            "steps": 35,
            "cfg": 7.0,
            "sampler": "euler_ancestral",
            "scheduler": "normal",
            "deepseek_api_key": "",
            "deepseek_model": "deepseek-v4-flash",
            "category_overrides": {
                "items": {"steps": 30, "cfg": 6.5},
                "skills": {"steps": 30, "cfg": 6.5},
            },
        },
        "categories": categories,
        "assets": assets,
    }
