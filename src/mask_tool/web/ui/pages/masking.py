# -*- coding: utf-8 -*-
"""脱敏处理 tab：上传区/步骤条/临时自定义词/检测结果确认（AgGrid）/执行与结果页。"""
import json
from pathlib import Path
from typing import Dict

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

def render_steps(current_step: int):
    """渲染步骤指示器 (1-4)"""
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
        step_html += f'<div class="step {cls}">{label}</div>'
        if i < len(steps) - 1:
            step_html += '<span class="step-arrow">→</span>'
    step_html += '</div>'
    st.markdown(step_html, unsafe_allow_html=True)


# ──────────────────────────────────────────────
# 标签页1：脱敏处理
# ──────────────────────────────────────────────

def _render_masking_tab(mode: str, ner_enabled: bool, irreversible: bool, learn_words: bool):
    """渲染脱敏处理标签页"""

    # 如果已有脱敏结果，展示结果页面
    if "mask_result" in st.session_state:
        _render_mask_result()
        return

    # ── Step 1: 文件上传 ──
    render_steps(1)

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
        st.info("📤 请上传需要脱敏的文件（支持 .docx / .xlsx）")
        st.caption("PPT 与 PDF 已暂时停用：检测到 .pptx / .pdf 文件将拒绝处理。")
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
    render_steps(2)

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
        st.caption("💡 自动检测误报的词（如普通词被识为人名），可加入 config/whitelist.yaml 的 whitelist 永久排除")

    if st.button("🔍 开始检测", type="primary", width="stretch"):
        with st.spinner("正在分析，检测敏感信息..."):
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

    # ── 检测结果统计 ──
    st.markdown("#### 📊 检测结果概览")

    # 紧凑统计条（代替 4 张大卡片）
    auto_count = sum(1 for r in all_results if r.status == DetectionStatus.AUTO_MASK)
    suggest_count = sum(1 for r in all_results if r.status == DetectionStatus.SUGGEST_MASK)
    hint_count = sum(1 for r in all_results if r.status == DetectionStatus.HINT_ONLY)
    st.markdown(
        f'<div class="statbar">'
        f'<div class="stat"><span class="v">{len(all_results)}</span><span class="k">检测总数</span></div>'
        f'<div class="stat s-auto"><span class="v">{auto_count}</span><span class="k">自动脱敏</span></div>'
        f'<div class="stat s-sugg"><span class="v">{suggest_count}</span><span class="k">建议脱敏</span></div>'
        f'<div class="stat s-hint"><span class="v">{hint_count}</span><span class="k">仅提示</span></div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # 类别分布
    type_counts: Dict[str, int] = {}
    for r in all_results:
        label = TYPE_LABELS.get(r.text_type, r.text_type.value)
        type_counts[label] = type_counts.get(label, 0) + 1

    if type_counts:
        chart_cols = st.columns([2, 1])
        with chart_cols[0]:
            # 用原生 HTML 条形图代替 st.bar_chart（避免 pyarrow 依赖）
            sorted_counts = sorted(type_counts.items(), key=lambda x: x[1])
            max_val = max(type_counts.values()) if type_counts else 1
            bars_html = '<div style="font-size:0.85rem;">'
            for label, count in sorted_counts:
                pct = int(count / max_val * 100)
                bars_html += (
                    f'<div style="display:flex;align-items:center;margin-bottom:4px;">'
                    f'<span style="width:120px;flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{label}</span>'
                    f'<div style="flex:1;background:var(--mt-bar-track,#eee);border-radius:4px;height:22px;position:relative;">'
                    f'<div style="background:linear-gradient(90deg,#667eea,#764ba2);width:{pct}%;height:100%;border-radius:4px;min-width:2px;"></div>'
                    f'<span style="position:absolute;right:6px;top:2px;font-size:0.78rem;font-weight:600;">{count}</span>'
                    f'</div></div>'
                )
            bars_html += '</div>'
            st.markdown(bars_html, unsafe_allow_html=True)
        with chart_cols[1]:
            st.markdown("**类别分布**")
            for label, count in sorted(type_counts.items(), key=lambda x: -x[1]):
                st.markdown(f"- {label}: **{count}** 项")

    # ── Step 3: 确认选择 ──
    st.markdown("---")
    render_steps(3)

    # 初始化选择状态
    if "user_selections" not in st.session_state:
        # 默认：自动脱敏和建议脱敏的项都勾选
        st.session_state["user_selections"] = {
            i: (r.status in (DetectionStatus.AUTO_MASK, DetectionStatus.SUGGEST_MASK))
            for i, r in enumerate(all_results)
        }

    # 筛选器
    st.markdown("#### 🎛️ 筛选与选择")

    filter_cols = st.columns(5)
    with filter_cols[0]:
        filter_type = st.selectbox(
            "按类别筛选",
            options=["全部"] + list(type_counts.keys()),
            key="filter_type",
        )
    with filter_cols[1]:
        filter_status = st.selectbox(
            "按处置筛选",
            options=["全部", "✅ 自动脱敏", "⚠️ 建议脱敏", "ℹ️ 仅提示"],
            key="filter_status",
        )
    with filter_cols[2]:
        filter_source = st.selectbox(
            "按来源筛选",
            options=["全部", "✍️ 手动", "📘 词典", "🤖 NER", "🔍 正则", "📄 文件名"],
            key="filter_source",
        )
    with filter_cols[3]:
        filter_file = st.selectbox(
            "按文件筛选",
            options=["全部"] + list(file_results.keys()),
            key="filter_file",
        )
    with filter_cols[4]:
        search_text = st.text_input("搜索", placeholder="输入关键词...", key="search_text")

    # 应用筛选
    filtered_indices = []
    for i, r in enumerate(all_results):
        # 类别筛选
        if filter_type != "全部":
            if TYPE_LABELS.get(r.text_type, r.text_type.value) != filter_type:
                continue
        # 状态筛选
        if filter_status != "全部":
            status_map = {
                "✅ 自动脱敏": DetectionStatus.AUTO_MASK,
                "⚠️ 建议脱敏": DetectionStatus.SUGGEST_MASK,
                "ℹ️ 仅提示": DetectionStatus.HINT_ONLY,
            }
            if r.status != status_map.get(filter_status):
                continue
        # 来源筛选
        if filter_source != "全部":
            source_map = {
                "✍️ 手动": "manual",
                "📘 词典": "dictionary",
                "🤖 NER": "ner",
                "🔍 正则": "regex",
                "📄 文件名": "path",
            }
            if r.source != source_map.get(filter_source):
                continue
        # 文件筛选
        if filter_file != "全部":
            if Path(r.location.file).name != filter_file:
                continue
        # 搜索
        if search_text:
            if search_text.lower() not in r.text.lower() and search_text.lower() not in r.context.lower():
                continue
        filtered_indices.append(i)

    # 批量操作按钮：分组工具栏（全选/清空/反选 ｜ 仅自动/仅建议 ｜ 加入词库）
    batch_cols = st.columns([1, 1, 1, 0.12, 1.25, 1.25, 0.12, 1.5])
    with batch_cols[0]:
        if st.button("全选", width="stretch"):
            for i in filtered_indices:
                st.session_state["user_selections"][i] = True
            st.rerun()
    with batch_cols[1]:
        if st.button("清空", width="stretch"):
            for i in filtered_indices:
                st.session_state["user_selections"][i] = False
            st.rerun()
    with batch_cols[2]:
        if st.button("反选", width="stretch"):
            for i in filtered_indices:
                st.session_state["user_selections"][i] = not st.session_state["user_selections"][i]
            st.rerun()
    with batch_cols[3]:
        st.markdown('<div class="col-sep"></div>', unsafe_allow_html=True)
    with batch_cols[4]:
        if st.button("仅自动脱敏", width="stretch"):
            for i in filtered_indices:
                st.session_state["user_selections"][i] = (
                    all_results[i].status == DetectionStatus.AUTO_MASK
                )
            st.rerun()
    with batch_cols[5]:
        if st.button("仅建议脱敏", width="stretch"):
            for i in filtered_indices:
                st.session_state["user_selections"][i] = (
                    all_results[i].status == DetectionStatus.SUGGEST_MASK
                )
            st.rerun()
    with batch_cols[6]:
        st.markdown('<div class="col-sep"></div>', unsafe_allow_html=True)
    with batch_cols[7]:
        if st.button("📚 选中项加入词库", width="stretch"):
            for i in filtered_indices:
                st.session_state["user_selections"][i] = True
                if "learn_set" not in st.session_state:
                    st.session_state["learn_set"] = set()
                st.session_state["learn_set"].add(i)
            st.rerun()

    # 选中计数
    selected_count = sum(
        1 for i in filtered_indices if st.session_state["user_selections"].get(i, False)
    )
    st.caption(f"当前显示 {len(filtered_indices)} 项，已选中 **{selected_count}** 项")

    # 检测结果表格（AgGrid；SELECTION_CHANGED：勾选变化立即回传并触发
    # rerun，保证下方"即将脱敏"列表与计数同步——I6 问题3）
    if filtered_indices:
        from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode

        display_rows = []
        for i in filtered_indices:
            r = all_results[i]
            display_rows.append({
                "index": i,
                "选择": st.session_state["user_selections"].get(i, False),
                "敏感信息": r.text,
                "类别": TYPE_LABELS.get(r.text_type, r.text_type.value),
                "来源": SOURCE_LABELS.get(r.source, r.source),
                "置信度": r.confidence,
                "处置": STATUS_LABELS.get(r.status, r.status.value),
                "文件": Path(r.location.file).name if r.location.file else "",
                "上下文": r.context[:80] + "..." if len(r.context) > 80 else r.context,
            })

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
        grid_response = AgGrid(
            df_display,
            gridOptions=gridOptions,
            update_mode=GridUpdateMode.VALUE_CHANGED,
            fit_columns_on_grid_load=False,
            height=500,
            allow_unsafe_jscode=True,
            theme="streamlit",
        )

        # 从回传数据（编辑后的全表）同步勾选态；有变化立即 rerun，
        # 保证计数、“即将脱敏”列表与表格一致（I6 问题3）
        changed = _apply_grid_selection(
            filtered_indices,
            grid_response.get("data"),
            st.session_state["user_selections"],
        )
        if changed:
            st.rerun()

    # ── Step 4: 执行脱敏 ──
    st.markdown("---")
    render_steps(4)

    # 最终确认的项（勾选变化后即时一致：I6 问题3）
    final_selected = _final_selected_indices(
        all_results, st.session_state["user_selections"]
    )

    if not final_selected:
        st.warning("⚠️ 请至少选择一项进行脱敏")
        return

    st.markdown(f"#### 📋 即将脱敏 **{len(final_selected)}** 项")

    # 展示选中项预览
    preview_items = []
    for i in final_selected:
        r = all_results[i]
        preview_items.append(f"- {r.text} ({TYPE_LABELS.get(r.text_type, '')})")
    with st.expander("查看选中项详情", expanded=False):
        st.markdown("\n".join(preview_items[:50]))
        if len(preview_items) > 50:
            st.caption(f"... 共 {len(preview_items)} 项")

    # 批次信息
    st.markdown("#### 📦 批次信息")
    batch_cols = st.columns(2)
    with batch_cols[0]:
        batch_name = st.text_input(
            "批次名称（可选）",
            placeholder="例如：2026年Q1财务报告脱敏",
            key="batch_name_input",
        )
    with batch_cols[1]:
        # 自动生成批次ID，每次 rerun 重新生成
        batch_id = _generate_batch_id()
        st.text_input(
            "批次ID（自动生成）",
            value=batch_id,
            disabled=True,
            key="batch_id_display",
        )

    exec_cols = st.columns(3)
    with exec_cols[0]:
        execute_btn = st.button(
            "🚀 执行脱敏",
            type="primary",
            width="stretch",
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
        with st.spinner("正在执行脱敏..."):
            _run_masking(
                uploaded_files, final_selected, all_results,
                mode, ner_enabled, irreversible, learn_words,
                batch_id, batch_name, mask_filenames=mask_filenames,
                manual_words=_parse_custom_words(custom_words_text),
                manual_only=manual_only,
            )


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

