"""脱敏执行器 - 薄委托 ReplacementEngine（检测与替换解耦，N4/H6 根治）

历史：旧实现逐词 ``str.replace`` 基于已污染文本做后续 ``in`` 判断，
嵌套/重叠词条会产生乱码；现统一委托 core.engine 的区间模型，
本模块仅保留兼容入口与 mappings 兼容属性。
"""

from typing import List, Optional, Set

from mask_tool.models.detection import DetectionResult, DetectionStatus
from mask_tool.models.mapping import TokenMapping
from mask_tool.core.engine import ReplacementEngine
from mask_tool.core.tokenizer import TokenGenerator


class Masker:
    """脱敏执行器（薄委托统一替换引擎）"""

    def __init__(
        self,
        token_generator: TokenGenerator,
        irreversible: bool = False,
        amount_mode: str = "token",
        batch_id: str = "",
    ):
        """
        Args:
            token_generator: Token生成器
            irreversible: 是否使用不可逆脱敏
            amount_mode: 金额脱敏模式 "token" | "fuzzy" | "fixed"（M5）
            batch_id: 批次标识，写入 mapping（M3/M4）
        """
        self.token_gen = token_generator
        self.irreversible = irreversible
        self.engine = ReplacementEngine(
            token_generator,
            irreversible=irreversible,
            amount_mode=amount_mode,
            batch_id=batch_id,
        )
        # 兼容旧属性：与 engine 共享同一列表（adapter 侧 append 仍有效）
        self.mappings: List[TokenMapping] = self.engine.mappings

    def mask_text(
        self,
        text: str,
        results: List[DetectionResult],
        statuses: Optional[frozenset] = None,
        allowed_originals: Optional[Set[str]] = None,
    ) -> str:
        """对文本执行脱敏替换，返回脱敏后的文本。

        Args:
            text: 原始文本
            results: 检测结果列表（需已设置status）
            statuses: 参与替换的状态集合；缺省用 engine.active_statuses
                （初始仅 AUTO_MASK，即 H6：SUGGEST_MASK 不再默认替换）
            allowed_originals: confirm 模式：仅替换勾选的原文集合

        说明:
            results 中 status 不在 statuses 内的项自动跳过（H6）；
            重叠区间由引擎长度优先消解（D1 §2.3）。
        """
        outcome = self.engine.mask_plain_text(text, results, statuses, allowed_originals)
        return outcome.text

    def get_mappings(self) -> List[TokenMapping]:
        """获取所有映射关系"""
        return list(self.mappings)

    def reset(self) -> None:
        """重置状态（mapping 登记；reserved 约束保留，见 TokenGenerator.reset）"""
        self.engine.reset()
        self.token_gen.reset()
