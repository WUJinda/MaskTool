# -*- coding: utf-8 -*-
"""tests/test_pdf_support.py — PDF 格式接入测试（方案 B，2026-10-14）

覆盖：
- 样本构造：fitz 造文本 PDF（china-s 内置字体插入中文，多页/同词多处）
- 检测面：detect_file_results 块级检测 + Location.page + "[第N页]" 上下文前缀
- 脱敏：redaction 物理擦除（get_text 检索不到原文）+ token 原位插回
  + 同 span 部分命中不连带擦除前后文本 + 跨页普通内容不动
- 还原：token → 原文插回（china-s），往返一致
- 边界：扫描件（无文本层）/ 加密 PDF 明确报错
- pipeline：process_file 路由 .pdf + output_name 透传
"""

import zipfile
from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")

from mask_tool.adapters.extract import (  # noqa: E402
    detect_file_results, extract_texts,
)
from mask_tool.adapters.pdf_adapter import PdfAdapter  # noqa: E402
from mask_tool.adapters.restore import restore_file_content  # noqa: E402
from mask_tool.core.detector import Detector  # noqa: E402
from mask_tool.core.masker import Masker  # noqa: E402
from mask_tool.core.policy import PolicyEngine  # noqa: E402
from mask_tool.core.tokenizer import TokenGenerator  # noqa: E402
from mask_tool.models.config import MaskConfig  # noqa: E402

DEFAULT_LEXICON = {
    "person": ["李四"],
    "company": ["某某科技有限公司"],
}


def _make_pdf(path: Path, pages: list) -> Path:
    """造文本 PDF：pages 为 [(行文本, 字号), ...] 列表，每项一页。"""
    doc = fitz.open()
    for lines in pages:
        page = doc.new_page()
        y = 100
        for text, size in lines:
            page.insert_text((72, y), text, fontname="china-s", fontsize=size)
            y += 30
    doc.save(str(path))
    doc.close()
    return path


def _make_adapter(lexicon=None):
    detector = Detector(
        {k: list(v) for k, v in (lexicon or DEFAULT_LEXICON).items()}, set(),
    )
    policy = PolicyEngine(MaskConfig(mode="smart"))
    masker = Masker(TokenGenerator())
    return PdfAdapter(detector, policy, masker), masker


def _full_text(path: Path) -> str:
    doc = fitz.open(str(path))
    try:
        return "".join(page.get_text() for page in doc)
    finally:
        doc.close()


SAMPLE_PAGES = [
    [
        ("项目负责人：李四", 14),
        ("供应商：某某科技有限公司", 14),
        ("复核人：李四（与负责人同名，验证部分命中不连带擦除）", 12),
    ],
    [
        ("第二页普通内容，无敏感词", 12),
    ],
]


@pytest.fixture
def sample_pdf(tmp_path) -> Path:
    return _make_pdf(tmp_path / "合同.pdf", SAMPLE_PAGES)


# ---------------------------------------------------------------------------
# 检测面
# ---------------------------------------------------------------------------

class TestDetectSurface:
    def test_detect_file_results_pdf(self, sample_pdf):
        adapter, _ = _make_adapter()
        results = detect_file_results(sample_pdf, adapter.detector,
                                      adapter.policy)
        texts = {r.text for r in results}
        assert "李四" in texts and "某某科技有限公司" in texts
        # 页码定位：全部结果带 page（第一页）
        assert all(r.location.page == 1 for r in results)

    def test_context_has_page_prefix(self, sample_pdf):
        adapter, _ = _make_adapter()
        results = detect_file_results(sample_pdf, adapter.detector,
                                      adapter.policy)
        assert all("[第1页]" in (r.context or "") for r in results)

    def test_extract_texts_pdf(self, sample_pdf):
        text = extract_texts(sample_pdf)
        assert "李四" in text and "第二页普通内容" in text

    def test_extract_rejects_encrypted(self, tmp_path):
        enc = tmp_path / "加密.pdf"
        doc = fitz.open()
        doc.new_page()
        doc.save(str(enc), encryption=fitz.PDF_ENCRYPT_AES_128,
                 owner_pw="x", user_pw="x")
        doc.close()
        with pytest.raises(ValueError, match="加密"):
            extract_texts(enc)


# ---------------------------------------------------------------------------
# 脱敏：物理擦除 + 原位插回 + 部分命中精确
# ---------------------------------------------------------------------------

class TestMask:
    def test_mask_removes_original_and_inserts_token(self, sample_pdf):
        adapter, masker = _make_adapter()
        out = adapter.process(sample_pdf, sample_pdf.parent / "out")

        assert out.name == "合同_masked.pdf"
        after = _full_text(out)
        # redaction 物理擦除：原文不可再检索
        assert "李四" not in after
        assert "某某科技" not in after
        # token 原位插回
        for m in masker.mappings:
            assert m.token in after, f"{m.token} 未插入"
        assert masker.mappings, "应产生映射"

    def test_partial_hit_keeps_surrounding_text(self, sample_pdf):
        """同 span 部分命中：命中词之外的行内文本必须保留。"""
        adapter, _ = _make_adapter()
        out = adapter.process(sample_pdf, sample_pdf.parent / "out")
        after = _full_text(out)
        assert "复核人：" in after, "命中词前的行内文本被连带擦除"
        assert "（与负责人同名" in after, "命中词后的行内文本被连带擦除"

    def test_other_page_untouched(self, sample_pdf):
        adapter, _ = _make_adapter()
        out = adapter.process(sample_pdf, sample_pdf.parent / "out")
        doc = fitz.open(str(out))
        try:
            assert "第二页普通内容，无敏感词" in doc[1].get_text()
        finally:
            doc.close()

    def test_same_word_multiple_positions(self, tmp_path):
        """同一敏感词多处出现 → 同 token 全部替换（tokenizer 同原文同 token）。"""
        pdf = _make_pdf(tmp_path / "多例.pdf", [
            [("甲方代表李四签字", 12), ("乙方代表也是李四", 12)],
        ])
        adapter, masker = _make_adapter()
        out = adapter.process(pdf, tmp_path / "out")
        after = _full_text(out)
        assert "李四" not in after
        # 两个 PERSON 映射去重为同一 token，且文本中出现两次
        person_tokens = [m.token for m in masker.mappings if "PERSON" in m.token]
        assert len(person_tokens) >= 1
        assert after.count(person_tokens[0]) >= 2

    def test_output_name_passthrough(self, sample_pdf):
        adapter, _ = _make_adapter()
        out = adapter.process(sample_pdf, sample_pdf.parent / "out",
                              output_name="改名后.pdf")
        assert out.name == "改名后.pdf"

    def test_short_hit_no_neighbor_damage_on_restore(self, tmp_path):
        """短命中区溢出收敛（对抗审查 P1-1）：窄命中区（2 字中文 9pt）
        塞长 token 时字号自适应不溢出伤邻；还原时右邻窄字形不被连带
        删除（审查实测 redact 相交≥1pt 即整字删除）。"""
        pdf = _make_pdf(tmp_path / "窄命中.pdf", [
            [("复审由李四ilte核验通过", 9)],   # 9pt：命中区 18pt，token 需 ≤2.8pt
        ])
        adapter, masker = _make_adapter()
        masked = adapter.process(pdf, tmp_path / "out")
        after_mask = _full_text(masked)
        # 脱敏后右邻窄字形保留
        assert "ilte" in after_mask, "脱敏时右邻窄字形被覆盖/擦除"

        token_map = {m.token: {"original": m.original} for m in masker.mappings}
        restored = tmp_path / "窄命中_restored.pdf"
        restore_file_content(masked, restored, token_map)
        after_restore = _full_text(restored)
        # 还原后右邻窄字形仍保留（不触发 ≥1pt 相交连带删除）
        assert "ilte" in after_restore, "还原时右邻窄字形被连带删除"
        assert "核验通过" in after_restore
        assert "李四" in after_restore


# ---------------------------------------------------------------------------
# 边界守卫
# ---------------------------------------------------------------------------

class TestGuards:
    def test_scanned_pdf_rejected(self, tmp_path):
        """无文本层（扫描件）：明确报错而非静默产出原件。"""
        blank = tmp_path / "扫描件.pdf"
        doc = fitz.open()
        doc.new_page()  # 空白页：无文本层
        doc.save(str(blank))
        doc.close()
        adapter, _ = _make_adapter()
        with pytest.raises(ValueError, match="文本层"):
            adapter.process(blank, tmp_path / "out")

    def test_encrypted_pdf_rejected(self, tmp_path):
        enc = tmp_path / "加密.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 100), "李四", fontname="china-s", fontsize=12)
        doc.save(str(enc), encryption=fitz.PDF_ENCRYPT_AES_128,
                 owner_pw="x", user_pw="x")
        doc.close()
        adapter, _ = _make_adapter()
        with pytest.raises(ValueError, match="加密"):
            adapter.process(enc, tmp_path / "out")


# ---------------------------------------------------------------------------
# pipeline 路由
# ---------------------------------------------------------------------------

class TestPipelineDispatch:
    def test_process_file_routes_pdf(self, tmp_path):
        from mask_tool.core.pipeline import Pipeline

        lex = tmp_path / "lex.yaml"
        lex.write_text("person:\n  - 李四\n", encoding="utf-8")
        wl = tmp_path / "wl.yaml"
        wl.write_text("whitelist: []\n", encoding="utf-8")
        cfg = MaskConfig(mode="smart")
        pipeline = Pipeline(cfg, lexicon_path=str(lex), whitelist_path=str(wl))

        pdf = _make_pdf(tmp_path / "合同.pdf", [[("负责人李四", 12)]])
        out = pipeline.process_file(pdf, tmp_path / "batch")
        assert out is not None and out.suffix == ".pdf"
        assert "李四" not in _full_text(out)


# ---------------------------------------------------------------------------
# 还原往返
# ---------------------------------------------------------------------------

class TestRestore:
    def test_restore_roundtrip(self, sample_pdf):
        adapter, masker = _make_adapter()
        masked = adapter.process(sample_pdf, sample_pdf.parent / "out")

        token_map = {m.token: {"original": m.original} for m in masker.mappings}
        restored = sample_pdf.parent / "合同_restored.pdf"
        restore_file_content(masked, restored, token_map)

        after = _full_text(restored)
        assert "李四" in after
        assert "某某科技有限公司" in after
        assert not any(m.token in after for m in masker.mappings)

    def test_restore_keeps_surrounding_text(self, sample_pdf):
        """还原替换 token 时同样精确到字符：行内其余文本不动。"""
        adapter, masker = _make_adapter()
        masked = adapter.process(sample_pdf, sample_pdf.parent / "out")
        token_map = {m.token: {"original": m.original} for m in masker.mappings}
        restored = sample_pdf.parent / "合同_restored.pdf"
        restore_file_content(masked, restored, token_map)

        after = _full_text(restored)
        assert "复核人：" in after and "（与负责人同名" in after
        assert "第二页普通内容" in after
