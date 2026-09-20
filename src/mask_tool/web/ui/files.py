# -*- coding: utf-8 -*-
"""文件级工具：目录 zip 安全解压/分类、文档文本抽取、图标与置信度分级。"""
import os
import shutil
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Dict, List

from .labels import BLOCKED_EXTS, SUPPORTED_MASK_EXTS


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


