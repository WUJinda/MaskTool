"""LLM 接入异常体系：上层只 catch 一个基类即可完成降级。"""


class LLMError(Exception):
    """LLM 接入错误基类（含预算耗尽等可降级情形）"""


class LLMUnavailableError(LLMError):
    """端点不可用：网络不通 / 超时 / 5xx / 预算耗尽"""


class LLMParseError(LLMError):
    """输出解析失败：非 JSON / 无法提取 JSON 对象"""
