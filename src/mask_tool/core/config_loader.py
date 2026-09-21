"""配置加载回退链公共模块（审查 R1-B6：CLI 与 Web 共用同一实现）

从 CLI ``_load_config`` 抽取的四级回退链（H5），返回结构化事件列表供
CLI（rich console）与 Web（st.warning）各自渲染，消除两端行为漂移：

1. 显式 --config：不存在 -> 报错退出（``ConfigError``）
2. CWD/config/default.yaml
3. 内嵌模板 core/templates.DEFAULT_CONFIG_YAML
4. 纯代码默认 MaskConfig(mode)（模板解析失败时的兜底）

每次回退均产生 warn 事件并明示词库路径与存在性；lexicon.yaml 缺失自动从
sample 复制（N2）。
"""

import shutil
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from mask_tool.models.config import MaskConfig

# 事件级别：ok（正常提示）/ info（操作提示）/ warn（警告）/ error（错误）
ConfigEvent = Tuple[str, str]

# 用户词库相对位置（与 config/default.yaml 的 lexicon_path 约定一致）
LEXICON_RELPATH = "config/lexicon.yaml"
SAMPLE_LEXICON_RELPATH = "config/sample_lexicon.yaml"


class ConfigError(Exception):
    """显式指定的配置文件不存在（调用方据此报错退出）。"""


def runtime_anchor_dirs() -> List[Path]:
    """数据文件（词库/白名单）查找锚点，按优先级去重。

    - CWD：用户在当前目录自建 config/ 时优先尊重（开发 cwd=项目根时
      即项目词库）；
    - frozen（PyInstaller）：exe 同级目录（便携式用户数据位）->
      _MEIPASS 打包内置（出厂词库，只读兜底与首次初始化复制源）；
    - 开发模式：源码树项目根（本文件上溯三级），修复桌面/bat 启动时
      cwd 不在项目根导致 ``config/lexicon.yaml`` 相对路径落空的场景。
    """
    dirs: List[Path] = [Path.cwd()]
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        if exe_dir not in dirs:
            dirs.append(exe_dir)
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass and Path(meipass) not in dirs:
            dirs.append(Path(meipass))
    else:
        src_root = Path(__file__).resolve().parents[3]  # src/mask_tool/core -> 项目根
        if src_root not in dirs:
            dirs.append(src_root)
    return dirs


def writable_anchor_dir() -> Path:
    """新建数据文件的落点锚。

    frozen 为 exe 同级目录（用户数据随软件目录走，便携分发友好）；
    开发模式为源码树项目根。均不可写时由调用方捕获 OSError 降级。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parents[3]  # src/mask_tool/core -> 项目根


def find_data_file(relpath: str) -> Optional[Path]:
    """按 runtime_anchor_dirs 顺序查找已存在的数据文件。

    绝对路径存在即返回；否则逐锚点探测，全部不存在返回 None。
    """
    p = Path(relpath)
    if p.is_absolute():
        return p if p.exists() else None
    for anchor in runtime_anchor_dirs():
        candidate = anchor / p
        if candidate.exists():
            return candidate.resolve()
    return None


def resolve_user_lexicon_path() -> Path:
    """用户词库（config/lexicon.yaml）的规范位置。

    Web 侧边栏（手动录入/批量导入/词条展示）与学习写入共用，保证
    全部读写落在同一文件：锚点链上已存在的 lexicon.yaml 直接复用
    （CWD 自建 -> exe 便携目录 -> 源码树）；全部不存在时锚定可写
    锚点，由调用方 mkdir 并按需从内置/sample 初始化。

    frozen 时跳过打包内置（_MEIPASS）锄点：那是出厂副本，用户词库
    固定在 exe 同级（与 finalize_paths 的 _relocate_out_of_bundle
    迁移目标一致）；内置副本仅作为初始化复制源。
    """
    rel = Path(LEXICON_RELPATH)
    meipass = getattr(sys, "_MEIPASS", "")
    frozen = getattr(sys, "frozen", False)
    for anchor in runtime_anchor_dirs():
        if frozen and meipass and anchor == Path(meipass):
            continue
        candidate = anchor / rel
        if candidate.exists():
            return candidate.resolve()
    return (writable_anchor_dir() / rel).resolve()


def resolve_data_path(
    value: str, base_dir: Path,
    extra_anchors: Sequence[Path] = (),
) -> Path:
    """词库等数据路径解析（H5）。

    绝对路径原样；相对路径依次尝试锚点：yaml 所在目录 -> yaml 父目录
    （随包模板约定：config/default.yaml 引用 config/lexicon.yaml）->
    CWD -> extra_anchors（部署形态锚点，仅在无真实配置文件、配置回退
    到内嵌模板时由调用方传入，避免显式配置场景逃逸到源码树），取
    第一个存在的候选；全部不存在时按模板约定锚定（yaml 位于 config/
    目录且路径首段同为 config -> 父目录），extra_anchors 场景锚定
    可写锚点，否则锚定 yaml 所在目录。
    """
    p = Path(value)
    if p.is_absolute():
        return p
    candidates = [base_dir / p, base_dir.parent / p, Path.cwd() / p]
    candidates += [anchor / p for anchor in extra_anchors]
    for c in candidates:
        if c.exists():
            return c.resolve()
    if p.parts and p.parts[0] == base_dir.name:
        return (base_dir.parent / p).resolve()
    if extra_anchors:
        return (writable_anchor_dir() / p).resolve()
    return (base_dir / p).resolve()


def config_from_dict(data: dict, config_dir: str = "") -> MaskConfig:
    """从已解析的 yaml dict 构造 MaskConfig。

    与 models.config.MaskConfig.from_yaml 保持同构（该类不归本模块所有，
    无法新增 from_data 入口）；字段增删时两处需同步。
    """
    from mask_tool.models.config import (
        LLMConfig, NERConfig, OCRConfig, PerformanceConfig, StorageConfig,
        Thresholds,
    )
    data = data or {}
    return MaskConfig(
        mode=data.get("mode", "smart"),
        thresholds=Thresholds(**data.get("thresholds", {})),
        ocr=OCRConfig(**data.get("ocr", {})),
        ner=NERConfig(**data.get("ner", {})),
        llm=LLMConfig(**data.get("llm", {})),
        storage=StorageConfig(**data.get("storage", {})),
        performance=PerformanceConfig(**data.get("performance", {})),
        lexicon_path=data.get("lexicon_path", "config/sample_lexicon.yaml"),
        whitelist_path=data.get("whitelist_path", "config/whitelist.yaml"),
        amount_mode=data.get("amount_mode", "token"),
        config_dir=config_dir,
        categories=data.get("categories", MaskConfig().categories),
    )


def _relocate_out_of_bundle(
    path: Path, label: str, events: List[ConfigEvent],
) -> Path:
    """frozen 下把指向打包内置目录(_MEIPASS)的数据文件迁到 exe 同级。

    目标已存在则直接改指（沿用用户数据）；否则复制过去并 info 提示；
    复制失败（安装到只读目录等）保留内置路径并 warn（升级覆盖
    _internal 会丢失改动）。非 frozen 或路径不在 _MEIPASS 内时原样返回。
    """
    if not getattr(sys, "frozen", False):
        return path
    meipass = getattr(sys, "_MEIPASS", "")
    if not meipass or not path.is_relative_to(Path(meipass)):
        return path
    target = writable_anchor_dir() / path.relative_to(meipass)
    if target.exists():
        return target
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        events.append((
            "info",
            f"{label}：已将打包内置副本初始化为用户数据文件: {target}",
        ))
        return target
    except OSError as exc:
        events.append((
            "warn",
            f"警告: 无法在 exe 目录创建用户{label}({exc})，继续使用内置副本"
            f"（改动在软件升级后可能丢失）: {path}",
        ))
        return path


def finalize_paths(
    cfg: MaskConfig, source_desc: str,
    implicit_config: bool = False,
) -> List[ConfigEvent]:
    """解析词库/白名单为绝对路径并做存在性检查（H5/N2）。

    - lexicon.yaml 缺失且同目录存在 sample_lexicon.yaml -> 自动复制（N2）
    - 解析后仍不存在 -> 警告明示"词库为空"
    - implicit_config=True（配置回退到内嵌模板/纯代码默认，无真实
      default.yaml）：追加部署形态锚点（CWD -> exe 目录 -> 源码树根 ->
      打包内置），修复桌面/bat 启动 cwd 不在项目根时词库落空的问题。
    - frozen 下解析到打包内置(_MEIPASS)词库 -> 迁到 exe 同级可写目录。
    """
    events: List[ConfigEvent] = []
    base_dir = Path(cfg.config_dir) if cfg.config_dir else Path.cwd()
    anchors = runtime_anchor_dirs() if implicit_config else []
    lexicon = resolve_data_path(cfg.lexicon_path, base_dir, anchors)
    whitelist = resolve_data_path(cfg.whitelist_path, base_dir, anchors)
    lexicon = _relocate_out_of_bundle(lexicon, "词库", events)
    whitelist = _relocate_out_of_bundle(whitelist, "白名单", events)

    if not lexicon.exists():
        sample = lexicon.parent / "sample_lexicon.yaml"
        if sample.exists():
            lexicon.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sample, lexicon)
            events.append((
                "info",
                f"词库 {lexicon} 不存在，已从示例词库自动复制创建"
                f"（{sample.name} -> {lexicon.name}）",
            ))
    if not lexicon.exists():
        events.append((
            "warn",
            f"警告: 词库文件不存在，词库为空（正则规则仍生效）: {lexicon}"
            f"（配置来源: {source_desc}）",
        ))
    else:
        events.append(("ok", f"词库: {lexicon} (已加载)"))
    if not whitelist.exists():
        events.append(("warn", f"警告: 白名单文件不存在，白名单为空: {whitelist}"))

    cfg.lexicon_path = str(lexicon)
    cfg.whitelist_path = str(whitelist)
    return events


def load_config(
    config_path: Optional[Path], mode: str,
) -> Tuple[MaskConfig, List[ConfigEvent]]:
    """加载配置（H5 回退链），返回 (配置, 事件列表)。

    显式路径缺失抛 ``ConfigError``（CLI/Web 均应作为错误处理，不静默回退）。
    """
    events: List[ConfigEvent] = []
    implicit_config = False
    if config_path is not None:
        if not Path(config_path).exists():
            raise ConfigError(f"指定的配置文件不存在: {config_path}")
        cfg = MaskConfig.from_yaml(Path(config_path))
        source_desc = str(config_path)
    else:
        cwd_cfg = Path.cwd() / "config" / "default.yaml"
        if cwd_cfg.exists():
            cfg = MaskConfig.from_yaml(cwd_cfg)
            source_desc = str(cwd_cfg)
        else:
            # 无真实配置文件：后续 finalize_paths 启用部署形态锚点
            # （CWD -> exe 目录 -> 源码树根 -> 打包内置），保证桌面/
            # bat 启动 cwd 错位时仍能找到词库
            implicit_config = True
            events.append((
                "warn",
                "警告: 当前目录未找到 config/default.yaml，"
                "回退到内嵌默认配置模板（core/templates.py）",
            ))
            try:
                import yaml

                from mask_tool.core.templates import DEFAULT_CONFIG_YAML

                cfg = config_from_dict(
                    yaml.safe_load(DEFAULT_CONFIG_YAML) or {}, "",
                )
                source_desc = "内嵌模板 core/templates.py"
            except Exception as exc:  # 模板不可用时纯代码默认
                events.append((
                    "warn",
                    f"警告: 内嵌模板加载失败({exc})，回退到纯代码默认配置",
                ))
                cfg = MaskConfig()
                source_desc = "纯代码默认"

    if mode != "smart":
        cfg.mode = mode
    # P3：应用级 LLM 配置合并（app_settings.yaml 的 llm 段 > default.yaml）
    events.extend(_merge_app_llm(cfg))
    events.extend(finalize_paths(cfg, source_desc, implicit_config))
    return cfg, events


def _merge_app_llm(cfg) -> list:
    """把 app_settings.yaml 的 llm 段合并进 cfg.llm（用户显式设置优先）。

    设置弹窗「模型配置」维护；Web 侧栏开关与 CLI --llm/--no-llm 最终
    覆盖 enabled（见 ui/service.py 与 cli）。延迟 import 防循环依赖
    （app_settings 模块级引用本模块的 find_data_file/writable_anchor_dir）。
    返回 events 列表（正常为空，合并失败一条 warn）。
    """
    try:
        from dataclasses import fields as _dc_fields
        from mask_tool.core.app_settings import get_llm_settings
        saved = get_llm_settings()
        if saved:
            valid = {f.name for f in _dc_fields(cfg.llm)}
            for k, v in saved.items():
                if k in valid:
                    setattr(cfg.llm, k, v)
        return []
    except Exception as exc:  # 设置文件损坏等：保持 YAML 配置，不阻断
        return [("warn", f"警告: 应用级 LLM 配置合并失败({exc})，使用配置文件值")]
