"""检测引擎 - 正则规则 + 词典匹配 + NER"""

import re
from typing import Dict, List, Optional, Set

from mask_tool.models.detection import DetectionResult, DetectionType, Location


class Detector:
    """敏感信息检测引擎"""

    def __init__(self, lexicon: Dict[str, List[str]], whitelist: Set[str],
                 ner_engine=None, manual_words: Optional[List[str]] = None,
                 regex_enabled: bool = True):
        """
        Args:
            lexicon: 词库，key为类别名，value为敏感词列表
            whitelist: 白名单，这些词不会被匹配
            ner_engine: NER引擎实例（可选），如JiebaNER
            manual_words: 临时手动词（Web 自定义敏感词，仅本次任务生效）。
                最高优先级精确匹配：source="manual"、置信度 0.95（与词典同档）、
                类别 CUSTOM；不受 whitelist 过滤（用户显式指定优先于白名单）；
                与词典/NER/正则命中同一文本时去重保留手动项
            regex_enabled: 正则通道开关（仅手动模式关闭；默认 True 行为不变）
        """
        self.lexicon = lexicon
        self.whitelist = whitelist
        self.ner_engine = ner_engine
        # 手动词清洗：去空/去重（保持输入顺序），不去 whitelist（见 docstring）
        self.manual_words: List[str] = []
        for w in manual_words or []:
            if w and w not in self.manual_words:
                self.manual_words.append(w)
        self._regex_rules: List[tuple] = (
            self._build_regex_rules() if regex_enabled else []
        )
        self._lexicon_patterns: List[tuple] = self._build_lexicon_patterns()

    def _build_regex_rules(self) -> List[tuple]:
        """构建正则规则列表: [(compiled_regex, DetectionType, confidence), ...]

        边界约束（M6）：手机/身份证/日期/银行卡加 (?<!\d) 前视与 (?!\d) 后顾，
        消除长数字串被多条规则截断产生重叠区间；金额通用规则加 (?<![\d.])
        且以数字起头。规则顺序：人民币在前，同文本命中时优先去重保留人民币整段。
        """
        rules = [
            # 人民币金额（调到通用金额之前，"人民币 1.2亿"优先整体命中）
            (re.compile(r'人民币\s*[\d,]+\.?\d*[万亿]?元?'), DetectionType.AMOUNT, 0.90),
            # 金额: 1.2亿, 500万, 1,234.56万亿（不以数字/小数点结尾的边界起头）
            (re.compile(r'(?<![\d.])\d[\d,]*\.?\d*[万亿]元?'), DetectionType.AMOUNT, 0.80),
            # 手机号（可带 +86 前缀，前后不能是数字）
            (re.compile(r'(?<!\d)(?:\+?86)?1[3-9]\d{9}(?!\d)'), DetectionType.CUSTOM, 0.85),
            # 身份证号（前后不能是数字）
            (re.compile(r'(?<!\d)\d{17}[\dXx](?!\d)'), DetectionType.CUSTOM, 0.85),
            # 邮箱
            (re.compile(r'[\w.-]+@[\w.-]+\.\w+'), DetectionType.CUSTOM, 0.75),
            # 日期（前后不能是数字）
            (re.compile(r'(?<!\d)\d{4}年\d{1,2}月\d{1,2}日(?!\d)'), DetectionType.CUSTOM, 0.70),
            # 银行卡号（前后不能是数字；在身份证之后，18位纯数字按规则顺序去重为身份证）
            (re.compile(r'(?<!\d)\d{16,19}(?!\d)'), DetectionType.CUSTOM, 0.65),
            # 分隔形态卡号（4-4-4-4[/-3]，如 6222 0202 0000 1234 5678；
            # 不与日期 2026-09-16 冲突：该模式要求 4 组以上 4 位分隔）
            (re.compile(r'(?<!\d)\d{4}(?:[ -]\d{4}){3}(?:[ -]\d{3})?(?!\d)'),
             DetectionType.CUSTOM, 0.65),
        ]
        return rules

    def _build_lexicon_patterns(self) -> List[tuple]:
        """构建词库匹配模式列表: [(text, DetectionType, confidence), ...]"""
        patterns = []
        type_map = {
            "company": DetectionType.COMPANY,
            "government": DetectionType.GOVERNMENT,
            "person": DetectionType.PERSON,
            "project": DetectionType.PROJECT,
            "subject": DetectionType.SUBJECT,
            "location": DetectionType.LOCATION,
            "amount": DetectionType.AMOUNT,
            "custom": DetectionType.CUSTOM,
        }
        for category, words in self.lexicon.items():
            det_type = type_map.get(category, DetectionType.CUSTOM)
            for word in words:
                if word not in self.whitelist:
                    patterns.append((word, det_type, 0.95))
        return patterns

    def detect(self, text: str, file_path: str = "") -> List[DetectionResult]:
        """
        对文本执行敏感信息检测

        检测优先级：词典 > NER > 正则
        （词典匹配的置信度最高，优先使用；NER补充词典未覆盖的实体）

        Args:
            text: 待检测文本
            file_path: 文件路径（用于Location）

        Returns:
            检测结果列表（已去重）
        """
        results: List[DetectionResult] = []
        seen_texts: Set[str] = set()

        # 0. 手动词（最高优先级；与词典同档置信度 0.95，source="manual"）。
        # 先于词典执行：同文本与词典/NER/正则命中时，seen_texts 去重保留手动项
        for word in self.manual_words:
            if word in text and word not in seen_texts:
                seen_texts.add(word)
                idx = text.index(word)
                start = max(0, idx - 50)
                end = min(len(text), idx + len(word) + 50)
                context = text[start:end]
                results.append(DetectionResult(
                    text=word,
                    text_type=DetectionType.CUSTOM,
                    source="manual",
                    confidence=0.95,
                    location=Location(file=file_path),
                    context=context,
                ))

        # 1. 词库匹配（最高优先级，置信度0.95）
        for word, det_type, confidence in self._lexicon_patterns:
            if word in text and word not in seen_texts:
                seen_texts.add(word)
                idx = text.index(word)
                start = max(0, idx - 50)
                end = min(len(text), idx + len(word) + 50)
                context = text[start:end]
                results.append(DetectionResult(
                    text=word,
                    text_type=det_type,
                    source="dictionary",
                    confidence=confidence,
                    location=Location(file=file_path),
                    context=context,
                ))

        # 2. NER识别（补充词典未覆盖的实体）
        if self.ner_engine and self.ner_engine.is_available():
            ner_results = self.ner_engine.recognize(text, file_path)
            for r in ner_results:
                if r.text not in seen_texts:
                    seen_texts.add(r.text)
                    results.append(r)

        # 3. 正则匹配（最低优先级，用于数字/日期等模式）
        for regex, det_type, confidence in self._regex_rules:
            for match in regex.finditer(text):
                matched_text = match.group()
                if matched_text not in self.whitelist and matched_text not in seen_texts:
                    seen_texts.add(matched_text)
                    start = max(0, match.start() - 50)
                    end = min(len(text), match.end() + 50)
                    context = text[start:end]
                    results.append(DetectionResult(
                        text=matched_text,
                        text_type=det_type,
                        source="regex",
                        confidence=confidence,
                        location=Location(file=file_path),
                        context=context,
                    ))

        return results
