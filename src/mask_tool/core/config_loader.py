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
from pathlib import Path
from typing import List, Optional, Tuple

from mask_tool.models.config import MaskConfig

# 事件级别：ok（正常提示）/ info（操作提示）/ warn（警告）/ error（错误）
ConfigEvent = Tuple[str, str]


class ConfigError(Exception):
    """显式指定的配置文件不存在（调用方据此报错退出）。"""


def resolve_data_path(value: str, base_dir: Path) -> Path:
    """词库等数据路径解析（H5）。

    绝对路径原样；相对路径依次尝试锚点：yaml 所在目录 -> yaml 父目录
    （随包模板约定：config/default.yaml 引用 config/lexicon.yaml）->
    CWD，取第一个存在的候选；全部不存在时按模板约定锚定（yaml 位于
    config/ 目录且路径首段同为 config -> 父目录），否则锚定 yaml 所在目录。
    """
    p = Path(value)
    if p.is_absolute():
        return p
    candidates = [base_dir / p, base_dir.parent / p, Path.cwd() / p]
    for c in candidates:
        if c.exists():
            return c.resolve()
    if p.parts and p.parts[0] == base_dir.name:
        return (base_dir.parent / p).resolve()
    return (base_dir / p).resolve()


def config_from_dict(data: dict, config_dir: str = "") -> MaskConfig:
    """从已解析的 yaml dict 构造 MaskConfig。

    与 models.config.MaskConfig.from_yaml 保持同构（该类不归本模块所有，
    无法新增 from_data 入口）；字段增删时两处需同步。
    """
    from mask_tool.models.config import (
        NERConfig, OCRConfig, PerformanceConfig, StorageConfig, Thresholds,
    )
    data = data or {}
    return MaskConfig(
        mode=data.get("mode", "smart"),
        thresholds=Thresholds(**data.get("thresholds", {})),
        ocr=OCRConfig(**data.get("ocr", {})),
        ner=NERConfig(**data.get("ner", {})),
        storage=StorageConfig(**data.get("storage", {})),
        performance=PerformanceConfig(**data.get("performance", {})),
        lexicon_path=data.get("lexicon_path", "config/sample_lexicon.yaml"),
        whitelist_path=data.get("whitelist_path", "config/whitelist.yaml"),
        amount_mode=data.get("amount_mode", "token"),
        config_dir=config_dir,
        categories=data.get("categories", MaskConfig().categories),
    )


def finalize_paths(cfg: MaskConfig, source_desc: str) -> List[ConfigEvent]:
    """解析词库/白名单为绝对路径并做存在性检查（H5/N2）。

    - lexicon.yaml 缺失且同目录存在 sample_lexicon.yaml -> 自动复制（N2）
    - 解析后仍不存在 -> 警告明示"词库为空"
    """
    events: List[ConfigEvent] = []
    base_dir = Path(cfg.config_dir) if cfg.config_dir else Path.cwd()
    lexicon = resolve_data_path(cfg.lexicon_path, base_dir)
    whitelist = resolve_data_path(cfg.whitelist_path, base_dir)

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
    events.extend(finalize_paths(cfg, source_desc))
    return cfg, events
