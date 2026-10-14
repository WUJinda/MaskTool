# -*- coding: utf-8 -*-
"""PDF 适配器：PyMuPDF redaction 原位替换（2026-10-14 重写，替代 MVP 报告模式）。

技术路线（方案 B，plan/pdf-xlsm-support.md）：
- 检测/处理面：``iter_pdf_text_blocks`` 逐 block 拼接字符文本
  （rawdict 逐字符 bbox/origin，extract.py 检测面与本 adapter 同源，R1-A1）
- 替换：块文本经 ``engine.mask_plain_text`` 得到 Replacement（携带原文本
  字符区间 span.start/end），区间映射回**逐字符**矩形 → ``add_redact_annot``
  精确登记（同 span 部分命中不连带擦除前后文本——探针验证发现的缺陷）
- 回写：``page.apply_redactions()`` 物理擦除（真删除内容，非遮挡；图片与
  矢量线稿不动），在原字符基线点 ``insert_text`` 写回替换文本。token
  仅含 ASCII 安全字符，内置 Helvetica（helv）即可，无中文字体嵌入问题；
  字号自适应防溢出（``_insert_text``，脱敏与还原共用）
- 边界（检测面=处理面，无静默盲区）：
  * 加密 PDF 拒绝处理（is_encrypted 守卫）
  * 无文本层（扫描件）全文档零命中 → 明确报错而非静默产出原件
  * 图片内文字、批注/表单域文本不在处理面（get_text 默认不含）
"""

from logging import Logger, getLogger
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from mask_tool.adapters.base import FileAdapter
from mask_tool.models.detection import Location

logger: Logger = getLogger("mask_tool")

# redact 矩形微扩（字符 bbox 偶尔不含边缘；过大会误伤邻字）
_RECT_PAD = 0.15
# 字号自适应下限：短命中区塞长 token 的极限。实测 9pt 正文 2 字中文
# 命中区（18pt）需 ≤2.8pt 才能塞下 [PERSON_001]；1.5pt 下限覆盖绝大多数
# 场景，最坏溢出 <1pt（还原侧 pad=0 配合后不触发"相交≥1pt 整字连带
# 删除"的误删阈值——对抗审查 P1-1 实测标定）
_MIN_FONT_SIZE = 1.5


def iter_pdf_text_blocks(page) -> Iterator[Tuple[str, List[Tuple[int, int, dict, dict]]]]:
    """生成 (block_text, chars)：检测面与处理面共用的块遍历（rawdict）。

    chars: [(char_start, char_end, char_info, span_info)]——块拼接文本中每个
    字符的区间、rawdict 字符字典（c / bbox / origin）与所属 span（size）。
    rawdict 每字符自带 bbox/origin，使部分命中的替换可精确到字符矩形。
    """
    for block in page.get_text("rawdict")["blocks"]:
        if block.get("type") != 0:
            continue  # 图片块不在处理面
        full = ""
        chars: List[Tuple[int, int, dict, dict]] = []
        for line in block.get("lines", []):
            for s in line.get("spans", []):
                for ch in s.get("chars", []):
                    c = ch.get("c")
                    if not c:
                        continue
                    start = len(full)
                    full += c
                    chars.append((start, len(full), ch, s))
        if full.strip():
            yield full, chars


def _collect_edit(chars, start: int, end: int, pad: float = _RECT_PAD) -> Optional[dict]:
    """把块内字符区间 [start, end) 汇总为一次编辑（redact 矩形 + 插入参数）。

    Returns:
        {"rects": [Rect...], "origin": (x, y), "size": float, "width": float}
        区间无法映射时返回 None。
    """
    import fitz

    covered = [
        (a, b, ch, s) for (a, b, ch, s) in chars if a < end and start < b
    ]
    if not covered:
        return None
    first_a, _first_b, first_ch, first_span = covered[0]
    rects = []
    for _a, _b, ch, _s in covered:
        rects.append(fitz.Rect(ch["bbox"]) + (-pad, -pad, pad, pad))
    # 宽度预算：首字符所在行的覆盖宽度（跨行命中时 token 插在首行）
    first_y0 = first_ch["bbox"][1]
    same_line = [ch for (_a, _b, ch, _s) in covered
                 if abs(ch["bbox"][1] - first_y0) < 1.0]
    xs0 = min(ch["bbox"][0] for ch in same_line)
    xs1 = max(ch["bbox"][2] for ch in same_line)
    return {
        "rects": rects,
        "origin": first_ch["origin"],
        "size": float(first_span.get("size", 12.0)),
        "width": max(xs1 - xs0, 4.0),
    }


def _insert_text(page, origin, base_size: float, max_width: float,
                 text: str, fontname: str = "helv") -> None:
    """在原字符基线点插入文本，字号自适应：宽出则缩，下限 _MIN_FONT_SIZE
    （仍超出时按下限溢出；下限已保证常见 token 不触发）。"""
    import fitz

    size = base_size
    while size > _MIN_FONT_SIZE:
        try:
            if fitz.get_text_length(text, fontname=fontname,
                                    fontsize=size) <= max_width:
                break
        except Exception:  # 内置 CJK 字体宽度表不可用时跳过自适应
            break
        size -= 0.25
    page.insert_text(fitz.Point(origin[0], origin[1]), text,
                     fontname=fontname, fontsize=size)


def _block_common_size(chars) -> float:
    """块内众数字号（按字符数加权）：还原插入时用正常文本字号，
避免缩小后的 token 字号（3pt）传染给还原文本。"""
    from collections import Counter

    sizes: Counter = Counter()
    for _a, _b, ch, s in chars:
        try:
            sizes[round(float(s.get("size", 12.0)), 1)] += 1
        except (TypeError, ValueError):
            continue
    return sizes.most_common(1)[0][0] if sizes else 12.0


def _apply_edits(page, edits: List[dict], texts: List[str],
                 fontname: str = "helv") -> None:
    """统一应用一批编辑：全部 redact → 一次性擦除 → 逐条插入。

    插入必须在 apply_redactions 之后（新插入文本不可被本轮擦除）。
    edits[i] 与 texts[i] 一一对应。
    """
    import fitz

    for edit in edits:
        for rect in edit["rects"]:
            page.add_redact_annot(rect)
    # 图片/线稿保持不动：仅移除文本（图文重叠区的图片不受影响）
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
    for edit, text in zip(edits, texts):
        _insert_text(page, edit["origin"], edit["size"], edit["width"],
                     text, fontname=fontname)


def pdf_structure_warnings(path) -> list:
    """PDF 结构边界检查（对抗审查 P1-2/P2-1/P2-3，检测阶段 UI 触达）。

    adapter 侧的 logger 告警在桌面版（pythonw 无控制台）不可见；本函数
    把"不在处理面但用户应知"的边界汇总为面向用户的警告文案：
    - 行级文本对象（每块仅一行）：跨 block 拆开的敏感词漏检风险
    - 乱码提取（U+FFFD 占比高，疑似缺 ToUnicode）：检测结果不可信
    - 批注/表单域：不在处理面（检测面=处理面，但用户需知其存在）
    """
    import fitz

    from collections import Counter

    warns: list = []
    doc = fitz.open(str(path))
    try:
        n_blocks = 0
        total_chars = 0
        n_replacement = 0
        n_annot = 0
        n_widget = 0
        n_img_page = 0
        for page in doc:
            for full, _chars in iter_pdf_text_blocks(page):
                n_blocks += 1
                total_chars += len(full)
                n_replacement += full.count("\ufffd")
            n_annot += sum(1 for _ in (page.annots() or []))
            n_widget += sum(1 for _ in (page.widgets() or []))
            if page.get_images(full=False):
                n_img_page += 1
        if n_blocks >= 20 and total_chars / max(n_blocks, 1) <= 30:
            warns.append(
                "该 PDF 为行级文本对象（表单/OCR 文本层常见）：跨行拆开的"
                "敏感词可能漏检，建议人工复核"
            )
        if total_chars > 0 and n_replacement / total_chars > 0.05:
            warns.append(
                "部分文本无法正确解码（疑似字体映射缺失）：检测结果可能"
                "不可信，建议人工复核"
            )
        if n_annot:
            warns.append(f"含 {n_annot} 条批注：批注文本不在脱敏范围，请人工确认")
        if n_widget:
            warns.append(f"含 {n_widget} 个表单域：表单内容不在脱敏范围，请人工确认")
        if doc.page_count() >= 2 and n_img_page == doc.page_count():
            warns.append("每页都含图片：图片内文字不在脱敏范围，请人工确认")
    except Exception:
        pass  # 结构检查尽力而为，不影响主流程
    finally:
        doc.close()
    return warns


class PdfAdapter(FileAdapter):
    """PDF 文档脱敏适配器（redaction 原位替换）"""

    def supported_extensions(self) -> list[str]:
        return [".pdf"]

    def process(
        self,
        input_path: Path,
        output_dir: Path,
        output_name: Optional[str] = None,
    ) -> Path:
        import fitz

        input_path = Path(input_path)
        output_dir = Path(output_dir)
        doc = fitz.open(str(input_path))
        try:
            if doc.is_encrypted:
                raise ValueError("PDF 已加密，请先解除密码保护后再脱敏")
            # 扫描件守卫：全文档无文本层时明确报错（否则静默产出原件）
            if not any(page.get_text().strip() for page in doc):
                raise ValueError(
                    "未检测到文本层（疑似扫描件）：PDF 需为文本型才可脱敏"
                )

            file_path = str(input_path)
            for pno, page in enumerate(doc):
                self._mask_page(page, pno + 1, file_path)

            output_dir.mkdir(parents=True, exist_ok=True)
            name = output_name or f"{input_path.stem}_masked.pdf"
            output_path = output_dir / name
            # garbage=3 去除被 redaction 清空的冗余对象，压缩体积
            doc.save(str(output_path), garbage=3, deflate=True)
            return output_path
        finally:
            doc.close()

    def _mask_page(self, page, page_no: int, file_path: str) -> None:
        loc = Location(file=file_path, page=page_no)
        edits: List[dict] = []
        texts: List[str] = []

        for full, chars in iter_pdf_text_blocks(page):
            results = self.detector.detect(full, file_path)
            for r in results:
                r.location = loc
                if not r.context.startswith("["):
                    r.context = f"[第{page_no}页] {r.context}"
            results = self.policy.apply(results)
            # PdfAdapter 仅在 Pipeline 注入的 Masker 下工作（薄委托必然带
            # engine，见 core/masker.py），不走无引擎 shim
            outcome = self.masker.engine.mask_plain_text(full, results)
            for rep in outcome.replacements:
                edit = _collect_edit(chars, rep.span.start, rep.span.end)
                if edit is None:
                    logger.warning(
                        "PDF 第 %d 页替换区间无法映射到字符矩形，已跳过: %r",
                        page_no, rep.span.text[:30],
                    )
                    continue
                edits.append(edit)
                texts.append(rep.new_text)

        if edits:
            _apply_edits(page, edits, texts, fontname="helv")
