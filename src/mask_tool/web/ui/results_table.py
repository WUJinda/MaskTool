# -*- coding: utf-8 -*-
"""检测结果 → AgGrid DataFrame（列文案与 labels 对应）。"""
from pathlib import Path
from typing import List

import pandas as pd

from mask_tool.models.detection import DetectionResult

from .labels import SOURCE_LABELS, STATUS_LABELS, TYPE_LABELS

def _results_to_dataframe(results: List[DetectionResult]) -> pd.DataFrame:
    """将检测结果转为 DataFrame（P3：存在 AI 判定时附加「AI 判定」列）"""
    has_llm = any(r.llm_reason for r in results)
    rows = []
    for i, r in enumerate(results):
        row = {
            "序号": i + 1,
            "敏感信息": r.text,
            "类别": TYPE_LABELS.get(r.text_type, r.text_type.value),
            "类别值": r.text_type.value,
            "来源": SOURCE_LABELS.get(r.source, r.source),
            "置信度": r.confidence,
            "处置": STATUS_LABELS.get(r.status, r.status.value),
            "状态值": r.status.value,
            "文件": Path(r.location.file).name if r.location.file else "",
            "上下文": r.context,
        }
        if has_llm:  # 未启用 LLM 时不产生该列，避免空列噪声
            reason = r.llm_reason or ""
            row["AI 判定"] = reason[:44] + ("…" if len(reason) > 44 else "")
        rows.append(row)
    return pd.DataFrame(rows)

