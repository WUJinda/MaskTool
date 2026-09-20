# -*- coding: utf-8 -*-
"""UI 层共享标签与常量：类型/状态/来源文案、运行模式说明、支持扩展名。

最底层模块：不依赖 ui 内其他模块。
"""
from typing import Dict

from mask_tool.models.detection import (
    DetectionStatus,
    DetectionType,
)

# I1a 契约：上传白名单/屏蔽后缀常量由 core/formats.py 提供；
# 并行改造未就位时降级为本地等价常量（集成点：core/formats.py 落地后自动切换）。
try:
    from mask_tool.core.formats import SUPPORTED_MASK_EXTS, BLOCKED_EXTS
except ImportError:  # pragma: no cover - I1a 未就位时的过渡分支
    SUPPORTED_MASK_EXTS = {".docx", ".xlsx"}
    BLOCKED_EXTS = {".pptx", ".pdf"}

SUPPORTED_EXTENSIONS = SUPPORTED_MASK_EXTS

TYPE_LABELS: Dict[DetectionType, str] = {
    DetectionType.COMPANY: "🏢 公司/机构",
    DetectionType.GOVERNMENT: "🏛️ 政府",
    DetectionType.PERSON: "👤 人名",
    DetectionType.PROJECT: "📋 项目",
    DetectionType.SUBJECT: "📌 主题",
    DetectionType.LOCATION: "📍 地名",
    DetectionType.AMOUNT: "💰 金额",
    DetectionType.CUSTOM: "🏷️ 自定义",
}

STATUS_LABELS: Dict[DetectionStatus, str] = {
    DetectionStatus.AUTO_MASK: "✅ 自动脱敏",
    DetectionStatus.SUGGEST_MASK: "⚠️ 建议脱敏",
    DetectionStatus.HINT_ONLY: "ℹ️ 仅提示",
}

SOURCE_LABELS: Dict[str, str] = {
    "manual": "✍️ 手动",
    "dictionary": "📘 词典",
    "ner": "🤖 NER",
    "regex": "🔍 正则",
    "path": "📄 文件名",
}

MODE_DESCRIPTIONS = {
    "focused": "精准模式：仅自动脱敏词典匹配项（置信度≥0.95），其余仅提示",
    "strict": "严格模式：高置信度自动脱敏，中置信度建议脱敏",
    "smart": "智能模式：平衡自动脱敏与建议脱敏（推荐）",
    "aggressive": "激进模式：尽可能多地脱敏，适合AI前处理",
}
