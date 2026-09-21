"""内嵌配置模板（H5/N2：`config` 命令与配置回退的兜底来源）

同步要求：本文件内容取材自仓库根 ``config/default.yaml``、
``config/sample_lexicon.yaml``、``config/whitelist.yaml``。
修改仓库根模板时必须同步更新本文件，反之亦然；
新增配置字段以 models/config.py 的定义为准。

与仓库根的差异（有意的增强，回写仓库根时请保留）：
- DEFAULT_CONFIG_YAML 增加 ``amount_mode`` 键并补全 mode 注释中的 focused 模式；
  其余内容与仓库根文件字节一致。

历史背景：原实现回退路径 ``Path(__file__).parent.parent.parent / "config"``
解析到不存在的 ``src/config/``，且打包安装后仓库根 config/ 不可达，
故将模板内嵌为字符串常量，彻底摆脱对磁盘目录的存在性依赖。
"""

# default.yaml 模板（与仓库根 config/default.yaml 同步，差异见模块 docstring）
DEFAULT_CONFIG_YAML = """\
# 脱敏模式: focused / strict / smart / aggressive
mode: smart

# 置信度阈值
thresholds:
  auto_mask: 0.85      # >= 此值自动脱敏
  suggest_mask: 0.6    # >= 此值建议脱敏
  # < 0.6 仅提示

# OCR配置（MVP阶段暂不启用）
ocr:
  enabled: false
  engine: paddleocr    # paddleocr / tesseract

# NER配置
ner:
  enabled: true            # 启用NER智能识别
  engine: jieba            # jieba（轻量）/ hanlp（高精度，需额外安装）

# LLM 增强检测（P1：内网 OpenAI 兼容端点，默认关闭）
# 兼容 Ollama / vLLM / Xinference / LMDeploy / One-API 类网关；
# 仅增强 smart/aggressive 模式（focused 不接入）；关闭时行为与无此段一致
llm:
  enabled: false
  role: adjudicator            # adjudicator（复核）/ detector / both（P2 起后两者生效）
  base_url: ""                 # 如 http://localhost:11434/v1（内网端点）
  model: ""                    # 如 qwen3:8b / 内网服务注册名
  api_key: ""                  # 内网通常留空；空时读环境变量 MASKTOOL_LLM_API_KEY
  trusted_endpoint: true       # 内网端点信任标记（不弹隐私确认）
  batch_size: 20               # 每请求合并候选条数
  max_concurrency: 2           # 并发上限（预留）
  timeout_seconds: 30
  budget_max_calls: 200        # 单次运行最大调用数（超出跳过后续）
  cache: true                  # 同实体判定复用（跨段去重）

# 存储配置
storage:
  mapping_format: json  # json / sqlite
  encrypt_mapping: false

# 金额脱敏模式: token（可逆）/ fuzzy（模糊化，如 1.2亿元->1亿+）/ fixed（***）
amount_mode: token

# 词库路径
# sample_lexicon.yaml 是随项目分发的通用示例词库
# lexicon.yaml 是用户自定义词库（不会提交到GitHub）
# 如果 lexicon.yaml 不存在，自动从 sample_lexicon.yaml 复制创建
lexicon_path: config/lexicon.yaml

# 白名单路径
whitelist_path: config/whitelist.yaml

# 性能配置
performance:
  workers: 4
  max_file_mb: 500

# 脱敏类别（按优先级排序）
categories:
  - company
  - government
  - person
  - project
  - subject
  - location
  - amount
  - custom
"""

# sample_lexicon.yaml 模板（与仓库根 config/sample_lexicon.yaml 字节一致）
SAMPLE_LEXICON_YAML = """\
# 敏感词词库配置
# 按类别管理敏感词，支持精确匹配
# 用户可根据实际业务需求增删词条
# 置信度: 词库匹配 = 0.95（focused模式下仅脱敏这些词）
#
# 使用说明：
#   1. 复制本文件为 lexicon.yaml（或自定义名称）
#   2. 删除示例词条，添加你自己的业务词条
#   3. 在 default.yaml 中将 lexicon_path 指向你的词库文件
#   4. 也可通过 Web 界面"学习新词"功能自动添加

company:
  # 示例：将以下替换为你业务中涉及的公司/机构名称
  - 某某建设集团有限公司
  - 某某投资有限公司
  - 某某科技有限公司

government:
  # 示例：将以下替换为你业务中涉及的政府/监管机构
  - 某某省发展和改革委员会
  - 某某市住房和城乡建设局

person:
  # 示例：将以下替换为你业务中涉及的人名
  - 张三
  - 李四

project:
  # 示例：将以下替换为你业务中涉及的项目名称
  - 某某新区基础设施建设项目
  - 某某道路改造工程

subject:
  # 示例：将以下替换为你业务中涉及的标的物/资产名称
  - 某某商业综合体
  - 某某住宅小区

location:
  # 示例：将以下替换为你业务中涉及的地名
  - 某某省某某市
  - 某某区某某路

custom:
  # 用户自定义关键词（如内部编号、代码等）
  - 内部编号XXXX-001
"""

# whitelist.yaml 模板（与仓库根 config/whitelist.yaml 字节一致）
WHITELIST_YAML = """\
# 白名单 - 这些词不会被脱敏
# 适用于常见但非敏感的词汇

whitelist:
  - 有限公司
  - 有限责任公司
  - 股份有限公司
  - 集团
  - 项目
  - 合同
  - 协议
"""
