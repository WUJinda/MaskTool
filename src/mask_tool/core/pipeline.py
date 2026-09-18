"""流水线编排 - 核心中的核心，串联所有模块"""

import json
import logging
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Set

from mask_tool.models.config import MaskConfig
from mask_tool.models.report import MaskReport
from mask_tool.core.detector import Detector
from mask_tool.core.engine import ReplacementEngine
from mask_tool.core.formats import BLOCKED_EXTS
from mask_tool.core.policy import PolicyEngine
from mask_tool.core.masker import Masker
from mask_tool.core.tokenizer import TokenGenerator
from mask_tool.store.lexicon import LexiconStore

logger = logging.getLogger("mask_tool")


def _new_batch_id() -> str:
    """生成批次标识：M4 模式 `YYYYmmdd-HHMMSS-xxxxxx`（与批次目录命名一致）"""
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


class Pipeline:
    """脱敏处理流水线"""

    def __init__(
        self,
        config: MaskConfig,
        *,
        lexicon_path: Optional[str] = None,
        whitelist_path: Optional[str] = None,
        batch_id: Optional[str] = None,
        manual_words: Optional[List[str]] = None,
        auto_detect_enabled: bool = True,
    ):
        """
        Args:
            config: 全局配置
            lexicon_path: 词库路径（H5：应传已解析为绝对路径的值；
                缺省用 config.lexicon_path）
            whitelist_path: 白名单路径（同上，缺省用 config.whitelist_path）
            batch_id: 批次标识（M3/M4，写入 mapping 与 metadata）；
                缺省自动按 `时间戳-uuid6` 生成
            manual_words: 临时手动词（Web 自定义敏感词，I6）。注入
                Detector 最高优先级通道；同一批检测与脱敏必须传同一套，
                否则会出现"检测看到了但脱敏没脱"
            auto_detect_enabled: 自动检测通道总开关（仅手动模式传 False）：
                NER 不初始化、词库置空、正则关闭，仅剩手动词通道；
                默认 True 行为不变
        """
        self.config = config
        self.batch_id = batch_id or _new_batch_id()
        self.report = MaskReport()

        # 加载词库
        lexicon_store = LexiconStore(
            lexicon_path or config.lexicon_path,
            whitelist_path or config.whitelist_path,
        )
        lexicon = lexicon_store.get_lexicon()
        whitelist = lexicon_store.get_whitelist()

        # I6：仅手动模式——词库/NER/正则三通道全关，Detector 仅剩手动词
        if not auto_detect_enabled:
            lexicon = {}

        # 初始化NER引擎（如果配置启用）
        ner_engine = None
        if config.ner.enabled and auto_detect_enabled:
            try:
                from mask_tool.core.ner.jieba_ner import JiebaNER
                ner_engine = JiebaNER()
                ner_engine.set_whitelist(whitelist)
            except Exception as e:
                logger.warning(f"NER引擎初始化失败: {e}")

        # 初始化各引擎：ReplacementEngine 组装后经 Masker 对外提供
        self.detector = Detector(
            lexicon, whitelist, ner_engine=ner_engine,
            manual_words=manual_words, regex_enabled=auto_detect_enabled,
        )
        self.policy = PolicyEngine(config)
        self.token_gen = TokenGenerator()
        self.masker = Masker(
            self.token_gen,
            irreversible=False,
            amount_mode=config.amount_mode,
            batch_id=self.batch_id,
        )

    @property
    def engine(self) -> ReplacementEngine:
        """统一替换引擎（与 masker.engine 同一实例；外部替换 masker 后仍指向新实例）"""
        return self.masker.engine

    def process_text(self, text: str, file_path: str = "") -> str:
        """
        对纯文本执行完整的脱敏流水线

        Args:
            text: 原始文本
            file_path: 文件路径（用于报告）

        Returns:
            脱敏后的文本（H6：默认仅替换 AUTO_MASK 项）
        """
        # 1. 检测
        results = self.detector.detect(text, file_path)

        # 2. 策略决策
        results = self.policy.apply(results)

        # 3. 脱敏
        masked_text = self.masker.mask_text(text, results)

        # 4. 记录到报告
        for result in results:
            self.report.add_result(result)

        return masked_text

    def prepare(self, files: Iterable[Path]) -> Set[str]:
        """M3 防线一：处理前预扫描输入文件中已出现的 token 样式串，
        登记 reserved 使后续编号让位（防撞号导致 unmask 误还原）。

        OOXML（docx/xlsx/pptx）按 zip 内 XML 明文扫描（token 仅含
        ASCII 安全字符，无 XML 转义）；其余格式按纯文本尽力读取。
        扫描失败（损坏/不可读）的文件静默跳过——预扫描是尽力而为的防线。

        Returns:
            汇总登记的 reserved token 集合
        """
        reserved: Set[str] = set()
        for f in files:
            reserved |= self._scan_file_tokens(Path(f))
        if reserved:
            self.token_gen.set_reserved(reserved)
        return reserved

    def _scan_file_tokens(self, path: Path) -> Set[str]:
        scan = ReplacementEngine.scan_tokens
        if path.suffix.lower() in {".docx", ".xlsx", ".pptx"}:
            try:
                with zipfile.ZipFile(path) as zf:
                    found: Set[str] = set()
                    for name in zf.namelist():
                        if name.endswith((".xml", ".rels")):
                            data = zf.read(name).decode("utf-8", errors="ignore")
                            found |= scan(data)
                    return found
            except Exception:
                pass  # 非 zip / 损坏：回退纯文本尝试
        try:
            return scan(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            return set()

    def process_file(
        self,
        input_path: Path,
        batch_dir: Path,
        *,
        allowed_originals: Optional[Set[str]] = None,
        statuses: Optional[frozenset] = None,
        output_name: Optional[str] = None,
    ) -> Optional[Path]:
        """对单个文件执行脱敏处理

        Args:
            input_path: 输入文件路径
            batch_dir: 输出目录（M4：批次目录）
            allowed_originals: confirm 模式：仅替换勾选的原文集合（None 不限制）
            statuses: 参与替换的状态集合（None 用引擎默认，仅 AUTO_MASK）
            output_name: 输出文件名（None 保持原名）；当前 adapter 尚无
                此参数，透传用 kwargs 容错——不支持则忽略并保持现状

        Returns:
            输出文件路径，或None（如果不支持该格式）
        """
        suffix = input_path.suffix.lower()
        self.report.input_files.append(str(input_path))

        if suffix in BLOCKED_EXTS:  # 双保险：CLI 已按 formats 常量过滤
            logger.warning(f"跳过 {input_path.name}: {BLOCKED_EXTS[suffix]}")
            return None

        # 记录处理前的映射数量，用于计算本文件新增的脱敏项
        mappings_before = len(self.masker.mappings)

        # confirm 参数经 engine 活动状态传导：adapter 内部调 mask_plain_text /
        # mask_text 不传参即生效（处理完恢复，不影响后续文件）
        engine = self.masker.engine
        prev_statuses = engine.active_statuses
        prev_allowed = engine.active_allowed_originals
        if statuses is not None:
            engine.active_statuses = frozenset(statuses)
        engine.active_allowed_originals = allowed_originals

        # 根据文件类型选择适配器（pptx/pdf 已屏蔽，方法保留不可达）
        try:
            if suffix == ".docx":
                result = self._process_docx(input_path, batch_dir, output_name)
            elif suffix == ".xlsx":
                result = self._process_xlsx(input_path, batch_dir, output_name)
            else:
                return None
        finally:
            engine.active_statuses = prev_statuses
            engine.active_allowed_originals = prev_allowed
            # R1-A1 兜底：确认模式下被 allowed_originals 过滤掉的检测项一次性
            # 汇总警告（检测面与处理面不同步时不再静默漏脱；正常勾选流程下
            # 用户取消勾选的项也会列出，语义是"以下项未勾选，未脱敏"）
            if allowed_originals is not None and engine.confirm_filtered:
                items = "、".join(
                    f"{text!r}({source})" for text, source in engine.confirm_filtered
                )
                logger.warning(
                    "确认模式：以下检测项不在勾选集内，未做脱敏（请确认是否在确认"
                    "表格中见过这些项）: %s", items,
                )
                engine.confirm_filtered.clear()

        # 将本文件新增的映射记录到报告
        for m in self.masker.mappings[mappings_before:]:
            self.report.auto_masked.append({
                "text": m.original,
                "type": m.text_type.value,
                "source": "adapter",
                "confidence": m.confidence,
                "file": str(input_path),
                "token": m.token,
            })

        return result

    def _run_adapter(self, adapter_cls, input_path: Path, batch_dir: Path,
                     output_name: Optional[str]) -> Path:
        """构造 adapter 并执行；output_name 用 kwargs 容错透传（不支持则忽略）"""
        adapter = adapter_cls(self.detector, self.policy, self.masker)
        if output_name is not None:
            try:
                output_path = adapter.process(input_path, batch_dir, output_name=output_name)
            except TypeError:
                # 当前 adapter 尚无 output_name 参数：忽略并保持现状
                output_path = adapter.process(input_path, batch_dir)
        else:
            output_path = adapter.process(input_path, batch_dir)
        self.report.output_files.append(str(output_path))
        return output_path

    def _process_docx(self, input_path: Path, batch_dir: Path,
                      output_name: Optional[str] = None) -> Path:
        """处理Word文档"""
        from mask_tool.adapters.docx_adapter import DocxAdapter
        return self._run_adapter(DocxAdapter, input_path, batch_dir, output_name)

    def _process_xlsx(self, input_path: Path, batch_dir: Path,
                      output_name: Optional[str] = None) -> Path:
        """处理Excel文档"""
        from mask_tool.adapters.xlsx_adapter import XlsxAdapter
        return self._run_adapter(XlsxAdapter, input_path, batch_dir, output_name)

    def _process_pptx(self, input_path: Path, output_dir: Path) -> Path:
        """处理PowerPoint文档（已屏蔽不可达；代码保留不删）"""
        from mask_tool.adapters.pptx_adapter import PptxAdapter
        adapter = PptxAdapter(self.detector, self.policy, self.masker)
        output_path = adapter.process(input_path, output_dir)
        self.report.output_files.append(str(output_path))
        return output_path

    def _process_pdf(self, input_path: Path, output_dir: Path) -> Path:
        """处理PDF文档（已屏蔽不可达；代码保留不删。MVP: 仅提取文本并生成报告）"""
        try:
            from mask_tool.adapters.pdf_adapter import PdfAdapter
        except ImportError:
            raise RuntimeError(
                "PDF处理需要安装PyMuPDF: pip install pymupdf"
            )
        adapter = PdfAdapter(self.detector, self.policy)
        output_path = adapter.process(input_path, output_dir)
        self.report.output_files.append(str(output_path))
        return output_path

    def save_mapping(self, output_path: Path) -> None:
        """保存映射表到JSON文件（metadata 含批次信息，M3/M4）"""
        from mask_tool import __version__

        data = {
            "tokens": {},
            "metadata": {
                "batch_id": self.batch_id,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "tool_version": __version__,
                "input_files": list(self.report.input_files),
                "total_mappings": len(self.masker.get_mappings()),
                "mode": self.config.mode,
            },
        }
        for m in self.masker.get_mappings():
            # 过渡期兼容：旧 adapter 绕过 engine 直接构造 TokenMapping 时无批次标识，
            # 保存前统一回填当前批次（engine 路径登记的已有正确值，不受影响）
            if not m.batch_id:
                m.batch_id = self.batch_id
            data["tokens"][m.token] = m.to_dict()

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def save_report(self, output_path: Path) -> None:
        """保存脱敏报告到JSON文件"""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self.report.to_dict(), f, ensure_ascii=False, indent=2)

    def get_report(self) -> MaskReport:
        """获取当前报告"""
        return self.report
