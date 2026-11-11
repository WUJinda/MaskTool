# -*- coding: utf-8 -*-
"""tests/test_pdf_alias_roundtrip.py — PDF alias 伪装词写入/还原回归（2026-10-28 修复）

根因背景（helv 写中文退化为 · 的还原失效事故）：
- PdfAdapter._apply_edits 曾把插入字体硬编码 helv（Latin-1），alias 伪装词
  （汤姆/A集团）写入后逐字退化为 ·，文本层不含伪装词 → unmask 零命中 →
  还原静默产出脱敏件，且对账（查残留）也查不出。
- 修复：字体按替换串字符集自适应（_font_for：ASCII → helv / CJK →
  china-s，per-edit）+ 每页写入后回读校验（fail-closed）+ 还原侧零命中
  警告（restore_pdf_file 返回 warnings）。

覆盖：
- _font_for 单元行为
- alias 模式 PDF 全链路：mask → 文本层可命中每条伪装词（写入校验的直接
  断言）→ restore → 原文回现、伪装词消失
- 混合批次：同页 alias 伪装词（china-s）+ AMOUNT token（helv）各自可还原
- 写入校验 fail-closed：monkeypatch _font_for 回 helv，中文伪装词写入
  失配必须抛错（绝不静默产出损坏件——事故回归线）
- 零命中警告：映射表替换串不在输入中 → restore 返回警告而非静默照抄
- token 样式（纯 ASCII）字体行为不变
"""

from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")

from mask_tool.adapters.extract import extract_texts  # noqa: E402
from mask_tool.adapters.pdf_adapter import (  # noqa: E402
    PdfAdapter, _apply_edits, _font_for,
)
from mask_tool.adapters.restore import restore_file_content  # noqa: E402
from mask_tool.core.alias_generator import AliasGenerator  # noqa: E402
from mask_tool.core.detector import Detector  # noqa: E402
from mask_tool.core.masker import Masker  # noqa: E402
from mask_tool.core.policy import PolicyEngine  # noqa: E402
from mask_tool.models.config import MaskConfig  # noqa: E402

# 词库：人名/公司走 alias 伪装词；"人民币…元"经 regex 检出为 AMOUNT，
# alias 生成器对 AMOUNT 回退 [AMOUNT_001] token → 同页混合两种字体
LEXICON = {
    "person": ["胡世东"],
    "company": ["国铁建设集团"],
}

SAMPLE_LINES = [
    ("项目负责人：胡世东，联系多方核实", 14),
    ("业主单位：国铁建设集团（含子单位）", 14),
    ("合同金额：人民币 1,234,567.89 元整", 14),
]


def _make_pdf(path: Path) -> Path:
    doc = fitz.open()
    page = doc.new_page()
    y = 100
    for text, size in SAMPLE_LINES:
        page.insert_text((72, y), text, fontname="china-s", fontsize=size)
        y += 30
    doc.save(str(path))
    doc.close()
    return path


def _make_alias_adapter():
    detector = Detector({k: list(v) for k, v in LEXICON.items()}, set())
    policy = PolicyEngine(MaskConfig(mode="smart"))
    masker = Masker(AliasGenerator())
    return PdfAdapter(detector, policy, masker), masker


def _token_map(masker) -> dict:
    return {m.token: m.to_dict() for m in masker.get_mappings()}


# ---------------------------------------------------------------------------
# _font_for 单元行为
# ---------------------------------------------------------------------------

class TestFontFor:
    def test_ascii_token_uses_helv(self):
        assert _font_for("[AMOUNT_001]") == "helv"
        assert _font_for("[COMPANY_012]") == "helv"

    def test_cjk_alias_uses_china_s(self):
        assert _font_for("汤姆") == "china-s"
        assert _font_for("A集团") == "china-s"   # 混合 ASCII+CJK 也走 CJK 字体

    def test_empty_and_edge(self):
        assert _font_for("") == "helv"
        assert _font_for("abc-123_") == "helv"


# ---------------------------------------------------------------------------
# _apply_edits per-edit 字体（混合批次核心）
# ---------------------------------------------------------------------------

class TestApplyEditsPerEditFont:
    def test_mixed_batch_both_recoverable(self):
        """同批 ASCII token（helv）与中文伪装词（china-s）都正确落入文本层。"""
        doc = fitz.open()
        page = doc.new_page()
        # 两个 edit：手工构造最小字符矩形（每字符 bbox/origin/size）
        def _ch(x, c):
            return {"c": c, "bbox": (x, 90, x + 12, 104), "origin": (x, 101)}

        chars = [
            (0, 2, _ch(72, "甲"), {"size": 12.0}),
            (2, 4, _ch(84, "乙"), {"size": 12.0}),
        ]
        import mask_tool.adapters.pdf_adapter as pa

        e1 = pa._collect_edit(chars, 0, 2)   # 替换为中文伪装词
        e2 = pa._collect_edit(chars, 2, 4)   # 替换为 ASCII token
        _apply_edits(page, [e1, e2], ["汤姆", "[AMOUNT_001]"])
        text = page.get_text()
        doc.close()
        assert "汤姆" in text
        assert "[AMOUNT_001]" in text


# ---------------------------------------------------------------------------
# alias 模式 PDF 全链路（事故回归线）
# ---------------------------------------------------------------------------

class TestAliasPdfRoundtrip:
    def test_mask_writes_alias_into_text_layer(self, tmp_path):
        """脱敏产物文本层必须含每条映射的替换串（写入校验的直接断言），
        且敏感原文被物理擦除。"""
        src = _make_pdf(tmp_path / "src.pdf")
        adapter, masker = _make_alias_adapter()
        out = adapter.process(src, tmp_path)

        text = extract_texts(out)
        tm = _token_map(masker)
        assert tm, "词库命中为空，测试前提失效"
        alias_keys = [k for k in tm if "[" not in k]
        token_keys = [k for k in tm if "[" in k]
        # 事故复现线：helv 写中文时这里全部断言失败
        for k in alias_keys:
            assert k in text, f"伪装词 {k!r} 未落入文本层（写入损坏）"
        for k in token_keys:
            assert k in text, f"token {k!r} 未落入文本层"
        for original in (v["original"] for v in tm.values()):
            assert original not in text, f"敏感原文 {original!r} 未被擦除"

    def test_restore_recovers_originals(self, tmp_path):
        """mask → restore 往返一致：原文逐一回现，替换串零残留。"""
        src = _make_pdf(tmp_path / "src.pdf")
        adapter, masker = _make_alias_adapter()
        masked = adapter.process(src, tmp_path)
        tm = _token_map(masker)

        restored = tmp_path / "restored.pdf"
        warnings = restore_file_content(masked, restored, tm)
        assert warnings == []

        text = extract_texts(restored)
        for entry in tm.values():
            assert entry["original"] in text, f"原文 {entry['original']!r} 未回现"
            # 同名多次出现的替换串应全部还原（残留即缺陷）
            assert not (
                "[" not in entry["token"] and entry["token"] in text
            ), f"伪装词 {entry['token']!r} 还原后残留"

    def test_write_verify_fail_closed(self, tmp_path, monkeypatch):
        """字体/编码回归（如 _font_for 误回 helv）必须当场抛错，
        绝不静默产出不可还原的损坏件。"""
        import mask_tool.adapters.pdf_adapter as pa

        monkeypatch.setattr(pa, "_font_for", lambda text: "helv")
        src = _make_pdf(tmp_path / "src.pdf")
        adapter, _ = _make_alias_adapter()
        with pytest.raises(ValueError, match="写入校验失败"):
            adapter.process(src, tmp_path)


# ---------------------------------------------------------------------------
# 还原侧零命中警告（新防线）
# ---------------------------------------------------------------------------

class TestRestoreZeroHitWarning:
    def test_zero_hit_returns_warning(self, tmp_path):
        """映射表替换串在输入中一次都没出现 → 返回警告而非静默照抄。"""
        src = _make_pdf(tmp_path / "plain.pdf")
        tm = {"汤姆": {"original": "胡世东"},
              "[AMOUNT_001]": {"original": "人民币 1,234,567.89 元"}}
        out = tmp_path / "restored.pdf"
        warnings = restore_file_content(src, out, tm)
        assert len(warnings) == 1
        assert "0 命中" in warnings[0]
        # 产物与输入同文（照抄兜底行为不变，但已有明确警告）
        assert extract_texts(out) == extract_texts(src)

    def test_normal_hit_returns_no_warning(self, tmp_path):
        src = _make_pdf(tmp_path / "src.pdf")
        adapter, masker = _make_alias_adapter()
        masked = adapter.process(src, tmp_path)
        tm = _token_map(masker)
        out = tmp_path / "restored.pdf"
        assert restore_file_content(masked, out, tm) == []

    def test_empty_token_map_no_warning(self, tmp_path):
        """空映射表（0 条）不触发零命中警告（无预期即无异常）。"""
        src = _make_pdf(tmp_path / "src.pdf")
        out = tmp_path / "restored.pdf"
        assert restore_file_content(src, out, {}) == []
