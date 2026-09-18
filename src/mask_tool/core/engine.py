"""统一替换引擎：区间定位 -> 重叠消解 -> 替换构建 -> 一次性重建

设计要点（D1 §2）：
- 检测与替换彻底解耦：检测永远基于整段原始文本，替换基于原文本上的
  绝对偏移区间，一次重建输出，杜绝"边替换边匹配"的污染链（N4/H1/H2 根源）。
- token 生成唯一入口：引擎持有 TokenGenerator，所有格式经同一实例生成
  token，同一原文跨格式复用同一 token；mapping 登记只在引擎发生。
- 不依赖具体文档库：只处理纯文本与抽象"文本单元序列"，
  docx 的 run 落库由 adapter 完成。
"""

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from mask_tool.models.detection import DetectionResult, DetectionStatus, DetectionType
from mask_tool.models.mapping import TokenMapping, fingerprint_of
from mask_tool.core.tokenizer import TokenGenerator
from mask_tool.utils.text import fuzzy_amount

# token 样式（与 TokenGenerator.TYPE_PREFIX 保持同步生成）
TOKEN_PATTERN = re.compile(
    r"\[(?:COMPANY|GOVERNMENT|PERSON|PROJECT|SUBJECT|LOCATION|AMOUNT|CUSTOM)_\d{3,}\]"
)

# 金额脱敏模式合法取值
AMOUNT_MODES = frozenset({"token", "fuzzy", "fixed"})


@dataclass(frozen=True)
class Span:
    """原文本上的一个待替换区间"""
    start: int                    # 含
    end: int                      # 不含
    text: str                     # 必须 == full_text[start:end]（由 locate 保证）
    result: DetectionResult       # 携带 text_type/confidence/status/source

    def __post_init__(self):
        if self.end <= self.start:
            raise ValueError(f"Span 区间非法: [{self.start}, {self.end})")
        if not self.text:
            raise ValueError("Span.text 不能为空")

    def overlaps(self, other: "Span") -> bool:
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True)
class Replacement:
    """一次已消解的替换计划"""
    span: Span
    new_text: str                 # token / "***" / 模糊金额
    token: Optional[str]          # 可逆时为 token；不可逆/模糊时为 None


@dataclass
class MaskOutcome:
    """一次整段替换的结果"""
    text: str                     # 重建后的新文本
    replacements: List[Replacement] = field(default_factory=list)
    dropped_overlaps: List[Span] = field(default_factory=list)  # 被消解规则丢弃的区间（供报告审计）
    new_mappings: List[TokenMapping] = field(default_factory=list)


class ReplacementEngine:
    """统一替换引擎。

    adapter / xlsx / web 统一经 ``mask_plain_text``（或四步纯函数）执行替换；
    docx 的 run 落库用 ``plan_run_rewrite``；unmask 用 ``unmask_text``。
    """

    def __init__(
        self,
        token_generator: TokenGenerator,
        *,
        irreversible: bool = False,
        amount_mode: str = "token",      # "token" | "fuzzy" | "fixed"（M5 接线）
        batch_id: str = "",              # M3/M4：批次标识，写入 mapping
    ) -> None:
        if amount_mode not in AMOUNT_MODES:
            raise ValueError(f"amount_mode 非法: {amount_mode!r}，取值 {sorted(AMOUNT_MODES)}")
        self.token_gen = token_generator
        self.irreversible = irreversible
        self.amount_mode = amount_mode
        self.batch_id = batch_id
        # mapping 登记唯一发生地；Masker.mappings 与本列表共享同一对象
        self.mappings: List[TokenMapping] = []
        self._registered: Set[str] = set()   # 已登记过的 original
        # 当前文件作用域的过滤参数（process_file 设置，处理完恢复），
        # adapter 调 mask_plain_text(text, results) 时不传参数即生效
        self.active_statuses: frozenset = frozenset({DetectionStatus.AUTO_MASK})
        self.active_allowed_originals: Optional[Set[str]] = None
        # R1-A1 兜底：确认模式下被 allowed_originals 过滤掉的检测项
        # （text, source）去重列表；pipeline.process_file 处理完单文件后
        # 一次性汇总警告并清空，防止检测面与处理面不同步时静默漏脱
        self.confirm_filtered: List[Tuple[str, str]] = []

    # ---- 四个纯函数步骤（可独立单测） ----

    @staticmethod
    def locate(
        text: str,
        results: Sequence[DetectionResult],
        statuses: frozenset = frozenset({DetectionStatus.AUTO_MASK}),
        allowed_originals: Optional[Set[str]] = None,   # confirm 模式：仅替换勾选项
    ) -> List[Span]:
        """对每条通过过滤的检测结果，find-all 定位其在 text 中的全部出现。"""
        spans: List[Span] = []
        for r in results:
            if r.status not in statuses:
                continue
            if allowed_originals is not None and r.text not in allowed_originals:
                continue
            if not r.text:
                continue
            cursor = 0
            while True:
                idx = text.find(r.text, cursor)
                if idx == -1:
                    break
                end = idx + len(r.text)
                assert text[idx:end] == r.text, "Span 与原文切片不一致"
                spans.append(Span(idx, end, r.text, r))
                cursor = end                    # 非重叠推进
        return spans

    @staticmethod
    def resolve_overlaps(
        spans: List[Span],
    ) -> Tuple[List[Span], List[Span]]:
        """返回 (按 start 升序的入选区间, 被丢弃区间)。

        消解规则（D1 §2.3）：长度降序为主序（漏脱代价远高于多脱，长区间优先
        最大化覆盖且保证最长原文被完整登记），置信度降序为次序（平局裁决，
        同区间取置信度最高者即排序首位），start 升序为第三序；贪心选择——
        按序遍历，与已选区间重叠者丢弃并记入 dropped_overlaps。
        """
        order = sorted(
            spans,
            key=lambda s: (-(s.end - s.start), -s.result.confidence, s.start),
        )
        chosen: List[Span] = []
        dropped: List[Span] = []
        for s in order:
            if any(s.overlaps(c) for c in chosen):
                dropped.append(s)
            else:
                chosen.append(s)
        return sorted(chosen, key=lambda s: s.start), dropped

    def build_replacements(
        self,
        text: str,
        spans: Sequence[Span],        # 须已消解、按 start 升序
    ) -> List[Replacement]:
        """为每个区间生成替换串；可逆时经 token_generator 生成 token，
        并在首次遇到某 original 时登记 TokenMapping（含 batch_id 与指纹）。"""
        reps: List[Replacement] = []
        memo: Dict[str, Replacement] = {}   # original -> Replacement（同一原文复用替换串）
        for s in spans:
            key = s.text
            if key in memo:
                # 同一原文再次出现：复用替换串，但区间（span）是各自的
                reps.append(Replacement(span=s, new_text=memo[key].new_text, token=memo[key].token))
                continue
            if self.irreversible:
                new_text, token = "***", None
            elif s.result.text_type == DetectionType.AMOUNT and self.amount_mode == "fuzzy":
                # M5：模糊金额，不可逆、不登记 mapping
                new_text, token = fuzzy_amount(key), None
            elif s.result.text_type == DetectionType.AMOUNT and self.amount_mode == "fixed":
                new_text, token = "***", None
            else:
                token = self.token_gen.generate(key, s.result.text_type)
                new_text = token
                if key not in self._registered:   # 仅首次登记
                    self._registered.add(key)
                    self.mappings.append(TokenMapping(
                        token=token,
                        original=key,
                        text_type=s.result.text_type,
                        confidence=s.result.confidence,
                        batch_id=self.batch_id,
                        fingerprint=fingerprint_of(key),
                    ))
            memo[key] = Replacement(span=s, new_text=new_text, token=token)
            reps.append(memo[key])
        return reps

    @staticmethod
    def apply(text: str, replacements: Sequence[Replacement]) -> str:
        """按区间一次重建新文本。replacements 须已消解且按 start 升序。"""
        out: List[str] = []
        cursor = 0
        for rep in replacements:
            if rep.span.start < cursor:
                raise ValueError("替换区间存在重叠或未按 start 升序排列")
            out.append(text[cursor:rep.span.start])
            out.append(rep.new_text)
            cursor = rep.span.end
        out.append(text[cursor:])
        return "".join(out)

    # ---- 组合入口（adapter / xlsx / web 统一调用） ----

    def mask_plain_text(
        self,
        text: str,
        results: Sequence[DetectionResult],
        statuses: Optional[frozenset] = None,
        allowed_originals: Optional[Set[str]] = None,
    ) -> MaskOutcome:
        """整段文本替换组合入口。

        statuses / allowed_originals 缺省时使用 active_*（由 process_file 设置的
        当前文件作用域参数；active_statuses 初始为仅 AUTO_MASK，即 H6 行为）。
        """
        if statuses is None:
            statuses = self.active_statuses
        if allowed_originals is None:
            allowed_originals = self.active_allowed_originals
        if allowed_originals is not None:
            # R1-A1 兜底：记录 AUTO 但被勾选集排除的项（去重），供 pipeline
            # 汇总警告（检测面盲区或用户未勾选，均不静默；SUGGEST 项不记，
            # 用户主动取消勾选建议项属正常语义）
            for r in results:
                if (
                    r.status == DetectionStatus.AUTO_MASK
                    and r.status in statuses
                    and r.text not in allowed_originals
                    and (r.text, r.source) not in self.confirm_filtered
                ):
                    self.confirm_filtered.append((r.text, r.source))
        spans = self.locate(text, results, statuses, allowed_originals)
        kept, dropped = self.resolve_overlaps(spans)
        mappings_before = len(self.mappings)
        reps = self.build_replacements(text, kept)
        return MaskOutcome(
            text=self.apply(text, reps),
            replacements=reps,
            dropped_overlaps=dropped,
            new_mappings=self.mappings[mappings_before:],
        )

    # ---- run 重建辅助（P1：整段并入首文本单元） ----

    @staticmethod
    def plan_run_rewrite(
        orig_texts: Sequence[str],    # 各文本单元（run/cell/w:t）原文本
        new_text: str,                # 整段替换后文本
    ) -> List[str]:
        """返回各单元的新文本：[new_text] + [""] * (n-1)；空单元列表返回 []。"""
        if not orig_texts:
            return []
        return [new_text] + [""] * (len(orig_texts) - 1)

    # ---- token 对账（M3 防护） ----

    @staticmethod
    def scan_tokens(text: str) -> Set[str]:
        """提取文本中已出现的全部 token 样式串（mask 前预扫描用）。"""
        return set(TOKEN_PATTERN.findall(text))

    # ---- unmask（M3：token -> original 整串替换） ----

    @staticmethod
    def unmask_text(text: str, token_map: Dict[str, str]) -> Tuple[str, Set[str]]:
        """按 token -> original 整串替换，返回 (新文本, 命中的 token 集合)。

        长 token 先替换，防止形如 ``[PERSON_1]`` 与 ``[PERSON_12]`` 的
        前缀吞并。未命中的 token 不影响文本。
        """
        new_text = text
        hits: Set[str] = set()
        for token in sorted(token_map, key=len, reverse=True):
            if token and token in new_text:
                new_text = new_text.replace(token, token_map[token])
                hits.add(token)
        return new_text, hits

    # ---- 状态管理 ----

    def reset(self) -> None:
        """清空 mapping 登记状态（不动 token_gen / reserved）。"""
        self.mappings.clear()
        self._registered.clear()
        self.confirm_filtered.clear()
