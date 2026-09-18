"""Token生成器 - 生成可逆脱敏的唯一Token"""

from typing import Dict, Iterable

from mask_tool.models.detection import DetectionType


class TokenGenerator:
    """按类别生成递增编号的Token，如 [COMPANY_001]"""

    # 类别前缀映射
    TYPE_PREFIX = {
        DetectionType.COMPANY: "COMPANY",
        DetectionType.GOVERNMENT: "GOVERNMENT",
        DetectionType.PERSON: "PERSON",
        DetectionType.PROJECT: "PROJECT",
        DetectionType.SUBJECT: "SUBJECT",
        DetectionType.LOCATION: "LOCATION",
        DetectionType.AMOUNT: "AMOUNT",
        DetectionType.CUSTOM: "CUSTOM",
    }

    def __init__(self):
        self._counters: Dict[str, int] = {}
        self._token_map: Dict[str, str] = {}  # original -> token
        self._reserved: set = set()            # M3：文档中已存在的 token 样式串，编号让位

    def generate(self, original: str, text_type: DetectionType) -> str:
        """
        为原文生成Token。同一原文在同一次运行中返回相同Token。

        Args:
            original: 原始文本
            text_type: 敏感信息类别

        Returns:
            Token字符串，如 "[COMPANY_001]"
        """
        # 同一原文复用Token
        if original in self._token_map:
            return self._token_map[original]

        prefix = self.TYPE_PREFIX.get(text_type, "CUSTOM")
        n = self._counters.get(prefix, 0)
        # M3：编号递增时跳过文档中已占用的 token 样式串，避免撞号
        while f"[{prefix}_{n + 1:03d}]" in self._reserved:
            n += 1
        n += 1
        self._counters[prefix] = n

        token = f"[{prefix}_{n:03d}]"
        self._token_map[original] = token
        return token

    def set_reserved(self, tokens: Iterable[str]) -> None:
        """
        登记预扫描得到的、文档中已出现的 token 样式串（累积合并）。

        后续 generate 的编号将跳过这些字符串，防止新生 token 与
        文档自然文本撞号导致 unmask 误还原（M3 防线一）。
        """
        self._reserved.update(tokens)

    def get_reserved(self) -> set:
        """获取当前已登记的 reserved token 集合"""
        return set(self._reserved)

    def get_all_mappings(self) -> Dict[str, str]:
        """获取所有 original -> token 映射"""
        return dict(self._token_map)

    def reset(self) -> None:
        """重置计数器和映射。reserved 约束保留（外部防线，不随单次运行清除）"""
        self._counters.clear()
        self._token_map.clear()
