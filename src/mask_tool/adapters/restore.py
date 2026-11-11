"""文件内容还原公共模块（审查 R1-A2：CLI 与 Web 共用同一实现）

从 CLI 抽取的还原路径，消除 Web/CLI/adapter 三份拷贝：

- docx：``DocxAdapter.restore_document``（XML walker，覆盖面与 mask 一致：
  页眉/页脚/脚注/尾注/批注/文本框全部还原，不再残留 token）
- xlsx：单元格（纯文本/富文本块/公式串/数组公式 ArrayFormula）/批注/页眉脚
  /数据验证 list/定义名称值+备注/超链接 tooltip 走
  ``XlsxAdapter.restore_value``；``load_workbook(rich_text=True)`` 保内联格式；
  kind=number 的整值 token 还原为数值（int/float）而非文本

R3-A1/A2：还原遍历面与 ``XlsxAdapter._process_workbook`` 写入面对称——
写入侧会替换 token 的每个部件（DV/定义名称/tooltip/ArrayFormula），
还原侧逐一覆盖，保证 token 零残留。

token_map 契约：``{token: 条目}``，条目为 mapping.json 的 dict（含 original /
可选 kind）或 TokenMapping 对象——与 ``XlsxAdapter.restore_value`` 一致。
"""

from pathlib import Path
from typing import Dict, List


def bracket_gate(token_map: Dict[str, dict]) -> bool:
    """是否允许各还原路径启用 ``"[" in text`` 快速短路优化。

    仅当 **全部** 键均为含 ``[`` 的 token 样式串时返回 True；只要存在
    任意伪装词键（alias/混合批次），含伪装词但不含 ``[`` 的文本段落
    就可能被短路误跳导致静默漏还原，必须禁用短路（宁慢勿漏）。
    """
    return bool(token_map) and all("[" in str(k) for k in token_map)


def restore_file_content(f: Path, output_path: Path, token_map: Dict[str, dict]) -> List[str]:
    """单文件内容还原：docx 走 walker，xlsx 走 restore_value（数值还原）。

    Returns:
        警告文案列表（空 = 正常）。全部格式统一"零命中"警告：映射表
        替换串在输入文件中一次都没出现（文件与映射表不匹配，或脱敏
        产物已损坏），此时产物等于输入照抄，调用方应透出，避免
        "零替换也报成功"误导分发。
    """
    f = Path(f)
    suffix = f.suffix.lower()
    if suffix == ".docx":
        from mask_tool.adapters.docx_adapter import DocxAdapter

        hits = DocxAdapter.restore_document(f, output_path, token_map)
        return _zero_hit_warning(token_map, len(hits))
    elif suffix in (".xlsx", ".xlsm"):
        hit_total = restore_xlsx_file(f, output_path, token_map)
        return _zero_hit_warning(token_map, hit_total)
    elif suffix == ".pdf":
        return restore_pdf_file(f, output_path, token_map)
    elif suffix in (".doc", ".xls"):
        # 转换式链路零命中判定：转换件在临时目录，无法低成本二次提取，
        # 委托层不统计（跨批次错配由 Web 历史选择/CLI R1-B8 提示兜底）
        restore_legacy_file(f, output_path, token_map)
        return []
    else:
        raise ValueError(f"不支持的还原格式: {suffix}")


def _zero_hit_warning(token_map: Dict[str, dict], hit_total: int) -> List[str]:
    """统一零命中警告（空表不触发）：与 restore_pdf_file 同文案口径。"""
    if token_map and hit_total == 0:
        return [
            "未发现任何映射表替换串（0 命中）：此文件可能不属于该映射表"
            "批次，或脱敏产物已损坏（文本层不含替换串）；本次产物与输入"
            "相同，请勿当作已还原文件分发"
        ]
    return []


def restore_legacy_file(input_path: Path, output_path: Path, token_map: Dict[str, dict]) -> None:
    """旧版 Office 格式还原（L2 转换式）：脱敏文件转 OOXML → 委托对应
    还原 → 转回原格式。

    与脱敏侧对称复用同一转换引擎（office_convert）；原地还原（output_path
    == input_path，CLI 目录模式）时目标转换产物直接覆盖原文件，OOXML
    中间件全程在独立临时目录，源文件先读后写无交叉。转换失败（无引擎/
    加密/损坏）原样抛出 OfficeConvertError 体系异常。
    """
    import shutil
    import tempfile

    from mask_tool.adapters.office_convert import convert_cached, convert_to

    input_path = Path(input_path)
    suffix = input_path.suffix.lower()
    delegate_suffix = ".docx" if suffix == ".doc" else ".xlsx"

    workspace = Path(tempfile.mkdtemp(prefix="masktool_legacy_restore_"))
    try:
        converted = convert_cached(input_path, delegate_suffix)
        restored = workspace / f"{input_path.stem}_restored{delegate_suffix}"
        if delegate_suffix == ".docx":
            from mask_tool.adapters.docx_adapter import DocxAdapter

            DocxAdapter.restore_document(converted, restored, token_map)
        else:
            restore_xlsx_file(converted, restored, token_map)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        convert_to(
            restored, suffix,
            output_path.parent, output_path.name,
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def restore_pdf_file(
    input_path: Path, output_path: Path, token_map: Dict[str, dict],
) -> List[str]:
    """PDF 内容还原：逐页搜索 token → redaction 擦除 → 原位插回原文。

    原文可能含中文，用 PyMuPDF 内置 CJK 字体（china-s）插回；插入区宽度
    只有原 token 的宽度，长原文缩小字号至下限后允许溢出（还原排版是
    近似的，可读性优先）。

    Returns:
        警告文案列表。零命中（映射表替换串在全文一次都没出现）时返回
        明确警告——此时产物等于输入照抄，多见于映射表与文件不匹配，
        或脱敏产物已损坏（helv 写中文退化为 · 的事故件文本层不含伪装
        词，对账查"残留"也查不到，必须从命中数倒推才能发现）。

    Raises:
        ValueError：回读校验失败——某些已插入的原文未落入文本层
        （如 china-s 渲染不了的字符静默退化为 ·/空白，mask 侧同款
        事故的还原镜像）。输入文件不会被修改，失败是安全的：拒绝
        产出"缺了原文"的还原件，由用户核查后重试。
    """
    import fitz

    from mask_tool.adapters.pdf_adapter import (
        _apply_edits, _block_common_size, _collect_edit, iter_pdf_text_blocks,
    )

    doc = fitz.open(str(input_path))
    try:
        if doc.is_encrypted:
            raise ValueError("PDF 已加密，无法还原（请先解除密码保护）")
        hit_count = 0
        for page in doc:
            edits = []
            originals = []
            for full, chars in iter_pdf_text_blocks(page):
                hits = [
                    (token, entry)
                    for token, entry in token_map.items()
                    if token and token in full
                ]
                if not hits:
                    continue
                hit_count += len(hits)
                # 插入字号用块内众数（正常文本字号），token 缩小字号不传染
                common_size = _block_common_size(chars)
                for token, entry in hits:
                    original = (
                        entry.get("original", "")
                        if isinstance(entry, dict) else str(entry)
                    )
                    pos = full.find(token)
                    while pos != -1:
                        # pad=0：token 字符是本工具插入的、bbox 精确覆盖实际
                        # 渲染字形；不外扩，溢出部分对右邻的相交压到最小
                        # （对抗审查 P1-1：≥1pt 相交即整字连带删除）
                        edit = _collect_edit(chars, pos, pos + len(token),
                                             pad=0.0)
                        if edit is not None:
                            if edit["size"] < common_size:
                                edit["size"] = common_size
                            edits.append(edit)
                            originals.append(original)
                        pos = full.find(token, pos + 1)
            if edits:
                _apply_edits(page, edits, originals, fontname="china-s")
                # 回读校验（与 mask 侧 _verify_inserted_texts 对称）：本页
                # 插入的原文必须落入本页文本层，否则宁可失败也不产出
                # 缺原文的还原件（按页校验，不跨页重查）
                page_text = "".join(
                    full for full, _chars in iter_pdf_text_blocks(page)
                )
                missing = [
                    o for o in dict.fromkeys(o for o in originals if o)
                    if o not in page_text
                ]
                if missing:
                    sample = "、".join(repr(o[:20]) for o in missing[:3])
                    raise ValueError(
                        f"还原写入校验失败（{len(missing)} 串原文未落入"
                        f"文本层，如 {sample}）：插入字体可能无法渲染这些"
                        "字符，已拒绝产出缺失原文的还原文件（输入未修改）"
                    )
        warnings = _zero_hit_warning(token_map, hit_count)
        doc.save(str(output_path), garbage=3, deflate=True)
        return warnings
    finally:
        doc.close()


def restore_xlsx_file(
    input_path: Path, output_path: Path, token_map: Dict[str, dict],
) -> int:
    """xlsx/xlsm 内容还原（D2 §6.1）：单元格/富文本块/公式串/ArrayFormula/批注/
    页眉脚/数据验证 list/定义名称值+备注/超链接 tooltip 走
    XlsxAdapter.restore_value，kind=number 的整值 token 还原为数值。
    xlsm 的 VBA 宏部件经 keep_vba 原样保留（与脱敏侧对称）。

    Returns:
        命中总次数（零命中供上层统一告警：文件与映射表可能不匹配）。
    """
    from openpyxl import load_workbook
    from openpyxl.cell.cell import MergedCell
    from openpyxl.cell.rich_text import CellRichText
    from mask_tool.adapters.xlsx_adapter import XlsxAdapter

    keep_vba = input_path.suffix.lower() == ".xlsm"
    wb = load_workbook(str(input_path), rich_text=True, keep_vba=keep_vba)
    # alias 伪装词批次禁用 '[' 短路（见 bracket_gate）：伪装词不含 '['
    gate = bracket_gate(token_map)
    hit_total = 0

    def restore_text(text: str):
        nonlocal hit_total
        value, hits = XlsxAdapter.restore_value(text, token_map)
        if hits:
            hit_total += len(hits)
            return value
        return None

    # 全局定义名称（与写入侧 _process_workbook 首步对称，R3-A1）
    _restore_defined_names(getattr(wb, "defined_names", {}), restore_text, gate)

    for ws in wb.worksheets:
        _restore_data_validations(ws, restore_text, gate)   # 写入侧 §4.4
        _restore_defined_names(getattr(ws, "defined_names", {}),
                               restore_text, gate)          # 写入侧 §4.5（sheet 级）
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell, MergedCell):
                    continue
                comment = getattr(cell, "comment", None)
                if comment is not None:
                    text = getattr(comment, "text", None)
                    if isinstance(text, str) and (not gate or "[" in text):
                        value = restore_text(text)
                        if value is not None:
                            comment.text = value
                # 超链接 tooltip（写入侧 §4.7 只写 token 进 tooltip）
                tooltip = getattr(getattr(cell, "hyperlink", None), "tooltip", None)
                if isinstance(tooltip, str) and (not gate or "[" in tooltip):
                    new_tooltip = restore_text(tooltip)
                    if new_tooltip is not None:
                        cell.hyperlink.tooltip = str(new_tooltip)
                value = cell.value
                if value is None:
                    continue
                if not isinstance(value, (str, CellRichText)):
                    # 数组公式（ArrayFormula，R3-A2）：token 只写入 .text 的
                    # 双引号字面量内，整串替换等价；.text 为可写属性
                    formula = getattr(value, "text", None)
                    if isinstance(formula, str) and (not gate or "[" in formula):
                        new_formula = restore_text(formula)
                        if new_formula is not None:
                            value.text = str(new_formula)
                    continue
                text = str(value)
                if gate and "[" not in text:
                    continue
                if isinstance(value, CellRichText):
                    for i, block in enumerate(value):
                        block_text = block if isinstance(block, str) else getattr(block, "text", None)
                        if isinstance(block_text, str) and (not gate or "[" in block_text):
                            new_block, hits = XlsxAdapter.restore_value(block_text, token_map)
                            if hits:
                                hit_total += len(hits)
                                if isinstance(block, str):
                                    value[i] = new_block
                                else:
                                    block.text = new_block
                else:
                    new_value = restore_text(text)
                    if new_value is not None:
                        cell.value = new_value
        for part_name in ("oddHeader", "evenHeader", "firstHeader",
                          "oddFooter", "evenFooter", "firstFooter"):
            item = getattr(ws, part_name, None)
            if item is None:
                continue
            for position in ("left", "center", "right"):
                part = getattr(item, position, None)
                text = getattr(part, "text", None)
                if isinstance(text, str) and (not gate or "[" in text):
                    value = restore_text(text)
                    if value is not None:
                        part.text = value

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(output_path))
    return hit_total


def _restore_data_validations(ws, restore_text, gate: bool = True) -> None:
    """数据验证 list 还原（R3-A1）：与写入侧 ``_process_data_validations``
    对称——token 写在引号包裹的逗号串内（``"item1,item2"`` 格式），
    剥引号整串替换后按原格式回写。"""
    dv_list = getattr(getattr(ws, "data_validations", None),
                      "dataValidation", None) or []
    for dv in dv_list:
        if getattr(dv, "type", None) != "list":
            continue
        formula1 = getattr(dv, "formula1", None)
        if (not isinstance(formula1, str) or len(formula1) < 2
                or not formula1.startswith('"')):
            continue
        inner = formula1[1:-1] if formula1.endswith('"') else formula1[1:]
        if not inner or (gate and "[" not in inner):
            continue
        new_inner = restore_text(inner)
        if new_inner is not None:
            dv.formula1 = '"' + str(new_inner) + '"'


def _restore_defined_names(defined_names, restore_text, gate: bool = True) -> None:
    """定义名称还原（R3-A1）：值/备注整串 token 替换，与写入侧
    ``_process_defined_names`` 对称（token 只写入双引号字面量内，
    整串替换与逐字面量替换等价）。"""
    try:
        items = list(defined_names.items())
    except Exception:
        return
    for _name, dn in items:
        for attr in ("value", "comment"):
            text = getattr(dn, attr, None)
            if isinstance(text, str) and (not gate or "[" in text):
                new_text = restore_text(text)
                if new_text is not None:
                    setattr(dn, attr, str(new_text))
