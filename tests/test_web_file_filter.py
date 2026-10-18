# -*- coding: utf-8 -*-
"""检测结果「文件」筛选口径回归测试（I7）。

背景：文件筛选此前按 basename 比较，与 service._run_detection 的
file_results 登记口径（zip 任务=相对路径）不一致：
- zip 子目录文件永远筛不出（0 结果）；
- 不同子目录同名文件互相串结果；
- 批量按钮（全选/清空/反选/仅自动/仅建议）先于搜索生效，与表格可见
  视图不同口径。

修复引入 `_result_file_key` 纯函数统一口径；本文件验证其语义。
"""
from pathlib import Path

from mask_tool.models.detection import (
    DetectionResult,
    DetectionStatus,
    DetectionType,
    Location,
)
from mask_tool.web.ui.pages.masking import _result_file_key


def _result(file: str) -> DetectionResult:
    return DetectionResult(
        text="张三", text_type=DetectionType.PERSON, source="ner",
        confidence=0.9, location=Location(file=file),
        context="", status=DetectionStatus.SUGGEST_MASK,
    )


class TestResultFileKey:

    def test_single_file_uses_basename(self):
        """单文件任务：location.file 为落盘全路径 → 键=文件名。"""
        r = _result(str(Path("C:/tmp/masktool") / "报告.docx"))
        assert _result_file_key(r, "files", "") == "报告.docx"

    def test_zip_uses_relative_posix_path(self):
        """zip 任务：键=相对 zip 根的 posix 相对路径（与登记口径一致）。"""
        root = Path("C:/tmp/masktool/zip_root")
        r = _result(str(root / "子目录" / "报告.docx"))
        assert _result_file_key(r, "zip", str(root)) == "子目录/报告.docx"

    def test_zip_same_name_dirs_isolated(self):
        """不同子目录同名文件 → 键互不相同，筛选不再互串。"""
        root = Path("C:/tmp/masktool/zip_root")
        r1 = _result(str(root / "dir1" / "a.docx"))
        r2 = _result(str(root / "dir2" / "a.docx"))
        k1 = _result_file_key(r1, "zip", str(root))
        k2 = _result_file_key(r2, "zip", str(root))
        assert k1 == "dir1/a.docx"
        assert k2 == "dir2/a.docx"
        assert k1 != k2

    def test_zip_root_escape_falls_back_to_basename(self):
        """location.file 不在 zip 根下（历史会话残留等）→ 回退文件名。"""
        r = _result("D:/elsewhere/a.docx")
        assert _result_file_key(r, "zip", "C:/tmp/masktool/zip_root") == "a.docx"

    def test_empty_location_file(self):
        """location.file 为空 → 空串（表格显示空单元格）。"""
        assert _result_file_key(_result(""), "files", "") == ""


class TestFileFilterConsistency:
    """筛选比较与 file_results 登记键必须一一对应：不漏筛、不串文件。"""

    def test_every_result_matches_exactly_one_key(self, tmp_path):
        """模拟 zip 任务：构造两文件（含子目录同名文件）的检测结果，
        按每个登记键筛选，命中集合必须恰好等于该文件的条目。"""
        from mask_tool.web.ui.service import _dedup_results  # noqa: F401
        from mask_tool.models.detection import DetectionType

        root = tmp_path / "zip_root"
        files = {
            "docs/报告.docx": ["张三"],
            "docs/a.docx": ["李四"],
            "财务/a.docx": ["王五"],
        }
        all_results = []
        file_results = {}
        for rel, words in files.items():
            fp = root / rel
            results = [
                DetectionResult(
                    text=w, text_type=DetectionType.PERSON, source="ner",
                    confidence=0.9, location=Location(file=str(fp)),
                    context="", status=DetectionStatus.SUGGEST_MASK,
                )
                for w in words
            ]
            all_results.extend(results)
            file_results[rel] = results  # 与 service 同口径登记

        for key, expected in file_results.items():
            view = [
                r for r in all_results
                if _result_file_key(r, "zip", str(root)) == key
            ]
            # 恰好命中该文件自身条目（修复前：子目录文件 0 命中，
            # 同名 a.docx 互串）
            assert len(view) == len(expected), (key, view)
            assert {r.text for r in view} == {r.text for r in expected}
