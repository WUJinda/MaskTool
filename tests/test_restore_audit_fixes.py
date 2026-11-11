# -*- coding: utf-8 -*-
"""tests/test_restore_audit_fixes.py — 还原链路对抗性审查修复的回归（2026-10-28 审查会话）

审查发现（全部限定还原链路，脱敏路径零改动）：
- P1-A：restore_pdf_file 插入后无回读校验——china-s 渲染不了的原文
  （谚文/假名等）静默退化为 ·/空白，产出"缺原文"的还原件且无告警
  （mask 侧 _verify_inserted_texts 的还原镜像缺口）
- P1-B：docx/xlsx 零命中静默（与 PDF 不对称）——映射表与文件不匹配
  时零替换也报成功
- P2-A：XlsxAdapter.restore_value 按插入序替换，与 engine.unmask_text
  长键优先不一致——互为前缀的键（跨批次/手工映射表）短键破坏长键
- P2-B：Web 单文件还原名匹配 next() 取第一条无唯一性防御（CLI
  _restored_file_name 有 len==1 防御）
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

fitz = pytest.importorskip("fitz")

from mask_tool.adapters.extract import extract_texts  # noqa: E402
from mask_tool.adapters.restore import restore_file_content  # noqa: E402
from mask_tool.adapters.xlsx_adapter import XlsxAdapter  # noqa: E402
from mask_tool.core.engine import ReplacementEngine  # noqa: E402


# ---------------------------------------------------------------------------
# P1-A：PDF 还原写入回读校验（fail-closed）
# ---------------------------------------------------------------------------

class TestPdfRestoreWriteVerify:
    def _make_masked(self, path: Path) -> Path:
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 100), "负责人是汤姆", fontname="china-s", fontsize=12)
        doc.save(str(path))
        doc.close()
        return path

    def test_unrenderable_original_raises(self, tmp_path):
        """china-s 渲不了的原文（谚文）→ 拒绝产出缺失原文的还原件。"""
        masked = self._make_masked(tmp_path / "masked.pdf")
        tm = {"汤姆": {"original": "카타카ナ한글"}}  # 含谚文
        out = tmp_path / "restored.pdf"
        with pytest.raises(ValueError, match="还原写入校验失败"):
            restore_file_content(masked, out, tm)
        assert not out.exists()  # 不产出损坏还原件

    def test_renderable_original_passes(self, tmp_path):
        """正常中文原文不受校验影响（回归：上轮修复主路径）。"""
        masked = self._make_masked(tmp_path / "masked.pdf")
        tm = {"汤姆": {"original": "胡世东"}}
        out = tmp_path / "restored.pdf"
        assert restore_file_content(masked, out, tm) == []
        assert "胡世东" in extract_texts(out)

    def test_input_untouched_on_failure(self, tmp_path):
        """校验失败时输入文件不被修改（失败安全：可换环境重试）。"""
        masked = self._make_masked(tmp_path / "masked.pdf")
        before = masked.read_bytes()
        tm = {"汤姆": {"original": "한글"}}
        with pytest.raises(ValueError):
            restore_file_content(masked, tmp_path / "r.pdf", tm)
        assert masked.read_bytes() == before


# ---------------------------------------------------------------------------
# P1-B：docx/xlsx 零命中统一警告
# ---------------------------------------------------------------------------

class TestZeroHitWarning:
    def test_docx_zero_hit_warns(self, tmp_path):
        from docx import Document

        d = Document()
        d.add_paragraph("普通内容，无任何替换串。")
        plain = tmp_path / "plain.docx"
        d.save(str(plain))
        warns = restore_file_content(plain, tmp_path / "r.docx",
                                     {"汤姆": {"original": "胡世东"}})
        assert len(warns) == 1 and "0 命中" in warns[0]

    def test_xlsx_zero_hit_warns(self, tmp_path):
        from openpyxl import Workbook

        wb = Workbook()
        wb.active["A1"] = "普通内容"
        plain = tmp_path / "plain.xlsx"
        wb.save(str(plain))
        warns = restore_file_content(plain, tmp_path / "r.xlsx",
                                     {"汤姆": {"original": "胡世东"}})
        assert len(warns) == 1 and "0 命中" in warns[0]

    def test_docx_normal_hit_no_warning(self, tmp_path):
        from docx import Document

        d = Document()
        d.add_paragraph("负责人是汤姆。")
        src = tmp_path / "m.docx"
        d.save(str(src))
        warns = restore_file_content(src, tmp_path / "r.docx",
                                     {"汤姆": {"original": "胡世东"}})
        assert warns == []

    def test_empty_map_no_warning(self, tmp_path):
        """空映射表不触发零命中警告（无预期即无异常，与 PDF 口径一致）。"""
        from docx import Document

        d = Document()
        d.add_paragraph("普通内容")
        plain = tmp_path / "plain.docx"
        d.save(str(plain))
        assert restore_file_content(plain, tmp_path / "r.docx", {}) == []

    def test_pdf_warning_wording_unchanged(self, tmp_path):
        """PDF 零命中文案口径不变（上轮行为的回归锚）。"""
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 100), "普通内容", fontname="china-s", fontsize=12)
        plain = tmp_path / "plain.pdf"
        doc.save(str(plain))
        doc.close()
        warns = restore_file_content(plain, tmp_path / "r.pdf",
                                     {"汤姆": {"original": "胡世东"}})
        assert len(warns) == 1 and "0 命中" in warns[0]


# ---------------------------------------------------------------------------
# P2-A：xlsx restore_value 长键优先（与 engine.unmask_text 一致）
# ---------------------------------------------------------------------------

class TestXlsxRestoreOrdering:
    TM = {"A市": {"original": "广州市"}, "A市场": {"original": "武汉广场"}}

    def test_prefix_key_long_first(self):
        """互为前缀的键：长键先替换，短键不得破坏长键（审查实测曾
        把"A市场"错还原为"广州市场"）。"""
        value, hits = XlsxAdapter.restore_value("地点在A市场附近", self.TM)
        assert value == "地点在武汉广场附近"
        assert hits == ["A市场"]

    def test_short_key_still_works(self):
        value, hits = XlsxAdapter.restore_value("地点在A市附近", self.TM)
        assert value == "地点在广州市附近"
        assert hits == ["A市"]

    def test_mixed_occurrences(self):
        value, _hits = XlsxAdapter.restore_value("A市场与A市", self.TM)
        assert value == "武汉广场与广州市"

    def test_consistent_with_engine(self):
        """与 docx 侧（engine.unmask_text）同输入同结果。"""
        text = "地点在A市场附近"
        plain = {k: v["original"] for k, v in self.TM.items()}
        xlsx_val, _ = XlsxAdapter.restore_value(text, self.TM)
        engine_val, _ = ReplacementEngine.unmask_text(text, plain)
        assert xlsx_val == engine_val


# ---------------------------------------------------------------------------
# P2-B：Web 单文件还原名唯一性防御
# ---------------------------------------------------------------------------

class TestRestoredOutputName:
    @staticmethod
    def _pm(rel_new, old_name, status="renamed"):
        return SimpleNamespace(rel_new=rel_new, old_name=old_name, status=status)

    def test_exact_unique_match(self):
        from mask_tool.web.ui.pages.restore import _restored_output_name

        paths = [self._pm("a_masked.docx", "合同A.docx"),
                 self._pm("b_masked.docx", "合同B.docx")]
        assert _restored_output_name("a_masked.docx", paths) == "合同A.docx"

    def test_basename_ambiguity_returns_none(self):
        """目录任务中不同路径同名文件互为歧义：不猜测，返回 None。"""
        from mask_tool.web.ui.pages.restore import _restored_output_name

        paths = [self._pm("dir1/合同_masked.docx", "合同一.docx"),
                 self._pm("dir2/合同_masked.docx", "合同二.docx")]
        assert _restored_output_name("合同_masked.docx", paths) is None

    def test_skipped_status_not_used(self):
        """未真正改名的记录（skipped）不参与还原名。"""
        from mask_tool.web.ui.pages.restore import _restored_output_name

        paths = [self._pm("原名.docx", "原名.docx", status="skipped")]
        assert _restored_output_name("原名.docx", paths) is None

    def test_no_record_returns_none(self):
        from mask_tool.web.ui.pages.restore import _restored_output_name

        assert _restored_output_name("任意.docx", []) is None
