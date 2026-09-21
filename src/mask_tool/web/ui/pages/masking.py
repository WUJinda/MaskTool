# -*- coding: utf-8 -*-
"""脱敏处理 tab：上传区/步骤条/临时自定义词/检测结果确认（AgGrid）/执行与结果页。"""
import json
from pathlib import Path
from typing import Dict
from uuid import uuid4

import streamlit as st
import pandas as pd
import yaml

from mask_tool.models.detection import DetectionStatus

from ..artifacts import _mask_result_artifacts, _render_save_to_dir_panel
from ..files import _file_icon
from ..history import _generate_batch_id
from ..labels import (
    BLOCKED_EXTS,
    SOURCE_LABELS,
    STATUS_LABELS,
    SUPPORTED_MASK_EXTS,
    TYPE_LABELS,
)
from ..lexicon_io import _merge_words_into_whitelist
from ..service import _run_detection, _run_masking
from ..state import (
    _apply_grid_selection,
    _clear_task_state,
    _final_selected_indices,
    _parse_custom_words,
    _reset_task_state,
)

# ──────────────────────────────────────────────
# 步骤指示器
# ──────────────────────────────────────────────

def _loading_overlay(title: str, sub: str):
    """全屏 loading 覆盖层：脚本阻塞执行期间显示，rerun 后随元素消失自动移除。"""
    st.markdown(
        f'<div class="mt-loading"><div class="mt-load-card">'
        f'<div class="mt-load-ring"></div>'
        f'<div class="mt-load-title">{title}</div>'
        f'<div class="mt-load-sub">{sub}</div>'
        f'<div class="mt-load-bar"></div>'
        f'</div></div>',
        unsafe_allow_html=True,
    )


def render_steps(current_step: int):
    """渲染步骤指示器 (1-4；current_step=5 表示全部完成)：
    节点连线式（数字圆点 + 渐变连线），卡片化 sticky 顶部"""
    steps = [
        ("1", "📤 上传"),
        ("2", "🔍 检测"),
        ("3", "✅ 确认选择"),
        ("4", "💾 执行脱敏"),
    ]
    step_html = '<div class="step-indicator">'
    for i, (num, label) in enumerate(steps):
        step_num = i + 1
        cls = "done" if step_num < current_step else ("active" if step_num == current_step else "pending")
        step_html += f'<span class="step {cls}"><span class="n">{num}</span>{label}</span>'
        if i < len(steps) - 1:
            line_cls = "done" if step_num < current_step else ""
            step_html += f'<span class="step-line {line_cls}"></span>'
    step_html += '</div>'
    st.markdown(step_html, unsafe_allow_html=True)


def _current_flow_step() -> int:
    """全局流程步骤（顶部固定步骤条的状态源）。

    由会话状态推导而非渲染位置（页面各区同轮全部渲染，位置无法表征进度）：
      1 = 待上传；3 = 检测完成、确认选择中；5 = 已执行完成（结果页）。
    """
    if "mask_result" in st.session_state:
        return 5
    if st.session_state.get("detection_results") is not None:
        return 3
    return 1


# ──────────────────────────────────────────────
# 标签页1：脱敏处理
# ──────────────────────────────────────────────

def _render_masking_tab(mode: str, ner_enabled: bool, irreversible: bool, learn_words: bool):
    """渲染脱敏处理标签页"""

    # 如果已有脱敏结果，展示结果页面
    if "mask_result" in st.session_state:
        _render_mask_result()
        return

    # ── Step 1: 文件上传 ──（唯一步骤条：固定顶部，下滑始终可见）
    render_steps(_current_flow_step())

    # 上传区并排：左单文件 / 右目录 zip（压缩纵向占用，比例 1.35:1）
    up_cols = st.columns([1.35, 1])
    with up_cols[0]:
        st.markdown(
            '<div class="up-title">📄 <b>单文件上传</b>'
            '<span>支持多选 .docx / .xlsx</span></div>',
            unsafe_allow_html=True,
        )
        uploaded_files = st.file_uploader(
            "上传待脱敏文件",
            type=sorted(ext.lstrip(".") for ext in SUPPORTED_MASK_EXTS),
            accept_multiple_files=True,
            label_visibility="collapsed",
            key="file_uploader",  # I6：新建任务时 pop 本 key 清空已传文件
        )
    with up_cols[1]:
        st.markdown(
            '<div class="up-title">🗂️ <b>目录压缩包</b>'
            '<span>产物为同结构目录 zip</span></div>',
            unsafe_allow_html=True,
        )
        # I6 问题2：目录上传入口——把目录压缩成 zip（解压后按目录模式处理，
        # 产物为同结构目录的 zip）
        dir_zip = st.file_uploader(
            "或上传目录压缩包（.zip）",
            type=["zip"],
            key="dir_zip_upload",
            label_visibility="collapsed",
            help=("把整个目录压缩成 zip 后上传：递归处理其中的 docx/xlsx，"
                  "pptx/pdf 等屏蔽类型会警告且不进产物；脱敏产物为同结构"
                  "目录的 zip 压缩包"),
        )
    if dir_zip is not None and uploaded_files:
        st.info("已同时上传单文件与目录 zip：本次按目录 zip 处理，单文件列表忽略")

    if not uploaded_files:
        st.markdown(
            '<div class="notice"><span class="ic">📤</span>'
            '<div class="tx">请上传需要脱敏的文件（支持 <b>.docx / .xlsx</b>）'
            '<small>PPT 与 PDF 已暂时停用：检测到 .pptx / .pdf 文件将拒绝处理</small>'
            '</div></div>',
            unsafe_allow_html=True,
        )
        return

    # 屏蔽策略双保险：uploader 已限类型，此处对绕过途径（API 调用等）给出明确报错
    blocked_msgs = [
        f"{f.name}：{BLOCKED_EXTS.get(Path(f.name).suffix.lower(), '该格式暂不支持脱敏')}"
        for f in uploaded_files if Path(f.name).suffix.lower() in BLOCKED_EXTS
    ]
    if blocked_msgs:
        st.error(
            "以下文件暂不支持脱敏，请移除后重试（当前仅支持 Word .docx / Excel .xlsx）：\n\n"
            + "\n\n".join(blocked_msgs)
        )
        return

    # 显示已上传文件（紧凑 chips，代替卡片网格）
    chips_html = "".join(
        f'<span class="fchip"><span class="fi">{_file_icon(Path(f.name).suffix)}</span>'
        f'<span class="fn">{f.name}</span>'
        f'<span class="fs">{f.size / 1024:.1f} KB</span></span>'
        for f in uploaded_files
    )
    st.markdown(f'<div class="filelist">{chips_html}</div>', unsafe_allow_html=True)

    # ── Step 2: 检测分析 ──
    st.markdown("---")

    # 输入区双栏：左 = 临时敏感词输入（加高）；右 = 本次任务选项（2.3 比例优化）
    mi_cols = st.columns([1, 0.42])
    with mi_cols[0]:
        # I6：临时自定义敏感词（仅本次任务生效，不写入词库文件）
        st.markdown(
            '<div class="mi-label"><b>✍️ 临时自定义敏感词</b>'
            '<span class="badge">仅本次任务生效</span></div>',
            unsafe_allow_html=True,
        )
        custom_words_text = st.text_area(
            "临时自定义敏感词",
            placeholder=("每行一个词，或用逗号分隔；仅本次任务生效，不写入词库。\n"
                         "例：某某科技有限公司，张三丰，2026年Q3财报"),
            key="custom_words_input",
            height=118,
            label_visibility="collapsed",
        )
    with mi_cols[1]:
        # 同时脱敏文件名（仅处理主名，不改扩展名）
        mask_filenames = st.checkbox(
            "同时脱敏文件名",
            value=True,
            key="mask_filenames",
            help="对文件主名（不含扩展名）执行同样的检测与替换，Token 与正文共享",
        )
        manual_only = st.checkbox(
            "仅脱敏我指定的词（跳过自动检测）",
            value=False,
            key="manual_only_mode",
            help=("开启后完全跳过自动检测（NER/正则/词库均不运行），"
                  "检测与脱敏只处理左侧手动指定的词；适合自动检测误报多、"
                  "只想针对性脱敏的场景"),
        )

    if st.button("🔍 开始检测", type="primary", width="stretch"):
        _loading_overlay("正在分析，检测敏感信息...", "大文件 / 多文件可能需要几十秒，请勿关闭窗口")
        _run_detection(
                [] if dir_zip is not None else uploaded_files,
                mode, ner_enabled,
                manual_words=_parse_custom_words(custom_words_text),
                manual_only=manual_only,
                zip_file=dir_zip,
            )

    # 检查是否已有检测结果
    if "detection_results" not in st.session_state:
        return

    all_results = st.session_state["detection_results"]
    file_results = st.session_state["file_results"]

    # I6 问题2：目录 zip 中的屏蔽类型（pptx/pdf 等）——警告且不进产物
    zip_blocked = st.session_state.get("zip_blocked_files") or []
    if zip_blocked:
        st.warning(
            "以下 " + str(len(zip_blocked)) + " 个屏蔽类型文件不会进入脱敏产物"
            "（与 CLI 目录模式语义一致）：\n\n- " + "\n- ".join(zip_blocked)
        )

    if not all_results:
        st.success("✅ 未检测到敏感信息，文件安全！")
        return

    # ── 检测结果概览：三段式（总数｜处置分布｜类别数量条）──
    auto_count = sum(1 for r in all_results if r.status == DetectionStatus.AUTO_MASK)
    suggest_count = sum(1 for r in all_results if r.status == DetectionStatus.SUGGEST_MASK)
    hint_count = sum(1 for r in all_results if r.status == DetectionStatus.HINT_ONLY)

    # 类别分布（按数量降序）
    type_counts: Dict[str, int] = {}
    for r in all_results:
        label = TYPE_LABELS.get(r.text_type, r.text_type.value)
        type_counts[label] = type_counts.get(label, 0) + 1
    sorted_counts = sorted(type_counts.items(), key=lambda x: -x[1])
    max_val = max(type_counts.values()) if type_counts else 1

    cats_html = "".join(
        f'<div class="ov-bar-row"><span class="ov-bar-label">{label}</span>'
        f'<div class="ov-bar-track"><div class="ov-bar-fill" style="width:{int(count / max_val * 100)}%"></div></div>'
        f'<span class="ov-bar-num">{count}</span></div>'
        for label, count in sorted_counts
    )
    st.markdown(
        f'<div class="ov">'
        f'<div class="ov-total"><span class="n">{len(all_results)}</span><span class="t">处敏感信息</span></div>'
        f'<div class="ov-sep"></div>'
        f'<div class="ov-breakdown">'
        f'<span class="ov-item auto">✅ <b>{auto_count}</b> 自动脱敏</span>'
        f'<span class="ov-item sugg">⚠️ <b>{suggest_count}</b> 建议脱敏</span>'
        f'<span class="ov-item hint">ℹ️ <b>{hint_count}</b> 仅提示</span>'
        f'</div>'
        f'<div class="ov-sep"></div>'
        f'<div class="ov-cats">{cats_html}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── Step 3: 确认选择 ──
    st.markdown("---")

    # 初始化选择状态
    _render_llm_banner()

    if "user_selections" not in st.session_state:
        # 默认：自动脱敏和建议脱敏的项都勾选
        st.session_state["user_selections"] = {
            i: (r.status in (DetectionStatus.AUTO_MASK, DetectionStatus.SUGGEST_MASK))
            for i, r in enumerate(all_results)
        }

    # 筛选工具条：行一 = 四个下拉；行二 = 搜索 + 批量操作（原型 filter-bar-preview）。
    # 卡片容器/控件样式由 app.css 以 aria-label :has 锚定渲染。
    filter_cols = st.columns(4)
    with filter_cols[0]:
        filter_type = st.selectbox(
            "类别",
            options=["全部类别"] + list(type_counts.keys()),
            key="filter_type",
        )
    with filter_cols[1]:
        filter_status = st.selectbox(
            "状态",
            options=["全部状态", "✅ 自动脱敏", "⚠️ 建议脱敏", "ℹ️ 仅提示"],
            key="filter_status",
        )
    with filter_cols[2]:
        filter_source = st.selectbox(
            "来源",
            options=["全部来源", "✍️ 手动", "📘 词典", "⚙️ NER", "🔍 正则", "📄 文件名"],
            key="filter_source",
        )
    with filter_cols[3]:
        filter_file = st.selectbox(
            "文件",
            options=["全部文件"] + list(file_results.keys()),
            key="filter_file",
        )

    # 应用筛选
    filtered_indices = []
    for i, r in enumerate(all_results):
        # 类别筛选
        if filter_type != "全部类别":
            if TYPE_LABELS.get(r.text_type, r.text_type.value) != filter_type:
                continue
        # 状态筛选
        if filter_status != "全部状态":
            status_map = {
                "✅ 自动脱敏": DetectionStatus.AUTO_MASK,
                "⚠️ 建议脱敏": DetectionStatus.SUGGEST_MASK,
                "ℹ️ 仅提示": DetectionStatus.HINT_ONLY,
            }
            if r.status != status_map.get(filter_status):
                continue
        # 来源筛选
        if filter_source != "全部来源":
            source_map = {
                "✍️ 手动": "manual",
                "📘 词典": "dictionary",
                "⚙️ NER": "ner",
                "🔍 正则": "regex",
                "📄 文件名": "path",
            }
            if r.source != source_map.get(filter_source):
                continue
        # 文件筛选
        if filter_file != "全部文件":
            if Path(r.location.file).name != filter_file:
                continue
        filtered_indices.append(i)
    # 注：搜索过滤在下方搜索框实例化之后应用（1.64 语义：widget 实例化
    # 时才把前端新值写入 session_state；渲染前读会滞后一轮）

    # 行二：搜索 + 批量操作（作用于当前筛选结果）
    batch_cols = st.columns([1.6, 0.7, 0.7, 0.7, 0.85, 0.85, 1.3])
    with batch_cols[0]:
        search_text = st.text_input(
            "搜索",
            placeholder="🔍 搜索敏感词内容…",
            key="search_text",
            label_visibility="collapsed",
        )
    with batch_cols[1]:
        if st.button("✓ 全选", width="stretch"):
            for i in filtered_indices:
                st.session_state["user_selections"][i] = True
            st.rerun()
    with batch_cols[2]:
        if st.button("✕ 清空", width="stretch"):
            for i in filtered_indices:
                st.session_state["user_selections"][i] = False
            st.rerun()
    with batch_cols[3]:
        if st.button("⇋ 反选", width="stretch"):
            for i in filtered_indices:
                st.session_state["user_selections"][i] = not st.session_state["user_selections"][i]
            st.rerun()
    with batch_cols[4]:
        if st.button("仅自动", width="stretch"):
            for i in filtered_indices:
                st.session_state["user_selections"][i] = (
                    all_results[i].status == DetectionStatus.AUTO_MASK
                )
            st.rerun()
    with batch_cols[5]:
        if st.button("仅建议", width="stretch"):
            for i in filtered_indices:
                st.session_state["user_selections"][i] = (
                    all_results[i].status == DetectionStatus.SUGGEST_MASK
                )
            st.rerun()
    with batch_cols[6]:
        if st.button("📚 加入词库", width="stretch"):
            for i in filtered_indices:
                st.session_state["user_selections"][i] = True
                if "learn_set" not in st.session_state:
                    st.session_state["learn_set"] = set()
                st.session_state["learn_set"].add(i)
            st.rerun()

    # 搜索过滤（search_text 刚实例化，值为最新）
    if search_text:
        _kw = search_text.lower()
        filtered_indices = [
            i for i in filtered_indices
            if _kw in all_results[i].text.lower() or _kw in all_results[i].context.lower()
        ]

    # 选中计数（表格上方右对齐）
    selected_count = sum(
        1 for i in filtered_indices if st.session_state["user_selections"].get(i, False)
    )
    st.markdown(
        f'<div class="sel-count">当前显示 {len(filtered_indices)} 项 · 已选中 <b>{selected_count}</b> 项</div>',
        unsafe_allow_html=True,
    )

    # 检测结果表格（AgGrid；VALUE_CHANGED：勾选变化立即回传并触发
    # rerun，保证表格勾选态与选中计数同步——I6 问题3；选中项终审
    # 已移至“执行脱敏”确认对话框，主页不再有联动预览区——R7）
    if filtered_indices:
        from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode

        # P3：存在 AI 判定时附加「AI 判定」列（未启用 LLM 时零噪声）
        _has_llm = any(all_results[i].llm_reason for i in filtered_indices)
        display_rows = []
        for i in filtered_indices:
            r = all_results[i]
            _row = {
                "index": i,
                "选择": st.session_state["user_selections"].get(i, False),
                "敏感信息": r.text,
                "类别": TYPE_LABELS.get(r.text_type, r.text_type.value),
                "来源": SOURCE_LABELS.get(r.source, r.source),
                "置信度": r.confidence,
                "处置": STATUS_LABELS.get(r.status, r.status.value),
            }
            if _has_llm:  # AI 判定插在处置与文件之间
                _reason = r.llm_reason or ""
                _row["AI 判定"] = _reason[:44] + ("…" if len(_reason) > 44 else "")
            _row["文件"] = Path(r.location.file).name if r.location.file else ""
            _row["上下文"] = r.context[:80] + "..." if len(r.context) > 80 else r.context
            display_rows.append(_row)

        df_display = pd.DataFrame(display_rows)

        # 构建 AgGrid 配置（兼容不同版本的参数命名）
        gb = GridOptionsBuilder.from_dataframe(df_display)
        gb.configure_column("index", hide=True)
        # “选择”列：布尔单元格 checkbox 编辑器；勾选变化作为“单元格值编辑”
        # 回传（VALUE_CHANGED），不走行选择（1.64 + st-aggrid 1.2.1 实测：
        # configure_selection 的 checkbox 落在被隐藏的 index 列上永远不可见，
        # SELECTION_CHANGED 永不触发 —— 勾选后“即将脱敏”不同步的根因）。
        gb.configure_column("选择", editable=True, width=68)
        gb.configure_column("敏感信息", editable=False, width=200)
        gb.configure_column("类别", editable=False, width=92)
        gb.configure_column("来源", editable=False, width=88)
        gb.configure_column("置信度", editable=False, type=["numericColumn"], precisionFormat=2, width=86)
        gb.configure_column("处置", editable=False, width=96)
        if _has_llm:
            gb.configure_column("AI 判定", editable=False, width=220)
        gb.configure_column("文件", editable=False, width=168)
        gb.configure_column("上下文", editable=False, width=380)
        # 注：不配置行选择（configure_selection）——见上方“选择”列注释。
        # configure_pagination: 兼容不同版本
        try:
            gb.configure_pagination(paginationAutoPageSize=False, paginationPageSize=30)
        except TypeError:
            try:
                gb.configure_pagination(pagination_auto_page_size=False, pagination_page_size=30)
            except TypeError:
                gb.configure_pagination(paginationPageSize=30)
        gridOptions = gb.build()

        # 渲染 AgGrid（显式列宽，上下文列加宽减少截断）；
        # 勾选变化（单元格值编辑）即时回传 + rerun；
        # 深色涂装由主题桥脚本直接向 st_aggrid iframe 注入 CSS 变量
        # （theme='dark' 字符串/custom_css 在该库 1.2.1 均无效，见 assets.py）
        # key：稳定 element id，防止勾选翻转→数据哈希变化→iframe 整体重建
        # （取消勾选后表格闪烁的根因）；server_sync_strategy="server_wins"
        # 必须配套：默认 client_wins 在首次手动编辑后永久忽略服务器推送，
        # 批量按钮（全选/清空等）将不再更新表格视觉。
        grid_response = AgGrid(
            df_display,
            key="masking_results_grid",
            gridOptions=gridOptions,
            update_mode=GridUpdateMode.VALUE_CHANGED,
            fit_columns_on_grid_load=False,
            height=500,
            allow_unsafe_jscode=True,
            theme="streamlit",
            server_sync_strategy="server_wins",
        )

        # 从回传数据（编辑后的全表）同步勾选态；有变化立即 rerun，
        # 保证计数与表格一致（I6 问题3；选中项终审在执行确认对话框，R7）。
        # server_wins 下服务器推送不会更新组件 return_value：陈旧回传会在
        # 批量按钮（全选/清空等）修改后立即覆盖回滚。用回传指纹守卫：
        # 同一份回传数据只应用一次，新编辑（新指纹）才重新同步。
        grid_data = grid_response.get("data")
        _sig = None
        if grid_data is not None:
            try:
                if isinstance(grid_data, list):
                    _checks = tuple(bool(row.get("选择")) for row in grid_data)
                else:
                    _checks = tuple(bool(v) for v in grid_data["选择"].tolist())
                _sig = hash(_checks)
            except Exception:
                _sig = None
        if _sig is None or _sig != st.session_state.get("last_grid_sig"):
            changed = _apply_grid_selection(
                filtered_indices,
                grid_data,
                st.session_state["user_selections"],
            )
            if _sig is not None:
                st.session_state["last_grid_sig"] = _sig
            if changed:
                st.rerun()

    # R9：未勾选项一键永久排除——勾选瞬间把当前所有未勾选的检测词
    # 写入白名单（去重），点「重新检测」后不再被识别；白名单可在
    # 「设置 → 词库管理 → 白名单」维护。替代原 whitelist.yaml 手工提示。
    unchecked_words = list(dict.fromkeys(
        r.text for i, r in enumerate(all_results)
        if not st.session_state["user_selections"].get(i, False)
    ))
    exclude_unchecked = st.checkbox(
        "对未勾选的敏感词进行永久排除",
        value=False,
        disabled=not unchecked_words,
        key="exclude_unchecked_to_whitelist",
        help=(
            f"勾选后立即把当前未勾选的 {len(unchecked_words)} 个词加入白名单"
            "（下次检测不再识别）；点击「🔄 重新检测」后生效。"
            "白名单可在「设置 → 词库管理 → 白名单」中维护"
        ),
    )
    if not exclude_unchecked:
        # 取消勾选可重新快照：再次勾选时按当时的未勾选集合重新写入
        st.session_state.pop("whitelist_applied", None)
    elif not st.session_state.get("whitelist_applied"):
        st.session_state["whitelist_applied"] = True  # 一次性写入，跨 rerun 不重复
        added, dup = _merge_words_into_whitelist(unchecked_words)
        if added:
            st.toast(
                f"✅ 已将 {added} 个未勾选词加入白名单（点击「重新检测」后生效）"
            )
        elif dup:
            st.toast("这些词已全部在白名单中，无需重复添加")
        else:
            st.toast("⚠️ 白名单写入失败，请检查 config/whitelist.yaml 权限")

    # ── Step 4: 执行脱敏 ──（步骤条已固定在顶部，此处不再重复）

    # 待确认的项：不在主页展示“即将脱敏”预览（R7），改为点击“执行脱敏”
    # 后在确认对话框中逐项勾选终审，表格勾选与预览的联动随之取消
    final_selected = _final_selected_indices(
        all_results, st.session_state["user_selections"]
    )

    exec_cols = st.columns(3)
    with exec_cols[0]:
        execute_btn = st.button(
            f"🚀 执行脱敏（{len(final_selected)} 项）"
            if final_selected else "🚀 执行脱敏",
            type="primary",
            width="stretch",
            disabled=not final_selected,
        )
    with exec_cols[1]:
        re_detect_btn = st.button(
            "🔄 重新检测",
            width="stretch",
        )
    with exec_cols[2]:
        reupload_btn = st.button(
            "⬅️ 重新上传文件",
            width="stretch",
            help="放弃当前检测结果与勾选，清空已上传文件（自定义词保留）",
        )

    if re_detect_btn:
        # 同批文件重走检测：保留上传组件，仅清检测/勾选态（I6 统一收敛至
        # _clear_task_state；learn_set 由 _run_detection 开头按索引失效重置）
        _clear_task_state(clear_upload=False)
        st.rerun()

    if reupload_btn:
        _reset_task_state()
        st.rerun()

    if execute_btn:
        # 打开执行确认对话框（R7）；每次打开换新 token，对话框内 checkbox
        # 的 session key 带 token 前缀，避免上次弹窗的勾选态串扰本次
        st.session_state["mask_dialog_token"] = uuid4().hex[:8]
        st.session_state.pop("pending_batch_id", None)
        _confirm_mask_dialog(
            uploaded_files=uploaded_files,
            final_selected=final_selected,
            all_results=all_results,
            mode=mode,
            ner_enabled=ner_enabled,
            irreversible=irreversible,
            learn_words=learn_words,
            mask_filenames=mask_filenames,
            custom_words_text=custom_words_text,
            manual_only=manual_only,
        )
    elif not final_selected:
        st.caption("⚠️ 请在上方表格至少勾选一项后再执行脱敏")


def _render_llm_banner():
    """P3 反馈机制：AI 增强运行状态横幅（成功/失败/熔断三态）。

    数据源 ``_llm_run_summary``（检测/脱敏结束时由 service 固化）；
    未启用 AI 或无摘要时不渲染（零噪声）。失败态展示友好化原因，
    用户可自助排查端点配置。
    """
    s = st.session_state.get("_llm_run_summary")
    if not s:
        return
    role = s.get("role", "adjudicator")
    if s.get("ok"):
        parts = []
        if role in ("adjudicator", "both"):
            parts.append(
                f"复核 {s.get('items_adjudicated', 0)} 项"
                f"（剔除 {s.get('dropped', 0)} · 修正 {s.get('adjusted', 0)}）"
            )
        if role in ("detector", "both"):
            parts.append(f"检出 {s.get('detected', 0)} 个新实体")
        stat = (
            f"模型 {s.get('model', '')} · {s.get('calls', 0)} 次调用"
            f" · {s.get('elapsed_seconds', 0):.1f}s"
        )
        hits = s.get("cache_hits", 0)
        if hits:
            stat += f" · 缓存命中 {hits} 次"
        st.info("✨ **AI 增强已生效**　" + " · ".join(parts) + "\n\n" + stat)
    else:
        reason = s.get("first_error") or "未知原因"
        tripped = "（连续 3 次失败已自动停用本次运行的 AI 增强）" if s.get("tripped") else ""
        st.warning(
            f"⚠️ **AI 增强未生效**，本次已按纯规则完成检测{tripped}\n\n"
            f"原因：{reason}\n\n"
            f"排查：⚙️ 设置 → 模型配置 → 测试连接；确认 Base URL 为 "
            f"OpenAI 兼容端点、API Key 有效、模型名正确"
        )


# ──────────────────────────────────────────────
# 执行脱敏确认对话框（R7：原“即将脱敏”预览区 + 批次信息区块改为
# 点击“执行脱敏”后的 check list 弹窗终审，主页联动预览随之移除）
# ──────────────────────────────────────────────

@st.dialog("✅ 确认脱敏内容", width="large")
def _confirm_mask_dialog(uploaded_files, final_selected, all_results,
                         mode, ner_enabled, irreversible, learn_words,
                         mask_filenames, custom_words_text, manual_only):
    """执行前确认弹窗：check list 终审 + 批次信息填写。

    - 清单默认全勾选（继承主表格勾选）；弹窗内取消的项本次不脱敏，
      确认时回写 user_selections，“返回重新脱敏”后主表格保持一致；
    - 批次信息随弹窗填写（批次ID 在弹窗打开时生成一次，确认过程稳定）；
    - 确认执行走 _run_masking：成功路径其内部 st.rerun() 关闭弹窗并
      跳转结果页；取消按钮直接 rerun 关闭弹窗。
    """
    token = st.session_state.get("mask_dialog_token", "")

    def _sel_key(i: int) -> str:
        return f"dlg_sel_{token}_{i}"

    st.markdown(
        f"即将对以下 **{len(final_selected)}** 项执行脱敏，"
        f"请逐项确认（取消勾选的项本次不处理）："
    )

    # check list：滚动容器逐项勾选（数量多时容器内滚动，弹窗不无限拉长）
    with st.container(height=340):
        for i in final_selected:
            r = all_results[i]
            text_show = r.text if len(r.text) <= 48 else r.text[:48] + "…"
            label = (
                f"{text_show}　|　{TYPE_LABELS.get(r.text_type, r.text_type.value)}"
                f" · {SOURCE_LABELS.get(r.source, r.source)}"
                f" · 置信度 {r.confidence:.2f}"
            )
            if r.location.file:
                label += f" · {Path(r.location.file).name}"
            # P3：AI 判定理由随终审清单展示（复核调整/剔除建议/增量检出）
            if r.llm_reason:
                label += f" · ✨ {r.llm_reason[:36]}{'…' if len(r.llm_reason) > 36 else ''}"
            st.checkbox(label, value=True, key=_sel_key(i))

    # 批次信息（原 Step 4 区块移入：批次名称作为执行前最后一步在此填写）
    if "pending_batch_id" not in st.session_state:
        st.session_state["pending_batch_id"] = _generate_batch_id()
    batch_id = st.session_state["pending_batch_id"]
    st.markdown("#### 📦 批次信息")
    batch_cols = st.columns(2)
    with batch_cols[0]:
        st.text_input(
            "批次名称（可选）",
            placeholder="例如：2026年Q1财务报告脱敏",
            key="batch_name_input",
        )
    with batch_cols[1]:
        st.text_input("批次ID（自动生成）", value=batch_id, disabled=True)

    dialog_selected = [
        i for i in final_selected if st.session_state.get(_sel_key(i), True)
    ]
    btn_cols = st.columns(2)
    confirmed = btn_cols[0].button(
        f"🚀 确认执行（{len(dialog_selected)} 项）"
        if dialog_selected else "🚀 确认执行",
        type="primary", width="stretch",
        disabled=not dialog_selected,
    )
    if btn_cols[1].button("取消", width="stretch"):
        st.session_state.pop("pending_batch_id", None)
        st.rerun()

    if confirmed:
        # 以弹窗内最终勾选为准，并回写主表格勾选态（返回重脱时保持一致）
        sel_set = set(dialog_selected)
        for i in final_selected:
            st.session_state["user_selections"][i] = i in sel_set
        batch_name = st.session_state.get("batch_name_input") or ""
        st.session_state.pop("pending_batch_id", None)
        _loading_overlay("正在执行脱敏，生成产物…", "正在按您的勾选替换敏感内容")
        _run_masking(
                uploaded_files, dialog_selected, all_results,
                mode, ner_enabled, irreversible, learn_words,
                batch_id, batch_name, mask_filenames=mask_filenames,
                manual_words=_parse_custom_words(custom_words_text),
                manual_only=manual_only,
            )
        # 成功路径 _run_masking 内部已 st.rerun() 关闭弹窗并跳转结果页；
        # 走到这里说明未产出脱敏文件，保持弹窗打开以展示错误/警告


def _render_mask_result():
    """展示脱敏结果页面（持久化到 session_state）"""
    result = st.session_state["mask_result"]

    # 成功横幅
    st.markdown(
        '<div class="success-banner">'
        '<h2>🎉 脱敏完成！</h2>'
        f'<p>成功处理 {result["confirmed_count"]} 项敏感信息</p>'
        f'<p>批次ID: {result["batch_id"]} | 批次名称: {result["batch_name"] or "未命名"}</p>'
        '</div>',
        unsafe_allow_html=True,
    )

    # 映射表信息
    mappings = result.get("mappings", [])
    if mappings:
        with st.expander("📋 脱敏映射表", expanded=False):
            mapping_df = pd.DataFrame([
                {
                    "Token": m["token"],
                    "原文": m["original"],
                    "类别": m.get("type_label", ""),
                    "置信度": f"{m.get('confidence', 0):.2f}",
                }
                for m in mappings
            ])
            st.dataframe(mapping_df, width="stretch", hide_index=True)

    # 保存到目标文件夹（桌面交付方式；浏览器下载入口已随桌面化移除）
    # （I6 问题1 产物规则：单文件 → 脱敏后同名文件；
    #   目录 zip / 多文件 → 同结构 zip 压缩包；mapping.json 单独落盘）
    st.markdown("#### 💾 保存脱敏文件")
    _render_save_to_dir_panel(
        artifacts=_mask_result_artifacts(result),
        panel_key="mask",
        not_set_what="脱敏文件",
    )
    st.warning(
        "⚠️ mapping.json 是还原钥匙（含原文与占位符对照）：请与脱敏文件分开保管，"
        "勿与脱敏 zip 一起外发"
    )

    # 返回按钮：同批文件重走流程（保留上传） / 全新任务（清空一切任务态）
    result_btn_cols = st.columns(2)
    with result_btn_cols[0]:
        if st.button("🔄 返回重新脱敏", width="stretch",
                     help="保留已上传文件与设置，回到检测页重新勾选"):
            # R1-B5 防御性清理已收敛至 _clear_task_state（正常流程
            # _run_masking finally 已删 tmp_dir，此处仅为兑底）
            _clear_task_state(clear_upload=False)
            st.rerun()
    with result_btn_cols[1]:
        if st.button("🆕 开始新任务", type="primary", width="stretch",
                     help="清空已上传文件与全部任务结果，保留设置与自定义敏感词"):
            _reset_task_state()
            st.rerun()

