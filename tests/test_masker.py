"""测试脱敏执行器"""

from mask_tool.core.masker import Masker
from mask_tool.core.tokenizer import TokenGenerator
from mask_tool.models.detection import (
    DetectionResult, DetectionType, DetectionStatus, Location,
)


class TestMasker:
    """脱敏执行器测试"""

    def setup_method(self):
        self.token_gen = TokenGenerator()
        self.masker = Masker(self.token_gen, irreversible=False)

    def test_reversible_masking(self):
        """测试可逆脱敏"""
        results = [
            DetectionResult(
                text="某某建设集团有限公司",
                text_type=DetectionType.COMPANY,
                source="dictionary",
                confidence=0.95,
                location=Location(file="test.docx"),
                status=DetectionStatus.AUTO_MASK,
            ),
        ]
        masked = self.masker.mask_text("甲方：某某建设集团有限公司", results)
        assert "某某建设集团有限公司" not in masked
        assert "[COMPANY_001]" in masked

    def test_irreversible_masking(self):
        """测试不可逆脱敏"""
        masker = Masker(TokenGenerator(), irreversible=True)
        results = [
            DetectionResult(
                text="某人甲",
                text_type=DetectionType.PERSON,
                source="dictionary",
                confidence=0.95,
                location=Location(file="test.docx"),
                status=DetectionStatus.AUTO_MASK,
            ),
        ]
        masked = masker.mask_text("负责人：某人甲", results)
        assert "某人甲" not in masked
        assert "***" in masked

    def test_hint_only_not_masked(self):
        """测试仅提示的项不被脱敏"""
        results = [
            DetectionResult(
                text="某人甲",
                text_type=DetectionType.PERSON,
                source="dictionary",
                confidence=0.50,
                location=Location(file="test.docx"),
                status=DetectionStatus.HINT_ONLY,
            ),
        ]
        masked = self.masker.mask_text("负责人：某人甲", results)
        assert "某人甲" in masked

    def test_multiple_matches(self):
        """测试多个匹配项"""
        results = [
            DetectionResult(
                text="某某建设集团有限公司",
                text_type=DetectionType.COMPANY,
                source="dictionary",
                confidence=0.95,
                location=Location(file="test.docx"),
                status=DetectionStatus.AUTO_MASK,
            ),
            DetectionResult(
                text="某人甲",
                text_type=DetectionType.PERSON,
                source="dictionary",
                confidence=0.95,
                location=Location(file="test.docx"),
                status=DetectionStatus.AUTO_MASK,
            ),
        ]
        masked = self.masker.mask_text("甲方：某某建设集团有限公司，负责人：某人甲", results)
        assert "[COMPANY_001]" in masked
        assert "[PERSON_001]" in masked

    def test_get_mappings(self):
        """测试获取映射关系"""
        results = [
            DetectionResult(
                text="某人甲",
                text_type=DetectionType.PERSON,
                source="dictionary",
                confidence=0.95,
                location=Location(file="test.docx"),
                status=DetectionStatus.AUTO_MASK,
            ),
        ]
        self.masker.mask_text("负责人：某人甲", results)
        mappings = self.masker.get_mappings()
        assert len(mappings) == 1
        assert mappings[0].token == "[PERSON_001]"
        assert mappings[0].original == "某人甲"

    def test_suggest_mask_not_replaced_by_default(self):
        """H6：SUGGEST_MASK 不再默认替换"""
        results = [
            DetectionResult(
                text="某人甲",
                text_type=DetectionType.PERSON,
                source="ner",
                confidence=0.70,
                location=Location(file="test.docx"),
                status=DetectionStatus.SUGGEST_MASK,
            ),
        ]
        masked = self.masker.mask_text("负责人：某人甲", results)
        assert masked == "负责人：某人甲"
        assert self.masker.get_mappings() == []

    def test_suggest_mask_replaced_with_explicit_statuses(self):
        """H6：显式传 statuses 含 SUGGEST_MASK 时替换（--all / --confirm 路径）"""
        results = [
            DetectionResult(
                text="某人甲",
                text_type=DetectionType.PERSON,
                source="ner",
                confidence=0.70,
                location=Location(file="test.docx"),
                status=DetectionStatus.SUGGEST_MASK,
            ),
        ]
        masked = self.masker.mask_text(
            "负责人：某人甲", results,
            statuses=frozenset({DetectionStatus.AUTO_MASK, DetectionStatus.SUGGEST_MASK}))
        assert "某人甲" not in masked
        assert "[PERSON_001]" in masked

    def test_allowed_originals_only_checked(self):
        """H6/确认模式：allowed_originals 只放行勾选项，未勾选的 AUTO 项也不替换"""
        results = [
            DetectionResult(
                text="某某建设集团有限公司",
                text_type=DetectionType.COMPANY,
                source="dictionary",
                confidence=0.95,
                location=Location(file="test.docx"),
                status=DetectionStatus.AUTO_MASK,
            ),
            DetectionResult(
                text="某人甲",
                text_type=DetectionType.PERSON,
                source="dictionary",
                confidence=0.95,
                location=Location(file="test.docx"),
                status=DetectionStatus.AUTO_MASK,
            ),
        ]
        masked = self.masker.mask_text(
            "某某建设集团有限公司 负责人：某人甲", results,
            allowed_originals={"某人甲"})
        assert "某某建设集团有限公司" in masked
        assert "某人甲" not in masked

    def test_overlapping_results_no_corruption(self):
        """N4/H1 根治：重叠词条不再产生嵌套乱码，长词优先完整替换"""
        results = [
            DetectionResult(
                text="集团",
                text_type=DetectionType.CUSTOM,
                source="ner",
                confidence=0.75,
                location=Location(file="test.docx"),
                status=DetectionStatus.AUTO_MASK,
            ),
            DetectionResult(
                text="某某建设集团有限公司",
                text_type=DetectionType.COMPANY,
                source="dictionary",
                confidence=0.95,
                location=Location(file="test.docx"),
                status=DetectionStatus.AUTO_MASK,
            ),
        ]
        masked = self.masker.mask_text("甲方：某某建设集团有限公司", results)
        assert masked == "甲方：[COMPANY_001]"
        assert "[CUSTOM" not in masked  # 短词被消解，无嵌套残片

    def test_engine_and_mappings_shared(self):
        """Masker.mappings 与 engine.mappings 共享同一列表（兼容 adapter 侧 append）"""
        assert self.masker.mappings is self.masker.engine.mappings
        results = [
            DetectionResult(
                text="某人甲",
                text_type=DetectionType.PERSON,
                source="dictionary",
                confidence=0.95,
                location=Location(file="test.docx"),
                status=DetectionStatus.AUTO_MASK,
            ),
        ]
        self.masker.mask_text("负责人：某人甲", results)
        m = self.masker.get_mappings()[0]
        assert m.batch_id == ""
        assert m.fingerprint
        assert m.kind == "text"

    def test_amount_mode_fuzzy(self):
        """M5：Masker 委托 engine 的 fuzzy 金额模式"""
        masker = Masker(TokenGenerator(), amount_mode="fuzzy")
        results = [
            DetectionResult(
                text="1.2亿元",
                text_type=DetectionType.AMOUNT,
                source="regex",
                confidence=0.80,
                location=Location(file="test.docx"),
                status=DetectionStatus.AUTO_MASK,
            ),
        ]
        masked = masker.mask_text("合同金额：1.2亿元", results)
        assert "1亿+" in masked
        assert masker.get_mappings() == []
