"""AliasGenerator（伪装词生成器）单测

覆盖：类别池取词顺序 / 同原文复用 / 撞词避让（register_source_text）/
前缀约束 / 非法字符过滤 / 风格边界（AMOUNT 与 regex-CUSTOM 走 token 样式）/
池耗尽 fallback 与编号让位 / reset 语义 / AliasLexicon 配置加载。
"""

import pytest

from mask_tool.core.alias_generator import (
    AliasGenerator, AliasLexicon,
)
from mask_tool.models.detection import DetectionType


@pytest.fixture
def gen():
    return AliasGenerator()


class TestBasicGeneration:
    def test_person_from_pool_in_order(self, gen):
        assert gen.generate("李海富", DetectionType.PERSON) == "汤姆"
        assert gen.generate("王小明", DetectionType.PERSON) == "杰瑞"
        assert gen.generate("赵铁柱", DetectionType.PERSON) == "张三"

    def test_same_original_reuses_alias(self, gen):
        a1 = gen.generate("李海富", DetectionType.PERSON)
        a2 = gen.generate("李海富", DetectionType.PERSON)
        assert a1 == a2 == "汤姆"
        assert len(gen.get_all_mappings()) == 1

    def test_location_letter_expand(self, gen):
        assert gen.generate("北京市", DetectionType.LOCATION) == "A市"
        assert gen.generate("上海市", DetectionType.LOCATION) == "B市"
        assert gen.generate("广州市", DetectionType.LOCATION) == "C市"

    def test_company_and_government(self, gen):
        assert gen.generate("某某集团", DetectionType.COMPANY) == "A集团"
        assert gen.generate("某局", DetectionType.GOVERNMENT) == "A单位"

    def test_project_subject_share_pool(self, gen):
        assert gen.generate("某某项目", DetectionType.PROJECT) == "A项目"
        assert gen.generate("某某大楼", DetectionType.SUBJECT) == "B项目"

    def test_custom_pool(self, gen):
        assert gen.generate("内部编号X", DetectionType.CUSTOM, source="manual") == "甲"
        assert gen.generate("内部编号Y", DetectionType.CUSTOM, source="dictionary") == "乙"


class TestStyleBoundary:
    """决策 D1：金额与 regex 来源的 CUSTOM 保持 token 样式"""

    def test_amount_uses_token_style(self, gen):
        token = gen.generate("1.2亿", DetectionType.AMOUNT)
        assert token == "[AMOUNT_001]"
        assert token in gen.get_all_mappings()["1.2亿"]

    def test_regex_custom_uses_token_style(self, gen):
        assert gen.generate("13800138000", DetectionType.CUSTOM, source="regex") == "[CUSTOM_001]"
        assert gen.generate("110101199001011234", DetectionType.CUSTOM, source="regex") == "[CUSTOM_002]"

    def test_manual_custom_uses_alias(self, gen):
        assert gen.generate("内部编号", DetectionType.CUSTOM, source="manual") == "甲"

    def test_amount_and_regex_not_alias_pool(self, gen):
        gen.generate("1.2亿", DetectionType.AMOUNT)
        gen.generate("13800138000", DetectionType.CUSTOM, source="regex")
        gen.generate("张全蛋", DetectionType.PERSON)
        assert "汤姆" in gen.get_used_aliases()
        assert "[AMOUNT_001]" in gen.get_used_aliases()


class TestAvoidance:
    def test_avoid_word_present_in_source(self, gen):
        gen.register_source_text("本文由张三起草，涉及李海富等人员。")
        # 原文含"张三"：张三不可用作伪装词
        assert gen.generate("李海富", DetectionType.PERSON) == "汤姆"
        assert gen.generate("王小明", DetectionType.PERSON) == "杰瑞"
        assert gen.generate("赵铁柱", DetectionType.PERSON) == "李四"  # 跳过张三

    def test_avoid_xml_noisy_text(self, gen):
        # XML 明文整串比对：标签不剥离，含标签文本仍能命中纯词
        gen.register_source_text("<w:t>张三</w:t>")
        gen.generate("李海富", DetectionType.PERSON)
        assert "张三" not in gen.get_used_aliases()

    def test_prefix_constraint(self, gen):
        # 构造自定义池制造前缀对："汤姆" 与 "汤姆猫"
        lex = AliasLexicon(person_pool=["汤姆猫", "汤姆", "杰克"])
        g = AliasGenerator(lex)
        first = g.generate("甲乙", DetectionType.PERSON)
        second = g.generate("丙丁", DetectionType.PERSON)
        used = g.get_used_aliases()
        # "汤姆" 不得在 "汤姆猫" 已用后启用（前缀冲突），应跳到 "杰克"
        assert first == "汤姆猫"
        assert second == "杰克"
        assert "汤姆" not in used

    def test_reverse_prefix_constraint(self, gen):
        # 先用短词，后一个候选是其扩展 -> 扩展被跳过
        lex = AliasLexicon(person_pool=["汤姆", "汤姆猫", "杰克"])
        g = AliasGenerator(lex)
        assert g.generate("甲乙", DetectionType.PERSON) == "汤姆"
        assert g.generate("丙丁", DetectionType.PERSON) == "杰克"


class TestForbiddenChars:
    def test_filename_forbidden_chars_filtered(self):
        lex = AliasLexicon(custom_pool=["甲", "A/B", 'C"D', "乙"])
        g = AliasGenerator(lex)
        assert g.generate("词1", DetectionType.CUSTOM, source="manual") == "甲"
        assert g.generate("词2", DetectionType.CUSTOM, source="manual") == "乙"
        assert "A/B" not in g.get_used_aliases()


class TestExhaustionFallback:
    def test_pool_exhaustion_falls_back_to_token(self):
        lex = AliasLexicon(person_pool=["汤姆"])
        g = AliasGenerator(lex)
        assert g.generate("甲", DetectionType.PERSON) == "汤姆"
        assert g.generate("乙", DetectionType.PERSON) == "[PERSON_001]"
        assert g.generate("丙", DetectionType.PERSON) == "[PERSON_002]"
        assert g.exhausted_categories() == ["PERSON"]

    def test_fallback_reserved_skipping(self):
        lex = AliasLexicon(person_pool=["汤姆"])
        g = AliasGenerator(lex)
        g.set_reserved({"[PERSON_001]"})   # 文档已存在该样式串
        g.generate("甲", DetectionType.PERSON)
        assert g.generate("乙", DetectionType.PERSON) == "[PERSON_002]"

    def test_custom_pool_exhaustion(self):
        lex = AliasLexicon(custom_pool=["甲"])
        g = AliasGenerator(lex)
        g.generate("词1", DetectionType.CUSTOM, source="manual")
        assert g.generate("词2", DetectionType.CUSTOM, source="manual") == "[CUSTOM_001]"


class TestSuffixExpand:
    def test_location_cycles_through_suffixes(self):
        lex = AliasLexicon(location_suffixes=["市", "省"])
        seq = lex.candidates_for(DetectionType.LOCATION)
        assert seq[0] == "A市"
        assert seq[25] == "Z市"
        assert seq[26] == "A省"

    def test_yaml_load_overrides(self, tmp_path):
        p = tmp_path / "alias.yaml"
        p.write_text(
            "person:\n  - 阿猫\n  - 阿狗\nlocation_suffixes: [道]\n",
            encoding="utf-8",
        )
        lex = AliasLexicon.from_yaml(p)
        assert lex.person_pool == ["阿猫", "阿狗"]
        assert lex.candidates_for(DetectionType.LOCATION)[0] == "A道"
        # 未配置段回退内置默认
        assert lex.custom_pool[0] == "甲"

    def test_load_missing_file_returns_default(self):
        lex = AliasLexicon.load("nonexistent/alias_lexicon.yaml", "")
        assert lex.person_pool[0] == "汤姆"


class TestReset:
    def test_reset_clears_mappings_keeps_defenses(self, gen):
        gen.register_source_text("原文含张三")
        gen.set_reserved({"[PERSON_009]"})
        gen.generate("李海富", DetectionType.PERSON)
        gen.reset()
        assert gen.get_all_mappings() == {}
        assert gen.get_used_aliases() == []
        # 与 TokenGenerator 同语义：计数器归零、防线保留（避让表仍在，
        # "张三"依然不可用作伪装词）
        assert gen._source_texts == ["原文含张三"]
        assert "[PERSON_009]" in gen._reserved
        assert gen.generate("新人", DetectionType.PERSON) == "汤姆"
        assert "张三" not in gen.get_used_aliases()


class TestEngineIntegration:
    """引擎经 generate(original, type, source) 三参签名取替换串"""

    def _make_result(self, text, dtype, source="dictionary",
                     status=None):
        from mask_tool.models.detection import (
            DetectionResult, DetectionStatus, Location,
        )
        return DetectionResult(
            text=text, text_type=dtype, source=source, confidence=0.95,
            location=Location(file="t"),
            status=status or DetectionStatus.AUTO_MASK,
        )

    def test_engine_with_alias_generator(self):
        from mask_tool.core.engine import ReplacementEngine

        eng = ReplacementEngine(AliasGenerator(), batch_id="b1")
        results = [
            self._make_result("李海富", DetectionType.PERSON),
            self._make_result("13800138000", DetectionType.CUSTOM, source="regex"),
        ]
        outcome = eng.mask_plain_text(
            "李海富的电话是13800138000。", results)
        assert "汤姆" in outcome.text
        assert "[CUSTOM_001]" in outcome.text
        assert "李海富" not in outcome.text
        assert "13800138000" not in outcome.text
        m = {x.token: x.original for x in outcome.new_mappings}
        assert m["汤姆"] == "李海富"
        assert m["[CUSTOM_001]"] == "13800138000"

    def test_engine_unmask_roundtrip_with_alias(self):
        from mask_tool.core.engine import ReplacementEngine

        eng = ReplacementEngine(AliasGenerator(), batch_id="b1")
        results = [
            self._make_result("李海富", DetectionType.PERSON),
            self._make_result("某某集团", DetectionType.COMPANY),
        ]
        text = "李海富任职于某某集团。"
        masked = eng.mask_plain_text(text, results).text
        assert masked == "汤姆任职于A集团。"
        token_map = {x.token: x.original for x in eng.mappings}
        restored, hits = ReplacementEngine.unmask_text(masked, token_map)
        assert restored == text
        assert hits == {"汤姆", "A集团"}
