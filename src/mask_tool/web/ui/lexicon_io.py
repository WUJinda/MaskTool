# -*- coding: utf-8 -*-
"""词库 IO：定位/初始化用户词库、合并词条、类别创建、CSV 导入导出。

白名单 IO（R9）：检测排除词（config/whitelist.yaml）的读写同在本模块，
与词库共用锚点链定位策略。
"""
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import streamlit as st
import yaml

from mask_tool.models.detection import DetectionType

from .labels import TYPE_LABELS

# 白名单文件相对路径（与 config/whitelist.yaml 默认配置一致）
WHITELIST_RELPATH = "config/whitelist.yaml"

def _merge_words_into_lexicon(pending: Dict[str, List[str]]) -> Tuple[int, int]:
    """把 {类别: [词条]} 合并写入用户词库（去重）；返回 (新增, 跳过重复)。"""
    p = _ensure_user_lexicon()
    if p is None:
        return 0, 0
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    added = dup = 0
    for cat, words in pending.items():
        existing = set(data.get(cat, []))
        fresh = []
        for w in words:
            w = w.strip()
            if not w:
                continue
            if w in existing:
                dup += 1
            else:
                existing.add(w)
                fresh.append(w)
                added += 1
        if fresh:
            data[cat] = list(data.get(cat, [])) + fresh
    try:
        with open(p, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    except OSError:
        return 0, dup
    return added, dup


def _create_lexicon_category(name: str) -> Tuple[bool, str]:
    """在用户词库中新建空类别；成功/重名/失败均返回说明。"""
    p = _ensure_user_lexicon()
    if p is None:
        return False, "无法定位或创建用户词库文件（config/lexicon.yaml）"
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if name in data:
            return False, f"类别「{name}」已存在"
        data[name] = []
        with open(p, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        return True, f"已创建类别「{name}」"
    except OSError as e:
        return False, f"写入词库失败：{e}"


def _export_lexicon_csv_bytes() -> Tuple[bytes, int]:
    """导出词库为 CSV（UTF-8 BOM，两列 category,word）；返回 (字节, 词条数)。"""
    import csv
    import io

    data = _get_lexicon_data() or {}
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["category", "word"])
    count = 0
    for cat, words in data.items():
        for w in words:
            writer.writerow([cat, w])
            count += 1
    return ("\ufeff" + buf.getvalue()).encode("utf-8"), count


def _import_lexicon_csv_text(text: str) -> str:
    """导入 CSV 词库文本（兼容中英文表头/类别名），合并去重；返回结果说明。"""
    import csv
    import io

    text = text.lstrip("\ufeff")
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return "❌ 文件为空或无法解析"

    # 表头容错：首行若是 category/类别 表头则跳过；无表头也兼容
    header = [c.strip().lower() for c in rows[0]]
    if header[:2] == ["category", "word"] or header[:2] == ["类别", "词条"]:
        rows = rows[1:]

    # 类别名映射：中文标签（去 emoji）与英文值均归一到词库 key
    alias = {}
    for t in DetectionType:
        alias[t.value.lower()] = t.value
        alias[TYPE_LABELS[t].split(" ", 1)[-1]] = t.value  # 去 emoji 后的中文标签

    pending: Dict[str, List[str]] = {}
    skipped_unknown = 0
    for r in rows:
        if len(r) < 2:
            continue
        cat_raw, word = r[0].strip(), r[1].strip()
        if not word:
            continue
        cat = alias.get(cat_raw.lower(), alias.get(cat_raw))
        if cat is None:
            cat = "custom"  # 未知类别归入自定义
            skipped_unknown += 1
        pending.setdefault(cat, []).append(word)

    added, dup = _merge_words_into_lexicon(pending)
    if added == 0 and dup == 0:
        return "❌ 无法定位或创建用户词库文件（config/lexicon.yaml）"

    parts = [f"✅ 已导入 {added} 条"]
    if dup:
        parts.append(f"跳过重复 {dup} 条")
    if skipped_unknown:
        parts.append(f"未知类别 {skipped_unknown} 条已归入「自定义」")
    return "，".join(parts)


def _export_lexicon_csv_to_file() -> str:
    """导出词库 CSV 到「保存路径」文件夹（桌面交付：不用浏览器下载）。"""
    from datetime import datetime

    from mask_tool.core.app_settings import get_save_dir

    try:
        data, count = _export_lexicon_csv_bytes()
        out_dir = Path(get_save_dir())
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / f"mask-tool-词库-{datetime.now().strftime('%Y%m%d')}.csv"
        p.write_bytes(data)
        return f"✅ 已导出 {count} 条词条到 {p}"
    except OSError as e:
        return f"❌ 导出失败：{e}"


def _find_lexicon_file() -> Optional[Path]:
    """按优先级定位词库文件：用户词库 -> 示例词库。

    锚点链（CWD -> exe 目录 -> 源码树根 -> 打包内置）由
    core/config_loader.runtime_anchor_dirs 统一提供，修复桌面/bat
    启动时 cwd 不在项目根导致 ``config/lexicon.yaml`` 相对路径落空、
    写入报 FileNotFoundError 的问题（2026-09-20）。
    """
    from mask_tool.core.config_loader import find_data_file

    return (
        find_data_file("config/lexicon.yaml")
        or find_data_file("config/sample_lexicon.yaml")
    )


def _ensure_user_lexicon() -> Optional[Path]:
    """定位用户词库，不存在则建目录并初始化；失败时提示并返回 None。

    初始化复制源优先级：打包内置 lexicon.yaml（frozen 出厂副本）->
    sample_lexicon.yaml -> 空文件。所有写入均先 ``mkdir(parents=True)``，
    避免 config/ 目录缺失时 ``write_text`` 抛 FileNotFoundError。
    """
    from mask_tool.core.config_loader import (
        LEXICON_RELPATH, SAMPLE_LEXICON_RELPATH, find_data_file,
        resolve_user_lexicon_path,
    )

    lexicon_path = resolve_user_lexicon_path()
    if lexicon_path.exists():
        return lexicon_path
    try:
        lexicon_path.parent.mkdir(parents=True, exist_ok=True)
        source = (
            find_data_file(LEXICON_RELPATH)   # frozen 内置副本优先（保留出厂词条）
            or find_data_file(SAMPLE_LEXICON_RELPATH)
        )
        if source and source != lexicon_path:
            shutil.copy2(source, lexicon_path)
        else:
            lexicon_path.write_text("", encoding="utf-8")
    except OSError as exc:
        st.error(
            f"❌ 无法创建词库文件：{lexicon_path}（{exc}）。"
            f"请检查目录权限或将软件放到可写位置后重试。"
        )
        return None
    return lexicon_path

def _get_lexicon_data() -> Optional[Dict[str, List[str]]]:
    """读取词库完整数据（分类别返回词条列表）"""
    try:
        p = _find_lexicon_file()
        if p:
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

    # 确保用户词库存在（锄点链解析 + 自动建目录/初始化）
    lexicon_path = _ensure_user_lexicon()
    if lexicon_path is None:
        return

    # 读取现有词库
    try:
        with open(lexicon_path, "r", encoding="utf-8") as f:
            existing = yaml.safe_load(f) or {}
    except OSError as exc:
        st.error(f"❌ 无法读取词库文件 {lexicon_path}：{exc}")
        return

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
        try:
            with open(lexicon_path, "w", encoding="utf-8") as f:
                yaml.dump(existing, f, allow_unicode=True, default_flow_style=False)
        except OSError as exc:
            st.error(f"❌ 词库保存失败 {lexicon_path}：{exc}")
            return
        st.success(f"✅ 成功添加 {added} 条词条到 [{actual_cat}]")
        st.caption(f"词库文件：{lexicon_path}")
    else:
        st.info("ℹ️ 所有词条已存在于词库中")


# ──────────────────────────────────────────────
# 白名单 IO（R9）：config/whitelist.yaml 的 whitelist 列表读写
#
# 用途：白名单内的词不会被检测识别（词典/正则/NER 三路均过滤），
# 供「自动检测误报永久排除」与设置弹窗白名单维护两个入口共用。
# 检测每次 _load_config 重新读文件，写入后点「重新检测」即生效。
# ──────────────────────────────────────────────


def _resolve_whitelist_path() -> Path:
    """白名单文件规范位置（与 resolve_user_lexicon_path 同策略）。

    锚点链上已存在的 whitelist.yaml 直接复用（CWD 自建 -> exe 便携
    目录 -> 源码树），跳过 frozen 内置只读副本（_MEIPASS，出厂副本
    仅作首次初始化复制源）；全部不存在时锚定可写锚点新建。
    """
    from mask_tool.core.config_loader import (
        runtime_anchor_dirs, writable_anchor_dir,
    )

    rel = Path(WHITELIST_RELPATH)
    meipass = getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", "")
    for anchor in runtime_anchor_dirs():
        if meipass and anchor == Path(meipass):
            continue
        candidate = anchor / rel
        if candidate.exists():
            return candidate.resolve()
    return (writable_anchor_dir() / rel).resolve()


def _get_whitelist() -> List[str]:
    """读白名单词条；文件不存在/损坏返回 []。"""
    try:
        p = _resolve_whitelist_path()
        if p and p.exists():
            with open(p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            words = data.get("whitelist", [])
            if isinstance(words, list):
                return [str(w) for w in words if str(w).strip()]
    except Exception:
        pass
    return []


def _merge_words_into_whitelist(words: List[str]) -> Tuple[int, int]:
    """合并追加白名单词条（去重保序）；返回 (新增, 跳过重复)。

    文件不存在时新建；顶层结构保持 {whitelist: [...]}，
    与出厂 whitelist.yaml 一致（注释不保留，重写为纯列表结构）。
    """
    p = _resolve_whitelist_path()
    existing: List[str] = []
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            wl = data.get("whitelist", [])
            if isinstance(wl, list):
                existing = [str(w) for w in wl if str(w).strip()]
        except (OSError, yaml.YAMLError):
            existing = []
    seen = set(existing)
    added = dup = 0
    for w in words:
        w = str(w).strip()
        if not w:
            continue
        if w in seen:
            dup += 1
        else:
            seen.add(w)
            existing.append(w)
            added += 1
    if added:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                yaml.safe_dump(
                    {"whitelist": existing}, f,
                    allow_unicode=True, sort_keys=False,
                )
        except OSError:
            return 0, dup
    return added, dup


def _remove_whitelist_words(words: List[str]) -> int:
    """从白名单删除指定词条；返回实际删除数。文件缺失返回 0。"""
    p = _resolve_whitelist_path()
    if not p.exists():
        return 0
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        wl = data.get("whitelist", [])
        if not isinstance(wl, list):
            return 0
        targets = {str(w).strip() for w in words if str(w).strip()}
        kept = [w for w in wl if str(w).strip() not in targets]
        removed = len(wl) - len(kept)
        if removed:
            with open(p, "w", encoding="utf-8") as f:
                yaml.safe_dump(
                    {"whitelist": kept}, f,
                    allow_unicode=True, sort_keys=False,
                )
        return removed
    except (OSError, yaml.YAMLError):
        return 0


def _get_lexicon_info() -> Optional[dict]:
    """获取词库统计信息（优先读取用户词库 lexicon.yaml）"""
    try:
        p = _find_lexicon_file()
        if p:
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

    # 确定用户词库路径（锄点链解析 + 自动建目录/初始化）
    lexicon_path = _ensure_user_lexicon()
    if lexicon_path is None:
        return

    # 读取现有词库
    try:
        with open(lexicon_path, "r", encoding="utf-8") as f:
            existing = yaml.safe_load(f) or {}
    except OSError as exc:
        st.error(f"❌ 无法读取词库文件 {lexicon_path}：{exc}")
        return

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
        try:
            with open(lexicon_path, "w", encoding="utf-8") as f:
                yaml.dump(existing, f, allow_unicode=True, default_flow_style=False)
        except OSError as exc:
            st.error(f"❌ 词库保存失败 {lexicon_path}：{exc}")
            return
        st.success(f"✅ 成功导入 {added_count} 条新词条到词库")
        st.caption(f"词库文件：{lexicon_path}")
    else:
        st.info("ℹ️ 没有新词条需要导入（全部已存在）")

