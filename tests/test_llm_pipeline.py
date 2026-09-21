# -*- coding: utf-8 -*-
"""Pipeline LLM 接线集成测试：包装开关 / focused 不接入 / 降级 / 审计。

默认关闭回归基线：llm.enabled=False（默认）时 detector 为原生 Detector，
report 输出与无 LLM 时完全一致。
"""

import json

import pytest

from mask_tool.core.detector import Detector
from mask_tool.core.llm.detector_wrapper import LLMEnhancedDetector
from mask_tool.models.config import MaskConfig


def _cfg_with_llm(**llm_kw):
    cfg = MaskConfig()
    base = dict(enabled=True, base_url="http://x:11434/v1", model="test-model")
    base.update(llm_kw)
    cfg.llm.__dict__.update(base)
    return cfg


class FakeLLMClient:
    """注入到 pipeline 接线的假客户端（monkeypatch OpenAICompatClient）。"""

    instances = []

    def __init__(self, base_url, model, api_key="", timeout=30, session=None):
        self.base_url = base_url
        self.model = model
        self.responses = []
        self.calls = 0
        FakeLLMClient.instances.append(self)

    def chat_json(self, messages, schema):
        self.calls += 1
        if self.responses:
            return self.responses.pop(0)
        return {"items": []}

    def health_check(self):
        return True, "ok"


class FakeLLMDetectClient:
    """P2：同时模拟 detect_new（entities）与 adjudicate（items）的假客户端。"""

    instances = []

    def __init__(self, base_url, model, api_key="", timeout=30, session=None):
        self.base_url = base_url
        self.model = model
        self.detect_responses = []      # detect_new 依次弹出
        self.adjudicate_responses = []  # adjudicate 依次弹出
        self.detect_calls = 0
        self.adjudicate_calls = 0
        FakeLLMDetectClient.instances.append(self)

    def chat_json(self, messages, schema):
        is_detect = "entities" in schema.get("properties", {})
        if is_detect:
            self.detect_calls += 1
            if self.detect_responses:
                return {"entities": self.detect_responses.pop(0)}
            return {"entities": []}
        self.adjudicate_calls += 1
        if self.adjudicate_responses:
            return {"items": self.adjudicate_responses.pop(0)}
        return {"items": []}

    def health_check(self):
        return True, "ok"


@pytest.fixture(autouse=True)
def _reset_instances():
    FakeLLMClient.instances = []
    FakeLLMDetectClient.instances = []
    yield
    FakeLLMClient.instances = []
    FakeLLMDetectClient.instances = []


@pytest.fixture
def patch_client(monkeypatch):
    monkeypatch.setattr(
        "mask_tool.core.llm.client.OpenAICompatClient", FakeLLMClient
    )


@pytest.fixture
def patch_detect_client(monkeypatch):
    monkeypatch.setattr(
        "mask_tool.core.llm.client.OpenAICompatClient", FakeLLMDetectClient
    )


def _new_pipeline(cfg, tmp_path):
    """构造 Pipeline（词库指向临时空文件，避免依赖仓库根词库）。"""
    from mask_tool.core.pipeline import Pipeline

    lex = tmp_path / "lex.yaml"
    lex.write_text("company:\n  - 测试建设集团有限公司\n", encoding="utf-8")
    wl = tmp_path / "wl.yaml"
    wl.write_text("whitelist: []\n", encoding="utf-8")
    return Pipeline(cfg, lexicon_path=str(lex), whitelist_path=str(wl))


# ---------------------------------------------------------------------------
# 包装开关
# ---------------------------------------------------------------------------

def test_disabled_by_default_keeps_plain_detector(tmp_path):
    cfg = MaskConfig()
    assert cfg.llm.enabled is False
    p = _new_pipeline(cfg, tmp_path)
    assert type(p.detector) is Detector
    assert not isinstance(p.detector, LLMEnhancedDetector)
    assert p.llm_stats is None


def test_enabled_wraps_detector(tmp_path, patch_client):
    cfg = _cfg_with_llm()
    p = _new_pipeline(cfg, tmp_path)
    assert isinstance(p.detector, LLMEnhancedDetector)
    assert isinstance(p.detector.base_detector, Detector)
    assert p.llm_stats is not None
    assert p.llm_stats.model == "test-model"


def test_focused_mode_never_wraps(tmp_path, patch_client):
    """focused 语义为"仅信词库"，即使 enabled 也不接入。"""
    cfg = _cfg_with_llm()
    cfg.mode = "focused"
    p = _new_pipeline(cfg, tmp_path)
    assert type(p.detector) is Detector


def test_missing_endpoint_warns_and_stays_plain(tmp_path, caplog):
    cfg = _cfg_with_llm(base_url="", model="")
    with caplog.at_level("WARNING"):
        p = _new_pipeline(cfg, tmp_path)
    assert type(p.detector) is Detector
    assert any("base_url/model 未配置" in r.message for r in caplog.records)


def test_init_failure_degrades_to_plain(tmp_path, monkeypatch, caplog):
    """客户端构造异常（如 base_url 非法）时回退纯规则，不抛出。"""
    def boom(*a, **kw):
        raise RuntimeError("构造失败")

    monkeypatch.setattr(
        "mask_tool.core.llm.client.OpenAICompatClient", boom
    )
    cfg = _cfg_with_llm()
    with caplog.at_level("WARNING"):
        p = _new_pipeline(cfg, tmp_path)
    assert type(p.detector) is Detector
    assert any("回退纯规则" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 端到端行为（run_text）
# ---------------------------------------------------------------------------

def test_run_text_with_llm_adjust_applied(tmp_path, patch_client):
    """LLM drop 判定把词库命中压到建议档之下，policy 降级 HINT_ONLY。"""
    cfg = _cfg_with_llm()
    p = _new_pipeline(cfg, tmp_path)
    client = FakeLLMClient.instances[0]
    client.responses = [{"items": [
        {"id": 0, "action": "drop", "reason": "示例词库词，非真实敏感"},
    ]}]

    text = "测试建设集团有限公司承建本项目"
    masked = p.process_text(text)
    # 词库命中被 AI 剔除 → 不替换，原文保留
    assert masked == text
    assert p.llm_stats.calls == 1
    assert p.llm_stats.dropped == 1


def test_run_text_llm_keep_preserves_behavior(tmp_path, patch_client):
    cfg = _cfg_with_llm()
    p = _new_pipeline(cfg, tmp_path)
    client = FakeLLMClient.instances[0]
    client.responses = [{"items": [
        {"id": 0, "action": "keep", "reason": "确属公司"},
    ]}]

    text = "测试建设集团有限公司承建本项目"
    masked = p.process_text(text)
    assert masked != text          # 正常脱敏（词库 0.95 → AUTO_MASK）
    assert "测试建设集团" not in masked


def test_run_text_llm_error_degrades_gracefully(tmp_path, patch_client):
    """端点故障（网络异常）时整段按规则结果处理，流程不中断。"""
    from mask_tool.core.llm.exceptions import LLMUnavailableError

    cfg = _cfg_with_llm()
    p = _new_pipeline(cfg, tmp_path)

    client = FakeLLMClient.instances[0]
    client.chat_json = lambda m, s: (_ for _ in ()).throw(
        LLMUnavailableError("端点不通")
    )

    text = "测试建设集团有限公司承建本项目"
    masked = p.process_text(text)
    assert masked != text                       # 规则脱敏照常
    assert p.llm_stats.errors >= 1


# ---------------------------------------------------------------------------
# 审计（report）
# ---------------------------------------------------------------------------

def test_report_contains_llm_stats_when_used(tmp_path, patch_client):
    cfg = _cfg_with_llm()
    p = _new_pipeline(cfg, tmp_path)
    FakeLLMClient.instances[0].responses = [
        {"items": [{"id": 0, "action": "keep", "reason": "ok"}]}
    ]
    p.process_text("测试建设集团有限公司承建本项目")

    report_file = tmp_path / "report.json"
    p.save_report(report_file)
    data = json.loads(report_file.read_text(encoding="utf-8"))
    assert "llm_stats" in data
    assert data["llm_stats"]["calls"] == 1
    assert data["llm_stats"]["model"] == "test-model"
    # keep 判定的 llm_reason 随条目入报告
    entries = data["auto_masked"] + data["suggested"] + data["hints"]
    assert any("AI 复核维持" in e.get("llm_reason", "") for e in entries)


def test_report_omits_llm_section_when_disabled(tmp_path):
    """关闭开关时 report.json 与无 LLM 版本完全一致（零差异回归基线）。"""
    cfg = MaskConfig()
    p = _new_pipeline(cfg, tmp_path)
    p.process_text("测试建设集团有限公司承建本项目")
    report_file = tmp_path / "report.json"
    p.save_report(report_file)
    data = json.loads(report_file.read_text(encoding="utf-8"))
    assert "llm_stats" not in data
    # 条目无 llm_reason 键
    for section in ("auto_masked", "suggested", "hints"):
        for e in data[section]:
            assert "llm_reason" not in e


def test_manual_words_not_adjudicated_in_pipeline(tmp_path, patch_client):
    """手动词（source=manual）不送 LLM：用户显式指定优先于 AI。"""
    cfg = _cfg_with_llm()
    lex = tmp_path / "lex.yaml"
    lex.write_text("company: []\n", encoding="utf-8")
    wl = tmp_path / "wl.yaml"
    wl.write_text("whitelist: []\n", encoding="utf-8")
    from mask_tool.core.pipeline import Pipeline

    p = Pipeline(cfg, lexicon_path=str(lex), whitelist_path=str(wl),
                 manual_words=["绝密代号"])
    client = FakeLLMClient.instances[0]
    masked = p.process_text("项目内部称绝密代号推进")
    # manual 词正常脱敏（用户指定优先级最高）
    assert "绝密代号" not in masked
    # manual 不产生 LLM 调用；无其他命中 → calls == 0
    assert client.calls == 0


# ---------------------------------------------------------------------------
# P2：role 编排（adjudicator / detector / both）
# ---------------------------------------------------------------------------

def test_role_detector_finds_new_entity_suggest_tier(tmp_path, patch_detect_client):
    """role=detector：AI 检出规则未覆盖实体；smart 下 0.80 封顶 → 建议档，
    默认流程（仅替换 AUTO_MASK）不替换原文。"""
    cfg = _cfg_with_llm(role="detector")
    p = _new_pipeline(cfg, tmp_path)
    client = FakeLLMDetectClient.instances[0]
    client.detect_responses = [
        # 词库外实体：只有 LLM 能检出（词典/正则/NER 均不命中）
        [{"text": "新联置业发展集团", "type": "company", "confidence": 0.9}],
    ]

    text = "另悉新联置业发展集团亦参与本标段"
    masked = p.process_text(text)
    # 0.80 封顶 < smart auto 0.85 → SUGGEST_MASK → 默认不替换
    assert masked == text
    assert client.detect_calls == 1 and client.adjudicate_calls == 0
    assert p.llm_stats.detected == 1
    # 建议档条目进入报告 suggested 段（source=llm）
    assert any(e.get("source") == "llm" for e in p.report.suggested)


def test_role_detector_aggressive_auto_masks(tmp_path, patch_detect_client):
    """aggressive（auto 0.70）下 AI 检出 0.80 → AUTO_MASK → 替换。"""
    cfg = _cfg_with_llm(role="detector")
    cfg.mode = "aggressive"
    p = _new_pipeline(cfg, tmp_path)
    client = FakeLLMDetectClient.instances[0]
    client.detect_responses = [
        [{"text": "测试建设集团有限公司", "type": "company", "confidence": 0.9}],
    ]

    text = "本工程由测试建设集团有限公司承建"
    masked = p.process_text(text)
    assert masked != text
    assert "测试建设集团" not in masked


def test_role_adjudicator_skips_detect_new(tmp_path, patch_detect_client):
    """role=adjudicator（默认）：不做增量检测，只复核。"""
    cfg = _cfg_with_llm(role="adjudicator")
    p = _new_pipeline(cfg, tmp_path)
    client = FakeLLMDetectClient.instances[0]
    client.adjudicate_responses = [
        {"items": [{"id": 0, "action": "keep", "reason": "ok"}]},
    ]
    p.process_text("测试建设集团有限公司承建")
    assert client.detect_calls == 0
    assert client.adjudicate_calls == 1
    assert p.detector.role == "adjudicator"


def test_role_both_detect_and_adjudicate(tmp_path, patch_detect_client):
    """role=both：先增量检测再复核；llm 检出项不进复核（无自证循环）。"""
    cfg = _cfg_with_llm(role="both")
    p = _new_pipeline(cfg, tmp_path)
    client = FakeLLMDetectClient.instances[0]
    client.detect_responses = [
        [{"text": "新检出公司", "type": "company", "confidence": 0.9}],
    ]
    # 文本须含 LLM 检出的实体（幻觉防护要求逐字出现）
    text = "另悉新检出公司也参与投标，测试建设集团有限公司为总包"
    p.process_text(text)
    assert client.detect_calls == 1
    # 复核只收到规则命中（词典 1 项），llm 项跳过 → 仍 1 次复核调用
    assert client.adjudicate_calls == 1


def test_invalid_role_falls_back_to_adjudicator(tmp_path, patch_detect_client, caplog):
    cfg = _cfg_with_llm(role="wizard")
    with caplog.at_level("WARNING"):
        p = _new_pipeline(cfg, tmp_path)
    assert p.detector.role == "adjudicator"
    assert any("llm.role 非法" in r.message for r in caplog.records)


def test_llm_detected_entity_roundtrip_report_reason(tmp_path, patch_detect_client):
    """AI 检出条目的 llm_reason 进 report，供人工复核审计。"""
    cfg = _cfg_with_llm(role="detector")
    p = _new_pipeline(cfg, tmp_path)
    client = FakeLLMDetectClient.instances[0]
    client.detect_responses = [
        [{"text": "新联置业发展集团", "type": "company",
          "confidence": 0.9, "reason": "上下文为投标单位"}],
    ]
    p.process_text("另悉新联置业发展集团亦参与本标段")
    report_file = tmp_path / "report.json"
    p.save_report(report_file)
    import json as _json
    data = _json.loads(report_file.read_text(encoding="utf-8"))
    llm_entries = [e for e in data["suggested"] if e.get("source") == "llm"]
    assert len(llm_entries) == 1
    assert llm_entries[0]["llm_reason"].startswith("AI 检出：")
    assert data["llm_stats"]["detected"] == 1
