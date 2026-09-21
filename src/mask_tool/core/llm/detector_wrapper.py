# -*- coding: utf-8 -*-
"""LLM 增强检测包装器（P1 接线核心）。

设计（见 plan/llm-integration-plan.md §3.3）：
- 继承 Detector 并 override detect()：先规则检测、后 LLM 复核
- Pipeline 构造处单点替换；全部 adapters / extract / inspect / 路径
  脱敏调用 ``detector.detect`` 的地方零改动，自动获得增强
- 未知属性委托内部 base 实例（manual_words / whitelist 等外部访问兼容）
- adjudicate 自身已捕获全部 LLM 错误，此处再拦一层防御（保证 detect
  在任何情况下都不因 LLM 抛错）
"""

import logging
from typing import List

from mask_tool.core.detector import Detector
from mask_tool.core.llm.adjudicator import LLMAdjudicator

logger = logging.getLogger("mask_tool")


class LLMEnhancedDetector(Detector):
    """Detector 装饰器：规则检测 → LLM 复核。

    不调用父类构造（词典/正则/NER 构建成本已由 base 完成），
    仅持有 base 实例与复核器。
    """

    def __init__(self, base: Detector, adjudicator: LLMAdjudicator):
        self._base = base
        self._adjudicator = adjudicator

    @property
    def base_detector(self) -> Detector:
        """被包装的原生规则检测器。"""
        return self._base

    @property
    def adjudicator(self) -> LLMAdjudicator:
        return self._adjudicator

    def detect(self, text: str, file_path: str = "") -> List:
        """规则检测 + LLM 复核；LLM 任何异常均降级为纯规则结果。"""
        results = self._base.detect(text, file_path)
        if not results:
            return results
        try:
            return self._adjudicator.adjudicate(results, file_path)
        except Exception as exc:  # 防御层：adjudicator 内部已捕获，双保险
            logger.warning("LLM 复核异常，本段按规则结果处理: %s", exc)
            return results

    def __getattr__(self, name):
        """未定义属性委托 base（whitelist / manual_words 等外部访问）。"""
        # 注意：仅在本实例确实缺少该属性时触发；_base 已在 __init__ 赋值
        return getattr(self._base, name)
