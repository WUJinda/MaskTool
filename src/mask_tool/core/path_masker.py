"""文件名/目录名脱敏 - 对路径主名执行词库+正则检测与可逆替换

设计依据：design-D3-paths.md。
核心流程（输出目录模式下"先内容后名字"）：
    内容脱敏完成后，对输出镜像树执行 mask_tree() 自底向上（先子后父）改名；
    unmask_tree() 同样自底向上还原，保证父目录改名不会使其子孙的映射路径失效。

与 Pipeline 共享 detector/policy/token_gen 实例，文件名中的实体与文档内容中的
同一实体获得同一 token，unmask 一次替换全部还原。

本模块零侵入 models/mapping.py：PathMapping 定义于本文件内，
通过 merge_into_mapping() 并入批次 mapping.json 的顶层 "paths" 段。
"""

import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from mask_tool.core.detector import Detector
from mask_tool.core.policy import PolicyEngine
from mask_tool.core.tokenizer import TokenGenerator
from mask_tool.models.detection import (
    DetectionResult,
    DetectionStatus,
    DetectionType,
)

# ──────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────

WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
# 查重用 casefold 形式（与查询键一致，避免大小写不匹配导致永不命中）
_RESERVED_FOLDED = {name.casefold() for name in WINDOWS_RESERVED}
ILLEGAL_CHARS = '<>:"/\\|?*'

# 新绝对路径长度超过该值时跳过改名（不截断 token，截断破坏还原；保原名最安全）。
# MAX_PATH 260 - 结尾 NUL = 259。
MAX_PATH_LIMIT = 259

# win_long() 触发 \\?\ 前缀的长度阈值（提前留出余量）
LONG_PATH_THRESHOLD = 240

# 状态常量（PathMapping.status 取值）
STATUS_RENAMED = "renamed"
STATUS_CONFLICT = "conflict_suffixed"
STATUS_SKIPPED = "skipped"

# regex+CUSTOM 的保留模式（出现在文件名中时几乎必然是隐私）
_PHONE_RE = re.compile(r"1[3-9]\d{9}")
_ID_RE = re.compile(r"\d{17}[\dXx]")
_EMAIL_RE = re.compile(r"[\w.-]+@[\w.-]+\.\w+")
_DATE_RE = re.compile(r"\d{4}年\d{1,2}月\d{1,2}日")
# 16~19 位纯数字：银行卡规则无边界，订单号/合同编号误报重灾区，一律滤除
# （纯数字身份证号与订单号在文件名场景不可区分，文档内容由内容脱敏覆盖）
_LONG_DIGITS_RE = re.compile(r"\d{16,19}")

# 目录+--confirm 模式下未勾选（内容未脱敏）文件的隔离目录名（R1-B9）：
# 拷入镜像树内该子目录，mask_tree 计划对其整体豁免改名，产物级区分
# "名字已脱敏、内容未脱敏"的误导风险。
SKIPPED_UNMASKED_DIR = "skipped_unmasked"


# ──────────────────────────────────────────────
# 数据对象
# ──────────────────────────────────────────────

@dataclass
class PathMapping:
    """一次路径改名记录（文件或目录）。只记录发生改名的路径；未命中的路径不记录。"""
    kind: str                  # "file" | "dir"
    old_name: str              # 原主名（文件=stem+suffix 完整名；目录=完整名）
    new_name: str              # 新主名（扩展名保持不变）
    rel_old: str               # 相对树根的旧相对路径（POSIX 分隔符，可移植）
    rel_new: str               # 相对树根的新相对路径
    depth: int                 # 距根深度（根=0；unmask 按此降序排序）
    categories: List[str]      # 命中类别列表，如 ["company","person"]
    tokens_used: List[str]     # 该主名用到的 token，如 ["[COMPANY_001]"]
    fingerprint: str           # sha256(f"{rel_old}|{new_name}")[:12]，还原定位校验
    status: str                # "renamed" | "conflict_suffixed" | "skipped"
    note: str = ""             # status != renamed 时的原因（"locked"/"too_long"/…）
    created_at: str = ""       # ISO-8601 UTC

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "old_name": self.old_name,
            "new_name": self.new_name,
            "rel_old": self.rel_old,
            "rel_new": self.rel_new,
            "depth": self.depth,
            "categories": list(self.categories),
            "tokens_used": list(self.tokens_used),
            "fingerprint": self.fingerprint,
            "status": self.status,
            "note": self.note,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PathMapping":
        return cls(
            kind=data.get("kind", "file"),
            old_name=data.get("old_name", ""),
            new_name=data.get("new_name", ""),
            rel_old=data.get("rel_old", ""),
            rel_new=data.get("rel_new", ""),
            depth=int(data.get("depth", 1)),
            categories=list(data.get("categories", [])),
            tokens_used=list(data.get("tokens_used", [])),
            fingerprint=data.get("fingerprint", ""),
            status=data.get("status", STATUS_RENAMED),
            note=data.get("note", ""),
            created_at=data.get("created_at", ""),
        )

    def make_fingerprint(self) -> str:
        """按定义重算指纹（构造后可调用）。"""
        return hashlib.sha256(
            f"{self.rel_old}|{self.new_name}".encode("utf-8")
        ).hexdigest()[:12]


@dataclass
class RenameItem:
    """计划阶段产物：一条待改名指令（纯数据，可单测）"""
    path: Path                                  # 当前绝对路径（计划时刻有效，旧父链）
    kind: str                                   # "file" | "dir"
    old_name: str
    new_name: str                               # 已含冲突序号、已 sanitize（最终名）
    depth: int
    detections: List[DetectionResult] = field(default_factory=list)
    tokens_used: List[str] = field(default_factory=list)
    categories: List[str] = field(default_factory=list)
    suffixed: bool = False                      # new_name 追加过冲突序号
    skip_reason: str = ""                       # 非空（如 "too_long"）则不执行 rename
    rel_new_final: str = ""                     # 全部改名完成后的最终相对路径（新父链）


@dataclass
class PathMaskResult:
    """一次 mask_tree / mask_filename 的执行结果"""
    renamed: List[PathMapping] = field(default_factory=list)   # renamed/conflict_suffixed
    skipped: List[PathMapping] = field(default_factory=list)   # skipped（locked 等）
    unchanged: int = 0                                          # 检测未命中、无需改名的路径数
    warnings: List[str] = field(default_factory=list)

    @property
    def all_mappings(self) -> List[PathMapping]:
        """写入 paths 段的顺序：renamed 在前、skipped 在后。"""
        return self.renamed + self.skipped


@dataclass
class UnmaskResult:
    """一次 unmask_tree 的执行结果"""
    restored: List[PathMapping] = field(default_factory=list)
    missing: List[PathMapping] = field(default_factory=list)   # rel_new 不存在（用户已改动/删除）
    extra: List[str] = field(default_factory=list)             # 树中存在但 paths 未覆盖的改名残留
    warnings: List[str] = field(default_factory=list)


# ──────────────────────────────────────────────
# 模块级工具函数（可独立单测）
# ──────────────────────────────────────────────

def _split_ext(name: str) -> Tuple[str, str]:
    """把完整名拆成 (stem, suffix)。仅当最后一个点位于非开头且非结尾时拆分。

    与 PurePath.suffix 语义一致，但纯字符串实现避免 "." 等特殊名的奇异行为。
    """
    idx = name.rfind(".")
    if idx > 0 and idx < len(name) - 1:
        return name[:idx], name[idx:]
    return name, ""


def sanitize_name(name: str, *, reserved_ok: bool = False) -> str:
    """清洗文件/目录主名，保证 Windows 可创建且"创建名 == 请求名"。

    ① 剔除 ILLEGAL_CHARS 与控制字符（替换为 "_"）；
    ② 剥离 stem 结尾空格/点（Win32 对整名末尾静默剥离，破坏 mapping 可逆性）；
    ③ 保留名检查：stem（首个 "." 前部分）casefold 命中 WINDOWS_RESERVED 时
       在 stem 末尾追加 "_"（CON.docx → CON_.docx）；
    ④ 结果为空串时返回 "_masked"。
    reserved_ok=True 跳过③（测试用）。
    """
    # ① 非法字符与控制字符
    cleaned = "".join(
        "_" if (ch in ILLEGAL_CHARS or ord(ch) < 0x20) else ch
        for ch in name
    )
    # ② stem 尾点/空格剥离
    stem, suffix = _split_ext(cleaned)
    stem = stem.rstrip(" .")
    # 无扩展名时整名剥离（rstrip 已含于 stem；suffix 为空即整名）
    if not suffix:
        cleaned = stem.rstrip(" .")
    else:
        cleaned = stem + suffix
    # ③ 保留名
    if not reserved_ok and cleaned:
        first, rest = _split_ext(cleaned)
        if first.casefold() in _RESERVED_FOLDED:
            cleaned = first + "_" + rest if rest else first + "_"
    # ④ 空兜底
    if not cleaned:
        cleaned = "_masked"
    return cleaned


def win_long(path: Path) -> str:
    """len(str(path)) >= 240 时返回 '\\\\?\\' + 绝对路径（仅反斜杠），否则原样 str。

    仅用于 os.rename/os.makedirs 等 os 层调用；Path 对象内部表示不变。
    """
    s = str(path)
    if len(s) >= LONG_PATH_THRESHOLD:
        return "\\\\?\\" + str(path.resolve())
    return s


def unique_name(parent: Path, desired: str, taken: Set[str]) -> str:
    """冲突消解：desired 与磁盘现存（大小写不敏感）或 taken 集合碰撞时，
    返回 f"{stem}_{n}{suffix}"（n 从 1 递增）。taken 以 casefold 形式登记。
    """
    parent = Path(parent)

    def _occupied(candidate: str) -> bool:
        if candidate.casefold() in taken:
            return True
        try:
            if (parent / candidate).exists():
                return True
        except OSError:
            pass
        return False

    if not _occupied(desired):
        return desired
    stem, suffix = _split_ext(desired)
    n = 1
    while True:
        candidate = f"{stem}_{n}{suffix}"
        if not _occupied(candidate):
            return candidate
        n += 1


# 瞬时锁重试参数：Windows 上新建文件可能被 Defender/索引服务短暂锁定
# （实测约 5% 概率、毫秒级），与用户持续占用（分钟级）不同。
RENAME_ATTEMPTS = 3
RENAME_RETRY_DELAY = 0.05  # 秒


def rename_with_retry(src: str, dst: str) -> None:
    """os.rename + 瞬时锁短重试。

    仅对 PermissionError 重试；持续占用重试耗尽后照常抛出，
    由调用方走 skipped 兜底（保持 D3 占用容错语义）。
    """
    for attempt in range(RENAME_ATTEMPTS):
        try:
            os.rename(src, dst)
            return
        except PermissionError:
            if attempt == RENAME_ATTEMPTS - 1:
                raise
            time.sleep(RENAME_RETRY_DELAY)


# ──────────────────────────────────────────────
# 主类
# ──────────────────────────────────────────────

class PathMasker:
    """文件名/目录名脱敏器。与 Pipeline 共享 detector/policy/token_gen 实例。"""

    def __init__(
        self,
        detector: Detector,
        policy: PolicyEngine,
        token_gen: TokenGenerator,
        irreversible: bool = False,
    ) -> None:
        self.detector = detector
        self.policy = policy
        self.token_gen = token_gen
        self.irreversible = irreversible
        self._accumulated: List[PathMapping] = []
        self._last_result: PathMaskResult = PathMaskResult()
        self._last_plan_unchanged: int = 0

    # ── 检测与命名（可独立调用，inspect 也用） ──

    def detect_name(
        self,
        name: str,
        context: str = "",
        allowed_originals: Optional[Set[str]] = None,
    ) -> List[DetectionResult]:
        """对单个主名（stem 或目录名）执行检测 + 短文本策略过滤。

        过滤规则（D3 §4.1，纵深防御，独立于 M6 是否修复）：
          保留: dictionary / ner
          保留: regex+CUSTOM 且匹配手机/身份证/邮箱/日期形态
          滤除: regex+AMOUNT（"500万.xlsx" 这类标价样式）
          滤除: regex+CUSTOM 且为 16~19 位纯数字（订单号/合同编号误报重灾区）
          滤除: ner 项嵌套于词典更长词条内（R1-B2：词典命中覆盖处不再
                叠加 ner 项，如词典"张三科技有限公司"内的 ner"张三"）

        返回项 source 统一改写为 "path"，使其在 Web/CLI 检测表格中可区分、可勾选。
        allowed_originals 给定时仅保留命中集合内的原文（Web 确认模式联动）。
        """
        raw = self.detector.detect(name, context or "")
        dict_texts = {
            r.text for r in raw if r.source == "dictionary"
        }
        kept: List[DetectionResult] = []
        for r in raw:
            if allowed_originals is not None and r.text not in allowed_originals:
                continue
            if r.source == "regex":
                if r.text_type == DetectionType.AMOUNT:
                    continue  # 标价/金额样式文件名不脱敏
                if r.text_type == DetectionType.CUSTOM:
                    if (
                        _PHONE_RE.fullmatch(r.text)
                        or _EMAIL_RE.fullmatch(r.text)
                        or _DATE_RE.fullmatch(r.text)
                        or (_ID_RE.fullmatch(r.text) and not r.text.isdigit())
                        or not _LONG_DIGITS_RE.fullmatch(r.text)
                    ):
                        pass  # 保留：手机/邮箱/日期/含校验位X的身份证/其他非纯长数字
                    else:
                        continue  # 16~19 位纯数字
            elif (
                r.source == "ner"
                and any(
                    r.text != d and r.text in d for d in dict_texts
                )
            ):
                # ner 项被词典更长词条包含：词典替换已覆盖该区间，叠加 ner
                # 只会产生多余 token 与噪音，直接滤除（R1-B2）
                continue
            r.source = "path"
            kept.append(r)
        return kept

    def build_new_name(
        self,
        name: str,
        context: str = "",
        kind: str = "file",
        allowed_originals: Optional[Set[str]] = None,
        statuses: frozenset = frozenset({DetectionStatus.AUTO_MASK}),
    ) -> Tuple[str, List[DetectionResult]]:
        """主名 → 脱敏后完整名（文件：新 stem + 原 suffix；目录：新名）。

        内部：sanitize → detect → policy 决策 → 按命中长度降序替换 → 再 sanitize。
        无命中（或命中均未获处置/未过 allowed_originals）时返回 (name, [])。

        statuses（R1-B2）：参与替换的处置集合，默认仅 AUTO_MASK，与内容侧
        默认一致；--all / 确认模式由调用方传 {AUTO, SUGGEST} 对齐。
        """
        stem, suffix = (_split_ext(name) if kind == "file" else (name, ""))
        stem = sanitize_name(stem, reserved_ok=True)  # 原名已是合法磁盘名，防御性清洗
        detections = self.detect_name(stem, context, allowed_originals)
        if not detections:
            return name, []
        detections = self.policy.apply(detections)
        masked = stem
        replaced_any = False
        for r in sorted(detections, key=lambda r: len(r.text), reverse=True):
            if r.status not in statuses:
                continue
            if r.text not in masked:
                continue
            if self.irreversible:
                replacement = "***"
            else:
                replacement = self.token_gen.generate(r.text, r.text_type)
            masked = masked.replace(r.text, replacement)
            replaced_any = True
        if not replaced_any:
            return name, []
        # stem 替换为空串的防御（token 非空，理论不可达）
        if not masked.strip():
            masked = "***" if self.irreversible else "_masked"
        new_full = sanitize_name(masked + suffix)
        return new_full, detections

    # ── 计划/执行分离 ──

    def plan_tree(
        self,
        root: Path,
        allowed_originals: Optional[Set[str]] = None,
        statuses: frozenset = frozenset({DetectionStatus.AUTO_MASK}),
    ) -> List[RenameItem]:
        """遍历 root 生成改名计划（纯计算不落盘）。

        返回条目按 depth 降序排列（同层按路径字典序保证确定性）= 自底向上执行顺序。
        冲突消解在计划阶段完成：taken 集合初始为各目录现存条目名（casefold），
        计划内条目最终名同步登记，双重查重。

        statuses（R1-B2）：参与改名的处置集，默认仅 AUTO；调用方按 --all / 确认
        模式传入对齐。根层 SKIPPED_UNMASKED_DIR 子树整体豁免（R1-B9）。

        rel_new 语义（D3 §6.1）：记录“全部改名完成后的最终相对路径”（父目录链取
        新名）。规划沿 os.walk 自顶向下增量推导各目录的新前缀，执行阶段仍
        自底向上（item.path 基于旧父链，子先父后改 → 规划时刻路径始终有效）。
        """
        root = Path(root)
        items: List[RenameItem] = []
        unchanged = 0
        taken: Dict[Path, Set[str]] = {}
        # 各目录“内容的新相对前缀”（父链取最终新名；未改名祖先取原名）
        new_prefix: Dict[Path, str] = {root: ""}
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            d = Path(dirpath)
            if d == root and SKIPPED_UNMASKED_DIR in dirnames:
                # R1-B9：未勾选（内容未脱敏）文件的隔离区不参与改名计划
                dirnames.remove(SKIPPED_UNMASKED_DIR)
            taken[d] = {e.name.casefold() for e in d.iterdir()}
            depth = len(d.relative_to(root).parts)
            prefix = new_prefix.get(d, d.relative_to(root).as_posix())
            for name in sorted(dirnames):
                child = d / name
                if child.is_symlink():
                    continue  # 不改链接名本身，不穿透
                item, child_prefix = self._plan_entry(
                    child, "dir", depth + 1, root, taken, allowed_originals,
                    prefix, statuses,
                )
                new_prefix[child] = child_prefix
                if item is None:
                    unchanged += 1
                else:
                    items.append(item)
            for name in sorted(filenames):
                child = d / name
                if child.is_symlink():
                    continue
                item, _ = self._plan_entry(
                    child, "file", depth + 1, root, taken, allowed_originals,
                    prefix, statuses,
                )
                if item is None:
                    unchanged += 1
                else:
                    items.append(item)
        items.sort(key=lambda it: (-it.depth, it.path.as_posix()))
        self._last_plan_unchanged = unchanged
        return items

    def _plan_entry(
        self,
        path: Path,
        kind: str,
        depth: int,
        root: Path,
        taken: Dict[Path, Set[str]],
        allowed_originals: Optional[Set[str]],
        parent_prefix: str,
        statuses: frozenset = frozenset({DetectionStatus.AUTO_MASK}),
    ) -> Tuple[Optional[RenameItem], str]:
        """规划单个条目。返回 (RenameItem|None, 该条目的新相对前缀/路径)。"""
        old_name = path.name
        new_name, detections = self.build_new_name(
            old_name, context=str(path), kind=kind,
            allowed_originals=allowed_originals, statuses=statuses,
        )
        if new_name == old_name:
            rel_old = path.relative_to(root).as_posix()
            # 未改名：新前缀 = 父新前缀 + 本名（不能用完整旧链：祖先改名时会断裂）
            child_prefix = f"{parent_prefix}/{old_name}" if parent_prefix else old_name
            return None, child_prefix
        parent = path.parent
        final = unique_name(parent, new_name, taken[parent])
        taken[parent].add(final.casefold())
        suffixed = final != new_name
        rel_new = f"{parent_prefix}/{final}" if parent_prefix else final
        skip_reason = ""
        if len(str(parent / final)) > MAX_PATH_LIMIT:
            # 不截断 token（截断破坏还原），保原名最安全
            skip_reason = "too_long"
        # 仅记录实际参与替换的项（status 在处置集内），避免为未启用项生成未使用 token
        tokens_used: List[str] = []
        categories: List[str] = []
        for r in detections:
            if r.status not in statuses:
                continue
            if r.text_type.value not in categories:
                categories.append(r.text_type.value)
            if not self.irreversible and r.text in old_name:
                token = self.token_gen.generate(r.text, r.text_type)  # 幂等
                if token not in tokens_used:
                    tokens_used.append(token)
        return RenameItem(
            path=path,
            kind=kind,
            old_name=old_name,
            new_name=final,
            depth=depth,
            detections=detections,
            tokens_used=tokens_used,
            categories=categories,
            suffixed=suffixed,
            skip_reason=skip_reason,
            rel_new_final=rel_new,
        ), rel_new

    def apply_plan(self, root: Path, plan: List[RenameItem]) -> PathMaskResult:
        """按 plan 顺序执行 os.rename，逐条记录 PathMapping；单条失败跳过并继续。

        自底向上（plan 已按 depth 降序）：任一节点失败的影响域 = 该节点自身。
        """
        root = Path(root)
        result = PathMaskResult()
        for item in plan:
            rel_old = item.path.relative_to(root).as_posix()
            if item.skip_reason:
                pm = PathMapping(
                    kind=item.kind, old_name=item.old_name, new_name=item.old_name,
                    rel_old=rel_old, rel_new=rel_old, depth=item.depth,
                    categories=item.categories, tokens_used=item.tokens_used,
                    fingerprint="", status=STATUS_SKIPPED, note=item.skip_reason,
                )
                pm.fingerprint = pm.make_fingerprint()
                result.skipped.append(pm)
                result.warnings.append(
                    f"新路径超长，保留原名: {rel_old} (note={item.skip_reason})"
                )
                continue
            status = STATUS_CONFLICT if item.suffixed else STATUS_RENAMED
            note = ""
            try:
                rename_with_retry(
                    win_long(item.path),
                    win_long(item.path.with_name(item.new_name)),
                )
            except PermissionError:
                # WinError 32 sharing violation / 5 access denied：瞬时锁已重试穿透，
                # 仍失败视为持续占用（用户打开文件）→ 跳过并警告
                status, note = STATUS_SKIPPED, "locked"
                result.warnings.append(f"文件被占用，跳过改名: {item.path}")
            except FileExistsError:
                # 异常路径：计划阶段已显式查重，理论不可达
                status, note = STATUS_SKIPPED, "exists"
                result.warnings.append(f"目标已存在，跳过改名: {item.path}")
            except OSError as e:
                status, note = STATUS_SKIPPED, f"os_error:{e}"
                result.warnings.append(f"改名失败，跳过: {item.path} ({e})")
            rel_new = (
                item.rel_new_final
                if (item.rel_new_final and status != STATUS_SKIPPED)
                else rel_old
            )
            pm = PathMapping(
                kind=item.kind, old_name=item.old_name,
                new_name=item.old_name if status == STATUS_SKIPPED else item.new_name,
                rel_old=rel_old, rel_new=rel_new, depth=item.depth,
                categories=item.categories, tokens_used=item.tokens_used,
                fingerprint="", status=status, note=note,
            )
            pm.fingerprint = pm.make_fingerprint()
            if status == STATUS_SKIPPED:
                result.skipped.append(pm)
            else:
                result.renamed.append(pm)
        return result

    def mask_tree(
        self,
        root: Path,
        allowed_originals: Optional[Set[str]] = None,
        statuses: frozenset = frozenset({DetectionStatus.AUTO_MASK}),
    ) -> PathMaskResult:
        """plan_tree + apply_plan 的便捷入口。root 为内容脱敏已完成的输出树。

        root 自身名字不改（批次目录语义是容器）；需脱敏最顶层目录名时把其父目录作为 root。
        statuses（R1-B2）：参与改名的处置集，默认仅 AUTO，与内容侧默认一致。
        """
        root = Path(root)
        plan = self.plan_tree(root, allowed_originals, statuses)
        result = self.apply_plan(root, plan)
        result.unchanged = self._last_plan_unchanged
        # 词库空防御性检查（H5 关联）：仅正则可用时路径检测效果有限
        lexicon_patterns = getattr(self.detector, "_lexicon_patterns", None)
        ner = getattr(self.detector, "ner_engine", None)
        ner_on = bool(ner and ner.is_available())
        if not lexicon_patterns and not ner_on:
            result.warnings.append("词库为空且 NER 未启用，路径检测仅正则规则生效")
        self._accumulated.extend(result.all_mappings)
        return result

    # ── 单文件（Web 用） ──

    def mask_filename(
        self,
        file_path: Path,
        allowed_originals: Optional[Set[str]] = None,
        statuses: frozenset = frozenset({DetectionStatus.AUTO_MASK}),
    ) -> Optional[Path]:
        """对单个已脱敏内容的文件原地改名（同目录）。

        statuses（R1-B2）：参与改名的处置集；Web 确认流程传
        {AUTO, SUGGEST}（勾选即放行）。
        返回新路径；未命中返回原路径；失败返回 None（调用方保留原文件）。
        结果记录于 last_result 并累计进 export_mappings()。
        """
        file_path = Path(file_path)
        self._last_result = PathMaskResult()
        new_name, detections = self.build_new_name(
            file_path.name, context=str(file_path), kind="file",
            allowed_originals=allowed_originals, statuses=statuses,
        )
        if new_name == file_path.name:
            self._last_result.unchanged = 1
            return file_path
        parent = file_path.parent
        taken = {e.name.casefold() for e in parent.iterdir()}
        final = unique_name(parent, new_name, taken)
        target = parent / final
        tokens_used: List[str] = []
        categories: List[str] = []
        for r in detections:
            if r.status not in statuses:
                continue
            if r.text_type.value not in categories:
                categories.append(r.text_type.value)
            if not self.irreversible and r.text in file_path.name:
                token = self.token_gen.generate(r.text, r.text_type)
                if token not in tokens_used:
                    tokens_used.append(token)
        status = STATUS_CONFLICT if final != new_name else STATUS_RENAMED
        note = ""
        try:
            rename_with_retry(win_long(file_path), win_long(target))
        except OSError as e:
            status, note = STATUS_SKIPPED, f"os_error:{e}"
            self._last_result.warnings.append(f"文件名脱敏失败，保留原名: {file_path} ({e})")
        pm = PathMapping(
            kind="file", old_name=file_path.name,
            new_name=file_path.name if status == STATUS_SKIPPED else final,
            rel_old=file_path.name,
            rel_new=file_path.name if status == STATUS_SKIPPED else final,
            depth=1, categories=categories, tokens_used=tokens_used,
            fingerprint="", status=status, note=note,
        )
        pm.fingerprint = pm.make_fingerprint()
        if status == STATUS_SKIPPED:
            self._last_result.skipped.append(pm)
            self._accumulated.append(pm)
            return None
        self._last_result.renamed.append(pm)
        self._accumulated.append(pm)
        return target

    @property
    def last_result(self) -> PathMaskResult:
        """最近一次 mask_filename / mask_tree 的结果对象。"""
        return self._last_result

    # ── 还原 ──

    def unmask_tree(
        self,
        root: Path,
        mappings: List[PathMapping],
        dry_run: bool = False,
    ) -> UnmaskResult:
        """按 depth 降序（先子后父）把 root 下的 rel_new 改回 rel_old。

        dry_run=True 只生成比对报告不改名（_diff_tree 呈现将发生的变化）。
        skipped 记录不参与还原；rel_new 不存在的记入 missing（报告，不猜测）。
        """
        root = Path(root)
        result = UnmaskResult()
        active = [m for m in mappings if m.status in (STATUS_RENAMED, STATUS_CONFLICT)]
        active.sort(key=lambda m: (-m.depth, m.rel_new))
        taken: Dict[Path, Set[str]] = {}
        for m in active:
            cur = root / Path(m.rel_new)
            # 先子后父：还原时父目录尚未还原（仍是新名），目标父取 rel_new 的父
            dst_parent = (root / Path(m.rel_new)).parent
            if not cur.exists():
                result.missing.append(m)
                result.warnings.append(f"还原目标不存在（可能已被改动/删除）: {m.rel_new}")
                continue
            if dst_parent not in taken:
                taken[dst_parent] = {e.name.casefold() for e in dst_parent.iterdir()}
            final = unique_name(dst_parent, m.old_name, taken[dst_parent])
            taken[dst_parent].add(final.casefold())
            if final != m.old_name:
                # R3-C10：还原名被占用加后缀时不再静默，警告透出（CLI/Web
                # 侧已有 warnings 打印通道则自然透出）
                result.warnings.append(
                    f"还原名 {m.old_name} 已被占用，改用 {final}: {m.rel_new}"
                )
            if not dry_run:
                try:
                    rename_with_retry(win_long(cur), win_long(dst_parent / final))
                except OSError as e:
                    result.warnings.append(f"还原失败: {m.rel_new} → {m.old_name} ({e})")
                    continue
            result.restored.append(m)
        extra, _missing = self._diff_tree(root, active)
        result.extra = sorted(extra)
        return result

    def unmask_filename(self, file_path: Path, m: PathMapping) -> Optional[Path]:
        """单文件改名还原（Web restore 用）。失败返回 None。"""
        file_path = Path(file_path)
        parent = file_path.parent
        taken = {e.name.casefold() for e in parent.iterdir()}
        final = unique_name(parent, m.old_name, taken)
        target = parent / final
        try:
            rename_with_retry(win_long(file_path), win_long(target))
        except OSError:
            return None
        return target

    # ── 结构比对 ──

    def _diff_tree(
        self, root: Path, active: List[PathMapping]
    ) -> Tuple[Set[str], Set[str]]:
        """结构比对（D3 §6.2）。

        expect = 把每条 rel_new 前缀映射回 rel_old 后的期望完整树；
        actual = walk(root) 全部相对路径。
        返回 (extra, missing)：extra=还原后仍存在的新名残留；missing=应存在却不存在的原名条目。
        """
        root = Path(root)
        actual: Set[str] = set()
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            d = Path(dirpath)
            for n in dirnames + filenames:
                actual.add((d / n).relative_to(root).as_posix())
        # 深度降序应用前缀替换（先子后父，与执行顺序一致）
        ordered = sorted(active, key=lambda m: -m.depth)
        expect: Set[str] = set()
        for rel in actual:
            mapped = rel
            for m in ordered:
                if mapped == m.rel_new:
                    mapped = m.rel_old
                elif mapped.startswith(m.rel_new + "/"):
                    mapped = m.rel_old + mapped[len(m.rel_new):]
            expect.add(mapped)
        extra = actual - expect
        missing = expect - actual
        return extra, missing

    # ── 序列化 ──

    def export_mappings(self) -> List[dict]:
        """累计的 PathMapping（含 skipped）转 dict 列表。"""
        return [pm.to_dict() for pm in self._accumulated]

    def reset(self) -> None:
        """清空累计映射与最近结果（不重置共享 token_gen）。"""
        self._accumulated.clear()
        self._last_result = PathMaskResult()

    @staticmethod
    def load_paths(mapping_dict: dict) -> List[PathMapping]:
        """从 mapping.json 的 dict 中读取 "paths" 段；缺省返回 []（向后兼容）。"""
        if not isinstance(mapping_dict, dict):
            return []
        paths = mapping_dict.get("paths")
        if not isinstance(paths, list):
            return []
        out: List[PathMapping] = []
        for item in paths:
            if isinstance(item, dict):
                out.append(PathMapping.from_dict(item))
        return out

    @staticmethod
    def merge_into_mapping(mapping_dict: dict, path_dicts: List[dict]) -> dict:
        """把 paths 段并入 save_mapping 产出的 dict（供 cli/pipeline 挂接方一行调用）。

        保留 tokens 段与 metadata 原有字段，仅新增/更新 paths 与 total_path_mappings。
        """
        if not isinstance(mapping_dict, dict):
            mapping_dict = {}
        mapping_dict["paths"] = list(path_dicts)
        metadata = mapping_dict.setdefault("metadata", {})
        if isinstance(metadata, dict):
            metadata["total_path_mappings"] = len(path_dicts)
        return mapping_dict
