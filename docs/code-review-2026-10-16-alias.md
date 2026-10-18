# 代码审查报告：alias 伪装词脱敏功能（2026-10-16）

> 审查对象：伪装词脱敏模式（alias mode）全量改动（新增 `core/alias_generator.py`、
> `config/alias_lexicon.yaml` 等 4 个新文件，修改 engine/pipeline/tokenizer/restore/
> adapters/cli/web 等 17 个文件）。
> 审查基线：505 passed / 6 skipped（审查开始时的自查修复后状态）。

## 1. 审查方式

三个独立视角，交叉验证：

| 视角 | 范围 | 方式 |
| --- | --- | --- |
| 审查子会话 A | alias 生成器与核心引擎（生成/避让/复用/池耗尽/接线） | 只读审查 + 临时验证脚本实证怀疑点 + 全量测试 |
| 审查子会话 B | 还原链路与入口接线（gate/对账/roundtrip/CLI/Web 传参链） | 同上 |
| 主会话自查 | 调用方完整性、跨模块契约、性能与内存 | grep 全调用点 + E2E 复验 |

**总体结论：0 critical。核心还原链路（bracket_gate 语义、对账、roundtrip、旧批次
零回归）审查确认正确；发现 2 项 Major、5 项 Minor 及若干 Info，已修复并补 25 个
回归用例。**

## 2. 发现与修复对照

### Major（A/B 子会话独立确认 + 实证复现）

| # | 问题 | 修复 | 回归测试 |
| --- | --- | --- | --- |
| M-1 | `PathMasker.detect_name` 把文件名检测项 `source` 统一改写为 `"path"`，丢失原始来源 → alias 模式下文件名里的手机号拿到伪装词"甲"（`合同13800138000.docx` → `合同甲.docx`），违背"数字类保持占位符"设计且消耗 custom 池 | `DetectionResult` 新增 `origin_source` 字段：展示语义（`source="path"`）不变，`generate` 改用真实来源判定风格边界；path_masker 3 处 + cli 审计循环改 `r.origin_source or r.source` | `test_phone_in_filename_keeps_token_style`、`test_display_source_and_origin_source`、`test_same_entity_content_and_filename_consistent`、`test_filename_alias_roundtrip_via_unmask_tree` + E2E |
| M-2 | 配置文件 `mask_style` 是死配置：CLI `--style` 默认值与 Web 侧栏无条件覆盖，`default.yaml` 写 `mask_style: alias` 永不生效且无警告 | CLI `--style` 改 `Optional[str]`（缺省跟随配置文件，显式传入优先，打印实际生效值）；Web 侧栏新增 `_config_default_mask_style()`，初值链：持久化偏好 > 配置文件 > token | `test_config_alias_effective_without_cli_flag`、`test_cli_flag_overrides_config`、`test_invalid_style_value_rejected`、`test_sidebar_default_falls_back_to_config` + E2E |

### Minor

| # | 问题 | 修复 |
| --- | --- | --- |
| A-Minor-1 | 自定义池混入 `[CUSTOM_001]` 样式词时，alias 与 fallback 复用同一替换串 → 双原文同词、mapping 覆盖、静默误还原 | `_is_free` 拒绝 `[` 开头候选；`_fallback_token` 编号同时跳过 reserved 与已用替换串 |
| A-Minor-2 | 避让扫描未反转义 XML 实体：池词 `A&B` 与文档存储态 `A&amp;B` 不匹配，漏避让 | `_scan_file_text` 剥标签后追加 `html.unescape` |
| A-Minor-3 / B-m-3 | 池耗尽、避让超限对用户完全静默；Web 还原无对账扫描 | CLI/Web 收尾透出黄色警告；Web 两条还原流程接入与 CLI 同口径的产物对账（残留红字提示，不阻断交付） |
| B-m-1 | templates.py 与 default.yaml 的 alias 段注释不同步；既有漂移（ner.enabled、wall_clock）未在 docstring 声明 | 两文件对齐；docstring 显式列出已知差异清单 |

### 主会话自查（审查开始前已修复，审查者验证了当前状态）

| 问题 | 修复 |
| --- | --- |
| `_scan_file_text` 保留原始 XML：跨 run 拆分词（`<w:t>张</w:t><w:t>三</w:t>`）漏避让 → unmask 误还原；且全量驻留内存 | 剥除 XML 标签（run 拼接后可命中 + 体积减半）；新增 64M 字符总量上限 + 单段 8M 截断保护 |
| 三处 `generate` 未传 source（xlsx shim 降级路径、pptx 预扫描、cli 审计循环） | 全部补传 |
| B 审查提及的 Info 杂项 | `_TYPE_PREFIX` 单源引用 TokenGenerator；`AliasLexicon.load` 失败告警；错别字修正 |

### E2E 复验发现（修复过程中）

| 问题 | 修复 |
| --- | --- |
| `exhausted_categories()` 把风格边界的正常回退（金额/手机号走占位符）计为"池耗尽"，几乎每个含金额的批次都误报警告 | 区分"真·池耗尽"（`_pool_exhausted`）与"设计内回退"（`_fallback_counters`），只有前者告警 |

## 3. 验证结果

- 全量 **530 passed, 6 skipped**（审查前 505 → 新增 25 个回归用例：
  `tests/test_alias_review_fixes.py` 15 项 + 拆分词/实体避让/上限保护专项）
- 两轮 CLI E2E：配置文件 alias 生效、文件名"人名→伪装词 + 手机号→占位符"
  （`李海富13800138000合同.docx` → `汤姆[CUSTOM_001]合同_masked.docx`）、
  unmask 对账零残留、误报警告消除
- 新增覆盖此前盲区：PathMasker × alias 文件名 roundtrip、`source="path"` 边界、
  docx 混合批次、token 样式池词、XML 实体避让、CLI/Web mask_style 生效链

## 4. 遗留建议（低优先级，未实施）

1. **预扫描双遍开销**：`_scan_file_tokens` 与 `_scan_file_text` 对同一 OOXML 各
   解压一遍，可合并为一次读取两用（实测最坏 ~14ms/次，不紧迫）
2. **相邻单元格/段落误拼接**：剥标签把 xlsx 相邻值直接相连（`"A"+"市场部"` 含
   `A市`）造成误避让、加速池耗尽——保守方向无正确性问题；若要精确可在块级结束
   标签（`</w:p>`、`</tc>`）注入换行（勿动 run 级标签，会重新引入拆分漏避让）
3. **`config` 命令生成的 alias_lexicon.yaml 为 CRLF**（模板串 LF）——YAML 语义
   等价，无功能影响
4. **文档提示**：README alias 段可补"自定义词池避免使用单字或超常见词"（roundtrip
   总前提"仅保证未再编辑文档"已声明）

## 5. 审查说明

- 两个审查子会话基于 09:18–09:25 磁盘版本；部分 minor 条目基于更早快照，主会话
  逐条核对当前状态后定级
- 审查期间确认无副作用项：`web irreversible` 分支 pipeline.token_gen 虽为
  AliasGenerator，但 `generate` 恒不被调用（`***` 路径），无泄漏
