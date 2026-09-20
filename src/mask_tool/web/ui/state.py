# -*- coding: utf-8 -*-
"""session_state 管理：任务态键约定与清理、AgGrid 勾选同步、自定义词解析。

任务态/配置态键语义见 TASK_STATE_KEYS 注释。
"""
import re
import shutil
from typing import Dict, List, Set, Tuple

import streamlit as st

from mask_tool.models.detection import DetectionResult

def _dedup_results(results: List[DetectionResult]) -> List[DetectionResult]:
    """跨文件去重：按(text, text_type)去重"""
    seen: Set[Tuple[str, str]] = set()
    deduped = []
    for r in results:
        key = (r.text, r.text_type.value)
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    return deduped


def _parse_custom_words(text: str) -> List[str]:
    """解析临时自定义敏感词输入（I6）：换行/中英文逗号分隔，
    去空白、去空项、去重（保持首次出现顺序）。空输入返回 []。"""
    if not text:
        return []
    words: List[str] = []
    seen: Set[str] = set()
    for part in re.split(r"[\n,，]+", text):
        w = part.strip()
        if w and w not in seen:
            seen.add(w)
            words.append(w)
    return words


# 任务态会话键：检测/勾选/产物/结果页筛选与批次信息，新建任务时全清。
# 配置态键（运行模式/NER/不可逆/学习词库/文件名脱敏开关、custom_words_input、
# manual_only_mode、learn_set）不在此列，跨任务保留。
TASK_STATE_KEYS = [
    "detection_results", "file_results", "user_selections",
    "tmp_dir", "saved_paths", "mask_result", "restore_result",
    "filter_type", "filter_status", "filter_source", "filter_file",
    "search_text", "batch_name_input", "batch_id_display",
    "task_kind", "zip_tree_root", "zip_blocked_files",
    "dir_zip_upload", "restore_zip_upload",
]


def _clear_task_state(clear_upload: bool = True) -> None:
    """清空任务态会话键并删除上传临时目录（配置态键保留）。

    - R1-B5：tmp_dir 指向的上传临时目录（含敏感信息副本）一并删除
    - clear_upload=True 同时 pop 上传组件 key（file_uploader），
      回到初始上传页；False 保留已上传文件（同批文件重走流程语义）
    """
    old_tmp = st.session_state.get("tmp_dir")
    if old_tmp:
        shutil.rmtree(old_tmp, ignore_errors=True)
    keys = list(TASK_STATE_KEYS)
    if clear_upload:
        keys.append("file_uploader")
    for key in keys:
        st.session_state.pop(key, None)


def _reset_task_state() -> None:
    """新建任务：任务态全清（检测/勾选/产物/上传文件），配置态保留。

    保留：侧边栏设置（运行模式/NER/不可逆/学习词库/同时脱敏文件名）、
    临时自定义敏感词（custom_words_input）、仅手动开关（manual_only_mode）、
    learn_set（新一轮检测开始时旧索引自动失效，见 _run_detection）。
    三个入口（侧边栏/重新上传/开始新任务）统一走本函数。
    """
    _clear_task_state(clear_upload=True)


def _apply_grid_selection(filtered_indices: List[int], selected_rows,
                          selections: Dict[int, bool]) -> bool:
    """AgGrid 回传同步到 user_selections（I6 问题3）。

    selected_rows 支持两种形态（streamlit-aggrid 1.x 实际返回结构）：
      - DataFrame / list[dict]，含 index 列：
        · 带“选择”列（VALUE_CHANGED 回传的编辑后数据）：按该列布尔值同步；
        · 无“选择”列（SELECTION_CHANGED 回传的选中行）：在集合中即为选中。
      - None：无回传，返回 False。
    仅同步当前筛选可见行（filtered_indices），不可见行勾选态不受影响。
    返回是否有变化（调用方据此 rerun 刷新“即将脱敏”列表）。
    """
    if selected_rows is None:
        return False
    if hasattr(selected_rows, "to_dict"):  # pandas DataFrame
        rows = selected_rows.to_dict("records")
    else:
        rows = list(selected_rows)
    checked: Dict[int, bool] = {}
    has_sel_col = any("选择" in r for r in rows if isinstance(r, dict))
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            idx = int(row.get("index", -1))
        except (TypeError, ValueError):
            continue
        if idx < 0:
            continue
        checked[idx] = bool(row.get("选择", True)) if has_sel_col else True
    changed = False
    for i in filtered_indices:
        new_val = checked.get(i, False)
        if selections.get(i) != new_val:
            selections[i] = new_val
            changed = True
    return changed


def _final_selected_indices(all_results, selections: Dict[int, bool]) -> List[int]:
    """"即将脱敏"确认列表：按 user_selections 过滤的全局索引。

    检测表格任何勾选变化后，本函数的输出与计数立即一致（I6 问题3）。
    """
    return [
        i for i in range(len(all_results)) if selections.get(i, False)
    ]

