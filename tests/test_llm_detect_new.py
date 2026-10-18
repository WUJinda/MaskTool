# -*- coding: utf-8 -*-
"""P2 增量检测（detect_new，source="llm"）单测。"""

from mask_tool.core.llm.adjudicator import (
    DETECT_SCHEMA,
    LLMAdjudicator,
    _DETECT_CAP,
    _split_chunks,
)
from mask_tool.core.llm.exceptions import LLMUnavailableError
from mask_tool.models.config import LLMConfig
from mask_tool.models.detection import DetectionType


class FakeClient:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def chat_json(self, messages, schema):
        if self.error:
            raise self.error
        self.calls.append((messages, schema))
        if self.responses:
            return self.responses.pop(0)
        return {"entities": []}


def _cfg(**kw):
    base = dict(enabled=True, base_url="http://x/v1", model="m",
                batch_size=20, budget_max_calls=200, cache=True)
    base.update(kw)
    return LLMConfig(**base)


TEXT = "王总在会上介绍了远景智能制造产业园项目，联系人电话见附件。"


def test_detect_new_basic_result_shape():
    client = FakeClient([{"entities": [
        {"text": "王总", "type": "person", "confidence": 0.9,
         "reason": "会议发言人称呼，指代具体人名"},
        {"text": "远景智能制造产业园", "type": "project", "confidence": 0.7,
         "reason": "项目全称"},
    ]}])
    adj = LLMAdjudicator(client, _cfg())
    out = adj.detect_new(TEXT, "a.docx")
    assert len(out) == 2
    r = out[0]
    assert r.source == "llm"
    assert r.text_type == DetectionType.PERSON
    assert r.confidence == _DETECT_CAP          # 封顶 0.80
    assert r.llm_reason.startswith("AI 检出：")
    assert r.location.file == "a.docx"
    assert "王总" in r.context
    assert adj.stats.detected == 2


def test_detect_new_hallucination_guard():
    """LLM 返回原文中不存在的 text（改写/概括）→ 丢弃（masker 精确替换前提）。"""
    client = FakeClient([{"entities": [
        {"text": "王总经理", "type": "person", "confidence": 0.9},  # 原文无此串
        {"text": "王总", "type": "person", "confidence": 0.9},
    ]}])
    out = LLMAdjudicator(client, _cfg()).detect_new(TEXT)
    assert [r.text for r in out] == ["王总"]


def test_detect_new_excludes_known_entities():
    client = FakeClient([{"entities": [
        {"text": "王总", "type": "person"},          # 已在排除集
        {"text": "远景智能制造产业园", "type": "project"},
    ]}])
    out = LLMAdjudicator(client, _cfg()).detect_new(
        TEXT, exclude_texts={"王总"})
    assert [r.text for r in out] == ["远景智能制造产业园"]
    # 排除集随 prompt 下发
    messages, _ = client.calls[0]
    assert "王总" in messages[1]["content"]


def test_detect_new_invalid_items_discarded():
    client = FakeClient([{"entities": [
        {"text": "", "type": "person"},                      # 空文本
        {"text": "王总", "type": "not_a_type"},              # 类别非法
        {"text": "王总", "type": "person", "confidence": 3},  # 越界 clamp 封顶
        {"text": 123, "type": "person"},                     # 非字符串
    ]}])
    out = LLMAdjudicator(client, _cfg()).detect_new(TEXT)
    assert len(out) == 1 and out[0].confidence <= _DETECT_CAP


def test_detect_new_dedup_within_response():
    client = FakeClient([{"entities": [
        {"text": "王总", "type": "person"},
        {"text": "王总", "type": "person"},   # 同块重复输出
    ]}])
    out = LLMAdjudicator(client, _cfg()).detect_new(TEXT)
    assert len(out) == 1


def test_detect_new_long_text_chunking():
    """超长文本按句子边界分块多次送审；不在块内的实体被幻觉防护丢弃。"""
    long_text = ("这是一段很长的介绍文本。" * 300) + "联系人张三丰。"
    # 每块都回张三丰：前两块文本中不存在 → 幻觉防护丢弃；末块真实检出
    client = FakeClient([{"entities": [
        {"text": "张三丰", "type": "person"},
    ]}] * 5)
    adj = LLMAdjudicator(client, _cfg())
    out = adj.detect_new(long_text, "a.docx")
    assert len(client.calls) >= 2            # 分块多次调用
    assert [r.text for r in out] == ["张三丰"]  # 仅末块命中，无重复


def test_detect_new_paragraph_cache():
    client = FakeClient([{"entities": [
        {"text": "王总", "type": "person"},
    ]}])
    adj = LLMAdjudicator(client, _cfg())
    first = adj.detect_new(TEXT, "a.docx")
    second = adj.detect_new(TEXT, "b.docx")  # 同文本不同文件
    assert len(client.calls) == 1            # 缓存命中零调用
    assert adj.stats.cache_hits == 1
    assert first[0].location.file == "a.docx"
    assert second[0].location.file == "b.docx"  # Location 按当前文件重建


def test_detect_new_error_degrades_to_empty():
    client = FakeClient(error=LLMUnavailableError("端点不通"))
    adj = LLMAdjudicator(client, _cfg())
    assert adj.detect_new(TEXT) == []        # 空列表，不抛出
    assert adj.stats.errors == 1


def test_detect_new_budget_exhaustion():
    client = FakeClient([{"entities": [
        {"text": "王总", "type": "person"},
    ]}] * 5)
    adj = LLMAdjudicator(client, _cfg(budget_max_calls=1, cache=False))
    text1 = TEXT
    text2 = "另一段包含李四丰的文本。"
    adj.detect_new(text1)                    # 消耗预算
    out = adj.detect_new(text2)              # 预算耗尽 → 空
    assert out == [] and adj.stats.calls == 1


def test_detect_new_empty_text_no_call():
    client = FakeClient()
    LLMAdjudicator(client, _cfg()).detect_new("   ")
    assert client.calls == []


def test_split_chunks_boundaries():
    assert _split_chunks("短文本", 1500) == ["短文本"]
    text = "甲公司中标。乙公司落标。" * 100
    chunks = _split_chunks(text, 50)
    assert all(len(c) <= 50 for c in chunks)
    assert "".join(chunks) == text          # 无损拼接


def test_prompt_uses_detect_schema():
    client = FakeClient([{"entities": []}])
    LLMAdjudicator(client, _cfg()).detect_new(TEXT)
    messages, schema = client.calls[0]
    assert schema == DETECT_SCHEMA
    assert messages[0]["role"] == "system"
    assert "逐字出现" in messages[0]["content"]


def test_detect_new_chinese_type_aliases():
    """端点假支持 json_schema 时模型输出中文类别标签 → 归一化后接受。"""
    client = FakeClient([{"entities": [
        {"text": "王总", "type": "人名", "confidence": 0.9},
        {"text": "远景产业园", "type": "项目名"},
        {"text": "神秘词", "type": "无法识别的类别"},   # 未知名 → 丢弃
    ]}])
    out = LLMAdjudicator(client, _cfg()).detect_new(
        "王总负责远景产业园项目，这个词是神秘词。")
    types = {r.text: r.text_type for r in out}
    assert types["王总"] == DetectionType.PERSON
    assert types["远景产业园"] == DetectionType.PROJECT
    assert "神秘词" not in types

# ---------------------------------------------------------------------------
# T2.3 采纳率统计
# ---------------------------------------------------------------------------

def test_summarize_ai_acceptance():
    from mask_tool.core.llm.adjudicator import summarize_ai_acceptance
    from mask_tool.models.detection import (
        DetectionResult, DetectionType, Location,
    )

    def mk(text, source="dictionary", reason=""):
        return DetectionResult(
            text=text, text_type=DetectionType.PERSON, source=source,
            confidence=0.7, location=Location(file="a"), context="",
            llm_reason=reason,
        )

    results = [
        mk("王总", source="llm"),                                   # 0 AI 检出
        mk("张工", source="llm"),                                   # 1 AI 检出
        mk("甲方", reason="AI 复核建议剔除（原置信度 0.80）：通用词"),  # 2 AI 剔除
        mk("某某公司"),                                             # 3 普通
    ]
    out = summarize_ai_acceptance(results, [0, 2])
    assert out == {"ai_detected": 2, "ai_detected_accepted": 1,
                   "ai_dropped": 1, "ai_dropped_overridden": 1}
    assert summarize_ai_acceptance([], []) == {
        "ai_detected": 0, "ai_detected_accepted": 0,
        "ai_dropped": 0, "ai_dropped_overridden": 0,
    }

# ---------------------------------------------------------------------------
# 前缀常量与真实 _apply 产物的闭环（防 llm_reason 前缀漂移静默失效）
# ---------------------------------------------------------------------------

def test_apply_drop_prefix_roundtrip():
    """真实 _apply 的 drop 产物必须被 summarize/is_ai_detected 正确识别。"""
    from mask_tool.core.llm.adjudicator import (
        LLMAdjudicator, _Verdict, DROP_REASON_PREFIX, is_ai_detected,
        summarize_ai_acceptance,
    )
    from mask_tool.models.detection import (
        DetectionResult, DetectionType, Location,
    )

    r = DetectionResult(text="某某公司", text_type=DetectionType.COMPANY,
                        source="dictionary", confidence=0.95,
                        location=Location(file="a"))

    class _Stats:
        dropped = 0
        adjusted = 0

    adj = object.__new__(LLMAdjudicator)
    adj._stats = _Stats()
    LLMAdjudicator._apply(adj, r, _Verdict(action="drop", reason="通用词"))

    assert r.llm_reason.startswith(DROP_REASON_PREFIX)
    assert not is_ai_detected(r)
    assert summarize_ai_acceptance([r], []) == {
        "ai_detected": 0, "ai_detected_accepted": 0,
        "ai_dropped": 1, "ai_dropped_overridden": 0,
    }
    assert summarize_ai_acceptance([r], [0]) == {
        "ai_detected": 0, "ai_detected_accepted": 0,
        "ai_dropped": 1, "ai_dropped_overridden": 1,
    }


def test_is_ai_detected_path_rewritten():
    """文件名/路径检测把 source 改写为 path 后，AI 检出仍应被识别。"""
    from mask_tool.core.llm.adjudicator import (
        DETECT_REASON_PREFIX, is_ai_detected,
    )
    from mask_tool.models.detection import (
        DetectionResult, DetectionType, Location,
    )

    # 真实场景：PathMasker 保留 origin_source（WIP 字段）
    r = DetectionResult(
        text="李四", text_type=DetectionType.PERSON, source="path",
        origin_source="llm", confidence=0.7,
        location=Location(file="李四-合同.docx"),
        llm_reason=f"{DETECT_REASON_PREFIX}：非正式指代",
    )
    assert is_ai_detected(r)
    # 不依赖 origin_source：仅 llm_reason 前缀也足以识别
    r2 = DetectionResult(
        text="李四", text_type=DetectionType.PERSON, source="path",
        confidence=0.7, location=Location(file="x.docx"),
        llm_reason=DETECT_REASON_PREFIX,
    )
    assert is_ai_detected(r2)
