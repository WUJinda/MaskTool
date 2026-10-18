# -*- coding: utf-8 -*-
"""检出基线回归护栏（T3.1）。

断言若干与 jieba 版本无关的**确定性不变量**，防止后续改动破坏规则基线；
不锁死 jieba 噪声的具体数量（那会随 jieba 版本漂移）。
"""

import importlib.util
from pathlib import Path

import pytest

_EVAL = Path(__file__).resolve().parent / "eval" / "run_eval.py"


def _load_run_eval():
    spec = importlib.util.spec_from_file_location("mask_eval_run_eval", _EVAL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def baseline():
    ev = _load_run_eval()
    cases = ev.load_cases()
    detector, _ = ev.build_detector()
    res = ev.evaluate(detector, cases)
    return res, {c["id"]: c for c in res["cases"]}


def test_whitelist_blocks_generic_words(baseline):
    """仅行业通用词的用例：白名单前置过滤 → 零检出（无 FP/FN）。"""
    _, per = baseline
    assert per["generic-words-only"] == {
        "id": "generic-words-only", "tp": 0, "fp": 0, "fn": 0,
    }


def test_whitelist_is_actually_filtering(tmp_path):
    """对照断言（可证伪）：白名单置空时通用词应被检出，否则上面的零误报是恒真的。"""
    ev = _load_run_eval()
    cases = [c for c in ev.load_cases() if c["id"] == "generic-words-only"]
    empty_wl = tmp_path / "empty_whitelist.yaml"
    empty_wl.write_text("whitelist: []\n", encoding="utf-8")
    detector, _ = ev.build_detector(whitelist=str(empty_wl))
    res = ev.evaluate(detector, cases)
    assert res["fp"] >= 1, "白名单置空后通用词应被检出（证明白名单确实在过滤）"


def test_regex_and_lexicon_no_miss(baseline):
    """正则与词库可确定命中的用例不应漏检。"""
    _, per = baseline
    assert per["amount-regex"]["tp"] == 2 and per["amount-regex"]["fn"] == 0
    assert per["id-and-card"]["tp"] == 2 and per["id-and-card"]["fn"] == 0
    assert per["lexicon-regex-clean"]["fn"] == 0


def test_overall_rule_baseline_floor(baseline):
    """整体 F1 下限（回归护栏；当前纯规则基线约 0.85）。"""
    res, _ = baseline
    assert res["f1"] >= 0.7
    assert res["recall"] >= 0.8
