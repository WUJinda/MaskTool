"""测试检测引擎"""

from mask_tool.core.detector import Detector
from mask_tool.models.detection import DetectionType


class TestDetector:
    """检测引擎测试"""

    def setup_method(self):
        lexicon = {
            "company": ["某某建设集团有限公司", "华为技术有限公司"],
            "person": ["某人甲"],
            "project": ["某某新区基础设施建设项目"],
            "location": ["某某市某某区"],
        }
        whitelist = {"有限公司"}
        self.detector = Detector(lexicon, whitelist)

    def test_dictionary_detection(self):
        """测试词库匹配"""
        results = self.detector.detect("甲方：某某建设集团有限公司")
        assert len(results) >= 1
        # "有限公司"在白名单中，不应单独匹配
        company_results = [r for r in results if r.text == "某某建设集团有限公司"]
        assert len(company_results) == 1
        assert company_results[0].text_type == DetectionType.COMPANY
        assert company_results[0].source == "dictionary"
        assert company_results[0].confidence == 0.95

    def test_regex_amount_detection(self):
        """测试金额正则匹配"""
        results = self.detector.detect("合同金额：1.2亿元")
        amount_results = [r for r in results if r.text_type == DetectionType.AMOUNT]
        assert len(amount_results) >= 1

    def test_regex_phone_detection(self):
        """测试手机号正则匹配"""
        results = self.detector.detect("联系电话：13812345678")
        phone_results = [r for r in results if "13812345678" in r.text]
        assert len(phone_results) == 1

    def test_regex_id_card_detection(self):
        """测试身份证号正则匹配"""
        results = self.detector.detect("身份证号：110101199001011234")
        id_results = [r for r in results if "110101199001011234" in r.text]
        assert len(id_results) >= 1
        # 身份证正则（17位+X）应匹配到
        id_card_hits = [r for r in id_results if r.confidence == 0.85]
        assert len(id_card_hits) == 1

    def test_whitelist_filtering(self):
        """测试白名单过滤"""
        results = self.detector.detect("某某建设集团有限公司")
        # "有限公司"不应作为独立匹配项
        standalone_results = [r for r in results if r.text == "有限公司"]
        assert len(standalone_results) == 0

    def test_empty_text(self):
        """测试空文本"""
        results = self.detector.detect("")
        assert len(results) == 0

    def test_no_matches(self):
        """测试无匹配文本"""
        results = self.detector.detect("这是一段普通文本，没有任何敏感信息。")
        assert len(results) == 0

    def test_location_info(self):
        """测试位置信息记录"""
        results = self.detector.detect("某人甲", file_path="test.docx")
        assert len(results) == 1
        assert results[0].location.file == "test.docx"

    def test_context_extraction(self):
        """测试上下文提取"""
        text = "甲方：某某建设集团有限公司，乙方：华为技术有限公司"
        results = self.detector.detect(text)
        for r in results:
            assert len(r.context) > 0

    def test_m6_19_digit_is_single_bank_card(self):
        """M6：19位数字串恰出1条银行卡，不再被身份证截断产生重叠"""
        results = self.detector.detect("卡号：1234567890123456789")
        hits = [r for r in results if "1234567890123456789" in r.text]
        assert len(hits) == 1
        assert hits[0].text == "1234567890123456789"
        assert hits[0].confidence == 0.65  # 银行卡规则

    def test_m6_plus86_phone_single_hit(self):
        """M6：+8613812345678 整体恰出1条手机号"""
        results = self.detector.detect("电话：+8613812345678")
        hits = [r for r in results if "13812345678" in r.text]
        assert len(hits) == 1
        assert hits[0].text == "+8613812345678"
        assert hits[0].confidence == 0.85

    def test_m6_12_digit_no_hit(self):
        """M6：12位数字串（手机号后带一位）0条命中"""
        results = self.detector.detect("号码：1381234567890")
        assert len(results) == 0

    def test_m6_18_digit_is_single_id_card(self):
        """M6：18位纯数字同文本按规则顺序去重为身份证一条"""
        results = self.detector.detect("身份证：110101199001011234")
        hits = [r for r in results if "110101199001011234" in r.text]
        assert len(hits) == 1
        assert hits[0].confidence == 0.85  # 身份证规则，银行卡同文本被去重

    def test_m6_rmb_rule_before_generic_amount(self):
        """M6：人民币规则前移，整段优先命中"""
        results = self.detector.detect("合同约定人民币 1.2亿，分期支付")
        texts = [r.text for r in results]
        assert "人民币 1.2亿" in texts
        rmb = next(r for r in results if r.text == "人民币 1.2亿")
        assert rmb.confidence == 0.90
        # 通用金额规则同时命中子串（不同文本不去重），由引擎长度优先消解
        assert "1.2亿" in texts
