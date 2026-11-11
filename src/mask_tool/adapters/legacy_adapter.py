"""旧版 Office 二进制格式（.doc / .xls）脱敏适配器（L2，2026-10-22）

转换式适配：.doc/.xls 是 OLE 复合二进制格式，Python 生态无可保真回写的
原生库，处理链为::

    原文件 --office_convert--> OOXML 临时件 --委托适配器--> 脱敏 OOXML
           --office_convert--> 原格式脱敏产物

- DocAdapter：.doc ↔ .docx，委托 DocxAdapter（全部件 walker 能力一致）；
- XlsAdapter：.xls ↔ .xlsx，委托 XlsxAdapter（单元格/富文本/公式/批注一致）。

检测项 Location 与报告的文件名始终指向原始 .doc/.xls 路径（display_path），
临时 OOXML 路径不外泄到映射表与 UI。正向转换经 office_convert.convert_cached
进程级缓存，与预扫描（pipeline.prepare）共用一次转换。

已知边界（README 同步声明）：
- 转换经 Office/LibreOffice 重渲染，复杂排版（图表、艺术字、OLE 对象）
  可能与原文件存在差异，还原同理为"内容级还原"；
- .doc/.xls 内嵌的 VBA 宏在转到 OOXML 时被移除——脱敏产物与还原产物
  均不含宏（宏内容本就不参与脱敏，移除在安全上是有利侧）；
- 需要本机可用转换引擎（MS Office 或 LibreOffice），缺失时明确报错
  （office_convert.EngineUnavailableError，含安装指引）。
"""

import shutil
import tempfile
from pathlib import Path
from typing import Optional

from mask_tool.adapters.base import FileAdapter


class LegacyOfficeAdapter(FileAdapter):
    """转换式适配基类：转换 → 委托 OOXML 适配器 → 转回原格式。"""

    legacy_suffix: str = ""       # 本适配器处理的扩展名（".doc" / ".xls"）
    delegate_suffix: str = ""     # 正向转换目标（".docx" / ".xlsx"）

    def _new_delegate(self) -> FileAdapter:
        """构造委托的 OOXML 适配器实例（延迟导入避免包级循环依赖）。"""
        raise NotImplementedError

    def process(
        self,
        input_path: Path,
        output_dir: Path,
        output_name: Optional[str] = None,
    ) -> Path:
        """处理旧版 Office 文档：正向转换 -> 委托脱敏 -> 转回原格式。

        Args:
            input_path: 输入文件路径（.doc / .xls）
            output_dir: 输出目录
            output_name: 输出文件名；None 时为 {stem}_masked.doc/.xls
                （目录/zip 模式由上层传原文件名以构建镜像树）
        """
        from mask_tool.adapters.office_convert import convert_cached, convert_to

        input_path = Path(input_path)
        output_dir = Path(output_dir)
        name = output_name or f"{input_path.stem}_masked{self.legacy_suffix}"
        output_path = output_dir / name

        workspace = Path(tempfile.mkdtemp(prefix="masktool_legacy_"))
        try:
            converted = convert_cached(input_path, self.delegate_suffix)
            delegate = self._new_delegate()
            masked = delegate.process(
                converted,
                workspace,
                output_name=converted.name,
                display_path=str(input_path),
            )
            output_dir.mkdir(parents=True, exist_ok=True)
            convert_to(
                masked, self.legacy_suffix,
                output_path.parent, output_path.name,
            )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        return output_path

    def supported_extensions(self) -> list[str]:
        return [self.legacy_suffix]


class DocAdapter(LegacyOfficeAdapter):
    """Word 97-2003 文档（.doc）脱敏适配器（转换式委托 DocxAdapter）"""

    legacy_suffix = ".doc"
    delegate_suffix = ".docx"

    def _new_delegate(self) -> FileAdapter:
        from mask_tool.adapters.docx_adapter import DocxAdapter
        return DocxAdapter(self.detector, self.policy, self.masker)


class XlsAdapter(LegacyOfficeAdapter):
    """Excel 97-2003 工作簿（.xls）脱敏适配器（转换式委托 XlsxAdapter）"""

    legacy_suffix = ".xls"
    delegate_suffix = ".xlsx"

    def _new_delegate(self) -> FileAdapter:
        from mask_tool.adapters.xlsx_adapter import XlsxAdapter
        return XlsxAdapter(self.detector, self.policy, self.masker)
