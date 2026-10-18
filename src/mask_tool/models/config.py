"""配置数据模型"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union


def _coerce_bool(v) -> bool:
    """把 YAML 手写引号布尔（"false"/"true"/"0"/"1"）归一化为真布尔。

    YAML 里 trusted_endpoint: "false"（带引号）会被解析成字符串，
    bool("false") == True 会让隐私门禁反向失效，必须在构造时纠正。
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


@dataclass
class Thresholds:
    """置信度阈值配置"""
    auto_mask: float = 0.85
    suggest_mask: float = 0.6


@dataclass
class OCRConfig:
    """OCR配置"""
    enabled: bool = False
    engine: str = "paddleocr"


@dataclass
class NERConfig:
    """NER配置"""
    enabled: bool = False
    engine: str = "hanlp"


@dataclass
class LLMConfig:
    """LLM 增强检测配置（P1：OpenAI 兼容内网端点，默认关闭）

    铁律：``enabled=False`` 时全链路行为与无 LLM 时完全一致。
    端点兼容 Ollama / vLLM / Xinference / LMDeploy / One-API 类网关。
    """
    enabled: bool = False
    role: str = "adjudicator"          # adjudicator / detector / both（P2 起后两者生效）
    base_url: str = ""                 # 如 http://localhost:11434/v1
    model: str = ""                    # 如 qwen3:8b / 内网服务注册名
    api_key: str = ""                  # 内网通常留空；空时读环境变量 MASKTOOL_LLM_API_KEY
    trusted_endpoint: bool = False     # 端点信任标记：False（默认）时，外部（非本机/内网）
                                       # 端点在 CLI/评测路径记录隐私告警；Web UI 无门禁
    batch_size: int = 20               # 每请求合并候选条数
    max_concurrency: int = 2           # 并发上限（P1 串行，字段预留）
    timeout_seconds: int = 30
    budget_max_calls: int = 200        # 单次运行最大调用数（超出跳过后续）
    wall_clock_budget_seconds: int = 180  # LLM 增强总时长预算（秒），0=不限制
    cache: bool = True                 # 同 (text, source, type) 判定复用
    last_test: Optional[dict] = None   # UI 层持久化连通状态 {ok,msg,at,sig}（不参与检测链路）

    def __post_init__(self):
        # 布尔字段归一化：防御 YAML 手写引号布尔导致的 fail-open。
        self.enabled = _coerce_bool(self.enabled)
        self.trusted_endpoint = _coerce_bool(self.trusted_endpoint)


@dataclass
class StorageConfig:
    """存储配置"""
    mapping_format: str = "json"
    encrypt_mapping: bool = False


@dataclass
class PerformanceConfig:
    """性能配置"""
    workers: int = 4
    max_file_mb: int = 500


@dataclass
class MaskConfig:
    """全局配置"""
    mode: str = "smart"                          # strict / smart / aggressive
    mask_style: str = "token"                    # 替换串风格: token（[COMPANY_001]）/ alias（汤姆/A集团）
    alias_lexicon_path: str = "config/alias_lexicon.yaml"   # 伪装词池配置（alias 风格时生效，缺失用内置默认）
    thresholds: Thresholds = field(default_factory=Thresholds)
    ocr: OCRConfig = field(default_factory=OCRConfig)
    ner: NERConfig = field(default_factory=NERConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    performance: PerformanceConfig = field(default_factory=PerformanceConfig)
    lexicon_path: str = "config/sample_lexicon.yaml"
    whitelist_path: str = "config/whitelist.yaml"
    amount_mode: str = "token"                  # 金额脱敏模式: token（可逆）/ fuzzy（模糊化）/ fixed（***）
    config_dir: str = ""                         # 配置文件所在目录（H5：由加载方填充，用于解析相对路径）
    categories: list = field(default_factory=lambda: [
        "company", "government", "person", "project",
        "subject", "location", "amount", "custom",
    ])

    @classmethod
    def from_yaml(cls, path: Path) -> "MaskConfig":
        """从YAML文件加载配置"""
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        thresholds = Thresholds(**data.get("thresholds", {}))
        ocr = OCRConfig(**data.get("ocr", {}))
        ner = NERConfig(**data.get("ner", {}))
        storage = StorageConfig(**data.get("storage", {}))
        performance = PerformanceConfig(**data.get("performance", {}))

        return cls(
            mode=data.get("mode", "smart"),
            mask_style=data.get("mask_style", "token"),
            alias_lexicon_path=data.get(
                "alias_lexicon_path", "config/alias_lexicon.yaml"),
            thresholds=thresholds,
            ocr=ocr,
            ner=ner,
            llm=LLMConfig(**data.get("llm", {})),
            storage=storage,
            performance=performance,
            lexicon_path=data.get("lexicon_path", "config/sample_lexicon.yaml"),
            whitelist_path=data.get("whitelist_path", "config/whitelist.yaml"),
            amount_mode=data.get("amount_mode", "token"),
            config_dir=str(path.parent),
            categories=data.get("categories", cls().categories),
        )
