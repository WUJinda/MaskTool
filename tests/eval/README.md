# tests/eval —— 检出质量评测（AI 增强收益的量化基线）

对角色的定位：这个目录回答“AI 增强到底有没有用、有多大用”，让提示词与
模型选型从盲调变成可对比的量化决策。

## 用法

```bash
python tests/eval/run_eval.py                    # 纯规则（确定性、可复现）
python tests/eval/run_eval.py --llm              # 规则 + AI（role=adjudicator）
python tests/eval/run_eval.py --llm --role both  # 复核 + 增量检测
python tests/eval/run_eval.py --json             # 机器可读（CI 解析）
python tests/eval/run_eval.py --lexicon config/lexicon.yaml   # 用真实词库测线上效果
```

`--llm` 需要先在「设置 → 模型配置」配好端点；否则脚本会提示退出。

## 文件

| 文件 | 作用 |
| --- | --- |
| `cases.yaml` | 标注语料：`text` + `expected`（真实敏感实体全集）+ `notes` |
| `lexicon.yaml` | 评测专用合成词库（与用户 `config/lexicon.yaml` 解耦，保证可复现） |
| `whitelist.yaml` | 评测专用白名单（行业通用词应被过滤） |
| `run_eval.py` | 评测脚本（可 `import` 复用，见下） |

## 指标

- **实体文本精确匹配**计 P/R/F1；`type` 单独统计类型准确率。
- 未标注却被检出 = **FP（误报）**；标注却未被检出 = **FN（漏检）**。
- 明细列出每条 FP/FN，直接指向“规则缺口 / jieba 噪声 / AI 误判”。

## 2026-10-16 基线（纯规则）

```
TP=14  FP=4  FN=1
Precision=0.778  Recall=0.933  F1=0.848  类型准确率=1.000
FP：云岭 / 华东（jieba 把公司名切成地名）、银行卡（jieba 把通用词标为人名）
FN：张工（姓氏+职务的非正式指代）
```

这组数字印证了设计判断：规则召回已高，**误报集中在 jieba 噪声**（正是
`adjudicator` 复核的目标区），**漏检集中在非正式指代**（正是 `detector`
增量检测的目标区）。用同一语料跑 `--llm` 即可对比出 AI 的实际增益。

## 回归护栏

`tests/test_eval_baseline.py` 断言若干**稳定不变量**（白名单零误报、
正则/词库用例无漏检、整体 F1 下限），防止后续改动破坏规则基线。
它只断言与 jieba 版本无关的确定性结论，不锁死 jieba 噪声数量。
