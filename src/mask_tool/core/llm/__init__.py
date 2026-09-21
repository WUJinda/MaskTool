"""LLM 增强检测（P1：Adjudicator 复核器接入）。

架构定位：LLM 只参与"检测与裁定"，不进入替换 / 映射 / 还原链路。
- client.py：OpenAI 兼容协议层（Ollama / vLLM / Xinference / 内网网关），
  三级降级链把端点结构化输出能力差异吸收在协议层内部
- adjudicator.py：语义层，对规则引擎命中结果做二次裁定（调置信度 /
  剔除误报 / 修正类别），失败降级原样返回，绝不阻断脱敏
- detector_wrapper.py：LLMEnhancedDetector 继承包装，pipeline 单点接线，
  全部 adapters / extract / inspect 调用点零改动

铁律：``llm.enabled: false`` 时全链路行为与不引入本包时逐字节一致。
"""

from mask_tool.core.llm.client import OpenAICompatClient
from mask_tool.core.llm.exceptions import (
    LLMError,
    LLMParseError,
    LLMUnavailableError,
)

__all__ = [
    "OpenAICompatClient",
    "LLMError",
    "LLMParseError",
    "LLMUnavailableError",
]
