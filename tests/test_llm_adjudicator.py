# -*- coding: utf-8 -*-
"""LLMAdjudicator 单测：判定应用规则 / 缓存 / 预算 / 降级语义。"""

from mask_tool.core.llm.adjudicator import (
    ADJUDICATE_SCHEMA,
    LLMAdjudicator,
)
from mask_tool.core.llm.exceptions import LLMUnavailableError
from mask_tool.models.config import LLMConfig
from mask_tool.models.detection import (
    DetectionResult,
    DetectionType,
    Location,
)


class FakeClient:
    """记录调用并返回预设 chat_json 结果的假客户端。"""

    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []  # 每次 (messages, schema)

    def chat_json(self, messages, schema):
        if self.error:
            raise self.error
        self.calls.append((messages, schema))
        if self.responses:
            return self.responses.pop(0)
        return {"items": []}


def _result(text, conf, source="dictionary", dtype=DetectionType.COMPANY):
    return DetectionResult(
        text=text,
        text_type=dtype,
        source=source,
        confidence=conf,
        location=Location(file="a.docx"),
        context=f"...{text}...",
    )


def _cfg(**kw):
    base = dict(enabled=True, base_url="http://x/v1", model="m",
                batch_size=20, budget_max_calls=200, cache=True)
    base.update(kw)
    return LLMConfig(**base)


# ---------------------------------------------------------------------------
# 判定应用
# ---------------------------------------------------------------------------

def test_keep_no_change():
    r = _result("某公司", 0.95)
    client = FakeClient([{"items": [
        {"id": 0, "action": "keep", "reason": "确属公司名"},
    ]}])
    LLMAdjudicator(client, _cfg()).adjudicate([r])
    assert r.confidence == 0.95
    assert r.llm_reason == "AI 复核维持：确属公司名"


def test_drop_lowers_confidence_below_all_suggest_thresholds():
    r = _result("项目", 0.95)
    client = FakeClient([{"items": [
        {"id": 0, "action": "drop", "reason": "行业通用词，非敏感"},
    ]}])
    LLMAdjudicator(client, _cfg()).adjudicate([r])
    assert r.confidence == 0.40  # 低于 aggressive 建议档 0.45
    assert "剔除" in r.llm_reason and "0.95" in r.llm_reason


def test_adjust_raise_capped():
    r = _result("某公司", 0.80)
    client = FakeClient([{"items": [
        {"id": 0, "action": "adjust", "confidence": 0.99,
         "reason": "上下文明确"},
    ]}])
    LLMAdjudicator(client, _cfg()).adjudicate([r])
    assert abs(r.confidence - 0.85) < 1e-9  # 0.80 + 0.05 上限


def test_adjust_lower_not_capped():
    r = _result("某机构", 0.85)
    client = FakeClient([{"items": [
        {"id": 0, "action": "adjust", "confidence": 0.30,
         "reason": "证据不足"},
    ]}])
    LLMAdjudicator(client, _cfg()).adjudicate([r])
    assert abs(r.confidence - 0.30) < 1e-9


def test_adjust_type_correction():
    r = _result("13800138000", 0.85, source="regex", dtype=DetectionType.CUSTOM)
    client = FakeClient([{"items": [
        {"id": 0, "action": "adjust", "confidence": 0.88,
         "type": "person", "reason": "实为联系人手机号关联人名"},
    ]}])
    LLMAdjudicator(client, _cfg()).adjudicate([r])
    assert r.text_type == DetectionType.PERSON


def test_invalid_verdict_items_discarded():
    """id 越界 / action 非法 / confidence 越界的判定逐项丢弃，不崩。"""
    r1 = _result("A公司", 0.95)
    r2 = _result("B项目", 0.80)
    client = FakeClient([{"items": [
        {"id": 99, "action": "drop"},                 # id 不在批内
        {"id": 0, "action": "explode"},               # action 非法
        {"id": 1, "action": "adjust", "confidence": 5},  # confidence 越界
    ]}])
    adj = LLMAdjudicator(client, _cfg())
    adj.adjudicate([r1, r2])
    assert r1.confidence == 0.95 and r2.confidence == 0.80  # 原样
    assert adj.stats.items_adjudicated == 0


# ---------------------------------------------------------------------------
# manual 跳过 / 空批零调用
# ---------------------------------------------------------------------------

def test_manual_source_skipped():
    r = _result("指定词", 0.95, source="manual")
    client = FakeClient()
    LLMAdjudicator(client, _cfg()).adjudicate([r])
    assert client.calls == []            # 不送审
    assert r.llm_reason == ""            # 不套用判定


def test_empty_results_no_call():
    client = FakeClient()
    LLMAdjudicator(client, _cfg()).adjudicate([])
    assert client.calls == []


# ---------------------------------------------------------------------------
# 缓存与预算
# ---------------------------------------------------------------------------

def test_cache_hit_skips_second_call():
    client = FakeClient([{"items": [
        {"id": 0, "action": "keep", "reason": "ok"},
    ]}])
    adj = LLMAdjudicator(client, _cfg())
    r1 = _result("某公司", 0.95)
    r2 = _result("某公司", 0.95)   # 跨段重复实体
    adj.adjudicate([r1])
    adj.adjudicate([r2])
    assert len(client.calls) == 1
    assert adj.stats.cache_hits == 1
    assert r2.llm_reason == "AI 复核维持：ok"


def test_cache_disabled_retries():
    client = FakeClient([
        {"items": [{"id": 0, "action": "keep"}]},
        {"items": [{"id": 0, "action": "keep"}]},
    ])
    adj = LLMAdjudicator(client, _cfg(cache=False))
    adj.adjudicate([_result("某公司", 0.95)])
    adj.adjudicate([_result("某公司", 0.95)])
    assert len(client.calls) == 2


def test_budget_exhausted_passthrough():
    client = FakeClient([{"items": [
        {"id": 0, "action": "drop", "reason": "误报"},
    ]}])
    adj = LLMAdjudicator(client, _cfg(budget_max_calls=1, batch_size=1))
    r1 = _result("A公司", 0.95)
    r2 = _result("B公司", 0.95)
    adj.adjudicate([r1])   # 消耗预算 1
    adj.adjudicate([r2])   # 预算耗尽 → 原样
    assert r1.confidence == 0.40
    assert r2.confidence == 0.95
    assert adj.stats.calls == 1


def test_batching_splits_large_pending():
    """候选超过 batch_size 时分多次请求。"""
    results = [_result(f"公司{i}", 0.95) for i in range(5)]
    client = FakeClient([{"items": []}] * 3)
    adj = LLMAdjudicator(client, _cfg(batch_size=2))
    adj.adjudicate(results)
    assert len(client.calls) == 3   # ceil(5/2)


# ---------------------------------------------------------------------------
# 失败降级
# ---------------------------------------------------------------------------

def test_llm_error_passthrough_with_stats():
    client = FakeClient(error=LLMUnavailableError("端点不通"))
    adj = LLMAdjudicator(client, _cfg())
    r = _result("某公司", 0.95)
    adj.adjudicate([r])
    assert r.confidence == 0.95 and r.llm_reason == ""  # 原样
    assert adj.stats.errors == 1


# ---------------------------------------------------------------------------
# prompt 与 schema
# ---------------------------------------------------------------------------

def test_prompt_carries_schema_and_context():
    client = FakeClient([{"items": []}])
    adj = LLMAdjudicator(client, _cfg())
    adj.adjudicate([_result("某公司", 0.95)])
    messages, schema = client.calls[0]
    assert schema == ADJUDICATE_SCHEMA
    assert messages[0]["role"] == "system"
    assert "候选实体列表" in messages[1]["content"]
    assert "某公司" in messages[1]["content"]


def test_consecutive_error_circuit_breaker():
    """连续 3 次调用错误后熔断：后续批次/段落不再发起调用。"""
    client = FakeClient(error=LLMUnavailableError("端点挂起"))
    adj = LLMAdjudicator(client, _cfg(batch_size=1))
    for _ in range(4):
        r = _result("不同公司%d" % _, 0.95)
        adj.adjudicate([r])
    # 4 个独立批次，但第 3 连错后熔断 → 实际只发起 3 次调用（calls 统计
    # 的是成功调用，故为 0；errors 计 3 而非 4）
    assert adj.stats.errors == 3
    assert adj._tripped is True


def test_success_resets_error_counter():
    client = FakeClient()
    adj = LLMAdjudicator(client, _cfg())
    # 错 2 次 → 成功 1 次 → 再错 2 次：不熔断（计数被成功归零）
    client.error = LLMUnavailableError("闪断")
    adj.adjudicate([_result("A公司", 0.95)])          # 错 1
    adj.adjudicate([_result("B公司", 0.95)])          # 错 2
    client.error = None
    client.responses = [{"items": [{"id": 0, "action": "keep"}]}]
    adj.adjudicate([_result("C公司", 0.95)])          # 成功，归零
    client.error = LLMUnavailableError("又断")
    adj.adjudicate([_result("D公司", 0.95)])          # 错 1
    adj.adjudicate([_result("E公司", 0.95)])          # 错 2
    assert adj._tripped is False
    assert adj.stats.errors == 4
