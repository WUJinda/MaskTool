"""统一替换引擎单测（D1 §8 engine 单测 1-7）"""

import pytest

from mask_tool.core.engine import (
    MaskOutcome,
    Replacement,
    ReplacementEngine,
    Span,
    TOKEN_PATTERN,
)
from mask_tool.core.tokenizer import TokenGenerator
from mask_tool.models.detection import (
    DetectionResult, DetectionStatus, DetectionType, Location,
)
from mask_tool.models.mapping import fingerprint_of


def make_result(text, text_type=DetectionType.COMPANY, confidence=0.95,
                status=DetectionStatus.AUTO_MASK, source="dictionary"):
    return DetectionResult(
        text=text,
        text_type=text_type,
        source=source,
        confidence=confidence,
        location=Location(file="test.docx"),
        status=status,
    )


def make_engine(irreversible=False, amount_mode="token", batch_id="B1"):
    return ReplacementEngine(
        TokenGenerator(),
        irreversible=irreversible,
        amount_mode=amount_mode,
        batch_id=batch_id,
    )


class TestLocate:
    """§8-1 locate：同一词多次出现全部定位；不同词独立定位"""

    def test_same_word_all_occurrences(self):
        text = "甲方是某某公司，乙方也是某某公司，某某公司盖章。"
        spans = ReplacementEngine.locate(text, [make_result("某某公司")])
        assert [s.start for s in spans] == [3, 12, 17]
        for s in spans:
            assert text[s.start:s.end] == "某某公司"

    def test_different_words_independent(self):
        text = "某某公司的负责人是某人甲"
        spans = ReplacementEngine.locate(text, [
            make_result("某某公司"), make_result("某人甲", DetectionType.PERSON),
        ])
        found = {s.text for s in spans}
        assert found == {"某某公司", "某人甲"}

    def test_status_filter(self):
        text = "负责人是某人甲"
        r_hint = make_result("某人甲", DetectionType.PERSON, status=DetectionStatus.HINT_ONLY)
        r_suggest = make_result("某人甲", DetectionType.PERSON, status=DetectionStatus.SUGGEST_MASK)
        # 默认 statuses 仅 AUTO_MASK
        assert ReplacementEngine.locate(text, [r_hint, r_suggest]) == []
        spans = ReplacementEngine.locate(
            text, [r_suggest],
            statuses=frozenset({DetectionStatus.SUGGEST_MASK}))
        assert len(spans) == 1

    def test_allowed_originals_filter(self):
        text = "甲方是某某公司，负责人是某人甲"
        results = [
            make_result("某某公司"),
            make_result("某人甲", DetectionType.PERSON),
        ]
        spans = ReplacementEngine.locate(
            text, results,
            allowed_originals={"某人甲"})
        assert [s.text for s in spans] == ["某人甲"]

    def test_absent_text_no_span(self):
        assert ReplacementEngine.locate("无关文本", [make_result("某某公司")]) == []


class TestResolveOverlaps:
    """§8-2 resolve_overlaps：嵌套取长；同长取高置信；被丢弃项进 dropped"""

    def test_nested_keeps_longer(self):
        long_r = make_result("某某建设集团有限公司", confidence=0.95)
        short_r = make_result("集团", confidence=0.95, source="ner")
        long_s = Span(0, 10, "某某建设集团有限公司", long_r)
        short_s = Span(6, 8, "集团", short_r)
        kept, dropped = ReplacementEngine.resolve_overlaps([short_s, long_s])
        assert kept == [long_s]
        assert dropped == [short_s]

    def test_same_length_higher_confidence_wins(self):
        r_high = make_result("某某公司", confidence=0.95)
        r_low = make_result("某某公司", DetectionType.SUBJECT, confidence=0.75, source="ner")
        s_high = Span(0, 4, "某某公司", r_high)
        s_low = Span(0, 4, "某某公司", r_low)
        kept, dropped = ReplacementEngine.resolve_overlaps([s_low, s_high])
        assert kept == [s_high]
        assert dropped == [s_low]

    def test_kept_sorted_by_start(self):
        r1 = make_result("某人甲", DetectionType.PERSON)
        r2 = make_result("某某公司")
        s_late = Span(10, 14, "某某公司", r2)
        s_early = Span(0, 3, "某人甲", r1)
        kept, dropped = ReplacementEngine.resolve_overlaps([s_late, s_early])
        assert kept == [s_early, s_late]
        assert dropped == []


class TestApply:
    """§8-3 apply：区间相邻不丢字；顶到 0 与 len(text)；空替换列表返回原文"""

    def _rep(self, start, end, text, new_text):
        return Replacement(Span(start, end, text, make_result(text)), new_text, None)

    def test_adjacent_spans_no_char_lost(self):
        text = "某某公司某人甲"
        reps = [
            self._rep(0, 4, "某某公司", "[COMPANY_001]"),
            self._rep(4, 7, "某人甲", "[PERSON_001]"),
        ]
        assert ReplacementEngine.apply(text, reps) == "[COMPANY_001][PERSON_001]"

    def test_span_at_both_ends(self):
        text = "某人甲中间普通文本某人乙"
        reps = [
            self._rep(0, 3, "某人甲", "X"),
            self._rep(9, 12, "某人乙", "Y"),
        ]
        assert ReplacementEngine.apply(text, reps) == "X中间普通文本Y"

    def test_empty_replacements_returns_original(self):
        text = "完全普通的文本"
        assert ReplacementEngine.apply(text, []) == text

    def test_overlapping_replacements_rejected(self):
        text = "某某公司某某集团"
        reps = [
            self._rep(0, 4, "某某公司", "A"),
            self._rep(2, 6, "公司某某", "B"),  # 与前一个重叠
        ]
        with pytest.raises(ValueError):
            ReplacementEngine.apply(text, reps)


class TestH6StatusesAndAllowed:
    """§8-4 H6 回归：statuses 默认不含 SUGGEST_MASK；allowed_originals 只放行勾选项"""

    def test_suggest_not_masked_by_default(self):
        engine = make_engine()
        results = [
            make_result("某人甲", DetectionType.PERSON, status=DetectionStatus.SUGGEST_MASK),
        ]
        outcome = engine.mask_plain_text("负责人：某人甲", results)
        assert outcome.text == "负责人：某人甲"
        assert outcome.replacements == []

    def test_suggest_masked_when_statuses_explicit(self):
        engine = make_engine()
        results = [
            make_result("某人甲", DetectionType.PERSON, status=DetectionStatus.SUGGEST_MASK),
        ]
        outcome = engine.mask_plain_text(
            "负责人：某人甲", results,
            statuses=frozenset({DetectionStatus.AUTO_MASK, DetectionStatus.SUGGEST_MASK}))
        assert "某人甲" not in outcome.text
        assert "[PERSON_001]" in outcome.text

    def test_allowed_originals_only_checked(self):
        engine = make_engine()
        results = [
            make_result("某某公司"),
            make_result("某人甲", DetectionType.PERSON),
        ]
        outcome = engine.mask_plain_text(
            "某某公司 负责人：某人甲", results,
            statuses=frozenset({DetectionStatus.AUTO_MASK, DetectionStatus.SUGGEST_MASK}),
            allowed_originals={"某人甲"})
        # 未勾选的 AUTO 项不替换，勾选项替换
        assert "某某公司" in outcome.text
        assert "某人甲" not in outcome.text

    def test_active_scope_from_masker_path(self):
        """process_file 传导契约：engine.active_* 生效于未显式传参的调用"""
        engine = make_engine()
        results = [
            make_result("某某公司"),
            make_result("某人甲", DetectionType.PERSON, status=DetectionStatus.SUGGEST_MASK),
        ]
        engine.active_statuses = frozenset(
            {DetectionStatus.AUTO_MASK, DetectionStatus.SUGGEST_MASK})
        engine.active_allowed_originals = {"某人甲"}
        outcome = engine.mask_plain_text("某某公司 某人甲", results)
        assert "某某公司" in outcome.text
        assert "某人甲" not in outcome.text


class TestReserved:
    """§8-5 reserved：预置 [PERSON_001] 的文本 mask 后新 token 编号 >= 002"""

    def test_reserved_via_scan_and_generate(self):
        token_gen = TokenGenerator()
        token_gen.set_reserved(ReplacementEngine.scan_tokens("已有占位 [PERSON_001]"))
        engine = ReplacementEngine(token_gen)
        outcome = engine.mask_plain_text(
            "负责人：张三", [make_result("张三", DetectionType.PERSON)])
        assert "[PERSON_001]" not in outcome.text
        assert "[PERSON_002]" in outcome.text

    def test_generate_skips_multiple_reserved(self):
        token_gen = TokenGenerator()
        token_gen.set_reserved(["[COMPANY_001]", "[COMPANY_002]"])
        assert token_gen.generate("公司A", DetectionType.COMPANY) == "[COMPANY_003]"

    def test_scan_tokens_extracts_all(self):
        text = "a [COMPANY_001] b [PERSON_012] c [AMOUNT_9999] d not_token"
        assert ReplacementEngine.scan_tokens(text) == {
            "[COMPANY_001]", "[PERSON_012]", "[AMOUNT_9999]"}


class TestRoundtrip:
    """§8-6 roundtrip：mask_plain_text -> 按 mapping 反替换 == 原文"""

    def test_roundtrip_multiple_entities(self):
        engine = make_engine(batch_id="B-rt")
        text = "甲方：某某建设集团有限公司，负责人：某人甲，金额1.2亿。某某建设集团有限公司再次出现。"
        results = [
            make_result("某某建设集团有限公司"),
            make_result("某人甲", DetectionType.PERSON),
            make_result("1.2亿", DetectionType.AMOUNT),
        ]
        outcome = engine.mask_plain_text(text, results)
        assert "某某建设集团有限公司" not in outcome.text
        assert "某人甲" not in outcome.text
        token_map = {m.token: m.original for m in engine.mappings}
        restored, hits = ReplacementEngine.unmask_text(outcome.text, token_map)
        assert restored == text
        assert hits == set(token_map)

    def test_unmask_long_token_first(self):
        """长 token 先替换，防前缀吞并"""
        token_map = {"[PERSON_1]": "甲", "[PERSON_12]": "乙"}
        text = "x [PERSON_1] y [PERSON_12]"
        restored, hits = ReplacementEngine.unmask_text(text, token_map)
        assert restored == "x 甲 y 乙"
        assert hits == {"[PERSON_1]", "[PERSON_12]"}

    def test_unmask_no_hit_returns_original(self):
        restored, hits = ReplacementEngine.unmask_text("plain", {"[PERSON_001]": "甲"})
        assert restored == "plain"
        assert hits == set()


class TestAmountMode:
    """§8-7 M5：amount_mode=fuzzy 输出模糊金额且 mapping 无记录"""

    def test_fuzzy_mode(self):
        engine = make_engine(amount_mode="fuzzy")
        results = [make_result("1.2亿元", DetectionType.AMOUNT, confidence=0.80, source="regex")]
        outcome = engine.mask_plain_text("合同金额：1.2亿元", results)
        assert "1亿+" in outcome.text
        assert "1.2亿元" not in outcome.text
        assert engine.mappings == []
        assert outcome.new_mappings == []
        assert outcome.replacements[0].token is None

    def test_fixed_mode(self):
        engine = make_engine(amount_mode="fixed")
        results = [make_result("1.2亿元", DetectionType.AMOUNT, confidence=0.80, source="regex")]
        outcome = engine.mask_plain_text("合同金额：1.2亿元", results)
        assert outcome.text == "合同金额：***"
        assert engine.mappings == []

    def test_token_mode_default_reversible(self):
        engine = make_engine()  # amount_mode 默认 token
        results = [make_result("1.2亿元", DetectionType.AMOUNT, confidence=0.80, source="regex")]
        outcome = engine.mask_plain_text("合同金额：1.2亿元", results)
        assert "[AMOUNT_001]" in outcome.text
        assert len(engine.mappings) == 1

    def test_invalid_amount_mode_rejected(self):
        with pytest.raises(ValueError):
            make_engine(amount_mode="bogus")


class TestMappingRegistration:
    """mapping 登记：batch_id / fingerprint / 同一原文复用 token 且不重复登记"""

    def test_batch_id_and_fingerprint(self):
        engine = make_engine(batch_id="20260101-000001-abc123")
        results = [make_result("某人甲", DetectionType.PERSON)]
        outcome = engine.mask_plain_text("负责人：某人甲", results)
        assert len(outcome.new_mappings) == 1
        m = outcome.new_mappings[0]
        assert m.batch_id == "20260101-000001-abc123"
        assert m.fingerprint == fingerprint_of("某人甲")
        assert m.kind == "text"

    def test_same_original_reuses_token_no_duplicate_mapping(self):
        engine = make_engine()
        results = [make_result("某某公司")]
        o1 = engine.mask_plain_text("某某公司与某某公司", results)
        assert o1.text.count("[COMPANY_001]") == 2
        assert len(engine.mappings) == 1
        # 第二段再次出现：仍复用，不新增登记
        o2 = engine.mask_plain_text("再见某某公司", results)
        assert o2.text == "再见[COMPANY_001]"
        assert len(engine.mappings) == 1
        assert o2.new_mappings == []


class TestOverlapResolutionIntegration:
    """M6 场景经引擎消解：人民币长区间优先，通用金额子串被丢弃"""

    def test_rmb_long_span_wins(self):
        engine = make_engine()
        text = "合同约定人民币 1.2亿，分三期支付"
        results = [
            make_result("人民币 1.2亿", DetectionType.AMOUNT, 0.90, source="regex"),
            make_result("1.2亿", DetectionType.AMOUNT, 0.80, source="regex"),
        ]
        outcome = engine.mask_plain_text(text, results)
        assert outcome.text == "合同约定[AMOUNT_001]，分三期支付"
        assert outcome.dropped_overlaps and outcome.dropped_overlaps[0].text == "1.2亿"

    def test_irreversible_masks_all(self):
        engine = make_engine(irreversible=True)
        results = [make_result("某人甲", DetectionType.PERSON)]
        outcome = engine.mask_plain_text("负责人：某人甲", results)
        assert outcome.text == "负责人：***"
        assert engine.mappings == []


class TestPlanRunRewrite:
    """run 重建辅助：整段并入首单元，其余置空"""

    def test_basic(self):
        assert ReplacementEngine.plan_run_rewrite(["a", "b", "c"], "XYZ") == ["XYZ", "", ""]

    def test_single_unit(self):
        assert ReplacementEngine.plan_run_rewrite(["ab"], "XYZ") == ["XYZ"]

    def test_empty_units(self):
        assert ReplacementEngine.plan_run_rewrite([], "XYZ") == []


class TestTokenPattern:
    def test_pattern_matches_generated_styles(self):
        for tok in ["[COMPANY_001]", "[GOVERNMENT_012]", "[PERSON_9999]", "[CUSTOM_123]"]:
            assert TOKEN_PATTERN.fullmatch(tok)
        for tok in ["[COMPANY_1]", "PERSON_001", "[FOO_001]", "[PERSON_0012x]"]:
            assert not TOKEN_PATTERN.fullmatch(tok)


class TestMaskOutcomeDefaults:
    def test_outcome_dataclass_defaults(self):
        outcome = MaskOutcome(text="x")
        assert outcome.replacements == []
        assert outcome.dropped_overlaps == []
        assert outcome.new_mappings == []
