# -*- coding: utf-8 -*-
"""OpenAI Chat Completions 兼容客户端（内网离线可用，requests 实现）。

设计决策（见 plan/llm-integration-plan.md §3.1）：
- 零新增依赖：requests 已随 streamlit 进入运行环境与 PyInstaller 产物，
  不引入 openai/httpx SDK，保证 frozen 打包体积与稳定性
- 接口面只有 4 个方法；端点结构化输出能力差异全部在 ``chat_json`` 的
  三级降级链内部消化，调用方无感知：
  ① response_format={"type":"json_schema", ...}
  ② response_format={"type":"json_object"}（schema 写进 system 提示）
  ③ 无 response_format（JSON 约定 + 容错解析兜底）
  能力按 (base_url, model) 在客户端实例内探测一次并缓存复用
- 失败语义：网络/超时/5xx 抛 LLMUnavailableError；输出非法抛
  LLMParseError——上层 catch LLMError 即可统一降级，绝不阻断脱敏
"""

import json
import logging
import os
import re
from typing import List, Optional, Tuple

import requests

from mask_tool.core.llm.exceptions import LLMError, LLMParseError, LLMUnavailableError

logger = logging.getLogger("mask_tool")

# 密钥环境变量（api_key 配置为空时读取）
API_KEY_ENV = "MASKTOOL_LLM_API_KEY"

# base_url 末尾版本段识别（…/v1、…/v2 等），无版本段时自动补 /v1
_VERSION_SUFFIX = re.compile(r"/v\d+$")

# 降级链档位（探测结果缓存值）
_CAP_JSON_SCHEMA = "json_schema"
_CAP_JSON_OBJECT = "json_object"
_CAP_PROMPT = "prompt"


class OpenAICompatClient:
    """OpenAI Chat Completions 标准客户端。

    兼容 Ollama（本地 api_key 任意值忽略）/ vLLM / Xinference / LMDeploy /
    One-API 类内网网关。非流式、temperature=0 调用为主（脱敏复核要求
    结果稳定可复现）。
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: int = 30,
        session: Optional[requests.Session] = None,
    ):
        """
        Args:
            base_url: 端点根地址，如 ``http://localhost:11434/v1``；
                缺少版本段时自动补 ``/v1``
            model: 模型名（如 ``qwen3:8b`` / 内网服务注册名）
            api_key: 鉴权密钥；空时读环境变量 ``MASKTOOL_LLM_API_KEY``，
                内网 Ollama 通常留空
            timeout: 单请求超时秒数
            session: 可注入的 requests.Session（测试用；默认新建）
        """
        self._base = self._normalize_base(base_url)
        self.model = model
        self._api_key = (api_key or os.environ.get(API_KEY_ENV, "")).strip()
        self._timeout = timeout
        self._session = session or requests.Session()
        # 结构化输出能力：None=未探测；探测后取最高可用档并缓存
        self._capability: Optional[str] = None

    # ------------------------------------------------------------------
    # 公开接口（仅 4 个方法）
    # ------------------------------------------------------------------

    def chat(
        self,
        messages: List[dict],
        *,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> str:
        """非流式补全，返回首个 choice 的 content 文本。

        Raises:
            LLMUnavailableError: 网络不通 / 超时 / HTTP 5xx / 预算类错误
        """
        return self._chat_content({
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        })

    def chat_json(self, messages: List[dict], schema: dict) -> dict:
        """结构化补全：返回从输出中提取并 json.loads 成功的 dict。

        三级降级链（能力探测 + 缓存，调用方无感知）：
            ① response_format json_schema（服务端约束生成）
            ② response_format json_object（schema 写进 system）
            ③ 无 response_format（JSON 约定提示 + 客户端容错解析）

        Args:
            messages: 消息列表（会被按档位追加/注入，原列表不修改）
            schema: JSON Schema（纯 dict，仅 ``{"type":"object", ...}`` 形态）

        Raises:
            LLMParseError: 三档均无法得到合法 JSON 对象
            LLMUnavailableError: 网络层失败
        """
        # 首次调用按序探测；已探测则直接用缓存档位
        ladder = [self._capability] if self._capability else [
            _CAP_JSON_SCHEMA, _CAP_JSON_OBJECT, _CAP_PROMPT,
        ]
        last_error: Optional[LLMError] = None
        for cap in ladder:
            payload_messages, response_format = self._build_request(
                messages, schema, cap,
            )
            payload = {
                "model": self.model,
                "messages": payload_messages,
                "temperature": 0.0,
                "stream": False,
            }
            if response_format is not None:
                payload["response_format"] = response_format
            try:
                content = self._chat_content(payload)
            except LLMUnavailableError as exc:
                # 网络层失败不降级（换档位也没意义），直接抛
                raise
            except LLMError as exc:
                last_error = exc
                continue
            try:
                obj = _extract_json_object(content)
            except LLMParseError as exc:
                last_error = exc
                logger.debug("LLM 输出解析失败（%s 档）: %s", cap, exc)
                continue
            # 请求与解析均成功：落定该档能力（后续复用，不再探测）
            self._capability = cap
            return obj

        raise last_error or LLMParseError("结构化补全失败：所有档位均不可用")

    def health_check(self) -> Tuple[bool, str]:
        """探活：GET /models 校验端点可达且模型存在。

        Returns:
            (可用, 说明文本)。不可用时说明文本给出原因（供 UI/日志展示）
        """
        try:
            resp = self._request("GET", "/models", None)
            data = resp.json()
        except LLMError as exc:
            return False, str(exc)
        except (ValueError, TypeError):
            return False, "端点响应非 JSON"
        models = [
            m.get("id", "") for m in data.get("data", [])
            if isinstance(m, dict)
        ]
        if self.model not in models:
            return False, f"端点可达但模型不存在: {self.model}"
        return True, f"端点可用（模型 {self.model}）"

    def list_models(self) -> List[str]:
        """列出端点模型 id 清单；失败抛 LLMUnavailableError。"""
        resp = self._request("GET", "/models", None)
        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMParseError(f"模型列表响应非 JSON: {exc}") from exc
        return [
            m.get("id", "") for m in data.get("data", [])
            if isinstance(m, dict)
        ]

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_base(base_url: str) -> str:
        base = (base_url or "").strip().rstrip("/")
        if not base:
            raise LLMError("base_url 为空：LLM 端点未配置")
        if not _VERSION_SUFFIX.search(base):
            base += "/v1"
        return base

    def _build_request(
        self, messages: List[dict], schema: dict, capability: str,
    ) -> Tuple[List[dict], Optional[dict]]:
        """按档位构造请求消息与 response_format（不修改传入 messages）。

        - json_schema 档：标准 response_format 携带 schema
        - json_object 档：schema 文本注入 system 消息
        - prompt 档：仅 JSON 约定提示 + schema 文本
        """
        msgs = [dict(m) for m in messages]
        schema_text = json.dumps(schema, ensure_ascii=False)
        if capability == _CAP_JSON_SCHEMA:
            return msgs, {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": schema},
            }
        if capability == _CAP_JSON_OBJECT:
            note = (
                "只输出一个符合以下 JSON Schema 的 JSON 对象"
                "（顶层必须是 {...} 对象，不要输出顶层数组 [...]），"
                f"不要输出任何其他文字：{schema_text}"
            )
            return _inject_system_note(msgs, note), {"type": "json_object"}
        note = (
            "只输出一个符合以下 JSON Schema 的 JSON 对象"
            "（顶层必须是 {...} 对象，不要输出顶层数组 [...]），"
            f"不要输出任何其他文字：{schema_text}"
        )
        return _inject_system_note(msgs, note), None

    def _chat_content(self, payload: dict) -> str:
        """单档尝试：请求 + 取 content。4xx 视为该档不支持（供降级）。

        Raises:
            LLMError: 该档不可用（4xx / 响应结构异常）
            LLMUnavailableError: 网络层失败（不降级）
        """
        resp = self._request("POST", "/chat/completions", payload)
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMParseError(f"补全响应结构异常: {exc}") from exc
        if not isinstance(content, str):
            raise LLMParseError("补全响应 content 非字符串")
        return content

    def _request(self, method: str, path: str, payload: Optional[dict]):
        """统一 HTTP 入口：网络异常与 5xx 归一化为 LLMUnavailableError；
        4xx 归一化为 LLMError（降级链据此换档）。成功时缓存该档能力。
        """
        url = f"{self._base}{path}"
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            resp = self._session.request(
                method, url,
                json=payload, headers=headers, timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise LLMUnavailableError(f"LLM 端点请求失败: {exc}") from exc
        if resp.status_code >= 500:
            raise LLMUnavailableError(
                f"LLM 端点服务错误: HTTP {resp.status_code}"
            )
        if resp.status_code >= 400:
            # 4xx：当前档位不被支持（含 json_schema/json_object 传参被拒）
            raise LLMError(
                f"LLM 端点拒绝请求: HTTP {resp.status_code}（档位可能不支持）"
            )
        return resp

    @property
    def capability(self) -> Optional[str]:
        """当前缓存的结构化输出档位（None=未探测）。"""
        return self._capability

    @property
    def base_url(self) -> str:
        """归一化后的端点根地址（含版本段）。"""
        return self._base


def _inject_system_note(messages: List[dict], note: str) -> List[dict]:
    """向消息列表注入（或合并进）system 约束，返回新列表。"""
    msgs = [dict(m) for m in messages]
    if msgs and msgs[0].get("role") == "system":
        msgs[0] = {
            "role": "system",
            "content": f"{msgs[0].get('content', '')}\n{note}",
        }
    else:
        msgs.insert(0, {"role": "system", "content": note})
    return msgs


def _extract_json_object(content: str) -> dict:
    """容错解析：剥 markdown 围栏 → 定位首个 JSON 对象 → 解码。

    Raises:
        LLMParseError: 无法提取合法 JSON 对象
    """
    if not content or not content.strip():
        raise LLMParseError("LLM 输出为空")
    text = content.strip()
    # 提取 ```json ... ``` / ``` ... ``` 围栏块（允许前后有噪声文字）
    m = re.search(r"```[a-zA-Z]*\s*(.*?)```", text, re.DOTALL)
    if m and m.group(1).strip():
        text = m.group(1).strip()
    # 直接解析
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, list):
            # 模型无视 schema 输出顶层数组（prompt 档常见漂移）：包装标记，
            # 由调用方按语义取用（复核=items / 检测=entities）
            return {"__root_array__": obj}
    except json.JSONDecodeError:
        pass
    # 定位首个平衡的 JSON 对象（raw_decode 从每个 '{' 起点尝试，
    # 跳过模型输出的前后噪声文字）
    decoder = json.JSONDecoder()
    # 先尝试整体解码（顶层可能直接是数组）
    stripped = text.lstrip()
    if stripped.startswith("["):
        try:
            obj, _ = decoder.raw_decode(stripped)
            if isinstance(obj, list):
                return {"__root_array__": obj}
        except json.JSONDecodeError:
            pass
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    raise LLMParseError("LLM 输出中未找到合法 JSON 对象")
