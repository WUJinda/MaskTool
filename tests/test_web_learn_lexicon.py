# -*- coding: utf-8 -*-
"""tests/test_web_learn_lexicon.py — "加入词库"核查对话框链路

覆盖：
- merge_words_into_lexicon：新建词库、追加去重、自定义类别、
  失败路径（路径指向目录）、空输入；
- _build_learn_candidates：同词多来源聚合去重、已存在词直接过滤、
  勾选驱动默认值、无勾选时回退全选、类别排序；
- 接线断言：按钮 → 全部已存在时直接提示不弹窗，否则打开核查对话框；
  对话框 → 只列新词 + 略过数标注 + 立即写库 + flash 反馈
  + 清空 learn_set；_save_learned_words 委托同一合并函数。
"""

import inspect
import yaml
from pathlib import Path

import pytest

from mask_tool.models.detection import (
    DetectionResult,
    DetectionStatus,
    DetectionType,
    Location,
)
from mask_tool.web.ui.service import merge_words_into_lexicon


def _result(text, cat=DetectionType.PERSON, source="ner", conf=0.8,
            file="报告.docx", status=DetectionStatus.SUGGEST_MASK):
    return DetectionResult(
        text=text, text_type=cat, source=source, confidence=conf,
        location=Location(file=file), context="", status=status,
    )


# ===========================================================================
# merge_words_into_lexicon：词库合并写入
# ===========================================================================

class TestMergeWordsIntoLexicon:

    def test_creates_missing_lexicon(self, tmp_path):
        """词库文件不存在 → 自动创建并写入，返回新词数与路径。"""
        path = tmp_path / "sub" / "lexicon.yaml"
        ok, new_count, msg = merge_words_into_lexicon(
            path, {"person": ["李四", "王五"]},
        )
        assert ok is True
        assert new_count == 2
        assert msg == str(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data == {"person": ["李四", "王五"]}

    def test_appends_and_dedups(self, tmp_path):
        """已有词库 → 追加新词；已存在词跳过不重复写。"""
        path = tmp_path / "lexicon.yaml"
        path.write_text(
            yaml.dump({"person": ["李四"]}, allow_unicode=True),
            encoding="utf-8",
        )
        ok, new_count, _ = merge_words_into_lexicon(
            path, {"person": ["李四", "王五"], "company": ["李四科技"]},
        )
        assert ok is True
        assert new_count == 2  # 王五 + 李四科技；李四已存在
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["person"] == ["李四", "王五"]
        assert data["company"] == ["李四科技"]

    def test_new_category_created(self, tmp_path):
        """写入词库中不存在的自定义类别 → 类别自动创建。"""
        path = tmp_path / "lexicon.yaml"
        path.write_text("person: []\n", encoding="utf-8")
        ok, new_count, _ = merge_words_into_lexicon(
            path, {"custom": ["专项债"]},
        )
        assert ok is True and new_count == 1
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["custom"] == ["专项债"]

    def test_empty_input_is_noop(self, tmp_path):
        """空 learned / 空词表 → 成功且零写入。"""
        path = tmp_path / "lexicon.yaml"
        ok, new_count, _ = merge_words_into_lexicon(path, {})
        assert ok is True and new_count == 0
        ok, new_count, _ = merge_words_into_lexicon(path, {"person": []})
        assert ok is True and new_count == 0
        assert not path.exists() or yaml.safe_load(
            path.read_text(encoding="utf-8")
        ) in (None, {"person": []})

    def test_path_is_directory_fails_gracefully(self, tmp_path):
        """词库路径指向目录 → ok=False 并带失败原因（不抛异常）。"""
        d = tmp_path / "lexicon.yaml"
        d.mkdir()
        ok, new_count, msg = merge_words_into_lexicon(d, {"person": ["李四"]})
        assert ok is False
        assert new_count == 0
        assert "失败" in msg or "无法" in msg

    def test_save_learned_words_delegates(self, tmp_path, monkeypatch):
        """_save_learned_words 委托同一合并函数（源码接线断言）。"""
        from mask_tool.web.ui import service as svc

        src = inspect.getsource(svc._save_learned_words)
        assert "merge_words_into_lexicon" in src


# ===========================================================================
# _build_learn_candidates：候选聚合
# ===========================================================================

class TestBuildLearnCandidates:

    def _page(self):
        """检测结果页模型：同词多来源/多文件 + 词库已有词。"""
        return [
            _result("李四", source="ner", conf=0.8, file="A.docx"),
            _result("李四", source="dictionary", conf=0.95, file="B.docx"),
            _result("李四科技", cat=DetectionType.COMPANY, source="regex",
                    conf=0.7, file="A.docx"),
        ]

    def test_aggregates_same_text_across_sources(self):
        """同词多来源/多文件 → 聚合为一条，来源合并、文件数统计。"""
        from mask_tool.web.ui.pages.masking import _build_learn_candidates

        out = _build_learn_candidates(
            self._page(), [0, 1, 2], {}, set(),
        )
        assert len(out) == 2
        li = next(c for c in out if c["text"] == "李四")
        assert li["category"] == DetectionType.PERSON.value  # 首现类别
        assert "NER" in li["sources"] and "词典" in li["sources"]
        assert li["file_count"] == 2
        assert li["confidence"] == 0.95  # 取最高置信度

    def test_existing_words_filtered_out(self):
        """已在词库的词直接不进候选列表（不再是“可勾选+标注”词条）。"""
        from mask_tool.web.ui.pages.masking import _build_learn_candidates

        out = _build_learn_candidates(
            self._page(), [0, 1, 2],
            selections={1: True},  # 李四被勾选也不例外
            existing_words={"李四"},
        )
        assert [c["text"] for c in out] == ["李四科技"]
        co = out[0]
        assert co["checked"] is False  # 未勾选行，保持不勾
        assert "exists" not in co      # exists 标注随过滤一并移除

    def test_all_existing_yields_empty_candidates(self):
        """全部候选已在词库 → 过滤后候选为空（对应不弹窗直接提示）。"""
        from mask_tool.web.ui.pages.masking import _build_learn_candidates

        out = _build_learn_candidates(
            self._page(), [0, 1, 2], {0: True, 2: True}, {"李四", "李四科技"},
        )
        assert out == []

    def test_fallback_all_checked_when_nothing_selected(self):
        """无任何勾选 → 回退为全部候选默认勾选（已存在词已被过滤）。"""
        from mask_tool.web.ui.pages.masking import _build_learn_candidates

        out = _build_learn_candidates(
            self._page(), [0, 1, 2], {}, {"李四科技"},
        )
        by_text = {c["text"]: c for c in out}
        assert set(by_text) == {"李四"}           # 已存在词不进候选
        assert by_text["李四"]["checked"] is True  # 剩余新词默认全勾

    def test_sorted_by_category_then_text(self):
        """输出按 (类别, 词文本) 排序，便于按类别核查。"""
        from mask_tool.web.ui.pages.masking import _build_learn_candidates

        page = [
            _result("乙", cat=DetectionType.COMPANY),
            _result("甲", cat=DetectionType.PERSON),
            _result("丙", cat=DetectionType.COMPANY),
        ]
        out = _build_learn_candidates(page, [0, 1, 2], {}, set())
        keys = [(c["category"], c["text"]) for c in out]
        assert keys == sorted(keys)


# ===========================================================================
# 交互接线：按钮 → 核查对话框 → 立即写库 + 反馈
# ===========================================================================

class TestWiring:

    def test_button_opens_review_dialog(self):
        """"加入词库"按钮：部分新词时打开核查对话框（快照候选 + 换 token）。"""
        from mask_tool.web.ui.pages import masking

        src = inspect.getsource(masking._render_masking_tab)
        assert "learn_candidates_idx" in src
        assert "learn_dialog_token" in src
        assert "_learn_to_lexicon_dialog(all_results, mode)" in src

    def test_button_skips_dialog_when_all_existing(self):
        """全部候选已在词库 → 按钮直接提示，不弹核查列表、不写库。"""
        from mask_tool.web.ui.pages import masking

        src = inspect.getsource(masking._render_masking_tab)
        # 预判用轻量路径读词库（与对话框同源锚点，不触发 _load_config）
        assert "resolve_user_lexicon_path" in src
        assert "_lexicon_existing_words" in src and "_read_lexicon_yaml" in src
        # 直接提示文案位于对话框触发（快照/token 赋值）之前：
        # 同一按钮 handler 内预判提示分支与弹窗分支互斥
        assert "均已在词库中，无需重复加入" in src
        assert src.index("均已在词库中") < src.index(
            'st.session_state["learn_candidates_idx"] = _view'
        )

    def test_dialog_lists_only_new_words_with_skip_note(self):
        """对话框只列新词：caption 注明略过数，无“✅ 已在词库”行内标注。"""
        from mask_tool.web.ui.pages import masking

        src = inspect.getsource(masking._learn_to_lexicon_dialog)
        assert "已在词库，已自动略过" in src
        assert "✅ 已在词库" not in src
        assert '"exists"' not in src

    def test_dialog_writes_immediately_and_feedbacks(self):
        """核查对话框确认后：立即写库 + learn_flash 反馈 + 清 learn_set。"""
        from mask_tool.web.ui.pages import masking

        src = inspect.getsource(masking._learn_to_lexicon_dialog)
        assert "merge_words_into_lexicon" in src          # 立即写库
        assert 'st.session_state["learn_flash"]' in src   # 主页反馈
        assert 'st.session_state.pop("learn_set", None)' in src  # 防重复写
        assert "existing_words" in src                    # 已存在核查（过滤新词）
        assert "st.rerun()" in src                        # 确认后关闭弹窗

    def test_dialog_function_registered(self):
        """对话框函数存在且被打 dialog 装饰（可从模块引用）。"""
        from mask_tool.web.ui.pages import masking

        assert callable(masking._learn_to_lexicon_dialog)
        assert callable(masking._build_learn_candidates)
        assert callable(masking._learn_category_options)

    def test_learn_set_untouched_by_button(self):
        """按钮不再直接改 user_selections/learn_set（旧静默行为移除）。"""
        from mask_tool.web.ui.pages import masking

        src = inspect.getsource(masking._render_masking_tab)
        # 旧的静默填充逻辑必须不存在（核查由对话框接管）
        assert 'st.session_state["learn_set"].add(i)' not in src

# ===========================================================================
