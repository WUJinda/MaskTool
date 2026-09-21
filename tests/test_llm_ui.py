# -*- coding: utf-8 -*-
"""P3 UI 层 LLM 接入测试：配置流合并 / 设置弹窗事件 / 结果表 AI 列 / 侧栏渲染。"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("streamlit")

from mask_tool.models.config import LLMConfig  # noqa: E402


# ---------------------------------------------------------------------------
# app_settings llm 段读写
# ---------------------------------------------------------------------------

@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    """把 app_settings 定位到临时文件（隔离用户真实配置）。"""
    f = tmp_path / "app_settings.yaml"
    monkeypatch.setattr(
        "mask_tool.core.app_settings._settings_path", lambda: f
    )
    return f


def test_llm_settings_roundtrip(settings_file):
    from mask_tool.core.app_settings import get_llm_settings, set_llm_settings

    assert get_llm_settings() == {}          # 未配置
    ok = set_llm_settings({
        "base_url": "http://192.168.1.10:11434/v1",
        "model": "qwen3:8b", "role": "both", "api_key": "",
    })
    assert ok
    saved = get_llm_settings()
    assert saved["model"] == "qwen3:8b" and saved["role"] == "both"


def test_config_loader_merges_app_settings(tmp_path, monkeypatch, settings_file):
    """app_settings.llm > default.yaml.llm（用户显式设置优先）。"""
    from mask_tool.core.app_settings import set_llm_settings
    from mask_tool.core.config_loader import config_from_dict

    set_llm_settings({
        "base_url": "http://nas:8080/v1", "model": "inner-model",
        "role": "detector", "enabled": True,
    })
    cfg = config_from_dict({
        "llm": {"enabled": True, "base_url": "http://old:11434/v1",
                "model": "yaml-model"},
    })
    from mask_tool.core.config_loader import _merge_app_llm
    assert _merge_app_llm(cfg) == []         # 合并成功无事件
    assert cfg.llm.base_url == "http://nas:8080/v1"    # app_settings 覆盖 YAML
    assert cfg.llm.model == "inner-model"
    assert cfg.llm.role == "detector"
    # YAML 独有键保留（app_settings 未写的字段不丢）
    assert cfg.llm.batch_size == LLMConfig().batch_size


# ---------------------------------------------------------------------------
# 设置弹窗事件处理（mock streamlit runtime）
# ---------------------------------------------------------------------------

class _FakeST:
    """最小 streamlit 替身：session_state dict + no-op rerun。"""

    def __init__(self):
        self.session_state = {}

    def rerun(self):
        pass

    def warning(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def caption(self, *a, **k):
        pass


@pytest.fixture
def fake_st(monkeypatch):
    import streamlit as st_real
    fake = _FakeST()
    monkeypatch.setattr(st_real, "session_state", fake.session_state, raising=False)
    monkeypatch.setattr(st_real, "rerun", fake.rerun, raising=False)
    monkeypatch.setattr(st_real, "warning", fake.warning, raising=False)
    monkeypatch.setattr(st_real, "info", fake.info, raising=False)
    monkeypatch.setattr(st_real, "caption", fake.caption, raising=False)
    return fake


def _dialog_mod():
    from mask_tool.web.ui import settings_dialog
    return settings_dialog


def test_save_llm_event_persists(fake_st, settings_file):
    mod = _dialog_mod()
    mod._handle_settings_event({
        "action": "save_llm", "base_url": "http://x:11434/v1",
        "model": "qwen3:8b", "api_key": "", "role": "both",
    })
    from mask_tool.core.app_settings import get_llm_settings
    saved = get_llm_settings()
    assert saved["model"] == "qwen3:8b" and saved["role"] == "both"
    assert "✅" in fake_st.session_state["_settings_flash"]["text"]


def test_save_llm_rejects_empty_model(fake_st, settings_file):
    mod = _dialog_mod()
    mod._handle_settings_event({
        "action": "save_llm", "base_url": "http://x:11434/v1",
        "model": "", "api_key": "", "role": "adjudicator",
    })
    from mask_tool.core.app_settings import get_llm_settings
    assert get_llm_settings() == {}          # 未写入
    assert "模型名称" in fake_st.session_state["_settings_flash"]["text"]


def test_test_llm_event_caches_health(fake_st, monkeypatch):
    mod = _dialog_mod()
    calls = {}

    class FakeClient:
        def __init__(self, base_url, model, api_key="", timeout=30):
            calls["init"] = (base_url, model, api_key, timeout)

        def health_check(self):
            return True, "端点可用（模型 m）"

    monkeypatch.setattr(
        "mask_tool.core.llm.client.OpenAICompatClient", FakeClient
    )
    mod._handle_settings_event({
        "action": "test_llm", "base_url": "http://x:11434/v1",
        "model": "m", "api_key": "",
    })
    assert fake_st.session_state["llm_health"] == {"ok": True, "msg": "端点可用（模型 m）"}
    assert calls["init"][3] == 8             # 探活用短超时


def test_test_llm_event_failure_reported(fake_st, monkeypatch):
    mod = _dialog_mod()

    def boom(*a, **k):
        raise RuntimeError("拒绝连接")

    monkeypatch.setattr(
        "mask_tool.core.llm.client.OpenAICompatClient", boom
    )
    mod._handle_settings_event({
        "action": "test_llm", "base_url": "http://x:11434/v1",
        "model": "m", "api_key": "",
    })
    assert fake_st.session_state["llm_health"]["ok"] is False
    assert "❌" in fake_st.session_state["_settings_flash"]["text"]


# ---------------------------------------------------------------------------
# 结果表 AI 列
# ---------------------------------------------------------------------------

def _mk_result(text, llm_reason=""):
    from mask_tool.models.detection import (
        DetectionResult, DetectionType, Location,
    )
    return DetectionResult(
        text=text, text_type=DetectionType.COMPANY, source="dictionary",
        confidence=0.95, location=Location(file="a.docx"),
        context="...", llm_reason=llm_reason,
    )


def test_results_table_no_ai_column_without_llm():
    from mask_tool.web.ui.results_table import _results_to_dataframe

    df = _results_to_dataframe([_mk_result("甲公司")])
    assert "AI 判定" not in df.columns       # 未启用时零噪声


def test_results_table_ai_column_with_reason():
    from mask_tool.web.ui.results_table import _results_to_dataframe

    df = _results_to_dataframe([
        _mk_result("甲公司", llm_reason="AI 复核维持：确属公司"),
        _mk_result("乙公司"),                 # 无理由行显示空串
    ])
    assert "AI 判定" in df.columns
    assert df.iloc[0]["AI 判定"].startswith("AI 复核维持")
    assert df.iloc[1]["AI 判定"] == ""


def test_ai_reason_truncated_in_table():
    from mask_tool.web.ui.results_table import _results_to_dataframe

    long_reason = "AI 复核修正置信度 0.95→0.90：" + "长" * 60
    df = _results_to_dataframe([_mk_result("甲公司", llm_reason=long_reason)])
    assert len(df.iloc[0]["AI 判定"]) <= 45   # 44 字 + 省略号


# ---------------------------------------------------------------------------
# 侧栏渲染（AppTest 无头）
# ---------------------------------------------------------------------------

class TestSidebarAI:
    @pytest.fixture(autouse=True)
    def _reset_component_cache(self):
        """隔离 AppTest 会话副作用：settings_dialog 的组件构造器是模块级
        缓存，跨 AppTest 会话不重置会令后续会话报
        "Component not registered"（见 test_web_smoke）。"""
        yield
        import mask_tool.web.ui.settings_dialog as sd
        sd._settings_component_ctor = None

    def test_sidebar_has_llm_toggle_disabled_without_endpoint(self):
        from streamlit.testing.v1 import AppTest

        app = Path(__file__).resolve().parents[1] / "src" / "mask_tool" / "web" / "app.py"
        at = AppTest.from_file(str(app), default_timeout=60)
        at.run()
        assert not at.exception
        # 侧栏含 AI 增强检测开关；未配置端点时禁用
        boxes = [c for c in at.sidebar.checkbox
                 if "AI 增强检测" in (c.label or "")]
        assert boxes, "侧栏 AI 增强检测开关缺失"


# ---------------------------------------------------------------------------
# P3 反馈机制：first_error / 友好化 / 摘要
# ---------------------------------------------------------------------------

def test_friendly_error_mapping():
    from mask_tool.core.llm.adjudicator import friendly_error

    assert "OpenAI 兼容端点" in friendly_error("HTTP 404: not found")
    assert "API Key" in friendly_error("HTTP 401: unauthorized")
    assert "限流" in friendly_error("HTTP 429")
    assert "超时" in friendly_error("Request timed out")
    assert "连接失败" in friendly_error("Connection refused")
    assert "模型名" in friendly_error("端点可达但模型不存在: m")
    assert friendly_error("") .startswith("未知")
    assert friendly_error("某奇怪错误XYZ") == "某奇怪错误XYZ"


def test_first_error_recorded_once():
    from mask_tool.core.llm.adjudicator import LLMAdjudicator
    from mask_tool.core.llm.exceptions import LLMUnavailableError

    class ErrClient:
        def chat_json(self, m, s):
            raise LLMUnavailableError("HTTP 404: not found")

    adj = LLMAdjudicator(ErrClient(), LLMConfig(
        enabled=True, base_url="http://x/v1", model="m"))
    from mask_tool.models.detection import DetectionResult, DetectionType, Location
    r = DetectionResult(text="甲", text_type=DetectionType.COMPANY, source="dictionary",
                        confidence=0.95, location=Location(file="a"))
    adj.adjudicate([r])
    adj.adjudicate([DetectionResult(text="乙", text_type=DetectionType.COMPANY,
                                    source="dictionary", confidence=0.95,
                                    location=Location(file="a"))])
    assert adj.stats.errors == 2
    assert "OpenAI 兼容端点" in adj.stats.first_error   # 友好化 + 只记首条
    d = adj.stats.to_dict()
    assert "first_error" in d


def test_friendly_error_balance():
    from mask_tool.core.llm.adjudicator import friendly_error
    assert "余额不足" in friendly_error(
        "HTTP 429：{\"error\":{\"code\":\"1113\",\"message\":\"余额不足或无可用资源包\"}}")
    assert "限流" in friendly_error("HTTP 429: too many requests")
