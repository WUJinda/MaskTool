# -*- coding: utf-8 -*-
"""LLM 复核器（Adjudicator，P1 核心）：对规则引擎命中结果做二次裁定。

职责边界（见 plan/llm-integration-plan.md §3.2）：
- 只允许修改 confidence / text_type / llm_reason；禁止改 text 原文
- ``drop`` 不物理删除：把置信度压到全部模式的建议档之下（0.40），
  由 PolicyEngine 自然降级为 HINT_ONLY，用户在确认页仍可见可勾选
- LLM 上调置信度设安全上限（默认 +0.05），防止幻觉把弱证据顶成自动脱敏
- source="manual" 的项跳过复核（用户显式指定优先于 AI 判断，与
  Detector 手动词通道语义一致）
- 任何 LLM 错误 → 该批结果原样返回 + 统计错误数，绝不阻断脱敏流程

性能模型（重要）：detect() 按段落粒度调用，段内命中通常 0~5 条；
同一实体跨段重复出现时靠 (text, source, text_type) 缓存去重，实际
调用量 ≈ 去重实体数 / batch_size。50 页文档首次全量复核约 5~15 次调用。
"""

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from mask_tool.core.llm.exceptions import LLMError
from mask_tool.models.config import LLMConfig
from mask_tool.models.detection import DetectionResult, DetectionType

logger = logging.getLogger("mask_tool")

# drop 判定的置信度落点：低于 aggressive 的建议档（0.45），
# 全部模式下 PolicyEngine 均降级为 HINT_ONLY
_DROP_CONFIDENCE = 0.40

# LLM 上调置信度的安全上限（防幻觉顶成自动脱敏）
_RAISE_CAP = 0.05

# 合法类别枚举（与 DetectionType 对齐；文本形式便于 schema 约束）
_VALID_TYPES = {t.value for t in DetectionType}

# 复核结果结构化输出契约（纯 dict JSON Schema，不引 pydantic）
ADJUDICATE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "action": {"enum": ["keep", "drop", "adjust"]},
                    "confidence": {
                        "type": "number", "minimum": 0, "maximum": 1,
                    },
                    "type": {
                        "enum": ["company", "government", "person", "project",
                                 "subject", "location", "amount", "custom"],
                    },
                    "reason": {"type": "string"},
                },
                "required": ["id", "action"],
            },
        }
    },
    "required": ["items"],
}

_SYSTEM_PROMPT = (
    "你是文档脱敏系统的敏感信息复核引擎。系统已用词典/正则/NER 规则"
    "检出候选实体，你需要结合上下文判断每一项是否真的属于需要脱敏的"
    "敏感信息（公司/机构/政府/人名/地名/项目名/标的物/金额/自定义编号、"
    "证件号、联系方式等）。判定规则：\n"
    "- keep：确属敏感信息，维持原判\n"
    "- drop：误报（普通词汇、行业通用词、非敏感内容），建议剔除\n"
    "- adjust：确属敏感，但类别或置信度需修正\n"
    "confidence 为你对该判定的把握（0.0~1.0，仅 adjust 时生效）。"
    "type 为最贴切类别（仅 adjust 时生效）。reason 用一句话中文说明依据。"
    "只输出符合约定 schema 的 JSON，不要输出任何其他文字。"
)

# 来源标签（送审 prompt 中帮助模型理解证据来源）
_SOURCE_LABELS = {
    "dictionary": "词库匹配",
    "regex": "正则规则",
    "ner": "NER识别",
    "path": "文件/目录名",
}


@dataclass
class LLMRunStats:
    """单次运行的 LLM 调用统计（进 report.json 的 llm 段）。"""

    model: str = ""
    base_url: str = ""
    calls: int = 0              # 实际 HTTP 调用次数
    cache_hits: int = 0         # 缓存命中（未发起调用）
    items_seen: int = 0         # 送审候选总数（去重前）
    items_adjudicated: int = 0  # 获得有效判定的候选数（去重后）
    adjusted: int = 0           # 应用 adjust 的次数
    dropped: int = 0            # 应用 drop 的次数
    errors: int = 0             # 调用/解析失败批次数
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "base_url": self.base_url,
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "items_seen": self.items_seen,
            "items_adjudicated": self.items_adjudicated,
            "adjusted": self.adjusted,
            "dropped": self.dropped,
            "errors": self.errors,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


@dataclass
class _Verdict:
    """单条候选的有效判定（校验通过后）。"""

    action: str
    confidence: Optional[float] = None
    type_value: Optional[str] = None
    reason: str = ""


class LLMAdjudicator:
    """规则命中结果的 LLM 二次裁定器。

    生命周期与 Pipeline 一致（一次运行一个实例），缓存跨段落生效——
    同一实体在文档中多处出现只送审一次。
    """

    def __init__(
        self,
        client,
        config: LLMConfig,
        stats: Optional[LLMRunStats] = None,
    ):
        """
        Args:
            client: OpenAICompatClient（或测试注入的同等接口对象）
            config: LLMConfig（batch_size / budget_max_calls / cache 等）
            stats: 外置统计对象（Pipeline 持有以写入 report）
        """
        self._client = client
        self._config = config
        self._stats = stats or LLMRunStats(
            model=getattr(client, "model", ""),
            base_url=getattr(client, "base_url", ""),
        )
        # (text, source, text_type) -> _Verdict；同一次运行内复用
        self._cache: Dict[Tuple[str, str, str], _Verdict] = {}
        self._budget_warned = False
        self._error_warned = False

    @property
    def stats(self) -> LLMRunStats:
        return self._stats

    # ------------------------------------------------------------------

    def adjudicate(
        self, results: List[DetectionResult], file_path: str = "",
    ) -> List[DetectionResult]:
        """对一批检测结果应用 LLM 复核，返回同一列表（原地修改）。

        失败语义：预算耗尽 / LLM 错误的批次原样返回，仅计数与一次性警告；
        空列表与全 manual 批次零调用。
        """
        if not results:
            return results
        self._stats.items_seen += len(results)

        # 待送审：排除 manual；cache 启用时跳过已判定项，按缓存键去重
        pending: Dict[Tuple[str, str, str], DetectionResult] = {}
        for r in results:
            if r.source == "manual":
                continue
            key = (r.text, r.source, r.text_type.value)
            if self._config.cache and key in self._cache:
                self._stats.cache_hits += 1
                continue
            pending.setdefault(key, r)

        if pending:
            self._adjudicate_pending(pending)

        # 应用判定（缓存命中的直接套用）
        for r in results:
            if r.source == "manual":
                continue
            verdict = self._cache.get((r.text, r.source, r.text_type.value))
            if verdict is not None:
                self._apply(r, verdict)
        return results

    # ------------------------------------------------------------------

    def _adjudicate_pending(
        self, pending: Dict[Tuple[str, str, str], DetectionResult],
    ) -> None:
        """分批送审并把有效判定写入缓存；预算与错误在此降级。"""
        batch_size = max(1, self._config.batch_size)
        items = list(pending.items())
        for start in range(0, len(items), batch_size):
            batch = items[start:start + batch_size]
            if self._stats.calls >= self._config.budget_max_calls:
                self._warn_budget_once()
                return
            try:
                verdicts = self._ask_llm(batch)
            except LLMError as exc:
                self._stats.errors += 1
                if not self._error_warned:
                    self._error_warned = True
                    logger.warning(
                        "LLM 复核不可用，相关候选按规则结果处理（仅警告一次）: %s",
                        exc,
                    )
                continue
            for key, verdict in verdicts.items():
                self._cache[key] = verdict
                self._stats.items_adjudicated += 1

    def _ask_llm(
        self, batch: List[Tuple[Tuple[str, str, str], DetectionResult]],
    ) -> Dict[Tuple[str, str, str], _Verdict]:
        """构造 prompt → 调用 chat_json → 校验回包 → 有效判定映射。

        校验规则：id 必须在批内；action 枚举合法；adjust 的 confidence
        在 [0,1]、type 在合法枚举内——任何越界丢弃该项判定（该候选
        保持规则结果，不重试）。
        """
        indexed = list(enumerate(batch))
        candidates = [
            {
                "id": i,
                "text": r.text,
                "type": r.text_type.value,
                "source": _SOURCE_LABELS.get(r.source, r.source),
                "confidence": r.confidence,
                "context": r.context[:200],
            }
            for i, (_, r) in indexed
        ]
        user_prompt = (
            "候选实体列表（JSON）：\n"
            + _dumps_compact(candidates)
            + "\n请逐项复核并按约定输出。"
        )
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        t0 = time.monotonic()
        data = self._client.chat_json(messages, ADJUDICATE_SCHEMA)
        self._stats.calls += 1
        self._stats.elapsed_seconds += time.monotonic() - t0

        verdicts: Dict[Tuple[str, str, str], _Verdict] = {}
        raw_items = data.get("items")
        if not isinstance(raw_items, list):
            return verdicts
        by_id = {i: key for i, (key, _) in indexed}
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if not isinstance(item_id, int) or item_id not in by_id:
                continue
            action = item.get("action")
            if action not in ("keep", "drop", "adjust"):
                continue
            verdict = _Verdict(action=action, reason=str(item.get("reason", "")))
            if action == "adjust":
                conf = item.get("confidence")
                if not isinstance(conf, (int, float)):
                    continue
                conf = float(conf)
                if not (0.0 <= conf <= 1.0):
                    continue
                verdict.confidence = conf
                type_value = item.get("type")
                if type_value is not None and type_value in _VALID_TYPES:
                    verdict.type_value = type_value
            verdicts[by_id[item_id]] = verdict
        return verdicts

    def _apply(self, r: DetectionResult, verdict: _Verdict) -> None:
        """把有效判定应用到单条结果（只改 confidence/type/llm_reason）。"""
        if verdict.action == "drop":
            r.llm_reason = (
                f"AI 复核建议剔除（原置信度 {r.confidence:.2f}）：{verdict.reason}"
            )
            r.confidence = _DROP_CONFIDENCE
            self._stats.dropped += 1
        elif verdict.action == "adjust":
            if verdict.confidence is not None:
                # 上调设安全上限，下调不限制（部分 drop 的等价形式）
                capped = min(verdict.confidence, r.confidence + _RAISE_CAP)
                if abs(capped - r.confidence) > 1e-9:
                    r.llm_reason = (
                        f"AI 复核修正置信度 {r.confidence:.2f}→{capped:.2f}："
                        f"{verdict.reason}"
                    )
                    r.confidence = capped
            if verdict.type_value and verdict.type_value != r.text_type.value:
                r.llm_reason = (
                    f"{r.llm_reason} " if r.llm_reason else ""
                ) + f"AI 复核修正类别 {r.text_type.value}→{verdict.type_value}：{verdict.reason}"
                r.text_type = DetectionType(verdict.type_value)
            self._stats.adjusted += 1
        else:  # keep
            if verdict.reason:
                r.llm_reason = f"AI 复核维持：{verdict.reason}"
            # keep 不改 confidence/type

    def _warn_budget_once(self) -> None:
        if not self._budget_warned:
            self._budget_warned = True
            logger.warning(
                "LLM 调用预算已耗尽（%d 次），后续候选仅按规则结果处理",
                self._config.budget_max_calls,
            )


def _dumps_compact(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)
