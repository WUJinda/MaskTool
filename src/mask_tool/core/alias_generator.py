"""伪装词生成器 - alias 脱敏风格下生成"汤姆/A市/A集团"式自然伪装词

与 ``core.tokenizer.TokenGenerator`` 同接口的替换串生成器（二选一经
Pipeline 按 ``mask_style`` 组装）：Token 风格生成 ``[COMPANY_001]`` 占位符，
本生成器生成伪装词——文档观感自然，读者不易察觉脱敏痕迹，还原链路不变
（mapping 的 token 字段存伪装词，unmask 按 ``{伪装词: 原文}`` 整串替换）。

设计要点：
- 确定性：固定词池 + 字母序展开，同一原文恒得同一伪装词（同批次内），
  跨文件一致（与 TokenGenerator 复用语义一致，引擎 memo 双保险）。
- 撞词避让（alias 特有风险）：伪装词是普通词汇，可能自然出现在原文中；
  若原文含"张三"而"张三"又被分配为他人伪装词，unmask 会误还原。防线：
  Pipeline.prepare 预扫描全文（XML 剥标签，跨 run 拆分的词可命中）经
  ``register_source_text`` 登记，生成时跳过任何已出现在文档中的候选词
  （宁多勿漏）；超大批次登记量达上限后退化为仅 unmask 对账兜底。
- 前缀约束：新候选与已用伪装词互不为前缀（"汤姆" vs "汤姆猫"），
  防 unmask 整串替换的前缀吞并（替换侧另有长 key 先行兜底）。
- 字符约束：候选不得含 Windows 文件名非法字符（PathMasker 共享本生成器，
  伪装词会进入文件名）；默认词池天然满足，自定义词池在生成时校验。
- 风格边界（决策 D1）：仅实体类走伪装词；AMOUNT 一律 token 样式；
  CUSTOM 按 source 区分——regex 命中（手机号/身份证/邮箱/日期/卡号）
  保持 ``[CUSTOM_xxx]`` 强样式（伪装词对数字类无意义），词库/NER/手动
  词走伪装词池。
- 池耗尽兜底：回退原类别 token 样式（编号让位兼容 set_reserved），
  映射与还原逻辑不变，仅观感退化为占位符（警告由调用方透出）。
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from mask_tool.core.tokenizer import TokenGenerator
from mask_tool.models.detection import DetectionType

logger = logging.getLogger("mask_tool")

# Windows 文件名非法字符 + 控制字符（伪装词会进入文件名，词池层面拦截）
_FILENAME_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# 内置人名假名池（顺序取用；均为常见代称/假名，彼此无前缀关系）
DEFAULT_PERSON_POOL = [
    "汤姆", "杰瑞", "张三", "李四", "王五", "赵六", "钱七", "孙八",
    "周九", "吴十", "小明", "小红", "小刚", "小丽", "阿福", "阿贵",
    "大壮", "来福", "建国", "建军", "爱国", "志强", "伟民", "秀英",
    "桂芳", "春花", "约翰", "大卫", "玛丽", "琳达", "杰克", "露西",
    "托尼", "安妮", "老王", "老李", "老张", "小刘", "小陈", "小杨",
    "阿明", "阿强", "阿芳", "秋月", "冬梅", "望舒", "云帆", "知远",
]

DEFAULT_LOCATION_SUFFIXES = ["市", "省", "县", "区"]
DEFAULT_COMPANY_SUFFIXES = ["集团", "公司", "企业", "厂"]
DEFAULT_GOVERNMENT_SUFFIXES = ["单位", "局", "委", "部", "厅", "办"]
DEFAULT_PROJECT_SUFFIXES = ["项目", "工程", "标的", "课题"]
# 天干地支（custom 词库/手动词池，22 个）
DEFAULT_CUSTOM_POOL = [
    "甲", "乙", "丙", "丁", "戊", "己", "庚", "辛", "壬", "癸",
    "子", "丑", "寅", "卯", "辰", "巳", "午", "未", "申", "酉", "戌", "亥",
]

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


@dataclass
class AliasLexicon:
    """伪装词池配置（内置默认 + 可选 config/alias_lexicon.yaml 覆盖）"""
    person_pool: List[str] = field(default_factory=lambda: list(DEFAULT_PERSON_POOL))
    location_suffixes: List[str] = field(
        default_factory=lambda: list(DEFAULT_LOCATION_SUFFIXES))
    company_suffixes: List[str] = field(
        default_factory=lambda: list(DEFAULT_COMPANY_SUFFIXES))
    government_suffixes: List[str] = field(
        default_factory=lambda: list(DEFAULT_GOVERNMENT_SUFFIXES))
    project_suffixes: List[str] = field(
        default_factory=lambda: list(DEFAULT_PROJECT_SUFFIXES))
    custom_pool: List[str] = field(default_factory=lambda: list(DEFAULT_CUSTOM_POOL))

    @classmethod
    def from_yaml(cls, path: Path) -> "AliasLexicon":
        """从 YAML 加载词池；缺段回退内置默认（逐键 merge，不做整体替换）"""
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        base = cls()
        mapping = {
            "person": "person_pool",
            "person_pool": "person_pool",
            "location_suffixes": "location_suffixes",
            "company_suffixes": "company_suffixes",
            "government_suffixes": "government_suffixes",
            "project_suffixes": "project_suffixes",
            "custom": "custom_pool",
            "custom_pool": "custom_pool",
        }
        for key, attr in mapping.items():
            value = data.get(key)
            if isinstance(value, list) and value:
                cleaned = [str(v) for v in value if str(v).strip()]
                if cleaned:
                    setattr(base, attr, cleaned)
        return base

    @classmethod
    def load(cls, path_str: str, config_dir: str = "") -> "AliasLexicon":
        """按配置路径加载词池；路径缺失回退内置默认（正常场景，不告警）；
        文件存在但解析失败时告警并回退内置默认（用户应知道自定义池未生效）"""
        from mask_tool.core.config_loader import find_data_file, resolve_data_path
        try:
            if path_str:
                base_dir = Path(config_dir) if config_dir else Path.cwd()
                p = resolve_data_path(path_str, base_dir)
                if not p.exists():
                    p = find_data_file(path_str)
                if p and p.exists():
                    try:
                        return cls.from_yaml(p)
                    except Exception as exc:
                        logger.warning(
                            "伪装词池配置解析失败（%s），回退内置默认池: %s", p, exc
                        )
        except Exception as exc:
            logger.warning("伪装词池配置定位失败，回退内置默认池: %s", exc)
        return cls()

    def candidates_for(self, text_type: DetectionType) -> List[str]:
        """类别的完整候选序列（字母序展开；调用方按下标惰性取用）"""
        if text_type == DetectionType.PERSON:
            return list(self.person_pool)
        if text_type == DetectionType.LOCATION:
            return self._expand(self.location_suffixes)
        if text_type == DetectionType.COMPANY:
            return self._expand(self.company_suffixes)
        if text_type == DetectionType.GOVERNMENT:
            return self._expand(self.government_suffixes)
        if text_type in (DetectionType.PROJECT, DetectionType.SUBJECT):
            return self._expand(self.project_suffixes)
        return list(self.custom_pool)

    @staticmethod
    def _expand(suffixes: List[str]) -> List[str]:
        """字母 × 后缀模板展开：A市 B市 … Z市 A省 …（后缀外层、字母内层）"""
        return [
            f"{LETTERS[i % len(LETTERS)]}{suffix}"
            for suffix in suffixes
            for i in range(len(LETTERS))
        ]


class AliasGenerator:
    """伪装词生成器（与 TokenGenerator 同接口，Pipeline 按 mask_style 二选一）

    引擎（ReplacementEngine.build_replacements）与文件名脱敏（PathMasker）
    仅调用 ``generate(original, text_type, source)``，不感知具体风格。
    """

    # 预扫描全文登记总量上限（字符数）：超大批次时后续文件不再登记并警告，
    # 避让退化为仅 unmask 对账兜底（第二道防线仍在）。约 64M 字符
    # （CJK 约 128-256MB 内存）已覆盖常规批次。
    SOURCE_TEXT_MAX_CHARS = 64_000_000
    # 单段登记上限：单个超大文件剥标签后仍整体入表会突破总量一个文件量，
    # 超限截断登记前段（部分避让优于不登记）并警告。
    SOURCE_BLOB_MAX_CHARS = 8_000_000

    def __init__(self, lexicon: Optional[AliasLexicon] = None):
        self.lexicon = lexicon or AliasLexicon()
        self._alias_map: Dict[str, str] = {}       # original -> 伪装词（同原文复用）
        self._counters: Dict[str, int] = {}        # 类别前缀 -> 已推进下标（含跳过项）
        self._used_aliases: List[str] = []         # 已分配伪装词（前缀约束 + 审计）
        self._source_texts: List[str] = []         # 预扫描全文（撞词避让）
        self._source_chars: int = 0                # 已登记字符总量（上限保护）
        self._source_overflow: bool = False        # 超限标志（重复警告去重）
        self._blob_truncated: bool = False         # 单段截断标志（重复警告去重）
        self._reserved: set = set()                # token 样式串（fallback 编号让位）
        self._fallback_counters: Dict[str, int] = {}
        self._pool_exhausted: set = set()           # 真·词池耗尽的类别（误报区分：
        #                                            # AMOUNT/regex-CUSTOM 走 token 是
        #                                            # 风格边界设计，不算耗尽）

    # ---- 生成入口 ----

    def generate(self, original: str, text_type: DetectionType, source: str = "") -> str:
        """为原文生成伪装词；同一原文在同一次运行中返回同一伪装词。

        风格边界：AMOUNT 与 regex 来源的 CUSTOM 返回 token 样式
        （数字类实体伪装词无意义）；其余实体类返回伪装词。
        """
        if original in self._alias_map:
            return self._alias_map[original]

        alias = self._generate_for_type(original, text_type, source)
        self._alias_map[original] = alias
        return alias

    def _generate_for_type(
        self, original: str, text_type: DetectionType, source: str,
    ) -> str:
        if text_type == DetectionType.AMOUNT or (
            text_type == DetectionType.CUSTOM and source == "regex"
        ):
            return self._fallback_token(text_type)

        prefix = _TYPE_PREFIX.get(text_type, "CUSTOM")
        candidates = self.lexicon.candidates_for(text_type)
        n = self._counters.get(prefix, 0)
        total = len(candidates)
        while n < total:
            candidate = candidates[n]
            n += 1
            if self._is_free(candidate):
                self._counters[prefix] = n
                self._used_aliases.append(candidate)
                return candidate
        self._counters[prefix] = n
        self._pool_exhausted.add(prefix)
        return self._fallback_token(text_type)

    def _fallback_token(self, text_type: DetectionType) -> str:
        """词池耗尽 / 数字类实体：回退原类别 token 样式（编号让位兼容）"""
        prefix = _TYPE_PREFIX.get(text_type, "CUSTOM")
        n = self._fallback_counters.get(prefix, 0)
        while True:
            cand = f"[{prefix}_{n + 1:03d}]"
            if cand in self._reserved or cand in self._used_aliases:
                n += 1
                continue
            break
        n += 1
        self._fallback_counters[prefix] = n
        token = f"[{prefix}_{n:03d}]"
        self._used_aliases.append(token)
        return token

    # ---- 约束检查 ----

    def _is_free(self, candidate: str) -> bool:
        """候选可用性：字符合法、未在源文档出现、与已用词互不为前缀、未复用"""
        if not candidate or _FILENAME_FORBIDDEN_RE.search(candidate):
            return False
        if candidate.startswith("["):
            # token 样式串混入自定义池：拒绝（否则会与 fallback 编号复用同一
            # 替换串 → 双原文同词 → mapping 覆盖 + unmask 静默误还原）
            return False
        if candidate in self._used_aliases:
            return False
        for blob in self._source_texts:
            if candidate in blob:
                return False
        for used in self._used_aliases:
            if used.startswith(candidate) or candidate.startswith(used):
                return False
        return True

    # ---- 预扫描登记（Pipeline.prepare 调用） ----

    def register_source_text(self, text: str) -> None:
        """登记一段源文档文本（mask 前预扫描），生成时避让其中已存在的词。

        单段超过 SOURCE_BLOB_MAX_CHARS 截断登记前段（部分避让优于不登记）；
        总量超过 SOURCE_TEXT_MAX_CHARS 后不再登记（防超大批次内存膨胀），
        避让降级为仅靠 unmask 对账兜底；该限制在 reset 后仍然保留
        （与 reserved 同为外部防线）。"""
        if not text:
            return
        if len(text) > self.SOURCE_BLOB_MAX_CHARS:
            text = text[: self.SOURCE_BLOB_MAX_CHARS]
            if not self._blob_truncated:
                self._blob_truncated = True
                logger.warning(
                    "单个文件预扫描文本超过 %.0fM 字符，已截断登记前段"
                    "（截断后部分的内容不参与撞词避让）",
                    self.SOURCE_BLOB_MAX_CHARS / 1_000_000,
                )
        if self._source_chars >= self.SOURCE_TEXT_MAX_CHARS:
            if not self._source_overflow:
                self._source_overflow = True
                logger.warning(
                    "alias 避让预扫描登记量已达上限（%.0fM 字符），后续文件"
                    "不再登记；撞词避让退化为仅 unmask 对账兜底",
                    self.SOURCE_TEXT_MAX_CHARS / 1_000_000,
                )
            return
        self._source_texts.append(text)
        self._source_chars += len(text)

    def set_reserved(self, tokens: Iterable[str]) -> None:
        """登记文档中已存在的 token 样式串（与 TokenGenerator 同名兼容；
        alias 模式下仅用于 fallback token 的编号让位）"""
        self._reserved.update(tokens)

    # ---- 状态与审计 ----

    def get_all_mappings(self) -> Dict[str, str]:
        """获取所有 original -> 伪装词映射"""
        return dict(self._alias_map)

    def get_used_aliases(self) -> List[str]:
        """本批次已分配的全部替换串（伪装词 + fallback token，审计用）"""
        return list(self._used_aliases)

    def exhausted_categories(self) -> List[str]:
        """词池真正耗尽（alias 池见底后回退占位符）的类别前缀，供调用方警告。

        AMOUNT / regex 来源 CUSTOM 走 token 样式是风格边界设计（决策 D1），
        不计入——否则几乎每个含金额/手机号的批次都会误报"词池耗尽"。"""
        return sorted(self._pool_exhausted)

    def reset(self) -> None:
        """重置计数器和映射。reserved 与 source_texts 保留（外部防线，
        不随单次运行清除，语义与 TokenGenerator.reset 一致）"""
        self._counters.clear()
        self._alias_map.clear()
        self._used_aliases.clear()
        self._fallback_counters.clear()
        self._pool_exhausted.clear()


# 类别前缀映射：与 TokenGenerator.TYPE_PREFIX 单源维护（审查 Info：防双份漂移）
_TYPE_PREFIX = TokenGenerator.TYPE_PREFIX
