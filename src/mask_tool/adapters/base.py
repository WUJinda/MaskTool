"""文件适配器抽象基类"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from mask_tool.core.detector import Detector
from mask_tool.core.engine import MaskOutcome
from mask_tool.core.masker import Masker
from mask_tool.core.policy import PolicyEngine
from mask_tool.models.detection import Location


class FileAdapter(ABC):
    """文件格式适配器基类，所有格式适配器必须继承此类"""

    def __init__(
        self,
        detector: Detector,
        policy: PolicyEngine,
        masker: Masker,
    ):
        self.detector = detector
        self.policy = policy
        self.masker = masker

    def _mask_text_block(
        self,
        text: str,
        file_path: str = "",
        *,
        context_tag: str = "",
        location: Optional[Location] = None,
    ) -> Optional[MaskOutcome]:
        """共享文本脱敏组合入口（D1 §3.4）：检测 -> 策略 -> 引擎替换。

        引擎调用不显式传 statuses / allowed_originals，当前文件作用域参数
        （Pipeline.process_file 设置的 engine.active_*）自动生效；直接调用
        adapter 时为引擎默认（仅 AUTO_MASK，即 H6 行为）。

        Args:
            text: 待脱敏的整段文本
            file_path: 来源文件路径（写入 Location）
            context_tag: 位置标签（如 "页眉L"），以 "[标签]" 前缀并入 context
            location: 预构造位置对象；None 时仅写 file

        Returns:
            有实际替换时返回 MaskOutcome，否则 None
        """
        if not text or not text.strip():
            return None
        results = self.detector.detect(text, file_path)
        for r in results:
            if location is not None:
                r.location = location
            if context_tag and not r.context.startswith("["):
                r.context = f"[{context_tag}] {r.context}"
        results = self.policy.apply(results)
        outcome = self.masker.engine.mask_plain_text(text, results)
        if not outcome.replacements:
            return None
        return outcome

    @abstractmethod
    def process(
        self,
        input_path: Path,
        output_dir: Path,
        output_name: Optional[str] = None,
    ) -> Path:
        """
        处理文件并输出脱敏后的文件

        Args:
            input_path: 输入文件路径
            output_dir: 输出目录
            output_name: 输出文件名；None 时由 adapter 自定（如 {stem}_masked）

        Returns:
            输出文件路径
        """
        ...

    @abstractmethod
    def supported_extensions(self) -> list[str]:
        """返回支持的文件扩展名列表"""
        ...
