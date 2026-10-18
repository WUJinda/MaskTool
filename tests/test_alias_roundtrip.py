"""alias 伪装词模式 roundtrip 集成测试

覆盖：docx / xlsx 的 alias 风格 mask -> unmask roundtrip（逐字节比对）、
混合批次（实体 alias + 金额 token）、撞词避让端到端、bracket_gate、
scan_alias_residuals、AliasGenerator 经 Pipeline 组装的接线。
"""

import json
import re
import zipfile
from pathlib import Path

import pytest
from docx import Document
from openpyxl import Workbook, load_workbook

from mask_tool.adapters.docx_adapter import DocxAdapter
from mask_tool.adapters.restore import bracket_gate, restore_file_content
from mask_tool.core.alias_generator import AliasGenerator, AliasLexicon
from mask_tool.core.detector import Detector
from mask_tool.core.engine import ReplacementEngine
from mask_tool.core.masker import Masker
from mask_tool.core.pipeline import Pipeline
from mask_tool.core.policy import PolicyEngine
from mask_tool.models.config import MaskConfig
from mask_tool.models.detection import DetectionType


PERSON_RE = re.compile(r"(汤姆|杰瑞|张三|李四|王五)")
COMPANY_RE = re.compile(r"[A-Z]集团")


def _alias_masker(lexicon=None, detector_lexicon=None):
    detector = Detector(detector_lexicon or {}, set())
    policy = PolicyEngine(MaskConfig(mode="smart"))
    masker = Masker(AliasGenerator(lexicon))
    return detector, policy, masker


# ---------------------------------------------------------------------------
# bracket_gate / scan_alias_residuals 单元
# ---------------------------------------------------------------------------

class TestBracketGate:
    def test_token_style_keys(self):
        assert bracket_gate({"[PERSON_001]": {"original": "李海富"}}) is True

    def test_alias_keys(self):
        assert bracket_gate({"汤姆": {"original": "李海富"}}) is False

    def test_mixed_batch_disables_gate(self):
        """混合批次：只要存在伪装词键，'[' 短路必须禁用（含伪装词但不含
        '[' 的段落会被误跳导致静默漏还原——E2E 曾因此报还原缺陷）"""
        tm = {"汤姆": {"original": "李海富"}, "[AMOUNT_001]": {"original": "1.2亿"}}
        assert bracket_gate(tm) is False

    def test_empty(self):
        assert bracket_gate({}) is False


class TestScanAliasResiduals:
    def test_finds_residuals(self):
        assert ReplacementEngine.scan_alias_residuals(
            "汤姆和李四去了A市", ["汤姆", "A集团", "李四"],
        ) == {"汤姆", "李四"}

    def test_clean_text(self):
        assert ReplacementEngine.scan_alias_residuals(
            "李海富去了B省", ["汤姆", "A集团"],
        ) == set()

    def test_empty_keys(self):
        assert ReplacementEngine.scan_alias_residuals("任何文本", []) == set()


# ---------------------------------------------------------------------------
# docx roundtrip（alias 风格）
# ---------------------------------------------------------------------------

class TestDocxAliasRoundtrip:
    def test_roundtrip_full_coverage(self, tmp_path):
        """正文/表格/页眉含实体：alias 脱敏后观感自然，还原逐字节一致"""
        lexicon = {
            "company": ["某某科技有限公司"],
            "person": ["李海富", "王晓芳"],
        }
        src = tmp_path / "src.docx"
        doc = Document()
        doc.add_paragraph("李海富就职于某某科技有限公司，与王晓芳对接。")
        table = doc.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "李海富"
        table.cell(0, 1).text = "某某科技有限公司"
        doc.sections[0].header.paragraphs[0].text = "某某科技有限公司 内部文件"
        doc.save(src)

        detector, policy, masker = _alias_masker(detector_lexicon=lexicon)
        adapter = DocxAdapter(detector, policy, masker)
        masked_path = adapter.process(src, tmp_path)

        # 1) 脱敏观感：不含原文实体；含伪装词
        masked_paras = "\n".join(DocxAdapter.extract_paragraph_texts(masked_path))
        assert "李海富" not in masked_paras
        assert "某某科技有限公司" not in masked_paras
        assert "汤姆" in masked_paras
        assert "A集团" in masked_paras
        # 同一实体跨位置复用同一伪装词
        assert masked_paras.count("汤姆") >= 2

        # 2) mapping：token 字段 = 伪装词，还原契约不变
        token_map = {m.token: {"original": m.original} for m in masker.mappings}
        assert "汤姆" in token_map and "A集团" in token_map
        assert token_map["汤姆"]["original"] == "李海富"

        # 3) 还原：逐字节一致
        restored = tmp_path / "restored.docx"
        DocxAdapter.restore_document(masked_path, restored, token_map)
        restored_paras = DocxAdapter.extract_paragraph_texts(restored)
        assert restored_paras[0] == "李海富就职于某某科技有限公司，与王晓芳对接。"
        assert "王晓芳" in "\n".join(restored_paras)
        # 伪装词零残留
        assert "汤姆" not in "\n".join(restored_paras)
        assert not PERSON_RE.search("\n".join(restored_paras).replace("李海富", "").replace("王晓芳", ""))

    def test_restore_document_without_bracket_gate_no_skip(self, tmp_path):
        """回归：alias 批次还原路径的 '[' 短路已禁用（否则静默漏还原）"""
        lexicon = {"person": ["李海富"]}
        src = tmp_path / "src.docx"
        doc = Document()
        doc.add_paragraph("负责人：李海富。")
        doc.save(src)

        detector, policy, masker = _alias_masker(detector_lexicon=lexicon)
        DocxAdapter(detector, policy, masker).process(src, tmp_path)
        token_map = {m.token: {"original": m.original} for m in masker.mappings}

        restored = tmp_path / "restored.docx"
        DocxAdapter.restore_document(src, restored, token_map)
        assert "李海富" in "\n".join(DocxAdapter.extract_paragraph_texts(restored))

    def test_token_style_still_works_after_gate_change(self, tmp_path):
        """回归：token 风格批次在 gate 改造后行为不变"""
        lexicon = {"person": ["李海富"]}
        src = tmp_path / "src.docx"
        doc = Document()
        doc.add_paragraph("负责人：李海富。")
        doc.save(src)

        detector = Detector(lexicon, set())
        policy = PolicyEngine(MaskConfig(mode="smart"))
        masker = Masker(AliasGenerator())   # 生成器被 engine 内 token_gen 决定？不，Masker 持有
        # 用 TokenGenerator 风格
        from mask_tool.core.tokenizer import TokenGenerator
        masker = Masker(TokenGenerator())
        DocxAdapter(detector, policy, masker).process(src, tmp_path)
        token_map = {m.token: {"original": m.original} for m in masker.mappings}
        assert list(token_map) == ["[PERSON_001]"]

        restored = tmp_path / "restored.docx"
        DocxAdapter.restore_document(src, restored, token_map)
        assert "李海富" in "\n".join(DocxAdapter.extract_paragraph_texts(restored))


# ---------------------------------------------------------------------------
# xlsx roundtrip（alias 风格）
# ---------------------------------------------------------------------------

class TestXlsxAliasRoundtrip:
    def test_roundtrip_cells_and_header(self, tmp_path):
        from mask_tool.adapters.xlsx_adapter import XlsxAdapter

        lexicon = {"company": ["某某科技有限公司"], "person": ["李海富"]}
        src = tmp_path / "src.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.title = "名单"
        ws["A1"] = "姓名"
        ws["B1"] = "单位"
        ws["A2"] = "李海富"
        ws["B2"] = "某某科技有限公司（负责人李海富）"
        ws["C2"] = "合计[XX_001]元"   # 文档自然文本含方括号，但不属于映射表
        wb.save(src)

        detector, policy, masker = _alias_masker(detector_lexicon=lexicon)
        adapter = XlsxAdapter(detector, policy, masker)
        masked_path = adapter.process(src, tmp_path)

        token_map = {m.token: {"original": m.original} for m in masker.mappings}
        assert "汤姆" in token_map and "A集团" in token_map

        restored = tmp_path / "restored.xlsx"
        restore_file_content(masked_path, restored, token_map)
        wb2 = load_workbook(restored)
        ws2 = wb2["名单"]
        assert ws2["A2"].value == "李海富"
        assert ws2["B2"].value == "某某科技有限公司（负责人李海富）"
        assert ws2["C2"].value == "合计[XX_001]元"   # 非映射表内容不动

    def test_mixed_batch_alias_plus_amount_token(self, tmp_path):
        """混合批次：实体 alias + regex 金额 token，双风格同批还原"""
        from mask_tool.adapters.xlsx_adapter import XlsxAdapter

        lexicon = {"person": ["李海富"]}
        src = tmp_path / "src.xlsx"
        wb = Workbook()
        ws = wb.active
        ws["A1"] = "李海富报销1200元"
        ws["A2"] = "联系人电话13800138000"
        wb.save(src)

        # 词库人名 + 正则金额/手机号
        detector = Detector(lexicon, set())
        policy = PolicyEngine(MaskConfig(mode="smart"))
        masker = Masker(AliasGenerator())
        XlsxAdapter(detector, policy, masker).process(src, tmp_path)

        token_map = {m.token: {"original": m.original} for m in masker.mappings}
        styles = set(token_map)
        assert any("[" in k for k in styles), "金额/手机号应走 token 样式"
        assert any("[" not in k for k in styles), "词库人名应走伪装词"
        assert "汤姆" in token_map
        assert bracket_gate(token_map) is False   # 混合批次禁用 '[' 短路

        restored = tmp_path / "restored.xlsx"
        restore_file_content(src, restored, token_map)
        ws2 = load_workbook(restored).active
        assert ws2["A1"].value == "李海富报销1200元"
        assert ws2["A2"].value == "联系人电话13800138000"

    def test_mixed_batch_alias_and_token_in_different_cells(self, tmp_path):
        """混合批次高危场景：伪装词与 token 分处不同单元格——
        若 '[' 短路未禁用，含伪装词但不含 '[' 的单元格会被静默跳过"""
        from mask_tool.adapters.xlsx_adapter import XlsxAdapter

        lexicon = {"person": ["李海富"]}
        src = tmp_path / "src.xlsx"
        wb = Workbook()
        ws = wb.active
        ws["A1"] = "李海富"                       # 仅伪装词，无 '['
        ws["A2"] = "电话13800138000"              # 仅 token（regex）
        ws["A3"] = "备注：无敏感内容"              # 无任何替换串
        wb.save(src)

        detector = Detector(lexicon, set())
        policy = PolicyEngine(MaskConfig(mode="smart"))
        masker = Masker(AliasGenerator())
        XlsxAdapter(detector, policy, masker).process(src, tmp_path)

        token_map = {m.token: {"original": m.original} for m in masker.mappings}
        restored = tmp_path / "restored.xlsx"
        restore_file_content(src, restored, token_map)
        ws2 = load_workbook(restored).active
        assert ws2["A1"].value == "李海富"
        assert ws2["A2"].value == "电话13800138000"
        assert ws2["A3"].value == "备注：无敏感内容"


# ---------------------------------------------------------------------------
# 撞词避让端到端
# ---------------------------------------------------------------------------

class TestAvoidanceEndToEnd:
    def test_source_word_not_used_as_alias(self, tmp_path):
        """原文含"张三"（词库人名被脱敏），后续实体不得分配"张三"为伪装词"""
        lexicon = {"person": ["张三", "李海富"]}
        detector = Detector(lexicon, set())
        policy = PolicyEngine(MaskConfig(mode="smart"))
        gen = AliasGenerator()
        gen.register_source_text("张三与李海富共同签署。")
        masker = Masker(gen)

        doc_text = "张三与李海富共同签署。"
        from mask_tool.models.detection import DetectionStatus, Location
        results = detector.detect(doc_text, "t.docx")
        results = policy.apply(results)
        masked = masker.mask_text(doc_text, results)

        # "张三"（原文敏感词）与"李海富"都得到替换；伪装词不含"张三"
        assert "张三" not in [m.token for m in masker.mappings]
        tokens = {m.token for m in masker.mappings}
        assert "汤姆" in tokens and "杰瑞" in tokens
        # roundtrip
        token_map = {m.token: m.original for m in masker.mappings}
        restored, hits = ReplacementEngine.unmask_text(masked, token_map)
        assert restored == doc_text
        assert hits == tokens

    def test_alias_word_in_original_is_avoided_across_files(self):
        """prepare 口径：XML 明文登记后避让（模拟 Pipeline.prepare）"""
        gen = AliasGenerator()
        gen.register_source_text("<w:t>张三</w:t>其他正文")
        gen.generate("李海富", DetectionType.PERSON)
        gen.generate("王小明", DetectionType.PERSON)
        gen.generate("赵铁柱", DetectionType.PERSON)
        used = gen.get_used_aliases()
        assert "张三" not in used
        assert used == ["汤姆", "杰瑞", "李四"]


# ---------------------------------------------------------------------------
# Pipeline 组装接线（mask_style=alias）
# ---------------------------------------------------------------------------

class TestPipelineWiring:
    def _cfg(self, tmp_path, style):
        return MaskConfig(mode="smart", mask_style=style)

    def test_pipeline_token_style_default(self, tmp_path):
        p = Pipeline(self._cfg(tmp_path, "token"), lexicon_path="nonexistent")
        assert isinstance(p.token_gen, __import__(
            "mask_tool.core.tokenizer", fromlist=["TokenGenerator"]).TokenGenerator)

    def test_pipeline_alias_style(self, tmp_path):
        p = Pipeline(self._cfg(tmp_path, "alias"), lexicon_path="nonexistent")
        assert isinstance(p.token_gen, AliasGenerator)

    def test_mapping_metadata_style(self, tmp_path):
        p = Pipeline(self._cfg(tmp_path, "alias"), lexicon_path="nonexistent",
                     batch_id="b-test")
        from mask_tool.models.detection import DetectionResult, DetectionStatus, Location
        results = [DetectionResult(
            text="李海富", text_type=DetectionType.PERSON, source="ner",
            confidence=0.9, location=Location(file="t"),
            status=DetectionStatus.AUTO_MASK,
        )]
        p.masker.mask_text("李海富", results)
        out = tmp_path / "mapping.json"
        p.save_mapping(out)
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["metadata"]["style"] == "alias"
        assert "汤姆" in data["tokens"]
        assert data["tokens"]["汤姆"]["original"] == "李海富"

    def test_mapping_metadata_style_token_default(self, tmp_path):
        p = Pipeline(self._cfg(tmp_path, "token"), lexicon_path="nonexistent",
                     batch_id="b-test")
        out = tmp_path / "mapping.json"
        p.save_mapping(out)
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["metadata"]["style"] == "token"

    def test_prepare_registers_alias_avoidance(self, tmp_path):
        """Pipeline.prepare 在 alias 模式下把全文登记进避让表"""
        # 构造一个含"汤姆"的纯文本文件（alias 池头词）
        f = tmp_path / "input.txt"
        f.write_text("本文提及汤姆与杰瑞。", encoding="utf-8")
        p = Pipeline(self._cfg(tmp_path, "alias"), lexicon_path="nonexistent")
        p.prepare([f])
        gen = p.token_gen
        gen.generate("李海富", DetectionType.PERSON)
        gen.generate("王小明", DetectionType.PERSON)
        gen.generate("赵铁柱", DetectionType.PERSON)
        used = gen.get_used_aliases()
        assert "汤姆" not in used and "杰瑞" not in used
        assert used == ["张三", "李四", "王五"]

    def test_prepare_split_run_word_avoided(self, tmp_path):
        """跨 run 拆分的自然词（非敏感）经剥标签拼接后被避让。

        回归：早期版本保留原始 XML，``<w:t>张</w:t><w:t>三</w:t>`` 的
        "张三"匹配不到 → 漏避让 → unmask 误还原。"""
        src = tmp_path / "split.docx"
        doc = Document()
        para = doc.add_paragraph()
        para.add_run("自然文本张")
        para.add_run("三出现在这里。")
        doc.save(src)

        p = Pipeline(self._cfg(tmp_path, "alias"), lexicon_path="nonexistent")
        p.prepare([src])
        gen = p.token_gen
        gen.generate("李海富", DetectionType.PERSON)
        gen.generate("王小明", DetectionType.PERSON)
        gen.generate("赵铁柱", DetectionType.PERSON)
        used = gen.get_used_aliases()
        assert "张三" not in used, "跨 run 拆分的词未被避让"
        assert used == ["汤姆", "杰瑞", "李四"]

    def test_source_text_cap_degrades_gracefully(self, monkeypatch):
        """登记量超上限后不再登记（内存防线），避让降级但不阻断"""
        from mask_tool.core.alias_generator import AliasGenerator as AG

        gen = AG()
        monkeypatch.setattr(AG, "SOURCE_TEXT_MAX_CHARS", 10)
        gen.register_source_text("a" * 10)     # 恰达上限
        gen.register_source_text("张三")        # 超限不再登记
        assert gen._source_texts == ["a" * 10]
        assert gen._source_overflow is True
        gen.generate("李海富", DetectionType.PERSON)
        gen.generate("王小明", DetectionType.PERSON)
        gen.generate("赵铁柱", DetectionType.PERSON)
        # "张三"未被登记避让，仍可分配（降级语义：宁漏避让不阻断流程）
        assert "张三" in gen.get_used_aliases()
