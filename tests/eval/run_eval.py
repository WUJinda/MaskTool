# -*- coding: utf-8 -*-
"""检出质量评测（P/R/F1）：AI 增强收益的量化基线。

对 ``tests/eval/cases.yaml`` 的人工标注语料运行检测，统计
precision / recall / F1 与 FP / FN 明细。

设计要点：
- 语料与词库随包提供（``tests/eval/``），默认与用户 ``config/lexicon.yaml``
  解耦，保证基线可复现；``--lexicon`` / ``--whitelist`` 可指向真实配置，
  用于测量线上效果（成对覆盖，见 README）。
- 默认纯规则（确定性、可复现）；``--llm`` 叠加 AI 增强（需先配置可用端点）。
  同一语料对比“纯规则 vs 规则+AI”，即可量化 AI 的实际收益。
- 隐私门禁与 Web/pipeline 一致：端点在外部网络且未标记信任时，默认拒绝
  发出语料（评测脚本同样不例外），须 ``--allow-external-llm`` 显式授权。
- FP/FN 明细直接指向规则缺口与 AI 误判，作为提示词/词库迭代的输入。

用法::

    python tests/eval/run_eval.py                    # 纯规则
    python tests/eval/run_eval.py --llm              # 规则 + AI（role=adjudicator）
    python tests/eval/run_eval.py --llm --role both  # 复核 + 增量检测
    python tests/eval/run_eval.py --llm --allow-external-llm  # 显式授权外部端点
    python tests/eval/run_eval.py --json             # 机器可读（CI）
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

EVAL_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = EVAL_DIR.parents[1]
_SRC = PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import yaml  # noqa: E402

from mask_tool.core.detector import Detector  # noqa: E402
from mask_tool.core.ner.jieba_ner import JiebaNER  # noqa: E402


def _norm(t: str) -> str:
    """实体文本归一化：strip + NFKC（全半角/兼容字符统一），保证可比。"""
    return unicodedata.normalize("NFKC", str(t).strip())


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_cases(path: Optional[str] = None) -> List[dict]:
    """读取评测语料（默认 tests/eval/cases.yaml）。"""
    p = Path(path) if path else EVAL_DIR / "cases.yaml"
    return _load_yaml(p).get("cases", [])


def build_detector(
    lexicon: Optional[str] = None,
    whitelist: Optional[str] = None,
    use_llm: bool = False,
    role: str = "adjudicator",
    allow_external_llm: bool = False,
) -> Tuple[Detector, object]:
    """构建检测器；返回 (detector, llm_stats|None)。"""
    lex = _load_yaml(Path(lexicon) if lexicon else EVAL_DIR / "lexicon.yaml")
    wl_doc = _load_yaml(Path(whitelist) if whitelist else EVAL_DIR / "whitelist.yaml")
    wl = set(wl_doc.get("whitelist", []))
    ner = JiebaNER()
    ner.set_whitelist(wl)
    det: Detector = Detector(lex, wl, ner_engine=ner)

    stats = None
    if use_llm:
        det, stats = _wrap_llm(det, role, allow_external_llm)
    return det, stats


def _wrap_llm(det: Detector, role: str, allow_external_llm: bool):
    """把规则检测器包装为 AI 增强检测器（读配置中的 llm 段）。"""
    from mask_tool.core.config_loader import load_config
    from mask_tool.core.llm.adjudicator import LLMAdjudicator, LLMRunStats
    from mask_tool.core.llm.client import OpenAICompatClient
    from mask_tool.core.llm.detector_wrapper import LLMEnhancedDetector
    from mask_tool.core.llm.endpoint import is_loopback_or_private

    cfg, _ = load_config(None, "smart")
    llm = cfg.llm
    if not (llm.base_url and llm.model):
        raise SystemExit(
            "启用 --llm 需要先配置 llm.base_url 与 llm.model"
            "（设置 → 模型配置，或 config/default.yaml）"
        )
    # 隐私门禁：与 Web/pipeline 同一口径。外部端点未信任时，评测语料同样
    # 会被发送到外部（可能含真实文档），未经显式授权不允许。
    if not getattr(llm, "trusted_endpoint", False) \
            and not is_loopback_or_private(llm.base_url):
        raise SystemExit(
            f"端点 {llm.base_url} 位于外部网络且未标记信任：评测脚本"
            "不会未经确认发送语料。可在配置中将 llm.trusted_endpoint "
            "设为 true，或显式加 --allow-external-llm 授权本次外发。"
        )
    client = OpenAICompatClient(
        llm.base_url, llm.model, api_key=llm.api_key,
        timeout=llm.timeout_seconds,
    )
    stats = LLMRunStats(model=llm.model, base_url=client.base_url)
    wrapped = LLMEnhancedDetector(
        det, LLMAdjudicator(client, llm, stats=stats), role=role,
    )
    return wrapped, stats


def evaluate(detector, cases: List[dict]) -> Dict[str, object]:
    """按“实体文本精确匹配”计算 P/R/F1（类型单独统计准确率）。

    文本比较前做 strip + NFKC 归一化，避免全半角/空白漂移制造伪 FP/FN。
    """
    tp = fp = fn = 0
    type_ok = type_total = 0
    fps: List[dict] = []
    fns: List[dict] = []
    per_case: List[dict] = []

    for c in cases:
        preds: Dict[str, object] = {}
        for r in detector.detect(c["text"]):
            preds.setdefault(_norm(r.text), r)  # 同文本去重（归一化后）
        exp = {_norm(e["text"]): e for e in (c.get("expected") or [])}

        c_tp = c_fp = c_fn = 0
        for t, r in preds.items():
            if t in exp:
                c_tp += 1
                etype = exp[t].get("type")
                if etype:  # 类型准确率仅在标注了 type 的命中项上计
                    type_total += 1
                    if r.text_type.value == etype:
                        type_ok += 1
            else:
                c_fp += 1
                fps.append({"case": c["id"], "text": r.text, "type": r.text_type.value})
        for t, e in exp.items():
            if t not in preds:
                c_fn += 1
                fns.append({"case": c["id"], "text": e["text"], "type": e.get("type")})

        tp += c_tp
        fp += c_fp
        fn += c_fn
        per_case.append({"id": c["id"], "tp": c_tp, "fp": c_fp, "fn": c_fn})

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        "tp": tp, "fp": fp, "fn": fn,
        "precision": precision, "recall": recall, "f1": f1,
        "type_accuracy": (type_ok / type_total if type_total else 0.0),
        "cases": per_case,
        "false_positives": fps,
        "false_negatives": fns,
    }


def main(argv=None) -> int:
    # Windows 默认代码页下中文输出乱码：面向中文用户，强制 stdout UTF-8。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="mask-tool 检出质量评测（P/R/F1）")
    ap.add_argument("--llm", action="store_true", help="叠加 AI 增强（需已配置端点）")
    ap.add_argument("--role", default="adjudicator",
                    choices=["adjudicator", "detector", "both"])
    ap.add_argument("--lexicon", help="覆盖词库路径（默认 tests/eval/lexicon.yaml）")
    ap.add_argument("--whitelist", help="覆盖白名单路径")
    ap.add_argument("--cases", help="覆盖语料路径（默认 tests/eval/cases.yaml）")
    ap.add_argument("--allow-external-llm", action="store_true",
                    help="显式授权向外部且未信任的端点发送语料")
    ap.add_argument("--json", action="store_true", help="输出 JSON（供 CI 解析）")
    args = ap.parse_args(argv)

    cases = load_cases(args.cases)
    detector, stats = build_detector(
        args.lexicon, args.whitelist, args.llm, args.role,
        allow_external_llm=args.allow_external_llm,
    )
    result = evaluate(detector, cases)
    result["mode"] = ("llm/" + args.role) if args.llm else "rules"
    if stats is not None:
        result["llm_calls"] = stats.calls
        result["llm_errors"] = stats.errors

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    mode = f"规则 + AI（{args.role}）" if args.llm else "纯规则"
    print(f"=== mask-tool 检出评测：{mode} · 语料 {len(cases)} 条 ===")
    print(f"TP={result['tp']}  FP={result['fp']}  FN={result['fn']}")
    print(f"Precision={result['precision']:.3f}  Recall={result['recall']:.3f}  "
          f"F1={result['f1']:.3f}  类型准确率={result['type_accuracy']:.3f}")
    if stats is not None:
        print(f"LLM 调用={stats.calls}  错误={stats.errors}")
    if result["false_positives"]:
        print("\n-- 误报 FP（应按规则/白名单修正，或确认非敏感）--")
        for x in result["false_positives"]:
            print(f"  [{x['case']}] {x['text']}  ({x['type']})")
    if result["false_negatives"]:
        print("\n-- 漏检 FN（规则缺口；AI 的目标区）--")
        for x in result["false_negatives"]:
            print(f"  [{x['case']}] {x['text']}  ({x['type']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
