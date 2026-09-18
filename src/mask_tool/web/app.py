"""Streamlit Web 界面 - mask-tool 可视化脱敏工具"""

import json
import os
import random
import re
import shutil
import string
import tempfile
import time
import zipfile
from dataclasses import dataclass, field, asdict
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import streamlit as st
import pandas as pd
import yaml

from mask_tool import __version__
from mask_tool.models.config import MaskConfig
from mask_tool.models.detection import (
    DetectionResult, DetectionStatus, DetectionType, Location,
)
from mask_tool.core.pipeline import Pipeline
from mask_tool.core.path_masker import PathMasker

# I1a 契约：上传白名单/屏蔽后缀常量由 core/formats.py 提供；
# 并行改造未就位时降级为本地等价常量（集成点：core/formats.py 落地后自动切换）。
try:
    from mask_tool.core.formats import SUPPORTED_MASK_EXTS, BLOCKED_EXTS
except ImportError:  # pragma: no cover - I1a 未就位时的过渡分支
    SUPPORTED_MASK_EXTS = {".docx", ".xlsx"}
    BLOCKED_EXTS = {".pptx", ".pdf"}

# ──────────────────────────────────────────────
# 常量与映射
# ──────────────────────────────────────────────

# 脱敏侧仅支持 Word/Excel；PPT/PDF 已暂时停用（屏蔽策略）
SUPPORTED_EXTENSIONS = SUPPORTED_MASK_EXTS

# 批次目录：脱敏输出与 mapping.json 的持久化位置（~/.mask-tool/batches/<batch_id>/）
BATCHES_DIR = Path.home() / ".mask-tool" / "batches"

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

HISTORY_PATH = Path.home() / ".mask-tool" / "history.json"
MAX_HISTORY = 50

# 目录 zip 上传防护（I6 问题2）：成员数与解压总大小上限（zip 炸弹防御）
ZIP_MAX_ENTRIES = 500
ZIP_MAX_TOTAL_BYTES = 500 * 1024 * 1024  # 500MB


def _safe_unzip(zip_bytes: bytes, dest: Path) -> Path:
    """安全解压目录 zip 到 dest/tree，返回解压根目录。

    防护（恶意/意外 zip 拒绝，抛 ValueError 由调用万 st.error）：
    - 路径穿越：成员路径 normpath 后必须仍在解压根内
      （拒绝 ../、绝对路径、盘符等逃逸形态）
    - zip 炸弹：成员数 ≤ 500、声明解压总大小 ≤ 500MB
    """
    root = dest / "tree"
    with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
        infos = zf.infolist()
        if len(infos) > ZIP_MAX_ENTRIES:
            raise ValueError(
                f"压缩包成员数 {len(infos)} 超过上限 {ZIP_MAX_ENTRIES}，已拒绝解压"
            )
        total = sum(i.file_size for i in infos)
        if total > ZIP_MAX_TOTAL_BYTES:
            raise ValueError(
                f"压缩包声明解压总大小 {total / 1024 / 1024:.1f}MB 超过上限 "
                f"{ZIP_MAX_TOTAL_BYTES // 1024 // 1024}MB，已拒绝解压"
            )
        root.mkdir(parents=True, exist_ok=True)
        base = root.resolve()
        for info in infos:
            # 归一化后必须仍在解压根内（拒绝 ../ 与绝对路径穿越）
            member = Path(os.path.normpath(str(root / info.filename)))
            try:
                member.relative_to(base)
            except ValueError:
                raise ValueError(
                    f"压缩包成员路径越界（疑似路径穿越），已拒绝解压: {info.filename!r}"
                )
            if info.is_dir():
                member.mkdir(parents=True, exist_ok=True)
                continue
            member.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(member, "wb") as dst:
                shutil.copyfileobj(src, dst)
    return root


def _classify_tree_files(tree_root: Path) -> Dict[str, List[Path]]:
    """遍历解压根，按处理面分类：docs（docx/xlsx 待脱敏）、
    blocked（pptx/pdf 等屏蔽类型，警告且不进产物）、others（其余原样拷入）。"""
    docs: List[Path] = []
    blocked: List[Path] = []
    others: List[Path] = []
    for p in sorted(tree_root.rglob("*")):
        if not p.is_file():
            continue
        suffix = p.suffix.lower()
        if suffix in SUPPORTED_MASK_EXTS:
            docs.append(p)
        elif suffix in BLOCKED_EXTS:
            blocked.append(p)
        else:
            others.append(p)
    return {"docs": docs, "blocked": blocked, "others": others}


# ──────────────────────────────────────────────
# 页面配置
# ──────────────────────────────────────────────

st.set_page_config(
    page_title="mask-tool 文件脱敏工具",
    page_icon="🔒",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ──────────────────────────────────────────────
# 自定义 CSS
# ──────────────────────────────────────────────

def _inject_css():
    st.markdown("""
    <style>
    /* 整体 */
    .main .block-container { padding-top: 2rem; }
    stApp { background: #f8f9fb; }

    /* ── 侧边栏：紧凑化（2.3 UI 重构）── */
    [data-testid="stSidebar"] {
        background: linear-gradient(180deg, #191a2e 0%, #151a33 100%);
        padding: 0.35rem 0.85rem 1rem;
    }
    [data-testid="stSidebar"] * { color: #dfe2ee !important; }
    /* 收紧侧栏内垂直间距（覆盖 Streamlit 内联 gap） */
    [data-testid="stSidebar"] [data-testid="stVerticalBlock"] {
        gap: 0.5rem !important;
    }
    /* 紧凑头部：logo + 名称 + 版本同一行 */
    .side-head { display: flex; align-items: baseline; gap: 0.45rem; margin: 0.1rem 0 0.15rem; }
    .side-head .logo { font-size: 1rem; }
    .side-head .name { font-size: 1rem; font-weight: 800; color: #fff !important; letter-spacing: 0.01em; }
    .side-head .ver { font-size: 0.68rem; color: rgba(226,229,240,.55) !important; }
    /* 小节标签：代替大号 st.subheader */
    .side-label {
        font-size: 0.72rem; font-weight: 700; letter-spacing: 0.08em;
        color: rgba(226,229,240,.55) !important;
        display: flex; align-items: center; gap: 0.4rem;
        margin: 0.45rem 0 0.05rem;
    }
    .side-label::after { content: ""; flex: 1; height: 1px; background: rgba(255,255,255,.08); }
    /* 词库徽章行：代替大号 st.metric */
    .lex-chip {
        display: flex; align-items: center; justify-content: space-between;
        background: rgba(255,255,255,.07); border: 1px solid rgba(255,255,255,.1);
        border-radius: 7px; padding: 0.32rem 0.6rem; margin-bottom: 0.15rem;
        font-size: 0.8rem;
    }
    .lex-chip b { font-size: 0.8rem; font-weight: 600; }
    .lex-chip .cnt { font-size: 0.8rem; font-weight: 800; color: #9fb0ff !important; }
    /* 侧栏按钮：品牌渐变 + 白色加粗文字（更清晰） */
    [data-testid="stSidebar"] .stButton > button {
        background: linear-gradient(135deg, #5b6ee8 0%, #764ba2 100%) !important;
        border: none !important;
        color: #ffffff !important;
        font-weight: 700 !important;
        font-size: 0.85rem !important;
        height: 2.1rem !important;
        padding: 0 0.6rem !important;
        border-radius: 7px !important;
        box-shadow: 0 2px 8px rgba(91,110,232,.35);
    }
    [data-testid="stSidebar"] .stButton > button:hover { filter: brightness(1.1); }
    /* 侧栏控件字号与行高 */
    [data-testid="stSidebar"] p,
    [data-testid="stSidebar"] label,
    [data-testid="stSidebar"] span { font-size: 0.82rem; }
    [data-testid="stSidebar"] small { font-size: 0.72rem !important; line-height: 1.45; }
    [data-testid="stSidebar"] input[type="checkbox"] { accent-color: #5b6ee8; }
    /* 侧栏折叠区紧凑 */
    [data-testid="stSidebar"] [data-testid="stExpanderDetails"] { padding-top: 0.2rem !important; }
    [data-testid="stSidebar"] details summary { padding: 0.3rem 0 !important; font-size: 0.82rem; }

    /* 指标卡片 */
    .metric-card {
        background: white;
        border-radius: 12px;
        padding: 1rem 1.5rem;
        box-shadow: 0 1px 3px rgba(0,0,0,0.08);
        text-align: center;
    }
    .metric-card .value {
        font-size: 2rem;
        font-weight: 700;
        color: #1a1a2e;
    }
    .metric-card .label {
        font-size: 0.85rem;
        color: #666;
        margin-top: 0.25rem;
    }

    /* 检测结果表格 */
    .detection-table {
        width: 100%;
        border-collapse: separate;
        border-spacing: 0;
        font-size: 0.9rem;
    }
    .detection-table th {
        background: #1a1a2e;
        color: white;
        padding: 0.6rem 0.8rem;
        text-align: left;
        font-weight: 600;
        position: sticky;
        top: 0;
    }
    .detection-table th:first-child { border-radius: 8px 0 0 0; }
    .detection-table th:last-child { border-radius: 0 8px 0 0; }
    .detection-table td {
        padding: 0.5rem 0.8rem;
        border-bottom: 1px solid #eee;
        vertical-align: middle;
    }
    .detection-table tr:hover td { background: #f0f4ff; }
    .detection-table .text-cell {
        font-weight: 600;
        color: #c0392b;
        max-width: 200px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .detection-table .context-cell {
        color: #555;
        font-size: 0.82rem;
        max-width: 350px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .detection-table .confidence-high { color: #27ae60; font-weight: 700; }
    .detection-table .confidence-mid { color: #f39c12; font-weight: 600; }
    .detection-table .confidence-low { color: #95a5a6; }

    /* 文件卡片 */
    .file-card {
        background: white;
        border: 1px solid #e0e0e0;
        border-radius: 10px;
        padding: 0.8rem 1rem;
        margin-bottom: 0.5rem;
        display: flex;
        align-items: center;
        gap: 0.8rem;
        transition: box-shadow 0.2s;
    }
    .file-card:hover { box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
    .file-card .icon { font-size: 1.5rem; }
    .file-card .name { font-weight: 600; color: #333; }
    .file-card .size { color: #999; font-size: 0.82rem; }

    /* ── 按钮体系：主按钮保持醒目，次级按钮全局紧凑（不换行）── */
    .stButton > button[kind="primary"] {
        background: linear-gradient(135deg, #5b6ee8 0%, #764ba2 100%);
        border: none;
        color: white;
        font-weight: 700;
        font-size: 0.9rem;
        letter-spacing: 0.02em;
        padding: 0.45rem 1.4rem;
        border-radius: 8px;
        transition: transform 0.1s;
    }
    .stButton > button[kind="primary"]:hover { transform: translateY(-1px); filter: brightness(1.06); }
    .stButton > button[kind="secondary"] {
        height: 1.95rem !important;
        padding: 0 0.7rem !important;
        font-size: 0.78rem !important;
        font-weight: 600 !important;
        white-space: nowrap !important;
        border-radius: 6px !important;
        line-height: 1 !important;
    }

    /* 步骤指示器（紧凑） */
    .step-indicator {
        display: flex;
        align-items: center;
        gap: 0.35rem;
        margin-bottom: 0.7rem;
        flex-wrap: wrap;
    }
    .step {
        display: flex;
        align-items: center;
        gap: 0.3rem;
        padding: 0.22rem 0.65rem;
        border-radius: 999px;
        font-size: 0.75rem;
        font-weight: 600;
    }
    .step.active { background: #667eea; color: white; }
    .step.done { background: #27ae60; color: white; }
    .step.pending { background: #eee; color: #999; }
    .step-arrow { color: #ccc; }

    /* 标签页 */
    .tab-container {
        display: flex;
        gap: 0;
        border-bottom: 2px solid #eee;
        margin-bottom: 1rem;
    }
    .tab {
        padding: 0.6rem 1.2rem;
        cursor: pointer;
        font-weight: 500;
        color: #666;
        border-bottom: 2px solid transparent;
        margin-bottom: -2px;
        transition: all 0.2s;
    }
    .tab.active {
        color: #667eea;
        border-bottom-color: #667eea;
    }
    .tab:hover { color: #667eea; }

    /* 成功横幅 */
    .success-banner {
        background: linear-gradient(135deg, #27ae60 0%, #2ecc71 100%);
        color: white;
        padding: 1.5rem 2rem;
        border-radius: 12px;
        text-align: center;
        margin: 1rem 0;
    }
    .success-banner h2 { margin: 0 0 0.5rem 0; }
    .success-banner p { margin: 0; opacity: 0.9; }

    /* ── 2.3 UI 紧凑化新增 ─────────────────── */

    /* 主区整体密度 */
    .main .block-container { padding-top: 0.9rem; padding-bottom: 2rem; }
    [data-testid="stAppViewContainer"] .main h4 {
        font-size: 1.0rem; font-weight: 700;
        margin: 0.6rem 0 0.35rem;
    }
    [data-testid="stAppViewContainer"] .main hr { margin: 0.55rem 0; }

    /* 主标题行：标题 + 副标题同行 */
    .main-head { display: flex; align-items: baseline; gap: 0.7rem; margin-bottom: 0.35rem; }
    .main-head h1 { font-size: 1.2rem; font-weight: 800; margin: 0; color: #1f2430; }
    .main-head .sub { font-size: 0.78rem; color: #6b7280; }

    /* 标签页：缩小内边距与字号 */
    [data-testid="stTabs"] [data-baseweb="tab"] {
        padding: 0.35rem 0.95rem !important;
        font-size: 0.85rem !important;
        font-weight: 600 !important;
    }
    [data-testid="stTabs"] [data-baseweb="tab-list"] { gap: 0.15rem; }

    /* 上传区：并排双卡小标题 */
    .up-title {
        display: flex; align-items: baseline; gap: 0.5rem;
        font-size: 0.82rem; color: #4a5164;
        margin-bottom: 0.1rem;
    }
    .up-title b { font-size: 0.85rem; font-weight: 700; color: #1f2430; }
    .up-title span { font-size: 0.72rem; color: #9aa1ad; }
    [data-testid="stFileUploaderDropzone"] {
        padding: 0.55rem 0.9rem !important;
        min-height: 3.4rem !important;
        gap: 0.5rem !important;
    }
    [data-testid="stFileUploaderDropzone"] > div:first-child { margin: 0 !important; }
    [data-testid="stFileUploaderDropzone"] span,
    [data-testid="stFileUploaderDropzone"] button { font-size: 0.8rem !important; }
    [data-testid="stFileUploaderDropzone"] small { font-size: 0.72rem !important; }
    [data-testid="stFileUploaderDropzone"] svg { height: 1.2rem !important; width: 1.2rem !important; }

    /* 已上传文件 chips */
    .filelist { display: flex; flex-wrap: wrap; gap: 0.4rem; margin: 0.3rem 0 0.1rem; }
    .fchip {
        display: inline-flex; align-items: center; gap: 0.45rem;
        background: #fff; border: 1px solid #e5e7eb; border-radius: 8px;
        padding: 0.3rem 0.7rem; font-size: 0.78rem;
        box-shadow: 0 1px 2px rgba(20,25,60,.05);
    }
    .fchip .fi { font-size: 0.95rem; }
    .fchip .fn { font-weight: 600; color: #333a48; }
    .fchip .fs { color: #9aa1ad; font-size: 0.72rem; }

    /* 输入区标签 + 徽章 */
    .mi-label { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.25rem; }
    .mi-label b { font-size: 0.85rem; color: #1f2430; }
    .badge {
        font-size: 0.66rem; font-weight: 700; padding: 0.1rem 0.5rem;
        border-radius: 999px; background: #eef0fe; color: #4a5bd0;
    }

    /* 紧凑统计条（代替 4 张大卡片） */
    .statbar { display: flex; gap: 0.55rem; margin: 0.45rem 0 0.4rem; }
    .stat {
        flex: 1; background: #fff; border: 1px solid #e5e7eb; border-radius: 9px;
        padding: 0.4rem 0.85rem; display: flex; align-items: baseline; gap: 0.55rem;
        box-shadow: 0 1px 2px rgba(20,25,60,.04);
    }
    .stat .v { font-size: 1.25rem; font-weight: 800; color: #1f2430; }
    .stat .k { font-size: 0.75rem; color: #6b7280; }
    .stat.s-auto .v { color: #1e9e5a; }
    .stat.s-sugg .v { color: #c07f0a; }
    .stat.s-hint .v { color: #8a90a0; }

    /* 批量按钮组分隔线 */
    .col-sep { width: 1px; height: 1.2rem; background: #e3e6ee; margin: 0.35rem auto 0; }

    /* 全局控件字号统一 */
    .stTextArea textarea, .stTextInput input { font-size: 0.85rem !important; }
    .stSelectbox [data-baseweb="select"] > div { min-height: 2rem !important; font-size: 0.82rem !important; }
    .stCheckbox { min-height: 1.9rem !important; }
    .stCheckbox label p { font-size: 0.82rem !important; }
    </style>
    """, unsafe_allow_html=True)


# ──────────────────────────────────────────────
# 辅助函数
# ──────────────────────────────────────────────

def _extract_text(file_path: Path) -> str:
    """从文件中提取纯文本（R1-A1：委托 adapters/extract 公共实现，
    与 CLI/adapter 处理面同源——docx walker 全部件、xlsx 全部件，
    消除旧版 doc.paragraphs+tables / read_only str 单元格的检测盲区）。"""
    suffix = file_path.suffix.lower()
    try:
        if suffix in (".docx", ".xlsx"):
            from mask_tool.adapters.extract import extract_texts
            return extract_texts(file_path)
        if suffix == ".pptx":
            from pptx import Presentation
            prs = Presentation(str(file_path))
            texts = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        texts.append(shape.text_frame.text)
                    if shape.has_table:
                        for row in shape.table.rows:
                            for cell in row.cells:
                                texts.append(cell.text)
            return "\n".join(texts)
        if suffix == ".pdf":
            try:
                import fitz
                doc = fitz.open(str(file_path))
                texts = [page.get_text() for page in doc]
                doc.close()
                return "\n".join(texts)
            except ImportError:
                return ""
    except Exception:
        return ""
    return ""


def _file_icon(suffix: str) -> str:
    """返回文件类型图标"""
    icons = {
        ".docx": "📄", ".xlsx": "📊", ".pptx": "📽️", ".pdf": "📕",
    }
    return icons.get(suffix.lower(), "📁")


def _confidence_class(confidence: float) -> str:
    """返回置信度对应的CSS类名"""
    if confidence >= 0.85:
        return "confidence-high"
    elif confidence >= 0.60:
        return "confidence-mid"
    return "confidence-low"


def _load_config(mode: str, config_path: Optional[str] = None) -> MaskConfig:
    """加载配置（R1-B6：与 CLI 共用 core/config_loader 四级回退链，
    不再静默回退到空词库）。

    1. 显式路径（存在时）
    2. CWD/config/default.yaml
    3. 内嵌模板（警告）
    4. 纯代码默认（警告）
    每次回退均 st.warning，并自动复制示例词库（N2）。
    """
    from mask_tool.core.config_loader import load_config

    explicit = Path(config_path) if config_path else None
    if explicit is not None and not explicit.exists():
        st.warning(f"指定的配置文件不存在: {explicit}，回退到默认查找链")
        explicit = None
    cfg, events = load_config(explicit, mode)
    for level, message in events:
        if level == "ok":
            st.caption(message)
        elif level == "info":
            st.info(message)
        else:
            st.warning(message)
    return cfg


def _ensure_lexicon_exists(cfg: MaskConfig) -> None:
    """兼容保留：词库自动复制已由 core/config_loader.finalize_paths 统一处理。"""
    from mask_tool.core.config_loader import finalize_paths

    finalize_paths(cfg, "web 兼容入口")


def _dedup_results(results: List[DetectionResult]) -> List[DetectionResult]:
    """跨文件去重：按(text, text_type)去重"""
    seen: Set[Tuple[str, str]] = set()
    deduped = []
    for r in results:
        key = (r.text, r.text_type.value)
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    return deduped


def _parse_custom_words(text: str) -> List[str]:
    """解析临时自定义敏感词输入（I6）：换行/中英文逗号分隔，
    去空白、去空项、去重（保持首次出现顺序）。空输入返回 []。"""
    if not text:
        return []
    words: List[str] = []
    seen: Set[str] = set()
    for part in re.split(r"[\n,，]+", text):
        w = part.strip()
        if w and w not in seen:
            seen.add(w)
            words.append(w)
    return words


# 任务态会话键：检测/勾选/产物/结果页筛选与批次信息，新建任务时全清。
# 配置态键（运行模式/NER/不可逆/学习词库/文件名脱敏开关、custom_words_input、
# manual_only_mode、learn_set）不在此列，跨任务保留。
TASK_STATE_KEYS = [
    "detection_results", "file_results", "user_selections",
    "tmp_dir", "saved_paths", "mask_result", "restore_result",
    "filter_type", "filter_status", "filter_source", "filter_file",
    "search_text", "batch_name_input", "batch_id_display",
    "task_kind", "zip_tree_root", "zip_blocked_files",
    "dir_zip_upload", "restore_zip_upload",
]


def _clear_task_state(clear_upload: bool = True) -> None:
    """清空任务态会话键并删除上传临时目录（配置态键保留）。

    - R1-B5：tmp_dir 指向的上传临时目录（含敏感信息副本）一并删除
    - clear_upload=True 同时 pop 上传组件 key（file_uploader），
      回到初始上传页；False 保留已上传文件（同批文件重走流程语义）
    """
    old_tmp = st.session_state.get("tmp_dir")
    if old_tmp:
        shutil.rmtree(old_tmp, ignore_errors=True)
    keys = list(TASK_STATE_KEYS)
    if clear_upload:
        keys.append("file_uploader")
    for key in keys:
        st.session_state.pop(key, None)


def _reset_task_state() -> None:
    """新建任务：任务态全清（检测/勾选/产物/上传文件），配置态保留。

    保留：侧边栏设置（运行模式/NER/不可逆/学习词库/同时脱敏文件名）、
    临时自定义敏感词（custom_words_input）、仅手动开关（manual_only_mode）、
    learn_set（新一轮检测开始时旧索引自动失效，见 _run_detection）。
    三个入口（侧边栏/重新上传/开始新任务）统一走本函数。
    """
    _clear_task_state(clear_upload=True)


def _apply_grid_selection(filtered_indices: List[int], selected_rows,
                          selections: Dict[int, bool]) -> bool:
    """AgGrid 勾选回传同步到 user_selections（I6 问题3）。

    selected_rows 为 grid 返回的选中行（含 index 列）；None 表示无回传。
    仅同步当前筛选可见行（filtered_indices），不可见行勾选态不受影响。
    返回是否有变化（调用方用于刷新计数显示）。
    """
    if selected_rows is None:
        return False
    in_grid = set()
    for row in selected_rows:
        try:
            idx = int(row.get("index", -1))
        except (AttributeError, TypeError, ValueError):
            continue
        if idx >= 0:
            in_grid.add(idx)
    changed = False
    for i in filtered_indices:
        new_val = i in in_grid
        if selections.get(i) != new_val:
            selections[i] = new_val
            changed = True
    return changed


def _final_selected_indices(all_results, selections: Dict[int, bool]) -> List[int]:
    """"即将脱敏"确认列表：按 user_selections 过滤的全局索引。

    检测表格任何勾选变化后，本函数的输出与计数立即一致（I6 问题3）。
    """
    return [
        i for i in range(len(all_results)) if selections.get(i, False)
    ]


def _results_to_dataframe(results: List[DetectionResult]) -> pd.DataFrame:
    """将检测结果转为 DataFrame"""
    rows = []
    for i, r in enumerate(results):
        rows.append({
            "序号": i + 1,
            "敏感信息": r.text,
            "类别": TYPE_LABELS.get(r.text_type, r.text_type.value),
            "类别值": r.text_type.value,
            "来源": SOURCE_LABELS.get(r.source, r.source),
            "置信度": r.confidence,
            "处置": STATUS_LABELS.get(r.status, r.status.value),
            "状态值": r.status.value,
            "文件": Path(r.location.file).name if r.location.file else "",
            "上下文": r.context,
        })
    return pd.DataFrame(rows)


def _do_mask_file(
    input_path: Path,
    output_dir: Path,
    pipeline: Pipeline,
    confirmed_results: Optional[List[DetectionResult]] = None,
    output_name: Optional[str] = None,
) -> Optional[Path]:
    """对单个文件执行脱敏（I1a 契约：改调 pipeline.process_file）。

    旧的"逐 run 文本替换"与"无替换时 copy2 原件"路径已废弃：
    process_file 按格式适配器处理并负责无命中文件的输出（H3 消除）。
    confirmed_results 提供时传入 allowed_originals/statuses（确认模式）。
    output_name：目录（zip）任务传原文件名——保持镜像树相对结构，
    名字脱敏统一交给 PathMasker.mask_tree。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    kwargs = {}
    if confirmed_results:
        kwargs["allowed_originals"] = {r.text for r in confirmed_results}
        kwargs["statuses"] = {DetectionStatus.AUTO_MASK, DetectionStatus.SUGGEST_MASK}
    if output_name is not None:
        kwargs["output_name"] = output_name
    try:
        return pipeline.process_file(input_path, output_dir, **kwargs)
    except TypeError:
        # 防御分支：core/pipeline.py 契约签名（allowed_originals/statuses/output_name，
        # I1a 已就位）若在旧环境下缺失，降级为无确认参数调用（全量自动脱敏）。
        kwargs.pop("output_name", None)
        return pipeline.process_file(input_path, output_dir, **kwargs)


# ──────────────────────────────────────────────
# 反脱敏函数（R1-A2：统一走 adapters/restore 公共实现，与 CLI/adapter 同源）
# ──────────────────────────────────────────────

def _unmask_pptx(input_path: Path, output_path: Path, tokens: dict) -> None:
    """反脱敏 pptx 文件。tokens 格式: {token_str: original_str}

    pptx 已无 mask 来源（BLOCKED_EXTS），仅服务旧版产物，保留旧实现。
    """
    from pptx import Presentation
    shutil.copy2(input_path, output_path)
    prs = Presentation(str(output_path))
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    for run in para.runs:
                        for token, original in tokens.items():
                            if token in run.text:
                                run.text = run.text.replace(token, original)
            if shape.has_table:
                for row in shape.table.rows:
                    for cell in row.cells:
                        if cell.text_frame:
                            for para in cell.text_frame.paragraphs:
                                for run in para.runs:
                                    for token, original in tokens.items():
                                        if token in run.text:
                                            run.text = run.text.replace(token, original)
    prs.save(str(output_path))


def _unmask_file(input_path: Path, output_path: Path, tokens: dict) -> Optional[Path]:
    """根据文件类型分发反脱敏（R1-A2）。

    tokens 兼容两种形态：{token: original_str}（旧版/_mapping_to_tokens）或
    mapping 的 tokens 段 {token: {original, kind, ...}}；docx/xlsx 统一委托
    adapters/restore.restore_file_content——页眉/脚注/批注/富文本全覆盖，
    kind=number 的数字单元格还原为数值（旧版 _restore_cell 单参调用导致
    数值还原静默失效的问题随之消除）。
    """
    suffix = input_path.suffix.lower()
    try:
        if suffix in (".docx", ".xlsx"):
            from mask_tool.adapters.restore import restore_file_content

            token_map = {
                t: (v if isinstance(v, dict) else {"original": v})
                for t, v in tokens.items()
            }
            restore_file_content(input_path, output_path, token_map)
        elif suffix == ".pptx":
            _unmask_pptx(input_path, output_path, tokens)
        else:
            # 纯文本文件
            text = input_path.read_text(encoding="utf-8")
            for token, entry in tokens.items():
                original = (
                    entry.get("original", "") if isinstance(entry, dict) else entry
                )
                text = text.replace(token, original)
            output_path.write_text(text, encoding="utf-8")
        return output_path
    except Exception as e:
        st.error(f"反脱敏 {input_path.name} 时出错: {e}")
        return None


# ──────────────────────────────────────────────
# 批次管理
# ──────────────────────────────────────────────

@dataclass
class BatchRecord:
    batch_id: str
    batch_name: str
    created_at: str  # ISO格式
    file_count: int
    mask_count: int
    mapping_data: str  # 兼容字段：仅旧版记录中含完整映射 JSON；新记录恒为 ""（不再保存映射明文）
    mapping_path: str = ""  # 新版：批次目录 mapping.json 的完整路径（映射留存处）


def _generate_batch_id() -> str:
    """生成批次ID，格式：MSK-YYYYMMDD-HHMMSS-XXX"""
    now = datetime.now()
    date_part = now.strftime("%Y%m%d")
    time_part = now.strftime("%H%M%S")
    rand_part = "".join(random.choices(string.ascii_uppercase + string.digits, k=3))
    return f"MSK-{date_part}-{time_part}-{rand_part}"


def _load_history() -> List[BatchRecord]:
    """加载批次历史记录（容忍旧/新字段差异）"""
    if not HISTORY_PATH.exists():
        return []
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        valid_fields = {f_.name for f_ in BatchRecord.__dataclass_fields__.values()}
        records = []
        for item in data:
            filtered = {k: v for k, v in item.items() if k in valid_fields}
            records.append(BatchRecord(**filtered))
        return records
    except Exception:
        return []


def _save_history(records: List[BatchRecord]) -> None:
    """保存批次历史记录"""
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 只保留最近 MAX_HISTORY 条
    records = records[-MAX_HISTORY:]
    data = [asdict(r) for r in records]
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _add_history(record: BatchRecord) -> None:
    """添加一条历史记录"""
    records = _load_history()
    records.append(record)
    _save_history(records)


# ──────────────────────────────────────────────
# 侧边栏
# ──────────────────────────────────────────────

def render_sidebar():
    """渲染侧边栏配置"""
    with st.sidebar:
        st.markdown(
            f'<div class="side-head"><span class="logo">🔒</span>'
            f'<span class="name">mask-tool</span>'
            f'<span class="ver">v{__version__}</span></div>',
            unsafe_allow_html=True,
        )

        # 新建任务：任何时候可见，放弃当前流程回到上传页（设置与自定义词保留）
        if st.button(
            "＋ 新建脱敏任务",
            use_container_width=True,
            key="new_task_btn",
            help=("放弃当前检测/勾选/脱敏结果并清空已上传文件，回到上传页；"
                  "运行设置与临时自定义敏感词保留"),
        ):
            _reset_task_state()
            st.rerun()

        # 运行模式
        st.markdown('<div class="side-label">运行模式</div>', unsafe_allow_html=True)
        mode = st.selectbox(
            "选择模式",
            options=["focused", "smart", "strict", "aggressive"],
            format_func=lambda x: {
                "focused": "🎯 精准模式",
                "smart": "🧠 智能模式（推荐）",
                "strict": "🔒 严格模式",
                "aggressive": "🚀 激进模式",
            }.get(x, x),
            index=1,
        )
        st.caption(MODE_DESCRIPTIONS.get(mode, ""))

        # NER 开关
        st.markdown('<div class="side-label">识别引擎</div>', unsafe_allow_html=True)
        ner_enabled = st.toggle(
            "启用 jieba NER",
            value=True,
            help="启用后可识别词典未覆盖的实体（人名、地名、机构名等），但可能产生误识别",
        )

        # 脱敏选项
        st.markdown('<div class="side-label">脱敏选项</div>', unsafe_allow_html=True)
        irreversible = st.checkbox(
            "不可逆脱敏",
            value=False,
            help="启用后将用 *** 替换敏感信息，无法还原",
        )
        learn_words = st.checkbox(
            "学习新词到词库",
            value=True,
            help="确认时标记为'加入词库'的词将写入词库文件",
        )

        # 词库管理
        st.markdown('<div class="side-label">词库管理</div>', unsafe_allow_html=True)
        lexicon_info = _get_lexicon_info()
        if lexicon_info:
            st.markdown(
                f'<div class="lex-chip"><b>📖 词库词条</b>'
                f'<span class="cnt">{lexicon_info["total"]:,} 条</span></div>',
                unsafe_allow_html=True,
            )

            # 2.1: 每个类别可展开查看明细
            lexicon_data = _get_lexicon_data()
            if lexicon_data:
                for cat in sorted(lexicon_data.keys(), key=lambda c: -len(lexicon_data[c])):
                    label = TYPE_LABELS.get(DetectionType(cat), cat)
                    count = len(lexicon_data[cat])
                    with st.expander(f"{label}: {count} 条"):
                        for word in lexicon_data[cat]:
                            st.code(word)
        else:
            st.caption("词库未加载")

        # 2.2: 手动录入词条
        with st.expander("✏️ 手动录入词条", expanded=False):
            valid_categories = {t.value: TYPE_LABELS.get(t, t.value) for t in DetectionType}
            col_cat, col_word = st.columns([1, 2])
            with col_cat:
                input_cat = st.selectbox(
                    "类别",
                    options=list(valid_categories.keys()),
                    format_func=lambda x: valid_categories[x],
                    key="manual_cat",
                    label_visibility="collapsed",
                )
            with col_word:
                input_words = st.text_area(
                    "词条（多条用逗号或换行分隔）",
                    placeholder="输入词条，多条用逗号或换行分隔...",
                    key="manual_words",
                    label_visibility="collapsed",
                    height=70,
                )
            # "其他"类别：允许自定义
            if input_cat == "custom":
                custom_cat_name = st.text_input(
                    "自定义类别名称（留空则归入 custom）",
                    key="custom_cat_name",
                    placeholder="如：brand, department...",
                )
            if st.button("➕ 添加到词库", use_container_width=True, key="add_words_btn"):
                _add_words_to_lexicon(input_cat, input_words, custom_cat_name if input_cat == "custom" else None)

        # 批量导入词库
        with st.expander("📥 批量导入词条", expanded=False):
            st.caption("支持 YAML 或 TXT 格式")
            import_file = st.file_uploader(
                "选择词库文件",
                type=["yaml", "yml", "txt"],
                key="lexicon_upload",
                label_visibility="collapsed",
            )
            if import_file:
                _import_lexicon(import_file)

        st.markdown('<div class="side-label">关于</div>', unsafe_allow_html=True)
        st.caption("mask-tool · MIT License")

    return mode, ner_enabled, irreversible, learn_words


def _get_lexicon_data() -> Optional[Dict[str, List[str]]]:
    """读取词库完整数据（分类别返回词条列表）"""
    try:
        config_paths = [
            Path("config/lexicon.yaml"),
            Path("config/sample_lexicon.yaml"),
            Path(__file__).parent.parent.parent / "config" / "lexicon.yaml",
            Path(__file__).parent.parent.parent / "config" / "sample_lexicon.yaml",
        ]
        for p in config_paths:
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                return {k: v for k, v in data.items() if isinstance(v, list)}
    except Exception:
        pass
    return None


def _add_words_to_lexicon(category: str, words_text: str, custom_category: Optional[str] = None) -> None:
    """手动添加词条到用户词库"""
    if not words_text or not words_text.strip():
        st.warning("请输入词条内容")
        return

    # 确定实际类别
    actual_cat = custom_category.strip() if custom_category and custom_category.strip() else category

    # 解析词条（支持逗号、中文逗号、换行分隔）
    words = []
    for part in words_text.replace("，", ",").replace("\n", ",").split(","):
        w = part.strip()
        if w:
            words.append(w)

    if not words:
        st.warning("未识别到有效词条")
        return

    # 确保用户词库存在
    lexicon_path = Path("config/lexicon.yaml")
    if not lexicon_path.exists():
        sample_path = Path("config/sample_lexicon.yaml")
        if sample_path.exists():
            shutil.copy2(sample_path, lexicon_path)
        else:
            lexicon_path.write_text("", encoding="utf-8")

    # 读取现有词库
    with open(lexicon_path, "r", encoding="utf-8") as f:
        existing = yaml.safe_load(f) or {}

    # 确保类别存在
    if actual_cat not in existing:
        existing[actual_cat] = []

    # 添加新词条（去重）
    added = 0
    for word in words:
        if word not in existing[actual_cat]:
            existing[actual_cat].append(word)
            added += 1

    # 保存
    if added > 0:
        with open(lexicon_path, "w", encoding="utf-8") as f:
            yaml.dump(existing, f, allow_unicode=True, default_flow_style=False)
        st.success(f"✅ 成功添加 {added} 条词条到 [{actual_cat}]")
    else:
        st.info("ℹ️ 所有词条已存在于词库中")


def _get_lexicon_info() -> Optional[dict]:
    """获取词库统计信息（优先读取用户词库 lexicon.yaml）"""
    try:
        # 按优先级查找词库文件
        config_paths = [
            Path("config/lexicon.yaml"),
            Path("config/sample_lexicon.yaml"),
            Path(__file__).parent.parent.parent / "config" / "lexicon.yaml",
            Path(__file__).parent.parent.parent / "config" / "sample_lexicon.yaml",
        ]
        for p in config_paths:
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                categories = {k: len(v) for k, v in data.items() if isinstance(v, list)}
                return {
                    "total": sum(categories.values()),
                    "categories": categories,
                    "path": str(p),
                }
    except Exception:
        pass
    return None


def _import_lexicon(uploaded_file) -> None:
    """从上传的文件批量导入词条到用户词库

    支持格式：
    - YAML: 与 sample_lexicon.yaml 相同格式（{category: [word1, word2, ...]}）
    - TXT: 每行一个词条，格式为 "类别:词条" 或纯词条（默认归入 custom）
    """
    import io

    # 确定用户词库路径
    lexicon_path = Path("config/lexicon.yaml")
    if not lexicon_path.exists():
        # 从示例词库复制
        sample_path = Path("config/sample_lexicon.yaml")
        if sample_path.exists():
            shutil.copy2(sample_path, lexicon_path)
        else:
            lexicon_path.write_text("", encoding="utf-8")

    # 读取现有词库
    with open(lexicon_path, "r", encoding="utf-8") as f:
        existing = yaml.safe_load(f) or {}

    # 确保所有类别键存在
    valid_categories = [t.value for t in DetectionType]
    for cat in valid_categories:
        if cat not in existing:
            existing[cat] = []

    filename = uploaded_file.name.lower()
    added_count = 0

    if filename.endswith((".yaml", ".yml")):
        # YAML 格式导入
        content = uploaded_file.read().decode("utf-8")
        new_data = yaml.safe_load(content)
        if isinstance(new_data, dict):
            for cat, words in new_data.items():
                if isinstance(words, list) and cat in valid_categories:
                    for word in words:
                        if isinstance(word, str) and word.strip() and word not in existing[cat]:
                            existing[cat].append(word.strip())
                            added_count += 1
                elif isinstance(words, list):
                    # 未知类别，归入 custom
                    for word in words:
                        if isinstance(word, str) and word.strip() and word not in existing["custom"]:
                            existing["custom"].append(word.strip())
                            added_count += 1

    elif filename.endswith(".txt"):
        # TXT 格式导入：每行一个词条
        content = uploaded_file.read().decode("utf-8")
        for line in content.strip().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                # 格式：类别:词条
                cat, word = line.split(":", 1)
                cat = cat.strip().lower()
                word = word.strip()
                if cat in valid_categories and word:
                    if word not in existing[cat]:
                        existing[cat].append(word)
                        added_count += 1
            else:
                # 纯词条，归入 custom
                if line not in existing["custom"]:
                    existing["custom"].append(line)
                    added_count += 1

    # 保存
    if added_count > 0:
        with open(lexicon_path, "w", encoding="utf-8") as f:
            yaml.dump(existing, f, allow_unicode=True, default_flow_style=False)
        st.success(f"✅ 成功导入 {added_count} 条新词条到词库")
    else:
        st.info("ℹ️ 没有新词条需要导入（全部已存在）")


# ──────────────────────────────────────────────
# 步骤指示器
# ──────────────────────────────────────────────

def render_steps(current_step: int):
    """渲染步骤指示器 (1-4)"""
    steps = [
        ("1", "📤 上传"),
        ("2", "🔍 检测"),
        ("3", "✅ 确认选择"),
        ("4", "💾 执行脱敏"),
    ]
    step_html = '<div class="step-indicator">'
    for i, (num, label) in enumerate(steps):
        step_num = i + 1
        cls = "done" if step_num < current_step else ("active" if step_num == current_step else "pending")
        step_html += f'<div class="step {cls}">{label}</div>'
        if i < len(steps) - 1:
            step_html += '<span class="step-arrow">→</span>'
    step_html += '</div>'
    st.markdown(step_html, unsafe_allow_html=True)


# ──────────────────────────────────────────────
# 标签页1：脱敏处理
# ──────────────────────────────────────────────

def _render_masking_tab(mode: str, ner_enabled: bool, irreversible: bool, learn_words: bool):
    """渲染脱敏处理标签页"""

    # 如果已有脱敏结果，展示结果页面
    if "mask_result" in st.session_state:
        _render_mask_result()
        return

    # ── Step 1: 文件上传 ──
    render_steps(1)

    # 上传区并排：左单文件 / 右目录 zip（压缩纵向占用，比例 1.35:1）
    up_cols = st.columns([1.35, 1])
    with up_cols[0]:
        st.markdown(
            '<div class="up-title">📄 <b>单文件上传</b>'
            '<span>支持多选 .docx / .xlsx</span></div>',
            unsafe_allow_html=True,
        )
        uploaded_files = st.file_uploader(
            "上传待脱敏文件",
            type=sorted(ext.lstrip(".") for ext in SUPPORTED_MASK_EXTS),
            accept_multiple_files=True,
            label_visibility="collapsed",
            key="file_uploader",  # I6：新建任务时 pop 本 key 清空已传文件
        )
    with up_cols[1]:
        st.markdown(
            '<div class="up-title">🗂️ <b>目录压缩包</b>'
            '<span>产物为同结构目录 zip</span></div>',
            unsafe_allow_html=True,
        )
        # I6 问题2：目录上传入口——把目录压缩成 zip（解压后按目录模式处理，
        # 产物为同结构目录的 zip）
        dir_zip = st.file_uploader(
            "或上传目录压缩包（.zip）",
            type=["zip"],
            key="dir_zip_upload",
            label_visibility="collapsed",
            help=("把整个目录压缩成 zip 后上传：递归处理其中的 docx/xlsx，"
                  "pptx/pdf 等屏蔽类型会警告且不进产物；脱敏产物为同结构"
                  "目录的 zip 压缩包"),
        )
    if dir_zip is not None and uploaded_files:
        st.info("已同时上传单文件与目录 zip：本次按目录 zip 处理，单文件列表忽略")

    if not uploaded_files:
        st.info("📤 请上传需要脱敏的文件（支持 .docx / .xlsx）")
        st.caption("PPT 与 PDF 已暂时停用：检测到 .pptx / .pdf 文件将拒绝处理。")
        return

    # 屏蔽策略双保险：uploader 已限类型，此处对绕过途径（API 调用等）给出明确报错
    blocked_msgs = [
        f"{f.name}：{BLOCKED_EXTS.get(Path(f.name).suffix.lower(), '该格式暂不支持脱敏')}"
        for f in uploaded_files if Path(f.name).suffix.lower() in BLOCKED_EXTS
    ]
    if blocked_msgs:
        st.error(
            "以下文件暂不支持脱敏，请移除后重试（当前仅支持 Word .docx / Excel .xlsx）：\n\n"
            + "\n\n".join(blocked_msgs)
        )
        return

    # 显示已上传文件（紧凑 chips，代替卡片网格）
    chips_html = "".join(
        f'<span class="fchip"><span class="fi">{_file_icon(Path(f.name).suffix)}</span>'
        f'<span class="fn">{f.name}</span>'
        f'<span class="fs">{f.size / 1024:.1f} KB</span></span>'
        for f in uploaded_files
    )
    st.markdown(f'<div class="filelist">{chips_html}</div>', unsafe_allow_html=True)

    # ── Step 2: 检测分析 ──
    st.markdown("---")
    render_steps(2)

    # 输入区双栏：左 = 临时敏感词输入（加高）；右 = 本次任务选项（2.3 比例优化）
    mi_cols = st.columns([1, 0.42])
    with mi_cols[0]:
        # I6：临时自定义敏感词（仅本次任务生效，不写入词库文件）
        st.markdown(
            '<div class="mi-label"><b>✍️ 临时自定义敏感词</b>'
            '<span class="badge">仅本次任务生效</span></div>',
            unsafe_allow_html=True,
        )
        custom_words_text = st.text_area(
            "临时自定义敏感词",
            placeholder=("每行一个词，或用逗号分隔；仅本次任务生效，不写入词库。\n"
                         "例：某某科技有限公司，张三丰，2026年Q3财报"),
            key="custom_words_input",
            height=118,
            label_visibility="collapsed",
        )
    with mi_cols[1]:
        # 同时脱敏文件名（仅处理主名，不改扩展名）
        mask_filenames = st.checkbox(
            "同时脱敏文件名",
            value=True,
            key="mask_filenames",
            help="对文件主名（不含扩展名）执行同样的检测与替换，Token 与正文共享",
        )
        manual_only = st.checkbox(
            "仅脱敏我指定的词（跳过自动检测）",
            value=False,
            key="manual_only_mode",
            help=("开启后完全跳过自动检测（NER/正则/词库均不运行），"
                  "检测与脱敏只处理左侧手动指定的词；适合自动检测误报多、"
                  "只想针对性脱敏的场景"),
        )
        st.caption("💡 自动检测误报的词（如普通词被识为人名），可加入 config/whitelist.yaml 的 whitelist 永久排除")

    if st.button("🔍 开始检测", type="primary", width="stretch"):
        with st.spinner("正在分析，检测敏感信息..."):
            _run_detection(
                [] if dir_zip is not None else uploaded_files,
                mode, ner_enabled,
                manual_words=_parse_custom_words(custom_words_text),
                manual_only=manual_only,
                zip_file=dir_zip,
            )

    # 检查是否已有检测结果
    if "detection_results" not in st.session_state:
        return

    all_results = st.session_state["detection_results"]
    file_results = st.session_state["file_results"]

    # I6 问题2：目录 zip 中的屏蔽类型（pptx/pdf 等）——警告且不进产物
    zip_blocked = st.session_state.get("zip_blocked_files") or []
    if zip_blocked:
        st.warning(
            "以下 " + str(len(zip_blocked)) + " 个屏蔽类型文件不会进入脱敏产物"
            "（与 CLI 目录模式语义一致）：\n\n- " + "\n- ".join(zip_blocked)
        )

    if not all_results:
        st.success("✅ 未检测到敏感信息，文件安全！")
        return

    # ── 检测结果统计 ──
    st.markdown("#### 📊 检测结果概览")

    # 紧凑统计条（代替 4 张大卡片）
    auto_count = sum(1 for r in all_results if r.status == DetectionStatus.AUTO_MASK)
    suggest_count = sum(1 for r in all_results if r.status == DetectionStatus.SUGGEST_MASK)
    hint_count = sum(1 for r in all_results if r.status == DetectionStatus.HINT_ONLY)
    st.markdown(
        f'<div class="statbar">'
        f'<div class="stat"><span class="v">{len(all_results)}</span><span class="k">检测总数</span></div>'
        f'<div class="stat s-auto"><span class="v">{auto_count}</span><span class="k">自动脱敏</span></div>'
        f'<div class="stat s-sugg"><span class="v">{suggest_count}</span><span class="k">建议脱敏</span></div>'
        f'<div class="stat s-hint"><span class="v">{hint_count}</span><span class="k">仅提示</span></div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # 类别分布
    type_counts: Dict[str, int] = {}
    for r in all_results:
        label = TYPE_LABELS.get(r.text_type, r.text_type.value)
        type_counts[label] = type_counts.get(label, 0) + 1

    if type_counts:
        chart_cols = st.columns([2, 1])
        with chart_cols[0]:
            # 用原生 HTML 条形图代替 st.bar_chart（避免 pyarrow 依赖）
            sorted_counts = sorted(type_counts.items(), key=lambda x: x[1])
            max_val = max(type_counts.values()) if type_counts else 1
            bars_html = '<div style="font-size:0.85rem;">'
            for label, count in sorted_counts:
                pct = int(count / max_val * 100)
                bars_html += (
                    f'<div style="display:flex;align-items:center;margin-bottom:4px;">'
                    f'<span style="width:120px;flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{label}</span>'
                    f'<div style="flex:1;background:#eee;border-radius:4px;height:22px;position:relative;">'
                    f'<div style="background:linear-gradient(90deg,#667eea,#764ba2);width:{pct}%;height:100%;border-radius:4px;min-width:2px;"></div>'
                    f'<span style="position:absolute;right:6px;top:2px;font-size:0.78rem;font-weight:600;">{count}</span>'
                    f'</div></div>'
                )
            bars_html += '</div>'
            st.markdown(bars_html, unsafe_allow_html=True)
        with chart_cols[1]:
            st.markdown("**类别分布**")
            for label, count in sorted(type_counts.items(), key=lambda x: -x[1]):
                st.markdown(f"- {label}: **{count}** 项")

    # ── Step 3: 确认选择 ──
    st.markdown("---")
    render_steps(3)

    # 初始化选择状态
    if "user_selections" not in st.session_state:
        # 默认：自动脱敏和建议脱敏的项都勾选
        st.session_state["user_selections"] = {
            i: (r.status in (DetectionStatus.AUTO_MASK, DetectionStatus.SUGGEST_MASK))
            for i, r in enumerate(all_results)
        }

    # 筛选器
    st.markdown("#### 🎛️ 筛选与选择")

    filter_cols = st.columns(5)
    with filter_cols[0]:
        filter_type = st.selectbox(
            "按类别筛选",
            options=["全部"] + list(type_counts.keys()),
            key="filter_type",
        )
    with filter_cols[1]:
        filter_status = st.selectbox(
            "按处置筛选",
            options=["全部", "✅ 自动脱敏", "⚠️ 建议脱敏", "ℹ️ 仅提示"],
            key="filter_status",
        )
    with filter_cols[2]:
        filter_source = st.selectbox(
            "按来源筛选",
            options=["全部", "✍️ 手动", "📘 词典", "🤖 NER", "🔍 正则", "📄 文件名"],
            key="filter_source",
        )
    with filter_cols[3]:
        filter_file = st.selectbox(
            "按文件筛选",
            options=["全部"] + list(file_results.keys()),
            key="filter_file",
        )
    with filter_cols[4]:
        search_text = st.text_input("搜索", placeholder="输入关键词...", key="search_text")

    # 应用筛选
    filtered_indices = []
    for i, r in enumerate(all_results):
        # 类别筛选
        if filter_type != "全部":
            if TYPE_LABELS.get(r.text_type, r.text_type.value) != filter_type:
                continue
        # 状态筛选
        if filter_status != "全部":
            status_map = {
                "✅ 自动脱敏": DetectionStatus.AUTO_MASK,
                "⚠️ 建议脱敏": DetectionStatus.SUGGEST_MASK,
                "ℹ️ 仅提示": DetectionStatus.HINT_ONLY,
            }
            if r.status != status_map.get(filter_status):
                continue
        # 来源筛选
        if filter_source != "全部":
            source_map = {
                "✍️ 手动": "manual",
                "📘 词典": "dictionary",
                "🤖 NER": "ner",
                "🔍 正则": "regex",
                "📄 文件名": "path",
            }
            if r.source != source_map.get(filter_source):
                continue
        # 文件筛选
        if filter_file != "全部":
            if Path(r.location.file).name != filter_file:
                continue
        # 搜索
        if search_text:
            if search_text.lower() not in r.text.lower() and search_text.lower() not in r.context.lower():
                continue
        filtered_indices.append(i)

    # 批量操作按钮：分组工具栏（全选/清空/反选 ｜ 仅自动/仅建议 ｜ 加入词库）
    batch_cols = st.columns([1, 1, 1, 0.12, 1.25, 1.25, 0.12, 1.5])
    with batch_cols[0]:
        if st.button("全选", width="stretch"):
            for i in filtered_indices:
                st.session_state["user_selections"][i] = True
            st.rerun()
    with batch_cols[1]:
        if st.button("清空", width="stretch"):
            for i in filtered_indices:
                st.session_state["user_selections"][i] = False
            st.rerun()
    with batch_cols[2]:
        if st.button("反选", width="stretch"):
            for i in filtered_indices:
                st.session_state["user_selections"][i] = not st.session_state["user_selections"][i]
            st.rerun()
    with batch_cols[3]:
        st.markdown('<div class="col-sep"></div>', unsafe_allow_html=True)
    with batch_cols[4]:
        if st.button("仅自动脱敏", width="stretch"):
            for i in filtered_indices:
                st.session_state["user_selections"][i] = (
                    all_results[i].status == DetectionStatus.AUTO_MASK
                )
            st.rerun()
    with batch_cols[5]:
        if st.button("仅建议脱敏", width="stretch"):
            for i in filtered_indices:
                st.session_state["user_selections"][i] = (
                    all_results[i].status == DetectionStatus.SUGGEST_MASK
                )
            st.rerun()
    with batch_cols[6]:
        st.markdown('<div class="col-sep"></div>', unsafe_allow_html=True)
    with batch_cols[7]:
        if st.button("📚 选中项加入词库", width="stretch"):
            for i in filtered_indices:
                st.session_state["user_selections"][i] = True
                if "learn_set" not in st.session_state:
                    st.session_state["learn_set"] = set()
                st.session_state["learn_set"].add(i)
            st.rerun()

    # 选中计数
    selected_count = sum(
        1 for i in filtered_indices if st.session_state["user_selections"].get(i, False)
    )
    st.caption(f"当前显示 {len(filtered_indices)} 项，已选中 **{selected_count}** 项")

    # 检测结果表格（AgGrid；SELECTION_CHANGED：勾选变化立即回传并触发
    # rerun，保证下方"即将脱敏"列表与计数同步——I6 问题3）
    if filtered_indices:
        from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode

        display_rows = []
        for i in filtered_indices:
            r = all_results[i]
            display_rows.append({
                "index": i,
                "选择": st.session_state["user_selections"].get(i, False),
                "敏感信息": r.text,
                "类别": TYPE_LABELS.get(r.text_type, r.text_type.value),
                "来源": SOURCE_LABELS.get(r.source, r.source),
                "置信度": r.confidence,
                "处置": STATUS_LABELS.get(r.status, r.status.value),
                "文件": Path(r.location.file).name if r.location.file else "",
                "上下文": r.context[:80] + "..." if len(r.context) > 80 else r.context,
            })

        df_display = pd.DataFrame(display_rows)

        # 构建 AgGrid 配置（兼容不同版本的参数命名）
        gb = GridOptionsBuilder.from_dataframe(df_display)
        gb.configure_column("index", hide=True)
        gb.configure_column("选择", headerCheckboxSelection=True, editable=True, width=68)
        gb.configure_column("敏感信息", editable=False, width=200)
        gb.configure_column("类别", editable=False, width=92)
        gb.configure_column("来源", editable=False, width=88)
        gb.configure_column("置信度", editable=False, type=["numericColumn"], precisionFormat=2, width=86)
        gb.configure_column("处置", editable=False, width=96)
        gb.configure_column("文件", editable=False, width=168)
        gb.configure_column("上下文", editable=False, width=380)
        # configure_selection: 兼容 camelCase / snake_case / 旧版参数
        try:
            gb.configure_selection(selectionMode="multiple", useCheckbox=True, preSelectedRows=[
                j for j, row in enumerate(display_rows) if row["选择"]
            ])
        except TypeError:
            try:
                gb.configure_selection(
                    selection_mode="multiple", use_checkbox=True,
                    pre_selected_rows=[j for j, row in enumerate(display_rows) if row["选择"]],
                )
            except TypeError:
                gb.configure_selection("multiple", use_checkbox=True)
        # configure_pagination: 兼容不同版本
        try:
            gb.configure_pagination(paginationAutoPageSize=False, paginationPageSize=30)
        except TypeError:
            try:
                gb.configure_pagination(pagination_auto_page_size=False, pagination_page_size=30)
            except TypeError:
                gb.configure_pagination(paginationPageSize=30)
        gridOptions = gb.build()

        # 渲染 AgGrid（显式列宽，上下文列加宽减少截断）；勾选变化即回传+rerun
        grid_response = AgGrid(
            df_display,
            gridOptions=gridOptions,
            update_mode=GridUpdateMode.SELECTION_CHANGED,
            fit_columns_on_grid_load=False,
            height=500,
            allow_unsafe_jscode=True,
            theme="streamlit",
        )

        # 从 AgGrid 响应中同步选择状态（SELECTION_CHANGED：勾选变化即回传）
        changed = _apply_grid_selection(
            filtered_indices,
            grid_response.get("selected_rows"),
            st.session_state["user_selections"],
        )
        # 如果选择状态有变化，更新计数显示（不 rerun 整个页面）
        if changed:
            selected_count = sum(
                1 for i in filtered_indices if st.session_state["user_selections"].get(i, False)
            )
            st.caption(f"当前显示 {len(filtered_indices)} 项，已选中 **{selected_count}** 项")

    # ── Step 4: 执行脱敏 ──
    st.markdown("---")
    render_steps(4)

    # 最终确认的项（勾选变化后即时一致：I6 问题3）
    final_selected = _final_selected_indices(
        all_results, st.session_state["user_selections"]
    )

    if not final_selected:
        st.warning("⚠️ 请至少选择一项进行脱敏")
        return

    st.markdown(f"#### 📋 即将脱敏 **{len(final_selected)}** 项")

    # 展示选中项预览
    preview_items = []
    for i in final_selected:
        r = all_results[i]
        preview_items.append(f"- {r.text} ({TYPE_LABELS.get(r.text_type, '')})")
    with st.expander("查看选中项详情", expanded=False):
        st.markdown("\n".join(preview_items[:50]))
        if len(preview_items) > 50:
            st.caption(f"... 共 {len(preview_items)} 项")

    # 批次信息
    st.markdown("#### 📦 批次信息")
    batch_cols = st.columns(2)
    with batch_cols[0]:
        batch_name = st.text_input(
            "批次名称（可选）",
            placeholder="例如：2026年Q1财务报告脱敏",
            key="batch_name_input",
        )
    with batch_cols[1]:
        # 自动生成批次ID，每次 rerun 重新生成
        batch_id = _generate_batch_id()
        st.text_input(
            "批次ID（自动生成）",
            value=batch_id,
            disabled=True,
            key="batch_id_display",
        )

    exec_cols = st.columns(3)
    with exec_cols[0]:
        execute_btn = st.button(
            "🚀 执行脱敏",
            type="primary",
            width="stretch",
        )
    with exec_cols[1]:
        re_detect_btn = st.button(
            "🔄 重新检测",
            width="stretch",
        )
    with exec_cols[2]:
        reupload_btn = st.button(
            "⬅️ 重新上传文件",
            width="stretch",
            help="放弃当前检测结果与勾选，清空已上传文件（自定义词保留）",
        )

    if re_detect_btn:
        # 同批文件重走检测：保留上传组件，仅清检测/勾选态（I6 统一收敛至
        # _clear_task_state；learn_set 由 _run_detection 开头按索引失效重置）
        _clear_task_state(clear_upload=False)
        st.rerun()

    if reupload_btn:
        _reset_task_state()
        st.rerun()

    if execute_btn:
        with st.spinner("正在执行脱敏..."):
            _run_masking(
                uploaded_files, final_selected, all_results,
                mode, ner_enabled, irreversible, learn_words,
                batch_id, batch_name, mask_filenames=mask_filenames,
                manual_words=_parse_custom_words(custom_words_text),
                manual_only=manual_only,
            )


def _render_mask_result():
    """展示脱敏结果页面（持久化到 session_state）"""
    result = st.session_state["mask_result"]

    # 成功横幅
    st.markdown(
        '<div class="success-banner">'
        '<h2>🎉 脱敏完成！</h2>'
        f'<p>成功处理 {result["confirmed_count"]} 项敏感信息</p>'
        f'<p>批次ID: {result["batch_id"]} | 批次名称: {result["batch_name"] or "未命名"}</p>'
        '</div>',
        unsafe_allow_html=True,
    )

    # 映射表信息
    mappings = result.get("mappings", [])
    if mappings:
        with st.expander("📋 脱敏映射表", expanded=False):
            mapping_df = pd.DataFrame([
                {
                    "Token": m["token"],
                    "原文": m["original"],
                    "类别": m.get("type_label", ""),
                    "置信度": f"{m.get('confidence', 0):.2f}",
                }
                for m in mappings
            ])
            st.dataframe(mapping_df, width="stretch", hide_index=True)

    # 下载按钮（I6 问题1 下载规则：单文件 → 脱敏后同名文件；
    # 目录 zip / 多文件 → 同结构 zip 压缩包）
    st.markdown("#### 📥 下载脱敏文件")

    dl_cols = st.columns(2)
    with dl_cols[0]:
        if result.get("download_kind") == "file":
            st.download_button(
                label=f"📥 下载脱敏文件（{result.get('file_name') or '文件'}）",
                data=result.get("file_bytes") or b"",
                file_name=result.get("file_name") or "masked.docx",
                key="download_masked_file",
                type="primary",
                width="stretch",
            )
        else:
            st.download_button(
                label="📥 下载脱敏文件 (ZIP)",
                data=result.get("zip_buffer") or b"",
                file_name=f"{result['batch_id']}_masked.zip",
                mime="application/zip",
                key="download_masked_zip",
                type="primary",
                width="stretch",
            )
    with dl_cols[1]:
        st.download_button(
            label="📋 下载映射表 (JSON)",
            data=result["mapping_data"].encode("utf-8"),
            file_name=f"{result['batch_id']}_mapping.json",
            mime="application/json",
            key="download_mapping_json",
            width="stretch",
        )
    st.warning(
        "⚠️ mapping.json 是还原钥匙（含原文与占位符对照）：请与脱敏文件分开保管，"
        "勿与脱敏 zip 一起外发"
    )

    # 返回按钮：同批文件重走流程（保留上传） / 全新任务（清空一切任务态）
    result_btn_cols = st.columns(2)
    with result_btn_cols[0]:
        if st.button("🔄 返回重新脱敏", width="stretch",
                     help="保留已上传文件与设置，回到检测页重新勾选"):
            # R1-B5 防御性清理已收敛至 _clear_task_state（正常流程
            # _run_masking finally 已删 tmp_dir，此处仅为兑底）
            _clear_task_state(clear_upload=False)
            st.rerun()
    with result_btn_cols[1]:
        if st.button("🆕 开始新任务", type="primary", width="stretch",
                     help="清空已上传文件与全部任务结果，保留设置与自定义敏感词"):
            _reset_task_state()
            st.rerun()


# ──────────────────────────────────────────────
# 标签页2：恢复还原
# ──────────────────────────────────────────────

def _render_restore_tab():
    """渲染恢复还原标签页"""

    # 检查是否有恢复结果需要展示
    if "restore_result" in st.session_state:
        restore_result = st.session_state["restore_result"]

        st.markdown(
            '<div class="success-banner">'
            '<h2>🎉 恢复完成！</h2>'
            f'<p>成功恢复 {restore_result["file_count"]} 个文件</p>'
            '</div>',
            unsafe_allow_html=True,
        )

        st.download_button(
            label="📥 下载恢复文件 (ZIP)",
            data=restore_result["zip_buffer"],
            file_name="restored_files.zip",
            mime="application/zip",
            type="primary",
            width="stretch",
        )

        if st.button("🔄 返回", width="stretch"):
            del st.session_state["restore_result"]
            st.rerun()
        return

    st.markdown("#### 🔓 恢复还原")
    st.caption("上传脱敏后的文件和映射表，将敏感信息还原为原始内容")

    st.markdown("---")

    # 选择恢复方式
    restore_method = st.radio(
        "选择恢复方式",
        options=["从历史记录恢复", "手动上传文件恢复"],
        horizontal=True,
    )

    tokens: Optional[dict] = None
    mapping: Optional[dict] = None

    if restore_method == "从历史记录恢复":
        mapping = _render_history_selector()
    else:
        mapping = _render_manual_upload()

    if mapping is None:
        return

    # 统一归约为 {token_str: original_str}（兼容 paths 段的原始 mapping 结构）
    tokens = _mapping_to_tokens(mapping)
    if not tokens:
        st.warning("该批次没有映射数据")
        return

    # 显示映射表预览
    st.markdown("#### 📋 映射表预览")
    preview_items = [
        f"- `{token}` → `{original}`"
        for token, original in list(tokens.items())[:20]
    ]
    st.markdown("\n".join(preview_items))
    if len(tokens) > 20:
        st.caption(f"... 共 {len(tokens)} 条映射")

    st.markdown("---")

    # 上传脱敏后的文件（I6 问题2：支持目录 masked zip 整批还原）
    st.markdown("#### 📁 上传脱敏后的文件")
    masked_files = st.file_uploader(
        "上传需要恢复的脱敏文件",
        type=["docx", "xlsx", "pptx", "pdf", "txt"],
        accept_multiple_files=True,
        label_visibility="collapsed",
    )
    restore_zip = st.file_uploader(
        "或上传目录脱敏 zip（.zip）整批还原",
        type=["zip"],
        key="restore_zip_upload",
        help="上传目录脱敏产物的 zip：内容与文件名/目录名一并还原，"
             "输出同结构目录的 zip",
    )
    if restore_zip is not None and masked_files:
        st.info("已同时上传单文件与目录 zip：本次按目录 zip 还原，单文件列表忽略")

    if not masked_files and restore_zip is None:
        st.info("📤 请上传需要恢复的脱敏文件")
        return

    # 显示已上传文件（单文件列表 + 目录 zip）
    shown_uploads = list(masked_files) + (
        [restore_zip] if restore_zip is not None else []
    )
    if shown_uploads:
        file_cols = st.columns(min(len(shown_uploads), 4))
        for i, f in enumerate(shown_uploads):
            with file_cols[i % len(file_cols)]:
                icon = _file_icon(Path(f.name).suffix)
                size_kb = f.size / 1024
                st.markdown(
                    f'<div class="file-card">'
                    f'<span class="icon">{icon}</span>'
                    f'<div><div class="name">{f.name}</div>'
                    f'<div class="size">{size_kb:.1f} KB</div></div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    # 执行恢复按钮
    if st.button("🔓 执行恢复", type="primary", width="stretch"):
        with st.spinner("正在恢复文件..."):
            if restore_zip is not None:
                _run_restore_zip(restore_zip, mapping)
            else:
                _run_restore(masked_files, mapping)


def _mapping_to_tokens(mapping_json) -> dict:
    """把 mapping 结构统一归约为 {token_str: original_str}。

    兼容三种形态：{"tokens": {token: {...original...}}}（新版含 paths 段）、
    裸 {token: original} dict、[{token, original}] 列表。
    """
    tokens = {}
    if isinstance(mapping_json, dict) and "tokens" in mapping_json:
        raw_tokens = mapping_json["tokens"]
    elif isinstance(mapping_json, dict):
        raw_tokens = mapping_json
    elif isinstance(mapping_json, list):
        raw_tokens = mapping_json
    else:
        return tokens
    if isinstance(raw_tokens, dict):
        for k, v in raw_tokens.items():
            if isinstance(v, dict) and "original" in v:
                tokens[k] = v["original"]
            elif isinstance(v, str):
                tokens[k] = v
    elif isinstance(raw_tokens, list):
        for item in raw_tokens:
            if isinstance(item, dict) and "token" in item and "original" in item:
                tokens[item["token"]] = item["original"]
    return tokens


def _render_history_selector() -> Optional[dict]:
    """从历史记录中选择批次，返回原始 mapping dict（含 tokens 与可选 paths 段）"""
    records = _load_history()

    if not records:
        st.warning("⚠️ 暂无历史记录，请先执行脱敏操作或选择手动上传方式")
        return None

    st.markdown("#### 📚 历史记录")

    # 构建选择列表
    options = []
    for r in reversed(records):  # 最新的在前
        name_part = f" | {r.batch_name}" if r.batch_name else ""
        options.append(
            f"{r.batch_id}{name_part} | {r.created_at[:19]} | "
            f"{r.file_count}个文件 | {r.mask_count}项脱敏"
        )

    selected_idx = st.selectbox(
        "选择批次",
        options=range(len(options)),
        format_func=lambda i: options[i],
    )

    # 获取选中的记录（倒序索引）
    record = records[-(selected_idx + 1)]

    # 解析映射数据：新版优先批次目录 mapping.json；旧版回退记录内明文
    mapping_json = None
    if record.mapping_path and Path(record.mapping_path).exists():
        try:
            with open(record.mapping_path, "r", encoding="utf-8") as f:
                mapping_json = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            st.error(f"批次映射表读取失败: {e}")
            return None
    elif record.mapping_data:
        try:
            mapping_json = json.loads(record.mapping_data)
        except (json.JSONDecodeError, TypeError) as e:
            st.error(f"映射表解析失败: {e}")
            return None

    if mapping_json is None:
        st.error("映射表缺失：批次目录 mapping.json 不存在且记录无映射数据")
        return None

    tokens = _mapping_to_tokens(mapping_json)
    if not tokens:
        st.warning("该批次没有映射数据")
        return None

    # 显示批次详情
    detail_cols = st.columns(4)
    with detail_cols[0]:
        st.metric("批次ID", record.batch_id)
    with detail_cols[1]:
        st.metric("批次名称", record.batch_name or "未命名")
    with detail_cols[2]:
        st.metric("文件数量", record.file_count)
    with detail_cols[3]:
        st.metric("脱敏项数", record.mask_count)

    return mapping_json


def _render_manual_upload() -> Optional[dict]:
    """手动上传映射表，返回原始 mapping dict（含 tokens 与可选 paths 段）"""
    st.markdown("#### 📋 上传映射表")
    st.caption("请上传脱敏时生成的 mapping.json 文件")

    mapping_file = st.file_uploader(
        "上传映射表 JSON",
        type=["json"],
        label_visibility="collapsed",
    )

    if not mapping_file:
        st.info("📤 请上传映射表 JSON 文件")
        return None

    try:
        content = mapping_file.read().decode("utf-8")
        mapping_json = json.loads(content)
        tokens = _mapping_to_tokens(mapping_json)
        if not tokens:
            st.warning("映射表为空")
            return None
        st.success(f"✅ 成功加载 {len(tokens)} 条映射")
        return mapping_json
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as e:
        st.error(f"映射表解析失败: {e}")
        return None


def _run_restore(masked_files, mapping: dict):
    """执行恢复流程。

    mapping 为原始 mapping dict：tokens 段用于内容还原（保留 kind 字段，
    数字单元格可还原为数值，R1-A2）；paths 段（PathMasker.load_paths）
    用于把脱敏后的文件名还原为原主名。
    """
    if isinstance(mapping, dict) and isinstance(mapping.get("tokens"), dict):
        raw_tokens = mapping["tokens"]           # 新版：{token: {original, kind, ...}}
    elif isinstance(mapping, list):
        raw_tokens = {
            item.get("token", ""): item
            for item in mapping
            if isinstance(item, dict) and "token" in item and "original" in item
        }
    else:
        raw_tokens = _mapping_to_tokens(mapping)  # 旧版：{token: original_str}
    tokens = _mapping_to_tokens(mapping)          # 预览/存在性检查用
    paths = PathMasker.load_paths(mapping)

    tmp_dir = Path(tempfile.mkdtemp())
    try:
        output_dir = tmp_dir / "restored"
        output_dir.mkdir(parents=True, exist_ok=True)

        # 保存上传文件到临时目录
        saved_paths = []
        for f in masked_files:
            save_path = tmp_dir / f.name
            with open(save_path, "wb") as fp:
                fp.write(f.read())
            saved_paths.append(save_path)

        # 逐文件恢复
        output_files: List[Path] = []
        for file_path in saved_paths:
            suffix = file_path.suffix.lower()
            if suffix not in {".docx", ".xlsx", ".pptx", ".pdf", ".txt"}:
                st.warning(f"跳过不支持的文件类型: {file_path.name}")
                continue

            # 输出名：paths 段有匹配记录（rel_new 文件名）→ 还原原主名；否则 _restored 后缀
            pm_record = next(
                (m for m in paths
                 if m.rel_new == file_path.name or Path(m.rel_new).name == file_path.name),
                None,
            )
            if pm_record is not None:
                out_name = pm_record.old_name
            else:
                out_name = f"{file_path.stem}_restored{suffix}"

            output_path = output_dir / out_name
            result = _unmask_file(file_path, output_path, raw_tokens)
            if result:
                output_files.append(result)

        if not output_files:
            st.error("❌ 未能恢复任何文件")
            return

        # 打包为 ZIP
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for fp in output_files:
                zf.write(fp, fp.name)

        zip_bytes = zip_buffer.getvalue()

        # 存入 session_state
        st.session_state["restore_result"] = {
            "zip_buffer": zip_bytes,
            "file_count": len(output_files),
        }

        st.rerun()
    finally:
        # 临时目录用完即删
        shutil.rmtree(tmp_dir, ignore_errors=True)



    # 展示结果（rerun 后会到达下面的代码）
    # 注意：由于上面已经 rerun，下面的代码不会执行
    # 结果展示在 _render_restore_tab 中检查 restore_result


def _run_restore_zip(zip_file, mapping: dict):
    """目录 masked zip 整批还原（I6 问题2）。

    流程：安全解压（穿越/炸弹防护，与脱敏侧同一套）→ docx/xlsx 内容还原
    到镜像树（restore_file_content，kind=number 数值还原）→ PathMasker
    .unmask_tree 自底向上还原文件名/目录名（paths 段）→ 重新打包 zip。
    mapping.json 不进入还原产物（含明文映射，不应随交付物流转）。
    """
    raw_tokens = {}
    if isinstance(mapping, dict) and isinstance(mapping.get("tokens"), dict):
        raw_tokens = mapping["tokens"]  # {token: {original, kind, ...}}
    else:
        raw_tokens = _mapping_to_tokens(mapping)  # 旧形态：{token: original_str}
    paths = PathMasker.load_paths(mapping)

    tmp_dir = Path(tempfile.mkdtemp())
    try:
        try:
            tree = _safe_unzip(zip_file.read(), tmp_dir)
        except (ValueError, zipfile.BadZipFile, OSError) as e:
            st.error(f"目录 zip 解压失败：{e}")
            return

        restored_root = tmp_dir / "restored"
        restored_root.mkdir(parents=True, exist_ok=True)

        # 1) 内容还原：docx/xlsx 逐文件到镜像树（restore_file_content
        #    契约：输入文件不被修改，因此写副本而非原地改）
        from mask_tool.adapters.restore import restore_file_content

        token_map = {
            t: (v if isinstance(v, dict) else {"original": v})
            for t, v in raw_tokens.items()
        }
        restored_files = 0
        for f in sorted(tree.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(tree)
            if rel.as_posix() == "mapping.json":
                continue  # 映射表不进还原产物
            dest = restored_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if f.suffix.lower() in (".docx", ".xlsx"):
                try:
                    restore_file_content(f, dest, token_map)
                    restored_files += 1
                except Exception as e:
                    st.warning(f"内容还原失败（原样拷入）: {rel.as_posix()} ({e})")
                    shutil.copy2(f, dest)
            else:
                shutil.copy2(f, dest)

        # 2) 名字还原：paths 段自底向上（先子后父）改回原名
        if paths:
            pm = PathMasker(None, None, None)  # 还原仅需改名能力
            res = pm.unmask_tree(restored_root, paths)
            for w in res.warnings:
                st.warning(w)

        # 3) 重新打包 zip
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for fp in sorted(p for p in restored_root.rglob("*") if p.is_file()):
                zf.write(fp, fp.relative_to(restored_root).as_posix())

        st.session_state["restore_result"] = {
            "zip_buffer": zip_buffer.getvalue(),
            "file_count": restored_files,
        }
        st.rerun()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ──────────────────────────────────────────────
# 检测流程
# ──────────────────────────────────────────────

def _run_detection(uploaded_files, mode: str, ner_enabled: bool,
                   manual_words: Optional[List[str]] = None,
                   manual_only: bool = False,
                   zip_file=None):
    """执行检测流程：正文检测 + 文件主名检测（source="path"，可勾选确认）

    R1-A1：正文检测面与 adapter 处理面同源（detect_file_results，
    docx walker 全部件 + xlsx 全部件含数字合成项）；
    R1-B5：进入新一轮检测前清理上一轮残留的上传临时目录（含敏感信息副本）。

    I6：manual_words 注入 Detector 最高优先级通道（source="manual"，
    置信度 0.95，与词库同档）；manual_only=True 时自动检测通道
    （NER/正则/词库）全部关闭，仅剩手动词。手动词条目置顶展示。

    I6 问题2：zip_file 提供时按目录任务处理——安全解压（穿越/炸弹防护）
    后递归检测 docx/xlsx（含文件名），屏蔽类型警告并登记不进产物。
    """
    # 新一轮检测：旧勾选/学习集索引已失效，先行重置（防止残留索引
    # 误读新结果集——learn_set 旧索引会把不相关的词写进词库文件）
    st.session_state.pop("user_selections", None)
    st.session_state.pop("learn_set", None)
    # 加载配置
    cfg = _load_config(mode)
    cfg.ner.enabled = ner_enabled and not manual_only

    pipeline = Pipeline(
        cfg, manual_words=manual_words,
        auto_detect_enabled=not manual_only,
    )

    # R1-B5：清理旧检测轮的上传临时目录（用户只检测不点脱敏时不再永久残留）
    old_tmp = st.session_state.get("tmp_dir")
    if old_tmp:
        shutil.rmtree(old_tmp, ignore_errors=True)
        for key in ("tmp_dir", "saved_paths"):
            st.session_state.pop(key, None)

    # 保存上传文件到临时目录（zip 任务：安全解压为目录树）
    tmp_dir = Path(tempfile.mkdtemp())
    saved_paths = []
    task_kind = "files"
    zip_tree_root: Optional[str] = None
    zip_blocked: List[str] = []
    if zip_file is not None:
        task_kind = "zip"
        try:
            tree_root = _safe_unzip(zip_file.read(), tmp_dir)
        except (ValueError, zipfile.BadZipFile, OSError) as e:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            st.error(f"目录压缩包解压失败：{e}")
            return
        classified = _classify_tree_files(tree_root)
        for p in classified["blocked"]:
            zip_blocked.append(p.relative_to(tree_root).as_posix())
        saved_paths = classified["docs"]
        zip_tree_root = str(tree_root)
        if not saved_paths:
            st.warning("压缩包内没有可处理的 docx/xlsx 文件")
    else:
        for f in uploaded_files:
            save_path = tmp_dir / f.name
            with open(save_path, "wb") as fp:
                fp.write(f.read())
            saved_paths.append(save_path)

    # 文件名脱敏器（检测阶段仅用其 detect_name；与后续脱敏共享组件）
    token_gen = getattr(pipeline, "token_gen", None) or getattr(
        pipeline.masker, "token_gen"
    )
    pm = PathMasker(pipeline.detector, pipeline.policy, token_gen)

    # 逐文件检测
    all_results: List[DetectionResult] = []
    file_results: Dict[str, List[DetectionResult]] = {}

    from mask_tool.adapters.extract import detect_file_results

    for file_path in saved_paths:
        try:
            suffix = file_path.suffix.lower()
            if suffix in BLOCKED_EXTS:
                st.error(
                    f"跳过 {file_path.name}：{BLOCKED_EXTS.get(suffix)}"
                )
                continue
            if suffix not in SUPPORTED_MASK_EXTS:
                st.warning(f"跳过不支持的文件类型: {file_path.name}")
                continue

            # R1-A1：与 adapter 处理面同源的检测（已含 policy 决策）
            results: List[DetectionResult] = list(
                detect_file_results(file_path, pipeline.detector, pipeline.policy)
            )
            # 文件主名检测：location.file 填全路径，避免重名上传文件错配
            name_results = pm.detect_name(file_path.name, str(file_path))
            if name_results:
                results.extend(pipeline.policy.apply(name_results))

            all_results.extend(results)
            # zip 任务按相对路径登记（子目录下同名文件不冲破）；单文件任务用文件名
            if task_kind == "zip":
                file_key = file_path.relative_to(Path(zip_tree_root)).as_posix()
            else:
                file_key = file_path.name
            file_results[file_key] = results
        except Exception as e:
            st.error(f"检测 {file_path.name} 时出错: {e}")

    # 跨文件去重
    all_results = _dedup_results(all_results)

    # I6：手动词条目置顶（稳定排序：manual 在前，其余保持原序）
    if manual_words:
        all_results.sort(key=lambda r: r.source != "manual")

    # 存入 session_state
    st.session_state["detection_results"] = all_results
    st.session_state["file_results"] = file_results
    st.session_state["tmp_dir"] = str(tmp_dir)
    st.session_state["saved_paths"] = [str(p) for p in saved_paths]
    st.session_state["task_kind"] = task_kind
    st.session_state["zip_tree_root"] = zip_tree_root
    st.session_state["zip_blocked_files"] = zip_blocked

    st.success(f"✅ 检测完成！共发现 **{len(all_results)}** 项敏感信息")
    st.rerun()


# ──────────────────────────────────────────────
# 脱敏执行流程
# ──────────────────────────────────────────────

def _run_masking(
    uploaded_files,
    selected_indices: List[int],
    all_results: List[DetectionResult],
    mode: str,
    ner_enabled: bool,
    irreversible: bool,
    learn_words: bool,
    batch_id: str,
    batch_name: str,
    mask_filenames: bool = True,
    manual_words: Optional[List[str]] = None,
    manual_only: bool = False,
):
    """执行脱敏流程，结果持久化到 session_state。

    - 输出与 mapping.json 写入持久批次目录 ~/.mask-tool/batches/<batch_id>/，
      history.json 仅存批次元数据（不再保存完整映射明文）；
    - mask_filenames=True 时对输出文件主名执行同套检测替换（Token 与内容共享）；
    - 上传临时目录用完即删（finally 清理）；
    - I6：manual_words/manual_only 必须与检测时传同一套（本函数由检测页控件
      取值注入，与 _run_detection 同源），否则会出现"检测看到了但脱敏没脱"。
    """
    # 加载配置
    cfg = _load_config(mode)
    cfg.ner.enabled = ner_enabled and not manual_only

    pipeline = Pipeline(
        cfg, batch_id=batch_id, manual_words=manual_words,
        auto_detect_enabled=not manual_only,
    )

    if irreversible:
        from mask_tool.core.masker import Masker
        from mask_tool.core.tokenizer import TokenGenerator
        pipeline.masker = Masker(TokenGenerator(), irreversible=True)

    # 获取确认的检测结果
    confirmed_results = [all_results[i] for i in selected_indices]
    # 设置状态为 AUTO_MASK
    for r in confirmed_results:
        r.status = DetectionStatus.AUTO_MASK
    confirmed_originals = {r.text for r in confirmed_results}

    # 处理学习词
    learn_set = st.session_state.get("learn_set", set())
    learned_words: Dict[str, List[str]] = {}
    for i in learn_set:
        if i < len(all_results):
            r = all_results[i]
            cat = r.text_type.value
            if cat not in learned_words:
                learned_words[cat] = []
            if r.text not in learned_words[cat]:
                learned_words[cat].append(r.text)

    # 任务分流（I6 问题2）：zip 任务复用检测阶段已解压的目录树；
    # 单文件任务沿用上传文件落盘（检测阶段已落盘的复用）
    task_kind = st.session_state.get("task_kind", "files")
    zip_tree_root: Optional[Path] = None
    tmp_dir = Path(st.session_state.get("tmp_dir", tempfile.mkdtemp()))
    saved_paths = []
    if task_kind == "zip":
        zip_tree_root = Path(st.session_state["zip_tree_root"])
        saved_paths = [Path(p) for p in st.session_state.get("saved_paths", [])]
    else:
        for f in uploaded_files:
            save_path = tmp_dir / f.name
            if not save_path.exists():
                with open(save_path, "wb") as fp:
                    fp.write(f.read())
            saved_paths.append(save_path)

    # M3 防线一（R3-A3）：mask 前预扫描输入中已出现的 token 样式串，
    # 编号让位防撞号（撞号会导致 unmask 时敏感实体错误注入），
    # 与 CLI mask 的 pipeline.prepare 对齐
    pipeline.prepare(saved_paths)

    # 输出目录：持久批次目录（mapping.json 留存于此）
    output_dir = BATCHES_DIR / batch_id
    output_dir.mkdir(parents=True, exist_ok=True)

    # 文件名脱敏器：与 pipeline 共享检测/策略/Token 组件（同一批次 Token 一致）
    token_gen = getattr(pipeline, "token_gen", None) or getattr(
        pipeline.masker, "token_gen"
    )
    pm = PathMasker(
        pipeline.detector, pipeline.policy, token_gen, irreversible=irreversible,
    )

    # 逐文件脱敏（zip 任务写到镜像树保持相对结构，产物打回 zip）
    output_files: List[Path] = []
    mapping_path = output_dir / "mapping.json"
    out_tree = tmp_dir / "out_tree"  # zip 任务的镜像树根（非 zip 任务不用）
    try:
        for file_path in saved_paths:
            suffix = file_path.suffix.lower()
            if suffix in BLOCKED_EXTS:
                st.error(
                    f"跳过 {file_path.name}：{BLOCKED_EXTS.get(suffix)}"
                )
                continue
            if suffix not in SUPPORTED_MASK_EXTS:
                st.warning(f"跳过不支持的文件类型: {file_path.name}")
                continue
            try:
                if task_kind == "zip":
                    # 目录模式（对齐 CLI _mask_directory_tree）：产物保持原名
                    # （不加 _masked 后缀），文件名/目录名统一交给 mask_tree
                    rel = file_path.relative_to(zip_tree_root)
                    result_path = _do_mask_file(
                        file_path, out_tree / rel.parent, pipeline,
                        confirmed_results, output_name=file_path.name,
                    )
                else:
                    result_path = _do_mask_file(
                        file_path, output_dir, pipeline, confirmed_results,
                    )
                if not result_path:
                    continue
                if task_kind != "zip" and mask_filenames:
                    # 单文件：主名脱敏（不改扩展名），Token 与正文共享；
                    # R1-B2：确认语义下勾选即放行，statuses 含 SUGGEST
                    new_path = pm.mask_filename(
                        result_path,
                        allowed_originals=confirmed_originals,
                        statuses=frozenset({
                            DetectionStatus.AUTO_MASK, DetectionStatus.SUGGEST_MASK,
                        }),
                    )
                    if new_path is not None:
                        result_path = new_path
                    for w in pm.last_result.warnings:
                        st.warning(w)
                output_files.append(result_path)
            except Exception as e:
                st.error(f"脱敏 {file_path.name} 时出错: {e}")

        # zip 任务：非文档文件原样拷贝 + 空目录迁移 + 镜像树改名（mask_tree）
        if task_kind == "zip":
            classified = _classify_tree_files(zip_tree_root)
            for p in classified["others"]:
                rel = p.relative_to(zip_tree_root)
                dest = out_tree / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest)
            for d in sorted(
                {x for x in zip_tree_root.rglob("*") if x.is_dir()},
                key=lambda x: len(x.parts), reverse=True,
            ):
                rel = d.relative_to(zip_tree_root)
                (out_tree / rel).mkdir(parents=True, exist_ok=True)
            out_tree.mkdir(parents=True, exist_ok=True)
            if mask_filenames:
                tree_result = pm.mask_tree(
                    out_tree,
                    allowed_originals=confirmed_originals,
                    statuses=frozenset({
                        DetectionStatus.AUTO_MASK, DetectionStatus.SUGGEST_MASK,
                    }),
                )
                for w in tree_result.warnings:
                    st.warning(w)
            # 收集改名后的产物树（屏蔽类型已不在 out_tree 内）
            output_files = sorted(
                p for p in out_tree.rglob("*") if p.is_file()
            )

        # 保存映射表（tokens 段）+ 并入文件名改名的 paths 段
        pipeline.save_mapping(mapping_path)
        try:
            with open(mapping_path, "r", encoding="utf-8") as f:
                mapping_dict = json.load(f)
            mapping_dict = PathMasker.merge_into_mapping(mapping_dict, pm.export_mappings())
            with open(mapping_path, "w", encoding="utf-8") as f:
                json.dump(mapping_dict, f, ensure_ascii=False, indent=2)
        except Exception as e:
            st.warning(f"映射表 paths 段写入失败: {e}")

        # 学习新词
        if learn_words and learned_words:
            _save_learned_words(learned_words, cfg)

        # ── 生成结果并持久化 ──
        if output_files:
            # 下载规则（I6 问题1）：zip 任务/多文件 → zip 压缩包；
            # 单文件 → 直接下载脱敏后的同名文件
            download_kind = (
                "zip" if (task_kind == "zip" or len(output_files) > 1) else "file"
            )
            zip_bytes = b""
            file_bytes = b""
            file_name = ""
            if download_kind == "zip":
                zip_buffer = BytesIO()
                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                    if task_kind == "zip":
                        # 目录任务：同结构镜像树打回 zip（arcname 为相对路径）
                        for fp in output_files:
                            zf.write(fp, fp.relative_to(out_tree).as_posix())
                    else:
                        # 多文件任务：文件名已同步替换（fp 即改名后的新路径）
                        for fp in output_files:
                            zf.write(fp, fp.name)
                    # 同时包含映射表（zip 任务另提供单独下载，这里一并放入便于
                    # 整批还原；单文件任务保持原有行为）
                    if mapping_path.exists():
                        zf.write(mapping_path, "mapping.json")
                zip_bytes = zip_buffer.getvalue()
            else:
                file_bytes = output_files[0].read_bytes()
                file_name = output_files[0].name

            # 读取映射数据
            if mapping_path.exists():
                with open(mapping_path, "r", encoding="utf-8") as f:
                    mapping_data = f.read()
            else:
                mapping_data = "{}"

            # 构建映射列表（用于展示）
            mappings = []
            for m in pipeline.masker.get_mappings():
                mappings.append({
                    "token": m.token,
                    "original": m.original,
                    "type_label": TYPE_LABELS.get(m.text_type, m.text_type.value),
                    "confidence": m.confidence,
                })

            # 持久化到 session_state
            st.session_state["mask_result"] = {
                "download_kind": download_kind,
                "zip_buffer": zip_bytes,
                "file_bytes": file_bytes,
                "file_name": file_name,
                "task_kind": task_kind,
                "mapping_data": mapping_data,
                "mappings": mappings,
                "output_files": [str(p) for p in output_files],
                "confirmed_count": len(confirmed_results),
                "batch_id": batch_id,
                "batch_name": batch_name,
            }

            # 保存批次历史（仅元数据；映射留在批次目录 mapping.json）
            history_record = BatchRecord(
                batch_id=batch_id,
                batch_name=batch_name or "",
                created_at=datetime.now().isoformat(),
                file_count=len(output_files),
                mask_count=len(confirmed_results),
                mapping_data="",
                mapping_path=str(mapping_path),
            )
            _add_history(history_record)

            st.rerun()
        else:
            st.warning("⚠️ 未能生成脱敏文件")
    finally:
        # 上传临时目录用完即删（结果已读入内存/批次目录，不依赖 tmp）
        shutil.rmtree(tmp_dir, ignore_errors=True)
        for key in ("tmp_dir", "saved_paths"):
            st.session_state.pop(key, None)


def _save_learned_words(learned: dict, config: MaskConfig):
    """将学习到的词追加到词库文件"""
    lexicon_path = Path(config.lexicon_path)
    if not lexicon_path.exists():
        return

    with open(lexicon_path, "r", encoding="utf-8") as f:
        existing = yaml.safe_load(f) or {}

    new_count = 0
    for category, words in learned.items():
        if category not in existing:
            existing[category] = []
        for word in words:
            if word not in existing[category]:
                existing[category].append(word)
                new_count += 1

    if new_count > 0:
        with open(lexicon_path, "w", encoding="utf-8") as f:
            yaml.dump(existing, f, allow_unicode=True, default_flow_style=False)


# ──────────────────────────────────────────────
# 主应用
# ──────────────────────────────────────────────

def main():
    _inject_css()

    # 侧边栏
    mode, ner_enabled, irreversible, learn_words = render_sidebar()

    # 标题（紧凑行：标题 + 副标题同行）
    st.markdown(
        '<div class="main-head"><h1>🔒 文件脱敏</h1>'
        '<span class="sub">上传 → 智能检测 → 交互确认 → 一键脱敏下载</span></div>',
        unsafe_allow_html=True,
    )

    # 标签页
    tab1, tab2 = st.tabs(["🔒 脱敏处理", "🔓 恢复还原"])

    with tab1:
        _render_masking_tab(mode, ner_enabled, irreversible, learn_words)

    with tab2:
        _render_restore_tab()


# ──────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────

def run_web():
    """CLI 入口：启动 Streamlit Web 界面

    用法：
        mask-tool-web              # 默认启动
        mask-tool-web --port 8080  # 指定端口
    """
    import sys
    import subprocess

    app_file = Path(__file__).resolve()
    args = [sys.executable, "-m", "streamlit", "run", str(app_file)]
    # 透传额外参数（如 --port, --server.headless 等）
    if len(sys.argv) > 1:
        args.extend(sys.argv[1:])
    subprocess.run(args)


if __name__ == "__main__":
    main()
