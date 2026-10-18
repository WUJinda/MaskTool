"""内嵌配置模板（H5/N2：`config` 命令与配置回退的兜底来源）

同步要求：本文件内容取材自仓库根 ``config/default.yaml``、
``config/sample_lexicon.yaml``、``config/whitelist.yaml``。
修改仓库根模板时必须同步更新本文件，反之亦然；
新增配置字段以 models/config.py 的定义为准。

与仓库根的差异（有意的增强，回写仓库根时请保留）：
- DEFAULT_CONFIG_YAML 增加 ``amount_mode``、``mask_style``、``alias_lexicon_path`` 键
  并补全 mode 注释中的 focused 模式；
- ``ner.enabled``：模板 true / 仓库根 false（模板面向桌面首次初始化，默认启用 NER）；
- 模板多 ``llm.wall_clock_budget_seconds: 180`` 一行（仓库根未同步）。

除上述清单外，其余内容与仓库根文件字节一致；新增配置字段时须同步两处并将
差异补入本清单。

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
  role: adjudicator            # adjudicator（复核）/ detector（AI 增量检测）/ both
  base_url: ""                 # 如 http://localhost:11434/v1（内网端点）
  model: ""                    # 如 qwen3:8b / 内网服务注册名
  api_key: ""                  # 内网通常留空；空时读环境变量 MASKTOOL_LLM_API_KEY
  trusted_endpoint: false      # 端点信任标记：false 时外部端点在 CLI/评测路径记录隐私告警（Web UI 无门禁）
  batch_size: 20               # 每请求合并候选条数
  max_concurrency: 2           # 并发上限（预留）
  timeout_seconds: 30
  budget_max_calls: 200        # 单次运行最大调用数（超出跳过后续）
  wall_clock_budget_seconds: 180  # LLM 增强总时长预算（秒），0=不限制
  cache: true                  # 同实体判定复用（跨段去重）

# 存储配置
storage:
  mapping_format: json  # json / sqlite
  encrypt_mapping: false

# 金额脱敏模式: token（可逆）/ fuzzy（模糊化，如 1.2亿元->1亿+）/ fixed（***）
amount_mode: token

# 替换串风格: token（[COMPANY_001] 占位符，默认）/ alias（汤姆/A集团式伪装词）
# alias：实体类（人名/地名/公司/机构/项目/词库与手动词）替换为自然伪装词，
#       文档观感更自然；金额/手机号/证件号等仍用 token 样式；
#       还原逻辑与 token 风格完全一致（mapping.json 可逆还原）
# 优先级：CLI --style / 界面选择 > 本配置文件
mask_style: token

# 伪装词池配置（mask_style: alias 时生效；文件缺失时用内置默认池）
# 可复制内置默认并自定义：人名池顺序取用，其余类别为字母 × 后缀模板展开
alias_lexicon_path: config/alias_lexicon.yaml

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

# alias_lexicon.yaml 模板（与仓库根 config/alias_lexicon.yaml 字节一致）
ALIAS_LEXICON_YAML = """\
# 伪装词池配置（mask_style: alias 时生效）
# 说明：
#   - person（人名池）：顺序取用，池内词彼此不应互为前缀
#   - *_suffixes（后缀模板）：按 字母 × 后缀 展开（A市 B市 … Z市 A省 …）
#   - 生成时自动避让原文中已存在的词（防还原撞词）、过滤 Windows 文件名非法字符
#   - 词池耗尽时自动回退 [PERSON_001] 式占位符（还原逻辑不变）
#   - 本文件可自由增删；删除本文件则回退内置默认池
person:
  - 汤姆
  - 杰瑞
  - 张三
  - 李四
  - 王五
  - 赵六
  - 钱七
  - 孙八
  - 周九
  - 吴十
  - 小明
  - 小红
  - 小刚
  - 小丽
  - 阿福
  - 阿贵
  - 大壮
  - 来福
  - 建国
  - 建军
  - 爱国
  - 志强
  - 伟民
  - 秀英
  - 桂芳
  - 春花
  - 约翰
  - 大卫
  - 玛丽
  - 琳达
  - 杰克
  - 露西
  - 托尼
  - 安妮
  - 老王
  - 老李
  - 老张
  - 小刘
  - 小陈
  - 小杨
  - 阿明
  - 阿强
  - 阿芳
  - 秋月
  - 冬梅
  - 望舒
  - 云帆
  - 知远

location_suffixes:
  - 市
  - 省
  - 县
  - 区

company_suffixes:
  - 集团
  - 公司
  - 企业
  - 厂

government_suffixes:
  - 单位
  - 局
  - 委
  - 部
  - 厅
  - 办

project_suffixes:
  - 项目
  - 工程
  - 标的
  - 课题

custom_pool:
  - 甲
  - 乙
  - 丙
  - 丁
  - 戊
  - 己
  - 庚
  - 辛
  - 壬
  - 癸
  - 子
  - 丑
  - 寅
  - 卯
  - 辰
  - 巳
  - 午
  - 未
  - 申
  - 酉
  - 戌
  - 亥
"""
