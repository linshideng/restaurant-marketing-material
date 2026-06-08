#!/usr/bin/env python3
"""Generate restaurant marketing materials with AI-led poster artwork.

The image model creates the full poster artwork, including composition,
headline typography, atmosphere, and optional mascot mood. The only hard
exception is QR: the model must not draw QR modules. The original user QR is
post-composited onto the generated poster with Pillow so scanability is not
lost.

Final pipeline (when QR is present):
  1. Responses API image_generation generates a complete poster without real QR modules
  2. Pillow overlays the selected local platform logo asset onto the poster
  3. Program scoring finds the best QR body area on the poster
  4. Pillow composites the original QR onto that area -> material_XX.png

The QR image is never recolored or redrawn. It is only proportionally scaled
and pasted onto a card/tray.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import html as _html
import http.client
import json
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import warnings
import zlib
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
STYLE_DIR = SKILL_ROOT / "templates" / "styles"
PROMPT_DIR = SKILL_ROOT / "templates" / "prompts"
ASSET_DIR = SKILL_ROOT / "assets"
LAYOUT_DIR = ASSET_DIR / "layouts"
BRAND_DIR = ASSET_DIR / "brand"
FONT_DIR = ASSET_DIR / "fonts"
MEITUAN_LOGO = BRAND_DIR / "meituan-logo.png"
MEITUAN_GROUP_BUYING_LOGO = BRAND_DIR / "meituan-group-buying-logo.png"
MEITUAN_WHITE_LOGO = BRAND_DIR / "meituan-white-logo.png"
MEITUAN_GROUP_WHITE_BUYING_LOGO = BRAND_DIR / "meituan-group-white-buying-logo.png"
DIANPING_LOGO = BRAND_DIR / "dianping-logo.png"
DIANPING_WHITE_LOGO = BRAND_DIR / "dianping-white-logo.png"
MASCOT_SHEET = BRAND_DIR / "meituan-mascot-sheet.png"

# --- Proxy/API Configuration (hardcoded, not from env) ---
# Proxy deployment placeholders. Replace these after restaurant-skill-proxy is deployed.
PROXY_BASE_URL = "http://10.15.212.158:8080/v1"
PROXY_TOKEN = "skill-proxy-2026-restaurant-material"
_PROXY_TOKEN_PLACEHOLDER = "PLACEHOLDER-DEPLOY-THEN-REPLACE"
API_MODEL = "gpt-5.5"
API_ENDPOINT_PATH = "/responses"
DX_PUSH_ENDPOINT_PATH = "/dx-push"
API_IMAGE_TOOL = {"type": "image_generation", "action": "generate"}
SELECTOR_SCORE_STRONG = 0.75
SELECTOR_SCORE_EXPLORE = 0.55
_REQUIRED_STYLE_FIELDS = {
    "template_id",
    "style_id",
    "label",
    "material_type",
    "scene_prompt",
    "color_strategy",
    "composition_strategy",
    "texture_guidance",
    "typography_profile",
}
LEGACY_VARIANT_IDS = {
    "bustling_warmth",
    "urban_chic",
    "poetic_leisure",
    "bold_fiery",
    "coastal_breeze",
    "nostalgic_lane",
    "youthful_buzz",
    "exotic_fusion",
    "festive_prosperity",
    "organic_garden",
    "midnight_glow",
    "pastel_dream",
    "family_joy",
    "industrial_craft",
}
_RETRYABLE_HTTP_CODES = {408, 429, 500, 502, 503, 504}

# Backward-compatible names used by tests and older callers.  The fixed API_*
# constants above remain the contract checked before any HTTP request is made.
API_BASE_URL = PROXY_BASE_URL
API_KEY = PROXY_TOKEN
RESPONSES_API_BASE_URL = PROXY_BASE_URL
RESPONSES_API_MODEL = API_MODEL
RESPONSES_API_ENDPOINT_PATH = API_ENDPOINT_PATH
RESPONSES_API_IMAGE_TOOL = API_IMAGE_TOOL
RESPONSES_API_KEY = PROXY_TOKEN

DEFAULT_VARIANTS = 2
DEFAULT_DPI = 150
DEFAULT_IMAGE_TIMEOUT = 420
DEFAULT_IMAGE_TIMEOUT_PANORAMA = 660
FALLBACK_IMAGE_TIMEOUT_PANORAMA = 660
DEFAULT_SECONDARY_DEADLINE_SECONDS = 300
MODEL_REFERENCE_MAX_LONG_EDGE = 1024
MODEL_REFERENCE_ALPHA_MAX_LONG_EDGE = 1024
MODEL_REFERENCE_JPEG_QUALITY = 88
MODEL_REFERENCE_SKIP_JPEG_BYTES = 500 * 1024
PANORAMA_WALL_TIME_BUDGET_SECONDS = 1200
PANORAMA_DX_PUSH_TIMEOUT_SECONDS = 45
PANORAMA_HTTP_RETRYABLE_CODES = {408, 429, 502}

MATERIAL_TYPES = {"营销海报", "KT板", "展架", "台卡", "传单", "美食头图", "朋友圈海报", "短视频封面", "小程序 Banner", "五连图"}
TEMPLATE_MATERIAL_ALIAS = {
    "美食头图": "营销海报",
    "朋友圈海报": "营销海报",
    "短视频封面": "营销海报",
    "小程序 Banner": "营销海报",
    "五连图": "营销海报",
}
DEFAULT_SIZE_BY_MATERIAL = {
    "营销海报": "3:4",
    "KT板": "4:3",
    "展架": "2:5",
    "台卡": "4:3",
    "传单": "2:3",
    "美食头图": "1:1",
    "朋友圈海报": "9:16",
    "短视频封面": "9:16",
    "小程序 Banner": "16:9",
    "五连图": "20:3",
}
DIGITAL_MATERIAL_COMPOSITION_RULES = {
    "美食头图": "极小尺寸展示优先：超大主体、极简背景、避免小字、无需 QR。",
    "朋友圈海报": "社交传播优先：上方标题清晰，中部主视觉强记忆点，底部信息克制。",
    "短视频封面": "短视频封面优先：顶部标题安全区，底部操作栏遮挡区避让，主视觉居中偏上。",
    "小程序 Banner": "横幅承接优先：左侧或中心保留短标题，主体识别明确，避免密集信息。",
    "五连图": "连续长画布设计作品：采用 density-aware 连续长卷构图，按风格和请求推断文字安全区、菜品节奏、背景层次与切片衔接；不得固定为左文右菜模板。",
}
PRESET_RATIOS = {
    "1:1": (1, 1),
    "2:3": (2, 3),
    "2:5": (2, 5),
    "3:4": (3, 4),
    "4:5": (4, 5),
    "9:16": (9, 16),
    "3:2": (3, 2),
    "4:3": (4, 3),
    "5:4": (5, 4),
    "16:9": (16, 9),
    "20:3": (20, 3),
}
PANORAMA_API_WIDTH = 10240
PANORAMA_API_HEIGHT = 1536
PANORAMA_FINAL_WIDTH = 10240
PANORAMA_FINAL_HEIGHT = 1536
PANORAMA_SLICE_COUNT = 5
# ---- 单张长图载体生成参数（V3b 校准版式 mask 策略） ----
# gpt-image-2 硬约束：最大边 ≤ 3840，长短边比 ≤ 3:1。载体取 3:1 上限 3840×1280。
# 模型在载体上方画一条 20:3 的内容带（理想高度 = 3840/(20/3) = 576），下方留纯白。
# 后处理检测并裁出完整内容带，校验高度偏差后强制 resize 到最终 10240×1536，允许轻微变形。
PANORAMA_CARRIER_WIDTH = 3840
PANORAMA_CARRIER_HEIGHT = 1280
PANORAMA_TARGET_BAND_HEIGHT = round(PANORAMA_CARRIER_WIDTH / (PANORAMA_FINAL_WIDTH / PANORAMA_FINAL_HEIGHT))  # 576
# 内容带高度相对目标 576 的最大可接受偏差；超出则重新生成。
PANORAMA_BAND_TOLERANCE = 0.10
PANORAMA_BAND_MIN_HEIGHT = round(PANORAMA_TARGET_BAND_HEIGHT * (1 - PANORAMA_BAND_TOLERANCE))
PANORAMA_BAND_MAX_HEIGHT = round(PANORAMA_TARGET_BAND_HEIGHT * (1 + PANORAMA_BAND_TOLERANCE))
PANORAMA_BAND_DETECTOR_VERSION = "top_band_v4_tail_trim"
PANORAMA_EFFECTIVE_ROW_CONTENT_FRAC = 0.30
PANORAMA_TAIL_TRIM_MIN_ROWS = 12
PANORAMA_SEVERE_BAND_DEVIATION = 0.30
# 五连图候选质量预算：含首次生成，高度失败和宿主视觉质量失败共享同一预算。
PANORAMA_QUALITY_MAX_ATTEMPTS = 3
# 内容带偏差超阈值时，首次之外最多再重试的次数（兼容旧配置语义）。
PANORAMA_BAND_MAX_RETRIES = 2
# 内部版式 mask 使用校准后的短灰条。该高度是模型控制锚点，不是业务目标；业务目标仍是最终 20:3 长图。
PANORAMA_LAYOUT_MASK_CONTENT_HEIGHT = 396
PANORAMA_HOST_QUALITY_MAX_LEASE_SECONDS = 90.0
PANORAMA_HOST_QUALITY_POLL_INTERVAL_SECONDS = 5.0
# --- B2 策略：膨胀补偿参数（基于 24 次实测标定，模型平均膨胀系数 1.27x） ---
# 在 prompt 和引导图中声称 454px 内容带，模型膨胀后实际约 576px
PANORAMA_COMPENSATED_BAND_HEIGHT = 454
PANORAMA_COMPENSATED_TOP_Y = 413                # (1280 - 454) // 2
PANORAMA_COMPENSATED_BOTTOM_Y = 867             # 413 + 454
PANORAMA_COMPENSATED_MARGIN = 413               # 上下白边各 413px
# 重试时渐进补偿：偏高则缩小请求高度，偏低则放大
PANORAMA_RETRY_SHRINK_FACTOR = 0.85             # 偏高时：454 * 0.85 ≈ 386px
PANORAMA_RETRY_EXPAND_FACTOR = 1.12             # 偏低时：454 * 1.12 ≈ 508px
PRESET_CANVAS_SIZES = {
    "1:1": (1024, 1024),
    "2:3": (1024, 1536),
    "2:5": (1024, 2560),
    "3:4": (1024, 1360),
    "4:5": (1024, 1280),
    "9:16": (1024, 1824),
    "3:2": (1536, 1024),
    "4:3": (1360, 1024),
    "5:4": (1280, 1024),
    "16:9": (1824, 1024),
    "20:3": (PANORAMA_FINAL_WIDTH, PANORAMA_FINAL_HEIGHT),
}
PANORAMA_DISABLED_FEATURES = {
    "qr_code_not_needed": True,
    "disclaimer_overlay": False,
    "use_meituan_logo": False,
}
STYLE_HINTS = {
    "快乐萌系": "cute 3D rendered miniature scene, playful, cheerful, bright spring color, rounded commercial atmosphere, volumetric lighting, clay-like texture",
    "水墨国风": "modern Chinese ink wash blended with 3D miniature elements, elegant brush texture, premium restaurant atmosphere, depth of field",
    "金属复古": "retro metallic signage mood with 3D depth, high contrast, bold commercial energy, volumetric warm light",
    "柔美细圆": "soft rounded 3D visual mood, gentle pastel color palette, clean local-life promotion, dreamy bokeh background",
    "拼贴胶带": "collage tape meets 3D miniature, sticker textures, playful cutouts, handmade promotion mood with depth",
    "美团餐饮团购": (
        "Premium Meituan restaurant group-buying poster. "
        "VISUAL STYLE: 3D rendered miniature scene with volumetric lighting, realistic material textures (clay, felt, plastic, wood), "
        "depth of field, and cinematic warm color grading. NOT flat illustration — must have 3D depth, shadows, and volume. "
        "COLOR PALETTE: Choose a rich thematic palette that fits the campaign mood — "
        "the palette should feel fresh and surprising each time, not repetitive. "
        "Meituan yellow (#FFD100) should appear as an accent (mascot, logo, highlights), "
        "not as the dominant background color. "
        "SCENE DEPTH: Build a layered miniature world — tiny 3D shop fronts, street elements, miniature trees, food props, "
        "floating decorative elements that create visual richness and depth. "
        "The scene should feel like a premium toy diorama or Pixar-style miniature set, not a flat graphic poster."
    ),
}
# Per-variant style guidance that OVERRIDES the generic STYLE_HINTS["美团餐饮团购"].
# Each variant has a distinct emotional mood. Descriptions are intentionally POSITIVE and open-ended
# so the model can interpret and surprise — avoid over-specifying rendering technique.
# The model uses CONTEXT INFERENCE to decide which mood fits the campaign content.
VARIANT_STYLE_GUIDANCE: dict[str, str] = {
    "bustling_warmth": (
        "BUSTLING STREET WARMTH — the viewer is immersed in a lively, inviting food scene at its most magnetic moment. "
        "Warm golden light from overhead string bulbs and glowing shopfront signs. Wisps of steam and aromatic haze. "
        "Layers of depth: foreground textures (counter, steamer, bowls), midground activity, background glow receding into a lane. "
        "The whole frame radiates convivial warmth — 'I want to sit down and eat here right now'. "
        "Color palette: amber, warm honey, lantern orange, touches of neon. "
        "Typography: hand-painted shopfront feel, warm and bold. "
        "Overall mood: bustling, welcoming, appetite-triggering."
    ),
    "urban_chic": (
        "URBAN CHIC ELEGANCE — refined, metropolitan visual with confident restraint. "
        "Large areas of breathing space, a single hero element (one exquisite dish, a sculptural detail, or a bold geometric accent). "
        "Clean surfaces: polished stone, brushed metal, soft-focus glass reflections. "
        "Lighting is directional and cinematic — single key light creating dramatic shadow play. "
        "Color palette: sophisticated neutrals (warm grey, charcoal, cream) with one deliberate accent color from campaign context. "
        "Typography: minimal, modern, precise — confident in its restraint. "
        "Overall mood: premium, aspirational, quietly powerful."
    ),
    "poetic_leisure": (
        "POETIC LEISURE — unhurried, contemplative, inviting the viewer to slow down and savor. "
        "Ink-wash fluidity meets modern design: soft mist gradients, dappled morning light through a courtyard, "
        "tea steam curling into stillness, or seasonal flora framing the composition. "
        "Generous negative space that feels intentional. Textures: rice paper grain, ink bleeding, watercolor washes. "
        "Color palette: muted and poetic — celadon, warm ivory, ink grey, whispers of gold or terracotta. "
        "Typography: calligraphic grace — brush-inspired but contemporary. "
        "Overall mood: serene, cultured, timelessly beautiful."
    ),
    "bold_fiery": (
        "BOLD & FIERY — raw energy hitting the viewer like a wave of heat. "
        "Flames licking upward, oil splashing, chili peppers tumbling, or broth erupting with aromatics. "
        "Dynamic composition — action frozen at peak moment, slightly chaotic. "
        "Bold thick brush-stroke typography stamped with urgency. "
        "Color palette: deep crimson, molten orange, charred black, flashes of Meituan yellow. "
        "Textures: cast iron, volcanic stone, lacquered wood. "
        "Overall mood: visceral, powerful, appetite-on-fire."
    ),
    "coastal_breeze": (
        "COASTAL BREEZE — light, airy, sun-kissed. Open sky and sparkling water. "
        "Palette from sea and shore: cerulean blue, coral pink, sandy beige, seafoam green, sun-bleached white. "
        "Composition feels expansive — horizon lines, open terrace, seaside glimpse. "
        "Light is bright and diffused, casting soft shadows. "
        "Textures: weathered driftwood, crisp linen, sea-salt crystallization, translucent seafood freshness. "
        "Typography: clean and relaxed — like a hand-painted beach café sign. "
        "Overall mood: vacation, freedom, breezy indulgence."
    ),
    "nostalgic_lane": (
        "NOSTALGIC LANE — a loving reimagination of neighborhood food memories. "
        "Faded painted signboards, enamel cups, checkered tiles, hand-lettered menus on chalkboard walls. "
        "Not retro pastiche but 'home-cooked warmth meets graphic design craft'. "
        "Color palette: sun-bleached pastels over warm undertones — faded teal, dusty rose, aged cream, pops of vermillion. "
        "Lighting: late-afternoon golden hour streaming through a narrow alley. "
        "Typography: handmade quality — imperfect, friendly, trustworthy. "
        "Overall mood: familiar comfort, genuine warmth, time-honored taste."
    ),
    "youthful_buzz": (
        "YOUTHFUL BUZZ — maximum energy, zero pretension. "
        "Bold geometric color blocks colliding, sticker/tape/cutout textures, doodles mixing with crisp vector shapes. "
        "Layout breaks conventional grid — elements overlap, rotate, burst out of frames. "
        "Color palette: dopamine-inducing — electric lime, hot magenta, sunshine yellow, vivid violet — 2-3 that clash joyfully. "
        "Typography: chunky, rounded, playful — maybe tilted or bouncing. "
        "The poster feels social-media-native, makes you want to screenshot and share. "
        "Overall mood: fun, irreverent, instantly shareable."
    ),
    "exotic_fusion": (
        "EXOTIC FUSION — a sensory journey beyond the everyday. "
        "Rich layered patterns from cross-cultural aesthetics: tiles meeting botanicals, spice market hues blending with modern design, "
        "or tropical lushness layered with geometric precision. "
        "Color palette: saturated and complex — saffron gold, deep teal, terracotta, jungle green, unexpected accent pairings. "
        "Textures: hand-painted ceramics, woven textiles, ornate metalwork, lush tropical leaves. "
        "Composition feels abundant but harmonious — every corner rewards attention. "
        "Typography: decorative and expressive, echoing cultural richness. "
        "Overall mood: wanderlust, discovery, sensory abundance."
    ),
    "festive_prosperity": (
        "FESTIVE PROSPERITY — modern Chinese red-gold celebration with commercial clarity. "
        "Layer lantern warmth, auspicious paper-cut shapes, ribbons, subtle fireworks sparkle, and premium festive props. "
        "Palette: crimson, vermillion, warm gold, ivory, with Meituan yellow as a controlled accent. "
        "Typography is bold, proud, ceremonial, and highly readable. "
        "Overall mood: joyful, prosperous, high-conversion celebration."
    ),
    "organic_garden": (
        "ORGANIC GARDEN — natural healthy freshness that feels grown from the soil. "
        "Morning sunlight through herbs and leaves, linen, ceramic, woven baskets, soft shadows, and breathable space. "
        "Palette: sage green, basil, warm ivory, oat beige, soft clay. "
        "Typography is clean, rounded, breathable, and trustworthy. "
        "Overall mood: clean, healthy, sunlit, grounded."
    ),
    "midnight_glow": (
        "MIDNIGHT GLOW — intimate late-night warmth, not a noisy night market. "
        "Single-point warm lamps, soft neon reflections, dark wood, amber glass, quiet counter seating, gentle steam. "
        "Palette: charcoal, deep brown, amber, warm cream, muted red. "
        "Typography is restrained, warm, slightly cinematic. "
        "Overall mood: low-light, close, relaxed, after-dark refuge."
    ),
    "pastel_dream": (
        "PASTEL DREAM — soft dessert and afternoon-tea fantasy with polished sweetness. "
        "Macaron pink, lavender, butter cream, pale mint, pearl highlights, rounded cream shapes, airy bokeh. "
        "Typography is soft, rounded, elegant, and clear. "
        "Overall mood: gentle, sweet, creamy, romantic."
    ),
    "family_joy": (
        "FAMILY JOY — warm, safe, cheerful family dining with rounded commercial shapes. "
        "Soft rainbow accents, toy-like 3D props, weekend sunlight, friendly mascot-compatible space, clear hierarchy. "
        "Palette: sky blue, warm yellow, soft coral, fresh green, creamy white. "
        "Overall mood: parent-child friendly, bright, safe, playful but readable."
    ),
    "industrial_craft": (
        "INDUSTRIAL CRAFT — rugged handmade confidence with controlled craft texture. "
        "Concrete, blackened steel, kraft paper, chalk texture, dark wood, barrels, workshop lighting. "
        "Palette: charcoal, concrete grey, kraft brown, brass, cream. "
        "Typography is bold, chalky, stamped, or label-like. "
        "Overall mood: handcrafted, material, rugged, confident."
    ),
}


QR_HOST_ALIGNMENT_RULE = (
    "如果需要 QR，设计一个与整体风格融合的 QR 宿主道具，内部留一个纯白正方形区域"
    "（边长约短边 30%-35%），不要画 QR 点阵；宿主道具可以有创意外观和材质，"
    "但承载二维码的白色正方形内区及紧贴内区的外框必须正对镜头、轴对齐："
    "四条边与画布水平/垂直方向平行，0° 旋转，无倾斜、无透视、无斜放招牌或斜拍容器。"
    "白色内区必须是纯白正方形，内部不要放任何文字（如'扫码下单'）——文字标签如果需要，放在白色区域外面。"
    "QR 宿主道具的边框/外轮廓必须与紧邻背景有清晰的色彩或明暗反差（亮度差至少 40%），"
    "避免白色道具放在白色/浅色背景上融为一体。如果整体背景是白色或浅色，"
    "宿主道具外框应选择深色系材质（如深木纹、黑色金属、深灰哑光等），确保白色内区被清晰框定。"
)
ARTISTIC_QR_HOST_ALIGNMENT_RULE = (
    "如果需要 QR，在画面右下或中下区域预留一个干净白色二维码区域，"
    "使用简洁线条边框或扁平图形装饰；不要使用 3D 水晶球、木牌、托盘等立体宿主道具。"
    "白色内区必须正对镜头、轴对齐：四条边与画布水平/垂直方向平行，0° 旋转，无倾斜、无透视。"
    "白色内区必须是纯白正方形，内部不要放任何文字（如'扫码下单'）——文字标签如果需要，放在白色区域外面。"
    "边框必须与紧邻背景有清晰的明暗反差（亮度差至少 40%），确保检测算法能识别边界。"
)


GROUP_BUYING_KEYWORDS = (
    "价格", "价", "元", "¥", "￥", "折", "减", "券", "省",
    "优惠", "便宜", "特惠", "套餐", "团购", "下单", "扫码", "购买", "买单", "抢购", "立减",
)
PLATFORM_LOGO_VALUES = {"auto", "meituan", "dianping", "none"}
DIANPING_LOGO_KEYWORDS = (
    "大众点评",
    "点评logo",
    "点评标识",
    "点评平台",
    "点评团购",
    "点评必吃榜",
    "dianpinglogo",
    "dianping",
)
QR_TRIGGER_KEYWORDS = (
    "扫码", "扫一扫", "二维码", "小程序", "公众号", "加群", "进群", "加微信",
    "核销", "优惠码", "券码", "下单", "点餐", "买单",
)
SCAN_CTA_KEYWORDS = ("扫码", "扫一扫", "二维码", "小程序码")
REALISM_VALUES = {"realistic", "balanced", "artistic"}
TONE_VALUES = {"", "极简清冷", "热闹喜庆", "复古怀旧", "国潮中式", "清新自然", "奢华精致"}
ITERATION_SCOPES = {"background", "color_tone", "copy", "style"}
BUSINESS_INTENT_VALUES = {
    "new_product",
    "promotion",
    "festival",
    "brand_image",
    "social_spread",
    "daily_attract",
    "membership",
    "recruitment",
}
BUSINESS_INTENT_LABELS = {
    "new_product": "推新品/上新",
    "promotion": "促销打折/引流",
    "festival": "节日活动",
    "brand_image": "品牌形象/宣传",
    "social_spread": "朋友圈/种草传播",
    "daily_attract": "日常到店引流",
    "membership": "会员复购/私域",
    "recruitment": "招聘/招商",
}
BUSINESS_INTENT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("festival", ("春节", "中秋", "七夕", "母亲节", "父亲节", "儿童节", "店庆", "周年", "周年庆", "节日", "情人节", "国庆", "元旦", "端午")),
    ("new_product", ("新品", "上新", "新菜", "限定", "首发", "主厨新作", "季节限定", "尝鲜")),
    ("promotion", ("打折", "促销", "满减", "团购", "优惠", "折扣", "开业", "酬宾", "特价", "立减", "秒杀")),
    ("brand_image", ("品牌", "形象", "宣传", "升级", "门店形象", "招商", "加盟")),
    ("social_spread", ("朋友圈", "种草", "打卡", "社群", "分享", "返图", "裂变", "小红书")),
    ("membership", ("会员", "积分", "储值", "复购", "老客", "会员日", "私域")),
    ("recruitment", ("招聘", "招人", "岗位", "店员", "服务员", "后厨", "加盟商")),
    ("daily_attract", ("日常", "每日", "午市", "午餐", "工作日", "引流", "到店", "套餐", "堂食")),
)
LEGACY_SCENE_INTENT_MAP = {
    "堂食引流": "daily_attract",
    "门店活动": "daily_attract",
    "桌面转化": "daily_attract",
    "套餐推荐": "daily_attract",
    "外卖转化": "daily_attract",
    "社区触达": "daily_attract",
    "节日营销": "festival",
    "新店开业": "promotion",
    "团购": "promotion",
    "优惠": "promotion",
    "促销": "promotion",
    "品牌宣传": "brand_image",
    "社交种草": "social_spread",
    "新品上市": "new_product",
    "会员引导": "membership",
}
INTENT_STYLE_ORDER = {
    "new_product": ["clean_hero", "natural_earth", "urban_chic", "korean_cream", "oversize_type", "dark_premium"],
    "promotion": ["bold_split", "dynamic_angle", "oversize_type", "collage_pop", "street_warm", "festive_red", "neon_night"],
    "festival": ["festive_red", "illustration_flat", "family_joy", "collage_pop", "ink_oriental", "dark_premium"],
    "brand_image": ["clean_hero", "urban_chic", "natural_earth", "korean_cream", "ink_oriental", "dark_premium"],
    "social_spread": ["collage_pop", "dynamic_angle", "oversize_type", "street_warm", "illustration_flat", "bold_split", "neon_night"],
    "daily_attract": ["street_warm", "dynamic_angle", "bold_split", "oversize_type", "retro_poster", "natural_earth"],
    "membership": ["clean_hero", "urban_chic", "korean_cream", "natural_earth", "oversize_type", "dark_premium"],
    "recruitment": ["clean_hero", "natural_earth", "family_joy", "bold_split"],
}
APPETITE_BRIGHT_STYLE_BASELINE = {
    "clean_hero": 9.0,
    "natural_earth": 8.0,
    "urban_chic": 7.0,
    "korean_cream": 7.0,
    "coastal_breeze": 7.0,
    "outdoor_picnic": 6.0,
    "family_joy": 6.0,
    "collage_pop": 5.0,
    "dynamic_angle": 4.0,
    "oversize_type": 4.0,
    "street_warm": 3.0,
    "latin_fiesta": 3.0,
    "sweet_dream": 3.0,
    "zen_tea_modern": 2.0,
    "poetic_leisure": 2.0,
}
DARK_APPETITE_DEFAULT_PENALTY = {
    "dark_premium": -8.0,
    "neon_night": -8.0,
    "industrial_craft": -10.0,
    "cyber_gaming": -10.0,
}
EXPLICIT_DARK_STYLE_KEYWORDS = (
    "夜宵", "深夜", "酒馆", "小酒馆", "居酒屋", "精酿", "酒吧", "威士忌",
    "暗调", "暗色", "黑金", "霓虹", "夜场", "夜店", "高端暗场", "私宴", "fine dining",
)
DARK_STYLE_FOOD_PROTECTION = {
    "dark_premium",
    "neon_night",
    "industrial_craft",
    "cyber_gaming",
}
APPETITE_COLOR_NEGATIVE_TERMS = [
    "no black-red dominated palette",
    "no muddy dark red background",
    "no underexposed food",
]
CHINESE_CATEGORY_KEYWORDS = (
    "中餐", "火锅", "川", "湘", "粤", "鲁", "闽", "东北", "西北", "云南", "广西",
    "贵州", "贵阳", "云贵川", "江西", "湖北", "江南", "潮汕", "老字号", "家常", "粉面", "饺子", "包子", "茶馆", "新中式",
    "烧烤", "烤肉", "麻辣", "砂锅", "私房",
)
NON_CHINESE_CATEGORY_KEYWORDS = (
    "西餐", "日料", "韩餐", "墨西哥", "拉美", "东南亚", "泰餐", "印度", "汉堡",
    "披萨", "咖啡", "甜品", "轻食", "沙拉",
)
STYLE_CONFLICT_PAIRS = {
    frozenset(("dark_premium", "neon_night")),
    frozenset(("street_warm", "collage_pop")),
    frozenset(("bold_split", "dynamic_angle")),    # 同为高冲击促销，避免两个硬核风格撞车
    frozenset(("oversize_type", "bold_split")),    # 同为强视觉重量，都很"重"
}
SEASON_KEYWORDS = {
    "春季": ("春", "春季", "春天", "春日", "樱花", "踏青"),
    "夏季": ("夏", "夏季", "夏天", "夏日", "盛夏", "解暑", "清凉", "清爽", "冰爽"),
    "秋季": ("秋", "秋季", "秋天", "秋日", "金秋", "丰收", "桂花"),
    "冬季": ("冬", "冬季", "冬天", "冬日", "暖冬", "围炉", "热腾腾"),
}
MONTH_TO_SEASON = {
    3: "春季", 4: "春季", 5: "春季",
    6: "夏季", 7: "夏季", 8: "夏季",
    9: "秋季", 10: "秋季", 11: "秋季",
    12: "冬季", 1: "冬季", 2: "冬季",
}
SICHUAN_HUNAN_FRESH_KEYWORDS = (
    "酸菜鱼", "酸汤鱼", "柠檬鱼", "藤椒鱼", "清江鱼", "鱼片", "鱼汤", "藕汤", "莲藕汤", "莲藕",
    "米粉", "桂林米粉", "清爽", "清甜", "解暑",
)
SICHUAN_HUNAN_CLASSIC_KEYWORDS = (
    "巴蜀", "宫保", "宫保鸡丁", "回锅肉", "一城", "非遗", "老店",
    "酒家", "宴席", "传承", "地道", "家常", "招牌川菜", "招牌湘菜",
    "云贵川", "桂林", "赣味", "楚味", "鄂味", "客家",
)
SICHUAN_HUNAN_IMPACT_KEYWORDS = (
    "爆炒", "现炒", "锅气", "辣子鸡", "小炒肉", "小炒", "江西小炒", "赣南小炒", "剁椒", "剁椒鱼头",
    "干锅", "毛血旺", "麻辣", "香辣", "下饭", "热辣", "爆款", "啤酒鸭", "藜蒿炒腊肉",
)
SICHUAN_HUNAN_RICH_DISH_KEYWORDS = (
    "小龙虾", "活虾", "虾", "水煮鱼", "水煮", "沸腾鱼", "烤鱼", "螺蛳粉", "腊肉",
    "夜宵", "宵夜", "酒馆", "高端", "高级", "私房",
)
REGIONAL_APPETITE_CUISINE_KEYWORDS = (
    "川湘菜", "湘菜", "川菜", "四川菜", "湖南菜",
    "云贵川", "云贵川菜", "云南菜", "贵州菜", "黔菜",
    "广西菜", "桂菜", "江西菜", "赣菜", "湖北菜", "鄂菜", "楚菜",
)
PROTECTED_IP_REPLACEMENTS = (
    (re.compile(r"(?i)\bFIFA\s*World\s*Cup\b"), "球赛"),
    (re.compile(r"(?i)\bWorld\s*Cup\b"), "球赛"),
    (re.compile(r"(?i)\bFIFA\b"), "球赛"),
    (re.compile(r"(?:世界杯|世界盃)(?:官方)?(?:会徽|标志|吉祥物|主题曲|口号)(?:氛围|风格)?"), "球赛氛围"),
    (re.compile(r"(?:卡塔尔|足球|男足|女足)?世界杯"), "球赛"),
    (re.compile(r"世界盃"), "球赛"),
    (re.compile(r"大力神杯(?:同款)?"), "看球"),
    (re.compile(r"(?:La['’`]?eeb|拉伊卜|Fuleco|Zakumi)", re.IGNORECASE), "球赛氛围"),
    (re.compile(r"(?i)(?<![A-Za-z])National\s+Basketball\s+Association(?![A-Za-z])"), "篮球赛"),
    (re.compile(r"(?i)(?<![A-Za-z])NBA(?![A-Za-z])"), "篮球赛"),
    (re.compile(r"(?i)(?<![A-Za-z])CBA(?![A-Za-z])"), "篮球赛"),
    (re.compile(r"(?i)\bUEFA\s+Champions\s+League\b|\bChampions\s+League\b"), "球赛"),
    (re.compile(r"欧冠|欧洲杯|英超|中超|亚冠|UEFA", re.IGNORECASE), "球赛"),
    (re.compile(r"(?i)(?<![A-Za-z])NFL(?![A-Za-z])|(?<![A-Za-z])Super\s+Bowl(?![A-Za-z])"), "橄榄球赛"),
    (re.compile(r"奥运五环|奥林匹克五环"), "运动会"),
    (re.compile(r"奥运会|奥林匹克|奥运|亚运会|亚运|IOC", re.IGNORECASE), "运动会"),
    (re.compile(r"(?i)\bWalt\s+Disney\b|\bDisney\b"), "童趣"),
    (re.compile(r"迪士尼(?:乐园|城堡|主题)?"), "童趣"),
    (re.compile(r"(?i)\bMickey\s+Mouse\b|\bMickey\b"), "童趣角色"),
    (re.compile(r"米奇|米老鼠|唐老鸭|高飞"), "童趣角色"),
    (re.compile(r"(?i)\bMarvel\b|\bAvengers\b"), "超级英雄"),
    (re.compile(r"漫威|复仇者联盟|钢铁侠|蜘蛛侠|美国队长|雷神"), "超级英雄"),
    (re.compile(r"(?i)\bStar\s+Wars\b"), "太空冒险"),
    (re.compile(r"星球大战|绝地武士|光剑"), "太空冒险"),
    (re.compile(r"(?i)\bHarry\s+Potter\b|\bHogwarts\b"), "魔法主题"),
    (re.compile(r"哈利波特|霍格沃茨"), "魔法主题"),
    (re.compile(r"(?i)\bPok[eé]mon\b|\bPikachu\b"), "萌趣"),
    (re.compile(r"宝可梦|皮卡丘"), "萌趣"),
    (re.compile(r"(?i)\bHello\s+Kitty\b"), "可爱主题"),
    (re.compile(r"凯蒂猫|哆啦A梦|机器猫"), "童趣主题"),
    (re.compile(r"海贼王|航海王|路飞"), "冒险主题"),
)
PROTECTED_IP_NEGATIVE = [
    "no protected IP logos or characters",
    "no FIFA/World Cup/Olympic/NBA wording",
    "no World Cup trophy or Olympic rings",
    "no Disney characters/castle",
    "no NBA logo/team jerseys",
    "no Marvel/Star Wars/Harry Potter/Pokemon/Hello Kitty/anime character likeness",
    "no World Cup emblem/mascot/team jersey",
]
PROTECTED_IP_VISUAL_RULE = (
    "受保护 IP 合规：严禁出现世界杯/FIFA/NBA/奥运/迪士尼/漫威等专有名称、logo、角色、奖杯、会徽、队徽球衣；"
    "统一用看球/球赛/篮球赛/运动会/童趣/超级英雄等中性表达。"
)
STYLE_FACTOR_OPTIONS = {
    "lighting_accent": [
        "golden_hour", "overcast", "neon_tint", "candlelight", "blue_hour",
        "soft_window_light", "lantern_glow", "studio_flash", "morning_haze",
    ],
    "texture_emphasis": [
        "matte", "glossy", "frosted", "rough_grain", "paper_cut",
        "linen_weave", "ceramic_glaze", "brushed_metal", "woodgrain",
    ],
    "color_shift": [
        "warm_10", "cool_10", "saturated_15", "neutral", "pastel_soft",
        "deep_contrast", "muted_earth", "fresh_green", "red_gold_accent",
    ],
    "composition_accent": [
        "asymmetric_balance", "centered_hero", "diagonal_flow", "frame_within_frame",
        "layered_depth", "top_heavy_title", "bottom_anchor", "floating_elements",
        "split_scene",
    ],
}

# Keywords that indicate a premium/atmosphere scene where mascot is inappropriate.
# When mascot_mode=auto and ANY of these appear in title/theme/store/style, skip sending mascot image.
MASCOT_SKIP_KEYWORDS = (
    "桃花源", "水墨", "国风", "禅", "雅", "高端", "精致", "极简", "简约",
    "文艺", "格调", "品味", "轻奢", "私房", "隐", "境", "苑", "阁", "轩",
)
MASCOT_AUTO_INCLUDE_KEYWORDS = (
    "袋鼠", "吉祥物", "IP", "ip", "亲子", "儿童", "小朋友", "家庭",
    "萌", "可爱", "卡通", "欢乐", "快乐", "family", "kid", "kids",
)

# Max long-edge (px) for mascot reference image sent to the model.
# The original sheet is 2294×1324; at 512px the model still reads the character clearly.
MASCOT_REF_MAX_PX = 512


class SkillError(Exception):
    pass


@dataclass
class RuntimeAssets:
    asset_dir: Path
    fonts: dict[str, Path]
    brand: dict[str, Path]
    selected_logo_path: Path | None
    selected_white_logo_path: Path | None  # white/inverted variant for dark backgrounds
    mascot_path: Path | None
    mascot_ref_path: Path | None  # compressed version sent to model (None = skipped)
    qr_path: Path | None
    qr_original_path: str
    qr_original_sha256: str
    qr_asset_sha256: str
    food_images: list[Path]
    generation_reference_images: list[str]
    generation_reference_image_roles: list[str]
    warnings: list[str] = field(default_factory=list)


@dataclass
class GeneratedVariant:
    index: int
    template: dict[str, Any]
    layout: dict[str, Any]
    prompt: str
    prompt_path: Path
    scene_image_path: Path
    final_no_qr_path: Path | None
    final_path: Path | None
    png_path: Path | None
    raw_response_path: Path | None
    qr_placement: dict[str, Any] | None = None
    qr_placement_path: Path | None = None
    delivery_slices: list[Path] = field(default_factory=list)
    provider_warnings: list[str] = field(default_factory=list)
    display_index: int | None = None
    source_index: int | None = None
    delivery_material_path: Path | None = None
    completed_at: str = ""


@dataclass
class TemplateSelection:
    templates: list[dict[str, Any]]
    audit: dict[str, Any]


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SkillError(f"输入文件不存在: {path}") from None
    except json.JSONDecodeError as exc:
        raise SkillError(f"JSON 格式错误: {path}: {exc}") from None


def read_reference_json(name: str) -> dict[str, Any]:
    path = SKILL_ROOT / "references" / name
    if not path.exists():
        return {}
    return read_json(path)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_space(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip())


def compact_text(value: Any) -> str:
    text = normalize_space(value)
    return re.sub(r"[\s，,。.!！?？、:：;；'\"""''—_/|\\-]+", "", text)


def normalize_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [normalize_space(value)] if normalize_space(value) else []
    if isinstance(value, list):
        return [normalize_space(item) for item in value if normalize_space(item)]
    return []


def sanitize_protected_ip_text(value: Any) -> tuple[str, bool]:
    """Replace protected IP terms with neutral promotional wording."""
    text = normalize_space(value)
    if not text:
        return "", False
    sanitized = text
    for pattern, replacement in PROTECTED_IP_REPLACEMENTS:
        sanitized = pattern.sub(replacement, sanitized)
    sanitized = normalize_space(sanitized)
    return sanitized, sanitized != text


def sanitize_protected_ip_value(value: Any) -> tuple[Any, bool]:
    if value is None:
        return value, False
    if isinstance(value, str):
        return sanitize_protected_ip_text(value)
    if isinstance(value, list):
        changed = False
        sanitized_items: list[Any] = []
        for item in value:
            sanitized_item, item_changed = sanitize_protected_ip_value(item)
            sanitized_items.append(sanitized_item)
            changed = changed or item_changed
        return sanitized_items, changed
    if isinstance(value, dict):
        changed = False
        sanitized_dict: dict[str, Any] = {}
        for key, item in value.items():
            sanitized_item, item_changed = sanitize_protected_ip_value(item)
            sanitized_dict[key] = sanitized_item
            changed = changed or item_changed
        return sanitized_dict, changed
    return value, False


def has_qr_trigger_text(req: dict[str, Any]) -> bool:
    blob = request_text_blob(req)
    return any(keyword in blob for keyword in QR_TRIGGER_KEYWORDS)


def has_protected_ip_risk_context(req: dict[str, Any]) -> bool:
    compliance = req.get("_compliance", {}) if isinstance(req.get("_compliance"), dict) else {}
    if compliance.get("protected_ip_sanitized"):
        return True
    copy_info = req.get("copy", {}) if isinstance(req.get("copy"), dict) else {}
    parts = [
        request_text_blob(req),
        copy_info.get("selected_text", ""),
        " ".join(copy_info.get("generated_candidates", []) if isinstance(copy_info.get("generated_candidates"), list) else []),
    ]
    blob = compact_text(" ".join(str(part) for part in parts if part))
    return any(
        keyword in blob
        for keyword in (
            "看球", "球赛", "足球", "篮球赛", "观赛", "决赛", "赛事", "运动会",
            "童趣", "卡通", "超级英雄", "太空冒险", "魔法主题", "萌趣", "可爱主题", "冒险主题",
        )
    )


def detect_business_intents(req: dict[str, Any]) -> list[str]:
    scene = req.get("scene", {}) if isinstance(req.get("scene"), dict) else {}
    explicit = normalize_space(scene.get("business_intent"))
    if explicit:
        if explicit not in BUSINESS_INTENT_VALUES:
            raise SkillError(f"不支持的 scene.business_intent: {explicit}")
        return [explicit]

    blob = request_text_blob(req)
    detected: list[str] = []
    for intent, keywords in BUSINESS_INTENT_KEYWORDS:
        if any(keyword in blob for keyword in keywords):
            detected.append(intent)
    if detected:
        return detected

    legacy_intent = normalize_space(scene.get("intent"))
    mapped = LEGACY_SCENE_INTENT_MAP.get(legacy_intent)
    if mapped:
        return [mapped]
    return ["daily_attract"]


def default_title_for_request(req: dict[str, Any]) -> str:
    store = normalize_space((req.get("store") or {}).get("name"))
    category = normalize_space((req.get("store") or {}).get("category"))
    if store:
        return f"{store}到店尝鲜"
    if category:
        return f"{category}到店尝鲜"
    return ""


def choose_style_factors(raw: Any, enable_random: bool = False) -> dict[str, str]:
    factors: dict[str, str] = {}
    if isinstance(raw, dict):
        for key, options in STYLE_FACTOR_OPTIONS.items():
            value = normalize_space(raw.get(key))
            if value:
                factors[key] = value if value in options else value
        return factors
    if not enable_random:
        return factors
    for key, options in STYLE_FACTOR_OPTIONS.items():
        factors[key] = random.choice(options)
    return factors


def request_text_blob(req: dict[str, Any]) -> str:
    copy_info = req.get("copy", {}) if isinstance(req.get("copy"), dict) else {}
    parts = [
        req.get("user_original_prompt", ""),
        req.get("title", ""),
        req.get("campaign", {}).get("theme", ""),
        req.get("campaign", {}).get("offer", ""),
        req.get("campaign", {}).get("cta", ""),
        req.get("store", {}).get("category", ""),
        req.get("store", {}).get("city", ""),
        req.get("campaign", {}).get("season", ""),
        copy_info.get("selected_text", ""),
        " ".join(copy_info.get("dimensions", []) if isinstance(copy_info.get("dimensions"), list) else []),
        " ".join(copy_info.get("generated_candidates", []) if isinstance(copy_info.get("generated_candidates"), list) else []),
        req.get("style", {}).get("season_atmosphere", ""),
        req.get("style", {}).get("notes", ""),
    ]
    for product in req.get("products", []):
        parts.extend([product.get("name", ""), product.get("price", ""), product.get("description", "")])
    return compact_text(" ".join(str(part) for part in parts if part))


def _has_qr_asset(assets: dict[str, Any]) -> bool:
    return bool(
        assets.get("qr_code_path")
        or assets.get("qr_asset_path")
        or assets.get("qr_code_data_url")
        or assets.get("qr_code_attachment")
    )


def is_group_buying_context(req: dict[str, Any]) -> bool:
    blob = request_text_blob(req)
    if any(keyword in blob for keyword in GROUP_BUYING_KEYWORDS):
        return True
    return any(normalize_space(product.get("price")) for product in req.get("products", []))


def has_dianping_platform_context(req: dict[str, Any]) -> bool:
    blob = request_text_blob(req).lower()
    return any(keyword in blob for keyword in DIANPING_LOGO_KEYWORDS)


def select_logo_asset(req: dict[str, Any]) -> tuple[Path | None, Path | None, str, str]:
    """Return (standard_logo_path, white_logo_path, label, reason).

    The white variant is used when the poster background is dark.
    """
    assets = req.get("assets", {}) if isinstance(req.get("assets"), dict) else {}
    platform_logo = normalize_space(assets.get("platform_logo") or "auto")
    if platform_logo not in PLATFORM_LOGO_VALUES:
        platform_logo = "auto"
    if platform_logo == "none":
        return None, None, "不展示平台标识", "用户明确选择不展示平台 Logo"
    if platform_logo == "dianping":
        return DIANPING_LOGO, DIANPING_WHITE_LOGO, "大众点评标识", "用户明确选择大众点评/点评平台标识"
    if platform_logo == "auto" and has_dianping_platform_context(req):
        return DIANPING_LOGO, DIANPING_WHITE_LOGO, "大众点评标识", "用户明确提到大众点评/点评平台语义"
    if assets.get("_force_group_buying_logo") or is_group_buying_context(req):
        return MEITUAN_GROUP_BUYING_LOGO, MEITUAN_GROUP_WHITE_BUYING_LOGO, "美团团购标识", "物料强调价格、下单、优惠、扫码或购买转化"
    return MEITUAN_LOGO, MEITUAN_WHITE_LOGO, "美团标识", "普通餐饮信息或氛围场景"


def _parse_reference_image_index(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    text = normalize_space(value)
    if not text:
        return None
    if re.fullmatch(r"\d+", text):
        parsed = int(text)
        return parsed if parsed > 0 else None
    return None


def dish_reference_items(req: dict[str, Any]) -> list[dict[str, Any]]:
    """Return products explicitly mapped to reference image slots."""
    assets = req.get("assets", {}) if isinstance(req.get("assets"), dict) else {}
    reference_images = assets.get("reference_images") or []
    if not isinstance(reference_images, list):
        return []

    items: list[dict[str, Any]] = []
    for product in req.get("products", []):
        if not isinstance(product, dict):
            continue
        ref_index = product.get("reference_image_index")
        if not isinstance(ref_index, int) or isinstance(ref_index, bool):
            continue
        if ref_index < 1 or ref_index > len(reference_images):
            continue
        item = {
            "name": normalize_space(product.get("name")),
            "reference_image_index": ref_index,
            "reference_label": f"reference photo #{ref_index}",
            "reference_image": reference_images[ref_index - 1],
        }
        description = normalize_space(product.get("description"))
        if description:
            item["description"] = description
        price = normalize_space(product.get("price"))
        if price:
            item["price"] = price
        items.append(item)
    return items


def dish_label_items(req: dict[str, Any]) -> list[dict[str, Any]]:
    """Return mapped dish references that should render visible dish-name labels."""
    return [item for item in dish_reference_items(req) if normalize_space(item.get("name"))]


# ---------------------------------------------------------------------------
# Request normalization
# ---------------------------------------------------------------------------

def normalize_request(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    warnings_list: list[str] = []
    req = dict(raw)
    protected_ip_sanitized = False

    def sanitize_ip_value(value: Any) -> Any:
        nonlocal protected_ip_sanitized
        sanitized, changed = sanitize_protected_ip_value(value)
        protected_ip_sanitized = protected_ip_sanitized or changed
        return sanitized

    material_type = normalize_space(req.get("type"))
    if not material_type:
        raise SkillError("生成前必须先追问物料类型，可选：营销海报、KT板、展架、台卡、传单。")
    if material_type not in MATERIAL_TYPES:
        raise SkillError(f"不支持的物料类型: {material_type}")
    req["type"] = material_type

    size = dict(req.get("size") or {})
    preset = normalize_space(size.get("preset"))
    custom_px = size.get("custom_px")
    custom_mm = size.get("custom_mm")
    if not preset and not custom_px and not custom_mm:
        preset = DEFAULT_SIZE_BY_MATERIAL[material_type]
        warnings_list.append(f"未提供尺寸，已按物料类型 {material_type} 默认使用 {preset}。")
    if preset and preset not in PRESET_RATIOS:
        raise SkillError(f"不支持的尺寸比例: {preset}")
    size["preset"] = preset
    if custom_px:
        if not isinstance(custom_px, dict):
            raise SkillError("size.custom_px 必须包含 width 和 height")
        try:
            width_px = int(round(float(custom_px["width"])))
            height_px = int(round(float(custom_px["height"])))
        except (KeyError, TypeError, ValueError):
            raise SkillError("size.custom_px.width/height 必须是数字") from None
        if width_px <= 0 or height_px <= 0:
            raise SkillError("size.custom_px.width/height 必须大于 0")
        size["custom_px"] = {"width": width_px, "height": height_px}
    if custom_mm:
        if not isinstance(custom_mm, dict):
            raise SkillError("size.custom_mm 必须包含 width 和 height")
        try:
            width_mm = float(custom_mm["width"])
            height_mm = float(custom_mm["height"])
        except (KeyError, TypeError, ValueError):
            raise SkillError("size.custom_mm.width/height 必须是数字") from None
        if width_mm <= 0 or height_mm <= 0:
            raise SkillError("size.custom_mm.width/height 必须大于 0")
        size["custom_mm"] = {"width": width_mm, "height": height_mm}
    req["size"] = size

    req["store"] = dict(req.get("store") or {})
    req["store"].pop("city", None)
    req["store"]["name"] = normalize_space(req["store"].get("name"))
    req["store"]["category"] = normalize_space(req["store"].get("category"))
    req["store"] = sanitize_ip_value(req["store"])

    req["campaign"] = dict(req.get("campaign") or {})
    for key in ("theme", "offer", "cta", "season"):
        req["campaign"][key] = normalize_space(req["campaign"].get(key))
    req["campaign"] = sanitize_ip_value(req["campaign"])

    copy_info = dict(req.get("copy") or {})
    copy_info["dimensions"] = normalize_list(copy_info.get("dimensions") or copy_info.get("dimension"))
    copy_info["generated_candidates"] = [
        normalize_space(item)
        for item in (copy_info.get("generated_candidates") or copy_info.get("candidates") or [])
        if normalize_space(item)
    ] if isinstance(copy_info.get("generated_candidates") or copy_info.get("candidates") or [], list) else []
    copy_info["selected_text"] = normalize_space(copy_info.get("selected_text"))
    copy_info = sanitize_ip_value(copy_info)
    req["copy"] = copy_info

    title = normalize_space(req.get("title"))
    if not title:
        title = copy_info["selected_text"] or req["campaign"].get("theme") or default_title_for_request(req)
        if title:
            warnings_list.append("title 未提供，已从 copy.selected_text、campaign.theme 或门店信息回填。")
    if not title:
        raise SkillError("title 为必填字段；可由 Agent 从候选文案或门店场景回填。")
    req["title"] = sanitize_ip_value(title)
    if "title_style" in req:
        req["title_style"] = sanitize_ip_value(normalize_space(req.get("title_style")))

    products = req.get("products") or []
    if not isinstance(products, list):
        raise SkillError("products 必须是数组，可为空")
    normalized_products: list[dict[str, Any]] = []
    for product_index, product in enumerate(products, start=1):
        if not isinstance(product, dict):
            continue
        item: dict[str, Any] = {
            "name": normalize_space(product.get("name")),
            "price": normalize_space(product.get("price")),
            "description": normalize_space(product.get("description")),
        }
        if "reference_image_index" in product:
            ref_index = _parse_reference_image_index(product.get("reference_image_index"))
            if ref_index is None:
                warnings_list.append(
                    f"products[{product_index}].reference_image_index 无效，已忽略；请使用从 1 开始的参考图序号。"
                )
            else:
                item["reference_image_index"] = ref_index
        normalized_products.append(item)
    req["products"] = sanitize_ip_value(normalized_products)

    style = dict(req.get("style") or {})
    style["name"] = normalize_space(style.get("name") or "美团餐饮团购")
    style["allow_ai_text"] = bool(style.get("allow_ai_text", True))
    role = normalize_space(style.get("ai_text_role") or "marketing_display")
    if role not in {"marketing_display", "pure_atmosphere"}:
        warnings_list.append(f"未知 ai_text_role={role}，已回退为 marketing_display")
        role = "marketing_display"
    style["ai_text_role"] = role
    style["cuisine_tag"] = normalize_space(style.get("cuisine_tag"))
    style["city_tag"] = normalize_space(style.get("city_tag"))
    style["typography_profile"] = normalize_space(style.get("typography_profile"))
    tone = normalize_space(style.get("tone"))
    if tone not in TONE_VALUES:
        warnings_list.append(f"未知 style.tone={tone}，已忽略")
        tone = ""
    style["tone"] = tone
    realism = normalize_space(style.get("realism") or "balanced")
    if realism not in REALISM_VALUES:
        warnings_list.append(f"未知 style.realism={realism}，已回退为 balanced")
        realism = "balanced"
    # Auto-switch to realistic when reference images are provided and user didn't explicitly choose
    if not style.get("realism") and req.get("assets", {}).get("reference_images"):
        realism = "realistic"
    style["realism"] = realism
    style["style_factors"] = choose_style_factors(
        style.get("style_factors"),
        enable_random=bool(style.get("enable_style_factors", False)),
    )
    req["style"] = sanitize_ip_value(style)

    req["brand_profile"] = dict(req.get("brand_profile") or {})
    req["brand_profile"]["brand_name"] = normalize_space(req["brand_profile"].get("brand_name"))
    req["brand_profile"]["logo_position"] = normalize_space(req["brand_profile"].get("logo_position"))
    req["brand_profile"]["brand_keywords"] = normalize_list(req["brand_profile"].get("brand_keywords"))
    req["brand_profile"]["primary_colors"] = normalize_list(req["brand_profile"].get("primary_colors"))
    req["brand_profile"]["visual_lock"] = bool(req["brand_profile"].get("visual_lock"))
    req["brand_profile"] = sanitize_ip_value(req["brand_profile"])
    req["scene"] = sanitize_ip_value(dict(req.get("scene") or {}))
    business_intents = detect_business_intents(req)
    req["scene"]["business_intent"] = business_intents[0]
    req["scene"]["business_intents"] = business_intents

    iteration = dict(req.get("iteration") or {})
    modify_scope = normalize_space(iteration.get("modify_scope"))
    if modify_scope:
        if modify_scope not in ITERATION_SCOPES:
            raise SkillError(f"iteration.modify_scope 不支持: {modify_scope}，可选：{sorted(ITERATION_SCOPES)}")
        iteration["modify_scope"] = modify_scope
    iteration["base_prompt_path"] = normalize_space(iteration.get("base_prompt_path"))
    preserve = iteration.get("preserve") or []
    iteration["preserve"] = normalize_list(preserve)
    req["iteration"] = iteration

    assets = dict(req.get("assets") or {})
    if material_type == "五连图":
        assets.update(PANORAMA_DISABLED_FEATURES)
    assets["qr_code_path"] = normalize_space(assets.get("qr_code_path"))
    assets["qr_code_data_url"] = normalize_space(assets.get("qr_code_data_url"))
    assets["qr_code_attachment"] = normalize_space(assets.get("qr_code_attachment"))
    if material_type == "五连图":
        assets["qr_code_path"] = ""
        assets["qr_code_data_url"] = ""
        assets["qr_code_attachment"] = ""
    assets["store_logo_path"] = normalize_space(assets.get("store_logo_path"))
    assets["brand_logo_path"] = normalize_space(assets.get("brand_logo_path"))
    assets["panorama_slice_requested"] = bool(assets.get("panorama_slice_requested"))
    assets["qr_code_not_needed"] = bool(
        assets.get("qr_code_not_needed")
        or assets.get("qr_code_declined")
        or assets.get("no_qr")
    )
    has_qr = _has_qr_asset(assets)
    conversion_needs_qr = has_qr_trigger_text(req) or is_group_buying_context(req)
    if not has_qr and not assets["qr_code_not_needed"] and not conversion_needs_qr:
        assets["qr_code_not_needed"] = True
        warnings_list.append("未提及二维码且未命中团购/优惠/扫码转化语义，Layer 1 默认生成不含二维码版本。")
    if not has_qr and not assets["qr_code_not_needed"]:
        raise SkillError("默认需要二维码。请先让用户上传二维码或提供附件路径；若用户明确不要二维码，请设置 assets.qr_code_not_needed=true。")
    if assets["qr_code_not_needed"]:
        scan_text = " ".join([
            req.get("title", ""),
            req.get("campaign", {}).get("theme", ""),
            req.get("campaign", {}).get("offer", ""),
            req.get("campaign", {}).get("cta", ""),
            req.get("copy", {}).get("selected_text", ""),
        ])
        if any(keyword in scan_text for keyword in SCAN_CTA_KEYWORDS):
            warnings_list.append("qr_code_not_needed=true 但文案/CTA 包含扫码语义；建议 Agent 修正文案或引导用户补充二维码。")

    assets["reference_images"] = assets.get("reference_images") or []
    if not isinstance(assets["reference_images"], list):
        raise SkillError("assets.reference_images 必须是数组")
    assets["reference_images"] = [normalize_space(p) for p in assets["reference_images"] if normalize_space(p)]
    if material_type == "五连图":
        ref_count = len(assets["reference_images"])
        if ref_count > 7:
            raise SkillError(f"五连图最多支持 7 张实拍图（当前 {ref_count} 张）")
    # --- style_reference_images normalization ---
    assets["style_reference_images"] = assets.get("style_reference_images") or []
    if not isinstance(assets["style_reference_images"], list):
        raise SkillError("assets.style_reference_images 必须是数组")
    assets["style_reference_images"] = [normalize_space(p) for p in assets["style_reference_images"] if normalize_space(p)]
    assets["style_reference_note"] = sanitize_ip_value(normalize_space(assets.get("style_reference_note")))
    ref_count = len(assets["reference_images"])
    for product_index, product in enumerate(req["products"], start=1):
        ref_index = product.get("reference_image_index")
        if isinstance(ref_index, int) and not isinstance(ref_index, bool) and ref_index > ref_count:
            warnings_list.append(
                f"products[{product_index}].reference_image_index={ref_index} 超出 assets.reference_images 数量 {ref_count}，已忽略。"
            )
            product.pop("reference_image_index", None)
    use_group_buying = bool(assets.get("use_meituan_group_buying_logo", False))
    platform_logo = normalize_space(assets.get("platform_logo"))
    if material_type == "五连图":
        platform_logo = "none"
        assets["use_meituan_logo"] = False
    else:
        if not platform_logo:
            platform_logo = "none" if assets.get("use_meituan_logo") is False else "auto"
        if platform_logo not in PLATFORM_LOGO_VALUES:
            warnings_list.append(f"未知 assets.platform_logo={platform_logo}，已回退为 auto")
            platform_logo = "auto"
        assets["use_meituan_logo"] = platform_logo != "none"
    assets["platform_logo"] = platform_logo
    if use_group_buying and platform_logo in {"auto", "meituan"}:
        assets["_force_group_buying_logo"] = True
    mascot_mode = normalize_space(assets.get("mascot_mode") or "auto")
    if mascot_mode not in {"auto", "official_reference", "generated_reference", "none", "official_overlay"}:
        warnings_list.append(f"未知 mascot_mode={mascot_mode}，已回退为 auto")
        mascot_mode = "auto"
    assets["mascot_mode"] = mascot_mode
    assets["food_image_not_needed"] = bool(
        assets.get("food_image_not_needed")
        or assets.get("food_image_declined")
        or assets.get("no_food_image")
    )
    if req["products"] and not assets["reference_images"] and not assets["food_image_not_needed"]:
        warnings_list.append("已提供菜品/商品信息但未提供菜品参考图；应在 Layer 2 询问用户是否补充菜品图。未补图时背景 prompt 禁止生成具体菜品。")
    if not assets["reference_images"] and not assets["food_image_not_needed"]:
        assets["food_image_not_needed"] = True
        warnings_list.append("未提供菜品参考图，Layer 1 默认不使用真实菜品图；如需具体菜品视觉，应在 Layer 2 引导上传。")
    req["assets"] = assets
    compliance = dict(req.get("_compliance") or {})
    compliance["protected_ip_sanitized"] = protected_ip_sanitized
    req["_compliance"] = compliance
    if protected_ip_sanitized:
        warnings_list.append("已将受保护 IP 相关表述替换为通用中性表达，避免潜在知识产权风险。")
    return req, warnings_list


def canvas_size(size: dict[str, Any], dpi: int) -> tuple[int, int]:
    custom_px = size.get("custom_px")
    if custom_px:
        width = round(custom_px["width"])
        height = round(custom_px["height"])
        return max(320, width), max(320, height)
    custom_mm = size.get("custom_mm")
    if custom_mm:
        width = round(custom_mm["width"] / 25.4 * dpi)
        height = round(custom_mm["height"] / 25.4 * dpi)
        return max(320, width), max(320, height)
    preset = size.get("preset") or "3:4"
    if preset in PRESET_CANVAS_SIZES:
        return PRESET_CANVAS_SIZES[preset]
    rw, rh = PRESET_RATIOS[preset]
    if rw <= rh:
        width = 1024
        height = round(width * rh / rw / 16) * 16
    else:
        height = 1024
        width = round(height * rw / rh / 16) * 16
    return width, height


def ratio_kind(width: int, height: int) -> str:
    if height > 0 and width / height >= 5:
        return "ultra_wide"
    if abs(width - height) / max(width, height) < 0.06:
        return "square"
    return "landscape" if width > height else "portrait"


def style_id_of(style: dict[str, Any]) -> str:
    return normalize_space(style.get("style_id") or style.get("variant"))


STYLE_REFERENCE_DOMINANT_STYLE_ID = "style_reference_dominant"


def has_style_reference(req: dict[str, Any]) -> bool:
    return bool(req.get("assets", {}).get("style_reference_images"))


def style_reference_dominant_style() -> dict[str, Any]:
    return {
        "template_id": STYLE_REFERENCE_DOMINANT_STYLE_ID,
        "style_id": STYLE_REFERENCE_DOMINANT_STYLE_ID,
        "label": "风格参考图主导模式",
        "scene_prompt": "用户已提供风格参考图；跳过所有预设风格选择，整体设计由参考图主导。",
        "color_strategy": "由风格参考图决定",
        "composition_strategy": "由风格参考图决定",
        "texture_guidance": "",
        "best_for": ["用户提供风格参考图，需要精准模仿参考图整体设计"],
        "avoid_for": [],
        "category_affinity": [],
    }


def load_styles(material_type: str) -> list[dict[str, Any]]:
    styles = []
    style_material_type = TEMPLATE_MATERIAL_ALIAS.get(material_type, material_type)
    for path in sorted(STYLE_DIR.glob("*.json")):
        style = read_json(path)
        missing = _REQUIRED_STYLE_FIELDS - set(style.keys())
        if missing:
            raise SkillError(f"风格文件 {path.name} 缺少必填字段: {sorted(missing)}")
        if style_material_type in style.get("material_type", []):
            style["_path"] = str(path)
            styles.append(style)
    if not styles:
        raise SkillError(f"未找到适用于 {material_type} 的设计风格")
    return styles


def load_templates(material_type: str) -> list[dict[str, Any]]:
    """Compatibility wrapper for older local callers; new code should use load_styles."""
    return load_styles(material_type)


def select_diverse_templates(templates: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """Select *count* templates with maximum mood diversity.

    Strategy:
    1. If count == 1, return the first template (respects pre-sorted priority order).
    2. For count > 1: group templates by mood_group, shuffle groups, and pick one
       from each group round-robin until *count* reached.
    This guarantees that any 2 selected templates have different mood_groups (as long as
    there are enough distinct groups), producing visually distinct outputs for the user.
    """
    import random

    # Single variant: respect input order (allows season-priority sorting upstream)
    if count == 1 and templates:
        return [templates[0]]

    if count >= len(templates):
        chosen = list(templates)
        random.shuffle(chosen)
        return chosen

    # Group by mood_group
    groups: dict[str, list[dict[str, Any]]] = {}
    for t in templates:
        key = t.get("mood_group", t.get("variant", "default"))
        groups.setdefault(key, []).append(t)

    # Shuffle within each group and shuffle group order
    group_keys = list(groups.keys())
    random.shuffle(group_keys)
    for key in group_keys:
        random.shuffle(groups[key])

    # Round-robin pick from different groups
    selected: list[dict[str, Any]] = []
    group_iters = {key: iter(groups[key]) for key in group_keys}
    key_cycle = group_keys[:]
    while len(selected) < count and key_cycle:
        next_cycle = []
        for key in key_cycle:
            if len(selected) >= count:
                break
            try:
                selected.append(next(group_iters[key]))
            except StopIteration:
                continue
            else:
                next_cycle.append(key)
        key_cycle = next_cycle

    return selected


def template_negative_items(template: dict[str, Any]) -> list[str]:
    """Return legacy and structured negative prompt items from a template."""
    items: list[str] = []
    legacy = template.get("negative")
    if isinstance(legacy, str):
        items.extend(part.strip() for part in legacy.split(",") if part.strip())
    elif isinstance(legacy, list):
        items.extend(str(part).strip() for part in legacy if str(part).strip())

    negative_style = template.get("negative_style")
    if isinstance(negative_style, str):
        items.extend(part.strip() for part in negative_style.split(",") if part.strip())
    elif isinstance(negative_style, list):
        items.extend(str(part).strip() for part in negative_style if str(part).strip())

    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item not in seen:
            deduped.append(item)
            seen.add(item)
    return deduped


def request_summary_for_selector(req: dict[str, Any]) -> dict[str, Any]:
    return {
        "material_type": req.get("type"),
        "title": req.get("title"),
        "store": req.get("store", {}),
        "campaign": req.get("campaign", {}),
        "style": {
            "name": req.get("style", {}).get("name"),
            "season_atmosphere": req.get("style", {}).get("season_atmosphere"),
            "notes": req.get("style", {}).get("notes"),
            "cuisine_tag": req.get("style", {}).get("cuisine_tag"),
            "city_tag": req.get("style", {}).get("city_tag"),
            "tone": req.get("style", {}).get("tone"),
            "realism": req.get("style", {}).get("realism"),
        },
        "copy": {
            "dimensions": req.get("copy", {}).get("dimensions", []),
            "selected_text": req.get("copy", {}).get("selected_text", ""),
        },
        "scene": req.get("scene", {}),
        "business_intent": {
            "primary": req.get("scene", {}).get("business_intent", "daily_attract"),
            "all": req.get("scene", {}).get("business_intents", ["daily_attract"]),
        },
        "brand_profile": req.get("brand_profile", {}),
        "products": req.get("products", []),
        "has_qr": _has_qr_asset(req.get("assets", {})),
    }


def build_template_catalog(req: dict[str, Any], templates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    for template in templates:
        catalog.append({
            "template_id": template.get("template_id"),
            "variant": template.get("variant"),
            "strategy": template.get("strategy"),
            "mood_group": template.get("mood_group"),
            "selector_summary": template.get("selector_summary") or trim_prompt_text(template.get("scene_prompt"), 220),
            "best_for": template.get("best_for", []),
            "avoid_for": template.get("avoid_for", []),
            "category_affinity": template.get("category_affinity", []),
            "festival_affinity": template.get("festival_affinity", []),
            "audience_affinity": template.get("audience_affinity", []),
            "visual_tags": template.get("visual_tags") or template.get("style_keywords", []),
            "qr_host_hint": template.get("qr_host_hint", ""),
        })
    return catalog


def build_style_catalog(req: dict[str, Any], styles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    for style in styles:
        catalog.append({
            "style_id": style.get("style_id"),
            "label": style.get("label"),
            "selector_summary": trim_prompt_text(style.get("scene_prompt"), 260),
            "best_for": style.get("best_for", []),
            "avoid_for": style.get("avoid_for", []),
            "category_affinity": style.get("category_affinity", []),
            "color_strategy": style.get("color_strategy"),
            "composition_strategy": style.get("composition_strategy"),
            "texture_guidance": style.get("texture_guidance"),
        })
    return catalog


def build_style_selector_prompt(req: dict[str, Any], catalog: list[dict[str, Any]], count: int) -> tuple[str, str]:
    system_prompt = (
        "你是餐饮营销物料的创意总监。你的任务只是在给定设计风格中选择 style_id，不生成图片。"
        "优先判断 scene.business_intent、餐厅品类、活动目标、受众和物料类型。"
        "第一名必须最贴合商业目的；多图时后续风格必须在色彩策略或构图手法上提供明显差异。"
        "只能选择候选 catalog 中存在的 style_id，不得编造 style_id。"
        "输出必须是合法 JSON，不能包含 JSON 之外的解释文字。"
        "JSON 格式: {\"detected_context\": {...}, \"ranked_styles\": "
        "[{\"style_id\": \"...\", \"score\": 0.0, \"reason\": \"...\"}], "
        "\"fallback_style\": \"...\"}。score 必须在 0 到 1 之间。"
    )
    user_payload = {
        "task": f"请选择 {count} 个最适合的设计风格，并给出完整排序。",
        "request": request_summary_for_selector(req),
        "catalog": catalog,
        "selection_policy": {
            "strong_score_threshold": SELECTOR_SCORE_STRONG,
            "explore_score_threshold": SELECTOR_SCORE_EXPLORE,
            "rule": "业务目的优先；双方案必须在 color_strategy 或 composition_strategy 上拉开差异。",
        },
    }
    return system_prompt, json.dumps(user_payload, ensure_ascii=False)


def build_template_selector_prompt(req: dict[str, Any], catalog: list[dict[str, Any]], count: int) -> tuple[str, str]:
    system_prompt = (
        "你是餐饮营销物料的创意总监。你的任务只是在给定模板中选择视觉模板，不生成图片。"
        "综合判断餐厅品类、城市地域、季节节气、节日、活动目标、受众、物料类型和 QR 转化需求。"
        "第一名必须最贴合商业目标；多图时后续模板应在保持相关性的前提下提供视觉差异。"
        "只能选择候选 catalog 中存在的 variant，不得编造 variant 或 template_id。"
        "输出必须是合法 JSON，不能包含 JSON 之外的解释文字。"
        "JSON 格式: {\"detected_context\": {...}, \"ranked_variants\": "
        "[{\"variant\": \"...\", \"score\": 0.0, \"reason\": \"...\"}], "
        "\"fallback_variant\": \"...\"}。score 必须在 0 到 1 之间。"
    )
    user_payload = {
        "task": f"请选择 {count} 个最适合的模板，并给出完整排序。",
        "request": request_summary_for_selector(req),
        "catalog": catalog,
        "selection_policy": {
            "strong_score_threshold": SELECTOR_SCORE_STRONG,
            "explore_score_threshold": SELECTOR_SCORE_EXPLORE,
            "rule": "先相关，再多样；不要为了多样性选择明显不相关的模板。",
        },
    }
    return system_prompt, json.dumps(user_payload, ensure_ascii=False)


def _strip_json_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()


def parse_template_selection(response_text: str) -> dict[str, Any]:
    """Parse selector JSON from extracted text or a raw Responses API body."""
    candidates = [response_text]
    try:
        raw = json.loads(response_text)
    except json.JSONDecodeError:
        raw = None
    if isinstance(raw, dict):
        if isinstance(raw.get("ranked_variants"), list):
            return raw
        extracted = extract_responses_text(raw)
        if extracted:
            candidates.insert(0, extracted)

    for candidate in candidates:
        text = _strip_json_fence(candidate)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, flags=re.S)
            if not match:
                continue
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                continue
        if isinstance(parsed, dict) and isinstance(parsed.get("ranked_variants"), list):
            return parsed
    raise SkillError("Template selector 返回内容不是合法选择 JSON")


def parse_style_selection(response_text: str) -> dict[str, Any]:
    candidates = [response_text]
    try:
        raw = json.loads(response_text)
    except json.JSONDecodeError:
        raw = None
    if isinstance(raw, dict):
        if isinstance(raw.get("ranked_styles"), list):
            return raw
        extracted = extract_responses_text(raw)
        if extracted:
            candidates.insert(0, extracted)

    for candidate in candidates:
        text = _strip_json_fence(candidate)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, flags=re.S)
            if not match:
                continue
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                continue
        if isinstance(parsed, dict) and isinstance(parsed.get("ranked_styles"), list):
            return parsed
    raise SkillError("Style selector 返回内容不是合法选择 JSON")


def _coerce_selector_score(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if score < 0 or score > 1:
        return None
    return score


def local_fallback_variant_order(req: dict[str, Any], templates: list[dict[str, Any]]) -> list[str]:
    available = {str(t.get("variant")) for t in templates}
    blob = request_text_blob(req)
    rules = [
        (("春节", "过年", "元宵", "开业", "国庆", "中秋", "店庆", "周年庆"), "festive_prosperity"),
        (("火锅", "川菜", "湘菜", "麻辣", "串串", "冒菜", "烧烤", "烤肉"), "bold_fiery"),
        (("甜品", "烘焙", "下午茶", "奶茶", "蛋糕", "面包"), "pastel_dream"),
        (("轻食", "沙拉", "有机", "素食", "健康", "低卡"), "organic_garden"),
        (("夜宵", "酒馆", "居酒屋", "精酿", "小酒馆", "烧鸟", "微醺"), "midnight_glow"),
        (("老店", "早餐", "饺子", "粉面", "社区", "邻里", "冬至"), "nostalgic_lane"),
        (("西餐", "烛光", "约会", "七夕", "情人节"), "urban_chic"),
        (("海鲜", "三亚", "海边", "沿海", "夏季", "夏日"), "coastal_breeze"),
        (("儿童", "亲子", "家庭", "周末"), "family_joy"),
    ]

    ordered: list[str] = []
    for keywords, variant in rules:
        if variant in available and any(keyword in blob for keyword in keywords):
            ordered.append(variant)
    if "bustling_warmth" in available:
        ordered.append("bustling_warmth")
    for template in templates:
        variant = str(template.get("variant"))
        if variant and variant in available:
            ordered.append(variant)

    result: list[str] = []
    seen: set[str] = set()
    for variant in ordered:
        if variant not in seen:
            result.append(variant)
            seen.add(variant)
    return result


def _templates_by_variant(templates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(template.get("variant")): template for template in templates if template.get("variant")}


def _styles_by_id(styles: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {style_id_of(style): style for style in styles if style_id_of(style)}


def _category_text(req: dict[str, Any]) -> str:
    return compact_text(" ".join([
        normalize_space(req.get("store", {}).get("category")),
        normalize_space(req.get("style", {}).get("cuisine_tag")),
    ]))


def _is_chinese_category(req: dict[str, Any]) -> bool:
    category = _category_text(req)
    if any(keyword in category for keyword in NON_CHINESE_CATEGORY_KEYWORDS):
        return False
    return any(keyword in category for keyword in CHINESE_CATEGORY_KEYWORDS)


def _is_sichuan_hunan_category(req: dict[str, Any]) -> bool:
    category = _category_text(req)
    if not category:
        return False
    return any(keyword in category for keyword in REGIONAL_APPETITE_CUISINE_KEYWORDS)


def _season_from_request_or_current(req: dict[str, Any]) -> str:
    blob = request_text_blob(req)
    for season, keywords in SEASON_KEYWORDS.items():
        if any(keyword in blob for keyword in keywords):
            return season
    return MONTH_TO_SEASON.get(time.localtime().tm_mon, "")


def _has_any_keyword(blob: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in blob for keyword in keywords)


def _sichuan_hunan_context(req: dict[str, Any]) -> dict[str, Any]:
    if not _is_sichuan_hunan_category(req):
        return {}
    blob = request_text_blob(req)
    season = _season_from_request_or_current(req)
    return {
        "season": season,
        "fresh": _has_any_keyword(blob, SICHUAN_HUNAN_FRESH_KEYWORDS),
        "classic": _has_any_keyword(blob, SICHUAN_HUNAN_CLASSIC_KEYWORDS),
        "impact": _has_any_keyword(blob, SICHUAN_HUNAN_IMPACT_KEYWORDS),
        "rich_dish": _has_any_keyword(blob, SICHUAN_HUNAN_RICH_DISH_KEYWORDS),
    }


def _style_allowed_for_category(req: dict[str, Any], style_id: str) -> bool:
    category = _category_text(req)
    if not category:
        return True
    is_chinese = _is_chinese_category(req)
    if not is_chinese and style_id in {"festive_red", "ink_oriental"}:
        return False
    if is_chinese and style_id == "latin_fiesta":
        return False
    return True


def _category_affinity_score(req: dict[str, Any], style: dict[str, Any]) -> float:
    category = _category_text(req)
    if not category:
        return 0.0
    for affinity in style.get("category_affinity", []):
        affinity_text = normalize_space(affinity)
        if affinity_text and (affinity_text in category or category in affinity_text):
            return 0.5
    return 0.0


def _has_explicit_dark_style_context(req: dict[str, Any]) -> bool:
    blob = request_text_blob(req).lower()
    return any(keyword.lower() in blob for keyword in EXPLICIT_DARK_STYLE_KEYWORDS)


def _explicit_dark_style_match_score(req: dict[str, Any], style_id: str) -> float:
    blob = request_text_blob(req).lower()
    style_keywords = {
        "neon_night": ("夜宵", "深夜", "酒馆", "小酒馆", "居酒屋", "精酿", "霓虹", "夜场"),
        "dark_premium": ("暗调", "暗色", "黑金", "高端暗场", "私宴", "fine dining"),
        "industrial_craft": ("精酿", "酒吧", "威士忌", "牛排", "工业"),
        "cyber_gaming": ("电竞", "赛博", "游戏", "科幻"),
    }
    if any(keyword.lower() in blob for keyword in style_keywords.get(style_id, ())):
        return 26.0 if style_id == "dark_premium" else 18.0
    return 0.0


def _global_appetite_style_adjustment(req: dict[str, Any], style_id: str) -> tuple[float, str]:
    score = APPETITE_BRIGHT_STYLE_BASELINE.get(style_id, 0.0)
    reasons: list[str] = []
    if score:
        reasons.append("全局餐饮默认明亮开胃")

    dark_penalty = DARK_APPETITE_DEFAULT_PENALTY.get(style_id, 0.0)
    if dark_penalty:
        if _has_explicit_dark_style_context(req):
            reasons.append("用户语境明确允许暗调/夜场风格")
        else:
            score += dark_penalty
            reasons.append("通用餐饮默认减少暗黑暗红")
        explicit_match = _explicit_dark_style_match_score(req, style_id)
        if explicit_match:
            score += explicit_match
            reasons.append("显式暗调/夜场关键词匹配当前风格")

    return score, "；".join(reasons)


def _sichuan_hunan_style_adjustment(req: dict[str, Any], style_id: str) -> tuple[float, str]:
    context = _sichuan_hunan_context(req)
    if not context:
        return 0.0, ""

    score = 0.0
    reasons: list[str] = ["地域菜系默认做明亮开胃差异化"]

    base_bright = {
        "natural_earth": 16.0,
        "ink_oriental": 12.0,
        "clean_hero": 10.0,
        "retro_poster": 8.0,
        "dynamic_angle": 8.0,
        "bold_split": 6.0,
        "street_warm": 2.0,
    }
    score += base_bright.get(style_id, 0.0)
    if style_id in {"dark_premium", "neon_night", "industrial_craft", "cyber_gaming"}:
        score -= 14.0

    if context.get("season") == "夏季":
        reasons.append("夏季优先浅绿、浅色国风和清爽食欲")
        score += {
            "natural_earth": 34.0,
            "clean_hero": 24.0,
            "ink_oriental": 30.0,
            "poetic_leisure": 20.0,
            "street_warm": 4.0,
            "dynamic_angle": 8.0,
            "bold_split": 4.0,
            "dark_premium": -22.0,
            "neon_night": -18.0,
        }.get(style_id, 0.0)
    elif context.get("season") == "春季":
        reasons.append("春季优先清新自然和浅色东方")
        score += {
            "natural_earth": 26.0,
            "clean_hero": 20.0,
            "ink_oriental": 18.0,
            "poetic_leisure": 16.0,
            "dark_premium": -16.0,
        }.get(style_id, 0.0)

    if context.get("fresh"):
        reasons.append("酸汤鱼/酸菜鱼/藕汤/米粉等优先清爽明亮")
        score += {
            "natural_earth": 22.0,
            "clean_hero": 16.0,
            "ink_oriental": 18.0,
            "poetic_leisure": 12.0,
            "street_warm": -6.0,
            "bold_split": -8.0,
            "dark_premium": -20.0,
        }.get(style_id, 0.0)

    if context.get("classic"):
        reasons.append("巴蜀/云贵/桂赣鄂经典菜适合浅色书法/招贴")
        score += {
            "ink_oriental": 70.0,
            "retro_poster": 28.0,
            "natural_earth": 8.0,
            "street_warm": 8.0,
            "clean_hero": 0.0,
        }.get(style_id, 0.0)

    if context.get("impact"):
        reasons.append("爆炒下饭菜强调锅气和字体冲击")
        score += {
            "dynamic_angle": 60.0,
            "bold_split": 56.0,
            "oversize_type": 24.0,
            "street_warm": 18.0,
            "retro_poster": 8.0,
            "natural_earth": -36.0,
            "clean_hero": -40.0,
        }.get(style_id, 0.0)

    if context.get("rich_dish"):
        reasons.append("小龙虾/水煮类允许更浓郁但仍需保留食欲亮面")
        score += {
            "dynamic_angle": 32.0,
            "street_warm": 30.0,
            "bold_split": 30.0,
            "retro_poster": 10.0,
            "dark_premium": 12.0,
            "ink_oriental": -6.0,
            "natural_earth": -28.0,
            "clean_hero": -12.0,
        }.get(style_id, 0.0)

    return score, "；".join(reasons)


def _styles_visually_similar(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a.get("color_strategy") and a.get("color_strategy") == b.get("color_strategy"):
        return True
    if a.get("composition_strategy") and a.get("composition_strategy") == b.get("composition_strategy"):
        return True
    return frozenset((style_id_of(a), style_id_of(b))) in STYLE_CONFLICT_PAIRS


def _intent_style_candidates(req: dict[str, Any], styles: list[dict[str, Any]]) -> list[tuple[float, dict[str, Any], str]]:
    by_id = _styles_by_id(styles)
    intent_order = req.get("scene", {}).get("business_intents") or [req.get("scene", {}).get("business_intent", "daily_attract")]
    ordered_ids: list[str] = []
    for intent in intent_order:
        for style_id in INTENT_STYLE_ORDER.get(intent, []):
            if style_id not in ordered_ids:
                ordered_ids.append(style_id)
    for style in styles:
        sid = style_id_of(style)
        if sid and sid not in ordered_ids:
            ordered_ids.append(sid)

    scored: list[tuple[float, dict[str, Any], str]] = []
    blob = request_text_blob(req)
    for rank, sid in enumerate(ordered_ids):
        style = by_id.get(sid)
        if not style or not _style_allowed_for_category(req, sid):
            continue
        score = 100.0 - rank * 4.0
        score += _category_affinity_score(req, style)
        global_adjustment, global_adjustment_reason = _global_appetite_style_adjustment(req, sid)
        score += global_adjustment
        style_adjustment, style_adjustment_reason = _sichuan_hunan_style_adjustment(req, sid)
        score += style_adjustment
        avoid_for = style.get("avoid_for", [])
        if any(normalize_space(item) and normalize_space(item) in blob for item in avoid_for):
            score -= 20.0
        reason = f"匹配业务目的：{BUSINESS_INTENT_LABELS.get(req.get('scene', {}).get('business_intent', 'daily_attract'), '日常到店引流')}"
        if global_adjustment_reason:
            reason = f"{reason}；{global_adjustment_reason}"
        if style_adjustment_reason:
            reason = f"{reason}；{style_adjustment_reason}"
        scored.append((score, style, reason))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored


def recommend_styles(req: dict[str, Any], styles: list[dict[str, Any]], top_n: int = 5) -> list[dict[str, Any]]:
    if has_style_reference(req):
        return []

    scored = _intent_style_candidates(req, styles)
    selected: list[tuple[float, dict[str, Any], str]] = []
    for candidate in scored:
        _, style, _ = candidate
        if any(_styles_visually_similar(style, chosen) for _, chosen, _ in selected):
            continue
        selected.append(candidate)
        if len(selected) >= top_n:
            break
    if len(selected) < top_n:
        selected_ids = {style_id_of(style) for _, style, _ in selected}
        for candidate in scored:
            _, style, _ = candidate
            if style_id_of(style) in selected_ids:
                continue
            selected.append(candidate)
            selected_ids.add(style_id_of(style))
            if len(selected) >= top_n:
                break

    results: list[dict[str, Any]] = []
    for score, style, reason in selected[:top_n]:
        scene_prompt = normalize_space(style.get("scene_prompt"))
        label = style.get("label", style_id_of(style))
        results.append({
            "id": style_id_of(style),
            "label": label,
            "description": scene_prompt[:90],
            "color_strategy": style.get("color_strategy"),
            "composition_strategy": style.get("composition_strategy"),
            "score": round(score, 2),
            "reason": reason,
            "display_text": f"{label} \u2014\u2014 {reason}",
        })
    return results


_WARM_TONE_MOODS = {"warm", "hot", "festive", "energetic"}
_COOL_TONE_MOODS = {"cool", "serene", "fresh", "natural"}


def _tone_group(mood: str) -> str:
    if mood in _WARM_TONE_MOODS:
        return "warm_tone"
    if mood in _COOL_TONE_MOODS:
        return "cool_tone"
    return mood


def _fill_with_fallback(req: dict[str, Any], templates: list[dict[str, Any]], selected: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    by_variant = _templates_by_variant(templates)
    selected_variants = {str(template.get("variant")) for template in selected}
    used_moods = {str(template.get("mood_group", template.get("variant"))) for template in selected}
    used_tones = {_tone_group(str(template.get("mood_group", ""))) for template in selected}
    fallback_order = local_fallback_variant_order(req, templates)

    # First pass: prefer variants with different mood_group AND different tone group
    for variant in fallback_order:
        if len(selected) >= count:
            break
        if variant in selected_variants:
            continue
        template = by_variant.get(variant)
        if template:
            mood = str(template.get("mood_group", variant))
            tone = _tone_group(mood)
            if mood not in used_moods and tone not in used_tones:
                selected.append(template)
                selected_variants.add(variant)
                used_moods.add(mood)
                used_tones.add(tone)

    # Second pass: allow same tone group but still require different mood_group
    for variant in fallback_order:
        if len(selected) >= count:
            break
        if variant in selected_variants:
            continue
        template = by_variant.get(variant)
        if template:
            mood = str(template.get("mood_group", variant))
            if mood not in used_moods:
                selected.append(template)
                selected_variants.add(variant)
                used_moods.add(mood)

    # Third pass: if still not enough, allow same mood_group
    for variant in fallback_order:
        if len(selected) >= count:
            break
        if variant in selected_variants:
            continue
        template = by_variant.get(variant)
        if template:
            selected.append(template)
            selected_variants.add(variant)
    return selected


def validate_and_rerank_selection(
    req: dict[str, Any],
    templates: list[dict[str, Any]],
    parsed: dict[str, Any],
    count: int,
    provider: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_variant = _templates_by_variant(templates)
    validation_warnings: list[str] = []
    candidates: list[dict[str, Any]] = []
    seen_variants: set[str] = set()

    for item in parsed.get("ranked_variants") or []:
        if not isinstance(item, dict):
            validation_warnings.append("invalid ranked item")
            continue
        variant = normalize_space(item.get("variant"))
        if variant not in by_variant:
            validation_warnings.append(f"unknown variant: {variant}")
            continue
        if variant in seen_variants:
            validation_warnings.append(f"duplicate variant: {variant}")
            continue
        score = _coerce_selector_score(item.get("score"))
        if score is None:
            validation_warnings.append(f"invalid score for {variant}: {item.get('score')}")
            continue
        candidates.append({
            "variant": variant,
            "score": score,
            "reason": normalize_space(item.get("reason")),
            "template": by_variant[variant],
        })
        seen_variants.add(variant)

    selected: list[dict[str, Any]] = []
    selected_variants: set[str] = set()
    used_moods: set[str] = set()

    if candidates:
        first = candidates[0]
        selected.append(first["template"])
        selected_variants.add(first["variant"])
        used_moods.add(str(first["template"].get("mood_group", first["variant"])))

    for candidate in candidates[1:]:
        if len(selected) >= count:
            break
        if candidate["score"] < SELECTOR_SCORE_EXPLORE:
            continue
        mood = str(candidate["template"].get("mood_group", candidate["variant"]))
        if mood in used_moods:
            continue
        selected.append(candidate["template"])
        selected_variants.add(candidate["variant"])
        used_moods.add(mood)

    for candidate in candidates[1:]:
        if len(selected) >= count:
            break
        if candidate["variant"] in selected_variants:
            continue
        if candidate["score"] < SELECTOR_SCORE_EXPLORE:
            continue
        selected.append(candidate["template"])
        selected_variants.add(candidate["variant"])

    _fill_with_fallback(req, templates, selected, count)
    audit = {
        "provider": provider,
        "parsed_selection": parsed,
        "validated_candidates": [
            {"variant": c["variant"], "score": c["score"], "reason": c["reason"]}
            for c in candidates
        ],
        "validation_warnings": validation_warnings,
    }
    return selected[:count], audit


def validate_and_rerank_style_selection(
    req: dict[str, Any],
    styles: list[dict[str, Any]],
    parsed: dict[str, Any],
    count: int,
    provider: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_id = _styles_by_id(styles)
    validation_warnings: list[str] = []
    candidates: list[tuple[float, dict[str, Any], str]] = []
    seen: set[str] = set()

    for item in parsed.get("ranked_styles") or []:
        if not isinstance(item, dict):
            validation_warnings.append("invalid ranked item")
            continue
        sid = normalize_space(item.get("style_id"))
        if sid not in by_id:
            validation_warnings.append(f"unknown style_id: {sid}")
            continue
        if sid in seen:
            validation_warnings.append(f"duplicate style_id: {sid}")
            continue
        score = _coerce_selector_score(item.get("score"))
        if score is None:
            validation_warnings.append(f"invalid score for {sid}: {item.get('score')}")
            continue
        style = by_id[sid]
        if not _style_allowed_for_category(req, sid):
            validation_warnings.append(f"category filtered style_id: {sid}")
            continue
        candidates.append((score * 100.0, style, normalize_space(item.get("reason"))))
        seen.add(sid)

    candidates.sort(key=lambda item: item[0], reverse=True)
    selected: list[dict[str, Any]] = []
    for _, style, _ in candidates:
        if len(selected) >= count:
            break
        if any(_styles_visually_similar(style, existing) for existing in selected):
            continue
        selected.append(style)
    if len(selected) < count:
        fallback = [by_id[item["id"]] for item in recommend_styles(req, styles, top_n=count)]
        for style in fallback:
            if style_id_of(style) not in {style_id_of(existing) for existing in selected}:
                selected.append(style)
            if len(selected) >= count:
                break
    audit = {
        "provider": provider,
        "parsed_selection": parsed,
        "validated_candidates": [
            {"style_id": style_id_of(style), "score": round(score / 100.0, 3), "reason": reason}
            for score, style, reason in candidates
        ],
        "validation_warnings": validation_warnings,
    }
    return selected[:count], audit


def local_fallback_selection(req: dict[str, Any], templates: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    return _fill_with_fallback(req, templates, selected, count)[:count]


def local_fallback_style_selection(req: dict[str, Any], styles: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    by_id = _styles_by_id(styles)
    return [by_id[item["id"]] for item in recommend_styles(req, styles, top_n=count) if item["id"] in by_id][:count]


def recommend_visual_atmospheres(req: dict[str, Any], templates: list[dict[str, Any]], top_n: int = 5) -> list[dict[str, Any]]:
    """Compatibility wrapper for the deprecated atmosphere API."""
    styles = templates if any(template.get("style_id") for template in templates) else load_styles(req.get("type", "营销海报"))
    return recommend_styles(req, styles, top_n=top_n)


def template_selection_audit(
    req: dict[str, Any],
    templates: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    provider: str,
    catalog: list[dict[str, Any]],
    raw_selector_text: str = "",
    parsed_selection: dict[str, Any] | None = None,
    validation_audit: dict[str, Any] | None = None,
    fallback_reason: str = "",
) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": "",
        "request_summary": request_summary_for_selector(req),
        "catalog": catalog,
        "raw_selector_text": raw_selector_text,
        "parsed_selection": parsed_selection or {},
        "validated_candidates": (validation_audit or {}).get("validated_candidates", []),
        "validation_warnings": (validation_audit or {}).get("validation_warnings", []),
        "fallback_reason": fallback_reason,
        "final_variants": [
            {
                "template_id": template.get("template_id"),
                "variant": template.get("variant"),
                "strategy": template.get("strategy"),
                "mood_group": template.get("mood_group"),
            }
            for template in selected
        ],
    }


def style_selection_audit(
    req: dict[str, Any],
    styles: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    provider: str,
    catalog: list[dict[str, Any]],
    raw_selector_text: str = "",
    parsed_selection: dict[str, Any] | None = None,
    validation_audit: dict[str, Any] | None = None,
    fallback_reason: str = "",
) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": "",
        "request_summary": request_summary_for_selector(req),
        "catalog": catalog,
        "raw_selector_text": raw_selector_text,
        "parsed_selection": parsed_selection or {},
        "validated_candidates": (validation_audit or {}).get("validated_candidates", []),
        "validation_warnings": (validation_audit or {}).get("validation_warnings", []),
        "fallback_reason": fallback_reason,
        "final_styles": [
            {
                "style_id": style.get("style_id"),
                "template_id": style.get("template_id"),
                "label": style.get("label"),
                "color_strategy": style.get("color_strategy"),
                "composition_strategy": style.get("composition_strategy"),
            }
            for style in selected
        ],
    }


def select_templates(
    req: dict[str, Any],
    templates: list[dict[str, Any]],
    count: int,
    provider: str,
    model: str,
    timeout: int,
    out_dir: Path,
) -> TemplateSelection:
    catalog = build_template_catalog(req, templates)
    raw_selector_text = ""
    parsed: dict[str, Any] | None = None
    validation_audit: dict[str, Any] | None = None
    fallback_reason = ""
    selected: list[dict[str, Any]]

    if provider == "none":
        selected = local_fallback_selection(req, templates, count)
        fallback_reason = "provider=none"
    elif provider in {"auto", "api"}:
        raw_response_path = out_dir / "raw_responses" / "template_selector.txt"
        system_prompt, user_prompt = build_template_selector_prompt(req, catalog, count)
        try:
            raw_selector_text = call_template_selector_api(system_prompt, user_prompt, raw_response_path, model=model, timeout=timeout)
            parsed = parse_template_selection(raw_selector_text)
            selected, validation_audit = validate_and_rerank_selection(req, templates, parsed, count, provider)
            if len(selected) < count:
                fallback_reason = "selector_candidates_insufficient"
        except Exception as exc:
            if provider == "api":
                raise SkillError(f"Template selector API 失败: {exc}") from None
            selected = local_fallback_selection(req, templates, count)
            fallback_reason = "selector_api_failed"
            validation_audit = {"validation_warnings": [sanitize_api_text(str(exc))], "validated_candidates": []}
    else:
        raise SkillError(f"未知 template selector provider: {provider}")

    audit = template_selection_audit(
        req,
        templates,
        selected,
        provider,
        catalog,
        raw_selector_text=raw_selector_text,
        parsed_selection=parsed,
        validation_audit=validation_audit,
        fallback_reason=fallback_reason,
    )
    audit["model"] = model
    write_json(out_dir / "template_selection.json", audit)
    return TemplateSelection(templates=selected, audit=audit)


def select_styles(
    req: dict[str, Any],
    styles: list[dict[str, Any]],
    count: int,
    provider: str,
    model: str,
    timeout: int,
    out_dir: Path,
) -> TemplateSelection:
    if has_style_reference(req):
        selected = [style_reference_dominant_style()]
        audit = style_selection_audit(
            req,
            styles,
            selected,
            "style_reference",
            [],
            fallback_reason="style_reference_images_provided",
        )
        audit["model"] = model
        write_json(out_dir / "template_selection.json", audit)
        return TemplateSelection(templates=selected, audit=audit)

    catalog = build_style_catalog(req, styles)
    raw_selector_text = ""
    parsed: dict[str, Any] | None = None
    validation_audit: dict[str, Any] | None = None
    fallback_reason = ""
    selected: list[dict[str, Any]]

    if provider == "none":
        selected = local_fallback_style_selection(req, styles, count)
        fallback_reason = "provider=none"
    elif provider in {"auto", "api"}:
        raw_response_path = out_dir / "raw_responses" / "style_selector.txt"
        system_prompt, user_prompt = build_style_selector_prompt(req, catalog, count)
        try:
            raw_selector_text = call_template_selector_api(system_prompt, user_prompt, raw_response_path, model=model, timeout=timeout)
            parsed = parse_style_selection(raw_selector_text)
            selected, validation_audit = validate_and_rerank_style_selection(req, styles, parsed, count, provider)
            if len(selected) < count:
                fallback_reason = "selector_candidates_insufficient"
        except Exception as exc:
            if provider == "api":
                raise SkillError(f"Style selector API 失败: {exc}") from None
            selected = local_fallback_style_selection(req, styles, count)
            fallback_reason = "selector_api_failed"
            validation_audit = {"validation_warnings": [sanitize_api_text(str(exc))], "validated_candidates": []}
    else:
        raise SkillError(f"未知 style selector provider: {provider}")

    audit = style_selection_audit(
        req,
        styles,
        selected,
        provider,
        catalog,
        raw_selector_text=raw_selector_text,
        parsed_selection=parsed,
        validation_audit=validation_audit,
        fallback_reason=fallback_reason,
    )
    audit["model"] = model
    write_json(out_dir / "template_selection.json", audit)
    return TemplateSelection(templates=selected, audit=audit)


def load_layouts() -> list[dict[str, Any]]:
    manifest = read_json(LAYOUT_DIR / "manifest.json")
    layouts = manifest.get("layouts") or []
    if not isinstance(layouts, list) or not layouts:
        raise SkillError("assets/layouts/manifest.json 缺少 layouts")
    return layouts


def select_layout(req: dict[str, Any], template: dict[str, Any], width: int, height: int) -> dict[str, Any]:
    layouts = load_layouts()
    kind = ratio_kind(width, height)
    has_qr = _has_qr_asset(req["assets"])
    has_food = bool(req["assets"].get("reference_images"))
    style_id = template.get("layout_preference") or template.get("style_id") or template.get("variant")
    best: tuple[int, dict[str, Any]] | None = None
    for layout in layouts:
        if req["type"] not in layout.get("material_types", []):
            continue
        if kind not in layout.get("ratio", []):
            continue
        if layout.get("requires_qr") and not has_qr:
            continue
        score = 10
        if layout.get("prefers_qr") and has_qr:
            score += 4
        if layout.get("prefers_food_image") and has_food:
            score += 8
        if layout.get("prefers_food_image") and not has_food:
            score -= 5
        if style_id in layout.get("style_tie_breaker", []) or style_id in layout.get("variant_tie_breaker", []):
            score += 2
        if req["type"] == "台卡" and layout["id"] == "layout_f":
            score += 5
        candidate = (score, layout)
        if best is None or candidate[0] > best[0]:
            best = candidate
    if best is None:
        raise SkillError(f"无法为 {req['type']} {kind} 选择布局")
    return best[1]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def image_to_data_url(path: Path) -> str:
    data = path.read_bytes()
    suffix = path.suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def materialize_data_url(data_url: str, out_path: Path) -> Path:
    match = re.match(r"data:image/([a-zA-Z0-9+.-]+);base64,(.+)", data_url, re.S)
    if not match:
        raise SkillError("qr_code_data_url 必须是 data:image/...;base64,... 格式")
    suffix = "jpg" if match.group(1).lower() in {"jpeg", "jpg"} else "png"
    out_path = out_path.with_suffix("." + suffix)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(base64.b64decode(re.sub(r"\s+", "", match.group(2))))
    return out_path


def resolve_image_path(value: str, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.exists():
        raise SkillError(f"{label} 不存在: {path}")
    return path.resolve()


def copy_asset(src: Path, dst_dir: Path, name: str | None = None) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / (name or src.name)
    if src.resolve() != dst.resolve():
        shutil.copy2(src, dst)
    return dst


def _prepare_mascot_ref(mascot_path: Path, asset_dir: Path) -> Path:
    """Return a compressed mascot reference image (max MASCOT_REF_MAX_PX on long edge).

    Saves ~94% upload size vs. the original 2294×1324 sheet.
    Falls back to the original path if Pillow is unavailable or resize fails.
    """
    out_path = asset_dir / "brand" / "meituan-mascot-ref.png"
    if out_path.exists():
        return out_path
    try:
        from PIL import Image as _PILImage  # type: ignore
    except ImportError:
        return mascot_path
    try:
        img = _PILImage.open(mascot_path).convert("RGBA")
        w, h = img.size
        long_edge = max(w, h)
        if long_edge <= MASCOT_REF_MAX_PX:
            return mascot_path  # already small enough
        scale = MASCOT_REF_MAX_PX / long_edge
        new_w, new_h = max(1, round(w * scale)), max(1, round(h * scale))
        resampling = getattr(_PILImage, "Resampling", _PILImage).LANCZOS
        img = img.resize((new_w, new_h), resampling)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, "PNG", optimize=True)
        orig_kb = mascot_path.stat().st_size // 1024
        new_kb = out_path.stat().st_size // 1024
        print(f"[mascot ref] compressed {orig_kb} KB → {new_kb} KB ({new_w}×{new_h}px)", flush=True)
        return out_path
    except Exception:
        return mascot_path


def _pil_image_has_alpha(image: Any) -> bool:
    if image.mode in {"RGBA", "LA"}:
        return True
    return image.mode == "P" and "transparency" in image.info


def _resize_image_to_long_edge(image: Any, max_long_edge: int) -> Any:
    width, height = image.size
    long_edge = max(width, height)
    if long_edge <= max_long_edge:
        return image
    scale = max_long_edge / long_edge
    new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
    resampling = getattr(image.__class__, "Resampling", None)
    if resampling is None:
        try:
            from PIL import Image as _PILImage  # type: ignore
            resampling = getattr(_PILImage, "Resampling", _PILImage)
        except Exception:
            resampling = image.__class__
    return image.resize(new_size, getattr(resampling, "LANCZOS"))


def _prepare_model_reference_image(
    src: Path,
    asset_dir: Path,
    name: str,
    role: str,
    warnings_list: list[str],
) -> tuple[Path, bool]:
    """Optimize a model reference image after it has been copied into assets/.

    The runtime-only panorama layout mask is created later inside
    _generate_panorama_carrier(), so it never enters this preprocessing stage.
    """
    try:
        from PIL import Image as _PILImage  # type: ignore
    except ImportError:
        warnings_list.append(f"{role} 预处理跳过：Pillow 不可用，已回退原图。")
        return src, True

    try:
        with _PILImage.open(src) as image:
            original_format = (image.format or "").upper()
            width, height = image.size
            long_edge = max(width, height)
            has_alpha = _pil_image_has_alpha(image)
            if (
                not has_alpha
                and original_format in {"JPEG", "JPG"}
                and long_edge <= MODEL_REFERENCE_MAX_LONG_EDGE
                and src.stat().st_size < MODEL_REFERENCE_SKIP_JPEG_BYTES
            ):
                warnings_list.append(
                    f"{role} {src.name} 已是小 JPEG（≤{MODEL_REFERENCE_MAX_LONG_EDGE}px 且 <500KB），跳过重压缩。"
                )
                return src, False

            max_long_edge = MODEL_REFERENCE_ALPHA_MAX_LONG_EDGE if has_alpha else MODEL_REFERENCE_MAX_LONG_EDGE
            out_suffix = ".png" if has_alpha else ".jpg"
            out_path = asset_dir / "model_refs" / f"{Path(name).stem}{out_suffix}"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            optimized = _resize_image_to_long_edge(image, max_long_edge)
            if has_alpha:
                optimized = optimized.convert("RGBA")
                optimized.save(out_path, "PNG", optimize=True)
            else:
                optimized = optimized.convert("RGB")
                optimized.save(out_path, "JPEG", quality=MODEL_REFERENCE_JPEG_QUALITY, optimize=True)
            warnings_list.append(
                f"{role} {src.name} 已压缩为 {out_path.name}，最长边≤{max_long_edge}px，供图像模型参考。"
            )
            return out_path, False
    except Exception as exc:
        warnings_list.append(f"{role} {src.name} 预处理失败，已回退原图: {exc}")
        return src, True


def _style_reference_image_role(index: int) -> str:
    return (
        f"STYLE_REFERENCE_HIGHEST_PRIORITY: style reference image #{index}; "
        "MUST control global visual style, color palette, composition rhythm, typography hierarchy, texture, decoration, and whitespace. "
        "MUST override template/cuisine/city/tone defaults when they conflict. "
        "MUST_NOT be used as a dish, logo, mascot, or source of visible copy."
    )


def _mascot_reference_image_role() -> str:
    return (
        "MASCOT_REFERENCE: official Meituan mascot character reference; "
        "MUST preserve character proportions, ears, face, body shape, and colors if mascot is used. "
        "MUST_NOT control global visual style or replace style reference images."
    )


def _food_or_ambient_reference_image_role(
    req: dict[str, Any],
    index: int,
    explicit_dish_names_by_index: dict[int, str],
    explicit_dish_mapping: bool,
) -> str:
    if explicit_dish_mapping:
        dish_name = explicit_dish_names_by_index.get(index)
        if dish_name is not None:
            label = dish_name or f"reference photo #{index}"
            return (
                f"DISH_REFERENCE: real food reference photo #{index} = {label}; "
                "MUST preserve dish structure, vessel, ingredient colors, realistic material, and natural lighting. "
                "MUST_NOT control global visual style when style reference images exist."
            )
        return (
            f"AMBIENT_REFERENCE: real environment/space/service reference photo #{index}; "
            "MUST be used only for background atmosphere, spatial cues, light, and material context. "
            "MUST_NOT be rendered as an extra dish subject or override style reference images."
        )

    if index == 1:
        return (
            f"DISH_REFERENCE: primary real food/material reference photo #{index}; "
            "MUST preserve the main subject, vessel, ingredient colors, realistic material, and natural lighting. "
            "MUST_NOT control global visual style when style reference images exist."
        )
    return (
        f"AMBIENT_REFERENCE_OR_ADDITIONAL_DISH_REFERENCE: real reference photo #{index}; "
        "use as an additional dish only if it visibly contains food, otherwise use as environment/atmosphere reference. "
        "MUST preserve visible real-world details and MUST_NOT override style reference images."
    )


def _logo_reference_image_role() -> str:
    return (
        "LOGO_REFERENCE: store/brand logo structure reference; "
        "MUST preserve logo shape, internal text strokes, icon elements, colors, and proportions. "
        "MUST_NOT redesign the logo or control global visual style."
    )


def _dedupe_generation_reference_images(
    paths: list[str],
    roles: list[str],
    warnings_list: list[str],
) -> tuple[list[str], list[str]]:
    """Deduplicate references after preprocessing while preserving first-seen order and role alignment."""
    deduped: list[str] = []
    deduped_roles: list[str] = []
    seen: dict[str, str] = {}
    for index, item in enumerate(paths):
        role = roles[index] if index < len(roles) else "REFERENCE_IMAGE: generic reference image."
        path = Path(item)
        try:
            digest = file_sha256(path)
        except Exception:
            deduped.append(item)
            deduped_roles.append(role)
            continue
        first = seen.get(digest)
        if first is None:
            seen[digest] = item
            deduped.append(item)
            deduped_roles.append(role)
        else:
            warnings_list.append(
                f"参考图去重：{path.name} 与 {Path(first).name} 内容相同，已跳过重复传输。"
            )
    return deduped, deduped_roles


def copy_runtime_assets(req: dict[str, Any], out_dir: Path) -> tuple[dict[str, Any], RuntimeAssets]:
    asset_dir = out_dir / "assets"
    fonts_dir = asset_dir / "fonts"
    brand_dir = asset_dir / "brand"
    warnings_list: list[str] = []
    fonts = {
        "regular": copy_asset(FONT_DIR / "Meituan Type-Regular.TTF", fonts_dir),
        "bold": copy_asset(FONT_DIR / "Meituan Type-Bold.TTF", fonts_dir),
    }
    brand = {}
    for src in (
        MEITUAN_LOGO,
        MEITUAN_GROUP_BUYING_LOGO,
        MEITUAN_WHITE_LOGO,
        MEITUAN_GROUP_WHITE_BUYING_LOGO,
        DIANPING_LOGO,
        DIANPING_WHITE_LOGO,
        MASCOT_SHEET,
    ):
        if src.exists():
            brand[src.name] = copy_asset(src, brand_dir)

    req = dict(req)
    assets = dict(req["assets"])
    selected_logo_path: Path | None = None
    selected_white_logo_path: Path | None = None
    if assets.get("use_meituan_logo", True):
        logo_path, white_logo_path, _, _ = select_logo_asset(req)
        if logo_path is None:
            assets["use_meituan_logo"] = False
        selected_logo_path = brand.get(logo_path.name) if logo_path else None
        selected_white_logo_path = brand.get(white_logo_path.name) if white_logo_path else None
        assets["selected_logo_path"] = str(selected_logo_path) if selected_logo_path else ""
        assets["selected_white_logo_path"] = str(selected_white_logo_path) if selected_white_logo_path else ""

    mascot_path: Path | None = None
    mascot_ref_path: Path | None = None
    generation_reference_images: list[str] = []
    generation_reference_image_roles: list[str] = []
    model_reference_fallback_used = False

    # --- Style reference images (highest priority, inserted first) ---
    style_ref_images: list[Path] = []
    copied_style_refs: list[str] = []
    for index, image in enumerate(assets.get("style_reference_images", []), start=1):
        src = resolve_image_path(image, "风格参考图")
        suffix = src.suffix if src.suffix else ".png"
        dst = copy_asset(src, asset_dir, f"style_reference_{index:02d}{suffix}")
        prepared, fallback_used = _prepare_model_reference_image(
            dst,
            asset_dir,
            f"style_reference_{index:02d}",
            "风格参考图",
            warnings_list,
        )
        model_reference_fallback_used = model_reference_fallback_used or fallback_used
        style_ref_images.append(dst)
        copied_style_refs.append(str(prepared))
        generation_reference_images.append(str(prepared))
        generation_reference_image_roles.append(_style_reference_image_role(index))
    assets["style_reference_images"] = copied_style_refs
    if style_ref_images:
        warnings_list.append(
            f"已收到 {len(style_ref_images)} 张风格参考图，已复制到输出 assets/；"
            "生成时将以风格参考图的色调、构图、字体风格、排版比例为最高优先级视觉指导。"
        )

    if assets.get("mascot_mode") in {"auto", "official_reference", "generated_reference", "official_overlay"}:
        mascot_path = brand[MASCOT_SHEET.name]
        if assets.get("mascot_mode") in {"auto", "official_reference", "generated_reference"}:
            skip_mascot = False
            if assets.get("mascot_mode") == "auto":
                text_blob = " ".join([
                    str(req.get("title", "")),
                    str(req.get("campaign", {}).get("theme", "")),
                    str(req.get("campaign", {}).get("offer", "")),
                    str(req.get("campaign", {}).get("cta", "")),
                    str(req.get("store", {}).get("name", "")),
                    str(req.get("store", {}).get("category", "")),
                    str(req.get("style", {}).get("name", "")),
                    str(req.get("style", {}).get("tone", "")),
                ])
                if any(kw in text_blob for kw in MASCOT_SKIP_KEYWORDS):
                    skip_mascot = True
                    warnings_list.append(
                        "mascot_mode=auto: 检测到高端/氛围场景关键词，已跳过传入吉祥物参考图以节省 token。"
                    )
                elif any(kw in text_blob for kw in MASCOT_AUTO_INCLUDE_KEYWORDS):
                    warnings_list.append(
                        "mascot_mode=auto: 检测到吉祥物/IP/亲子相关语义，已传入压缩吉祥物参考图。"
                    )
                else:
                    skip_mascot = True
                    warnings_list.append(
                        "mascot_mode=auto: 默认不上传吉祥物参考图以缩短生图请求；如需袋鼠形象请使用 official_reference。"
                    )
            if not skip_mascot:
                # Optimization 1: compress mascot sheet before sending to model
                mascot_ref_path = _prepare_mascot_ref(mascot_path, asset_dir)
                generation_reference_images.append(str(mascot_ref_path))
                generation_reference_image_roles.append(_mascot_reference_image_role())

    qr_path: Path | None = None
    qr_original_path = ""
    qr_original_sha256 = ""
    qr_asset_sha256 = ""
    if assets.get("qr_code_data_url"):
        source = materialize_data_url(assets["qr_code_data_url"], asset_dir / "qr_code")
        qr_original_path = "data_url"
    elif assets.get("qr_code_attachment"):
        source = resolve_image_path(assets["qr_code_attachment"], "二维码图片")
        qr_original_path = str(source)
    elif assets.get("qr_code_path"):
        source = resolve_image_path(assets["qr_code_path"], "二维码图片")
        qr_original_path = str(source)
    else:
        source = None
    if source:
        qr_original_sha256 = file_sha256(source)
        suffix = source.suffix if source.suffix else ".png"
        qr_path = copy_asset(source, asset_dir, "qr_code" + suffix)
        qr_asset_sha256 = file_sha256(qr_path)
        assets["qr_code_path"] = str(qr_path)
        assets["qr_asset_path"] = str(qr_path)
        assets["qr_sha256"] = qr_asset_sha256
        warnings_list.append("二维码已复制到输出 assets/；不会传入图像生成模型，最终由后合成脚本原图贴入。")
    elif assets.get("qr_code_not_needed"):
        warnings_list.append("用户已明确不需要二维码；最终海报不执行二维码后合成。")

    food_images: list[Path] = []
    copied_refs: list[str] = []
    dish_refs_for_roles = dish_reference_items(req)
    explicit_dish_mapping_for_roles = bool(dish_refs_for_roles)
    dish_names_by_reference_index = {
        int(item["reference_image_index"]): normalize_space(item.get("name")) or f"reference photo #{item['reference_image_index']}"
        for item in dish_refs_for_roles
    }
    for index, image in enumerate(assets.get("reference_images", []), start=1):
        src = resolve_image_path(image, "参考图")
        suffix = src.suffix if src.suffix else ".png"
        dst = copy_asset(src, asset_dir, f"reference_{index:02d}{suffix}")
        prepared, fallback_used = _prepare_model_reference_image(
            dst,
            asset_dir,
            f"reference_{index:02d}",
            "参考图",
            warnings_list,
        )
        model_reference_fallback_used = model_reference_fallback_used or fallback_used
        food_images.append(dst)
        copied_refs.append(str(prepared))
        generation_reference_images.append(str(prepared))
        generation_reference_image_roles.append(
            _food_or_ambient_reference_image_role(
                req,
                index,
                dish_names_by_reference_index,
                explicit_dish_mapping_for_roles,
            )
        )
    assets["reference_images"] = copied_refs
    if food_images:
        if req.get("type") == "五连图":
            warnings_list.append("实拍图已复制到输出 assets/，可作为五连图图像模型参考；第一张默认视为最重要实拍图。")
        else:
            warnings_list.append("真实菜品/素材图已复制到输出 assets/，可作为图像模型参考图；发布前需核对真实性。")

    logo_source_value = assets.get("brand_logo_path") or assets.get("store_logo_path")
    if logo_source_value:
        logo_source = resolve_image_path(logo_source_value, "门店/品牌 Logo")
        suffix = logo_source.suffix if logo_source.suffix else ".png"
        logo_dst = copy_asset(logo_source, asset_dir, "brand_logo" + suffix)
        logo_prepared, fallback_used = _prepare_model_reference_image(
            logo_dst,
            asset_dir,
            "brand_logo",
            "门店/品牌 Logo",
            warnings_list,
        )
        model_reference_fallback_used = model_reference_fallback_used or fallback_used
        if assets.get("brand_logo_path"):
            assets["brand_logo_path"] = str(logo_prepared if logo_prepared.suffix.lower() == ".png" else logo_dst)
        if assets.get("store_logo_path"):
            assets["store_logo_path"] = str(logo_prepared if logo_prepared.suffix.lower() == ".png" else logo_dst)
        generation_reference_images.append(str(logo_prepared))
        generation_reference_image_roles.append(_logo_reference_image_role())
        warnings_list.append("门店/品牌 Logo 已作为模型参考图传入，不执行本地强制叠加。")

    # 去重必须在预处理之后执行：不同原图在降采样/转码后可能得到相同内容。
    generation_reference_images, generation_reference_image_roles = _dedupe_generation_reference_images(
        generation_reference_images,
        generation_reference_image_roles,
        warnings_list,
    )
    if model_reference_fallback_used:
        assets["_model_reference_fallback_used"] = True

    req["assets"] = assets
    runtime = RuntimeAssets(
        asset_dir=asset_dir,
        fonts=fonts,
        brand=brand,
        selected_logo_path=selected_logo_path,
        selected_white_logo_path=selected_white_logo_path,
        mascot_path=mascot_path,
        mascot_ref_path=mascot_ref_path,
        qr_path=qr_path,
        qr_original_path=qr_original_path,
        qr_original_sha256=qr_original_sha256,
        qr_asset_sha256=qr_asset_sha256,
        food_images=food_images,
        generation_reference_images=generation_reference_images,
        generation_reference_image_roles=generation_reference_image_roles,
        warnings=warnings_list,
    )
    return req, runtime


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def split_display_text_lines(text: str, max_line_chars: int = 7) -> list[str]:
    """Suggest semantic line breaks for longer Chinese display copy.

    Avoids breaking in the middle of Chinese two-character words by preferring
    natural break points:
      - After a digit+unit pair (e.g. "8折", "68元", "5份") — the unit closes a token
      - Before a digit that starts a new token (e.g. "炸鸡|8折尝鲜")
      - Between two CJK chars where the next char starts a common verb/adj (享/尝/到/来/有...)

    When no ideal break is found, it backs off 1-2 chars to avoid leaving a
    single trailing character (orphan line).
    """
    text = normalize_space(text)
    if not text:
        return []
    # First split on whitespace / punctuation into semantic phrases
    phrases = [part for part in re.split(r"\s+", text) if part]
    if len(phrases) <= 1:
        phrases = [part for part in re.split(r"[，,、/|｜]+", text) if part]

    # Characters that typically close a numeric token (break AFTER these).
    # NOTE: "送/赠" excluded — they are verbs (e.g. "买一送一") not unit closers.
    UNIT_CLOSERS = set("折元份杯%起减免个只瓶箱")
    # Characters that are bad to leave as a single orphan on the next line
    # (they need their preceding context)
    STICKY_SUFFIX = set("折元份杯%起减免赠送的了吗呢啊哦一")

    lines: list[str] = []
    for phrase in phrases:
        phrase = phrase.strip()
        if not phrase:
            continue
        while len(phrase) > max_line_chars:
            best_break = None
            best_score = -1
            # Search window: prefer breaking around max_line_chars, but allow
            # backing off up to half the line width for a better semantic break
            search_start = max(max_line_chars // 2, 2)
            search_end = min(max_line_chars + 1, len(phrase))
            for try_pos in range(search_end - 1, search_start - 1, -1):
                if try_pos >= len(phrase):
                    continue
                char_before = phrase[try_pos - 1]
                char_after = phrase[try_pos]
                score = 0
                # After a unit closer (e.g. "8折|尝鲜") — best break point
                if char_before in UNIT_CLOSERS:
                    score = 15
                # Before a digit that starts a new numeric token (e.g. "炸鸡|8折")
                elif char_after.isdigit() and '\u4e00' <= char_before <= '\u9fff':
                    score = 9
                # After a digit followed by CJK that is NOT a unit (e.g. rare)
                elif char_before.isdigit() and '\u4e00' <= char_after <= '\u9fff' and char_after not in UNIT_CLOSERS:
                    score = 5
                # Generic CJK-to-CJK break — acceptable but not ideal
                elif '\u4e00' <= char_before <= '\u9fff' and '\u4e00' <= char_after <= '\u9fff':
                    score = 3

                # Penalty: if breaking here leaves the next char as a sticky suffix
                if score > 0 and char_after in STICKY_SUFFIX:
                    score -= 2
                # Hard penalty: never leave a single char alone on the next line
                remaining = len(phrase) - try_pos
                if remaining == 1:
                    score -= 20
                # Bonus: prefer breaks closer to max_line_chars (balanced lines)
                if score > 0:
                    distance_penalty = abs(try_pos - max_line_chars) * 0.5
                    score -= distance_penalty

                if score > best_score:
                    best_score = score
                    best_break = try_pos

            # Fallback: if no good semantic break found
            if best_break is None or best_score <= 0:
                remainder = len(phrase) - max_line_chars
                if remainder == 1:
                    best_break = max_line_chars - 1
                elif remainder == 0:
                    best_break = max_line_chars
                else:
                    best_break = max_line_chars

            lines.append(phrase[:best_break])
            phrase = phrase[best_break:]
        if phrase:
            lines.append(phrase)
    return lines or [text]


def ai_display_text_candidates(req: dict[str, Any], max_items: int = 4, max_chars: int = 24) -> list[dict[str, Any]]:
    """Return short, user-provided display text candidates for the image model.

    Priority order: title > offer (discount/subtitle) > cta > theme.
    theme is deprioritised because it often duplicates the title semantically.
    max_items=4 so all explicitly user-provided texts can reach allowed_text.
    max_chars is intentionally longer than copy-candidate guidance; user-selected
    slogans must not be silently shortened before reaching the image model.
    """
    selected_text = normalize_space(req.get("copy", {}).get("selected_text", ""))
    if selected_text:
        sources = [("copy.selected_text", selected_text)]
    else:
        sources = [
            ("title", req.get("title", "")),
            ("campaign.offer", req.get("campaign", {}).get("offer", "")),
            ("campaign.cta", req.get("campaign", {}).get("cta", "")),
            ("campaign.theme", req.get("campaign", {}).get("theme", "")),
        ]
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for field_name, raw_value in sources:
        text = normalize_space(raw_value)
        if not text:
            continue
        safe_text = re.sub(r"[\"<>]", "", text)
        if not safe_text:
            continue
        compact_key = compact_text(safe_text)
        if compact_key in seen:
            continue
        seen.add(compact_key)
        truncated = safe_text
        was_truncated = False
        if len(truncated) > max_chars:
            truncated = truncated[:max_chars]
            was_truncated = True
        candidates.append({
            "field": field_name,
            "text": truncated,
            "original": text,
            "truncated": was_truncated,
            "suggested_lines": split_display_text_lines(truncated),
        })
        if len(candidates) >= max_items:
            break
    return candidates


def trim_prompt_text(value: Any, max_chars: int = 420) -> str:
    text = normalize_space(value)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _build_visual_requirements(
    has_qr: bool,
    text_rule: str,
    realism: str = "balanced",
    include_logo_safe_zone: bool = True,
    style_reference_dominant: bool = False,
) -> list[str]:
    """Build visual_requirements list, conditionally including QR rules."""
    reqs: list[str] = [
        (
            "整张图是完整的商业物料，画面和谐统一；当存在风格参考图时，整体色调、构图、字体、材质和版式节奏以风格参考图为最高优先级。"
            if style_reference_dominant
            else "整张图是完整的商业物料，画面和谐统一，视觉风格与 style.guidance 高度一致。"
        ),
    ]
    if has_qr:
        reqs.append("除二维码预留位外不要留空白或无意义空白；画面饱满，每个区域都有视觉价值。")
    else:
        reqs.append(
            "不要留任何空白、占位框、白色方块或预留区域；画面饱满，每个区域都有视觉价值。"
            "DO NOT leave any white placeholder, blank card, empty frame, or reserved area anywhere in the poster."
        )
    reqs.extend([
        "主标题有冲击力、醒目突出，辅助信息克制，整体避免平庸模板感。",
        (
            "TITLE INTEGRITY: The title text in allowed_text MUST be rendered COMPLETELY — every single character including units (元/折/份/杯) "
            "must appear in the poster. Do NOT truncate, abbreviate or omit any character from the title. "
            "If title_style.highlight is specified, render the highlighted portion (e.g. '8 元') in the specified color (e.g. red #FF0000) "
            "and make it visually larger/bolder than surrounding text for maximum impact."
        ),
        text_rule,
    ])
    if include_logo_safe_zone:
        reqs.append(
            "BRAND LOGO SAFE ZONE: "
            "The TOP-LEFT corner (upper 15% height × left 30% width) will have a brand logo composited on top later. "
            "Keep this zone FREE of text, headline characters, and high-contrast busy elements. "
            "Subtle, low-contrast decorations (soft gradient, blurred bubbles, distant clouds, gentle bokeh) are perfectly fine and encouraged to keep the scene natural. "
            "Just ensure no text or visually competing objects overlap this area. "
            "If the headline is large, position it center-right or start it below 15% from the top edge."
        )
    if style_reference_dominant:
        reqs.append(
            "SEASON & CONTEXT INFERENCE: read campaign.title, campaign.theme, campaign.offer, style.season_atmosphere, and style.notes carefully. "
            "Seasonal cues may adjust supporting props, accent colors, and atmosphere only when they do not conflict with the uploaded style reference. "
            "Do not replace the reference image's design language with generic seasonal templates."
        )
    else:
        reqs.append(
            "SEASON & CONTEXT INFERENCE: read campaign.title, campaign.theme, campaign.offer, style.season_atmosphere, and style.notes carefully. "
            "If they mention spring/春 → use bright fresh green, cherry blossom pink, warm sunshine yellow, 3D grass hills, flowers, butterflies. "
            "If they mention summer/夏 → use cool blue, tropical vibes, ice, watermelon. "
            "If they mention autumn/秋 → use warm orange, maple leaves, harvest gold. "
            "If they mention winter/冬 → use warm red, snowflakes, cozy atmosphere. "
            "The seasonal atmosphere MUST dominate the entire visual — palette, props, lighting, and scene elements should all reinforce the season. "
            "Do NOT default to generic dark/night/ink-wash backgrounds when a bright seasonal mood is specified; "
            "prefer cheerful, cute 3D rendered miniature scenes with volumetric lighting for spring/summer campaigns."
        )
    if has_qr:
        qr_rule = ARTISTIC_QR_HOST_ALIGNMENT_RULE if realism == "artistic" else QR_HOST_ALIGNMENT_RULE
        reqs.append(qr_rule)
        reqs.append(
            "QR 宿主区域必须远离标题、logo、吉祥物脸部，后续真实 QR 会贴入这个白色内区。"
            "CRITICAL QR POSITION: The QR hosting area MUST be placed in the RIGHT-CENTER or RIGHT-BOTTOM quadrant of the poster "
            "(horizontally in the right 40%, vertically between 40%-80% of height). "
            "Do NOT place it in the top-right corner or top area — that conflicts with the headline zone. "
            + (
                "Use a clean flat border suitable for graphic illustration."
                if realism == "artistic"
                else "The QR host must have a CLEAN, PLAIN white square interior with NO decorative patterns, "
                     "NO ornate borders, NO corner ornaments inside the white area. "
                     "The exterior can blend with the scene, but the interior must be pure flat white."
            )
        )
    return reqs


def _lookup_style_tag(kind: str, tag: str) -> dict[str, Any]:
    if not tag:
        return {}
    data = read_reference_json("style_tags.json")
    bucket = data.get(kind, {})
    if not isinstance(bucket, dict):
        return {}
    value = bucket.get(tag, {})
    return value if isinstance(value, dict) else {}


def _style_controls() -> dict[str, Any]:
    return read_reference_json("style_controls.json")


FOOD_REFERENCE_NEGATIVE_TERMS = [
    "no AI-rendered food",
    "no 3D-rendered dish",
    "no toy-like dish",
    "no plastic food texture",
    "no over-polished sauce",
    "no waxy ingredients",
    "no waxy food",
    "no fake perfect food surface",
    "no miniature diorama food",
    "no painterly smearing",
    "no smeared food texture",
    "no over-smoothed food surface",
    "no mushy ingredient edges",
    "no oil-paint food texture",
    "no heavy greasy sheen",
    "no greasy filter",
    "no artificial steam haze over the dish",
    "no global warm amber filter",
    "no oversaturated orange color grade",
    "no invented extra dishes",
    "no duplicate side dish",
    "no blurred restaurant diners behind the dish",
    "no underexposed food",
    "no muddy low-contrast dish",
    "no blurred food details",
    "no dull matte food surface",
    "no gray ink wash over the dish",
    "no washed-out food colors",
    "no dark muted dish subject",
]

FOOD_REFERENCE_STYLE_REPLACEMENTS = {
    "油亮食材": "参考图真实食材色彩",
    "锅气蒸汽": "克制背景动势",
    "火焰蒸汽": "克制背景动势",
    "蒸汽": "轻微背景氛围",
    "油光": "参考图真实高光",
    "油亮": "参考图真实高光",
    "glossy chili oil": "reference-accurate sauce color",
    "soft steam glow around food": "subtle background atmosphere away from the dish",
    "steam and oil sheen": "subtle background atmosphere away from the dish",
    "rim-lit steam": "background graphic energy away from the dish",
    "Wisps of steam": "subtle background atmosphere away from the dish",
    "cinematic warm color grading": "neutral color correction that preserves the reference photo",
    "warm amber": "reference-accurate color temperature",
    "molten orange": "controlled accent color away from the dish",
}


def _reference_image_realism_constraint() -> str:
    controls = _style_controls()
    value = controls.get("realism_reference_image_constraint", "") if isinstance(controls, dict) else ""
    return normalize_space(value)


def _food_reference_fidelity_section(ref_count: int) -> dict[str, Any]:
    if ref_count <= 0:
        return {}
    return {
        "priority": "参考图保真优先；此约束高于模板中的 3D、微缩、黏土、塑料、Pixar、diorama 等风格词。",
        "reference_count": ref_count,
        "dish_subject_rule": (
            "菜品主体锁定：菜品主体采用真实美食摄影感。保留参考图的主要器皿、菜品结构、食材色泽、真实材质、光线方向和自然瑕疵；"
            "允许适度商业美化，包括轻度补光、轻微提亮、清理杂乱背景、克制增强食欲色彩和融入海报构图。"
            "禁止把菜品处理成涂抹油画、厚重油光、蜡质塑料、暖黄滤镜或虚构餐厅大片。"
        ),
        "brightness_clarity_rule": (
            "菜品亮度和清晰度下限：菜品主体必须是 well-lit dish subject，整体曝光充足但不过曝；"
            "保留辣椒、肉类、汤汁和盘沿的清晰边缘、细节纹理和自然食物高光，形成 sharp ingredient details。"
            "可以做轻度补光、自然对比度提升、适度锐化和局部提亮，让菜品比背景更清楚、更有食欲；"
            "禁止暗沉低对比、灰墨感覆盖菜品、模糊细节或把菜品做成哑光发闷。"
        ),
        "style_boundary": (
            "模板中的 3D、微缩、黏土、塑料、Pixar、diorama、油亮、蒸汽、电影暖调等描述只能用于标题、版式、色块或远离菜品的背景装饰，不能作用到菜品主体。"
        ),
        "constraint": _reference_image_realism_constraint(),
    }


def _appetite_color_policy(style_id: str) -> dict[str, str]:
    policy = {
        "name": "appetite_color_policy",
        "default_policy": (
            "默认明亮开胃：餐饮物料优先使用充足曝光、高显色食材、明快暖色或清爽浅色背景、真实食物高光和干净对比。"
            "减少暗黑、暗红、低曝光、泥感红黑和大面积压暗背景；即使做强冲击促销，也用亮朱红、番茄红、暖橙、金黄、奶油白、鲜绿等更有食欲的组合。"
        ),
        "food_subject_rule": (
            "菜品或 AI 生成的食欲主体必须是 well-lit dish subject，边缘清晰、汤汁/肉类/蔬菜色泽自然鲜明，"
            "不得暗沉、发灰、低对比或被背景色吞掉。"
        ),
    }
    if style_id in DARK_STYLE_FOOD_PROTECTION:
        policy["dark_style_food_protection"] = (
            "显式深色风格菜品保护：允许暗色、霓虹、黑金、工业或夜场氛围用于背景深度、材质和装饰，"
            "但深色只作用于背景氛围；菜品主体必须保持 well-lit dish subject、清晰细节、自然食物高光和可激发食欲的真实色泽。"
        )
    return policy


def _sanitize_food_reference_style_text(text: str) -> str:
    sanitized = normalize_space(text)
    for source, replacement in FOOD_REFERENCE_STYLE_REPLACEMENTS.items():
        sanitized = sanitized.replace(source, replacement)
    return sanitized


def _food_reference_safe_style_guidance(template: dict[str, Any]) -> dict[str, str]:
    style_id = style_id_of(template)
    label = normalize_space(template.get("label"))
    color_strategy = normalize_space(template.get("color_strategy"))
    composition_strategy = normalize_space(template.get("composition_strategy"))
    return {
        "scene_prompt": (
            f"REFERENCE FOOD SAFE STYLE MODE — {label} ({style_id}). "
            "菜品主体锁定：uploaded dish photo is the source of truth for the dish subject. "
            "Apply this style only to poster layout, typography, color blocks, background graphics, and non-food decorations. "
            "Do NOT transfer style texture, oil sheen, steam haze, warm amber grading, painterly smoothing, or dramatic restaurant bokeh onto the dish subject. "
            "Do NOT invent extra plates, duplicate side dishes, or unrelated restaurant diners behind the dish."
            " Keep the dish subject well-lit, sharp, naturally glossy, and more visually clear than the background."
        ),
        "design": (
            "菜品主体锁定：菜品按参考图保持真实摄影质感；"
            f"海报排版可使用 color_strategy={color_strategy} composition_strategy={composition_strategy}，"
            "但颜色、光影和质感只作用于背景、标题和装饰，不覆盖菜品主体。"
            "菜品主体需要曝光充足、细节清晰、自然食物高光可见，避免暗沉和低对比。"
        ),
        "texture_guidance": "菜品主体不继承模板质感词；食材、汤汁、器皿、金属反光和表面纹理按参考图真实呈现。",
    }


def _food_reference_safe_panorama_family(family: dict[str, Any]) -> dict[str, Any]:
    safe = dict(family)
    for key in ("background", "composition"):
        safe[key] = _sanitize_food_reference_style_text(safe.get(key, ""))
    safe["food_subject_rule"] = "菜品主体锁定参考图；背景家族风格只能作为远离菜品的装饰和连续长卷氛围。"
    return safe


VALID_PANORAMA_DENSITIES = {"clean", "rich", "explosive"}
VALID_PANORAMA_TEXT_PLACEMENTS = {"left_block", "center_overlay", "top_banner"}
STYLE_DEFAULT_DENSITY = {
    "clean_hero": "clean",
    "korean_cream": "clean",
    "urban_chic": "clean",
    "zen_tea_modern": "clean",
    "poetic_leisure": "clean",
    "collage_pop": "explosive",
    "bold_split": "explosive",
    "festive_red": "explosive",
    "neon_night": "explosive",
}
STYLE_TEXT_PREFERENCE = {
    "ink_oriental": "top_banner",
    "natural_earth": "top_banner",
    "collage_pop": "center_overlay",
    "oversize_type": "center_overlay",
    "bold_split": "center_overlay",
}
TEXT_PLACEMENT_BOXES = {
    "left_block": {"x": 0.035, "y": 0.167, "w": 0.345, "h": 0.667},
    "center_overlay": {"x": 0.25, "y": 0.15, "w": 0.30, "h": 0.70},
    "top_banner": {"x": 0.05, "y": 0.10, "w": 0.55, "h": 0.25},
}
TEXT_PLACEMENT_COMPOSITIONS = {
    "left_block": (
        "left_block: title occupies 3.5%-38% width, vertically centered, integrated with background texture; "
        "hero food anchor sits in slice_02-03 center-right, supporting dishes extend with varied scale."
    ),
    "center_overlay": (
        "center_overlay: title overlays the scene as an integrated graphic layer around 25%-55% width, "
        "with hero food layered behind or around title without blocking readability."
    ),
    "top_banner": (
        "top_banner: a designed top title area integrated into the background illustration, not a separate app-style banner or platform header; "
        "hero food anchor sits lower-center across slice_02-03."
    ),
}
HERO_POSITION_BY_TEXT_PLACEMENT = {
    "left_block": "slice_02-03 center-right",
    "center_overlay": "slice_01-02 center, layered behind or around title",
    "top_banner": "slice_02-03 lower-center",
}
HERO_METRICS_BY_DENSITY = {
    "clean": {"height_ratio": "38%-46% of content strip height", "slice_span": "about 0.8-1.1 panels", "crop_policy": "fully visible; no cropping"},
    "rich": {"height_ratio": "46%-54% of content strip height", "slice_span": "about 1.0-1.4 panels", "crop_policy": "mostly visible (>85%); minor side edge crop acceptable, never bottom"},
    "explosive": {"height_ratio": "54%-64% of content strip height", "slice_span": "about 1.2-1.8 panels", "crop_policy": "controlled partial bleed on side or top edges only; never bleed into the bottom white margin"},
}
PANORAMA_HARD_NEGATIVE = [
    "no QR code",
    "no QR modules",
    "no fake QR",
    "no self-made or altered Meituan/Dianping platform logo",
    "no invented price/date/rule/store claim",
    "no watermark",
    "no segmented five separate posters",
    "no content in the bottom white margin",
    "no dishes or text spilling into the lower blank area",
    "no non-white pixels in the bottom white area",
    "no shadow, steam, smoke, glow, gradient, texture, or decoration in the bottom white area",
    "no poster card shadow",
    "no copied layout mask gray fill or guide line",
    "no inset banner inside a larger canvas",
    "no picture-in-picture panorama",
    "no severely cropped hero dish",
    "no hero dish cut in half",
    "no main plate/pot/bowl/serving vessel cut off at bottom edge",
    "no cropped plate rim",
    "no large horizontal tabletop",
    "no tabletop horizon line",
    "no dish pushed to lower edge",
    "no important food subject destroyed by canvas edge",
    "no text cropped or cut off by canvas edges",
    "no text with any character partially hidden by canvas edge",
    "no text rotated so much that it becomes hard to read",
    "no distorted or unreadable text",
    "no random unreadable decorative text; only render provided headline, subtitle, dish names, and approved copy",
]
PANORAMA_STYLE_NEGATIVE_BY_DENSITY = {
    "clean": [
        "avoid cropped dishes",
        "avoid partial plates",
        "avoid large visible dining tables",
        "avoid empty horizontal tabletop surfaces",
        "no unrelated thick platform-like banner",
        "no fake app header bar",
        "no clear center image over blurred enlarged background",
        "no blurred duplicate side panels",
    ],
    "rich": [
        "avoid large visible dining tables",
        "avoid empty horizontal tabletop surfaces",
        "avoid food simply arranged on one table",
        "no unrelated thick platform-like banner",
        "no clear center image over blurred enlarged background",
        "no blurred duplicate side panels",
    ],
    "explosive": [
        "avoid dirty or low-perspective tabletops",
        "avoid food simply arranged on one table",
        "no opaque banner that blocks the food scene",
    ],
}
PANORAMA_DENSITY_PERMISSIONS = {
    "clean": (
        "Keep all dishes mostly complete. Use subtle visual anchoring (shadow, light, color block, plate rim) and restrained layering. "
        "Breathing room is generous but food is not weightless."
    ),
    "rich": (
        "Supporting dishes may be softly cropped at side/top canvas edges only; never into the bottom white margin. "
        "Hero dish remains mostly complete (>85% visible). Ingredients, steam, and decorative elements may extend beyond side/top edges naturally; never into the bottom white margin."
    ),
    "explosive": (
        "Hero anchor may have controlled partial bleed on side or top edges only; never into the bottom white margin. "
        "Supporting dishes, ingredients, flames, steam, ice, and props may crop naturally at side/top canvas edges; never into the bottom white margin. "
        "Dense layering and overlap between non-hero elements is encouraged."
    ),
}
PANORAMA_DISH_SCALE_RULE_BY_DENSITY = {
    "clean": "Use non-uniform food scale: one hero dish (largest, visually anchored), plus 1-2 smaller supporting dishes with strong breathing room. Avoid equal-size lineup or equal-spacing arrangement.",
    "rich": "Use non-uniform food scale: one hero anchor (largest), 2-3 medium supporting dishes at varied sizes and angles, plus smaller ingredient accents and props for visual rhythm. Avoid equal-size, equal-spacing dish lineup.",
    "explosive": "Use dramatic non-uniform food scale: one oversized hero anchor dominating the scene, multiple medium dishes at varied angles and distances, many small ingredients, flames, steam, and graphic accents filling gaps. Avoid any orderly equal-size arrangement.",
}
PANORAMA_DENSITY_MODIFIERS = {
    "clean": "Density direction: fewer elements, generous whitespace, magazine-level breathing room. Background serves as subtle texture, not visual noise. Food is visually anchored — not floating weightlessly — but the scene remains minimal and refined.",
    "rich": "Density direction: multiple layers of texture, depth, and visual interest. Background has atmosphere and spatial cues. Food is anchored by scene context — environment, materials, light, and supporting elements. Non-uniform dish scale creates visual rhythm across the panorama.",
    "explosive": "Density direction: fill the frame, maximize visual density and energy. Ingredients, flames, steam, props, and graphic accents fill gaps between dishes. Hero anchor dominates, supporting elements surround densely. Controlled chaos — high energy but still visually organized with a clear focal point.",
}
_DENSITY_ANCHORING = {
    "clean": "Food anchored by shadow, light, color block, or plate rim — minimal but present. Not floating weightlessly.",
    "rich": "Food anchored by multi-layer scene context: environment, materials, light, supporting elements.",
    "explosive": "Food anchored by ingredient piles, flames, steam, dense graphic layers — explosive presence.",
}
CUISINE_FAMILY_PRIORITY_RULES = [
    ("火锅专门", ("火锅", "涮", "锅底")),
    ("烧烤夜宵", ("烧烤", "烤串", "羊肉串", "夜宵", "宵夜", "串串")),
    ("咖啡甜品", ("咖啡", "甜品", "蛋糕", "奶茶", "面包", "烘焙")),
    ("日韩精致", ("日料", "寿司", "刺身", "韩餐", "韩式", "居酒屋")),
    ("东南亚热带", ("泰", "越南", "东南亚", "咖喱")),
    ("西餐洋食", ("西餐", "披萨", "汉堡", "意面", "牛排")),
    ("川湘辣系", ("川", "湘", "麻辣", "冒菜", "酸菜鱼", "辣", "赣", "江西")),
    ("粤闽精细", ("粤", "闽", "潮汕", "早茶", "肠粉", "煲仔")),
    ("江浙淮扬", ("江南", "淮扬", "本帮", "杭帮", "苏菜")),
    ("西北豪放", ("西北", "新疆", "陕西", "兰州", "羊肉", "馕")),
    ("东北大气", ("东北", "铁锅", "炖")),
    ("中餐通用", ("中餐", "家常", "粉面", "饺子", "包子")),
]
VALID_CUISINE_FAMILIES = {family for family, _ in CUISINE_FAMILY_PRIORITY_RULES} | {"餐饮通用"}


def _infer_visual_density(req: dict[str, Any], style_id: str) -> str:
    requested = normalize_space(req.get("style", {}).get("visual_density"))
    if requested in VALID_PANORAMA_DENSITIES:
        return requested
    return STYLE_DEFAULT_DENSITY.get(style_id, "rich")


def _infer_cuisine_family(req: dict[str, Any]) -> str:
    explicit = normalize_space(req.get("style", {}).get("cuisine_family"))
    if explicit in VALID_CUISINE_FAMILIES:
        return explicit
    blob = request_text_blob(req)
    for family, keywords in CUISINE_FAMILY_PRIORITY_RULES:
        if any(keyword in blob for keyword in keywords):
            return family
    return "餐饮通用"


def _infer_text_placement(style_id: str, cuisine_family: str, visual_density: str) -> str:
    preference = STYLE_TEXT_PREFERENCE.get(style_id, "left_block")
    if visual_density == "clean" and preference == "center_overlay":
        preference = "left_block"
    return preference


def _resolve_text_placement(req: dict[str, Any], style_id: str, cuisine_family: str, visual_density: str) -> str:
    requested = normalize_space(req.get("style", {}).get("text_placement"))
    if requested in VALID_PANORAMA_TEXT_PLACEMENTS:
        return requested
    return _infer_text_placement(style_id, cuisine_family, visual_density)


def _resolve_style_reference_text_placement(req: dict[str, Any], fallback: str) -> str:
    requested = normalize_space(req.get("style", {}).get("text_placement"))
    if requested in VALID_PANORAMA_TEXT_PLACEMENTS:
        return requested

    note = normalize_space(req.get("assets", {}).get("style_reference_note"))
    if any(keyword in note for keyword in ("左侧", "左边", "左栏", "左上", "左下", "左文", "左置")):
        return "left_block"
    if any(keyword.lower() in note.lower() for keyword in ("顶部", "上方", "顶栏", "横幅", "banner", "top")):
        return "top_banner"
    if any(keyword in note for keyword in ("居中", "中央", "中心", "中间", "中置")):
        return "center_overlay"

    return fallback if fallback in VALID_PANORAMA_TEXT_PLACEMENTS else "left_block"


def _slice_seam_safety_policy() -> dict[str, Any]:
    return {
        "slice_boundaries_at": ["20%", "40%", "60%", "80%"],
        "exclusion_zone": "2.5% canvas width on each side of every slice boundary",
        "text_rule": (
            "Text blocks may span multiple panels to create momentum, but no individual Chinese character or critical word should sit directly across a slice boundary. "
            "Keep every character stroke at least 2% canvas width away from any slice seam. If a headline spans two panels, place the seam in inter-character space, not mid-stroke."
        ),
        "visual_subject_rule": "Also avoid placing key dish edges, hero serving vessel edges, user-provided logo assets, and human faces directly on slice seams.",
        "slice_05_rule": "Slice 05 must feel intentionally finished, not like the panorama was randomly cut off.",
    }


def _visual_anchoring_policy(visual_density: str) -> dict[str, Any]:
    return {
        "principle": (
            "Food should feel visually anchored, not weightless. Use bowls, plates, pots, grills, ice beds, ingredient piles, flames, steam, smoke, "
            "shadows, graphic color blocks, or scene elements as anchors. Avoid large empty tabletops or all dishes sitting on one horizontal table."
        ),
        "preferred_anchors": [
            "plate rim", "bowl shadow", "pot edge", "grill grate edge", "ice bed", "ingredient scatter",
            "flame/steam base", "smoke layer", "graphic color block", "cast shadow", "light halo", "fabric/paper texture zone",
        ],
        "limited_surface_anchors": ["small partial counter edge", "small wooden bar section", "small stone slab"],
        "surface_rule": "Surface anchors must stay local and partial — the surface should read as a small local anchor, not as the main background or stage. Never a large empty tabletop or dominant horizontal plane.",
        "density_anchoring": _DENSITY_ANCHORING[visual_density],
    }


def _panorama_background_design(visual_density: str, panorama_family: dict[str, Any]) -> dict[str, str]:
    layers = {
        "clean": "底色材质层 + 一个空间/氛围暗示层 + 克制光影层 + 前景菜品层。",
        "rich": "底色材质层、空间/氛围暗示层、菜系装饰层、光影/颗粒/雾气层、前景菜品层。",
        "explosive": "底色材质层、空间/氛围暗示层、菜系装饰层、食材/火焰/蒸汽/动势密度层、前景菜品层。",
    }
    return {
        "goal": "背景必须服务于连续长卷和食欲焦点，按 density 增减层次，不能只是纯浅黄、简单线稿、单一渐变或空白纹理板。",
        "required_layers": layers[visual_density],
        "allowed_contexts": "可使用远景山水、村落、产地、街巷、门店环境、室内烟火、人气氛围、纸张拼贴、墙面纹理、植物、建筑剪影和地方纹样作为背景层。",
        "style_specific_direction": panorama_family.get("background", ""),
        "continuity_rule": "五张切片共享同一套背景世界观和光影方向，允许局部变化，但不能像五张互不相关的海报。",
    }


def _panorama_slice_storyboard(visual_density: str) -> dict[str, str]:
    if visual_density == "clean":
        return {
            "slice_01": "identity — brand name/headline/cuisine cue/regional identity, generous breathing.",
            "slice_02": "hero_focus — hero dish (1-2 dishes), visually anchored presentation.",
            "slice_03": "secondary — supporting dishes, breathing room maintained.",
            "slice_04": "atmosphere — craft/ingredient/scene suggestion.",
            "slice_05": "quiet_closure — color/atmosphere fade, echoes slice_01, intentionally finished.",
        }
    if visual_density == "explosive":
        return {
            "slice_01": "impact_blast — bold headline + primary visual impact.",
            "slice_02": "hero_anchor — oversized hero dish + supporting elements surrounding.",
            "slice_03": "dense_spread — high-density multi-dish + ingredients + decoration.",
            "slice_04": "texture_energy — close-up/process/collage layers.",
            "slice_05": "energy_closure — maintain style energy, but use a closing dish/signature element/color fade to create intentional finish; never abrupt cut or random clutter.",
        }
    return {
        "slice_01": "identity_hook — brand recognition + hero dish partial reveal as hook.",
        "slice_02": "hero_spread — hero dish full display + headline core.",
        "slice_03": "dish_parade — 2-3 dishes non-uniform scale.",
        "slice_04": "context_layer — ingredients/storefront/scene anchoring.",
        "slice_05": "closure_dish — closing dish + natural finish, intentionally finished.",
    }


def _hero_visual_anchor(req: dict[str, Any], dish_refs: list[dict[str, Any]], ref_images: list[str], cuisine_family: str, text_placement: str, visual_density: str) -> dict[str, str]:
    metrics = HERO_METRICS_BY_DENSITY[visual_density]
    if dish_refs:
        what = normalize_space(dish_refs[0].get("name")) or f"dish from reference photo #{dish_refs[0]['reference_image_index']}"
    elif ref_images:
        what = "main dish from reference photo #1"
    else:
        what = f"{cuisine_family} atmospheric visual element (no concrete dish — use steam, utensils, silhouette, or abstract food cues)"
    blob = request_text_blob(req)
    anchor_type = "cooking_moment" if any(keyword in blob for keyword in ("现做", "现炒", "手工", "火锅", "烧烤")) or cuisine_family in {"火锅专门", "烧烤夜宵"} else "hero_dish"
    intent = req.get("scene", {}).get("business_intent", "daily_attract")
    role = "spectacle" if intent == "promotion" else "signature" if intent == "brand_image" else "appetite"
    return {
        "what": what,
        "type": anchor_type,
        "position": HERO_POSITION_BY_TEXT_PLACEMENT[text_placement],
        "height_ratio": metrics["height_ratio"],
        "slice_span": metrics["slice_span"],
        "crop_policy": metrics["crop_policy"],
        "role": role,
    }


def _dish_showcase_integrity(visual_density: str, composition_priority: str) -> dict[str, str]:
    return {
        "priority": "hero dish must remain recognizable and appetizing; no severe crop, no cut-in-half hero, no bottom-margin intrusion.",
        "composition_priority": composition_priority,
        "scale_rule": PANORAMA_DISH_SCALE_RULE_BY_DENSITY[visual_density],
        "safe_band": "Bottom white margin is absolute safety area. Main plate/pot/bowl/serving vessel must never be cut from the bottom-margin direction.",
        "size_rule": "Hero and supporting dish sizes are governed by hero_visual_anchor, dish_scale_rule, and crop_and_layering_permissions rather than fixed equal-size limits.",
    }


def _section_base_style(realism: str) -> str:
    controls = _style_controls()
    frameworks = controls.get("realism_frameworks", {}) if isinstance(controls, dict) else {}
    if isinstance(frameworks, dict) and realism in frameworks:
        return normalize_space(frameworks[realism])
    defaults = {
        "realistic": "Professional food photography style. Natural lighting, real materials, authentic restaurant atmosphere, avoid over-rendering.",
        "balanced": STYLE_HINTS["美团餐饮团购"],
        "artistic": "Bold graphic illustration style. Flat color blocks, simplified shapes, hand-drawn textures, strong visual clarity.",
    }
    return defaults.get(realism, defaults["balanced"])


def _style_reference_base_style() -> str:
    return (
        "风格参考图主导：整体视觉基调、设计手法、材质质感、摄影/插画/平面风格、构图节奏、字体气质和色调氛围都以用户上传的风格参考图为最高优先级。"
        "不要套用默认 3D 微缩场景、固定模板光影或预设材质；仅保留商业物料的清晰度、完整度和可读性要求。"
    )


def _section_tone_modifier(tone: str) -> str:
    if not tone:
        return ""
    controls = _style_controls()
    modifiers = controls.get("tone_modifiers", {}) if isinstance(controls, dict) else {}
    if isinstance(modifiers, dict) and tone in modifiers:
        return f"{tone}: {normalize_space(modifiers[tone])}"
    fallback = {
        "极简清冷": "minimalist composition, generous negative space, cool-toned lighting, restrained palette",
        "热闹喜庆": "festive abundance, warm saturated colors, celebratory props, joyful energy",
        "复古怀旧": "vintage film grain, faded warm tones, retro typography, aged textures",
        "国潮中式": "modern Chinese aesthetic, traditional patterns with contemporary twist",
        "清新自然": "fresh natural light, botanical elements, soft pastel accents, airy composition",
        "奢华精致": "premium materials, gold accents, dramatic lighting, sophisticated restraint",
    }
    return f"{tone}: {fallback[tone]}" if tone in fallback else ""


def _sichuan_hunan_typography_profile_id(req: dict[str, Any], style_id: str) -> str:
    context = _sichuan_hunan_context(req)
    if not context:
        return ""
    if context.get("fresh") or context.get("season") in {"春季", "夏季"}:
        if style_id in {"natural_earth", "clean_hero", "poetic_leisure"}:
            return "warm_handwritten_kai"
        if style_id == "ink_oriental":
            return "elegant_serif_song"
    if context.get("classic"):
        if style_id in {"ink_oriental", "bold_split"}:
            return "bold_brush_calligraphy"
        if style_id in {"retro_poster", "natural_earth"}:
            return "retro_mincho_display"
    if context.get("impact"):
        if style_id == "dynamic_angle":
            return "heavy_impact_sans"
        if style_id == "oversize_type":
            return "ultra_display_heavy"
        if style_id in {"bold_split", "street_warm"}:
            return "bold_brush_calligraphy"
    if context.get("rich_dish"):
        if style_id in {"dark_premium", "ink_oriental"}:
            return "elegant_serif_song"
        if style_id in {"bold_split", "street_warm"}:
            return "bold_brush_calligraphy"
    return ""


def _typography_profile(req: dict[str, Any], style: dict[str, Any] | str) -> dict[str, Any]:
    controls = _style_controls()
    profiles = controls.get("typography_profiles", {}) if isinstance(controls, dict) else {}
    if not isinstance(profiles, dict):
        profiles = {}

    style_profile = ""
    style_id = ""
    if isinstance(style, dict):
        style_profile = normalize_space(style.get("typography_profile"))
        style_id = style_id_of(style)
    requested_id = normalize_space(req.get("style", {}).get("typography_profile"))
    cuisine_profile = _sichuan_hunan_typography_profile_id(req, style_id)
    default_id = cuisine_profile or style_profile or "modern_minimal_sans"
    profile_id = requested_id if requested_id in profiles else default_id
    if profile_id not in profiles and profiles:
        profile_id = next(iter(profiles))
    profile = profiles.get(profile_id, {})
    if not isinstance(profile, dict):
        profile = {}
    return {
        "id": normalize_space(profile.get("id") or profile_id),
        "prompt_guidance": normalize_space(profile.get("prompt_guidance")),
    }


def _sichuan_hunan_dish_focus(req: dict[str, Any]) -> str:
    blob = request_text_blob(req)
    for keyword in (
        "酸菜鱼", "酸汤鱼", "柠檬鱼", "藤椒鱼", "水煮鱼", "小龙虾", "活虾",
        "藕汤", "莲藕汤", "桂林米粉", "螺蛳粉", "宫保鸡丁", "辣子鸡",
        "小炒肉", "江西小炒", "剁椒鱼头", "毛血旺", "回锅肉",
    ):
        if keyword in blob:
            return keyword
    return ""


def _sichuan_hunan_prompt_strategy(req: dict[str, Any]) -> dict[str, Any]:
    context = _sichuan_hunan_context(req)
    if not context:
        return {}
    season = context.get("season", "")
    dish_focus = _sichuan_hunan_dish_focus(req)
    guidance: list[str] = []
    if season == "夏季":
        guidance.append("夏季地域菜系不要默认暗红黑，优先浅绿、浅米、清透浅黄、荷叶/山水/水汽等清爽元素，让辣味与解暑感并存。")
    elif season == "春季":
        guidance.append("春季地域菜系优先嫩绿、浅粉、暖白和自然阳光，减少厚重暗场景。")
    elif season == "秋季":
        guidance.append("秋季地域菜系可以用暖橙、稻谷金、木色和成熟辣椒色，但保持画面明亮。")
    elif season == "冬季":
        guidance.append("冬季地域菜系可以更热烈，但仍要用菜品高光、蒸汽和暖白餐具提亮画面。")

    if context.get("fresh"):
        guidance.append("酸菜鱼、酸汤鱼、藤椒鱼、藕汤、米粉等清爽/汤粉类，走浅色清爽食欲：青绿水墨、浅陶瓷碗、汤色高光、地域山水/植物元素，避免重油暗黑。")
    if context.get("classic"):
        guidance.append("巴蜀、云贵、广西、江西、湖北经典菜或酒家场景，走浅色国风/地域招贴：宣纸浅底、淡绿山水、红色印章、菜品白瓷盘，避免堆满红金。")
    if context.get("impact"):
        guidance.append("爆炒、小炒、辣子鸡、剁椒、江西小炒等下饭菜，走明亮高冲击：椒红、金黄锅气、白盘食物高光和粗标题字，画面要辣但不脏不暗。")
    if context.get("rich_dish"):
        guidance.append("小龙虾、水煮鱼、螺蛳粉、腊肉等浓郁菜可以用暖棕、辣油红和局部暗部，但暗色只服务菜品质感；普通地域菜系不要默认暗色系。")
    if not guidance:
        guidance.append("普通川湘、云贵川、广西、江西、湖北等地域菜系优先明亮开胃：浅底、白瓷、青绿/椒红点缀、真实菜品高光和热气；只有高端餐厅、夜宵、小龙虾/水煮类明确场景才使用暗色系。")

    return {
        "title": "地域菜系季节菜品差异化",
        "applicable_cuisines": "川湘/云贵川/广西/江西/湖北：川湘菜、云南菜、贵州菜、广西菜、江西菜、湖北菜等口味接近的地域菜系。",
        "brightness_policy": "普通地域菜系优先明亮开胃，只有高端餐厅、夜宵、小龙虾/水煮类明确场景才使用暗色系；不要一股脑默认街巷烟火版。",
        "current_or_requested_season": season,
        "dish_focus": dish_focus,
        "season_and_dish_guidance": guidance,
        "typography_strategy": (
            "字体策略：夏季清爽/酸菜鱼/酸汤鱼/藕汤/米粉用松弛手写或温润楷意标题；"
            "巴蜀/云贵/桂赣鄂经典菜用大字书法标题搭配克制宋体副标题；"
            "爆炒小炒促销用重磅粗黑体或豪爽毛笔字；"
            "小龙虾/水煮鱼/螺蛳粉等浓郁质感可用厚重书法或精致宋体。"
            "只使用通用字形描述，不指定或模仿具体商业字体。"
        ),
    }


def _build_cuisine_city_section(req: dict[str, Any]) -> dict[str, Any]:
    style = req.get("style", {})
    tone = style.get("tone", "")
    entries: list[dict[str, Any]] = []
    conflict = False
    for kind, tag in (("cuisine_styles", style.get("cuisine_tag")), ("city_styles", style.get("city_tag"))):
        info = _lookup_style_tag(kind, tag)
        if not info:
            continue
        compatible = info.get("compatible_tones") or []
        if tone and compatible and tone not in compatible:
            conflict = True
            entry = {
                "tag": tag,
                "kind": kind,
                "color_keywords": info.get("color_keywords", ""),
                "elements": info.get("elements", ""),
                "atmosphere": "",
            }
        else:
            entry = {
                "tag": tag,
                "kind": kind,
                "color_keywords": info.get("color_keywords", ""),
                "elements": info.get("elements", ""),
                "atmosphere": info.get("atmosphere", ""),
            }
        entries.append(entry)
    section = {
        "entries": entries,
        "conflict_policy": (
            "tone_overrides_atmosphere; cuisine/city keep only elements and colors"
            if conflict
            else "no_conflict"
        ),
    }
    strategy = _sichuan_hunan_prompt_strategy(req)
    if strategy:
        section["sichuan_hunan_strategy"] = strategy
    return section


def _copy_atmosphere_section(req: dict[str, Any]) -> dict[str, Any]:
    dimensions = req.get("copy", {}).get("dimensions", [])
    guidance = {
        "营销促销": "commercial urgency, direct conversion, clear value cues",
        "餐品特色": "ingredient craft, freshness, signature taste cues",
        "城市地域情感": "local memory, neighborhood culture, recognizable regional warmth",
        "品牌门店故事": "heritage, trust, time, craft and founder story atmosphere",
        "消费场景召唤": "occasion-based invitation, gathering, family or friend dining scene",
        "社交货币种草": "shareable composition, photo-friendly details, social buzz",
        "新品上市": "fresh launch, novelty, discovery and first-try excitement",
        "节日祝福": "seasonal celebration, blessing, festive visual symbols",
    }
    return {
        "visual_guidance": [guidance.get(item, item) for item in dimensions],
        "selected_text": req.get("copy", {}).get("selected_text", ""),
    }


def _brand_constraint_section(req: dict[str, Any]) -> dict[str, Any]:
    profile = req.get("brand_profile", {})
    if not isinstance(profile, dict):
        return {}
    return {
        "brand_name": normalize_space(profile.get("brand_name")),
        "visual_lock": bool(profile.get("visual_lock")),
        "logo_position": normalize_space(profile.get("logo_position")),
        "logo_size_ratio": profile.get("logo_size_ratio"),
        "primary_colors": profile.get("primary_colors") or [],
        "brand_keywords": normalize_list(profile.get("brand_keywords")),
        "priority": "primary_colors override variant palette when visual_lock=true",
    }


def _panorama_decoration_guidance(req: dict[str, Any], template: dict[str, Any]) -> str:
    """Generate decoration and atmosphere guidance based on cuisine_tag, style, and user context."""
    cuisine_tag = req.get("style", {}).get("cuisine_tag", "")
    city_tag = req.get("style", {}).get("city_tag", "")
    tone = req.get("style", {}).get("tone", "")
    style_id = style_id_of(template)
    brand_keywords = req.get("brand_profile", {}).get("brand_keywords", [])

    # Cuisine-specific decoration suggestions
    cuisine_decor_map: dict[str, str] = {
        "广西菜": "竹编器具、瓷碗、桂林山水剪影、亚热带植物叶片、红辣椒串、蒜瓣姜片等食材装饰",
        "川湘菜": "红辣椒、花椒、蒸汽、铁锅、竹筷、麻绳、辣椒串装饰",
        "粤菜": "茶壶茶杯、砂锅、瓷勺、岭南窗花、荷叶、蒸笼",
        "日料": "木质托盘、竹帘、和纸、樱花枝、暖帘元素",
        "火锅": "沸腾蒸汽、辣椒花椒、九宫格、铜锅/红锅、蘸料碟",
        "烤肉/烧烤": "炭火、烟熏气、铁签、啤酒、工业风铁网",
        "西北菜": "陶罐、粗陶碗、羊角辣椒、黄土色调、面粉扬尘",
        "东北菜": "铁锅、玉米、大葱、花棉布、热气腾腾",
        "云南菜": "鲜花、菌菇、竹器、少数民族纹样、自然绿植",
        "咖啡甜品": "拉花杯、甜品碟、奶油花、咖啡豆、马卡龙色",
        "西餐": "银质刀叉、红酒杯、亚麻餐巾、烛光、香草",
        "面馆/粉面": "大碗、汤勺、葱花香菜、蒸汽、面条纹理",
        "烘焙": "面粉撒落、擀面杖、麦穗、烤盘、奶油裱花",
        "东南亚菜": "香茅、椰子壳、芭蕉叶、彩色辛香料、藤编",
    }

    # Style-specific decoration approach
    style_decor_map: dict[str, str] = {
        "ink_oriental": "水墨晕染过渡、留白意境、山水远景剪影、古朴印章元素、宣纸纹理",
        "natural_earth": "自然木纹、亚麻布、晨光光斑、叶片阴影、大地色渐变过渡",
        "dark_premium": "暗色背景光晕、金色光点点缀、高级感光影对比、微妙烟雾",
        "street_warm": "暖光灯泡、街巷砖墙、热气蒸汽、烟火气氛围光",
        "bold_split": "几何色块分割、强对比撞色过渡带、动感斜线或弧线装饰",
        "retro_poster": "做旧纸张纹理、颗粒质感、复古色调叠加、旧式边框",
        "festive_red": "红色灯笼、金色祥云、喜庆边框、中国结元素",
        "clean_hero": "纯净留白、极简线条、柔和阴影、干净色块过渡",
        "collage_pop": "拼贴风色块、不规则边框、趣味贴纸元素、多彩填充",
    }

    parts: list[str] = []
    parts.append(
        "在文案主视觉区和菜品展示区之间，以及画面边缘，"
        "适当融入与菜系和风格匹配的装饰元素来提升设计感和氛围。"
        "装饰元素是氛围补充而非主体，不能喧宾夺主、遮挡菜品。"
    )

    cuisine_decor = cuisine_decor_map.get(cuisine_tag, "")
    if not cuisine_decor:
        # Try partial match
        for key, val in cuisine_decor_map.items():
            if key in cuisine_tag or cuisine_tag in key:
                cuisine_decor = val
                break
    if cuisine_decor:
        parts.append(f"菜系装饰元素建议：{cuisine_decor}。")

    style_decor = style_decor_map.get(style_id, "")
    if style_decor:
        parts.append(f"风格装饰手法：{style_decor}。")

    if city_tag:
        parts.append(f"可融入「{city_tag}」地域特色视觉符号作为点缀（如地标剪影、地方纹样等）。")

    if brand_keywords:
        kw_str = "、".join(brand_keywords[:4])
        parts.append(f"品牌关键词「{kw_str}」可作为装饰方向的参考。")

    return " ".join(parts)


def _panorama_style_family(style_id: str) -> dict[str, str]:
    controls = _style_controls()
    configured = controls.get("panorama_style_families", {}) if isinstance(controls, dict) else {}
    if isinstance(configured, dict):
        value = configured.get(style_id) or configured.get("default")
        if isinstance(value, dict):
            return {str(k): normalize_space(v) for k, v in value.items()}

    fallback: dict[str, dict[str, str]] = {
        "collage_pop": {
            "family": "拼贴爆款",
            "background": "手撕纸、贴纸、网点颗粒、撞色色块、菜品抠图和热闹促销装饰形成高密度招贴长卷。",
            "composition": "前两张用大字和不规则拼贴建立冲击，后续切片用菜品、食材和贴纸节奏延展。",
        },
        "natural_earth": {
            "family": "地域自然",
            "background": "产地山水、村落远景、植物叶影、木纹亚麻和晨光雾气形成自然可信的长卷空间。",
            "composition": "前两张用地域/品牌识别和自然大字，后续切片用食材、菜品和环境层次延展。",
        },
        "street_warm": {
            "family": "门店烟火",
            "background": "真实门店、街巷暖光、砖墙/木质/灯串、蒸汽和用餐人气作为平面化远景层。",
            "composition": "前两张强调老店、人气和主菜热气，后续切片用门店场景与菜品交替延展。",
        },
        "bold_split": {
            "family": "热辣爆款",
            "background": "番茄红、暖橙、奶油白和少量鲜绿形成明亮高对比，配合火焰蒸汽、撕裂分割、辣椒花椒和粗颗粒海报质感组成强冲击长卷。",
            "composition": "前两张用大字和主菜强对比压住首屏，后续切片用斜线、锅气和食材动势延展。",
        },
        "retro_poster": {
            "family": "复古招贴",
            "background": "旧纸、套印网点、怀旧门头、边框标牌、褪色油墨和地方老店纹理。",
            "composition": "前两张像连续老招贴，后续切片用图章、旧照片卡和菜品画框延展。",
        },
        "ink_oriental": {
            "family": "山水意境",
            "background": "宣纸纤维、水墨山水、地域村落、竹木纹理、淡雾和克制金线形成东方长卷。",
            "composition": "前两张用大字与山水留白建立识别，后续切片用菜品、印章和地域纹样延展。",
        },
        "festive_red": {
            "family": "节庆国潮",
            "background": "亮朱红纸材质、米白留白、灯笼暖光、祥云边框、金色纹样和团圆餐桌氛围，避免大面积深红压暗。",
            "composition": "前两张用节庆大字和主菜建立仪式感，后续切片用灯笼、蒸汽和菜品层次延展。",
        },
        "neon_night": {
            "family": "夜场霓虹",
            "background": "深色街区、霓虹灯牌、湿润反光、吧台光影和夜宵蒸汽；菜品主体必须有清楚高光，不被夜色吞掉。",
            "composition": "前两张用发光标题和主菜形成夜场记忆点，后续切片用霓虹深度和菜品高光延展。",
        },
        "dark_premium": {
            "family": "暗调高级",
            "background": "暗木、石材、金属边缘、单点聚光、微妙烟雾和高级餐厅远景；暗色只做背景深度，菜品主体必须明亮清晰、食物高光可见。",
            "composition": "前两张用克制大字和主菜聚光，后续切片用材质细节与菜品高光延展。",
        },
        "default": {
            "family": "丰富场景长卷",
            "background": "结合菜系、地域和门店语境，使用多层材质、远景空间、装饰元素、光影颗粒和菜品层次形成完整长卷。",
            "composition": "前两张完成识别和核心卖点，第三张开始延展菜品/工艺/场景，后续保持连续节奏。",
        },
    }
    return fallback.get(style_id, fallback["default"])


# ---------------------------------------------------------------------------
# Style reference guidance builder
# ---------------------------------------------------------------------------

TEXT_ACCURACY_NEGATIVE = (
    "no garbled text, no gibberish characters, no typos, no homophone substitutions, "
    "no visually-similar-character substitutions, no mixed simplified/traditional unless intended, "
    "no invented words, no random strokes; if any text cannot be rendered with 100% character accuracy then omit that text entirely"
)

STYLE_REFERENCE_NOTE_MIN_CHARS = 50
STYLE_REFERENCE_LOW_RES_SHORT_SIDE = 400
STYLE_REFERENCE_LOW_RES_NOTE_MIN_CHARS = 200
STYLE_REFERENCE_OBSERVATION_DIMENSIONS = (
    "色调",
    "构图比例",
    "文字风格",
    "菜品/主体呈现方式",
    "背景元素",
    "分隔/留白/节奏方式",
)


def _style_reference_short_side(image_path: str) -> int | None:
    path = Path(normalize_space(image_path))
    if not path.exists():
        return None
    try:
        from PIL import Image  # type: ignore

        with Image.open(path) as image:
            width, height = image.size
        return min(width, height)
    except Exception:
        return None


def _style_reference_constraint_text(note: str, focus: str) -> str:
    note = normalize_space(note)
    note_part = f" style_reference_note={note}." if note else " style_reference_note is empty; infer the missing details directly from style reference images."
    return (
        f"STYLE REFERENCE DOMINANT MUST: {focus} MUST be derived from style reference images and style_reference_note."
        f"{note_part} "
        "MUST imitate all non-factual visual attributes from the references as closely as possible: palette, composition, layout, typography, atmosphere, texture, decoration, and whitespace. "
        "MUST override template style, cuisine/city default style, tone, and appetite color policy when they conflict. "
        "MUST_NOT copy or infer factual business content from the reference image, including dish names, prices, store names, campaign rules, dates, concrete visible copy, dish facts, protected IP, or logo as new content. "
        "MUST_NOT override food-reference fidelity or logo-reference fidelity constraints."
    )


def _build_style_reference_guidance(req: dict[str, Any]) -> dict[str, Any] | None:
    """Build style_reference_guidance section for prompt if user provided style reference images."""
    style_refs = req.get("assets", {}).get("style_reference_images", [])
    if not style_refs:
        return None
    note = normalize_space(req.get("assets", {}).get("style_reference_note", ""))
    note_length = len(note)
    reference_position_note = (
        "在五连图生成中，它们紧跟在系统版式 mask 之后；版式 mask 只控制几何比例，不影响视觉风格。"
        if req.get("type") == "五连图"
        else "它们是 generation_reference_images 中最前面的图片。"
    )
    guidance: dict[str, Any] = {
        "role": "HIGHEST_PRIORITY_VISUAL_STYLE_GUIDE",
        "count": len(style_refs),
        "instruction": (
            f"用户提供了 {len(style_refs)} 张风格参考图（style reference images），{reference_position_note}"
            "这些参考图是最高优先级的视觉设计权威——除菜名、价格、店名、活动规则、日期等事实性业务内容外，必须尽量深度模仿参考图的所有视觉特征："
            "整体色调氛围、构图比例（文字vs菜品的面积分配）、字体风格（笔触粗细/力度/材质感）、"
            "文字排版位置与大小、菜品排列方式与大小层次、背景纹理与装饰元素风格。"
            "当本 prompt 中任何其他规则与参考图的视觉风格冲突时，以参考图为绝对权威。"
            "参考图仅用于视觉风格指导，不要复制或推断参考图中的具体文案、菜名、价格、店名、活动规则或菜品事实。"
        ),
        "conflict_priority": [
            "style_reference_images",
            "style_reference_note",
            "模板风格 / template style/color/composition/texture",
            "cuisine/city default style",
            "tone/appetite color policy",
        ],
        "user_visual_constraints": {
            "MUST": [
                "MUST imitate every non-factual visual attribute in the reference images as closely as possible: palette, composition, layout, typography hierarchy, atmosphere, texture, decoration language, and whitespace ratio.",
                "MUST treat style reference images and style_reference_note as the highest-priority visual style authority.",
                "MUST prefer style reference images whenever template, cuisine, city, tone, or appetite-color rules conflict.",
            ],
            "MUST_NOT": [
                "MUST_NOT copy or infer factual business content from the style reference image, including dish names, prices, store names, campaign rules, dates, concrete visible copy, menu facts, or protected IP.",
                "MUST_NOT use style reference images as dish, mascot, or logo references.",
                "MUST_NOT let the style reference weaken real food fidelity or logo fidelity requirements.",
            ],
        },
    }
    if note:
        # Only include user_note once (avoid repeating in note_instruction to save prompt space)
        guidance["user_visual_requirements"] = note
    if note_length < STYLE_REFERENCE_NOTE_MIN_CHARS:
        dimensions = "、".join(STYLE_REFERENCE_OBSERVATION_DIMENSIONS)
        guidance["structured_observation_fallback"] = (
            "style_reference_note 过短或过于笼统；先从参考图识别设计要素再生成。"
            f"必须逐项观察并复现：{dimensions}。"
            "如果某类特征不明显，按“无明显特征”处理，不要编造参考图中不存在的元素。"
        )
    low_resolution_refs: list[dict[str, Any]] = []
    for ref in style_refs:
        short_side = _style_reference_short_side(str(ref))
        if short_side is not None and short_side < STYLE_REFERENCE_LOW_RES_SHORT_SIDE:
            low_resolution_refs.append({"path": str(ref), "short_side": short_side})
    if low_resolution_refs and note_length < STYLE_REFERENCE_LOW_RES_NOTE_MIN_CHARS:
        guidance["low_resolution_reference_warning"] = (
            "检测到低分辨率风格参考图（短边 <400px），图片结构信息有限；"
            "生成前必须更依赖 style_reference_note 补足视觉结构。"
        )
        guidance["low_resolution_compensation_instruction"] = (
            "请把参考图拆解为至少 200 个中文字符级别的具体视觉约束后再生成，"
            "重点补足色调、构图比例、文字风格、菜品/主体呈现方式、背景元素、分隔/留白/节奏方式；"
            "不明显项写“无明显特征”，不要强行想象不存在的内容。"
        )
        guidance["low_resolution_references"] = low_resolution_refs
    return guidance


def _build_panorama_prompt(
    req: dict[str, Any],
    template: dict[str, Any],
    layout: dict[str, Any],
    width: int,
    height: int,
    quality_feedback: list[str] | None = None,
    compensated_band_h: int | None = None,
    compensated_top_y: int | None = None,
    compensated_bottom_y: int | None = None,
) -> str:
    style_id = style_id_of(template)
    panorama_family = _panorama_style_family(style_id)
    realism = req.get("style", {}).get("realism", "balanced")
    tone = req.get("style", {}).get("tone", "")
    ref_images = req.get("assets", {}).get("reference_images", [])
    dish_refs = dish_reference_items(req)
    dish_labels = dish_label_items(req)
    explicit_dish_mapping = bool(dish_refs)
    dish_ref_indexes = {item["reference_image_index"] for item in dish_refs}
    ambient_refs = [
        {
            "reference_image_index": index,
            "reference_label": f"ambient/environment reference photo #{index}",
            "reference_image": image,
        }
        for index, image in enumerate(ref_images, start=1)
        if explicit_dish_mapping and index not in dish_ref_indexes
    ]
    dish_showcase_names = [
        normalize_space(item.get("name")) or f"dish from reference photo #{item['reference_image_index']}"
        for item in dish_refs
    ]
    dish_showcase_count = len(dish_refs) if explicit_dish_mapping else len(ref_images)
    food_reference_fidelity = _food_reference_fidelity_section(dish_showcase_count)
    if food_reference_fidelity:
        safe_style = _food_reference_safe_style_guidance(template)
        style_scene_guidance = safe_style["scene_prompt"]
        style_design_guidance = safe_style["design"]
        style_texture_guidance = safe_style["texture_guidance"]
        panorama_family = _food_reference_safe_panorama_family(panorama_family)
    else:
        style_scene_guidance = trim_prompt_text(template.get("scene_prompt") or template.get("scene_description") or "", 1200)
        style_design_guidance = trim_prompt_text(
            " ".join([
                normalize_space(template.get("texture_guidance")),
                f"color_strategy={normalize_space(template.get('color_strategy'))}",
                f"composition_strategy={normalize_space(template.get('composition_strategy'))}",
            ]),
            600,
        )
        style_texture_guidance = template.get("texture_guidance", "")

    # --- Style Reference Dominant Mode ---
    # When user provides style_reference_images, suppress all template visual descriptions
    # and let the reference images + optional note drive the visual style.
    _style_ref_dominant = bool(req.get("assets", {}).get("style_reference_images"))
    if _style_ref_dominant:
        _sr_note = req.get("assets", {}).get("style_reference_note", "")
        _sr_bg = _style_reference_constraint_text(_sr_note, "background color, texture, decoration, and atmosphere")
        _sr_comp = _style_reference_constraint_text(_sr_note, "composition rhythm, text/food area ratio, and whitespace")
        panorama_family = {
            "family": "用户风格参考图主导",
            "background": _sr_bg,
            "composition": _sr_comp,
        }
        style_scene_guidance = (
            "STYLE REFERENCE DOMINANT MODE: 所有非事实性视觉风格由用户提供的风格参考图定义。"
            "除菜名、价格、店名、活动规则等必须使用用户确认内容外，忽略模板预设的色彩策略、材质、场景描述和装饰风格。"
            "从风格参考图中提取并尽量复现：整体色调氛围、构图节奏与留白比例、"
            "字体样式与文字排版位置、海报设计风格、文字与图片的面积比例关系。"
        )
        style_design_guidance = _style_reference_constraint_text(_sr_note, "overall design guidance")
        style_texture_guidance = ""

    typography_profile = _typography_profile(req, template)
    logo_ref = req.get("assets", {}).get("brand_logo_path") or req.get("assets", {}).get("store_logo_path")
    title = normalize_space(req.get("title"))
    store_name = normalize_space(req.get("store", {}).get("name", ""))
    visual_density = _infer_visual_density(req, style_id)
    cuisine_family = _infer_cuisine_family(req)
    text_placement = _resolve_text_placement(req, style_id, cuisine_family, visual_density)
    if _style_ref_dominant:
        text_placement = _resolve_style_reference_text_placement(req, text_placement)
    title_safe_box = TEXT_PLACEMENT_BOXES[text_placement]
    hero_visual_anchor = _hero_visual_anchor(req, dish_refs, ref_images, cuisine_family, text_placement, visual_density)
    conditional_negatives: list[str] = []
    if food_reference_fidelity:
        conditional_negatives.extend(FOOD_REFERENCE_NEGATIVE_TERMS)
    if not ref_images:
        # 0 张实拍：禁止虚构任何具体菜品
        conditional_negatives.extend([
            "no plated dish",
            "no bowl of food",
            "no pot of food",
            "no recognizable cooked dish",
            "no invented food subject",
            "no fake dish",
            "no imaginary meal",
        ])
    if not _style_ref_dominant:
        # Only enforce appetite color negatives when NOT in style ref dominant mode
        conditional_negatives.extend(APPETITE_COLOR_NEGATIVE_TERMS)
    if has_protected_ip_risk_context(req):
        conditional_negatives.extend(PROTECTED_IP_NEGATIVE)
    if dish_labels:
        conditional_negatives.extend([
            "no oversized dish label text",
            "no dish label crossing slice boundary",
            "no dish label covering food subject",
        ])
    else:
        conditional_negatives.append("no dish-name labels or captions next to food items")
    negative = (
        PANORAMA_HARD_NEGATIVE
        + PANORAMA_STYLE_NEGATIVE_BY_DENSITY[visual_density]
        + conditional_negatives
    )

    if explicit_dish_mapping:
        dish_showcase_count_rule = (
            f"show exactly {len(dish_refs)} dishes: {', '.join(dish_showcase_names)}; do NOT omit any dish. "
            "Only dish_showcase_references are dish subjects; ambient/environment references are background and atmosphere only."
        )
        dish_showcase_composition_priority = (
            f"菜品展示区必须 {dish_showcase_count_rule}"
            "按 hero_visual_anchor、dish_scale_rule 和 crop_and_layering_permissions 建立主次尺度、错落层次和视觉锚点。"
        )
        dish_reference_summary = "；".join(
            f"reference photo #{item['reference_image_index']} = {normalize_space(item.get('name')) or f'dish from reference photo #{item['reference_image_index']}'}"
            for item in dish_refs
        )
        ambient_reference_summary = (
            "；".join(item["reference_label"] for item in ambient_refs)
            if ambient_refs
            else "none"
        )
        photo_reference_instruction = (
            f"用户提供了 {len(ref_images)} 张实拍参考图，其中 {len(dish_refs)} 张被显式标记为菜品参考图。"
            f"菜品展陈必须 {dish_showcase_count_rule}"
            f"菜品参考图映射：{dish_reference_summary}。"
            f"环境/空间参考图：{ambient_reference_summary}。"
            "菜品参考图进入 dish showcase：参考其食材色泽、器皿特征、菜品结构和自然光线，按 hero_visual_anchor、dish_scale_rule 和 crop_and_layering_permissions 横向错落分布。"
            "ambient/environment references 只用于背景、空间层、门店真实感、街巷/室内氛围、光影和材质，不得被画成额外盘装菜品，也不得增加到 exactly dishes 计数。"
            "菜品形体安全：hero dish must remain recognizable and appetizing; no severe crop, no cut-in-half hero, no bottom-margin intrusion. "
            "主承载物不得从底部白边方向被切断；辅菜和氛围元素的裁切只按 crop_and_layering_permissions 执行。"
            "参考图保真优先：菜品主体必须保持真实美食摄影感，保留参考图的主要器皿、菜品结构、真实材质和自然光线；"
            "允许适度商业美化，包括轻度补光、提亮、清理杂乱背景、增强食欲色彩和融入长图构图。"
            "菜品亮度和清晰度下限：菜品主体必须曝光充足、细节清晰、自然食物高光可见，不能暗沉、低对比或被灰墨感覆盖。"
            "真实清晰细节：菜品主体必须是 realistic food photography with sharp ingredient details；葱花、辣椒、肉片、汤汁、盘沿纹样等细节清晰，不得涂抹、蜡感、塑料感或 AI 渲染感。"
            "模板里的 3D、微缩、黏土、塑料、Pixar、diorama 风格词只能用于背景装饰或版式氛围，不能作用到菜品主体。"
        )
    elif not ref_images:
        # 0 张实拍图：五连图禁止虚构菜品，只用氛围元素
        dish_showcase_count_rule = ""
        dish_showcase_composition_priority = (
            "无菜品实拍图：禁止生成任何具体的盘装菜品、碗装菜品或食物主体。"
            "使用抽象的用餐氛围元素填充画面：蒸汽、餐具、灯光、色彩、食材纹理剪影、季节元素、门店空间感或品类象征图形。"
        )
        photo_reference_instruction = (
            "用户未提供菜品实拍图。"
            "⚠️ 严禁虚构菜品：不得生成任何具体的盘装菜品、碗装食物、锅装菜肴或可识别的具体食物主体。"
            "画面应使用抽象的餐饮氛围元素代替菜品：蒸汽升腾、精致餐具、暖色灯光、食材局部纹理（如辣椒、花椒、葱段等散落装饰）、"
            "色彩渐变、季节元素、门店空间轮廓或品类象征图形（如火锅剪影、锅铲、砂锅轮廓等），"
            "形成有食欲暗示但不具体呈现菜品的视觉氛围。"
        )
    else:
        dish_showcase_count_rule = "至少展示 2-4 个菜品/器皿，形成主菜锚点与辅菜节奏。"
        dish_showcase_composition_priority = (
            "菜品展示区至少 2-4 个菜品/器皿清晰可识别；"
            "按 hero_visual_anchor、dish_scale_rule 和 crop_and_layering_permissions 建立主次尺度、错落层次和视觉锚点。"
        )
        photo_reference_instruction = (
            f"用户提供了 {len(ref_images)} 张实拍参考图。"
            "参考实拍图的食材色泽和器皿特征。第一张实拍图最重要，作为 hero_visual_anchor 的来源，尺寸相对最大但必须清晰可识别。"
            "其余菜品按 dish_scale_rule 形成错落节奏；辅菜和氛围元素的裁切只按 crop_and_layering_permissions 执行。"
            "菜品形体安全：hero dish must remain recognizable and appetizing; no severe crop, no cut-in-half hero, no bottom-margin intrusion. "
            "主承载物不得从底部白边方向被切断。"
            "参考图保真优先：菜品主体必须保持真实美食摄影感，保留参考图的主要器皿、菜品结构、真实材质和自然光线；"
            "允许适度商业美化，包括轻度补光、提亮、清理杂乱背景、增强食欲色彩和融入长图构图。"
            "菜品亮度和清晰度下限：菜品主体必须曝光充足、细节清晰、自然食物高光可见，不能暗沉、低对比或被灰墨感覆盖。"
            "真实清晰细节：菜品主体必须是 realistic food photography with sharp ingredient details；葱花、辣椒、肉片、汤汁、盘沿纹样等细节清晰，不得涂抹、蜡感、塑料感或 AI 渲染感。"
            "模板里的 3D、微缩、黏土、塑料、Pixar、diorama 风格词只能用于背景装饰或版式氛围，不能作用到菜品主体。"
        )

    if dish_labels:
        dish_label_instruction: Any = {
            "mode": "model_integrated_auxiliary_labels",
            "labels": dish_labels,
            "placement_rule": (
                "菜名标签必须放在对应菜品附近 3%-6% 视觉距离内，作为小号辅助标签自然融入画面；"
                "避开 20%/40%/60%/80% 切片分割线左右各 2.5%、画布边缘、Logo 区和菜品主体轮廓。"
                "字体、颜色、描边、投影和装饰应跟随整体 typography_profile，层级低于主标题，不得像独立 UI 贴片。"
            ),
        }
    else:
        dish_label_instruction = "未提供有效菜品名映射；不要在菜品旁添加菜名标签或说明文字。"

    if ref_images:
        dish_camera_and_tabletop_policy = {
            "camera_framing": (
                "菜品主体采用 medium-shot food product display，中景产品展示，不是餐厅桌面横拍。"
                "使用轻俯视 15°–30° 或正面微俯视视角，让每个主菜在 20:3 内容带内菜品垂直居中。"
            ),
            "dish_integrity": (
                "主菜完整器皿可见：complete plate/bowl/pot rim visible，盘沿、碗沿、锅沿和主体外轮廓不得被底部切断；"
                "hero dish 和主要菜品不能被推到内容带下边缘。"
            ),
            "tabletop_rule": (
                "允许小面积浅阴影、小托盘、小底座或局部台面作为视觉锚点，但不得出现贯穿画面的水平桌面线，"
                "不得让大面积 tabletop 占据内容带下半部，不得让画面变成一排菜放在同一张桌子上的横拍照片。"
            ),
        }
    else:
        dish_camera_and_tabletop_policy = {
            "no_dish_policy": (
                "用户未提供菜品实拍图，严禁生成任何具体菜品。"
                "画面使用氛围元素（蒸汽、餐具、灯光、食材剪影、品类象征图形）代替菜品，营造餐饮氛围。"
            ),
        }

    prompt: dict[str, Any] = {
        "task": (
            "生成一张 3840×1280 像素的五连图载体。第一张参考图是系统提供的版式 mask，只用于控制上下分区几何，不是视觉风格。"
            "请把完整连续的餐饮五连图内容画成一条贴在画布最上方的超扁横向长条标签/窄幅腰封。"
            "这条长条必须非常宽、非常扁，像横向贴纸贴在白纸顶部；不要画成普通高横幅、整页海报或满幅背景。"
            "下方大面积区域必须是一整块纯白 #FFFFFF 空白白纸。"
            f"采用 density-aware 连续长卷构图（visual_density={visual_density}）：clean/rich 偏中景展示和清晰食欲主体，explosive 可结合近景冲击与中景展示。"
            "标题文字必须清晰，hero dish must remain recognizable and appetizing; no severe crop, no cut-in-half hero, no bottom-margin intrusion。"
            "顶部长条内部设计必须 edge-to-edge full-bleed 铺满，严禁在长条内部再留白边，也严禁把任何内容画到下方纯白区。"
            "这是线上门店装修位的营销平面设计。允许把远景山水、街巷、门店环境、产地、室内烟火、纸张拼贴、墙面纹理等处理成平面化背景层，"
            "但它们只能出现在顶部长条标签内部，不能让画面变成普通室内照片，也不能破坏顶部长条和底部纯白区。"
        ),
        "canvas_px": f"{width}x{height}",
        "layout_mask_reference": {
            "reference_image_position": "generation_reference_images[0]",
            "role": "INTERNAL_GEOMETRY_MASK_ONLY",
            "mask_rule": (
                "第一张参考图只表示版式比例：浅灰顶部短条 = 唯一可绘制的超扁横向长条标签；白色下半区 = 纯白禁画区。"
                "不要复制浅灰颜色、灰线或任何 mask 视觉元素。"
            ),
            "calibrated_gray_strip_height_px": PANORAMA_LAYOUT_MASK_CONTENT_HEIGHT,
            "target_detected_content_height_px": PANORAMA_TARGET_BAND_HEIGHT,
            "instruction": (
                "严格跟随 mask 的上下分区观感：只有顶部一条超扁横向贴纸可绘制，下面是一整块纯白空白纸。"
                "不要自行把内容区扩大成普通横幅、卡片、满幅海报或整页背景。"
            ),
        },
        **(
            {
                "boundary_guide_reference": {
                    "reference_image_position": "generation_reference_images[1]",
                    "role": "PIXEL_BOUNDARY_GUIDE",
                    "description": (
                        f"第二张参考图是红色边界引导图。两条水平红线标记内容带的绝对上下边界："
                        f"上边界 y={compensated_top_y}px，下边界 y={compensated_bottom_y}px，"
                        f"内容带高度仅 {compensated_band_h}px。"
                    ),
                    "rule": (
                        "所有可见内容（菜品、文字、装饰、背景色块）必须严格限制在两条红线之间。"
                        "红线上方和红线下方必须是纯白 #FFFFFF，不允许任何像素溢出。"
                        "不要在生成结果中复现红线本身。"
                    ),
                    "compensated_band_h_px": compensated_band_h,
                    "compensated_top_y_px": compensated_top_y,
                    "compensated_bottom_y_px": compensated_bottom_y,
                },
            }
            if compensated_band_h is not None
            else {}
        ),
        "carrier_layout_rule": (
            "【载体上下分区 — 最高优先级，高于任何风格参考图】"
            "画布分上下两区：顶部是一条贴在画布最上方的超扁横向长条标签/窄幅腰封；底部是占据画布大部分高度的纯白 #FFFFFF 空白白纸。"
            "所有标题、菜品、装饰、背景都必须完全限制在顶部长条标签内。"
            "底部纯白区绝对不允许任何菜品、文字、标题、装饰、阴影、蒸汽、背景纹理或渐变。"
            "长条标签要非常宽、非常扁，接近最终 20:3 长卷比例；宁可偏矮，也不要画成高横幅。"
            "如果提供了风格参考图，只能把参考图的色调、字体、排版、装饰语言迁移到顶部长条内部；"
            "不得模仿参考图的满幅海报结构，不得让参考图覆盖或改变上下分区。"
            + (
                f" 【B2 像素级约束】内容带精确占据 y={compensated_top_y}~{compensated_bottom_y}，"
                f"高度仅 {compensated_band_h}px（画布高度的 {compensated_band_h / height * 100:.1f}%）。"
                "红色边界参考图中的两条红线标记了绝对边界，所有可见内容必须在两条红线之间，"
                "红线外侧必须是纯白 #FFFFFF。请严格遵循红线位置，不要超出。"
                if compensated_band_h is not None
                else ""
            )
        ),
        "material_type": "五连图",
        "layout": {"id": layout.get("id"), "name": layout.get("name")},
        "homepage_first_screen_rule": (
            "真实门店首页首屏约 2.5 张卡片可见，因此前两张必须已经完成品牌/地域/菜系识别和核心卖点表达，"
            "第三张负责菜品、工艺或场景延展并引导继续浏览。不要把关键信息全部放到第四、第五张。"
        ),
        "slice_storyboard": _panorama_slice_storyboard(visual_density),
        "slice_seam_safety_policy": _slice_seam_safety_policy(),
        "visual_density": visual_density,
        "cuisine_family": cuisine_family,
        "text_placement": text_placement,
        "panorama_style_family": {
            "style_id": style_id,
            "family": panorama_family.get("family", "丰富场景长卷"),
            "background": panorama_family.get("background", ""),
            "composition": panorama_family.get("composition", ""),
            "density_direction": PANORAMA_DENSITY_MODIFIERS[visual_density],
        },
        "panorama_background_design": _panorama_background_design(visual_density, panorama_family),
        "crop_and_layering_permissions": PANORAMA_DENSITY_PERMISSIONS[visual_density],
        "visual_anchoring_policy": _visual_anchoring_policy(visual_density),
        "dish_camera_and_tabletop_policy": dish_camera_and_tabletop_policy,
        "hero_visual_anchor": hero_visual_anchor,
        "dish_scale_rule": PANORAMA_DISH_SCALE_RULE_BY_DENSITY[visual_density],
        "dish_showcase_integrity": _dish_showcase_integrity(visual_density, dish_showcase_composition_priority),
        "dish_showcase_references": dish_refs,
        "ambient_reference_images": ambient_refs,
        "dish_label_instruction": dish_label_instruction,
        "composition_structure": TEXT_PLACEMENT_COMPOSITIONS[text_placement],
        "photo_reference_instruction": photo_reference_instruction,
        "title_instruction": {
            "mode": "model_integrated_text",
            "text": title,
            "store_name": store_name,
            "safe_text_box_ratio": title_safe_box,
            "instruction": (
                f"主题文案必须完整绘制为：「{title}」。"
                "标题必须与菜品和背景一起绘制，成为整体视觉的一部分。"
                f"resolved text_placement mode is {text_placement}; title_instruction.safe_text_box_ratio is derived from that mode and is authoritative. "
                "标题不得贴近或越过画布边缘；不得被菜品、装饰、边框或后续裁切遮挡。"
                "如果需要两行，按自然语义分成上下两行；单个汉字笔画不得压到 20%/40%/60%/80% 切片缝。"
            ),
        },
        "decoration_guidance": (
            "装饰元素跟随风格参考图的视觉语言，不使用模板预设装饰风格。"
            if _style_ref_dominant
            else _panorama_decoration_guidance(req, template)
        ),
        "appetite_color_policy": (
            "色调由风格参考图主导，不强制使用食欲暖色或模板预设色彩策略。"
            if _style_ref_dominant
            else _appetite_color_policy(style_id)
        ),
        "logo_instruction": (
            (
                "用户提供了门店/品牌 Logo 参考图。必须忠实还原 Logo 的完整图案：保持原始形状（圆形/方形/异形）、内部所有文字笔画、图标元素和配色不变形不丢失。"
                "Logo 作为品牌徽章自然嵌入并自然融入画面左侧标题区域，大小适中（不超过内容带高度的 40%），清晰可辨认。"
                "⚠️ Logo 保真要求：Logo 中的每一个文字必须笔画完整正确，图形元素（如辣椒、麦穗、边框线条）必须保留原始比例和细节，不得重新设计或简化。"
                + (
                    "\n\n【Logo 精确结构描述 — 必须严格按照以下描述还原，不得偏离】\n"
                    + req.get("brand_profile", {}).get("logo_visual_description", "")
                    if req.get("brand_profile", {}).get("logo_visual_description")
                    else ""
                )
            )
            if logo_ref
            else "用户未提供门店/品牌 Logo；不要自行创造门店 Logo。"
        ),
        "store_context": {
            "_DO_NOT_RENDER": "store_context fields are for scene understanding only. NEVER render category/cuisine_tag/dimensions as visible text.",
            "name": req.get("store", {}).get("name", ""),
            "category": req.get("store", {}).get("category", ""),
            "cuisine_tag": req.get("style", {}).get("cuisine_tag", ""),
            "campaign": {k: v for k, v in (req.get("campaign") or {}).items() if k not in ("theme",) or v != req.get("title")},
            "copy": {"selected_text": req.get("copy", {}).get("selected_text", "")},
        },
        "style": (
            {
                "_DO_NOT_RENDER": "All fields in 'style' are internal design parameters. NEVER render name/label/cuisine_tag/city_tag/tone as visible text.",
                "name": req.get("style", {}).get("name", "美团餐饮团购"),
                "realism": realism,
                "base_style": _style_reference_base_style(),
                "tone": tone,
                "tone_modifier": _section_tone_modifier(tone),
                "style_id": "style_reference_dominant",
                "label": "风格参考图主导模式",
                "scene_guidance": style_scene_guidance,
                "design_guidance": style_design_guidance,
                "color_strategy": "由风格参考图决定",
                "composition_strategy": "由风格参考图决定",
                "texture_guidance": style_texture_guidance,
                "typography_profile": typography_profile,
                "panorama_text_rule": "resolved text_placement mode is authoritative; title_instruction.safe_text_box_ratio is derived from that mode. Style-level text position descriptions are only advisory; only render the specified title/copy.",
            }
            if _style_ref_dominant
            else {
                "_DO_NOT_RENDER": "All fields in 'style' are internal design parameters. NEVER render name/label/cuisine_tag/city_tag/tone as visible text.",
                "name": req.get("style", {}).get("name", "美团餐饮团购"),
                "realism": realism,
                "base_style": _section_base_style(realism),
                "tone": tone,
                "tone_modifier": _section_tone_modifier(tone),
                "style_id": style_id,
                "label": template.get("label", ""),
                "scene_guidance": style_scene_guidance,
                "design_guidance": style_design_guidance,
                "color_strategy": template.get("color_strategy", ""),
                "composition_strategy": template.get("composition_strategy", ""),
                "texture_guidance": style_texture_guidance,
                "typography_profile": typography_profile,
                "panorama_text_rule": "resolved text_placement mode is authoritative; title_instruction.safe_text_box_ratio is derived from that mode. Style-level text position descriptions are only advisory; only render the specified title/copy.",
            }
        ),
        "food_reference_fidelity": food_reference_fidelity,
        "food_style_isolation": (
            "⚠️ FOOD STYLE ISOLATION (MANDATORY): 菜品区域与背景/装饰区域必须分层渲染。"
            "背景层可以自由使用设计风格（复古颗粒、牛皮纸纹理、做旧笔触、国潮水墨、霓虹光效等），"
            "但菜品主体（食材、汤汁、酱料、器皿内部）必须保持摄影级写实渲染，不继承背景的材质滤镜。"
            "具体要求：菜品区域禁止叠加颗粒噪点、纸张纹理、做旧划痕、水彩笔触、油画肌理等背景材质效果；"
            "菜品色温只允许受环境光自然影响（如暖光场景下的轻微暖色偏移），不允许被背景滤镜整体染色；"
            "菜品高光、油光、汤汁反射必须保持自然通透，不能被背景的哑光/磨砂质感压制。"
            "盘沿、碗沿等器皿边缘是菜品层与背景层的自然过渡区，允许轻微的环境光色温融合和柔和阴影，"
            "但器皿内部的食物主体必须严格保持写实摄影质感。"
            "简言之：背景可以很有设计感，菜品必须像相机拍的。"
        ),
        "text_accuracy_constraint": (
            "⚠️ TEXT ACCURACY (MANDATORY): 物料中所有文字（主标题、副标题、菜名标签、CTA、背景装饰小字等）"
            "必须逐字准确，严禁乱码、错别字、同音字替换、形近字替换、繁简混用或凭空臆造文字。"
            "如无法保证某处文字逐字正确，宁可该处不出现文字。"
        ),
        "negative": ", ".join(negative) + ", " + TEXT_ACCURACY_NEGATIVE,
    }
    if quality_feedback:
        prompt["quality_feedback"] = {
            "scope": "only_fix_issues_observed_in_previous_candidate",
            "instruction": (
                "上一版候选未通过检查。本轮只修正以下关键问题，不要追加无关解释，"
                "不要改变已正确的菜名、菜品数量、整体风格和顶部内容带/底部纯白结构。"
            ),
            "fixes": [trim_prompt_text(item, 90) for item in quality_feedback[:3] if normalize_space(item)],
        }
    # Inject style reference guidance at HIGHEST priority position (top of dict)
    style_ref_guidance = _build_style_reference_guidance(req)
    if style_ref_guidance:
        # Rebuild prompt dict with style_reference_guidance at the very top
        prompt = {"style_reference_guidance": style_ref_guidance, **prompt}

    # --- Style Reference Dominant: override composition/layout constraints ---
    # When a style reference image is provided, the template's rigid layout rules
    # (storyboard, text placement, hero anchor, dish camera policy, etc.) should
    # NOT override the reference image's visual language. Replace them with soft
    # guidance that defers to the reference image.
    if _style_ref_dominant:
        _sr_note = req.get("assets", {}).get("style_reference_note", "")
        _dominant_composition_note = _style_reference_constraint_text(
            _sr_note,
            "five-slice storyboard, text placement, food placement, and visual rhythm",
        )
        prompt["slice_storyboard"] = {
            "guidance": "STYLE REFERENCE DOMINANT: 五片叙事结构由风格参考图主导。"
                        "参考图中的布局节奏（文字区域、菜品区域、留白比例）是权威指引。"
                        "不需要严格遵循 identity_hook/hero_spread 等固定套路。",
            "reference_note": _dominant_composition_note,
        }
        prompt["composition_structure"] = (
            "STYLE REFERENCE DOMINANT: 构图由风格参考图决定。"
            "标题文字位置、大小、菜品排列方式都跟随参考图的视觉语言。"
            f"用户说明：{_dominant_composition_note}"
        )
        prompt["dish_camera_and_tabletop_policy"] = {
            "guidance": (
                "STYLE REFERENCE DOMINANT: 菜品展示方式由风格参考图主导。"
                "如果参考图中菜品是悬浮叠层展示、带容器浮在背景上方，就采用同样方式。"
                "如果参考图是俯视餐桌，就用俯视餐桌。不要强制中景产品展示视角。"
            ),
        }
        prompt["hero_visual_anchor"] = {
            "guidance": (
                "STYLE REFERENCE DOMINANT: 主菜位置和大小由风格参考图的布局节奏决定，"
                "不固定到特定 slice 或特定比例。参考图中最大/最突出的菜品位置就是 hero 位置。"
            ),
        }
        prompt["visual_anchoring_policy"] = {
            "guidance": (
                "STYLE REFERENCE DOMINANT: 食物锚点方式由风格参考图决定。"
                "如果参考图中菜品悬浮在场景背景上方，就用同样方式；"
                "不强制要求阴影锚点、台面锚点或特定的固定方式。"
            ),
        }
        # Override title_instruction to remove rigid positioning
        prompt["title_instruction"] = {
            "mode": "model_integrated_text",
            "text": title,
            "store_name": store_name,
            "instruction": (
                f"主题文案必须完整绘制为：「{title}」。"
                "STYLE REFERENCE DOMINANT: 标题的位置、大小、字体风格由风格参考图主导。"
                "参考图中标题很大、很冲击、居中占据画面核心位置时，就采用同样的大标题居中方式。"
                "标题必须与菜品和背景一起绘制为整体设计。"
                "单个汉字笔画不得压到 20%/40%/60%/80% 切片缝。"
            ),
        }
        # Remove or soften the homepage_first_screen_rule
        prompt["homepage_first_screen_rule"] = (
            "STYLE REFERENCE DOMINANT: 首屏信息分配由风格参考图的布局节奏决定。"
        )
        # Override dish_scale_rule to be reference-driven
        prompt["dish_scale_rule"] = (
            "STYLE REFERENCE DOMINANT: 菜品大小和排列方式由风格参考图决定。"
            "如果参考图中菜品有大有小、有前有后层叠，就采用同样方式。"
            "不强制 non-uniform scale 或特定的主次比例。"
        )
        # Simplify dish_showcase_integrity — remove template composition rules
        _orig_integrity = prompt.get("dish_showcase_integrity", {})
        prompt["dish_showcase_integrity"] = {
            "priority": "hero dish must remain recognizable and appetizing.",
            "dish_count": _orig_integrity.get("dish_count", ""),
            "style_note": "STYLE REFERENCE DOMINANT: 菜品展示方式、大小层次和排列位置由风格参考图主导，不强制横向错落分布。",
        }
        # Override photo_reference_instruction — this is a critical override.
        # Without this, the instruction still forces "横向错落分布" and "中景产品展示" layout.
        _dish_refs = dish_reference_items(req)
        _dish_names = [normalize_space(item.get("name")) or f"dish #{item['reference_image_index']}" for item in _dish_refs]
        _dish_count = len(_dish_refs) if _dish_refs else len(req.get("assets", {}).get("reference_images", []))
        prompt["photo_reference_instruction"] = (
            f"用户提供了 {_dish_count} 张菜品实拍参考图"
            + (f"：{', '.join(_dish_names)}" if _dish_names else "")
            + "。"
            "STYLE REFERENCE DOMINANT: 菜品的排列方式、位置、大小层次完全由风格参考图决定。"
            "不要强制横向一字排开或中景产品展示视角。"
            "如果参考图中文字是主体、菜品穿插在文字周围/后方，就采用同样方式。"
            "如果参考图中菜品有大有小层叠交错，就复现同样的大小层次。"
            "菜品保真要求不变：保留参考图中菜品的主要器皿、食材色泽和真实材质感。"
        )
        # Simplify food_reference_fidelity to reduce prompt bloat
        if prompt.get("food_reference_fidelity"):
            prompt["food_reference_fidelity"] = {
                "priority": "参考图保真优先",
                "reference_count": _dish_count,
                "dish_subject_rule": (
                    "菜品主体保持真实美食摄影感，保留器皿、食材色泽和真实材质；"
                    "允许适度商业美化（补光、提亮、增强食欲色彩）。"
                    "禁止把菜品处理成油画、蜡质塑料或 AI 渲染感。"
                ),
                "style_note": "STYLE REFERENCE DOMINANT: 菜品拍摄角度和展示方式由参考图主导，不强制特定视角。",
            }
        # Simplify panorama_background_design — let reference image drive background style
        prompt["panorama_background_design"] = {
            "goal": "STYLE REFERENCE DOMINANT: 背景风格由参考图主导。",
            "style_specific_direction": _style_reference_constraint_text(
                _sr_note,
                "panorama background style, color, texture, and decorative layers",
            ),
            "continuity_rule": "五张切片共享同一套背景世界观和光影方向。",
        }

    return json.dumps(prompt, ensure_ascii=False, indent=2)


def build_scene_prompt(req: dict[str, Any], template: dict[str, Any], layout: dict[str, Any], width: int, height: int) -> str:
    if req["type"] == "五连图":
        return _build_panorama_prompt(req, template, layout, width, height)

    has_food = bool(req["assets"].get("reference_images"))
    has_qr = _has_qr_asset(req["assets"])
    realism = req["style"].get("realism", "balanced")
    tone = req["style"].get("tone", "")
    allow_ai_text = bool(req["style"].get("allow_ai_text", True)) and req["style"].get("ai_text_role") != "pure_atmosphere"
    text_candidates = ai_display_text_candidates(req) if allow_ai_text else []
    dish_labels = dish_label_items(req)
    selected_logo, _, logo_label, logo_reason = select_logo_asset(req)
    include_platform_logo = selected_logo is not None
    mascot_mode = req["assets"].get("mascot_mode", "auto")
    protected_ip_guard = has_protected_ip_risk_context(req)
    style_id = style_id_of(template)
    if has_food:
        safe_style = _food_reference_safe_style_guidance(template)
        style_scene_guidance = safe_style["scene_prompt"]
        style_design_guidance = safe_style["design"]
        style_texture_guidance = safe_style["texture_guidance"]
    else:
        style_scene_guidance = trim_prompt_text(template.get("scene_prompt") or template.get("scene_description") or "", 1200)
        style_design_guidance = trim_prompt_text(
            " ".join([
                normalize_space(template.get("texture_guidance")),
                f"color_strategy={normalize_space(template.get('color_strategy'))}",
                f"composition_strategy={normalize_space(template.get('composition_strategy'))}",
            ]),
            600,
        )
        style_texture_guidance = template.get("texture_guidance", "")

    # --- Style Reference Dominant Mode (regular poster) ---
    _style_ref_dominant = bool(req.get("assets", {}).get("style_reference_images"))
    if _style_ref_dominant:
        _sr_note = req.get("assets", {}).get("style_reference_note", "")
        style_scene_guidance = (
            "STYLE REFERENCE DOMINANT MODE: 所有非事实性视觉风格由用户提供的风格参考图定义。"
            "除菜名、价格、店名、活动规则等必须使用用户确认内容外，忽略模板预设的色彩策略、材质、场景描述和装饰风格。"
            "从风格参考图中提取并尽量复现：整体色调氛围、构图节奏与留白比例、"
            "字体样式与文字排版位置、海报设计风格、文字与图片的面积比例关系。"
        )
        style_design_guidance = _style_reference_constraint_text(_sr_note, "regular poster design guidance")
        style_texture_guidance = ""

    typography_profile = _typography_profile(req, template)
    business_intents = req.get("scene", {}).get("business_intents") or [req.get("scene", {}).get("business_intent", "daily_attract")]
    primary_business_intent = business_intents[0] if business_intents else "daily_attract"

    if mascot_mode in {"official_reference", "generated_reference"}:
        mascot_rule = "必须使用本地吉祥物参考图，严格保持美团袋鼠的比例、五官、耳朵、体态和颜色；最多 1-2 个，不能改造成其它角色。"
    elif mascot_mode == "auto":
        mascot_rule = "根据场景决定是否使用本地美团袋鼠吉祥物：节庆、亲子、轻松团购可用；高级、极简或画面已饱满时不要用。若使用，必须严格参考本地素材，最多 1 个主形象加 1 个小辅助。"
    else:
        mascot_rule = "不要出现吉祥物。"

    allowed_text = [item["text"] for item in text_candidates]
    seen_allowed = {compact_text(text) for text in allowed_text}
    if allow_ai_text:
        for label in dish_labels:
            label_text = normalize_space(label.get("name"))
            compact_label = compact_text(label_text)
            if label_text and compact_label not in seen_allowed:
                allowed_text.append(label_text)
                seen_allowed.add(compact_label)
    primary_allowed_text = text_candidates[0]["text"] if text_candidates else ""
    if allowed_text:
        exact_title_rule = (
            f"EXACT TITLE CONTRACT: 主标题必须逐字复制「{primary_allowed_text}」，禁止同音字、形近字、错别字、繁简替换、谐音替换或自行改写；"
            "如果无法保证逐字准确，宁可减少文字装饰也不能替换任何一个汉字。"
            if primary_allowed_text
            else "本次没有主视觉展示文案；不要把辅助菜名标签放大成主标题。"
        )
        dish_label_rule = (
            "DISH LABEL CONTRACT: dish_labels 中的文字是辅助菜名标签（dish label），不属于 headline/copy/cta 主视觉文字区块；"
            "仅允许贴近对应 reference_image_index 的菜品附近小面积呈现，字体、颜色、描边和装饰必须跟随整体 typography_profile，"
            "不得遮挡菜品主体、Logo、二维码承载区或主标题。"
            if dish_labels
            else "未提供有效菜品名映射；不要自行添加菜品旁标签。"
        )
        text_rule = (
            "⚠️ STRICT TEXT WHITELIST: 画面上只允许出现 allowed_text 列表中的文字，且每条文字在整张海报中只能出现一次，严禁重复渲染同一条文案。"
            f"{exact_title_rule}"
            "⚠️ FORBIDDEN TEXT: 绝对禁止将 business_intent.label（如'日常引流'、'推新品'、'促销打折'）、style.name、cuisine_tag、city_tag、copy.dimensions 等内部元数据字段值渲染为画面可见文字。这些字段仅用于风格选择，不是展示文案。"
            "海报上的主视觉文字层次：1个大标题 + 最多1条副标题/优惠信息 + 最多1个行动号召按钮。总共不超过3个 headline/copy/cta 文字区块。"
            "辅助菜名标签是独立 label 层，不计入 headline/copy/cta 三类主视觉文字区块，但只能使用 dish_labels 指定的菜名。"
            "如果 allowed_text 中有语义重复的主视觉内容（如标题和优惠信息含义相近），只渲染其中一条作为主标题，不要两条都画上去。"
            "长标题必须完整渲染，不得截断、删尾或只保留前半句；"
            "LINE BREAK RULE: When text needs to wrap to multiple lines, STRICTLY follow the suggested_lines array in display_text_plan. "
            "Each element in suggested_lines is one visual line — do NOT re-split or merge them. "
            "NEVER break in the middle of a two-character Chinese word (e.g. '尝鲜', '优惠', '炸鸡' must stay on the same line). "
            "If the model must deviate from suggested_lines, break ONLY at: after a digit+unit pair (8折, 68元), before a digit, or at a space. "
            f"{dish_label_rule}"
        )
    else:
        text_rule = "不要出现任何可读文字。"
    # Build the business_intent label to explicitly ban it from rendering
    _intent_label = BUSINESS_INTENT_LABELS.get(primary_business_intent, "")
    negative = [
        "no QR modules",
        "no fake QR",
        "no watermark",
        "no invented price/date/rule/store claim",
        "no self-made or altered Meituan/Dianping platform logo",
        "no repeated text",
        "no decorative Chinese text on props",
        "no distorted mascot",
        "no internal metadata as visible text (business_intent labels, style names, cuisine/city tags are NEVER rendered)",
        f"no '{_intent_label}' text on poster" if _intent_label else "",
        "no '日常引流' or '日常到店引流' text", "no '推新品' or '推新品/上新' text",
        "no '促销打折' or '促销打折/引流' text", "no '品牌形象' or '品牌形象/宣传' text",
        "no '社交传播' or '朋友圈/种草传播' text", "no '节日活动' text",
        "no '会员复购' or '会员复购/私域' text", "no '招聘/招商' text",
    ]
    negative = [n for n in negative if n]  # remove empty strings
    if protected_ip_guard:
        negative.extend(PROTECTED_IP_NEGATIVE)
    if has_qr:
        negative.append("no blank empty area except the QR placeholder")
        negative.extend([
            "no tilted QR hosting area",
            "no rotated QR placeholder",
            "no perspective-skewed QR container",
            "no diagonal QR sign",
        ])
    else:
        negative.append(
            "no blank empty area, no white placeholder square, no empty frame, "
            "no reserved card area, no QR hosting prop"
        )
    if not allow_ai_text:
        negative.extend(["no text", "no words", "no letters"])
    if has_food:
        negative.extend(FOOD_REFERENCE_NEGATIVE_TERMS)
    if not _style_ref_dominant:
        # Only enforce appetite color and template-specific negatives when NOT in style ref dominant mode
        negative.extend(APPETITE_COLOR_NEGATIVE_TERMS)
        negative.extend(template_negative_items(template))
    qr_host_section = ARTISTIC_QR_HOST_ALIGNMENT_RULE if realism == "artistic" else (QR_HOST_ALIGNMENT_RULE if has_qr else "")
    prompt_sections = {
        "BASE_STYLE": _style_reference_base_style() if _style_ref_dominant else _section_base_style(realism),
        "DESIGN_STYLE": (
            {
                "style_id": "style_reference_dominant",
                "label": "风格参考图主导模式",
                "scene_prompt": style_scene_guidance,
                "design": style_design_guidance,
                "color_strategy": "由风格参考图决定",
                "composition_strategy": "由风格参考图决定",
                "texture_guidance": style_texture_guidance,
            }
            if _style_ref_dominant
            else {
                "style_id": style_id,
                "label": template.get("label", ""),
                "scene_prompt": style_scene_guidance,
                "design": style_design_guidance,
                "color_strategy": template.get("color_strategy", ""),
                "composition_strategy": template.get("composition_strategy", ""),
                "texture_guidance": style_texture_guidance,
            }
        ),
        "TONE_MODIFIER": _section_tone_modifier(tone),
        "CUISINE_CITY": _build_cuisine_city_section(req),
        "SCENE_ELEMENTS": {
            "material_type": req["type"],
            "layout": {"id": layout.get("id"), "name": layout.get("name")},
            "scene": {
                **{k: v for k, v in (req.get("scene") or {}).items() if k not in ("intent", "business_intent", "business_intents")},
                "_note": "scene.intent/business_intent are internal routing fields — do NOT render them as visible text",
            },
            "seasonal_pack": req.get("scene", {}).get("seasonal_pack", ""),
            "composition_rule": DIGITAL_MATERIAL_COMPOSITION_RULES.get(req["type"], ""),
        },
        "COPY_ATMOSPHERE": _copy_atmosphere_section(req),
        "BRAND_CONSTRAINT": _brand_constraint_section(req),
        "appetite_color_policy": (
            "色调由风格参考图主导，不强制使用食欲暖色或模板预设色彩策略。"
            if _style_ref_dominant
            else _appetite_color_policy(style_id)
        ),
        "STYLE_FACTORS": req["style"].get("style_factors", {}),
        "QR_HOST": qr_host_section,
    }
    if has_food:
        prompt_sections["FOOD_REFERENCE"] = _food_reference_fidelity_section(
            len(req["assets"].get("reference_images") or [])
        )
    visual_requirements = _build_visual_requirements(
        has_qr,
        text_rule,
        realism,
        include_logo_safe_zone=include_platform_logo,
        style_reference_dominant=_style_ref_dominant,
    )
    if protected_ip_guard:
        visual_requirements.append(PROTECTED_IP_VISUAL_RULE)

    prompt: dict[str, Any] = {
        "task": "生成一张完整的美团餐饮营销物料海报，画面和谐一体，适合 Responses API image_generation。",
        "⚠️_TEXT_POLICY": (
            "ONLY render text from 'allowed_text' list below. "
            "NEVER render business_intent labels, style names, cuisine_tag, city_tag, copy.dimensions, "
            "or any other internal metadata as visible text on the poster. "
            "Fields marked with '_DO_NOT_RENDER' are strictly internal."
        ),
        "canvas_px": f"{width}x{height}",
        "material_type": req["type"],
        "business_intent": {
            "primary": primary_business_intent,
            "all": business_intents,
            "label": BUSINESS_INTENT_LABELS.get(primary_business_intent, "日常到店引流"),
            "_DO_NOT_RENDER": "This field is internal metadata for style selection only. NEVER render business_intent label as visible text on the poster image.",
        },
        "copy": {
            "selected_text": req.get("copy", {}).get("selected_text", ""),
        },
        "campaign": {
            "title": req["title"],
            "title_style": req.get("title_style"),
            "theme": req["campaign"].get("theme"),
            "offer": req["campaign"].get("offer"),
            "cta": req["campaign"].get("cta"),
            "store": req["store"].get("name"),
            "category": req["store"].get("category"),
            "season_atmosphere": req.get("style", {}).get("season_atmosphere"),
        },
        "local_assets_policy": {
            "brand_asset_rule": "本地品牌素材由脚本后合成或作为参考提供，模型不要自行生成、改造或替换这些资产。",
            "logo_rule": (
                f"最终海报必须带 {logo_label}；脚本会把本地 logo 后合成到左上角，模型不要自行生成或改造 logo。原因：{logo_reason}。"
                if include_platform_logo
                else "本次不展示平台 Logo；脚本不会后合成平台 logo。模型不要自行生成、改造或替换美团/大众点评平台 logo。"
            ),
            "mascot_rule": mascot_rule,
            "font_rule": (
                "字体版权合规：不得指定或模仿具体商业/版权字体；"
                "只按 typography_profile.prompt_guidance 设计通用字形。"
            ),
        },
        "visual_requirements": visual_requirements,
        "style": (
            {
                "_DO_NOT_RENDER": "All fields in this 'style' object are internal design parameters. NEVER render name/label/cuisine_tag/city_tag/tone as visible text on the poster.",
                "name": req["style"]["name"],
                "realism": realism,
                "tone": tone,
                "style_id": "style_reference_dominant",
                "label": "风格参考图主导模式",
                "guidance": style_design_guidance,
                "scene": style_scene_guidance,
                "color_strategy": "由风格参考图决定",
                "composition_strategy": "由风格参考图决定",
                "texture_guidance": style_texture_guidance,
                "cuisine_tag": req["style"].get("cuisine_tag", ""),
                "city_tag": req["style"].get("city_tag", ""),
                "style_factors": req["style"].get("style_factors", {}),
                "typography_profile": typography_profile,
            }
            if _style_ref_dominant
            else {
                "_DO_NOT_RENDER": "All fields in this 'style' object are internal design parameters. NEVER render name/label/cuisine_tag/city_tag/tone as visible text on the poster.",
                "name": req["style"]["name"],
                "realism": realism,
                "tone": tone,
                "style_id": style_id,
                "label": template.get("label", ""),
                "guidance": style_design_guidance,
                "scene": style_scene_guidance,
                "color_strategy": template.get("color_strategy", ""),
                "composition_strategy": template.get("composition_strategy", ""),
                "texture_guidance": style_texture_guidance,
                "cuisine_tag": req["style"].get("cuisine_tag", ""),
                "city_tag": req["style"].get("city_tag", ""),
                "style_factors": req["style"].get("style_factors", {}),
                "typography_profile": typography_profile,
            }
        ),
        "prompt_sections": prompt_sections,
        "allowed_text": allowed_text,
        "display_text_plan": text_candidates,
        "dish_labels": dish_labels,
        "food_rule": (
            "已提供真实菜品/素材参考图，参考图保真优先。菜品主体采用真实美食摄影感，保留主要器皿、菜品结构、食材色泽、真实材质、光线方向和自然瑕疵；允许适度商业美化，包括轻度补光、提亮、清理杂乱背景、增强食欲色彩和融入海报构图。菜品亮度和清晰度下限：菜品主体必须曝光充足、细节清晰、自然食物高光可见，不能暗沉、低对比或被灰墨感覆盖。不得编造未提供的菜品事实。"
            if has_food
            else "未提供真实菜品图，不要生成具体假菜品或假菜单；用蒸汽、餐具、灯光、色彩和抽象食欲线索表现餐饮氛围。"
        ),
        "food_style_isolation": (
            "⚠️ FOOD STYLE ISOLATION (MANDATORY): 菜品区域与背景/装饰区域必须分层渲染。"
            "背景层可以自由使用设计风格（复古颗粒、牛皮纸纹理、做旧笔触、国潮水墨、霓虹光效等），"
            "但菜品主体（食材、汤汁、酱料、器皿内部）必须保持摄影级写实渲染，不继承背景的材质滤镜。"
            "具体要求：菜品区域禁止叠加颗粒噪点、纸张纹理、做旧划痕、水彩笔触、油画肌理等背景材质效果；"
            "菜品色温只允许受环境光自然影响（如暖光场景下的轻微暖色偏移），不允许被背景滤镜整体染色；"
            "菜品高光、油光、汤汁反射必须保持自然通透，不能被背景的哑光/磨砂质感压制。"
            "盘沿、碗沿等器皿边缘是菜品层与背景层的自然过渡区，允许轻微的环境光色温融合和柔和阴影，"
            "但器皿内部的食物主体必须严格保持写实摄影质感。"
            "简言之：背景可以很有设计感，菜品必须像相机拍的。"
        ) if has_food else None,
        "text_accuracy_constraint": (
            "⚠️ TEXT ACCURACY (MANDATORY): 物料中所有文字（主标题、副标题、菜名标签、CTA、背景装饰小字等）"
            "必须逐字准确，严禁乱码、错别字、同音字替换、形近字替换、繁简混用或凭空臆造文字。"
            "如无法保证某处文字逐字正确，宁可该处不出现文字。"
        ),
        "negative": ", ".join(negative) + ", " + TEXT_ACCURACY_NEGATIVE,
    }
    # Inject style reference guidance at HIGHEST priority position (top of dict)
    style_ref_guidance = _build_style_reference_guidance(req)
    if style_ref_guidance:
        prompt = {"style_reference_guidance": style_ref_guidance, **prompt}
    if has_qr:
        prompt["qr_placement_output"] = {
            "instruction": "生成后输出 <qr_placement>{...}</qr_placement>，坐标必须对应真实 QR 将要贴入的白色正方形内区；该内区必须正对镜头、轴对齐、0° 旋转、无倾斜、无透视。",
            "schema": {
                "x_px": "QR body left",
                "y_px": "QR body top",
                "size_px": "QR body square side",
                "anchor": "top-left/top-center/top-right/center-left/center/center-right/bottom-left/bottom-center/bottom-right",
            },
        }
    return json.dumps(prompt, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# QR placement parsing
# ---------------------------------------------------------------------------

QR_PLACEMENT_ANCHORS = {
    "top-left", "top-center", "top-right",
    "center-left", "center", "center-right",
    "bottom-left", "bottom-center", "bottom-right",
}


def parse_qr_placement(text: str, canvas_width: int, canvas_height: int) -> dict[str, Any] | None:
    """Extract and validate qr_placement JSON from model text response."""
    tag_match = re.search(r"<qr_placement>\s*(\{.*?\})\s*</qr_placement>", text, re.S)
    candidate_texts: list[str] = []
    if tag_match:
        candidate_texts.append(tag_match.group(1))
    for json_match in re.finditer(r"\{[^{}]*\"x_px\"[^{}]*\}", text, re.S):
        candidate_texts.append(json_match.group(0))

    for raw in candidate_texts:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if not {"x_px", "y_px", "size_px"}.issubset(data.keys()):
            continue
        try:
            x_px = max(0, int(data["x_px"]))
            y_px = max(0, int(data["y_px"]))
            size_px = max(32, int(data["size_px"]))
        except (TypeError, ValueError):
            continue
        size_px = min(size_px, min(canvas_width, canvas_height) // 2)
        x_px = min(x_px, max(0, canvas_width - size_px))
        y_px = min(y_px, max(0, canvas_height - size_px))
        anchor = data.get("anchor", "")
        if anchor not in QR_PLACEMENT_ANCHORS:
            anchor = _infer_anchor(x_px, y_px, size_px, canvas_width, canvas_height)
        return {
            "x_px": x_px,
            "y_px": y_px,
            "size_px": size_px,
            "anchor": anchor,
            "canvas_width": canvas_width,
            "canvas_height": canvas_height,
            "_source": "model_response",
        }
    return None


def _infer_anchor(x_px: int, y_px: int, size_px: int, canvas_width: int, canvas_height: int) -> str:
    cx = x_px + size_px / 2
    cy = y_px + size_px / 2
    col = "left" if cx < canvas_width / 3 else ("right" if cx > canvas_width * 2 / 3 else "center")
    row = "top" if cy < canvas_height / 3 else ("bottom" if cy > canvas_height * 2 / 3 else "center")
    if row == "center" and col == "center":
        return "center"
    if row == "center":
        return f"center-{col}"
    if col == "center":
        return f"{row}-center"
    return f"{row}-{col}"


def fallback_qr_placement(canvas_width: int, canvas_height: int) -> dict[str, Any]:
    """Return a safe default QR placement when the model provides no usable output.

    Default position is vertically centered (50% of height) and horizontally centered,
    which works well for most poster layouts where the QR should be in the visual center.
    """
    size_px = max(220, round(min(canvas_width, canvas_height) * 0.25))
    x_px = (canvas_width - size_px) // 2
    y_px = (canvas_height - size_px) // 2
    return {
        "x_px": x_px,
        "y_px": y_px,
        "size_px": size_px,
        "anchor": "center",
        "canvas_width": canvas_width,
        "canvas_height": canvas_height,
        "_source": "fallback",
    }


# ---------------------------------------------------------------------------
# Program-based QR candidate region scoring (sampled for efficiency)
# ---------------------------------------------------------------------------

def _sampled_region_luminance(pixels: list[tuple[int, ...]], x: int, y: int, size: int, img_w: int, img_h: int) -> float:
    """Mean luminance of a square region using stride-based sampling for speed."""
    total = 0.0
    count = 0
    step = max(1, size // 16)  # sample ~16x16 = 256 points max
    for row in range(max(0, y), min(y + size, img_h), step):
        for col in range(max(0, x), min(x + size, img_w), step):
            r, g, b = pixels[row * img_w + col][:3]
            total += 0.299 * r + 0.587 * g + 0.114 * b
            count += 1
    return total / max(1, count)


def _sampled_region_edge_density(pixels: list[tuple[int, ...]], x: int, y: int, size: int, img_w: int, img_h: int) -> float:
    """Rough edge density using stride-based sampling."""
    total = 0.0
    count = 0
    step = max(1, size // 16)
    for row in range(max(0, y), min(y + size, img_h), step):
        for col in range(max(0, x), min(x + size - 1, img_w - 1), step):
            idx = row * img_w + col
            r1, g1, b1 = pixels[idx][:3]
            r2, g2, b2 = pixels[idx + 1][:3]
            lum1 = 0.299 * r1 + 0.587 * g1 + 0.114 * b1
            lum2 = 0.299 * r2 + 0.587 * g2 + 0.114 * b2
            total += abs(lum1 - lum2)
            count += 1
    return (total / max(1, count)) / 255.0


def _sampled_region_color_complexity(pixels: list[tuple[int, ...]], x: int, y: int, size: int, img_w: int, img_h: int) -> float:
    """Color complexity using stride-based sampling."""
    quantized: set[tuple[int, int, int]] = set()
    step = max(1, size // 32)
    for row in range(max(0, y), min(y + size, img_h), step):
        for col in range(max(0, x), min(x + size, img_w), step):
            r, g, b = pixels[row * img_w + col][:3]
            quantized.add((r >> 4, g >> 4, b >> 4))
    max_possible = min(512, (size // step) ** 2)
    return len(quantized) / max(1, max_possible)


def _image_pixels(img: Any) -> list[tuple[int, ...]]:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=DeprecationWarning, message=r"Image\.Image\.getdata.*")
        return list(img.getdata())


def score_qr_candidates(
    poster_path: Path,
    canvas_width: int,
    canvas_height: int,
    qr_size_px: int,
    model_hint: dict[str, Any] | None = None,
    *,
    grid_step: int | None = None,
    safety_margin: int | None = None,
) -> list[dict[str, Any]]:
    """Score candidate QR placement regions on the poster image.

    Uses adaptive grid step for efficiency — larger step on bigger canvases.
    Returns top candidates sorted by score (best first).
    """
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        if model_hint:
            model_hint["score"] = 0.5
            model_hint["_source"] = "model_hint_only"
            return [model_hint]
        fb = fallback_qr_placement(canvas_width, canvas_height)
        fb["score"] = 0.3
        return [fb]

    try:
        img = Image.open(poster_path).convert("RGB")
    except Exception:
        if model_hint:
            model_hint["score"] = 0.5
            model_hint["_source"] = "model_hint_only"
            return [model_hint]
        fb = fallback_qr_placement(canvas_width, canvas_height)
        fb["score"] = 0.3
        return [fb]

    img_w, img_h = img.size
    pixels = _image_pixels(img)

    # Scale model hint coordinates if canvas differs from actual image
    if model_hint:
        hint_canvas_w = int(model_hint.get("canvas_width", canvas_width) or canvas_width)
        hint_canvas_h = int(model_hint.get("canvas_height", canvas_height) or canvas_height)
        if hint_canvas_w != img_w or hint_canvas_h != img_h:
            model_hint = dict(model_hint)
            model_hint["x_px"] = round(int(model_hint.get("x_px", 0)) * img_w / max(1, hint_canvas_w))
            model_hint["y_px"] = round(int(model_hint.get("y_px", 0)) * img_h / max(1, hint_canvas_h))
            model_hint["size_px"] = round(int(model_hint.get("size_px", qr_size_px)) * min(img_w / max(1, hint_canvas_w), img_h / max(1, hint_canvas_h)))
            model_hint["canvas_width"] = img_w
            model_hint["canvas_height"] = img_h

    # Adaptive defaults — larger QR targets use coarser grid for speed.  Tests
    # and debugging callers can override them for deterministic candidate scans.
    if grid_step is None:
        grid_step = max(qr_size_px // 2, 48)
    if safety_margin is None:
        safety_margin = max(16, qr_size_px // 8)
    candidates: list[dict[str, Any]] = []

    for cy in range(safety_margin, img_h - qr_size_px - safety_margin + 1, grid_step):
        for cx in range(safety_margin, img_w - qr_size_px - safety_margin + 1, grid_step):
            lum = _sampled_region_luminance(pixels, cx, cy, qr_size_px, img_w, img_h)
            edge = _sampled_region_edge_density(pixels, cx, cy, qr_size_px, img_w, img_h)
            cplx = _sampled_region_color_complexity(pixels, cx, cy, qr_size_px, img_w, img_h)

            min_dist = min(cx, img_w - (cx + qr_size_px), cy, img_h - (cy + qr_size_px))
            safety_ok = min_dist >= safety_margin

            lum_score = max(0.0, min(1.0, (lum - 80) / 175))
            edge_penalty = edge
            cplx_penalty = cplx
            safety_bonus = 0.1 if safety_ok else -0.2

            center_y_ratio = (cy + qr_size_px / 2) / max(1, img_h)
            if center_y_ratio < 0.20:
                placement_bonus = -0.25
            elif center_y_ratio < 0.30:
                placement_bonus = -0.10
            elif center_y_ratio < 0.70:
                placement_bonus = 0.20
            elif center_y_ratio < 0.80:
                placement_bonus = 0.05
            else:
                placement_bonus = -0.10

            score = (
                lum_score * 0.45
                + (1.0 - edge_penalty) * 0.25
                + (1.0 - cplx_penalty) * 0.15
                + safety_bonus
                + placement_bonus
            )

            # Model hint proximity bonus
            model_prior = 0.0
            if model_hint:
                hx = model_hint.get("x_px", 0)
                hy = model_hint.get("y_px", 0)
                dist = ((cx - hx) ** 2 + (cy - hy) ** 2) ** 0.5
                max_dist = (img_w ** 2 + img_h ** 2) ** 0.5
                proximity = 1.0 - min(1.0, dist / max(1, max_dist * 0.3))
                model_prior = proximity * 0.1
                score += model_prior

            candidates.append({
                "x_px": cx,
                "y_px": cy,
                "size_px": qr_size_px,
                "score": round(score, 4),
                "luminance": round(lum, 1),
                "edge_density": round(edge, 4),
                "color_complexity": round(cplx, 4),
                "safety_ok": safety_ok,
                "model_prior": round(model_prior, 4),
                "canvas_width": img_w,
                "canvas_height": img_h,
                "_source": "program_scored",
            })

    # Also score the model's explicitly reported QR slot
    if model_hint:
        try:
            hint_size = max(32, int(model_hint.get("size_px", qr_size_px)))
            hint_size = min(hint_size, min(img_w, img_h) // 2)
            hx = max(0, min(int(model_hint.get("x_px", 0)), max(0, img_w - hint_size)))
            hy = max(0, min(int(model_hint.get("y_px", 0)), max(0, img_h - hint_size)))
        except (TypeError, ValueError):
            hx = hy = hint_size = 0
        if hint_size:
            lum = _sampled_region_luminance(pixels, hx, hy, hint_size, img_w, img_h)
            edge = _sampled_region_edge_density(pixels, hx, hy, hint_size, img_w, img_h)
            cplx = _sampled_region_color_complexity(pixels, hx, hy, hint_size, img_w, img_h)
            min_dist = min(hx, img_w - (hx + hint_size), hy, img_h - (hy + hint_size))
            safety_ok = min_dist >= safety_margin
            lum_score = max(0.0, min(1.0, (lum - 80) / 175))
            center_y_ratio = (hy + hint_size / 2) / max(1, img_h)
            if center_y_ratio < 0.20:
                placement_bonus = -0.25
            elif center_y_ratio < 0.30:
                placement_bonus = -0.10
            elif center_y_ratio < 0.70:
                placement_bonus = 0.20
            elif center_y_ratio < 0.80:
                placement_bonus = 0.05
            else:
                placement_bonus = -0.10
            score = (
                lum_score * 0.45
                + (1.0 - edge) * 0.25
                + (1.0 - cplx) * 0.15
                + (0.1 if safety_ok else -0.2)
                + placement_bonus
                + 0.16  # model prior bonus
            )
            candidates.append({
                "x_px": hx,
                "y_px": hy,
                "size_px": hint_size,
                "score": round(score, 4),
                "luminance": round(lum, 1),
                "edge_density": round(edge, 4),
                "color_complexity": round(cplx, 4),
                "safety_ok": safety_ok,
                "model_prior": 0.16,
                "canvas_width": img_w,
                "canvas_height": img_h,
                "_source": "model_hint_scored",
            })

    candidates.sort(key=lambda c: c["score"], reverse=True)

    # Tag each with anchor
    for c in candidates[:20]:
        cx_val, cy_val, c_size = c["x_px"], c["y_px"], c["size_px"]
        center_x = cx_val + c_size / 2
        center_y = cy_val + c_size / 2
        vert = "top" if center_y < img_h * 0.33 else ("bottom" if center_y > img_h * 0.66 else "center")
        horiz = "left" if center_x < img_w * 0.33 else ("right" if center_x > img_w * 0.66 else "center")
        c["anchor"] = f"{vert}-{horiz}"
        c["bg_luminance"] = "light" if c["luminance"] > 160 else "dark"
        c["contrast_suggestion"] = "dark-on-light" if c["luminance"] > 160 else "light-on-dark"

    return candidates[:20]


def _slot_geometry_is_clean_for_artistic(slot_info: dict[str, Any]) -> bool:
    inner = slot_info.get("inner_rect") or {}
    try:
        iw = float(inner.get("w", 0))
        ih = float(inner.get("h", 0))
        aspect = float(slot_info.get("aspect_ratio") or (iw / max(1.0, ih)))
        rotation = abs(float(slot_info.get("rotation_deg", 0.0)))
        luminance = float(slot_info.get("slot_luminance", 0.0))
    except (TypeError, ValueError):
        return False
    return (
        iw > 0
        and ih > 0
        and 0.75 <= aspect <= 1.35
        and rotation <= 2.0
        and luminance >= 220.0
    )


def _decide_qr_slot_fit_mode(
    slot_info: dict[str, Any],
    model_hint: dict[str, Any] | None,
    width: int,
    height: int,
    realism: str,
) -> tuple[str, str]:
    """Apply the trust chain for QR slot detection."""
    slot_status = slot_info.get("status", "no_slot")
    if slot_status not in ("detected_slot", "soft_slot") or not slot_info.get("inner_rect"):
        return "fallback_card", "artistic_fallback" if realism == "artistic" else ("model_hint_preferred" if model_hint else "fallback")

    try:
        slot_score = float(slot_info.get("slot_score", 0.0))
    except (TypeError, ValueError):
        slot_score = 0.0
    border_kind = slot_info.get("border_kind", "none")
    is_artistic = realism == "artistic"
    high_threshold = 0.75 if is_artistic and border_kind == "regular_frame" else 0.85

    if slot_score >= high_threshold:
        return slot_status, "artistic_slot_high_confidence" if is_artistic else "slot_high_confidence"

    if slot_score >= 0.50:
        if model_hint:
            inner = slot_info["inner_rect"]
            slot_cx = inner["x"] + inner["w"] / 2
            slot_cy = inner["y"] + inner["h"] / 2
            hint_cx = model_hint.get("x_px", 0) + model_hint.get("size_px", 0) / 2
            hint_cy = model_hint.get("y_px", 0) + model_hint.get("size_px", 0) / 2
            short_side = min(width, height)
            distance = ((slot_cx - hint_cx) ** 2 + (slot_cy - hint_cy) ** 2) ** 0.5
            if distance < short_side * 0.15:
                return slot_status, "artistic_slot_cross_validated" if is_artistic else "slot_cross_validated"
            return "fallback_card", "artistic_model_hint_preferred" if is_artistic else "model_hint_preferred"

        if is_artistic:
            if border_kind == "regular_frame" and _slot_geometry_is_clean_for_artistic(slot_info):
                return slot_status, "artistic_slot_medium_regular_frame_no_hint"
            return "fallback_card", "artistic_fallback"

        return slot_status, "slot_medium_no_hint"

    return "fallback_card", "artistic_fallback" if is_artistic else ("model_hint_preferred" if model_hint else "fallback")


def _qr_alignment_check(
    poster_path: Path,
    inner_rect: dict[str, Any],
    qr_rect: dict[str, Any],
) -> dict[str, Any]:
    """Check that the pasted QR rectangle actually lands inside the detected slot."""
    ix, iy, iw, ih = (int(inner_rect.get(k, 0)) for k in ("x", "y", "w", "h"))
    qx, qy, qw, qh = (int(qr_rect.get(k, 0)) for k in ("x", "y", "w", "h"))
    inner_cx = ix + iw / 2
    inner_cy = iy + ih / 2
    qr_cx = qx + qw / 2
    qr_cy = qy + qh / 2
    center_distance = ((inner_cx - qr_cx) ** 2 + (inner_cy - qr_cy) ** 2) ** 0.5

    overlap_x0 = max(ix, qx)
    overlap_y0 = max(iy, qy)
    overlap_x1 = min(ix + iw, qx + qw)
    overlap_y1 = min(iy + ih, qy + qh)
    overlap_area = max(0, overlap_x1 - overlap_x0) * max(0, overlap_y1 - overlap_y0)
    qr_area = max(1, qw * qh)
    coverage_ratio = overlap_area / qr_area

    original_luminance = 0.0
    try:
        from PIL import Image  # type: ignore

        with Image.open(poster_path).convert("RGB") as image:
            pixels = _image_pixels(image)
            original_luminance = _sampled_region_luminance(
                pixels,
                max(0, qx),
                max(0, qy),
                max(1, min(qw, qh)),
                image.width,
                image.height,
            )
    except Exception:
        original_luminance = 0.0

    max_center_distance = max(12.0, min(max(1, iw), max(1, ih)) * 0.10)
    status = "ok"
    if center_distance > max_center_distance or coverage_ratio < 0.85 or original_luminance < 200:
        status = "warning"

    return {
        "alignment_status": status,
        "alignment_check": {
            "center_distance_px": round(center_distance, 2),
            "slot_coverage_ratio": round(coverage_ratio, 3),
            "original_luminance": round(original_luminance, 1),
        },
    }


# ---------------------------------------------------------------------------
# Mock PNG generation (for dry-run / none provider)
# ---------------------------------------------------------------------------

def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)


def write_mock_png(path: Path, width: int, height: int, variant: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    palettes = {
        "street_warm": ((255, 180, 50), (200, 80, 20), (255, 240, 200)),
        "clean_hero": ((248, 246, 240), (216, 212, 204), (255, 255, 255)),
        "dark_premium": ((17, 17, 17), (199, 131, 58), (243, 232, 208)),
        "bold_split": ((177, 18, 24), (243, 107, 33), (21, 16, 14)),
        "collage_pop": ((240, 68, 56), (37, 99, 235), (255, 243, 214)),
        "natural_earth": ((143, 174, 139), (233, 220, 197), (185, 120, 85)),
        "neon_night": ((11, 13, 18), (232, 62, 140), (29, 183, 201)),
        "festive_red": ((217, 31, 38), (216, 164, 65), (255, 241, 214)),
        "illustration_flat": ((255, 244, 222), (255, 107, 95), (76, 175, 112)),
        "ink_oriental": ((245, 240, 230), (60, 63, 58), (168, 184, 160)),
        "retro_poster": ((79, 138, 139), (166, 66, 42), (239, 224, 194)),
        "latin_fiesta": ((201, 76, 46), (0, 140, 140), (246, 179, 49)),
    }
    c1, c2, c3 = palettes.get(variant, palettes["street_warm"])
    rows = []
    for y in range(height):
        t = y / max(1, height - 1)
        row = bytearray([0])
        for x in range(width):
            u = x / max(1, width - 1)
            blend = min(1.0, max(0.0, (t * 0.68 + u * 0.32)))
            r = round(c1[0] * (1 - blend) + c2[0] * blend)
            g = round(c1[1] * (1 - blend) + c2[1] * blend)
            b = round(c1[2] * (1 - blend) + c2[2] * blend)
            if (x // 38 + y // 38) % 9 == 0:
                r = round(r * 0.78 + c3[0] * 0.22)
                g = round(g * 0.78 + c3[1] * 0.22)
                b = round(b * 0.78 + c3[2] * 0.22)
            row.extend((r, g, b))
        rows.append(bytes(row))
    raw = b"".join(rows)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(raw, 6))
        + png_chunk(b"IEND", b"")
    )
    path.write_bytes(png)


# ---------------------------------------------------------------------------
# Image payload extraction from API response
# ---------------------------------------------------------------------------

def find_first_image_payload(value: Any) -> tuple[str, str] | None:
    if isinstance(value, dict):
        for key in ("b64_json", "base64", "image_base64"):
            if isinstance(value.get(key), str) and value[key].strip():
                return "b64", value[key].strip()
        for key in ("url", "image", "image_url"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                payload = item.strip()
                if payload.startswith("data:image/"):
                    return "data_url", payload
                if payload.startswith("http://") or payload.startswith("https://"):
                    return "url", payload
            if isinstance(item, dict):
                found = find_first_image_payload(item)
                if found:
                    return found
        for item in value.values():
            found = find_first_image_payload(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_first_image_payload(item)
            if found:
                return found
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith("data:image/"):
            return "data_url", text
        try:
            found = find_first_image_payload(json.loads(text))
            if found:
                return found
        except json.JSONDecodeError:
            pass
        data_url = re.search(r"data:image/[a-zA-Z0-9+.-]+;base64,[A-Za-z0-9+/=\s]+", text)
        if data_url:
            return "data_url", re.sub(r"\s+", "", data_url.group(0))
        md = re.search(r"!\[[^\]]*\]\((https?://[^)]+)\)", text)
        if md:
            return "url", md.group(1)
        url = re.search(r"https?://[^\s)\"']+", text)
        if url:
            return "url", url.group(0)
        if len(text) > 512 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", text):
            return "b64", re.sub(r"\s+", "", text)
    return None


def save_payload_as_image(kind: str, payload: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "url":
        with urllib.request.urlopen(payload, timeout=120) as response:
            out_path.write_bytes(response.read())
        return
    if kind == "data_url":
        payload = payload.split(",", 1)[1]
    if kind in {"b64", "data_url"}:
        out_path.write_bytes(base64.b64decode(payload))
        return
    raise SkillError(f"未知图片 payload 类型: {kind}")


# ---------------------------------------------------------------------------
# Brand logo overlay
# ---------------------------------------------------------------------------

def _logo_region_luminance(poster: "Any", x: int, y: int, logo_w: int, logo_h: int, padding: int) -> float:
    """Sample mean luminance of the poster region where the logo will be placed.

    Uses a sampling grid for efficiency (max ~256 samples).
    Returns a value in [0, 255]; < 128 means dark background.
    """
    x0, y0 = max(0, x), max(0, y)
    x1 = min(x0 + logo_w + padding, poster.width)
    y1 = min(y0 + logo_h + padding, poster.height)
    region_w = max(1, x1 - x0)
    region_h = max(1, y1 - y0)
    step_x = max(1, region_w // 16)
    step_y = max(1, region_h // 16)
    total = 0.0
    count = 0
    pixels = poster.load()
    for row in range(y0, y1, step_y):
        for col in range(x0, x1, step_x):
            r, g, b = pixels[col, row][:3]
            total += 0.299 * r + 0.587 * g + 0.114 * b
            count += 1
    return total / max(1, count)


def overlay_brand_logo(
    poster_path: Path,
    logo_path: Path,
    out_path: Path | None = None,
    white_logo_path: Path | None = None,
    luminance_threshold: float = 128.0,
    realism: str = "balanced",
    logo_position: str = "top-left",
    logo_size_ratio: float | None = None,
) -> Path:
    """Overlay the official local platform logo asset on the poster.

    If white_logo_path is provided, the function detects the luminance of the
    poster's top-left region (where the logo will sit). If the region is dark
    (luminance < threshold), it uses the white/inverted logo variant for better
    contrast and visual harmony.
    """
    try:
        from PIL import Image, ImageFilter  # type: ignore
    except ImportError:
        raise SkillError("品牌 logo 后合成需要 Pillow，请安装 Pillow 后再生成正式物料。") from None

    poster = Image.open(poster_path).convert("RGBA")

    # --- Determine logo size first (needed for luminance sampling region) ---
    logo_standard = Image.open(logo_path).convert("RGBA")
    bbox = logo_standard.getbbox()
    if bbox:
        logo_standard = logo_standard.crop(bbox)

    aspect = logo_standard.width / max(1, logo_standard.height)
    width_ratio = float(logo_size_ratio) if logo_size_ratio else (0.25 if aspect >= 3.6 else 0.18)
    width_ratio = min(0.35, max(0.10, width_ratio))
    target_w = max(100, round(poster.width * width_ratio))
    target_h = round(target_w / aspect)
    max_h = max(56, round(poster.height * 0.10))
    if target_h > max_h:
        target_h = max_h
        target_w = round(target_h * aspect)

    padding = max(24, round(poster.width * 0.035))

    position = logo_position if logo_position in {
        "top-left", "top-right", "bottom-left", "bottom-right",
    } else "top-left"
    if position.endswith("right"):
        x = max(padding, poster.width - padding - target_w)
    else:
        x = padding
    if position.startswith("bottom"):
        y = max(padding, poster.height - padding - target_h)
    else:
        y = padding

    # --- Adaptive logo selection based on background luminance ---
    lum = _logo_region_luminance(poster, x, y, target_w, target_h, padding)
    use_outline_protection = realism == "artistic" and 100 <= lum <= 160
    if lum < luminance_threshold and not use_outline_protection and white_logo_path and white_logo_path.exists():
        logo_img = Image.open(white_logo_path).convert("RGBA")
        bbox_w = logo_img.getbbox()
        if bbox_w:
            logo_img = logo_img.crop(bbox_w)
        # Recompute size based on white logo's aspect (may differ slightly)
        aspect_w = logo_img.width / max(1, logo_img.height)
        target_w_w = max(100, round(poster.width * (0.25 if aspect_w >= 3.6 else 0.18)))
        target_h_w = round(target_w_w / aspect_w)
        if target_h_w > max_h:
            target_h_w = max_h
            target_w_w = round(target_h_w * aspect_w)
        target_w, target_h = target_w_w, target_h_w
        if position.endswith("right"):
            x = max(padding, poster.width - padding - target_w)
        if position.startswith("bottom"):
            y = max(padding, poster.height - padding - target_h)
        used_logo = "white"
    else:
        if lum < luminance_threshold and not use_outline_protection:
            warnings.warn(
                f"Dark background detected (luminance={lum:.2f}) but white logo "
                f"{'not found at ' + str(white_logo_path) if white_logo_path else 'path not provided'}. "
                "Falling back to standard logo which may have poor contrast.",
                stacklevel=2,
            )
        logo_img = logo_standard
        used_logo = "standard"

    resampling = getattr(Image, "Resampling", Image).LANCZOS
    logo_img = logo_img.resize((target_w, target_h), resampling)

    # Shadow only for standard logo (white logo on dark bg doesn't need dark shadow)
    if use_outline_protection:
        shadow = Image.new("RGBA", poster.size, (0, 0, 0, 0))
        mask = logo_img.split()[3]
        outline_width = max(2, round(target_h * 0.035))
        outline = Image.new("RGBA", poster.size, (0, 0, 0, 0))
        for dx in range(-outline_width, outline_width + 1):
            for dy in range(-outline_width, outline_width + 1):
                if dx * dx + dy * dy <= outline_width * outline_width:
                    outline.paste((255, 255, 255, 255), (x + dx, y + dy), mask)
        shadow_mask = mask.filter(ImageFilter.GaussianBlur(max(3, target_h // 18)))
        shadow.paste((0, 0, 0, 90), (x + max(2, target_h // 24), y + max(2, target_h // 24)), shadow_mask)
        poster = Image.alpha_composite(poster, shadow)
        poster = Image.alpha_composite(poster, outline)
        used_logo = "standard_outline"
    elif used_logo == "standard":
        shadow = Image.new("RGBA", poster.size, (0, 0, 0, 0))
        mask = logo_img.split()[3]
        shadow_mask = mask.filter(ImageFilter.GaussianBlur(max(3, target_h // 18)))
        shadow.paste((0, 0, 0, 105), (x + max(2, target_h // 26), y + max(2, target_h // 26)), shadow_mask)
        poster = Image.alpha_composite(poster, shadow)
    else:
        mask = logo_img.split()[3]

    poster.paste(logo_img, (x, y), mask)

    target = out_path or poster_path
    target.parent.mkdir(parents=True, exist_ok=True)
    poster.save(target, "PNG")
    print(f"[logo overlay] used={used_logo}, region_luminance={lum:.1f}, threshold={luminance_threshold}", flush=True)
    return target


# ---------------------------------------------------------------------------
# API call helpers
# ---------------------------------------------------------------------------

def _assert_proxy_configured() -> None:
    if PROXY_TOKEN == _PROXY_TOKEN_PLACEHOLDER or "placeholder.sankuai.com" in PROXY_BASE_URL:
        raise SkillError(
            "代理服务尚未配置：PROXY_TOKEN 和 PROXY_BASE_URL 仍为占位符。\n"
            "请先部署 restaurant-skill-proxy，然后将 scripts/material_skill.py 中的\n"
            "PROXY_BASE_URL 和 PROXY_TOKEN 替换为真实值。"
        )


def sanitize_api_text(text: str) -> str:
    redacted = text
    for secret in {API_KEY, RESPONSES_API_KEY, PROXY_TOKEN}:
        redacted = redacted.replace(secret, "<redacted>")
        if secret.startswith("sk-") and len(secret) > 3:
            redacted = redacted.replace(secret[3:], "<redacted>")
    redacted = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", redacted, flags=re.I)
    return redacted


def assert_api_contract(endpoint: str, payload: dict[str, Any]) -> None:
    expected_endpoint = PROXY_BASE_URL + API_ENDPOINT_PATH
    if endpoint != expected_endpoint:
        raise SkillError(f"只允许调用 Responses API: {expected_endpoint}，当前 endpoint={endpoint}")
    if payload.get("model") != API_MODEL:
        raise SkillError(f"只允许调用 Responses API model={API_MODEL}")
    tools = payload.get("tools", [])
    if len(tools) != 1:
        raise SkillError("只允许调用 Responses API image_generation tool，不允许使用其它生图工具或内置 GenerateImage")
    tool = tools[0]
    if tool.get("type") != "image_generation" or tool.get("action") != "generate":
        raise SkillError("只允许调用 Responses API image_generation tool，不允许使用其它生图工具或内置 GenerateImage")
    # 允许额外的合法字段如 size、quality
    allowed_keys = {"type", "action", "size", "quality"}
    if set(tool.keys()) - allowed_keys:
        raise SkillError("只允许调用 Responses API image_generation tool，不允许使用其它生图工具或内置 GenerateImage")


def post_json_with_retries(
    endpoint: str,
    payload: dict[str, Any],
    raw_response_path: Path,
    timeout: int = DEFAULT_IMAGE_TIMEOUT,
    max_attempts: int = 3,
    retryable_http_codes: set[int] | None = None,
) -> str:
    request_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    response_text: str | None = None
    retry_codes = _RETRYABLE_HTTP_CODES if retryable_http_codes is None else retryable_http_codes
    for attempt in range(1, max_attempts + 1):
        request = urllib.request.Request(
            endpoint,
            data=request_data,
            headers={"Authorization": f"Bearer {PROXY_TOKEN}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_text = response.read().decode("utf-8")
            break
        except urllib.error.HTTPError as exc:
            try:
                body = sanitize_api_text(exc.read().decode("utf-8", errors="replace"))
            finally:
                exc.close()
            raw_response_path.write_text(body, encoding="utf-8")
            if exc.code in retry_codes and attempt < max_attempts:
                wait = _retry_delay(attempt)
                print(f"[retry] HTTP {exc.code}，{wait:.1f}s 后重试 (attempt {attempt}/{max_attempts})", file=sys.stderr)
                time.sleep(wait)
                continue
            raise SkillError(f"API HTTP {exc.code}: {body[:500]}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, http.client.HTTPException) as exc:
            message = sanitize_api_text(str(exc))
            raw_response_path.write_text(f"API request failed on attempt {attempt}/{max_attempts}: {message}\n", encoding="utf-8")
            if attempt >= max_attempts:
                raise SkillError(f"API 请求失败: {message}") from None
            time.sleep(_retry_delay(attempt))
    if response_text is None:
        raise SkillError("API 请求失败: empty response")
    raw_response_path.write_text(response_text, encoding="utf-8")
    return response_text


def _retry_delay(attempt: int) -> float:
    return 2 ** attempt + random.uniform(0, 1.5)


def call_template_selector_api(
    system_prompt: str,
    user_prompt: str,
    raw_response_path: Path,
    model: str,
    timeout: int = 30,
    max_attempts: int = 1,
) -> str:
    """Call the proxy Responses API for pure-text template selection.

    Default max_attempts=1 so that in 'auto' mode we fail fast and fall back
    to local rules instead of wasting 90+ seconds on retries that compete with
    the parallel image generation requests for API bandwidth.
    """
    _assert_proxy_configured()
    endpoint = PROXY_BASE_URL.rstrip("/") + API_ENDPOINT_PATH
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    response_text = post_json_with_retries(endpoint, payload, raw_response_path, timeout=timeout, max_attempts=max_attempts)
    response_json = json.loads(response_text)
    if not isinstance(response_json, dict):
        raise SkillError(f"Template selector API 返回 JSON 顶层不是对象，原始响应已保存: {raw_response_path}")
    extracted = extract_responses_text(response_json)
    if extracted:
        return extracted
    return response_text


def extract_responses_text(response_json: dict[str, Any]) -> str:
    text_parts: list[str] = []
    if isinstance(response_json.get("output_text"), str):
        text_parts.append(response_json["output_text"])
    for output in response_json.get("output") or []:
        if not isinstance(output, dict):
            continue
        content_val = output.get("content")
        if isinstance(content_val, str):
            text_parts.append(content_val)
        elif isinstance(content_val, list):
            for part in content_val:
                if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                    text_parts.append(str(part.get("text", "")))
    return "\n".join(part for part in text_parts if part)


def find_responses_image_payload(response_json: dict[str, Any]) -> tuple[str, str] | None:
    for output in response_json.get("output") or []:
        if not isinstance(output, dict) or output.get("type") != "image_generation_call":
            continue
        result = output.get("result")
        if isinstance(result, str) and result.strip():
            return "b64", result.strip()
    return find_first_image_payload(response_json)


def call_image_api(
    prompt: str,
    reference_images: list[str],
    out_path: Path,
    raw_response_path: Path,
    width: int = 1024,
    height: int = 1024,
    timeout: int = DEFAULT_IMAGE_TIMEOUT,
    max_attempts: int = 3,
    retryable_http_codes: set[int] | None = None,
    reference_image_roles: list[str] | None = None,
) -> str:
    """Call Responses API with the image_generation tool and save the image."""
    _assert_proxy_configured()
    endpoint = PROXY_BASE_URL.rstrip("/") + RESPONSES_API_ENDPOINT_PATH
    content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    roles = reference_image_roles or []
    for index, ref in enumerate(reference_images, start=1):
        role = normalize_space(roles[index - 1]) if index - 1 < len(roles) else ""
        if not role:
            role = "reference image: use only as visual reference according to the main prompt."
        content.append({"type": "input_text", "text": f"IMAGE_{index} ROLE: {role}"})
        content.append({"type": "input_image", "image_url": image_to_data_url(Path(ref))})
    image_tool = dict(RESPONSES_API_IMAGE_TOOL)
    image_tool["quality"] = "high"
    # 注意：GPT Image 的 size 限制为长短边比例不超过 3:1，
    # 五连图的 20:3 (6.67:1) 超出此限制，模型会自行选择最接近的合法尺寸
    if width > 0 and height > 0 and max(width, height) / min(width, height) <= 3.0:
        image_tool["size"] = f"{width}x{height}"
    payload = {
        "model": RESPONSES_API_MODEL,
        "reasoning": {"effort": "high"},
        "input": [{"role": "user", "content": content}],
        "tools": [image_tool],
    }
    assert_api_contract(endpoint, payload)
    response_text = post_json_with_retries(
        endpoint,
        payload,
        raw_response_path,
        timeout=timeout,
        max_attempts=max_attempts,
        retryable_http_codes=retryable_http_codes,
    )
    response_json = json.loads(response_text)
    if not isinstance(response_json, dict):
        raise SkillError(f"API 返回 JSON 顶层不是对象，原始响应已保存: {raw_response_path}")
    found = find_responses_image_payload(response_json)
    if not found:
        raise SkillError(f"Responses API 返回中未找到 image_generation 图片结果，原始响应已保存: {raw_response_path}")
    save_payload_as_image(found[0], found[1], out_path)
    return extract_responses_text(response_json)


def generate_scene_image(
    prompt: str,
    reference_images: list[str],
    out_path: Path,
    raw_response_path: Path,
    provider: str,
    dry_run: bool,
    width: int,
    height: int,
    variant: str,
    image_timeout: int = DEFAULT_IMAGE_TIMEOUT,
    image_max_attempts: int = 3,
    image_retryable_http_codes: set[int] | None = None,
    reference_image_roles: list[str] | None = None,
) -> tuple[list[str], str]:
    """Generate AI poster image and return (warnings, model_text)."""
    gen_warnings: list[str] = []
    if dry_run or provider == "none":
        write_mock_png(out_path, width, height, variant)
        gen_warnings.append("未调用图像模型；已生成本地 mock 海报用于链路验证。")
        return gen_warnings, ""
    if provider in {"api", "auto"}:
        model_text = call_image_api(
            prompt,
            reference_images,
            out_path,
            raw_response_path,
            width=width,
            height=height,
            timeout=image_timeout,
            max_attempts=image_max_attempts,
            retryable_http_codes=image_retryable_http_codes,
            reference_image_roles=reference_image_roles,
        )
        gen_warnings.append(f"AI 海报来源: Responses API ({API_MODEL} + image_generation)")
        return gen_warnings, model_text
    raise SkillError(f"未知 image provider: {provider}")


# ---------------------------------------------------------------------------
# HTML / Review output helpers
# ---------------------------------------------------------------------------

def rel(from_dir: Path, target: Path | None) -> str:
    if not target:
        return ""
    return Path(os.path.relpath(target, from_dir)).as_posix()


def esc(value: Any) -> str:
    return _html.escape(str(value or ""), quote=True)


def delivery_run_id() -> str:
    return f"{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"


# 成功调用代理 Responses API 时由 generate_scene_image 记录的来源标记前缀。
_PROXY_PROVENANCE_PREFIX = "AI 海报来源: Responses API"


def _collect_deliverable_pngs(out_dir: Path, run_id: str | None = None) -> list[str]:
    """收集 deliverables/<run_id>/ 下的交付 PNG。

    五连图的 5 张 4:3 切片位于 option_NN_slices/ 子目录，必须纳入
    status.json.files；dx-push 可在调用侧选择只推完整长图。
    """
    deliverables_dir = out_dir / "deliverables"
    files: list[str] = []
    if deliverables_dir.exists():
        run_dirs = [deliverables_dir / run_id] if run_id else sorted(deliverables_dir.iterdir())
        for run_dir_item in run_dirs:
            if run_dir_item.is_dir():
                for f in sorted(run_dir_item.rglob("*.png")):
                    rel_parts = f.relative_to(run_dir_item).parts
                    if f.is_file() and not any(part.startswith(".") for part in rel_parts):
                        files.append(str(f))
    return files


def _proxy_api_actually_used(variants: list["GeneratedVariant"]) -> bool:
    """根据成功变体记录的来源标记判断是否真的走了代理 Responses API。

    material_skill 没有进程内回退：provider 为 api/auto 时只会调用代理；若代理失败，
    变体直接失败、不会静默换源。因此"实际走代理"等价于"存在成功变体且其来源标记为
    Responses API"。比直接看入参 args.image_provider 更准确（入参无法反映运行时失败）。
    """
    return any(
        any(str(w).startswith(_PROXY_PROVENANCE_PREFIX) for w in v.provider_warnings)
        for v in variants
    )


def detect_panorama_content_band(
    image_path: Path,
    white_threshold: int = 244,
    row_content_frac: float = 0.05,
    effective_row_content_frac: float = PANORAMA_EFFECTIVE_ROW_CONTENT_FRAC,
    tail_trim_min_rows: int = PANORAMA_TAIL_TRIM_MIN_ROWS,
) -> dict[str, Any]:
    """检测载体图内容带的实际边界与相对目标 20:3 的偏差。

    模型在 3840×1280 载体上画一条 20:3 内容带、其余留纯白。本函数双向扫描：
    从顶部向下找第一个非白行（top），从底部向上找最后一个非白行（raw_bottom）。
    然后对头尾连续低密度行做对称修剪，避免稀疏阴影、渐变或蒸汽把内容带错误拉大。
    最终 band_height 使用修剪后的 trimmed_top ~ trimmed_bottom 计算。

    Returns dict: {top, bottom, band_height, raw_top, raw_bottom, trimmed_top,
                   trimmed_bottom, deviation, abs_deviation, tail_trimmed, head_trimmed, ...}
    """
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        raise SkillError("五连图内容带检测需要 Pillow，请安装 Pillow。") from None

    with Image.open(image_path).convert("RGB") as image:
        w, h = image.size
        px = image.load()
        step = max(1, w // 640)

        def row_nonwhite_frac(y: int) -> float:
            nonwhite = total = 0
            for x in range(0, w, step):
                r, g, b = px[x, y]
                total += 1
                if not (r > white_threshold and g > white_threshold and b > white_threshold):
                    nonwhite += 1
            return nonwhite / max(1, total)

        target = max(1, round(w / (PANORAMA_FINAL_WIDTH / PANORAMA_FINAL_HEIGHT)))
        trim_params = {
            "detector_version": PANORAMA_BAND_DETECTOR_VERSION,
            "white_threshold": white_threshold,
            "row_content_frac": row_content_frac,
            "effective_row_content_frac": effective_row_content_frac,
            "tail_trim_min_rows": tail_trim_min_rows,
        }

        # 从上往下扫描：第一个有内容的行 = top
        top = None
        for y in range(h):
            if row_nonwhite_frac(y) > row_content_frac:
                top = y
                break
        if top is None:
            deviation = (0 - target) / target
            return {
                "detector_version": PANORAMA_BAND_DETECTOR_VERSION,
                "top": 0,
                "bottom": 0,
                "band_height": 0,
                "deviation": deviation,
                "abs_deviation": abs(deviation),
                "raw_top": 0,
                "raw_bottom": 0,
                "raw_band_height": 0,
                "trimmed_bottom": 0,
                "trimmed_band_height": 0,
                "tail_trimmed": False,
                "tail_trim_rows": 0,
                "content_missing": True,
                "trim_params": trim_params,
            }

        # 从底部向上扫描：第一个有内容的行 = raw_bottom（更鲁棒，避免 top 附近密度波动导致误判）
        raw_bottom = top
        for y in range(h - 1, top, -1):
            if row_nonwhite_frac(y) > row_content_frac:
                raw_bottom = y
                break

        # 尾部修剪：从 raw_bottom 向上跳过连续低密度行（阴影、蒸汽等）
        trimmed_bottom = raw_bottom
        scan_y = raw_bottom
        while scan_y >= top and row_nonwhite_frac(scan_y) <= effective_row_content_frac:
            scan_y -= 1
        tail_trim_rows = raw_bottom - scan_y
        if scan_y >= top and tail_trim_rows >= tail_trim_min_rows:
            trimmed_bottom = scan_y
        else:
            tail_trim_rows = 0

        # 头部修剪：从 top 向下跳过连续低密度行（对称处理）
        trimmed_top = top
        scan_y = top
        while scan_y <= trimmed_bottom and row_nonwhite_frac(scan_y) <= effective_row_content_frac:
            scan_y += 1
        head_trim_rows = scan_y - top
        if scan_y <= trimmed_bottom and head_trim_rows >= tail_trim_min_rows:
            trimmed_top = scan_y
        else:
            head_trim_rows = 0

    raw_band_height = raw_bottom - top + 1
    band_height = trimmed_bottom - trimmed_top + 1
    deviation = (band_height - target) / target
    return {
        "detector_version": PANORAMA_BAND_DETECTOR_VERSION,
        "top": trimmed_top,
        "bottom": trimmed_bottom,
        "band_height": band_height,
        "deviation": deviation,
        "abs_deviation": abs(deviation),
        "raw_top": top,
        "raw_bottom": raw_bottom,
        "raw_band_height": raw_band_height,
        "trimmed_top": trimmed_top,
        "trimmed_bottom": trimmed_bottom,
        "trimmed_band_height": band_height,
        "tail_trimmed": trimmed_bottom != raw_bottom,
        "tail_trim_rows": tail_trim_rows,
        "head_trimmed": trimmed_top != top,
        "head_trim_rows": head_trim_rows,
        "content_missing": False,
        "trim_params": trim_params,
    }


def create_panorama_layout_mask(
    target_path: Path,
    width: int = PANORAMA_CARRIER_WIDTH,
    height: int = PANORAMA_CARRIER_HEIGHT,
    content_height: int = PANORAMA_LAYOUT_MASK_CONTENT_HEIGHT,
) -> Path:
    """Create an internal layout reference mask for panorama carrier generation."""
    try:
        from PIL import Image, ImageDraw  # type: ignore
    except ImportError:
        raise SkillError("五连图版式 mask 需要 Pillow，请安装 Pillow。") from None

    content_height = max(1, min(height - 1, int(content_height)))
    target_path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width - 1, content_height - 1), fill=(220, 220, 220))
    draw.line((0, content_height - 1, width - 1, content_height - 1), fill=(128, 128, 128), width=1)
    image.save(target_path, "PNG")
    return target_path


def create_panorama_boundary_guide(
    target_path: Path,
    width: int = PANORAMA_CARRIER_WIDTH,
    height: int = PANORAMA_CARRIER_HEIGHT,
    top_y: int = PANORAMA_COMPENSATED_TOP_Y,
    bottom_y: int = PANORAMA_COMPENSATED_BOTTOM_Y,
    band_h: int = PANORAMA_COMPENSATED_BAND_HEIGHT,
) -> Path:
    """生成 B2 策略的双红线边界引导参考图。

    红线标记内容带的上下边界（补偿后的像素坐标），引导模型仅在此区间内绘制。
    此图作为参考图传给 API，模型不应复制红线本身——只用于理解内容的上下限位置。
    """
    try:
        from PIL import Image, ImageDraw, ImageFont  # type: ignore
    except ImportError:
        raise SkillError("五连图边界引导图需要 Pillow，请安装 Pillow。") from None

    target_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    # 红色边界线（4px 粗，醒目但不过分）
    draw.rectangle([0, top_y - 2, width - 1, top_y + 2], fill=(255, 0, 0))
    draw.rectangle([0, bottom_y - 2, width - 1, bottom_y + 2], fill=(255, 0, 0))

    # 文字标注（不关键但帮助理解）
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 36)
        font_sm = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 28)
    except Exception:
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 36)
            font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
        except Exception:
            font = ImageFont.load_default()
            font_sm = font

    # 禁区标注
    draw.text((width // 2 - 200, top_y // 2 - 18), "NO CONTENT", fill=(200, 200, 200), font=font)
    draw.text(
        (width // 2 - 200, bottom_y + (height - bottom_y) // 2 - 18),
        "NO CONTENT", fill=(200, 200, 200), font=font,
    )
    # 内容区标注
    mid_y = (top_y + bottom_y) // 2
    draw.text((width // 2 - 260, mid_y - 14), f"CONTENT ZONE ({band_h}px)", fill=(0, 170, 0), font=font_sm)

    img.save(target_path, "PNG")
    return target_path


def crop_panorama_content_band(source_path: Path, target_path: Path, band: dict[str, Any]) -> Path:
    """裁出检测到的完整内容带，保留下游 resize 前的真实边界。"""
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        raise SkillError("五连图内容带裁切需要 Pillow，请安装 Pillow。") from None

    image = Image.open(source_path).convert("RGB")
    w, h = image.size
    top = max(0, min(h - 1, int(band.get("top", 0))))
    bottom = max(top + 1, min(h, int(band.get("bottom", h - 1)) + 1))
    cropped = image.crop((0, top, w, bottom))
    target_path.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(target_path, "PNG")
    return target_path


def normalize_panorama_image(source_path: Path, target_path: Path) -> Path:
    """将内容带强制 resize 到最终交付尺寸 (10240×1536)，不裁剪。"""
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        raise SkillError("五连图后处理需要 Pillow，请安装 Pillow 后再生成正式物料。") from None

    target_path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.open(source_path).convert("RGB")
    resampling = getattr(Image, "Resampling", Image).LANCZOS
    resized = image.resize((PANORAMA_FINAL_WIDTH, PANORAMA_FINAL_HEIGHT), resampling)
    resized.save(target_path, "PNG")
    return target_path


def slice_panorama_image(panorama_path: Path, slice_dir: Path) -> list[Path]:
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        raise SkillError("五连图切片需要 Pillow，请安装 Pillow 后再生成正式物料。") from None

    slice_dir.mkdir(parents=True, exist_ok=True)
    image = Image.open(panorama_path).convert("RGB")
    if image.size != (PANORAMA_FINAL_WIDTH, PANORAMA_FINAL_HEIGHT):
        tmp = slice_dir.parent / f".{panorama_path.stem}.normalized.png"
        normalize_panorama_image(panorama_path, tmp)
        image = Image.open(tmp).convert("RGB")
    panel_w = PANORAMA_FINAL_WIDTH // PANORAMA_SLICE_COUNT
    paths: list[Path] = []
    for index in range(PANORAMA_SLICE_COUNT):
        left = panel_w * index
        right = panel_w * (index + 1) if index < PANORAMA_SLICE_COUNT - 1 else PANORAMA_FINAL_WIDTH
        panel = image.crop((left, 0, right, PANORAMA_FINAL_HEIGHT))
        out_path = slice_dir / f"slice_{index + 1:02d}.png"
        panel.save(out_path, "PNG")
        paths.append(out_path)
    return paths


def validate_panorama_delivery_slices(slices: list[Path]) -> None:
    if len(slices) != PANORAMA_SLICE_COUNT:
        raise SkillError(f"五连图切片交付失败：需要 {PANORAMA_SLICE_COUNT} 张切片，实际 {len(slices)} 张")
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        raise SkillError("五连图切片校验需要 Pillow，请安装 Pillow 后再生成正式物料。") from None

    expected_size = (PANORAMA_FINAL_WIDTH // PANORAMA_SLICE_COUNT, PANORAMA_FINAL_HEIGHT)
    for index, path in enumerate(slices, start=1):
        if not path.exists():
            raise SkillError(f"五连图切片交付失败：slice_{index:02d} 不存在: {path}")
        with Image.open(path) as image:
            if image.size != expected_size:
                raise SkillError(
                    f"五连图切片交付失败：slice_{index:02d} 尺寸应为 "
                    f"{expected_size[0]}×{expected_size[1]}，实际 {image.size[0]}×{image.size[1]}"
                )


def _normalize_panorama_quality_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        result = {}
    status = normalize_space(result.get("status")) or "skipped"
    if status not in {"passed", "failed", "skipped"}:
        status = "skipped"
    issues = normalize_list(result.get("issues"))[:4]
    passed = bool(result.get("passed", status in {"passed", "skipped"}))
    if status == "skipped":
        passed = True
    text_error_count = result.get("text_error_count", 0)
    dish_error_count = result.get("dish_error_count", 0)
    label_position_error_count = result.get("label_position_error_count", 0)
    try:
        text_error_count = max(0, int(text_error_count))
    except (TypeError, ValueError):
        text_error_count = len([issue for issue in issues if "菜名" in issue or "字" in issue or "文字" in issue])
    try:
        dish_error_count = max(0, int(dish_error_count))
    except (TypeError, ValueError):
        dish_error_count = len([issue for issue in issues if "道" in issue or "菜" in issue])
    try:
        label_position_error_count = max(0, int(label_position_error_count))
    except (TypeError, ValueError):
        label_position_error_count = len([issue for issue in issues if "位置" in issue or "标注" in issue or "旁边" in issue])
    if status != "skipped" and (text_error_count or dish_error_count or label_position_error_count):
        status = "failed"
        passed = False
    return {
        "status": status,
        "passed": passed,
        "issues": issues,
        "summary": trim_prompt_text(result.get("summary", ""), 160),
        "text_error_count": text_error_count,
        "dish_error_count": dish_error_count,
        "label_position_error_count": label_position_error_count,
    }


def _panorama_host_quality_env_enabled() -> bool:
    value = normalize_space(os.environ.get("RESTAURANT_PANORAMA_HOST_QUALITY_MODE", ""))
    return value.lower() in {"1", "true", "yes", "agent", "agent_file", "host"}


def _panorama_host_quality_timeout_seconds() -> float:
    raw = normalize_space(os.environ.get("RESTAURANT_PANORAMA_HOST_QUALITY_TIMEOUT", "0"))
    try:
        timeout = max(0.0, float(raw))
    except ValueError:
        return 0.0
    if timeout <= 0:
        return 0.0
    return min(timeout, PANORAMA_HOST_QUALITY_MAX_LEASE_SECONDS)


def _panorama_host_quality_poll_interval_seconds() -> float:
    return PANORAMA_HOST_QUALITY_POLL_INTERVAL_SECONDS


def _panorama_post_check_buffer_seconds() -> float:
    return max(10.0, _panorama_host_quality_timeout_seconds() + _panorama_host_quality_poll_interval_seconds())


def _panorama_out_dir_from_carrier(carrier_path: Path) -> Path | None:
    # .../out/materials/panorama_01/carrier_attemptN.png
    try:
        if carrier_path.parent.parent.name == "materials":
            return carrier_path.parent.parent.parent
    except IndexError:
        pass
    return None


def _panorama_quality_expected_dishes(req: dict[str, Any]) -> list[str]:
    names = [
        normalize_space(item.get("name"))
        for item in dish_label_items(req)
        if normalize_space(item.get("name"))
    ]
    if names:
        return names
    return [
        normalize_space(item.get("name"))
        for item in req.get("products", [])
        if isinstance(item, dict) and normalize_space(item.get("name"))
    ]


def run_panorama_host_quality_check(carrier_path: Path, req: dict[str, Any], attempt: int) -> dict[str, Any]:
    """Host/Agent 视觉质检入口。

    Python 子进程不能直接调用 CatClaw 的 read(path=...) 或 CatDesk 的 image reader。
    因此默认返回 skipped；若宿主通过环境变量启用 agent_file 模式，则写出请求文件并
    等待 Agent 用宿主识图工具读取原始 3840×1280 载体后回填 result.json。
    """
    carrier_path = Path(carrier_path).expanduser().resolve()
    if not _panorama_host_quality_env_enabled():
        return _normalize_panorama_quality_result({
            "status": "skipped",
            "passed": True,
            "issues": [],
            "summary": "host visual quality check is not enabled in this process",
        })

    timeout = _panorama_host_quality_timeout_seconds()
    if timeout <= 0:
        return _normalize_panorama_quality_result({
            "status": "skipped",
            "passed": True,
            "issues": [],
            "summary": "host visual quality check timeout is 0",
        })

    out_dir = _panorama_out_dir_from_carrier(carrier_path)
    result_path = carrier_path.with_name(f"quality_check_attempt{attempt}.result.json")
    request_path = carrier_path.with_name(f"quality_check_attempt{attempt}.request.json")
    expected_dishes = _panorama_quality_expected_dishes(req)
    if expected_dishes:
        content_check_instruction = (
            "先确认能清晰看到预期菜品和菜名标签。"
            "菜名标签只检查 expected_dish_names 中列出的菜名。"
        )
    else:
        content_check_instruction = (
            "先确认能清晰看到预期菜品。"
            "expected_dish_names 为空时，不检查菜名标签缺失，只检查画面是否有有效菜品内容。"
        )
    request_payload = {
        "status": "awaiting_host_quality_check",
        "attempt": attempt,
        "carrier_path": str(carrier_path),
        "carrier_px": f"{PANORAMA_CARRIER_WIDTH}x{PANORAMA_CARRIER_HEIGHT}",
        "expected_dish_names": expected_dishes,
        "result_path": str(result_path),
        "lease_seconds": timeout,
        "poll_interval_seconds": _panorama_host_quality_poll_interval_seconds(),
        "instructions": (
            "请使用宿主/Agent 自身视觉能力直接读取 carrier_path 原始 3840×1280 载体图。"
            f"{content_check_instruction}"
            "只检查顶部内容带，必须检查以下三项："
            "1. 先逐一读出画面中所有可见中文菜名、角标、短标签、标题和营销文字，"
            "再和 expected_dish_names 以及画面语义逐字核对；任何单字错、漏字、多字、异体伪字、"
            "形近字替换都算文字错误，即使大致能猜出含义也必须失败。"
            "重点留意中文形近字和 AI 伪字，例如'推荐'不能写成形似'推蓉/推荅'的错字，"
            "'香辣焖鱼'不能把'焖'写成形似'焖/焗/闷'的错误字形。"
            "2. 菜品是否明显认错（一眼能认出对应菜即通过）；"
            "3. 菜名标签与菜品位置是否匹配（每个菜名标签必须标注在其对应菜品的附近，"
            "而非标注在另一道菜旁边。例如'口水鸡'标签必须靠近口水鸡图片，"
            "不能出现在鲈鱼图片旁边）。"
            "任一项不通过则 status=failed，issues 中说明具体错误文字、应写文字和错误位置。"
            "空白图、内容极少、菜品缺失、菜名缺失或过于模糊时写 status=failed。"
            "不要读取裁切长图或切片。只有宿主工具不可用、非多模态、读图失败、超时或结果解析失败时，才写 status=skipped。"
        ),
        "result_schema": {
            "status": "passed|failed|skipped",
            "passed": "boolean",
            "issues": ["精简问题，最多 3 条"],
            "text_error_count": "number",
            "dish_error_count": "number",
            "label_position_error_count": "number — 菜名标签位置与菜品不匹配的数量",
            "summary": "optional short note",
        },
        "host_tool_examples": {
            "CatClaw": f'read(path="{carrier_path}")',
            "CatDesk": f'mcp_tool_sdk-image-reader_image_read(path="{carrier_path}", scale=true)',
        },
    }
    write_json(request_path, request_payload)
    if out_dir is not None:
        try:
            _write_status(
                out_dir,
                "awaiting_quality_check",
                quality_check_request=request_payload,
            )
        except Exception:
            pass

    deadline = time.monotonic() + timeout
    poll_interval = _panorama_host_quality_poll_interval_seconds()
    while time.monotonic() < deadline:
        if result_path.exists():
            try:
                result = read_json(result_path)
                normalized = _normalize_panorama_quality_result(result)
                if out_dir is not None:
                    try:
                        _write_status(
                            out_dir,
                            "generating",
                            quality_check_request={"status": "completed", "attempt": attempt, "result": normalized},
                        )
                    except Exception:
                        pass
                return normalized
            except Exception as exc:
                skipped = _normalize_panorama_quality_result({
                    "status": "skipped",
                    "passed": True,
                    "issues": [],
                    "summary": f"host quality result parse failed: {exc}",
                })
                if out_dir is not None:
                    try:
                        _write_status(
                            out_dir,
                            "generating",
                            quality_check_request={"status": "skipped", "attempt": attempt, "result": skipped},
                        )
                    except Exception:
                        pass
                return skipped
        time.sleep(min(poll_interval, max(0.1, deadline - time.monotonic())))

    skipped = _normalize_panorama_quality_result({
        "status": "skipped",
        "passed": True,
        "issues": [],
        "summary": f"host visual quality check timed out after {timeout:.0f}s",
    })
    if out_dir is not None:
        try:
            _write_status(
                out_dir,
                "generating",
                quality_check_request={"status": "skipped", "attempt": attempt, "result": skipped},
            )
        except Exception:
            pass
    return skipped


def _panorama_quality_passed(result: dict[str, Any]) -> bool:
    normalized = _normalize_panorama_quality_result(result)
    return normalized["status"] == "skipped" or bool(normalized.get("passed"))


def _panorama_height_feedback(band: dict[str, Any]) -> str:
    deviation = float(band.get("deviation", 0.0))
    band_height = int(band.get("band_height", 0) or 0)
    if band_height <= max(32, round(PANORAMA_TARGET_BAND_HEIGHT * 0.25)):
        return "上一版顶部长条内容过矮或内容不足，本次要把餐饮长卷完整画在顶部超扁横向长条标签内，保持菜品和文字清晰可见。"
    if deviation >= 0:
        return "上一版顶部长条标签画得太高，侵入了下方白纸。本次把所有内容压回画布最上方的一条超扁横向贴纸里，下面保留一整块纯白空白。"
    return "上一版顶部长条标签偏矮，本次略微增加顶部长条内部内容，但仍保持超扁横向贴纸形态，下面保留一整块纯白空白。"


def _panorama_quality_feedback_items(result: dict[str, Any]) -> list[str]:
    normalized = _normalize_panorama_quality_result(result)
    if normalized["status"] == "skipped" or normalized["passed"]:
        return []
    return [trim_prompt_text(issue, 90) for issue in normalized.get("issues", [])[:2] if normalize_space(issue)]


def _panorama_attempt_feedback(
    height_passed: bool,
    band: dict[str, Any],
    quality_result: dict[str, Any],
) -> list[str]:
    feedback: list[str] = []
    if not height_passed:
        feedback.append(_panorama_height_feedback(band))
    feedback.extend(_panorama_quality_feedback_items(quality_result))
    return feedback[:3]


def _panorama_best_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    if not candidates:
        raise SkillError("五连图生成失败：没有可用候选")

    def deviation(c: dict[str, Any]) -> float:
        try:
            return abs(float(c.get("deviation", float("inf"))))
        except (TypeError, ValueError):
            return float("inf")

    def quality_rank(c: dict[str, Any]) -> int:
        quality_check = c.get("quality_check", {})
        if not isinstance(quality_check, dict):
            quality_check = {}
        if c.get("quality_passed") and quality_check.get("status") == "passed":
            return 0
        if c.get("quality_passed") and quality_check.get("status") == "skipped":
            return 1
        if c.get("quality_passed"):
            return 2
        return 3

    def quality_issue_counts(c: dict[str, Any]) -> tuple[int, int, int]:
        quality_check = c.get("quality_check", {})
        if not isinstance(quality_check, dict):
            quality_check = {}
        return (
            int(quality_check.get("text_error_count", 0) or 0),
            int(quality_check.get("dish_error_count", 0) or 0),
            int(quality_check.get("label_position_error_count", 0) or 0),
        )

    def candidate_key(c: dict[str, Any]) -> tuple[int, int, int, int, int, float]:
        text_errors, dish_errors, label_position_errors = quality_issue_counts(c)
        return (
            quality_rank(c),
            text_errors,
            dish_errors,
            label_position_errors,
            0 if c.get("height_passed") else 1,
            deviation(c),
        )

    complete = [c for c in candidates if c.get("accepted")]
    if complete:
        return min(complete, key=candidate_key)

    return min(candidates, key=candidate_key)


def publish_delivery_material(
    variant: GeneratedVariant,
    delivery_dir: Path,
    display_index: int,
    disclaimer: bool = True,
    material_type: str = "",
) -> Path:
    delivery_dir.mkdir(parents=True, exist_ok=True)

    # --- 常规交付路径 ---
    source = variant.final_path or variant.png_path
    if not source or not Path(source).exists():
        raise SkillError(f"variant {variant.index} 没有可交付 PNG")
    target = delivery_dir / f"option_{display_index:02d}.png"
    tmp = target.with_name(f".{target.name}.tmp.{os.getpid()}")
    if material_type == "五连图":
        # 单张载体已裁出完整内容带；如已是最终尺寸则直接复制，避免重复大图 resize。
        copied = False
        try:
            from PIL import Image  # type: ignore
            with Image.open(Path(source)) as image:
                copied = image.size == (PANORAMA_FINAL_WIDTH, PANORAMA_FINAL_HEIGHT)
        except Exception:
            copied = False
        if copied:
            shutil.copy2(source, tmp)
        else:
            normalize_panorama_image(Path(source), tmp)
    else:
        shutil.copy2(source, tmp)
    os.replace(tmp, target)
    if disclaimer and material_type != "五连图":
        _overlay_disclaimer(target)
    if material_type == "五连图":
        # 五连图始终切片交付：option_NN.png 为完整长图，slices 为面向用户的 5 张交付图。
        variant.delivery_slices = slice_panorama_image(target, delivery_dir / f"option_{display_index:02d}_slices")
        validate_panorama_delivery_slices(variant.delivery_slices)
    variant.display_index = display_index
    variant.source_index = variant.index
    variant.delivery_material_path = target
    variant.completed_at = time.strftime("%Y-%m-%d %H:%M:%S")
    return target


def _overlay_disclaimer(image_path: Path) -> None:
    """Overlay 'AI辅助生成 以实际门店为准' on the delivered image.

    Style: white text + drop shadow, no background mask, bottom-right or
    bottom-left (whichever avoids the QR area), font size ~1.8% of shorter side.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont  # type: ignore
    except ImportError:
        return  # graceful degradation — disclaimer shown in delivery message instead

    text = "AI辅助生成 以实际门店为准"
    img = Image.open(image_path).convert("RGBA")
    short_side = min(img.width, img.height)
    font_size = max(12, round(short_side * 0.018))

    font_path = FONT_DIR / "Meituan Type-Regular.TTF"
    try:
        font = ImageFont.truetype(str(font_path), font_size)
    except (OSError, IOError):
        try:
            font = ImageFont.truetype("Arial", font_size)
        except (OSError, IOError):
            font = ImageFont.load_default()

    draw = ImageDraw.Draw(img)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    padding = max(8, round(short_side * 0.015))
    # Default: bottom-right
    x = img.width - text_w - padding
    y = img.height - text_h - padding

    # Drop shadow (1px offset, dark)
    shadow_offset = max(1, font_size // 12)
    draw.text((x + shadow_offset, y + shadow_offset), text, font=font, fill=(0, 0, 0, 160))
    # White text
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 230))

    img = img.convert("RGB")
    img.save(image_path, "PNG")


# ---------------------------------------------------------------------------
# Panorama segmented generation
# ---------------------------------------------------------------------------

def _generate_panorama_carrier(
    index: int,
    template: dict[str, Any],
    req: dict[str, Any],
    runtime: RuntimeAssets,
    layout: dict[str, Any],
    out_dir: Path,
    provider: str,
    dry_run: bool,
    image_timeout: int = DEFAULT_IMAGE_TIMEOUT,
    image_max_attempts: int = 2,
    image_retryable_http_codes: set[int] | None = None,
) -> tuple[Path, list[str], str]:
    """单张载体生成五连图：3840×1280 载体上方画 20:3 内容带。

    每次候选生成后，在同一张原始载体上并行执行内容带高度检查和宿主视觉质量检查；
    高度、文字、菜品问题共享 PANORAMA_QUALITY_MAX_ATTEMPTS 总预算。全部通过后才裁切、
    resize；若预算耗尽则按质量优先规则选择最佳候选并提示人工关注。

    Returns: (panorama_final_path, warnings, model_text)
    """
    materials_dir = out_dir / "materials"
    materials_dir.mkdir(parents=True, exist_ok=True)
    carrier_dir = materials_dir / f"panorama_{index:02d}"
    carrier_dir.mkdir(parents=True, exist_ok=True)
    layout_mask_path = create_panorama_layout_mask(carrier_dir / f"layout_mask_{PANORAMA_LAYOUT_MASK_CONTENT_HEIGHT}px.png")
    layout_mask_role = (
        "INTERNAL_GEOMETRY_MASK_ONLY: layout mask for panorama carrier; "
        "MUST control only top content band vs bottom pure white area geometry. "
        "MUST_NOT influence visual style, color palette, typography, food, logo, or decoration."
    )

    # B2 策略：初始补偿参数（每轮可能动态调整）
    compensated_band_h = PANORAMA_COMPENSATED_BAND_HEIGHT
    compensated_top_y = PANORAMA_COMPENSATED_TOP_Y
    compensated_bottom_y = PANORAMA_COMPENSATED_BOTTOM_Y

    warnings: list[str] = []
    model_text = ""
    candidates: list[dict[str, Any]] = []
    prompt_records: list[dict[str, Any]] = []
    next_feedback: list[str] = []
    max_attempts_total = PANORAMA_QUALITY_MAX_ATTEMPTS
    prompt_path = out_dir / "variants" / f"variant_{index:02d}" / "prompt.json"
    wall_start = time.monotonic()

    def safe_quality_check(path: Path, attempt_no: int) -> dict[str, Any]:
        try:
            return _normalize_panorama_quality_result(run_panorama_host_quality_check(path, req, attempt_no))
        except Exception as exc:
            return _normalize_panorama_quality_result({
                "status": "skipped",
                "passed": True,
                "issues": [],
                "summary": f"host visual quality check failed: {exc}",
            })

    for attempt in range(1, max_attempts_total + 1):
        if attempt > 1 and candidates:
            elapsed = time.monotonic() - wall_start
            next_candidate_budget = (
                float(image_timeout) * max(1, int(image_max_attempts))
                + _panorama_post_check_buffer_seconds()
            )
            if elapsed + next_candidate_budget > PANORAMA_WALL_TIME_BUDGET_SECONDS:
                warnings.append(
                    f"panorama_carrier: 总耗时预算 {PANORAMA_WALL_TIME_BUDGET_SECONDS}s 即将到期，"
                    "停止新增候选并按已有候选择优交付。"
                )
                break

        # B2 渐进补偿：根据上一轮实测偏差动态调整请求的内容带高度
        if attempt > 1 and candidates:
            last_band = candidates[-1].get("band", {})
            last_deviation = float(last_band.get("deviation", 0.0))
            if last_deviation > 0:
                # 上一轮偏高 → 缩小请求高度
                compensated_band_h = max(350, round(compensated_band_h * PANORAMA_RETRY_SHRINK_FACTOR))
            elif last_deviation < -PANORAMA_BAND_TOLERANCE:
                # 上一轮偏低 → 放大请求高度
                compensated_band_h = min(550, round(compensated_band_h * PANORAMA_RETRY_EXPAND_FACTOR))
            compensated_top_y = (PANORAMA_CARRIER_HEIGHT - compensated_band_h) // 2
            compensated_bottom_y = compensated_top_y + compensated_band_h
            warnings.append(
                f"panorama_carrier: 第 {attempt} 轮自适应补偿：请求 {compensated_band_h}px 内容带 "
                f"(y={compensated_top_y}~{compensated_bottom_y})"
            )

        # 每轮生成 boundary guide（参数可能随重试变化）
        boundary_guide_path = create_panorama_boundary_guide(
            carrier_dir / f"boundary_guide_attempt{attempt}.png",
            top_y=compensated_top_y,
            bottom_y=compensated_bottom_y,
            band_h=compensated_band_h,
        )
        boundary_guide_role = (
            f"LAYOUT BOUNDARY GUIDE: Two red horizontal lines mark the absolute content boundaries. "
            f"Top line at y={compensated_top_y}, bottom line at y={compensated_bottom_y}. "
            f"Content zone is only {compensated_band_h}px tall "
            f"({compensated_band_h / PANORAMA_CARRIER_HEIGHT * 100:.1f}% of canvas). "
            "Do NOT reproduce the red lines. Do NOT place any content outside these lines. "
            "The white zones above and below MUST remain pure #FFFFFF."
        )
        # 组装参考图列表：layout_mask + boundary_guide + 用户参考图
        generation_reference_images = [
            str(layout_mask_path),
            str(boundary_guide_path),
        ] + list(runtime.generation_reference_images)
        generation_reference_image_roles = [
            layout_mask_role,
            boundary_guide_role,
        ] + list(runtime.generation_reference_image_roles)

        try:
            _write_status(
                out_dir,
                "generating",
                progress={
                    "phase": "carrier_generation",
                    "current_attempt": attempt,
                    "max_attempts": max_attempts_total,
                    "compensated_band_h": compensated_band_h,
                },
            )
        except Exception:
            pass
        prompt = _build_panorama_prompt(
            req,
            template,
            layout,
            PANORAMA_CARRIER_WIDTH,
            PANORAMA_CARRIER_HEIGHT,
            quality_feedback=next_feedback or None,
            compensated_band_h=compensated_band_h,
            compensated_top_y=compensated_top_y,
            compensated_bottom_y=compensated_bottom_y,
        )
        prompt_payload = {
            "template_id": template.get("template_id"),
            "style_id": style_id_of(template),
            "layout": layout,
            "mode": "carrier_3to1_top_band_B2",
            "carrier_px": f"{PANORAMA_CARRIER_WIDTH}x{PANORAMA_CARRIER_HEIGHT}",
            "target_band_height": PANORAMA_TARGET_BAND_HEIGHT,
            "compensated_band_h": compensated_band_h,
            "band_tolerance": PANORAMA_BAND_TOLERANCE,
            "quality_budget_attempts": PANORAMA_QUALITY_MAX_ATTEMPTS,
            "layout_mask_path": str(layout_mask_path),
            "boundary_guide_path": str(boundary_guide_path),
            "generation_reference_images": generation_reference_images,
            "generation_reference_image_roles": generation_reference_image_roles,
            "attempt": attempt,
            "quality_feedback": next_feedback,
            "prompt": json.loads(prompt),
            "carrier_attempts": prompt_records,
        }
        write_json(carrier_dir / f"prompt_attempt{attempt}.json", prompt_payload)
        write_json(prompt_path, prompt_payload)

        carrier_path = carrier_dir / f"carrier_attempt{attempt}.png"
        raw_path = out_dir / "raw_responses" / f"variant_{index:02d}_carrier{attempt}.txt"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        gen_warnings, gen_text = generate_scene_image(
            prompt=prompt,
            reference_images=generation_reference_images,
            out_path=carrier_path,
            raw_response_path=raw_path,
            provider=provider,
            dry_run=dry_run,
            width=PANORAMA_CARRIER_WIDTH,
            height=PANORAMA_CARRIER_HEIGHT,
            variant=style_id_of(template),
            image_timeout=image_timeout,
            image_max_attempts=image_max_attempts,
            image_retryable_http_codes=image_retryable_http_codes,
            reference_image_roles=generation_reference_image_roles,
        )
        warnings.extend(gen_warnings)
        if gen_text:
            model_text = gen_text
        if dry_run or not carrier_path.exists():
            quality_check = _normalize_panorama_quality_result({
                "status": "skipped",
                "passed": True,
                "issues": [],
                "summary": "dry-run or carrier image unavailable",
            })
            candidate = {
                "attempt": attempt,
                "carrier_path": carrier_path,
                "band": {"top": 0, "bottom": PANORAMA_CARRIER_HEIGHT - 1, "band_height": PANORAMA_TARGET_BAND_HEIGHT, "deviation": 0.0, "abs_deviation": 0.0},
                "deviation": 0.0,
                "height_passed": True,
                "quality_check": quality_check,
                "quality_passed": True,
                "accepted": True,
            }
            candidates.append(candidate)
            prompt_records.append({k: v for k, v in candidate.items() if k != "carrier_path"} | {"carrier_path": str(carrier_path)})
            write_json(prompt_path, {**prompt_payload, "carrier_attempts": prompt_records})
            break

        with ThreadPoolExecutor(max_workers=2) as check_executor:
            band_future = check_executor.submit(detect_panorama_content_band, carrier_path)
            quality_future = check_executor.submit(safe_quality_check, carrier_path, attempt)
            band = band_future.result()
            quality_check = quality_future.result()
        dev = band["abs_deviation"]
        height_passed = dev <= PANORAMA_BAND_TOLERANCE
        quality_passed = _panorama_quality_passed(quality_check)
        accepted = height_passed and quality_passed
        print(
            f"  [panorama carrier attempt {attempt}/{max_attempts_total}] "
            f"band_h={band['band_height']}px deviation={dev * 100:+.1f}% "
            f"quality={quality_check['status']}",
            flush=True,
        )
        warnings.append(
            f"panorama_carrier: 第 {attempt} 次内容带偏差 {dev * 100:.1f}% "
            f"{'≤' if height_passed else '>'} {PANORAMA_BAND_TOLERANCE * 100:.0f}%"
        )
        if band.get("tail_trimmed"):
            warnings.append(
                "panorama_carrier: 第 "
                f"{attempt} 次尾部低信息区已修剪 {int(band.get('tail_trim_rows', 0) or 0)}px，"
                f"raw_band={band.get('raw_band_height')}px，trimmed_band={band.get('band_height')}px。"
            )
        warnings.append(
            f"panorama_carrier: 第 {attempt} 次 quality_check: {quality_check['status']}"
            + (f" ({quality_check['summary']})" if quality_check.get("summary") else "")
        )
        if quality_check.get("issues"):
            warnings.append(
                "panorama_carrier: 第 "
                f"{attempt} 次质量问题: {'；'.join(quality_check['issues'][:2])}"
            )
        candidate = {
            "attempt": attempt,
            "carrier_path": carrier_path,
            "band": band,
            "deviation": dev,
            "height_passed": height_passed,
            "quality_check": quality_check,
            "quality_passed": quality_passed,
            "accepted": accepted,
        }
        candidates.append(candidate)
        prompt_records.append({
            "attempt": attempt,
            "carrier_path": str(carrier_path),
            "band": band,
            "deviation": dev,
            "height_passed": height_passed,
            "quality_check": quality_check,
            "quality_passed": quality_passed,
            "accepted": accepted,
        })
        write_json(prompt_path, {**prompt_payload, "carrier_attempts": prompt_records})

        if accepted:
            warnings.append(
                f"panorama_carrier: 第 {attempt} 次生成通过，内容带偏差 {dev * 100:.1f}% ≤ "
                f"{PANORAMA_BAND_TOLERANCE * 100:.0f}%，质量检查 {quality_check['status']}。"
            )
            break

        next_feedback = _panorama_attempt_feedback(height_passed, band, quality_check)
        if attempt < max_attempts_total:
            warnings.append(
                f"panorama_carrier: 第 {attempt} 次未通过统一检查，下一轮反馈: "
                f"{'；'.join(next_feedback) if next_feedback else '无可注入反馈'}"
            )
        else:
            warnings.append("panorama_carrier: 质量预算已用完，将按最佳候选规则选择交付版本。")

    best = _panorama_best_candidate(candidates)
    if not best.get("accepted"):
        attention = _panorama_attempt_feedback(
            best.get("height_passed", False),
            best.get("band", {}),
            best.get("quality_check", {}),
        )
        if attention:
            warnings.append(
                "panorama_carrier: 采用未完全通过的最佳候选，请人工关注: "
                + "；".join(attention)
            )
        else:
            warnings.append("panorama_carrier: 采用最佳候选，质量检查未完全通过但无可结构化问题。")

    panorama_final = carrier_dir / "panorama_final.png"
    if dry_run:
        # dry-run：载体即 mock 图，直接强制 resize 到最终尺寸
        normalize_panorama_image(best["carrier_path"], panorama_final)
    else:
        cropped = carrier_dir / "panorama_cropped_band.png"
        crop_panorama_content_band(best["carrier_path"], cropped, best["band"])
        if best.get("band", {}).get("tail_trimmed"):
            warnings.append(
                "panorama_carrier: 最终候选使用修剪后的内容带裁切，"
                f"raw_band={best['band'].get('raw_band_height')}px，"
                f"trimmed_band={best['band'].get('band_height')}px。"
            )
        normalize_panorama_image(cropped, panorama_final)
    warnings.append(f"panorama_carrier: 已裁出完整内容带并强制 resize 至 {PANORAMA_FINAL_WIDTH}×{PANORAMA_FINAL_HEIGHT}")
    return panorama_final, warnings, model_text


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def _process_single_variant(
    index: int,
    template: dict[str, Any],
    req: dict[str, Any],
    runtime: RuntimeAssets,
    width: int,
    height: int,
    out_dir: Path,
    provider: str,
    dry_run: bool,
    qr_shared: dict[str, Any],
    image_timeout: int = DEFAULT_IMAGE_TIMEOUT,
    image_max_attempts: int = 3,
    image_retryable_http_codes: set[int] | None = None,
    verify_qr_scan: bool = True,
) -> GeneratedVariant:
    """Process a single variant: generate image, overlay logo, QR composition.

    This function is designed to be called in parallel via ThreadPoolExecutor.
    qr_shared contains pre-computed QR data shared across all variants:
      - qr_normalized_path, normalization_info, grid_info, grid_snap_info, qr_size_px
    """
    style_id = style_id_of(template)
    variant_dir = out_dir / "variants" / f"variant_{index:02d}"
    variant_dir.mkdir(parents=True, exist_ok=True)
    layout = select_layout(req, template, width, height)
    materials_dir = out_dir / "materials"

    # --- 五连图单张载体生成路径（3:1 载体 → 顶部完整内容带 → 强制 resize） ---
    if req["type"] == "五连图":
        scene_image_path, provider_warnings, model_text = _generate_panorama_carrier(
            index=index,
            template=template,
            req=req,
            runtime=runtime,
            layout=layout,
            out_dir=out_dir,
            provider=provider,
            dry_run=dry_run,
            image_timeout=image_timeout,
            image_max_attempts=image_max_attempts,
            image_retryable_http_codes=image_retryable_http_codes,
        )
        prompt_path = variant_dir / "prompt.json"
        prompt = json.dumps({"mode": "carrier_3to1_top_band", "see": str(prompt_path)}, ensure_ascii=False)
        raw_response_path = out_dir / "raw_responses" / f"variant_{index:02d}_carrier1.txt"
    else:
        # --- 常规（非五连图）生成路径 ---
        prompt = build_scene_prompt(req, template, layout, width, height)
        prompt_path = variant_dir / "prompt.json"
        write_json(
            prompt_path,
            {
                "template_id": template.get("template_id"),
                "style_id": style_id,
                "layout": layout,
                "prompt": json.loads(prompt),
            },
        )
        scene_image_path = materials_dir / f"material_{index:02d}_ai.png"
        raw_response_path = out_dir / "raw_responses" / f"variant_{index:02d}.txt"
        provider_warnings, model_text = generate_scene_image(
            prompt=prompt,
            reference_images=runtime.generation_reference_images,
            out_path=scene_image_path,
            raw_response_path=raw_response_path,
            provider=provider,
            dry_run=dry_run,
            width=width,
            height=height,
            variant=style_id,
            image_timeout=image_timeout,
            image_max_attempts=image_max_attempts,
            image_retryable_http_codes=image_retryable_http_codes,
            reference_image_roles=runtime.generation_reference_image_roles,
        )

    # Logo overlay (adaptive: standard vs white based on poster background luminance)
    if runtime.selected_logo_path and scene_image_path.exists():
        brand_profile = req.get("brand_profile", {})
        locked = bool(brand_profile.get("visual_lock"))
        overlay_brand_logo(
            scene_image_path,
            runtime.selected_logo_path,
            scene_image_path,
            white_logo_path=runtime.selected_white_logo_path,
            realism=req.get("style", {}).get("realism", "balanced"),
            logo_position=brand_profile.get("logo_position") if locked else "top-left",
            logo_size_ratio=brand_profile.get("logo_size_ratio") if locked else None,
        )
        provider_warnings.append(f"brand_logo_overlay: 已使用本地品牌素材后合成 logo（自适应明暗选择）")

    # --- QR post-composition ---
    has_qr = _has_qr_asset(req["assets"])
    final_no_qr_path: Path | None = scene_image_path
    qr_placement: dict[str, Any] | None = None
    qr_placement_path: Path | None = None

    if has_qr:
        try:
            from qr_enhance import (
                analyze_qr_grid,
                compute_qr_rect,
                detect_qr_slot,
                normalize_qr,
                snap_to_grid,
                verify_qr_decode,
            )
        except ImportError:
            analyze_qr_grid = None  # type: ignore[assignment]
            normalize_qr = None  # type: ignore[assignment]
            detect_qr_slot = None  # type: ignore[assignment]
            compute_qr_rect = None  # type: ignore[assignment]
            snap_to_grid = None  # type: ignore[assignment]
            verify_qr_decode = None  # type: ignore[assignment]
            provider_warnings.append("qr_enhance 模块不可用，回退到基础链路。")

        # Use shared QR pre-processing results
        qr_normalized_path = qr_shared.get("qr_normalized_path") or runtime.qr_path
        normalization_info = qr_shared.get("normalization_info", {})
        grid_info = qr_shared.get("grid_info", {})
        grid_snap_info = qr_shared.get("grid_snap_info", {})
        qr_size_px = qr_shared.get("qr_size_px", max(220, round(min(width, height) * 0.25)))

        # Copy normalization artifacts to variant dir for traceability
        if normalization_info:
            write_json(variant_dir / "qr_normalization.json", normalization_info)
        if qr_normalized_path and Path(str(qr_normalized_path)).exists():
            norm_dst = variant_dir / "qr_normalized.png"
            if Path(str(qr_normalized_path)).resolve() != norm_dst.resolve():
                shutil.copy2(str(qr_normalized_path), norm_dst)

        # Detect AI slot in poster
        fit_mode = "fallback_card"
        slot_info: dict[str, Any] = {}
        realism = req.get("style", {}).get("realism", "balanced")
        decision_path = "artistic_fallback" if realism == "artistic" else "fallback"
        model_hint: dict[str, Any] | None = None
        if model_text:
            model_hint = parse_qr_placement(model_text, width, height)
        if detect_qr_slot and final_no_qr_path and final_no_qr_path.exists():
            slot_kwargs = {"min_slot_size": round(qr_size_px * 0.8)}
            if realism == "artistic":
                slot_kwargs.update({"min_area_ratio": 0.015, "prefer_framed_slot": True})
            slot_info = detect_qr_slot(final_no_qr_path, **slot_kwargs)
            slot_score = slot_info.get("slot_score", 0.0)
            slot_status = slot_info.get("status", "no_slot")
            provider_warnings.append(f"qr_slot: {slot_info.get('reason', 'N/A')}，score={slot_score:.3f}")

            fit_mode, decision_path = _decide_qr_slot_fit_mode(slot_info, model_hint, width, height, realism)

        # Build placement
        if fit_mode in ("detected_slot", "soft_slot") and slot_info.get("inner_rect"):
            inner_rect = slot_info["inner_rect"]
            if compute_qr_rect:
                qr_rect = compute_qr_rect(inner_rect, qr_size_px, grid_snap_info or None)
            else:
                ix, iy, iw, ih = inner_rect["x"], inner_rect["y"], inner_rect["w"], inner_rect["h"]
                target = round(min(iw, ih) * 0.82)
                qr_rect = {"x": ix + (iw - target) // 2, "y": iy + (ih - target) // 2, "w": target, "h": target}

            qr_placement = {
                "x_px": qr_rect["x"],
                "y_px": qr_rect["y"],
                "size_px": qr_rect["w"],
                "canvas_width": slot_info.get("poster_width", width),
                "canvas_height": slot_info.get("poster_height", height),
                "fit_mode": fit_mode,
                "host_rect": slot_info.get("host_rect"),
                "inner_rect": inner_rect,
                "qr_rect": qr_rect,
                "slot_score": slot_info.get("slot_score"),
                "slot_luminance": slot_info.get("slot_luminance"),
                "border_contrast": slot_info.get("border_contrast"),
                "border_kind": slot_info.get("border_kind"),
                "border_uniformity": slot_info.get("border_uniformity"),
                "edge_strength": slot_info.get("edge_strength"),
                "candidate_area_ratio": slot_info.get("candidate_area_ratio"),
                "rotation_deg": slot_info.get("rotation_deg", 0.0),
                "scale_filter": "nearest",
                "grid_snap": grid_snap_info if grid_snap_info.get("enabled") else {"enabled": False},
                "anchor": "slot-detected",
                "decision_path": decision_path,
                "_source": "slot_detection",
            }
            if final_no_qr_path and final_no_qr_path.exists():
                alignment = _qr_alignment_check(final_no_qr_path, inner_rect, qr_rect)
                qr_placement.update(alignment)
                if alignment["alignment_status"] == "warning":
                    provider_warnings.append(f"qr_alignment: {alignment['alignment_check']}")
        else:
            # Fallback: program-scored placement
            fit_mode = "fallback_card"
            if not model_hint and model_text:
                model_hint = parse_qr_placement(model_text, width, height)

            if final_no_qr_path and final_no_qr_path.exists():
                scored = score_qr_candidates(final_no_qr_path, width, height, qr_size_px, model_hint=model_hint)
                if scored:
                    qr_placement = scored[0]
                    qr_placement["fit_mode"] = "fallback_card"
                    qr_placement["scale_filter"] = "nearest"
                    qr_placement["grid_snap"] = grid_snap_info if grid_snap_info.get("enabled") else {"enabled": False}
                    qr_placement["decision_path"] = decision_path

            if qr_placement is None:
                if model_hint:
                    model_hint["size_px"] = max(qr_size_px, int(model_hint.get("size_px", qr_size_px)))
                    qr_placement = model_hint
                    qr_placement["_source"] = "model_hint_fallback"
                    qr_placement["fit_mode"] = "fallback_card"
                    qr_placement["scale_filter"] = "nearest"
                    qr_placement["grid_snap"] = grid_snap_info if grid_snap_info.get("enabled") else {"enabled": False}
                else:
                    qr_placement = fallback_qr_placement(width, height)
                    qr_placement["size_px"] = max(qr_size_px, int(qr_placement["size_px"]))
                    qr_placement["fit_mode"] = "fallback_card"
                    qr_placement["scale_filter"] = "nearest"
                    qr_placement["grid_snap"] = grid_snap_info if grid_snap_info.get("enabled") else {"enabled": False}

        qr_placement_path = variant_dir / "qr_placement.json"
        write_json(qr_placement_path, qr_placement)

    # QR composite via subprocess
    final_path: Path | None = None
    if has_qr and final_no_qr_path and final_no_qr_path.exists() and qr_placement and runtime.qr_path:
        final_path = materials_dir / f"material_{index:02d}.png"
        qr_composite_script = Path(__file__).resolve().parent / "qr_composite.py"
        qr_for_composite = qr_normalized_path if qr_normalized_path else runtime.qr_path
        comp_cmd = [
            sys.executable, str(qr_composite_script),
            "--poster", str(final_no_qr_path),
            "--qr", str(qr_for_composite),
            "--placement", str(qr_placement_path),
            "--out", str(final_path),
        ]
        try:
            comp_result = subprocess.run(comp_cmd, text=True, capture_output=True, timeout=60, check=False)
        except Exception as exc:
            provider_warnings.append(f"qr_composite: 子进程启动异常: {exc}")
            comp_result = None

        if comp_result is not None and comp_result.returncode == 0 and final_path.exists():
            provider_warnings.append(f"qr_composite: 后合成成功 → {final_path.name}，fit_mode={fit_mode}")
            # Scan verification
            if verify_qr_scan and verify_qr_decode and runtime.qr_path:
                qr_rect_for_verify = qr_placement.get("qr_rect")
                verify_result = verify_qr_decode(runtime.qr_path, final_path, qr_rect_for_verify)
                qr_placement["scan_verified"] = verify_result.get("payload_match")
                qr_placement["scan_decoder"] = verify_result.get("decoder")
                qr_placement["scan_status"] = verify_result.get("status")
                write_json(qr_placement_path, qr_placement)
            elif not verify_qr_scan:
                provider_warnings.append("qr_verify: 多方案快速交付模式下跳过同步扫码验证，发布前请人工扫码复核。")
        else:
            detail = ""
            if comp_result is not None:
                rc = comp_result.returncode
                detail = (comp_result.stderr or comp_result.stdout or "")[:400]
                if rc == 2:
                    detail = "Pillow 未安装。请运行: pip install Pillow"
            provider_warnings.append(f"qr_composite: 后合成失败: {detail}")
            final_path = None
    elif not has_qr and scene_image_path.exists():
        final_path = materials_dir / f"material_{index:02d}.png"
        if final_path != scene_image_path:
            shutil.copy2(scene_image_path, final_path)

    actual_png = final_path if final_path and final_path.exists() else scene_image_path

    return GeneratedVariant(
        index=index,
        template=template,
        layout=layout,
        prompt=prompt,
        prompt_path=prompt_path,
        scene_image_path=scene_image_path,
        final_no_qr_path=final_no_qr_path if final_no_qr_path and final_no_qr_path.exists() else None,
        final_path=final_path if final_path and final_path.exists() else None,
        png_path=actual_png,
        raw_response_path=raw_response_path if raw_response_path.exists() else None,
        qr_placement=qr_placement,
        qr_placement_path=qr_placement_path,
        provider_warnings=provider_warnings,
    )


def _preprocess_qr_shared(req: dict[str, Any], runtime: RuntimeAssets, width: int, height: int, templates: list[dict[str, Any]], out_dir: Path) -> dict[str, Any]:
    """Pre-process QR data once, shared across all variants for efficiency."""
    has_qr = _has_qr_asset(req["assets"])
    if not has_qr or not runtime.qr_path:
        return {}

    try:
        from qr_enhance import analyze_qr_grid, normalize_qr, snap_to_grid
    except ImportError:
        return {}

    # Normalize QR (once)
    qr_normalized_path = runtime.qr_path
    normalization_info: dict[str, Any] = {}
    norm_out = out_dir / "assets" / "qr_normalized.png"
    if normalize_qr:
        normalization_info = normalize_qr(runtime.qr_path, norm_out)
        if normalization_info.get("status") == "ok":
            qr_normalized_path = Path(normalization_info["path"])

    # Analyze grid (once)
    grid_info: dict[str, Any] = {}
    if analyze_qr_grid and qr_normalized_path:
        grid_info = analyze_qr_grid(qr_normalized_path)

    # Compute target QR size
    short_side = min(width, height)
    text_blob = request_text_blob(req)
    is_conversion = any(keyword in text_blob for keyword in ("扫码", "下单", "优惠", "团购", "购买"))
    qr_fraction_of_short = 0.28 if is_conversion else 0.23
    qr_size_px = max(220, round(short_side * qr_fraction_of_short))

    # Grid-snap
    grid_snap_info: dict[str, Any] = {}
    if snap_to_grid and grid_info.get("status") == "ok":
        grid_snap_info = snap_to_grid(qr_size_px, grid_info.get("module_count"))
        if grid_snap_info.get("enabled"):
            qr_size_px = grid_snap_info["snapped_size"]

    return {
        "qr_normalized_path": qr_normalized_path,
        "normalization_info": normalization_info,
        "grid_info": grid_info,
        "grid_snap_info": grid_snap_info,
        "qr_size_px": qr_size_px,
    }


def _arg_value(args: argparse.Namespace, name: str, default: Any) -> Any:
    return getattr(args, name, default)


def _parse_variant_csv(value: Any) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _styles_for_preselected_styles(
    all_styles: list[dict[str, Any]],
    style_ids: list[str],
) -> list[dict[str, Any]]:
    by_style = _styles_by_id(all_styles)
    missing = [style_id for style_id in style_ids if style_id not in by_style]
    if missing:
        raise SkillError(
            f"--pre-selected-styles 包含未知 style_id: {missing}。"
            f"可用 styles: {list(by_style)}"
        )
    return [by_style[style_id] for style_id in style_ids]


def run(args: argparse.Namespace) -> int:
    request_path = Path(args.request).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = delivery_run_id()
    delivery_dir = out_dir / "deliverables" / run_id
    for sub in ("assets", "raw_responses", "variants", "materials"):
        (out_dir / sub).mkdir(parents=True, exist_ok=True)
    delivery_dir.mkdir(parents=True, exist_ok=True)

    raw_req = read_json(request_path)
    req, normalize_warnings = normalize_request(raw_req)
    is_panorama = req["type"] == "五连图"
    run_warnings: list[str] = []
    width, height = canvas_size(req["size"], args.dpi)
    if is_panorama:
        width, height = PANORAMA_API_WIDTH, PANORAMA_API_HEIGHT
    req, runtime = copy_runtime_assets(req, out_dir)
    write_json(out_dir / "request.normalized.json", req)

    all_templates = load_styles(req["type"])

    # Legacy variant pre-selection was removed with the v1 style migration.
    pre_selected = str(_arg_value(args, "pre_selected_variant", "")).strip()
    pre_selected_many = _parse_variant_csv(_arg_value(args, "pre_selected_variants", ""))
    if pre_selected or pre_selected_many:
        raise SkillError(
            "--pre-selected-variants 已迁移为 --pre-selected-styles；"
            f"旧值 {pre_selected_many or [pre_selected]} 不再兼容，请使用 12 个新 style_id。"
        )

    # --pre-selected-style(s): skip style selector API entirely.
    pre_selected_style = str(_arg_value(args, "pre_selected_style", "")).strip()
    pre_selected_styles = _parse_variant_csv(_arg_value(args, "pre_selected_styles", ""))
    if pre_selected_style and pre_selected_styles:
        raise SkillError("--pre-selected-style 与 --pre-selected-styles 不能同时使用")
    if has_style_reference(req):
        selection = select_styles(
            req=req,
            styles=all_templates,
            count=1,
            provider="style_reference",
            model=args.template_selector_model,
            timeout=args.template_selector_timeout,
            out_dir=out_dir,
        )
        templates = selection.templates
    elif pre_selected_style or pre_selected_styles:
        style_ids = pre_selected_styles or [pre_selected_style]
        ignored_pre_selected_styles: list[str] = []
        if is_panorama and len(style_ids) > 1:
            ignored_pre_selected_styles = style_ids[1:]
            style_ids = style_ids[:1]
            run_warnings.append(
                "五连图固定只生成 1 套方案；已忽略额外预选风格: "
                + ", ".join(ignored_pre_selected_styles)
            )
        templates = _styles_for_preselected_styles(all_templates, style_ids)
        audit = {
            "provider": "pre_selected",
            "model": args.template_selector_model,
            "pre_selected_styles": style_ids,
            "final_styles": [
                {
                    "style_id": template.get("style_id"),
                    "template_id": template.get("template_id", ""),
                    "label": template.get("label", ""),
                }
                for template in templates
            ],
            "fallback_reason": "",
        }
        if ignored_pre_selected_styles:
            audit["ignored_pre_selected_styles"] = ignored_pre_selected_styles
        write_json(out_dir / "template_selection.json", audit)
        selection = TemplateSelection(templates=templates, audit=audit)
    else:
        # Filter out excluded styles (used for parallel diversity across independent calls)
        exclude_styles = str(_arg_value(args, "exclude_styles", ""))
        exclude_set = {v.strip() for v in exclude_styles.split(",") if v.strip()}
        if exclude_set:
            filtered_templates = [t for t in all_templates if t.get("style_id") not in exclude_set]
            # Fallback: if filtering removes all styles, ignore the exclusion
            if not filtered_templates:
                filtered_templates = all_templates
        else:
            filtered_templates = all_templates
        selection = select_styles(
            req=req,
            styles=filtered_templates,
            count=1 if is_panorama else max(1, args.variants),
            provider=args.template_selector_provider,
            model=args.template_selector_model,
            timeout=args.template_selector_timeout,
            out_dir=out_dir,
        )
        templates = selection.templates
        if is_panorama and args.variants > 1:
            run_warnings.append(
                f"五连图固定只生成 1 套方案；已将 --variants {args.variants} 收敛为 1。"
            )
    global_warnings = normalize_warnings + runtime.warnings + run_warnings

    # Pre-process QR data once (shared across all variants)
    qr_shared = _preprocess_qr_shared(req, runtime, width, height, templates, out_dir)

    variants: list[GeneratedVariant] = []
    failed_variants: list[dict[str, Any]] = []
    max_workers = min(len(templates), 4)
    is_panorama = req.get("type") == "五连图"
    default_timeout = DEFAULT_IMAGE_TIMEOUT_PANORAMA if is_panorama else DEFAULT_IMAGE_TIMEOUT
    image_timeout_arg = _arg_value(args, "image_timeout", None)
    if image_timeout_arg is not None:
        image_timeout = int(image_timeout_arg)
    elif is_panorama and req.get("assets", {}).get("_model_reference_fallback_used"):
        image_timeout = FALLBACK_IMAGE_TIMEOUT_PANORAMA
        global_warnings.append(
            f"参考图预处理存在回退原图，本次五连图 image_timeout 提升至 {FALLBACK_IMAGE_TIMEOUT_PANORAMA}s。"
        )
    else:
        image_timeout = default_timeout
    secondary_deadline_seconds = int(_arg_value(args, "secondary_deadline_seconds", DEFAULT_SECONDARY_DEADLINE_SECONDS))
    fast_multi = max_workers > 1 and args.image_provider in {"api", "auto"} and not args.dry_run
    panorama_api = is_panorama and args.image_provider in {"api", "auto"} and not args.dry_run
    if panorama_api:
        image_max_attempts = 2
        image_retryable_http_codes = PANORAMA_HTTP_RETRYABLE_CODES
    elif fast_multi:
        image_max_attempts = 2
        image_retryable_http_codes = {408, 429}
    else:
        image_max_attempts = 3
        image_retryable_http_codes = None
    verify_qr_scan = not fast_multi
    disclaimer_overlay = req.get("assets", {}).get("disclaimer_overlay", True)

    if max_workers > 1 and args.image_provider in {"api", "auto"} and not args.dry_run:
        executor = ThreadPoolExecutor(max_workers=max_workers)
        futures = {
            executor.submit(
                _process_single_variant,
                index, template, req, runtime, width, height,
                out_dir, args.image_provider, args.dry_run, qr_shared,
                image_timeout, image_max_attempts, image_retryable_http_codes, verify_qr_scan,
            ): index
            for index, template in enumerate(templates, start=1)
        }
        pending = set(futures)
        first_success_at: float | None = None
        display_index = 0
        try:
            while pending:
                timeout = None
                if first_success_at is not None:
                    timeout = max(0.0, first_success_at + secondary_deadline_seconds - time.monotonic())
                done, pending = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
                if not done:
                    for future in list(pending):
                        idx = futures[future]
                        future.cancel()
                        failed_variants.append({"index": idx, "error": f"secondary deadline exceeded after {secondary_deadline_seconds}s"})
                    pending.clear()
                    break
                for future in done:
                    idx = futures[future]
                    try:
                        variant = future.result()
                        display_index += 1
                        delivery_path = publish_delivery_material(
                            variant,
                            delivery_dir,
                            display_index,
                            disclaimer=disclaimer_overlay,
                            material_type=req["type"],
                        )
                        variants.append(variant)
                        if first_success_at is None:
                            first_success_at = time.monotonic()
                        print(
                            f"[option {display_index:02d} ready] {delivery_path} "
                            f"(source_style={idx:02d}, style={style_id_of(variant.template)})",
                            flush=True,
                        )
                    except Exception as exc:
                        message = sanitize_api_text(str(exc))
                        print(f"WARNING: style option {idx} failed: {message}", file=sys.stderr)
                        failed_variants.append({"index": idx, "error": message})
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
    else:
        for index, template in enumerate(templates, start=1):
            variant = _process_single_variant(
                index, template, req, runtime, width, height,
                out_dir, args.image_provider, args.dry_run, qr_shared,
                image_timeout, image_max_attempts, image_retryable_http_codes, verify_qr_scan,
            )
            delivery_path = publish_delivery_material(
                variant,
                delivery_dir,
                index,
                disclaimer=disclaimer_overlay,
                material_type=req["type"],
            )
            variants.append(variant)
            print(
                f"[option {index:02d} ready] {delivery_path} "
                f"(source_style={index:02d}, style={style_id_of(variant.template)})",
                flush=True,
            )

    manifest = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dry_run": bool(args.dry_run),
        "rendering_mode": "ai_full_poster_post_qr",
        "delivery_run_id": run_id,
        "delivery_dir": str(delivery_dir),
        "canvas": {"width": width, "height": height, "dpi": args.dpi},
        "provider": {
            "requested": args.image_provider,
            "api_base_url": PROXY_BASE_URL,
            "model": API_MODEL,
            "image_generation_tool": True,
            "style_selector": selection.audit,
            "image_timeout_seconds": image_timeout,
            "image_max_attempts": image_max_attempts,
            "secondary_deadline_seconds": secondary_deadline_seconds,
        },
        "request": req,
        "assets": {
            "asset_dir": str(runtime.asset_dir),
            "fonts": {key: str(value) for key, value in runtime.fonts.items()},
            "selected_logo": str(runtime.selected_logo_path) if runtime.selected_logo_path else "",
            "selected_white_logo": str(runtime.selected_white_logo_path) if runtime.selected_white_logo_path else "",
            "logo_selection_mode": (
                "disabled"
                if not runtime.selected_logo_path
                else "adaptive (luminance-based: dark bg → white logo, light bg → standard logo)"
            ),
            "mascot": str(runtime.mascot_path) if runtime.mascot_path else "",
            "mascot_ref": str(runtime.mascot_ref_path) if runtime.mascot_ref_path else "",
            "food_images": [str(path) for path in runtime.food_images],
            "qr": {
                "original_path": runtime.qr_original_path,
                "asset_path": str(runtime.qr_path) if runtime.qr_path else "",
                "original_sha256": runtime.qr_original_sha256,
                "asset_sha256": runtime.qr_asset_sha256,
                "post_composition": "Pillow paste original QR onto AI poster",
                "passed_to_image_model": False,
            },
        },
        "reference_images_for_image_model": runtime.generation_reference_images,
        "reference_image_roles_for_image_model": runtime.generation_reference_image_roles,
        "failed_variants": failed_variants,
        "variants": [
            {
                "index": v.index,
                "display_index": v.display_index or v.index,
                "source_index": v.source_index or v.index,
                "template_id": v.template.get("template_id"),
                "style_id": v.template.get("style_id"),
                "strategy": v.template.get("label"),
                "layout_id": v.layout.get("id"),
                "layout_name": v.layout.get("name"),
                "prompt": str(v.prompt_path),
                "scene_image": str(v.scene_image_path),
                "final_no_qr": str(v.final_no_qr_path) if v.final_no_qr_path else "",
                "final": str(v.final_path) if v.final_path else "",
                "material": str(v.png_path) if v.png_path else "",
                "delivery_material": str(v.delivery_material_path) if v.delivery_material_path else "",
                "delivery_slices": [str(path) for path in v.delivery_slices],
                "generation_mode": "carrier_3to1_top_band" if req["type"] == "五连图" else "single",
                "completed_at": v.completed_at,
                "raw_response": str(v.raw_response_path) if v.raw_response_path else "",
                "qr_placement": v.qr_placement,
                "qr_placement_path": str(v.qr_placement_path) if v.qr_placement_path else "",
                "warnings": v.provider_warnings,
            }
            for v in variants
        ],
        "warnings": global_warnings,
    }
    write_json(out_dir / "manifest.json", manifest)
    write_review(out_dir / "review.md", manifest)
    write_html_index(out_dir / "index.html", variants)
    if failed_variants:
        print(f"WARNING: {len(failed_variants)}/{len(failed_variants) + len(variants)} 个方案失败", file=sys.stderr)
        if not variants:
            raise SkillError(f"所有 {len(failed_variants)} 个方案均生成失败")
    print(f"Generated {len(variants)} AI-led material option(s): {out_dir}")

    # ---- dx-push notification (同步与异步模式下均自动执行) ----
    # 收集 deliverables 目录中的交付文件，发送大象通知；结果写入 status.json 供外部查询。
    dx_files = _collect_deliverable_pngs(out_dir, run_id=run_id)
    if not args.dry_run and args.image_provider != "none":
        # 用成功变体记录的来源标记判断是否真的走了代理（比直接看入参更准确，能反映运行时回退）。
        proxy_api_used = _proxy_api_actually_used(variants)
        # 大象推送仅发送原图（长图），不发送切片图；切片仍保留在 status.json.files 供交付。
        dx_push_files = [f for f in dx_files if "_slices/" not in f and "_slices\\" not in f]
        _write_status(
            out_dir,
            "done",
            files=dx_files,
            exit_code=0,
            dx_push={"status": "pending", "expected_images": len(dx_push_files)},
        )
        dx_push_result = _send_dx_push_notification(out_dir, dx_push_files, proxy_api_used)
        if dx_push_result.get("status") != "sent":
            print(f"WARNING: dx-push notification failed: {dx_push_result}", file=sys.stderr, flush=True)
        _write_status(out_dir, "done", files=dx_files, exit_code=0, dx_push=dx_push_result)
    else:
        # dry-run 或 image-provider=none 时不推送，仅写 status
        _write_status(out_dir, "done", files=dx_files, exit_code=0, dx_push={"status": "skipped", "reason": "dry-run or image-provider=none"})

    return 0


# ---------------------------------------------------------------------------
# Review / HTML output
# ---------------------------------------------------------------------------

def write_review(path: Path, manifest: dict[str, Any]) -> None:
    req = manifest["request"]
    selected_logo, _, logo_label, logo_reason = select_logo_asset(req)
    qr = manifest["assets"]["qr"]
    logo_strategy = f"- Logo 策略: {logo_label}（{logo_reason}）"
    if selected_logo:
        logo_strategy += "；自适应明暗选择，已使用本地品牌素材后合成"
    else:
        logo_strategy += "；本次未后合成平台 Logo"
    lines = [
        "# 餐饮营销物料审核报告",
        "",
        f"- 物料类型: {req['type']}",
        f"- 画布: {manifest['canvas']['width']}x{manifest['canvas']['height']} px @ {manifest['canvas']['dpi']} DPI",
        "- 生成模式: Responses API image_generation 生成完整海报成稿，本地平台 logo（如展示）与真实 QR 后合成",
        f"- 风格: {req['style']['name']}",
        logo_strategy,
        f"- 吉祥物模式: {req['assets'].get('mascot_mode')}",
        "",
        "## 二维码完整性",
        "",
    ]
    if qr["asset_path"]:
        lines += [
            f"- 原始 QR: `{qr['original_path']}`",
            f"- 输出 QR: `{qr['asset_path']}`",
            f"- 原始 SHA-256: `{qr['original_sha256']}`",
            f"- 输出 SHA-256: `{qr['asset_sha256']}`",
            "- 后合成: QR 原图不传入图像模型，Pillow 只做 NEAREST 等比缩放和粘贴。",
            "- 发布前仍需用最终 PNG 扫码验证。",
        ]
    else:
        lines.append("- 用户明确不需要二维码，本次未渲染 QR。")
    lines += ["", "## 全局提醒"]
    if manifest.get("warnings"):
        lines += [f"- {warning}" for warning in manifest["warnings"]]
    lines += [
        "- P0 检查: 二维码可扫、标题无错别字、logo/品牌呈现正确、没有虚构价格/门店/规则。",
    ]
    for variant in manifest["variants"]:
        display_index = variant.get("display_index") or variant.get("index")
        source_index = variant.get("source_index") or variant.get("index")
        delivery_material = variant.get("delivery_material") or variant.get("final") or variant.get("material") or "无"
        lines += [
            "",
            f"## 方案 {display_index:02d} · {variant['strategy']}",
            "",
            f"- 源生成序号: `{source_index:02d}`",
            f"- 模板: `{variant['template_id']}`",
            f"- 布局: `{variant['layout_id']}` / {variant['layout_name']}",
            f"- AI 海报: `{variant['scene_image']}`",
            f"- 稳定交付图: `{delivery_material}`",
        ]
        qp = variant.get("qr_placement")
        if qp:
            fm = qp.get("fit_mode", "fallback_card")
            fm_label = {"detected_slot": "空槽直贴", "soft_slot": "半透明衬底", "fallback_card": "白色卡片托盘"}.get(fm, fm)
            lines += [
                "",
                "### QR 后合成",
                "",
                f"- 适配模式: **{fm_label}** (`{fm}`)",
            ]
            qr_rect = qp.get("qr_rect")
            if qr_rect:
                lines.append(f"- QR 区域: x={qr_rect.get('x')}px y={qr_rect.get('y')}px {qr_rect.get('w')}×{qr_rect.get('h')}px")
            else:
                lines.append(f"- 位置: anchor=`{qp.get('anchor')}` x={qp.get('x_px')}px y={qp.get('y_px')}px size={qp.get('size_px')}px")
            gs = qp.get("grid_snap", {})
            if gs.get("enabled"):
                lines.append(f"- 网格对齐: modules={gs.get('qr_body_modules')} ppm={gs.get('pixels_per_module')}")
            scan_status = qp.get("scan_status")
            if scan_status == "ok":
                scan_icon = "✓" if qp.get("scan_verified") else "✗"
                lines.append(f"- 扫码验证: {scan_icon} ({qp.get('scan_decoder', '')})")
            lines += [
                "",
                "**后合成命令：**",
                "```bash",
                f"python3 {SKILL_ROOT}/scripts/qr_composite.py \\",
                f"  --poster {variant.get('scene_image', '<ai_poster.png>')} \\",
                f"  --qr {qr['asset_path'] or '<qr_code_path>'} \\",
                f"  --placement {variant.get('qr_placement_path', '')} \\",
                f"  --fit-mode {fm} \\",
                f"  --out {variant.get('final') or '<out.png>'}",
                "```",
            ]
        lines.append("- 人工检查项: AI 标题准确性、QR 是否可扫、二维码是否够大、主体是否被遮挡。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_html_index(path: Path, variants: list[GeneratedVariant]) -> None:
    cards = []
    for variant in variants:
        display_index = variant.display_index or variant.index
        source_index = variant.source_index or variant.index
        title = f"方案 {display_index:02d} · {variant.layout.get('name', '')}"
        best_png = variant.delivery_material_path or variant.final_path or variant.png_path
        if best_png:
            rel_png = rel(path.parent, best_png)
            preview = f'<img src="{esc(rel_png)}" alt="{esc(title)}">'
            badge = "稳定交付"
        else:
            preview = '<p style="color:#999">无可预览产物</p>'
            badge = "无产物"
        link_items = []
        if variant.delivery_material_path:
            link_items.append(f'<a href="{esc(rel(path.parent, variant.delivery_material_path))}">下载稳定交付图</a>')
        if variant.final_path:
            link_items.append(f'<a href="{esc(rel(path.parent, variant.final_path))}">下载 final.png</a>')
        if variant.png_path and variant.png_path != variant.final_path:
            link_items.append(f'<a href="{esc(rel(path.parent, variant.png_path))}">AI海报</a>')
        links = " · ".join(link_items) or "无可下载产物"
        cards.append(
            '<section class="card">'
            f"<h2>{esc(title)} <small>({badge})</small></h2>"
            f"{preview}"
            f"<p>源生成序号：{source_index:02d}</p>"
            f"<p>{links}</p>"
            "</section>"
        )
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>餐饮营销物料预览</title>
  <style>
    body{{margin:0;background:#f4f4f4;color:#111;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}}
    header{{padding:28px 32px;background:#111;color:#FFD100}}
    main{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:24px;padding:24px}}
    .card{{background:white;border-radius:10px;padding:18px;box-shadow:0 8px 28px rgba(0,0,0,.08)}}
    h1{{margin:0;font-size:24px}} h2{{font-size:16px;margin:0 0 12px}}
    img{{width:100%;aspect-ratio:3/4;border:1px solid #eee;background:#fff}}
    a{{color:#111;font-weight:700}}
  </style>
</head>
<body>
  <header><h1>餐饮营销物料预览</h1></header>
  <main>{''.join(cards)}</main>
</body>
</html>
"""
    path.write_text(html, encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run_recommend_atmospheres(args: argparse.Namespace) -> int:
    """Deprecated alias for --recommend-styles."""
    req_path = Path(args.request).expanduser()
    if not req_path.exists():
        raise SkillError(f"--request 文件不存在: {req_path}")
    req = json.loads(req_path.read_text(encoding="utf-8"))
    req, _ = normalize_request(req)
    styles = load_styles(req.get("type", "营销海报"))
    recommendations = recommend_styles(req, styles, top_n=args.recommend_atmospheres)
    print(json.dumps(recommendations, ensure_ascii=False, indent=2))
    return 0


def _run_recommend_styles(args: argparse.Namespace) -> int:
    """Output Top N design style recommendations as JSON and exit."""
    req_path = Path(args.request).expanduser()
    if not req_path.exists():
        raise SkillError(f"--request 文件不存在: {req_path}")
    req = json.loads(req_path.read_text(encoding="utf-8"))
    req, _ = normalize_request(req)
    styles = load_styles(req.get("type", "营销海报"))
    recommendations = recommend_styles(req, styles, top_n=args.recommend_styles)
    print(json.dumps(recommendations, ensure_ascii=False, indent=2))
    return 0


def recommend_copy_dimensions(req: dict[str, Any]) -> dict[str, Any]:
    """Return recommended copy dimensions and example templates based on store category."""
    copy_dims_path = SKILL_ROOT / "references" / "copy_dimensions.json"
    if not copy_dims_path.exists():
        return {"preset_dimensions": [], "all_dimensions": []}
    data = json.loads(copy_dims_path.read_text(encoding="utf-8"))

    cuisine_tag = (req.get("style") or {}).get("cuisine_tag", "")
    store_category = (req.get("store") or {}).get("category", "")
    category_key = cuisine_tag or store_category

    presets = data.get("category_dimension_presets", {})
    preset_dims = presets.get(category_key, [])
    if not preset_dims:
        for key, dims in presets.items():
            if key in category_key or category_key in key:
                preset_dims = dims
                break
    if not preset_dims:
        preset_dims = ["营销促销", "消费场景召唤"]

    all_dims = []
    for dim in data.get("dimensions", []):
        all_dims.append({
            "id": dim["id"],
            "description": dim["description"],
            "examples": dim["examples"][:3],
        })

    return {
        "preset_dimensions": preset_dims,
        "all_dimensions": all_dims,
        "generation_rules": data.get("copy_generation_rules", {}),
    }


def _run_recommend_copy(args: argparse.Namespace) -> int:
    """Output copy dimension recommendations as JSON and exit."""
    req_path = Path(args.request).expanduser()
    if not req_path.exists():
        raise SkillError(f"--request 文件不存在: {req_path}")
    req = json.loads(req_path.read_text(encoding="utf-8"))
    result = recommend_copy_dimensions(req)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _write_status(out_dir: Path, status: str, **kwargs: Any) -> Path:
    """Write/update status.json in out_dir for async polling (merge mode)."""
    status_path = out_dir / "status.json"
    data: dict[str, Any] = {}
    if status_path.exists():
        try:
            data = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    data["status"] = status
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    data.update(kwargs)
    status_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return status_path


def _write_status_fresh(out_dir: Path, status: str, **kwargs: Any) -> Path:
    """Write status.json from scratch (overwrite mode) — used at async start to clear stale data."""
    status_path = out_dir / "status.json"
    data: dict[str, Any] = {"status": status, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    data.update(kwargs)
    status_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return status_path


def _dx_caller_mis(req: dict[str, Any] | None = None) -> str:
    # 优先级：request.json 的 caller_mis（主路径，每次请求独立、可复盘）
    #   → 环境变量 RESTAURANT_DX_CALLER_MIS（应急/测试 override）
    #   → SANDBOX_MIS → CATPAW_CONFIG_CONTENT.misId（平台注入）
    candidates = [
        normalize_space((req or {}).get("caller_mis", "")),
        normalize_space(os.environ.get("RESTAURANT_DX_CALLER_MIS", "")),
        normalize_space(os.environ.get("SANDBOX_MIS", "")),
    ]
    config = os.environ.get("CATPAW_CONFIG_CONTENT", "")
    if config:
        try:
            candidates.append(normalize_space(json.loads(config).get("misId", "")))
        except Exception:
            pass
    for raw in candidates:
        if not raw:
            continue
        # 合法 mis 号（字母开头的字母数字串）直接返回。
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", raw):
            return raw
        # 纯数字 uid 不丢弃，保留可追溯信息（如 uid:1303903）。
        if raw.isdigit():
            return f"uid:{raw}"
    return "unknown"


def _format_user_original_prompt(value: Any) -> str:
    """把 request.json 的 user_original_prompt 规整为带序号的多行文本。

    支持两种写法：
    - 字符串：原始输入（+澄清）已由 Agent 拼好，原样返回（已带序号则不重复加）。
    - 字符串列表：第 1 条为触发 Skill 的原始输入，其余为各轮澄清回答，逐条编号。
    """
    if isinstance(value, (list, tuple)):
        lines = [normalize_space(str(item)) for item in value if normalize_space(str(item))]
        if not lines:
            return ""
        return "\n".join(f"{i}. {line}" for i, line in enumerate(lines, start=1))
    # 字符串：按行规整（保留 Agent 拼好的换行），逐行去多余空白后丢弃空行。
    raw = "" if value is None else str(value)
    lines = [normalize_space(line) for line in raw.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    numbered_re = re.compile(r"^\d+[.、]\s*")
    if all(numbered_re.match(line) for line in lines):
        return "\n".join(lines)
    stripped = [numbered_re.sub("", line, count=1) for line in lines]
    return "\n".join(f"{i}. {line}" for i, line in enumerate(stripped, start=1))


def _dx_user_prompt_summary(req: dict[str, Any]) -> str:
    # 优先级：request.json 的 user_original_prompt（主路径，每次请求独立、可复盘落盘）
    #   → 环境变量 RESTAURANT_DX_USER_PROMPTS（应急/测试 override）
    #   → 结构化字段拼接（兜底，原始输入缺失时的近似还原）
    original = _format_user_original_prompt(req.get("user_original_prompt"))
    if original:
        return original
    override = normalize_space(os.environ.get("RESTAURANT_DX_USER_PROMPTS", ""))
    if override:
        return override
    parts = [
        req.get("title", ""),
        req.get("store", {}).get("name", ""),
        req.get("store", {}).get("category", ""),
        req.get("campaign", {}).get("theme", ""),
        req.get("campaign", {}).get("offer", ""),
        req.get("campaign", {}).get("cta", ""),
        req.get("copy", {}).get("selected_text", ""),
    ]
    summary = "；".join(dict.fromkeys(normalize_space(part) for part in parts if normalize_space(part)))
    return f"1. {summary or '未获取到用户 Prompt'}"


def _dx_push_endpoint() -> str:
    return PROXY_BASE_URL.rstrip("/") + DX_PUSH_ENDPOINT_PATH


def _send_dx_push_notification(
    out_dir: Path,
    files: list[str],
    proxy_api_used: bool,
    timeout_seconds: int = PANORAMA_DX_PUSH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Send generation record and all deliverable images to the dx-push proxy.

    Notification failures are returned as status metadata so async generation can
    still complete while exposing the operational failure in status.json.
    """
    if not files:
        return {"status": "skipped", "reason": "no delivery files", "expected_images": 0}
    if os.environ.get("RESTAURANT_DX_PUSH_DISABLED") == "1":
        return {"status": "skipped", "reason": "RESTAURANT_DX_PUSH_DISABLED=1", "expected_images": len(files)}

    normalized_request_path = out_dir / "request.normalized.json"
    try:
        req = json.loads(normalized_request_path.read_text(encoding="utf-8"))
    except Exception:
        req = {}

    image_base64_list: list[str] = []
    for file_path in files:
        path = Path(file_path)
        if not path.exists():
            return {"status": "failed", "reason": f"delivery image missing: {path}", "expected_images": len(files)}
        image_base64_list.append(base64.b64encode(path.read_bytes()).decode("ascii"))
    if len(image_base64_list) != len(files):
        return {
            "status": "failed",
            "reason": "len(image_base64_list) == len(status.json.files) check failed",
            "expected_images": len(files),
            "encoded_images": len(image_base64_list),
        }

    payload = {
        "text": (
            "【生图记录】\n"
            f"调用者：{_dx_caller_mis(req)}\n"
            f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"生图代理API：{'是' if proxy_api_used else '否（实际：非代理或 dry-run）'}\n"
            "用户Prompt：\n"
            f"{_dx_user_prompt_summary(req)}"
        ),
        "image_base64_list": image_base64_list,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        _dx_push_endpoint(),
        data=data,
        headers={
            "Authorization": f"Bearer {PROXY_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except Exception as exc:
        return {"status": "failed", "reason": str(exc), "expected_images": len(files)}

    try:
        response_json = json.loads(raw)
    except json.JSONDecodeError:
        return {"status": "failed", "reason": "dx-push returned non-JSON response", "raw_response": raw[:500], "expected_images": len(files)}

    results = response_json.get("results") or []
    text_ok = any(item.get("type") == "text" and item.get("success") is True for item in results)
    image_ok_count = sum(1 for item in results if item.get("type") == "image" and item.get("success") is True)
    if not text_ok or image_ok_count != len(files):
        return {
            "status": "failed",
            "reason": "dx-push result count check failed",
            "text_success": text_ok,
            "expected_images": len(files),
            "image_success": image_ok_count,
            "response": response_json,
        }

    return {
        "status": "sent",
        "text_success": True,
        "expected_images": len(files),
        "image_success": image_ok_count,
    }


def _async_worker(argv: list[str], out_dir: Path) -> None:
    """Background worker: run the generation and update status.json on completion.

    Note: dx-push notification is now handled inside run() directly, so the worker
    only needs to handle failure cases and ensure status.json reflects the final state.
    """
    try:
        # Re-parse args without --async to run synchronously in this process
        sync_argv = [arg for arg in argv if arg != "--async"]
        exit_code = main(sync_argv)
        if exit_code != 0:
            _write_status(out_dir, "failed", exit_code=exit_code, error=f"Process exited with code {exit_code}")
        # exit_code == 0: run() already wrote status.json with dx_push result
    except Exception as exc:
        _write_status(out_dir, "failed", exit_code=2, error=str(exc))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate AI-led restaurant marketing material PNG drafts.")
    parser.add_argument("--request", required=True, help="Path to material_request.json")
    parser.add_argument("--out", required=True, help="Output directory")
    parser.add_argument("--variants", type=int, default=DEFAULT_VARIANTS, help="Number of variants to generate")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI, help="DPI used for custom_mm conversion")
    parser.add_argument("--dry-run", action="store_true", help="Do not call image model; generate mock backgrounds")
    parser.add_argument("--image-provider", choices=["auto", "api", "none"], default="auto",
                        help="auto/api use Responses API with image_generation; none writes mock backgrounds")
    parser.add_argument("--template-selector-provider", choices=["auto", "api", "none"], default="auto",
                        help="auto tries AI selector then falls back; api requires selector API; none uses local fallback")
    parser.add_argument("--template-selector-model", default=API_MODEL, help="Model used for pure-text template selection")
    parser.add_argument("--template-selector-timeout", type=int, default=15, help="Template selector API timeout in seconds (reduced from 30s for faster fallback)")
    parser.add_argument("--exclude-variants", default="",
                        help="Deprecated; use --exclude-styles with style_id values")
    parser.add_argument("--pre-selected-variant", default="",
                        help="Deprecated; use --pre-selected-style")
    parser.add_argument("--pre-selected-variants", default="",
                        help="Deprecated; use --pre-selected-styles")
    parser.add_argument("--exclude-styles", default="",
                        help="Comma-separated style_id values to exclude from selection (for parallel diversity)")
    parser.add_argument("--pre-selected-style", default="",
                        help="Directly use this style_id, skipping style selector API entirely.")
    parser.add_argument("--pre-selected-styles", default="",
                        help="Comma-separated style_id values to generate in one process, skipping style selector API.")
    parser.add_argument("--image-timeout", type=int, default=None,
                        help="Per image Responses API timeout in seconds (default: 420 normal, 660 panorama)")
    parser.add_argument("--secondary-deadline-seconds", type=int, default=DEFAULT_SECONDARY_DEADLINE_SECONDS,
                        help="After first option is ready, wait this many seconds for remaining variants before marking them failed")
    parser.add_argument("--recommend-atmospheres", type=int, default=0, metavar="N",
                        help="Deprecated alias: output Top N design style recommendations as JSON and exit")
    parser.add_argument("--recommend-styles", type=int, default=0, metavar="N",
                        help="Output Top N design style recommendations as JSON and exit (no generation)")
    parser.add_argument("--recommend-copy", action="store_true",
                        help="Output copy dimension recommendations as JSON and exit (no generation)")
    parser.add_argument("--async", dest="async_mode", action="store_true",
                        help="Async mode: fork background worker, write status.json, return immediately")
    args = parser.parse_args(argv)

    # --async mode: fork worker and return immediately
    if args.async_mode:
        out_dir = Path(args.out).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        # Reset status.json completely for new run — clear stale fields (files, dx_push, etc.)
        # from previous runs to prevent Agent from reading cached results.
        _write_status_fresh(out_dir, "generating", started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        # Build argv list for the worker (original argv without --async)
        worker_argv = [a for a in (argv if argv is not None else sys.argv[1:]) if a != "--async"]
        pid = os.fork()
        if pid == 0:
            # Child process: detach from parent, run generation
            os.setsid()
            # Redirect stdout/stderr to log file
            log_path = out_dir / "worker.log"
            log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            os.dup2(log_fd, 1)
            os.dup2(log_fd, 2)
            os.close(log_fd)
            _async_worker(worker_argv, out_dir)
            os._exit(0)
        else:
            # Parent process: print status and exit immediately
            status_path = out_dir / "status.json"
            print(json.dumps({
                "status": "generating",
                "poll_file": str(status_path),
                "worker_pid": pid,
                "message": "Image generation started in background. Poll status.json for progress.",
            }, ensure_ascii=False))
            return 0

    if args.recommend_styles > 0:
        return _run_recommend_styles(args)
    if args.recommend_atmospheres > 0:
        return _run_recommend_atmospheres(args)
    if args.recommend_copy:
        return _run_recommend_copy(args)
    if args.variants < 1:
        raise SkillError("--variants 必须大于 0")
    if args.template_selector_timeout < 1:
        raise SkillError("--template-selector-timeout 必须大于 0")
    if args.image_timeout is not None and args.image_timeout < 1:
        raise SkillError("--image-timeout 必须大于 0")
    if args.secondary_deadline_seconds < 1:
        raise SkillError("--secondary-deadline-seconds 必须大于 0")
    try:
        return run(args)
    except SkillError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
