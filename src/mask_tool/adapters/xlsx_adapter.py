"""Excel 文档(.xlsx)脱敏适配器（重写版）

实现依据：design-D2-xlsx.md；引擎接口契约：design-D1-engine-docx.md §2.2/§6。

覆盖面：
- 单元格：纯文本 / 富文本(rich_text=True 加载) / 数字（M1：货币格式、大整数、
  卡号 Luhn 校验） / 公式（仅双引号字面量内替换，不动公式结构） / 批注 /
  超链接 tooltip（target 仅检测提示）
- 工作表级：页眉页脚 6 部位 x L/C/R、数据验证 list 项、sheet 级定义名称
- 工作簿级：全局定义名称（名称仅提示；值/备注在字面量内替换）
- zip 预检：线程批注 / 透视表缓存 / OLE 嵌入等 at-risk 部件告警

引擎接缝（D1 §2.2 契约）：优先使用 masker.engine 的
ReplacementEngine.mask_plain_text(text, results) -> MaskOutcome；engine 未注入
（core/engine.py 尚未合入）时退化为等价本地实现（长度降序 + 整词替换），
两种路径行为一致。

值类型还原契约（D2 §6.1）：数字单元格整值脱敏时 TokenMapping 标记 kind="number"
（模型暂无该字段时以实例属性降级记录），unmask 侧经 restore_value() 按 kind
决定写回数值还是文本。

明确不支持：线程批注（openpyxl 保存时丢弃该部件，预检告警）、透视表缓存保真、
工作表名/定义名称的重命名替换（会破坏引用，仅检测提示）。
"""

import re
import zipfile
from decimal import Decimal
from logging import Logger, getLogger
from pathlib import Path
from typing import Any, Optional

from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell
from openpyxl.cell.rich_text import CellRichText

from mask_tool.adapters.base import FileAdapter
from mask_tool.models.detection import (
    DetectionResult,
    DetectionStatus,
    DetectionType,
    Location,
)

logger: Logger = getLogger("mask_tool")

# Excel 公式内的字符串字面量（字面量内 "" 为转义引号）
_FORMULA_LITERAL = re.compile(r'"((?:[^"]|"")*)"')

# 货币格式的 number_format 判别字符（D2 §R1）
_CURRENCY_MARKS = ("¥", "￥", "$", "€", "£")

# unmask 数值还原的从严形态（≤19 位有效整数 + ≤6 位小数，杜绝浮点垃圾尾）。
# R3-B1：16-19 位覆盖 Luhn 卡号——kind=number 的整值 token 还原为 int，
# 下游公式引用不断裂；>19 位超出 xlsx 数值单元格现实使用域，降级文本。
_NUMBER_FORM = re.compile(r"-?\d{1,19}(?:\.\d{1,6})?")

# 页眉页脚 6 部位
_HF_PARTS = ("oddHeader", "evenHeader", "firstHeader",
             "oddFooter", "evenFooter", "firstFooter")

# zip 预检：不支持/高危部件（前缀匹配）
_AT_RISK_PARTS: tuple = (
    ("xl/threadedComments/", "线程批注",
     "openpyxl 不支持，保存时该部件将被丢弃，内容可能残留或丢失，请人工确认"),
    ("xl/pivotCache/", "数据透视表缓存", "不支持保真，输出可能丢失或失真"),
    ("xl/pivotTables/", "数据透视表", "不支持保真，输出可能丢失或失真"),
    ("xl/embeddings/", "嵌入对象", "不支持，其内容不在脱敏范围"),
    ("xl/oleObject", "OLE 对象", "不支持，其内容不在脱敏范围"),
    ("xl/ctrlProps/", "表单控件属性", "不支持"),
    ("xl/activeX/", "ActiveX 控件", "不支持"),
    ("xl/customXml/", "自定义 XML 部件", "不支持，其内容不在脱敏范围"),
)

# zip 预检：尽力保留但不纳入往返保证的部件
_BEST_EFFORT_PARTS: tuple = (
    ("xl/charts/", "图表"),
    ("xl/media/", "图片"),
)


class XlsxAdapter(FileAdapter):
    """Excel 文档脱敏适配器"""

    def supported_extensions(self) -> list[str]:
        return [".xlsx"]

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    def process(
        self,
        input_path: Path,
        output_dir: Path,
        output_name: Optional[str] = None,
    ) -> Path:
        """处理 Excel 文档：逐工作表脱敏后另存。

        Args:
            input_path: 输入文件路径
            output_dir: 输出目录
            output_name: 输出文件名；None 时保持 {stem}_masked.xlsx，
                供文件名脱敏流程协调原名输出
        """
        input_path = Path(input_path)
        output_dir = Path(output_dir)

        self._precheck_zip(input_path)                     # ① zip 预检
        wb = load_workbook(str(input_path), rich_text=True)  # ② 富文本保格式

        file_path = str(input_path)
        self._process_workbook(wb, file_path)              # ③ 逐部件脱敏

        output_dir.mkdir(parents=True, exist_ok=True)      # ④ 保存
        name = output_name or f"{input_path.stem}_masked.xlsx"
        output_path = output_dir / name
        wb.save(str(output_path))
        return output_path

    # ------------------------------------------------------------------
    # unmask 支持（D2 §6.1）：供 cli/web 一行调用的值类型还原
    # ------------------------------------------------------------------

    @staticmethod
    def restore_value(token_text: str, token_map: dict) -> tuple:
        """将含 token 的文本还原为原文，并按 mapping 的 kind 决定写回类型。

        Args:
            token_text: 单元格当前文本（如 "[AMOUNT_001]" 或 "合计[AMOUNT_001]元"）
            token_map: token -> 映射条目。条目可以是 mapping.json 的 dict
                （含 original / 可选 kind），也可以是 TokenMapping 对象。

        Returns:
            (value, hit_tokens)：value 通常为 str；当整格恰为一个
            kind=="number" 的 token 且还原串符合从严数值形态时，
            value 为 int/float。kind 缺失（旧 mapping）时降级为文本写回。
        """
        if not isinstance(token_text, str) or not token_map:
            return token_text, []
        restored, hits = token_text, []
        for token, entry in token_map.items():
            if token and token in restored:
                original, _kind = XlsxAdapter._mapping_fields(entry)
                restored = restored.replace(token, original)
                hits.append(token)
        if not hits:
            return token_text, []
        if len(hits) == 1 and token_text.strip() == hits[0]:
            _original, kind = XlsxAdapter._mapping_fields(token_map[hits[0]])
            if kind == "number" and _NUMBER_FORM.fullmatch(restored):
                value: Any = float(restored) if "." in restored else int(restored)
                return value, hits
        return restored, hits

    @staticmethod
    def _mapping_fields(entry) -> tuple:
        """兼容 dict 条目（mapping.json）与 TokenMapping 对象两种输入。"""
        if isinstance(entry, dict):
            original = entry.get("original", "")
            kind = entry.get("kind") or "text"
        else:
            original = getattr(entry, "original", "")
            kind = getattr(entry, "kind", None) or "text"
        return str(original), kind

    # ------------------------------------------------------------------
    # 工作簿遍历
    # ------------------------------------------------------------------

    def _process_workbook(self, wb, file_path: str) -> None:
        # 全局定义名称（D2 §4.5）
        self._process_defined_names(getattr(wb, "defined_names", {}), file_path)
        for ws in wb.worksheets:
            self._hint_scan_sheet_title(ws, file_path)          # §4.3 只提示
            self._process_defined_names(getattr(ws, "defined_names", {}),
                                        file_path, sheet=ws.title)
            self._process_header_footer(ws, file_path)           # §4.2
            self._process_data_validations(ws, file_path)         # §4.4
            for row in ws.iter_rows():
                for cell in row:
                    if isinstance(cell, MergedCell):
                        continue                                 # 合并区仅锚点可写
                    self._process_cell(cell, file_path, ws.title)

    def _process_cell(self, cell, file_path: str, sheet: str) -> None:
        """单单元格分派：批注/超链接与值无关，先处理；再按值类型走对应管线。"""
        self._process_comment(cell, file_path, sheet)
        self._process_hyperlink(cell, file_path, sheet)
        value = cell.value
        if value is None:
            return
        if isinstance(value, CellRichText):                     # 富文本管线 §5.2
            self._process_rich_cell(cell, file_path, sheet)
            return
        data_type = cell.data_type
        if data_type == "s" and isinstance(value, str):         # 纯文本管线
            self._process_text_cell(cell, file_path, sheet)
        elif data_type == "n":                                  # 数字管线 §3
            self._process_numeric_cell(cell, file_path, sheet)
            # 写侧精度保真：未脱敏的 ≥16 位整值经 openpyxl save 会丢精度，
            # 统一预转文本（值保真；卡号/长号本就是文本语义）。
            # R3-C11：19 位 int 读入即 float 的形态（float.is_integer() 且
            # abs≥10^15）同样走预转文本——真 float 场景罕见且转文本同为值保真，
            # 优先保真。已脱敏为 token 文本的单元格不受影响（非 int/float）。
            current = cell.value
            if (isinstance(current, int) and not isinstance(current, bool)
                    and abs(current) >= 10**15):
                cell.value = str(current)
                cell.number_format = "@"
            elif (isinstance(current, float) and current.is_integer()
                    and abs(current) >= 10**15):
                cell.value = str(int(current))
                cell.number_format = "@"
        elif data_type == "f":                                  # 公式管线 §5.1
            self._process_formula_cell(cell, file_path, sheet)
        # 'd' / 'b' / 'e'：日期、布尔、错误不做处理（文本态日期已由 's' 覆盖）

    # ------------------------------------------------------------------
    # 统一文本管线（D2 §2.2）
    # ------------------------------------------------------------------

    def _mask_text(
        self,
        text: str,
        *,
        file_path: str,
        loc: Location,
        context_tag: str = "",
        kind: str = "text",
        only_auto: bool = True,
    ) -> tuple:
        """对一段文本执行 检测 -> 策略 -> 引擎替换。

        Returns:
            (新文本, 实际替换的结果条数)。context_tag 写入结果 context 前缀
            （如 "[批注]" "[页眉L]" "[验证列表]"）用于报告区分来源。
        """
        if not text or not text.strip():
            return text, 0
        results = self.detector.detect(text, file_path)
        for r in results:
            r.location = loc
            if context_tag and not r.context.startswith("["):
                r.context = f"{context_tag} {r.context}"
        results = self.policy.apply(results)
        # 状态过滤交给 engine（active_statuses 文件作用域：默认仅 AUTO，--all 时含 SUGGEST）；
        # 仅在无 engine 的过渡 shim 下按 only_auto 预过滤
        if getattr(self.masker, "engine", None) is None:
            results = [
                r for r in results
                if r.status == DetectionStatus.AUTO_MASK
                or (r.status == DetectionStatus.SUGGEST_MASK and not only_auto)
            ]
            if not results:
                return text, 0
        new_text, n_replaced = self._engine_apply(text, results, kind=kind)
        return new_text, n_replaced

    # ------------------------------------------------------------------
    # 引擎接缝（D2 §2.3，唯一对外假设）
    # ------------------------------------------------------------------

    def _engine_apply(self, text: str, results: list, *, kind: str = "text") -> tuple[str, int]:
        """区间替换 + token 生成 + mapping 记录，返回 (新文本, 实际替换数)。

        masker.engine（core/engine.py 的 ReplacementEngine，D1 契约）存在时直接
        委托；否则退化为与 Masker.mask_text 等价的本地实现（长度降序 + 整词
        替换，修 N1 的顺序缺陷），保证引擎合入前后行为一致。
        """
        engine = getattr(self.masker, "engine", None)
        mask_plain_text = getattr(engine, "mask_plain_text", None)
        if mask_plain_text is not None:
            outcome = mask_plain_text(text, results)
            if kind != "text":
                self._mark_mapping_kind(getattr(outcome, "new_mappings", []), kind)
            return outcome.text, len(outcome.replacements)

        # ---- 过渡 shim（引擎未注入）----
        out = text
        n = 0
        for r in sorted(results, key=lambda r: len(r.text), reverse=True):
            if not r.text or r.text not in out:
                continue
            if self.masker.irreversible:
                out = out.replace(r.text, "***")
            else:
                token = self.masker.token_gen.generate(r.text, r.text_type)
                self.masker.mappings.append(self._new_mapping(token, r, kind))
                out = out.replace(r.text, token)
            n += 1
        return out, n

    @staticmethod
    def _new_mapping(token: str, result: DetectionResult, kind: str = "text"):
        """构造 TokenMapping；模型尚无 kind 字段时以实例属性降级记录。"""
        from mask_tool.models.mapping import TokenMapping

        kwargs = dict(
            token=token,
            original=result.text,
            text_type=result.text_type,
            confidence=result.confidence,
        )
        if kind == "text":
            return TokenMapping(**kwargs)
        try:
            return TokenMapping(kind=kind, **kwargs)
        except TypeError:
            mapping = TokenMapping(**kwargs)
            try:
                # 旧模型无 kind 字段：内存态还原生效，序列化降级为文本
                mapping.kind = kind
            except Exception:
                pass
            return mapping

    @staticmethod
    def _mark_mapping_kind(mappings: list, kind: str) -> None:
        """引擎路径：为本段替换新登记的 mapping 标记 kind。"""
        for m in mappings:
            try:
                m.kind = kind
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 数字单元格检测（M1，D2 §3）
    # ------------------------------------------------------------------

    def _process_numeric_cell(self, cell, file_path: str, sheet: str) -> None:
        value = cell.value
        if value is None or cell.is_date:      # 日期序列值前置排除；N4：is None 判空
            return
        canon = self._numeric_canonical(value)
        if canon is None:                      # bool/NaN/inf 不参与数字检测
            return
        loc = Location(file=file_path, sheet=sheet, cell_ref=cell.coordinate)
        synth = self._synth_numeric_results(canon, cell.number_format or "", loc)
        generic = self.detector.detect(canon, file_path)   # 吸收词典/正则通用能力
        merged = self._merge_numeric_results(synth, generic, loc)
        if not merged:
            return
        merged = self.policy.apply(merged)
        # 状态过滤交给 engine（active_statuses；--all 时 SUGGEST 也替换），
        # 无 engine 的 shim 下仅 AUTO 替换维持整值不变量
        if getattr(self.masker, "engine", None) is None:
            merged = [r for r in merged if r.status == DetectionStatus.AUTO_MASK]
            if not merged:
                return
        # 整值替换：数字单元格一律替换为单个 token 文本，number_format 保留不动
        new_text, _ = self._engine_apply(canon, merged, kind="number")
        if new_text != canon:
            cell.value = new_text

    @staticmethod
    def _numeric_canonical(value) -> Optional[str]:
        """规范化渲染：无科学计数、无千分位的十进制字符串。

        int -> str(int)（任意精度无损）；float -> repr 最短往返形式，
        科学计数时经 Decimal 展开为纯小数。bool/NaN/inf/None -> None。
        """
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                return None
            s = repr(value)
            if "e" in s or "E" in s:
                s = format(Decimal(s), "f")
            return s
        return None

    @staticmethod
    def _synth_numeric_results(
        canon: str, number_format: str, loc: Location
    ) -> list:
        """数字形态判别规则（D2 §3.2，adapter 侧合成 source="xlsx_numeric"）。

        裁决顺序：R1 货币优先（16-19 位 + 货币格式 -> AMOUNT，不判卡）；
        否则 R4/R4'（卡号，Luhn 把关）优先于 R2/R3（位数更特异）。

        静态方法不依赖实例状态：adapters/extract.py 提取路径（确认模式检测面）
        与本写路径共用同一实现，禁止复制第二份规则（R1-A1）。
        """
        results = []

        def mk(text_type: DetectionType, confidence: float) -> DetectionResult:
            return DetectionResult(
                text=canon,
                text_type=text_type,
                source="xlsx_numeric",
                confidence=confidence,
                location=loc,
            )

        digits = canon.lstrip("-")
        is_int_form = "." not in canon
        if XlsxAdapter._is_currency_format(number_format):              # R1 / R1'
            try:
                magnitude = abs(float(canon))
            except OverflowError:
                magnitude = float("inf")
            if magnitude >= 10000:
                results.append(mk(DetectionType.AMOUNT, 0.85))
        elif is_int_form and 16 <= len(digits) <= 19:                  # R4 / R4'
            if XlsxAdapter._luhn_ok(digits):
                results.append(mk(DetectionType.CUSTOM, 0.85))
            else:
                results.append(mk(DetectionType.CUSTOM, 0.65))
        elif is_int_form and 9 <= len(digits) <= 15:                   # R2
            results.append(mk(DetectionType.AMOUNT, 0.70))
        elif (not is_int_form) and len(digits.split(".")[0]) >= 6:     # R3
            results.append(mk(DetectionType.AMOUNT, 0.60))
        return results

    @staticmethod
    def _merge_numeric_results(
        synth: list, generic: list, loc: Location
    ) -> list:
        """数字口径统一与去重（D2 §3.3）。

        按 r.text 全等去重，优先级：xlsx_numeric > 词典(0.95) > 正则。
        通用检测结果中与 canonical 不全等的命中（旧版无边界 \\d{16,19} 正则
        在超长数字上可能产生部分命中）直接丢弃，以维持"数字单元格整值替换"
        不变量；M6 边界修复后该过滤自然成为无操作的保护。
        """
        canon_texts = {r.text for r in synth}

        def priority(r: DetectionResult) -> int:
            if r.source == "xlsx_numeric":
                return 0
            if r.source == "dictionary":
                return 1
            return 2

        by_text: dict = {}
        for r in sorted(synth + generic, key=priority):
            if r.source != "xlsx_numeric" and r.text not in canon_texts:
                continue
            if r.text in by_text:
                continue
            r.location = loc
            by_text[r.text] = r
        return list(by_text.values())

    @staticmethod
    def _is_currency_format(number_format: str) -> bool:
        return any(mark in number_format for mark in _CURRENCY_MARKS)

    @staticmethod
    def _luhn_ok(digits: str) -> bool:
        """Luhn 校验（银行卡号合法性）。"""
        if not digits.isdigit():
            return False
        total = 0
        for i, ch in enumerate(reversed(digits)):
            d = ord(ch) - 48
            if i % 2 == 1:
                d *= 2
                if d > 9:
                    d -= 9
            total += d
        return total % 10 == 0

    # ------------------------------------------------------------------
    # 文本 / 富文本 / 公式单元格
    # ------------------------------------------------------------------

    def _process_text_cell(self, cell, file_path: str, sheet: str) -> None:
        text = cell.value
        if not isinstance(text, str) or not text.strip():
            return
        new_text, hits = self._mask_text(
            text,
            file_path=file_path,
            loc=Location(file=file_path, sheet=sheet, cell_ref=cell.coordinate),
        )
        if hits and new_text != text:
            cell.value = new_text

    def _process_rich_cell(self, cell, file_path: str, sheet: str) -> None:
        """富文本单元格（D2 §5.2）。

        第 1 遍：块内命中只改该块文本，内联格式保留（设计伪码为命中一块即
        收；此处改为遍历全部块——提前返回会漏掉其他块的敏感词，违背脱敏
        完整性，属对设计伪码的有意修正）。
        第 2 遍：拼接整串仍有命中（跨块实体）-> 整格展平为纯文本并告警。
        """
        value = cell.value
        loc = Location(file=file_path, sheet=sheet, cell_ref=cell.coordinate)
        for i, block in enumerate(value):
            text = block if isinstance(block, str) else getattr(block, "text", None)
            if not isinstance(text, str) or not text.strip():
                continue
            new_text, hits = self._mask_text(text, file_path=file_path, loc=loc)
            if hits and new_text != text:
                if isinstance(block, str):
                    value[i] = new_text
                else:
                    block.text = new_text
        whole = str(value)
        if not whole.strip():
            return
        new_whole, hits = self._mask_text(whole, file_path=file_path, loc=loc)
        if hits and new_whole != whole:
            logger.warning(
                "工作表 %r 单元格 %s 跨格式块命中敏感词，该单元格内联格式已展平",
                sheet, cell.coordinate,
            )
            cell.value = new_whole

    def _process_formula_cell(self, cell, file_path: str, sheet: str) -> None:
        """公式单元格：只替换双引号字面量内的命中，不动公式结构（D2 §5.1）。

        缓存值交由 openpyxl load(非 data_only)->save 清空（数据卫生正收益，
        禁止 data_only=True 加载）。字面量外的命中不替换（改表名/引用会
        产生 #NAME?/#REF!）。
        """
        value = cell.value
        if isinstance(value, str):
            formula = value
        else:
            formula = getattr(value, "text", None)   # ArrayFormula
            if not isinstance(formula, str):
                return
        if '"' not in formula:
            return
        new_formula = self._mask_formula_literals(
            formula, file_path, sheet, cell_ref=cell.coordinate, where="公式",
        )
        if new_formula != formula:
            if isinstance(value, str):
                cell.value = new_formula
            else:
                try:
                    value.text = new_formula
                except Exception:
                    cell.value = new_formula

    def _mask_formula_literals(
        self,
        formula: str,
        file_path: str,
        sheet: Optional[str],
        cell_ref: Optional[str] = None,
        where: str = "公式",
    ) -> str:
        """对公式串内每个双引号字面量独立走文本管线；从后往前回拼防位移。"""
        parts = []
        for m in _FORMULA_LITERAL.finditer(formula):
            literal = m.group(1).replace('""', '"')
            new_literal, hits = self._mask_text(
                literal,
                file_path=file_path,
                loc=Location(file=file_path, sheet=sheet, cell_ref=cell_ref),
                context_tag=f"[{where}字面量]",
            )
            if hits and new_literal != literal:
                parts.append((m.span(1), new_literal.replace('"', '""')))
        if not parts:
            return formula
        out = formula
        for (start, end), new in reversed(parts):
            out = out[:start] + new + out[end:]
        return out

    # ------------------------------------------------------------------
    # 批注 / 超链接 / 页眉页脚 / 数据验证 / 定义名称 / 工作表名
    # ------------------------------------------------------------------

    def _process_comment(self, cell, file_path: str, sheet: str) -> None:
        """批注（D2 §4.1）：text 走文本管线；author 一并清除（R3-B7）。

        author 是真实人名元数据，与 docx 侧 ``_clear_comment_authors`` 语义
        对齐：单向脱敏（置为"匿名"占位），unmask 不还原（还原会重新泄露人名）。"""
        comment = cell.comment
        if comment is None:
            return
        if getattr(comment, "author", None):
            # 置为"匿名"占位：openpyxl 批注 author 经 authorId 间接引用，
            # 置空串保存后重载会显示为 'None' 字符串
            comment.author = "匿名"
        text = getattr(comment, "text", None)
        if not text or not text.strip():
            return
        new_text, hits = self._mask_text(
            text,
            file_path=file_path,
            loc=Location(file=file_path, sheet=sheet, cell_ref=cell.coordinate),
            context_tag="[批注]",
        )
        if hits and new_text != text:
            comment.text = new_text

    def _process_hyperlink(self, cell, file_path: str, sheet: str) -> None:
        """超链接（D2 §4.7）：tooltip 走文本管线；target 仅检测提示。"""
        link = getattr(cell, "hyperlink", None)
        if link is None:
            return
        tooltip = getattr(link, "tooltip", None)
        if tooltip and tooltip.strip():
            new_tooltip, hits = self._mask_text(
                tooltip,
                file_path=file_path,
                loc=Location(file=file_path, sheet=sheet, cell_ref=cell.coordinate),
                context_tag="[超链接]",
            )
            if hits and new_tooltip != tooltip:
                link.tooltip = new_tooltip
        target = getattr(link, "target", None)
        if target and self.detector.detect(target, file_path):
            logger.warning(
                "工作表 %r 单元格 %s 超链接 URL 含敏感词（不改写以保链接语义），请人工确认",
                sheet, cell.coordinate,
            )

    def _process_header_footer(self, ws, file_path: str) -> None:
        """页眉页脚（D2 §4.2）：6 部位 x L/C/R 逐部位走文本管线。

        &P/&A 等控制码与正文混排，引擎只替换命中区间，不影响控制码。
        """
        for part_name in _HF_PARTS:
            item = getattr(ws, part_name, None)
            if item is None:
                continue
            label = "页眉" if part_name.endswith("Header") else "页脚"
            for position in ("left", "center", "right"):
                part = getattr(item, position, None)
                if part is None:
                    continue
                text = getattr(part, "text", None)
                if not text or not text.strip():
                    continue
                new_text, hits = self._mask_text(
                    text,
                    file_path=file_path,
                    loc=Location(file=file_path, sheet=ws.title),
                    context_tag=f"[{label}{position[0].upper()}]",
                )
                if hits and new_text != text:
                    part.text = new_text

    def _process_data_validations(self, ws, file_path: str) -> None:
        """数据验证 list 字面量（D2 §4.4）：项级替换，sqref 等结构不动。"""
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
            if not inner:
                continue
            sqref = str(getattr(dv, "sqref", "") or "")
            new_items, changed = [], False
            for item in inner.split(","):
                new_item, hits = self._mask_text(
                    item,
                    file_path=file_path,
                    loc=Location(file=file_path, sheet=ws.title, cell_ref=sqref),
                    context_tag="[验证列表]",
                )
                if hits and new_item != item:
                    changed = True
                new_items.append(new_item)
            if changed:
                dv.formula1 = '"' + ",".join(new_items) + '"'

    def _process_defined_names(self, defined_names, file_path: str,
                               sheet: Optional[str] = None) -> None:
        """定义名称（D2 §4.5）：名称只提示；值/备注在双引号字面量内替换。"""
        try:
            items = list(defined_names.items())
        except Exception:
            return
        for name, dn in items:
            dn_name = getattr(dn, "name", name)
            if dn_name and self.detector.detect(dn_name, file_path):
                logger.warning(
                    "定义名称 %r 含敏感词（仅提示：重命名会破坏引用，未做替换）",
                    dn_name,
                )
            value = getattr(dn, "value", None)
            if isinstance(value, str) and '"' in value:
                new_value = self._mask_formula_literals(
                    value, file_path, sheet, where="定义名称",
                )
                if new_value != value:
                    dn.value = new_value
            comment = getattr(dn, "comment", None)
            if isinstance(comment, str) and '"' in comment:
                new_comment = self._mask_formula_literals(
                    comment, file_path, sheet, where="定义名称备注",
                )
                if new_comment != comment:
                    dn.comment = new_comment

    def _hint_scan_sheet_title(self, ws, file_path: str) -> None:
        """工作表名（D2 §4.3）：只检测提示，不改名（重命名破坏公式/名称引用）。"""
        title = ws.title
        if not title:
            return
        results = self.detector.detect(title, file_path)
        if results:
            logger.warning(
                "工作表名 %r 含敏感词 %s（仅提示：重命名会破坏引用，需人工处理）",
                title, [r.text for r in results],
            )

    # ------------------------------------------------------------------
    # zip 预检（D2 §4.6）
    # ------------------------------------------------------------------

    def _precheck_zip(self, input_path: Path) -> None:
        """扫描输入包内 at-risk 部件并告警（openpyxl 能力边界明示）。"""
        try:
            with zipfile.ZipFile(str(input_path)) as zf:
                names = zf.namelist()
        except (zipfile.BadZipFile, OSError) as exc:
            logger.warning("zip 预检失败（%s）：%s", input_path, exc)
            return
        for prefix, label, advice in _AT_RISK_PARTS:
            if any(name.startswith(prefix) for name in names):
                logger.warning("输入文件含%s（%s...）：%s", label, prefix, advice)
        for prefix, label in _BEST_EFFORT_PARTS:
            if any(name.startswith(prefix) for name in names):
                logger.info("输入文件含%s（%s...）：将尝试保留，未纳入往返保证",
                            label, prefix)
