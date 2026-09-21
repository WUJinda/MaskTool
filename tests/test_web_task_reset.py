# -*- coding: utf-8 -*-
"""tests/test_web_task_reset.py — I6：新建任务入口与任务态清理

覆盖：
- _reset_task_state：任务态键全清（检测/勾选/产物/筛选/批次/上传组件），
  配置态键保留（自定义敏感词/仅手动开关/文件名脱敏/learn_set），
  tmp_dir 指向的上传临时目录被物理删除
- _clear_task_state(clear_upload=False)：保留上传组件（同批文件重走语义）
- file_uploader key 重置（pop 逻辑）
- _run_detection 开头防御：残留 user_selections / learn_set 旧索引失效重置
- 全链路：脱敏完成后 _reset_task_state → session 干净 → 新任务可重新检测
"""

import io
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from docx import Document

from mask_tool.core.templates import (
    DEFAULT_CONFIG_YAML, SAMPLE_LEXICON_YAML, WHITELIST_YAML,
)

LEXICON = {"company": ["某某科技有限公司"], "person": ["李四"]}


@pytest.fixture(autouse=True)
def _cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write_config_dir(lexicon=None):
    cfgdir = Path("config")
    cfgdir.mkdir(parents=True, exist_ok=True)
    default = DEFAULT_CONFIG_YAML.replace("enabled: true", "enabled: false")
    (cfgdir / "default.yaml").write_text(default, encoding="utf-8")
    (cfgdir / "whitelist.yaml").write_text(WHITELIST_YAML, encoding="utf-8")
    (cfgdir / "sample_lexicon.yaml").write_text(SAMPLE_LEXICON_YAML, encoding="utf-8")
    (cfgdir / "lexicon.yaml").write_text(
        yaml.dump(lexicon or LEXICON, allow_unicode=True), encoding="utf-8",
    )


def _make_docx_bytes(paragraphs) -> bytes:
    buf = io.BytesIO()
    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    doc.save(buf)
    return buf.getvalue()


def _upload(name: str, payload: bytes) -> SimpleNamespace:
    return SimpleNamespace(name=name, read=lambda: payload)


def _patch_streamlit(monkeypatch, web):
    monkeypatch.setattr(web, "BATCHES_DIR", Path("batches"))
    monkeypatch.setattr(web, "_add_history", lambda rec: None)
    for fn in ("rerun", "error", "warning", "info", "success", "caption"):
        monkeypatch.setattr(web.st, fn, lambda *a, **k: None, raising=False)
    monkeypatch.setattr(
        web.st, "spinner", lambda *a, **k: _NullCtx(), raising=False,
    )
    for key in list(web.st.session_state.keys()):
        del web.st.session_state[key]


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _seed_task_state(web) -> Path:
    """造一套完整任务态 + 配置态，返回临时目录 Path（用于断言被删）。"""
    ss = web.st.session_state
    # 任务态
    tmp_dir = Path(tempfile.mkdtemp())
    (tmp_dir / "a.docx").write_bytes(b"fake")
    ss["detection_results"] = ["r1", "r2"]
    ss["file_results"] = {"a.docx": ["r1"]}
    ss["user_selections"] = {0: True, 1: False}
    ss["tmp_dir"] = str(tmp_dir)
    ss["saved_paths"] = [str(tmp_dir / "a.docx")]
    ss["mask_result"] = {"batch_id": "X"}
    ss["restore_result"] = {"file_count": 1}
    ss["filter_type"] = "全部"
    ss["filter_status"] = "全部"
    ss["filter_source"] = "✍️ 手动"
    ss["filter_file"] = "a.docx"
    ss["search_text"] = "关键词"
    ss["batch_name_input"] = "旧批次"
    ss["pending_batch_id"] = "MSK-OLD"          # R7：对话框生成的批次ID
    ss["mask_dialog_token"] = "deadbeef"         # R7：对话框勾选 token
    ss["dlg_sel_deadbeef_0"] = True               # R7：对话框勾选态（随任务清理）
    ss["file_uploader"] = [SimpleNamespace(name="a.docx")]
    # 配置态
    ss["custom_words_input"] = "绝密项目代号，内部代号X7"
    ss["manual_only_mode"] = True
    ss["mask_filenames"] = False
    ss["learn_set"] = {0, 1}
    return tmp_dir


TASK_KEYS = [
    "detection_results", "file_results", "user_selections", "tmp_dir",
    "saved_paths", "mask_result", "restore_result", "filter_type",
    "filter_status", "filter_source", "filter_file", "search_text",
    "batch_name_input", "pending_batch_id", "mask_dialog_token",
    "dlg_sel_deadbeef_0",
]
CONFIG_KEYS = ["custom_words_input", "manual_only_mode", "mask_filenames",
               "learn_set"]


class TestResetTaskState:

    def test_reset_clears_task_keeps_config_and_deletes_tmp(self, monkeypatch):
        """_reset_task_state：任务态全清、配置态保留、tmp_dir 目录被删。"""
        web = pytest.importorskip("mask_tool.web.app")
        _patch_streamlit(monkeypatch, web)
        tmp_dir = _seed_task_state(web)

        web._reset_task_state()

        for key in TASK_KEYS + ["file_uploader"]:
            assert key not in web.st.session_state, f"任务态未清: {key}"
        for key in CONFIG_KEYS:
            assert key in web.st.session_state, f"配置态被误清: {key}"
        assert web.st.session_state["custom_words_input"] == "绝密项目代号，内部代号X7"
        assert web.st.session_state["manual_only_mode"] is True
        assert web.st.session_state["learn_set"] == {0, 1}
        assert not tmp_dir.exists(), "上传临时目录未删除"

    def test_reset_pops_file_uploader_key(self, monkeypatch):
        """file_uploader key 被 pop（UI 上传区清空）。"""
        web = pytest.importorskip("mask_tool.web.app")
        _patch_streamlit(monkeypatch, web)
        web.st.session_state["file_uploader"] = ["f1", "f2"]
        web._reset_task_state()
        assert "file_uploader" not in web.st.session_state

    def test_clear_keep_upload_retains_file_uploader(self, monkeypatch):
        """_clear_task_state(clear_upload=False)：上传组件保留（同批文件重走）。"""
        web = pytest.importorskip("mask_tool.web.app")
        _patch_streamlit(monkeypatch, web)
        tmp_dir = _seed_task_state(web)

        web._clear_task_state(clear_upload=False)

        assert "file_uploader" in web.st.session_state
        assert len(web.st.session_state["file_uploader"]) == 1
        for key in TASK_KEYS:
            assert key not in web.st.session_state, f"任务态未清: {key}"
        assert not tmp_dir.exists()

    def test_reset_on_clean_state_is_noop(self, monkeypatch):
        """空 session 上调用不抛错（幂等）。"""
        web = pytest.importorskip("mask_tool.web.app")
        _patch_streamlit(monkeypatch, web)
        web._reset_task_state()
        assert list(web.st.session_state.keys()) == []


class TestDetectionResetsStaleIndexes:

    def test_run_detection_invalidates_stale_selections(self, monkeypatch):
        """新一轮检测：残留 user_selections / learn_set 旧索引被重置
        （learn_set 旧索引会把不相关的词写进词库，必须失效）。"""
        web = pytest.importorskip("mask_tool.web.app")
        _write_config_dir()
        _patch_streamlit(monkeypatch, web)
        web.st.session_state["user_selections"] = {0: True, 99: True}
        web.st.session_state["learn_set"] = {0, 99}

        web._run_detection(
            [_upload("a.docx", _make_docx_bytes(["负责人李四对接"]))],
            "smart", ner_enabled=False, manual_words=[], manual_only=False,
        )

        assert "learn_set" not in web.st.session_state
        # user_selections 由渲染层按新结果重建（此处检测函数内已 pop）
        results = web.st.session_state["detection_results"]
        assert any(r.text == "李四" for r in results)


class TestFullCycleReset:

    def test_mask_complete_then_reset_then_new_detection(self, monkeypatch):
        """脱敏完成 → 开始新任务（_reset_task_state）→ session 干净、
        自定义词保留 → 新任务可正常检测（全流程可重新开始）。"""
        web = pytest.importorskip("mask_tool.web.app")
        _write_config_dir()
        _patch_streamlit(monkeypatch, web)

        upload = _upload(
            "a.docx", _make_docx_bytes(["内部代号绝密项目代号，负责人李四对接"]),
        )
        manual = ["绝密项目代号"]
        web._run_detection(
            [upload], "smart", ner_enabled=False,
            manual_words=manual, manual_only=False,
        )
        web._run_masking(
            [upload], [0], web.st.session_state["detection_results"],
            mode="smart", ner_enabled=False, irreversible=False,
            learn_words=False, batch_id="MSK-R1", batch_name="t1",
            mask_filenames=False, manual_words=manual, manual_only=False,
        )
        assert "mask_result" in web.st.session_state

        # 开始新任务
        web.st.session_state["custom_words_input"] = "全新任务词"
        web._reset_task_state()

        assert "mask_result" not in web.st.session_state
        assert "detection_results" not in web.st.session_state
        assert "tmp_dir" not in web.st.session_state
        assert "file_uploader" not in web.st.session_state
        assert web.st.session_state["custom_words_input"] == "全新任务词"

        # 新任务：另一个文件、另一套手动词，流程正常
        upload2 = _upload(
            "b.docx", _make_docx_bytes(["内部代号X7待处理"]),
        )
        web._run_detection(
            [upload2], "smart", ner_enabled=False,
            manual_words=["内部代号X7"], manual_only=True,
        )
        results2 = web.st.session_state["detection_results"]
        assert [(r.text, r.source) for r in results2] == [
            ("内部代号X7", "manual"),
        ]
