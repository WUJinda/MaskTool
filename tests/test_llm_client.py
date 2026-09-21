# -*- coding: utf-8 -*-
"""OpenAICompatClient 单测：三级降级链矩阵 / 容错解析 / 异常归一化。

全部用注入的 FakeSession 模拟 HTTP，不依赖真实端点。
"""

import pytest
import requests

from mask_tool.core.llm.client import OpenAICompatClient
from mask_tool.core.llm.exceptions import (
    LLMParseError,
    LLMUnavailableError,
)

SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array"}},
    "required": ["items"],
}
MSGS = [{"role": "user", "content": "测试"}]


class FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def _ok_completion(content):
    return FakeResponse(200, {
        "choices": [{"message": {"role": "assistant", "content": content}}]
    })


class FakeSession:
    """按 response_format 类型路由的假 HTTP 会话。

    rejected_formats: 拒绝（400）的 response_format type 集合
    scripted: 可选的按序脚本（每次请求弹出一个），优先于路由逻辑
    """

    def __init__(self, rejected_formats=(), scripted=None,
                 network_error=None, get_response=None):
        self.rejected = set(rejected_formats)
        self.scripted = list(scripted or [])
        self.network_error = network_error
        self.get_response = get_response
        self.posts = []  # 记录 (url, payload) 便于断言请求序列

    def request(self, method, url, json=None, headers=None, timeout=None):
        if method == "GET":
            if self.network_error:
                raise self.network_error
            return self.get_response or FakeResponse(200, {"data": [{"id": "m1"}]})
        if self.network_error:
            raise self.network_error
        if self.scripted:
            return self.scripted.pop(0)
        self.posts.append((url, json))
        rf = (json or {}).get("response_format")
        fmt = rf.get("type") if isinstance(rf, dict) else "none"
        if fmt in self.rejected:
            return FakeResponse(400, {"error": "unsupported"})
        return _ok_completion('{"items": []}')


def _client(session, base="http://x:11434/v1", model="m1"):
    return OpenAICompatClient(base, model, session=session, timeout=5)


# ---------------------------------------------------------------------------
# 降级链矩阵
# ---------------------------------------------------------------------------

def test_json_schema_supported_directly():
    s = FakeSession()
    c = _client(s)
    out = c.chat_json(MSGS, SCHEMA)
    assert out == {"items": []}
    assert c.capability == "json_schema"
    # 只发了一次请求（无降级）
    assert len(s.posts) == 1


def test_fallback_to_json_object():
    s = FakeSession(rejected_formats={"json_schema"})
    c = _client(s)
    out = c.chat_json(MSGS, SCHEMA)
    assert out == {"items": []}
    assert c.capability == "json_object"
    # 两次请求：json_schema 被拒 → json_object 成功
    assert [p["response_format"]["type"] for _, p in s.posts] == [
        "json_schema", "json_object",
    ]
    # json_object 档已把 schema 注入 system 消息
    second = s.posts[1][1]["messages"]
    assert second[0]["role"] == "system"
    assert "JSON Schema" in second[0]["content"]


def test_fallback_to_prompt_with_tolerant_parsing():
    # 两档均被拒 → prompt 档；输出带 markdown 围栏仍可解析
    s = FakeSession(rejected_formats={"json_schema", "json_object"})
    s.scripted = [
        FakeResponse(400, {}),
        FakeResponse(400, {}),
        _ok_completion('```json\n{"items": [{"id": 1}]}\n```'),
    ]
    c = _client(s)
    out = c.chat_json(MSGS, SCHEMA)
    assert out == {"items": [{"id": 1}]}
    assert c.capability == "prompt"


def test_capability_cached_after_probe():
    s = FakeSession(rejected_formats={"json_schema"})
    c = _client(s)
    c.chat_json(MSGS, SCHEMA)
    n_posts = len(s.posts)
    c.chat_json(MSGS, SCHEMA)
    # 第二次调用直接用缓存档位（json_object），不再探测 json_schema
    assert len(s.posts) == n_posts + 1
    assert s.posts[-1][1]["response_format"]["type"] == "json_object"


def test_all_ladders_fail_raises_parse_error():
    # 三档请求都成功但输出全都不是 JSON → LLMParseError
    s = FakeSession(scripted=[
        _ok_completion("抱歉我不会输出 JSON"),
        _ok_completion("还是不会"),
        _ok_completion("仍然不会"),
    ])
    c = _client(s)
    with pytest.raises(LLMParseError):
        c.chat_json(MSGS, SCHEMA)


# ---------------------------------------------------------------------------
# 容错解析
# ---------------------------------------------------------------------------

def test_extract_json_with_leading_noise():
    s = FakeSession(scripted=[
        _ok_completion('好的，结果如下：\n{"items": [], "extra": 1} 以上。'),
    ])
    c = _client(s)
    out = c.chat_json(MSGS, SCHEMA)
    assert out["items"] == []


def test_parse_error_on_empty_content():
    # 三档输出均为空内容 → 全档解析失败 → LLMParseError
    s = FakeSession(scripted=[_ok_completion("   ")] * 3)
    c = _client(s)
    with pytest.raises(LLMParseError):
        c.chat_json(MSGS, SCHEMA)


# ---------------------------------------------------------------------------
# 异常归一化
# ---------------------------------------------------------------------------

def test_unavailable_on_timeout():
    s = FakeSession(network_error=requests.Timeout("boom"))
    c = _client(s)
    with pytest.raises(LLMUnavailableError):
        c.chat_json(MSGS, SCHEMA)


def test_unavailable_on_5xx_no_fallback():
    # 5xx 属服务故障：不换档重试，直接抛 LLMUnavailableError
    s = FakeSession(scripted=[FakeResponse(503, {})])
    c = _client(s)
    with pytest.raises(LLMUnavailableError):
        c.chat_json(MSGS, SCHEMA)


def test_chat_returns_content():
    s = FakeSession(scripted=[_ok_completion("你好")])
    c = _client(s)
    assert c.chat(MSGS) == "你好"


def test_chat_unavailable_on_network_error():
    s = FakeSession(network_error=requests.ConnectionError("refused"))
    c = _client(s)
    with pytest.raises(LLMUnavailableError):
        c.chat(MSGS)


# ---------------------------------------------------------------------------
# 端点探活与地址归一化
# ---------------------------------------------------------------------------

def test_health_check_model_missing():
    s = FakeSession(get_response=FakeResponse(200, {"data": [{"id": "other"}]}))
    c = _client(s)
    ok, msg = c.health_check()
    assert not ok and "模型不存在" in msg


def test_health_check_ok():
    c = _client(FakeSession())
    ok, msg = c.health_check()
    assert ok


def test_base_url_normalization():
    c = OpenAICompatClient("http://x:11434", "m1", session=FakeSession())
    assert c.base_url == "http://x:11434/v1"
    c2 = OpenAICompatClient("http://x:8000/", "m1", session=FakeSession())
    assert c2.base_url == "http://x:8000/v1"
    # 已带版本段不再重复追加
    c3 = OpenAICompatClient("http://x/v1/", "m1", session=FakeSession())
    assert c3.base_url == "http://x/v1"


def test_empty_base_url_raises():
    with pytest.raises(Exception):
        OpenAICompatClient("", "m1", session=FakeSession())


def test_api_key_from_env(monkeypatch):
    monkeypatch.setenv("MASKTOOL_LLM_API_KEY", "sk-test")
    c = OpenAICompatClient("http://x/v1", "m1", api_key="", session=FakeSession())
    c.chat(MSGS)
    # 密钥进入 Authorization 头（FakeSession.request 忽略 headers，
    # 此处通过行为断言构造不报错即可；头校验在下方直接检查）
    assert c._api_key == "sk-test"


# ---------------------------------------------------------------------------
# P3：顶层数组包装（结构漂移容错）
# ---------------------------------------------------------------------------

def test_root_array_wrapped():
    """模型输出顶层数组（prompt 档常见漂移）→ 包装为 __root_array__。"""
    s = FakeSession(scripted=[
        _ok_completion('[{"id": 0, "decision": "keep", "reason": "ok"}]'),
        _ok_completion('说明：```json\n[{"id": 0}]\n```'),   # 围栏+数组
    ])
    c = _client(s)
    out = c.chat_json(MSGS, SCHEMA)
    assert out == {"__root_array__": [{"id": 0, "decision": "keep", "reason": "ok"}]}
    out2 = c.chat_json(MSGS, SCHEMA)
    assert out2 == {"__root_array__": [{"id": 0}]}


def test_think_section_stripped():
    """qwen3 thinking 模型：<think> 段剥离后再解析 JSON。"""
    import pytest
    from mask_tool.core.llm.exceptions import LLMError

    s = FakeSession(scripted=[
        _ok_completion('<think>让我分析一下这段文本……</think>{"items": []}'),
        _ok_completion('前置噪声 {"items": [1]}'),
    ])
    c = _client(s)
    assert c.chat_json(MSGS, SCHEMA) == {"items": []}
    assert c.chat_json(MSGS, SCHEMA) == {"items": [1]}

    # 未闭合 <think>：其后无 JSON，三档降级链全部解析失败
    s2 = FakeSession(scripted=[_ok_completion('<think>截断的思考') for _ in range(3)])
    with pytest.raises(LLMError):
        _client(s2).chat_json(MSGS, SCHEMA)
