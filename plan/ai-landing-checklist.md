# mask-tool AI 增强：问题修复与落地清单

> 生成日期：2026-10-15
> 范围：`src/mask_tool/core/llm/*`、`src/mask_tool/web/ui/*`、`config/*.yaml`
> 依据：对现有实现的逐文件核查（配置内容、UI 接线、策略阈值、提交历史）

## 一、核查结论摘要

现有 AI 增强的**技术实现是合格的**：装饰器零侵入、LLM 异常永不阻断脱敏、权限最小化（只改 confidence/type/reason，禁止改原文）、幻觉防护（回包须逐字在原文）、调用数 + wall-clock 双预算、连续 3 错熔断、结构化输出三级降级链、判定理由可解释入表。

问题集中在**定位层**与**三处实现层缺陷**。以下缺陷均可复现。

| 编号 | 缺陷 | 证据位置 | 后果 |
| --- | --- | --- | --- |
| **D1** | `trusted_endpoint` 是**死配置**：字段与“不弹隐私确认”的注释存在，但全代码库无任何逻辑引用，设置弹窗也不暴露该字段 | `models/config.py:41`、`core/templates.py:47`、`config/default.yaml`；全库 grep 仅命中定义处 | 承诺的“非信任端点弹隐私确认”从未实现。送审**未脱敏原文**到任意端点（含公网）都没有门禁，只有文案提示 |
| **D2** | AI 检出项在确认页**默认勾选**，与设计文档“默认流程不自动替换，人工确认勾选后生效”**矛盾** | `adjudicator.py` `_DETECT_CAP=0.80` → smart 模式落 `SUGGEST_MASK`（阈值 0.6）；`pages/masking.py:474-478` 默认勾选 AUTO + SUGGEST | 弱模型/幻觉检出的实体被**默认纳入脱敏**，用户不主动取消就会误脱，污染交给外部的交付物 |
| **D3** | 通用词消误报靠 **LLM 推理**，而非白名单过滤 | prompt 硬编码“甲方/乙方/监理/总包/分包/标段/图纸/附件/竣工/验收”；`config/whitelist.yaml` 仅 7 个通用词，不含以上任一 | 每份含这些词的文档都在调用 LLM 去判定一个**确定性事实**。本可用配置零成本解决 |
| **D4** | 无任何检出质量评估基准 | `tests/` 无评测集与评测脚本；`LLMRunStats` 只统计调用次数，不测准确率 | “AI 有没有用、有多大用”无法量化，prompt 调优与模型选型只能盲调 |
| **D5** | AI 能力未沉淀为规则资产 | 词库反馈（`_merge_words_into_lexicon`）与白名单反馈（`_merge_words_into_whitelist`）**闭环均已存在**，但没有面向 `source="llm"` 的主动入口 | AI 每次运行都要重新发现同样的实体，端点必须常开，隐私与延迟代价常态化 |

## 二、任务清单

### 阶段 0：配置纠正（零风险，约 0.5 天）

#### T0.1 补齐白名单通用词 — 必要性：必做
- **文件**：`config/whitelist.yaml`
- **改动**：在 `whitelist:` 下追加（建议值，请按贵司文档实际复核）：
  `甲方、乙方、监理、总包、分包、标段、图纸、附件、竣工、验收、招标、投标、承包、发包`
- **说明**：白名单为**精确匹配**（`core/detector.py:84`、`detector.py:152`），仅过滤这些独立词，不影响“第一标段”这类复合实体
- **验收**：构造含以上词的样本，`mask-tool inspect` 输出中这些词零命中（修复前会命中后再被 AI drop）
- **验证**：对比修复前后 `report.json` 的 `llm.calls`，应明显下降

#### T0.2 提示词通用词与白名单一致性 — 必要性：建议
- **文件**：`src/mask_tool/core/llm/adjudicator.py`（`_SYSTEM_PROMPT` / `_DETECT_SYSTEM_PROMPT`）
- **改动**：加注释指向 `config/whitelist.yaml`，说明“已入白名单的词不会到达模型；此处列表仅作模式示例（few-shot）”
- **说明**：prompt 中的列表是**教学示例**，whitelist 是**过滤器**，职责不同、无需删除。此条只为防后续维护漂移

### 阶段 1：缺陷修复（必要性高，约 1.5 天）

#### T1.1 实现端点隐私门禁（修 D1）— 必要性：必做
- **现状**：`trusted_endpoint` 全库无逻辑引用；设置弹窗不暴露；UI 仅文案提示“建议内网或本机部署”
- **改动**：
  - `web/components/settings_dialog/component.js` + `web/ui/settings_dialog.py`：新增“信任此端点（不弹隐私确认）”开关，随 `save_llm` 一并保存 `trusted_endpoint`
  - 新增判定函数（建议放 `core/llm/` 或 `core/app_settings.py`）：`is_loopback_or_private(base_url)`，判断 host 是否 loopback / RFC1918
  - `core/pipeline.py:117` `_wrap_with_llm` 或 `web/ui/sidebar.py` 启用开关处：`trusted_endpoint=false` 且端点非内网时，启用前弹一次确认并记日志
  - 可选（更硬）：新增“仅允许内网端点”配置项，非内网直接拒绝启用
- **验收**：配置公网端点（如智谱）+ 关闭信任 → 启用 AI 时出现隐私确认；配置 localhost → 不弹
- **测试**：`tests/test_llm_ui.py` 补断言

#### T1.2 修正 AI 检出项默认勾选（修 D2）— 必要性：必做
- **现状**：AI 检出（`source="llm"`、conf ≤ 0.80）落 SUGGEST 档，确认页默认勾选 → 默认会被脱敏
- **改动**：二选一，**推荐 A**
  - **A（推荐）**：落实设计意图——`pages/masking.py:474-478` 默认勾选逻辑对 `source=="llm"` 且 `status==SUGGEST_MASK` 的项默认 `False`，用户需主动勾选
  - **B**：保留现状行为，修正 `adjudicator.py` `detect_new` 的 docstring 与相关文案，明确“AI 检出在 smart 模式默认纳入脱敏”
- **验收**：A 方案单测断言 AI 检出项 `user_selections[i] is False`，人工勾选后可正常脱敏
- **理由**：脱敏文档会被交给外部，AI 误脱会同时破坏交付物语义；人工确认成本远低于误脱代价

#### T1.3 修正实现与文档不一致处 — 必要性：建议
- `core/llm/adjudicator.py`：`detect_new` docstring 中“默认流程不自动替换”与 T1.2 结论对齐
- `web/ui/sidebar.py:120` 帮助文案、`detect_new` 说明同步更新

### 阶段 2：把 AI 能力沉淀为规则资产（必要性最高，约 2.5 天）

> 这是让 AI 从“每次运行的负担”变成“一次性资产”的关键阶段。闭环基础设施**已存在**，本阶段是接线与呈现。

#### T2.1 AI 检出项 → 词库 一键固化 — 必要性：必做
- **现状**：`_build_learn_candidates`（`pages/masking.py:113`）已通用支持所有 source，`SOURCE_LABELS["llm"]="✨ AI"` 已就位；但无“只看 AI 检出”的入口，AI 项与规则项混在一起
- **改动**：
  - 结果表新增筛选/标记：“仅看 ✨ AI 检出”
  - 结果页新增按钮“把 AI 检出项加入词库” → 复用 `_learn_to_lexicon_dialog`（`pages/masking.py:161`），初始候选仅含 `source=="llm"` 的词
  - learn 对话框对 AI 来源词加 ✨ 徽标并默认勾选
- **验收**：AI 跑一轮 → 一键固化 → **关闭 AI** 重跑同一文档，这些词由纯词典命中（`source=="dictionary"`）

#### T2.2 AI 剔除项 → 白名单 定向固化 — 必要性：强烈建议
- **现状**：R9“对未勾选的敏感词进行永久排除”（`pages/masking.py:743-775`）面向**所有**未勾选项，噪声大（会把用户没来得及看的敏感词也排除）
- **改动**：新增“仅把 AI 建议剔除的词加入白名单”按钮，输入集限定为 `llm_reason` 含“建议剔除”且 status 为 HINT_ONLY 的项
- **验收**：AI drop 的词进白名单后下一轮不再出现；未勾选的其它敏感词不受影响
- **理由**：AI 的 drop 判断是“该词是误报”的**高质量信号**，正好喂给白名单这种确定性机制；比“全部未勾选”精确得多

#### T2.3 记录 AI 建议采纳率 — 必要性：建议
- **文件**：`core/llm/adjudicator.py`（`LLMRunStats`）、`models/report.py`
- **改动**：运行结束统计“AI 检出被确认 / AI 剔除被接受 / AI 复核被推翻”的计数，写入 `report.json` 的 `llm` 段
- **用途**：为 T3 评估集提供真实分布，也是判断“AI 增强必要性”的长期数据

### 阶段 3：建立可量化基准（必要，约 2 天，语料成本另计）

#### T3.1 评估集与评测脚本 — 必要性：必做
- **新增** `tests/eval/`：
  - `cases/*.yaml`：20~30 段代表性语料 + 人工标注的敏感实体。可从历史批次 `mapping.json` 去标识化生成，无隐私泄露
  - `run_eval.py`：对同一语料跑各 role（纯规则 / adjudicator / detector / both），输出 P/R/F1 与调用数、耗时
- **验收**：产出基线表写入 `docs/`；后续任何 prompt / 模型改动先跑它
- **说明**：标注语料是人工成本，建议先做 20 段最小集；`mapping.json` 是现成的高质量标注源

### 阶段 4：可选增强（按需，各 3~5 天）

#### T4.1 脱敏后语义残留审计 — 必要性：选做
- 独立后处理：读替换稿 → LLM 通读 → 输出“可疑语义残留”报告，**不自动改文件**
- 价值：直接服务“交给外部 AI”的产品终点；审计的是已脱敏文本，隐私边界不破
- 入口：脱敏完成页新增按钮 + 报告展示（复用 `core/llm/client.py` `chat_json`）

#### T4.2 漏检反馈 → 相似扩充 — 必要性：选做
- 用户在确认页确认某漏检词后，以其为 few-shot 锚点让 LLM 全文找同类指代，批量建议
- 复用 `detect_new`，需新建“确认页内即时调用”入口（注意 pipeline 生命周期外的 adjudicator 复用）

## 三、优先级总表

| 编号 | 任务 | 必要性 | 工作量 | 风险 |
| --- | --- | --- | --- | --- |
| T0.1 | 补齐白名单通用词 | 必做 | 0.2d | 极低 |
| T1.2 | AI 检出项默认不勾选 | 必做 | 0.5d | 低 |
| T1.1 | 端点隐私门禁 | 必做 | 1d | 低 |
| T2.1 | AI 检出 → 词库固化 | 必做 | 1d | 低 |
| T3.1 | 评估集与评测脚本 | 必做 | 2d+ | 低 |
| T2.2 | AI 剔除 → 白名单定向 | 强烈建议 | 0.5d | 低 |
| T2.3 | AI 采纳率统计 | 建议 | 0.5d | 极低 |
| T0.2 / T1.3 | 文档与注释一致性 | 建议 | 0.3d | 极低 |
| T4.1 | 语义残留审计 | 选做 | 3~5d | 中 |
| T4.2 | 相似扩充 | 选做 | 3~5d | 中 |

### 建议执行顺序

1. **第一批（约 1 天）**：T0.1 + T1.2 —— 立即消除“本可不花 LLM 却花了”与“误脱风险”两个最实的问题
2. **第二批（约 2 天）**：T1.1 + T2.1 —— 补上隐私门禁，并把 AI 输出变成词库资产
3. **第三批（约 3 天）**：T3.1 + T2.2 + T2.3 —— 建立可量化基准与反馈数据
4. **第四批（按需）**：T4.1 / T4.2

## 四、总体定位建议（非代码）

- 把 AI 增强的定位从 **“每次运行的常驻检测增强”** 改为 **“词库/白名单冷启动加速器 + 长尾补全器”**：用 AI 发现新模式 → 用户确认 → 沉淀进规则 → 常规文档下次纯规则即可覆盖。
- 配置层建议把默认 `role` 从 `adjudicator`（复核，价值大半可被 T0.1 白名单替代）改为 `detector`（补漏检，唯一不可被规则替代的能力）；或在文档中明确推荐 `detector`。
- 冷启动复用同一端点即可，无需为了“每次运行都开着 AI”承担隐私与延迟代价。

## 五、明确不在本清单内

- **文档画像 / 跨段别名归并**：需改 mapping 结构语义（多表面形式 → 同 Token），存在数据兼容风险，等 T3 量化后再评估
- **表格列级语义检测**：收益取决于文档中表格占比，先观察使用数据
- **AI 直接决定自动脱敏**：现有“建议而非决策”的边界不应放松


---

## 六、执行状态（2026-10-16）

**已完成：T0.1、T0.2、T1.1、T1.2、T1.3、T2.1、T2.2、T2.3、T3.1**（T4.x 未做，按计划留待按需）。

### 执行中新发现的缺陷（已一并修复）

- **D6：白名单对 AI 增量检测不生效。** `detector_wrapper.detect` 的排除集原先只有
  规则结果，白名单词（已判定非敏感）仍会被 `detect_new` 重新检出。已把白名单并入
  排除集（`detector_wrapper.py`），使 `config/whitelist.yaml` 成为检测面的统一过滤器。
- **D7：评测脚本首次运行暴露标注侧问题。** YAML 把纯数字标注解析为 int，
  导致同一实体同时计入 FP 与 FN。已在 `run_eval.py` 侧做 `str()` 归一（并对语料加引号）。

### 验收记录

| 项 | 验证方式 | 结果 |
| --- | --- | --- |
| T0.1 | 构造含 10 个通用词的文本跑检测器 | 全部零命中（词典/NER/正则三路均已过滤） |
| T1.2 | 单测 `test_default_selection_ai_suggest_not_checked` | 通过（AI 建议档默认不勾选；aggressive 升自动档仍勾选） |
| T1.1 | AppTest 实跑侧栏 | 外部端点：隐私确认框出现 + AI 开关禁用；内网端点：无确认、开关可用 |
| T1.1 | 单测 `test_endpoint_loopback_or_private` / `test_save_llm_persists_trusted_endpoint` | 通过 |
| T2.1 | 单测 `TestViewAiDetected` + 全量回归 | 通过 |
| T2.2 | 单测 `test_view_ai_dropped` | 通过 |
| T2.3 | 单测 `test_summarize_ai_acceptance` | 通过 |
| T3.1 | `python tests/eval/run_eval.py` | 纯规则基线 P=0.778 / R=0.933 / F1=0.848 |
| 全量 | `pytest` | 515 passed, 6 skipped, 0 failed（复跑一致） |

### 新增/修改文件

**新增**：`src/mask_tool/core/llm/endpoint.py`、`tests/eval/{lexicon,whitelist,cases}.yaml`、
`tests/eval/run_eval.py`、`tests/eval/README.md`、`tests/test_eval_baseline.py`。

**修改**：`config/whitelist.yaml`、`config/default.yaml`、`core/templates.py`、
`models/config.py`、`core/pipeline.py`、`core/llm/detector_wrapper.py`、
`core/llm/adjudicator.py`、`web/ui/sidebar.py`、`web/ui/service.py`、
`web/ui/settings_dialog.py`、`web/components/settings_dialog/component.js`、
`web/ui/pages/masking.py`、`tests/{test_llm_ui,test_web_learn_lexicon,test_llm_detect_new}.py`。

### 遗留（需你决策，未擅自改动）

- **默认 role 是否由 `adjudicator` 改为 `detector`**：清单§四建议改，但会改变默认
  行为，留待你确认（当前 `trusted_endpoint` 默认已改为 `false`，属安全默认，已改）。
- **既有测试顺序敏感问题**：`test_web_smoke.py` 与 `test_llm_ui.py` 同进程组合运行时，
  第二个 AppTest 会报 `Component 'settings_dialog' is not registered`（跨 AppTest 会话的
  组件注册缓存污染）。全量 `pytest` 顺序下不复现，属既有测试基建问题，未在本次范围内修。
- **T4.1 / T4.2**（语义残留审计、相似扩充）：按清单为选做，未实施。
