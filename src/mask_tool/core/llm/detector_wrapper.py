# -*- coding: utf-8 -*-
"""LLM 增强检测包装器（P1 接线 / P2 role 编排）。

设计（见 plan/llm-integration-plan.md §3.3）：
- 继承 Detector 并 override detect()，按 role 编排：
    adjudicator：规则检测 → LLM 复核（P1 默认）
    detector   ：规则检测 → LLM 增量检测（source="llm"）
    both       ：规则检测 → 增量检测（合并）→ 复核（llm 项跳过复核）
- Pipeline 构造处单点替换；全部 adapters / extract / inspect / 路径
  脱敏调用 ``detector.detect`` 的地方零改动，自动获得增强
- 未知属性委托内部 base 实例（manual_words / whitelist 等外部访问兼容）
- LLM 任何异常均降级为纯规则结果，保证 detect 永不因 LLM 抛错
"""

import logging
from typing import List

from mask_tool.core.detector import Detector
from mask_tool.core.llm.adjudicator import LLMAdjudicator

logger = logging.getLogger("mask_tool")

# 合法 role 取值（Pipeline 接线处归一化后传入）
VALID_ROLES = ("adjudicator", "detector", "both")


class LLMEnhancedDetector(Detector):
    """Detector 装饰器：规则检测 + LLM 增强（复核 / 增量检测）。

    不调用父类构造（词典/正则/NER 构建成本已由 base 完成），
    仅持有 base 实例、复核器与角色。
    """

    def __init__(
        self,
        base: Detector,
        adjudicator: LLMAdjudicator,
        role: str = "adjudicator",
    ):
        self._base = base
        self._adjudicator = adjudicator
        self._role = role if role in VALID_ROLES else "adjudicator"

    @property
    def base_detector(self) -> Detector:
        """被包装的原生规则检测器。"""
        return self._base

    @property
    def adjudicator(self) -> LLMAdjudicator:
        return self._adjudicator

    @property
    def role(self) -> str:
        return self._role

    def detect(self, text: str, file_path: str = "") -> List:
        """规则检测 + 按 role 的 LLM 增强；任何 LLM 异常均降级纯规则结果。"""
        results = self._base.detect(text, file_path)

        # P2：增量检测（detector / both）——规则已检出的实体作为排除集
        if self._role in ("detector", "both") and results is not None:
            try:
                exclude = {r.text for r in results}
                new = self._adjudicator.detect_new(
                    text, file_path, exclude_texts=exclude,
                )
                if new:
                    results = list(results) + new
            except Exception as exc:  # 防御层：detect_new 内部已捕获
                logger.warning("LLM 增量检测异常，本段按规则结果处理: %s", exc)

        # P1：复核（adjudicator / both）——llm/manual 项在内部跳过
        if self._role in ("adjudicator", "both") and results:
            try:
                results = self._adjudicator.adjudicate(results, file_path)
            except Exception as exc:  # 防御层：adjudicate 内部已捕获
                logger.warning("LLM 复核异常，本段按规则结果处理: %s", exc)

        return results

    def __getattr__(self, name):
        """未定义属性委托 base（whitelist / manual_words 等外部访问）。"""
        # 仅在本实例确实缺少该属性时触发；_base/_adjudicator 未就绪时
        # （pickle/copy 等特殊路径）抛 AttributeError 而非无限递归
        if name.startswith("_") and name not in ("_base", "_adjudicator"):
            raise AttributeError(name)
        base = self.__dict__.get("_base")
        if base is None:
            raise AttributeError(name)
        return getattr(base, name)
