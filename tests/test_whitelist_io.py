# -*- coding: utf-8 -*-
"""tests/test_whitelist_io.py — 白名单 IO 回归（R9，2026-09-21）

白名单（config/whitelist.yaml 的 whitelist 列表）服务于两个入口：
检测页「对未勾选的敏感词进行永久排除」与设置弹窗白名单维护。
此处验证读写合并删除的正确性与文件结构兼容（顶层 {whitelist: [...]}）。
锚点链注入临时目录，避免触碰真实 config/。
"""

import yaml

import pytest

lexicon_io = pytest.importorskip("mask_tool.web.ui.lexicon_io")
config_loader = pytest.importorskip("mask_tool.core.config_loader")


@pytest.fixture(autouse=True)
def _isolated_anchors(tmp_path, monkeypatch):
    """白名单锚点链指向临时目录（含可写锚点），隔离真实 config/。"""
    monkeypatch.setattr(
        config_loader, "runtime_anchor_dirs", lambda: [tmp_path]
    )
    monkeypatch.setattr(
        config_loader, "writable_anchor_dir", lambda: tmp_path
    )
    yield


def _wl_file(tmp_path):
    return tmp_path / "config" / "whitelist.yaml"


def test_merge_creates_file_with_canonical_structure(tmp_path):
    added, dup = lexicon_io._merge_words_into_whitelist(["误报词", "张三"])
    assert (added, dup) == (2, 0)
    p = _wl_file(tmp_path)
    assert p.exists()
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data == {"whitelist": ["误报词", "张三"]}
    assert lexicon_io._get_whitelist() == ["误报词", "张三"]


def test_merge_into_existing_file_dedups(tmp_path):
    lexicon_io._merge_words_into_whitelist(["A", "B"])
    added, dup = lexicon_io._merge_words_into_whitelist(["B", "C", "  ", ""])
    assert (added, dup) == (1, 1)  # B 重复；空白词跳过不计
    assert lexicon_io._get_whitelist() == ["A", "B", "C"]


def test_merge_all_dup_reports_zero_added(tmp_path):
    lexicon_io._merge_words_into_whitelist(["X"])
    assert lexicon_io._merge_words_into_whitelist(["X"]) == (0, 1)


def test_remove_words(tmp_path):
    lexicon_io._merge_words_into_whitelist(["A", "B", "C"])
    assert lexicon_io._remove_whitelist_words(["B", "不存在"]) == 1
    assert lexicon_io._get_whitelist() == ["A", "C"]


def test_remove_from_missing_file_returns_zero(tmp_path):
    assert lexicon_io._remove_whitelist_words(["A"]) == 0


def test_get_whitelist_tolerates_corrupt_file(tmp_path):
    p = _wl_file(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text("whitelist: [broken", encoding="utf-8")
    assert lexicon_io._get_whitelist() == []


def test_get_whitelist_missing_file(tmp_path):
    assert lexicon_io._get_whitelist() == []


def test_merge_preserves_real_world_factory_shape(tmp_path):
    """出厂文件形态（含注释与既有词条）追加后仍可被检测链路解析。"""
    p = _wl_file(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text(
        "# 白名单 - 这些词不会被脱敏\nwhitelist:\n  - 有限公司\n",
        encoding="utf-8",
    )
    added, _ = lexicon_io._merge_words_into_whitelist(["新误报词"])
    assert added == 1
    assert lexicon_io._get_whitelist() == ["有限公司", "新误报词"]
