# -*- coding: utf-8 -*-
"""脱敏处理 tab：上传区/步骤条/临时自定义词/检测结果确认（AgGrid）/执行与结果页。"""
import json
from pathlib import Path
from typing import Dict
from uuid import uuid4

import streamlit as st
import pandas as pd
import yaml

from mask_tool.models.detection import DetectionStatus, DetectionType
from mask_tool.core.config_loader import resolve_user_lexicon_path
from mask_tool.core.llm.adjudicator import is_ai_detected

from ..artifacts import _mask_result_artifacts, _render_save_to_dir_panel
from ..context_locate import (
    _consume_grid_locate_click,
    _context_locate_dialog,
    _ctx_cell_renderer,
)
from ..history import _generate_batch_id
from ..labels import (
    BLOCKED_EXTS,
    SOURCE_LABELS,
    STATUS_LABELS,
    SUPPORTED_MASK_EXTS,
    TYPE_LABELS,
)
from ..lexicon_io import _merge_words_into_whitelist
from ..service import (
    _load_config,
    _run_detection,
    _run_masking,
    merge_words_into_lexicon,
)
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


def render_steps(current_step: int, steps=None):
    """渲染步骤指示器（节点连线式：数字圆点 + 渐变连线，卡片化 sticky 顶部）。

    steps: [(编号, 标签), ...]，缺省为脱敏主流程 4 步；
    current_step 超过步数表示全部完成（如恢复还原结果页）。
    其他页签（恢复还原）传入自己的步骤序列复用同一视觉。
    """
    if steps is None:
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



# # ──────────────────────────────────────────────
# “加入词库”核查对话框（候选聚合 + 弹窗核查 + 立即写库）
# # ──────────────────────────────────────────────

_LEARN_CATEGORY_LABELS = {t.value: TYPE_LABELS[t] for t in DetectionType}


def _read_lexicon_yaml(lexicon_path) -> dict:
    """读词库 yaml 为 dict（文件缺失/IO 错误/yaml 损坏均视为空词库）。

    加入词库链路（按钮预判与核查对话框）共用，保证两处读取口径一致；
    读取失败只降级不阻断——预判失败照常弹窗，由对话框核查兜底。
    """
    try:
        p = Path(lexicon_path)
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        pass
    return {}


def _lexicon_existing_words(existing: dict) -> set:
    """从已读取的词库 dict 提取全部已有词集合（忽略非 list 类别）。"""
    return {
        w for words in existing.values() if isinstance(words, list) for w in words
    }


def _learn_category_options(existing: dict) -> list:
    """类别下拉选项：系统八类 ∪ 词库现有自定义类别（英文 value，展示转中文）。"""
    extra = {k for k, v in existing.items() if isinstance(v, list)}
    return sorted(set(_LEARN_CATEGORY_LABELS) | extra)


def _default_selection(result) -> bool:
    """确认页默认勾选判定（纯函数，便于单测）。

    - AUTO_MASK：默认勾选（含 aggressive 下升为自动档的 AI 检出）；
    - SUGGEST_MASK：规则来源默认勾选；**AI 增量检出（source="llm"）默认不勾选**，
      须用户显式确认——AI 检出置信度封顶 0.80、来自概率模型，落实“AI 只建议、
      不自动替换”的安全边界，避免弱模型/幻觉检出被默认纳入脱敏污染交付物；
    - HINT_ONLY：默认不勾选。
    """
    if result.status == DetectionStatus.AUTO_MASK:
        return True
    if result.status == DetectionStatus.SUGGEST_MASK:
        return not is_ai_detected(result)
    return False


def _result_file_key(r, task_kind: str = "files", zip_root: str = "") -> str:
    """结果条目对应的文件键（与 service._run_detection 登记口径一致；纯函数）。

    zip 任务 = 相对 zip 根目录的 posix 相对路径（子目录同名文件互不串扰）；
    单文件任务 = 文件名。location.file 为空返回空串；相对化失败回退文件名
    （如历史会话残留数据）。
    """
    f = str(getattr(getattr(r, "location", None), "file", "") or "")
    if not f:
        return ""
    p = Path(f)
    if task_kind == "zip" and zip_root:
        try:
            return p.relative_to(zip_root).as_posix()
        except ValueError:
            return p.name
    return p.name


def _build_learn_candidates(all_results, indices, selections, existing_words):
    """聚合“加入词库”候选词条（纯函数，对话框渲染与测试共用）。

    - 按“词文本”去重聚合（同词多来源/多文件合并），类别取首次出现值；
    - 已在词库的词（text in existing_words）直接过滤，不进候选列表：
      对话框只展示新词，避免“已在词库仍可勾选”的重复写入路径；
    - checked：该词任一来源行被勾选即为 True；候选范围内完全无勾选行
      时回退为“全部候选默认勾选”（过滤后剩下的全是新词），对应“未
      勾选直接点按钮”的用法，避免点击无反馈；判定看原始勾选行而非
      聚合后 checked（勾选的词可能恰好都已在词库被过滤）；
    - 返回按 (类别, 词文本) 排序的 list[dict]。
    """
    agg = {}
    for i in indices:
        r = all_results[i]
        ent = agg.setdefault(r.text, {
            "category": r.text_type.value,
            "sources": [],
            "files": set(),
            "confidence": 0.0,
            "checked": False,
        })
        if r.source not in ent["sources"]:
            ent["sources"].append(r.source)
        if r.location.file:
            ent["files"].add(Path(r.location.file).name)
        ent["confidence"] = max(ent["confidence"], float(r.confidence))
        if selections.get(i, False):
            ent["checked"] = True

    candidates = []
    for text, ent in agg.items():
        if text in existing_words:
            continue  # 已在词库：不再作为候选词条展示，防重复加入
        candidates.append({
            "text": text,
            "category": ent["category"],
            "sources": " · ".join(SOURCE_LABELS.get(s, s) for s in ent["sources"]),
            "file_count": len(ent["files"]),
            "confidence": ent["confidence"],
            "checked": ent["checked"],
        })
    if candidates and not any(selections.get(i, False) for i in indices):
        for c in candidates:
            c["checked"] = True
    candidates.sort(key=lambda c: (c["category"], c["text"]))
    return candidates


@st.dialog("📚 加入词库", width="large")
def _learn_to_lexicon_dialog(all_results, mode):
    """“加入词库”核查对话框：勾选词条 → 调整类别 → 确认立即写入词库。

    - 候选范围 = 点击按钮时的筛选视图快照（learn_candidates_idx，
      已含与页面一致的搜索关键字过滤）；
    - 已在词库中的词不进候选（_build_learn_candidates 已过滤），
      对话框只展示新词；caption 注明自动略过数，避免词条变少生疑；
    - 默认勾选 = 已勾选检测项（未勾选任何项时回退为全部候选）；
    - 确认后立即写入 cfg.lexicon_path（管线真正加载的那份词库），
      结果经 learn_flash 在主页展示；同时清空 learn_set——
      脱敏阶段不再重复写（旧自动写库路径保留给 CLI/兼容场景）。
    """
    idx = st.session_state.get("learn_candidates_idx") or []
    if not idx:
        st.warning("当前没有可加入词库的检测项，请先检测")
        return

    cfg = _load_config(mode)
    lexicon_path = Path(cfg.lexicon_path)
    existing = _read_lexicon_yaml(lexicon_path)
    existing_words = _lexicon_existing_words(existing)
    cat_options = _learn_category_options(existing)

    selections = st.session_state.get("user_selections", {})
    candidates = _build_learn_candidates(
        all_results, idx, selections, existing_words,
    )
    if not candidates:
        # 预判与弹窗之间的极端窗口（如词库读取失败又被他处写入）：
        # 过滤后无新词可加，直接提示不渲染空列表
        st.info("所列检测词均已在词库中，无需重复加入")
        return
    fallback = not any(selections.get(i, False) for i in idx)
    skipped_count = len({all_results[i].text for i in idx} & existing_words)
    if fallback:
        st.info(
            f"未勾选任何检测项，已默认带出当前筛选视图全部 **{len(candidates)}** 个新词条，"
            "请逐条核查后再确认"
        )
    skipped_note = f"（另有 {skipped_count} 个词已在词库，已自动略过）" if skipped_count else ""
    st.caption(
        f"共 **{len(candidates)}** 个词条{skipped_note}。"
        "加入后这些词将按词典匹配（置信度 0.95）参与后续脱敏，"
        "可随时在「词库管理」中移除。"
    )

    token = st.session_state.get("learn_dialog_token", "")
    with st.container(height=min(420, 60 * len(candidates) + 48)):
        for n, c in enumerate(candidates):
            row = st.columns([2.4, 0.9, 1.1])
            with row[0]:
                st.checkbox(
                    f"{c['text'][:44]}{'…' if len(c['text']) > 44 else ''}"
                    f"　|　{c['sources']}",
                    value=c["checked"],
                    key=f"learn_chk_{token}_{n}",
                )
            with row[1]:
                st.caption(f"置信度 {c['confidence']:.2f}")
            with row[2]:
                _default = (
                    c["category"] if c["category"] in cat_options else cat_options[0]
                )
                st.selectbox(
                    "类别",
                    options=cat_options,
                    index=cat_options.index(_default),
                    format_func=lambda v: _LEARN_CATEGORY_LABELS.get(v, v),
                    key=f"learn_cat_{token}_{n}",
                    label_visibility="collapsed",
                )

    checked_count = sum(
        1 for n in range(len(candidates))
        if st.session_state.get(f"learn_chk_{token}_{n}", False)
    )
    btn_cols = st.columns(2)
    if btn_cols[0].button(
        f"✓ 加入词库（{checked_count} 个词条）",
        type="primary",
        width="stretch",
        disabled=checked_count == 0,
    ):
        learned: Dict[str, list] = {}
        for n, c in enumerate(candidates):
            if st.session_state.get(f"learn_chk_{token}_{n}", False):
                cat = st.session_state.get(f"learn_cat_{token}_{n}", c["category"])
                learned.setdefault(cat, []).append(c["text"])
        ok, new_count, msg = merge_words_into_lexicon(lexicon_path, learned)
        if ok:
            # 词已立即写库：清空 learn_set，脱敏阶段不再重复写
            st.session_state.pop("learn_set", None)
            st.session_state["learn_flash"] = {
                "msg": f"✅ 已将 {new_count} 个新词写入词库",
                "path": str(lexicon_path),
            }
            st.rerun()  # 关闭对话框，主页展示结果
        else:
            st.error(f"写入失败：{msg}")
    if btn_cols[1].button("取消", width="stretch"):
        st.rerun()


# ──────────────────────────────────────────────
# 标签页1：脱敏处理
# ──────────────────────────────────────────────

def _render_masking_tab(mode: str, ner_enabled: bool, irreversible: bool, learn_words: bool, mask_style: str = "token"):
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
            '<span>支持多选 .docx / .xlsx / .xlsm / .pdf</span></div>',
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
            help=("把整个目录压缩成 zip 后上传：递归处理其中的 docx/xlsx/xlsm/pdf，"
                  "pptx 等屏蔽类型会警告且不进产物；脱敏产物为同结构"
                  "目录的 zip 压缩包"),
        )
    if dir_zip is not None and uploaded_files:
        st.info("已同时上传单文件与目录 zip：本次按目录 zip 处理，单文件列表忽略")

    if not uploaded_files and dir_zip is None:
        st.markdown(
            '<div class="notice"><span class="ic">📤</span>'
            '<div class="tx">请上传需要脱敏的文件（支持 <b>.docx / .xlsx / .xlsm / .pdf</b>，'
            '或上传整个目录的 <b>.zip</b> 压缩包）'
            '<small>仅 PPT 已停用；PDF 需文本型（扫描件不支持）</small>'
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
            "以下文件暂不支持脱敏，请移除后重试"
            "（当前支持 Word .docx 与 Excel .xlsx / .xlsm 及文本型 PDF）：\n\n"
            + "\n\n".join(blocked_msgs)
        )
        return

    # 已上传文件：由上传框内原生文件 chips 展示（含单个删除与追加按钮，
    # 交互完整）；不再另渲染自定义清单，避免同一信息双重呈现

    # ── Step 2: 检测分析 ──
    st.markdown("---")

    # 输入区双栏（卡片化由 app.css 锚定）：左 = 临时敏感词输入；右 = 本次任务选项
    mi_cols = st.columns([1.5, 1])
    with mi_cols[0]:
        # I6：临时自定义敏感词（仅本次任务生效，不写入词库文件）
        st.markdown(
            '<div class="mi-label"><b>✍️ 临时自定义敏感词</b>'
            '<span class="badge">仅本次任务生效</span></div>'
            '<div class="mi-hint">每行一个词，或用逗号分隔；不写入词库</div>',
            unsafe_allow_html=True,
        )
        custom_words_text = st.text_area(
            "临时自定义敏感词",
            placeholder="例：某某科技有限公司，张三丰，2026年Q3财报",
            key="custom_words_input",
            height=104,
            label_visibility="collapsed",
        )
        _n_words = len(_parse_custom_words(custom_words_text))
        st.markdown(
            f'<div class="mi-count">已识别 <b>{_n_words}</b> 个词</div>',
            unsafe_allow_html=True,
        )
    with mi_cols[1]:
        # 同时脱敏文件名（仅处理主名，不改扩展名）；说明文字走 help=
        # 问号悬停提示（与侧栏一致的策略）。持久化：初值取自配置，
        # on_change 写回（sidebar._persist_pref）
        st.markdown('<div class="mi-right-title">本次任务选项</div>', unsafe_allow_html=True)
        from ..sidebar import _persist_pref
        from mask_tool.core.app_settings import get_ui_prefs
        _prefs = get_ui_prefs()
        mask_filenames = st.checkbox(
            "同时脱敏文件名",
            value=bool(_prefs.get("mask_filenames", True)),
            key="mask_filenames",
            on_change=_persist_pref,
            args=("mask_filenames",),
            help="对文件主名（不含扩展名）执行同样的检测与替换，Token 与正文共享",
        )
        manual_only = st.checkbox(
            "仅脱敏我指定的词",
            value=bool(_prefs.get("manual_only_mode")),
            key="manual_only_mode",
            on_change=_persist_pref,
            args=("manual_only_mode",),
            help="完全跳过自动检测（NER/正则/词库），只处理左侧指定的词；适合误报多时",
        )

    if st.button("🔍 开始检测", type="primary", width="stretch"):
        _ai_on = bool(st.session_state.get("llm_enabled", False))
        _loading_overlay(
            "正在分析，检测敏感信息...",
            ("AI 增强已开启：模型响应速度决定耗时，超预算自动降级纯规则"
             if _ai_on else
             "大文件 / 多文件可能需要几十秒，请勿关闭窗口"),
        )
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

    # I6 问题2：目录 zip 中的屏蔽类型（pptx 等）——警告且不进产物
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

    # “加入词库”核查对话框的结果反馈（确认后 rerun 到主页展示）
    _learn_flash = st.session_state.pop("learn_flash", None)
    if _learn_flash:
        st.success(_learn_flash["msg"])
        st.caption(f"词库文件：{_learn_flash['path']}")

    if "user_selections" not in st.session_state:
        # 默认勾选：自动脱敏项 + 规则来源的建议项；AI 增量检出（source="llm"）
        # 落建议档时默认不勾选，须用户显式确认（见 _default_selection 说明）
        st.session_state["user_selections"] = {
            i: _default_selection(r) for i, r in enumerate(all_results)
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
            options=["全部来源", "✍️ 手动", "📘 词典", "⚙️ NER", "🔍 正则", "📄 文件名", "✨ AI"],
            key="filter_source",
        )
    with filter_cols[3]:
        filter_file = st.selectbox(
            "文件",
            options=["全部文件"] + list(file_results.keys()),
            key="filter_file",
        )

    # 应用筛选：下拉四项 → 搜索关键字，全部作用完再渲染批量按钮，
    # 保证「全选/清空/反选/仅自动/仅建议」、选中计数、表格三者同一视图
    # （此前批量按钮先于搜索生效：搜索后点全选会勾到被搜索隐藏的行，
    # 仅自动/仅建议会误清隐藏行）
    _task_kind = st.session_state.get("task_kind", "files")
    _zip_root = str(st.session_state.get("zip_tree_root") or "")
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
                "✨ AI": "llm",
            }
            if r.source != source_map.get(filter_source):
                continue
        # 文件筛选：与 file_results 登记同口径（zip 任务=相对路径，
        # 单文件=文件名）。此前按 basename 比较：zip 子目录文件永远
        # 筛不出、不同子目录同名文件互相串结果
        if filter_file != "全部文件":
            if _result_file_key(r, _task_kind, _zip_root) != filter_file:
                continue
        filtered_indices.append(i)

    # 行二：搜索 + 批量操作（作用于当前筛选结果）；末列（加入词库）
    # 权重 2.5 -> 1.75：水平收缩 30%，其余按钮 stretch 自动分摊铺满
    batch_cols = st.columns([1.5, 0.62, 0.62, 0.62, 0.78, 0.78, 1.75])
    with batch_cols[0]:
        search_text = st.text_input(
            "搜索",
            placeholder="🔍 搜索敏感词内容…",
            key="search_text",
            label_visibility="collapsed",
        )
    # 搜索过滤：search_text 刚实例化，值为最新（1.64 语义：widget 实例化
    # 时才把前端新值写入 session_state）。先于批量按钮应用——批量操作、
    # 选中计数、表格三者始终同一视图
    if search_text:
        _kw = search_text.lower()
        filtered_indices = [
            i for i in filtered_indices
            if _kw in all_results[i].text.lower()
            or _kw in all_results[i].context.lower()
        ]
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
        if st.button(
            "📚 加入词库", width="stretch",
            help="弹窗核查勾选的敏感词后立即写入词库（可调整类别，已在词库中的词自动略过）",
        ):
            # filtered_indices 已含搜索过滤，与页面显示天然同口径
            _view = filtered_indices
            if not _view:
                st.info("当前筛选结果为空，没有可加入词库的检测项")
            else:
                # 预判：视图内全部词都已在词库 → 不弹核查列表，直接提示。
                # 词库路径用 resolve_user_lexicon_path：轻量、不触发
                # _load_config 的事件渲染，且与对话框 cfg.lexicon_path 同源；
                # 读取失败视为空词库，照常弹窗由对话框核查兜底
                _texts = {all_results[i].text for i in _view}
                if _texts <= _lexicon_existing_words(
                    _read_lexicon_yaml(resolve_user_lexicon_path())
                ):
                    st.info(
                        f"所列 **{len(_texts)}** 个词均已在词库中，无需重复加入"
                    )
                else:
                    st.session_state["learn_candidates_idx"] = _view
                    # 每次打开换新 token：对话框内控件 key 带前缀，避免上一次
                    # 弹窗的勾选态串扰本次（同 _confirm_mask_dialog 的做法）
                    st.session_state["learn_dialog_token"] = uuid4().hex[:8]
                    _learn_to_lexicon_dialog(all_results, mode)
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
            # 与文件筛选同口径：zip 任务显示相对路径（子目录同名文件可区分）
            _row["文件"] = _result_file_key(r, _task_kind, _zip_root)
            _row["上下文"] = r.context[:80] + "..." if len(r.context) > 80 else r.context
            _row["loc"] = 0  # 隐藏列：上下文点击回传通道（context_locate.py）
            display_rows.append(_row)

        df_display = pd.DataFrame(display_rows)

        # 构建 AgGrid 配置（兼容不同版本的参数命名）
        gb = GridOptionsBuilder.from_dataframe(df_display)
        gb.configure_column("index", hide=True)
        gb.configure_column("loc", hide=True)  # 上下文点击回传通道（隐藏）
        # “选择”列：布尔单元格 checkbox 编辑器；勾选变化作为“单元格值编辑”
        # 回传（VALUE_CHANGED），不走行选择（1.64 + st-aggrid 1.2.1 实测：
        # configure_selection 的 checkbox 落在被隐藏的 index 列上永远不可见，
        # SELECTION_CHANGED 永不触发 —— 勾选后“即将脱敏”不同步的根因）。
        gb.configure_column("选择", editable=True, width=68)
        gb.configure_column("敏感信息", editable=False, width=200)
        gb.configure_column("类别", editable=False, width=92)
        gb.configure_column("来源", editable=False, width=88)
        gb.configure_column("置信度", editable=False, type=["numericColumn"], precisionFormat=2, width=104)
        gb.configure_column("处置", editable=False, width=96)
        if _has_llm:
            gb.configure_column("AI 判定", editable=False, width=220)
        gb.configure_column("文件", editable=False, width=168)
        # 上下文列：可点击定位原文位置（cellRenderer 点击写隐藏 loc 列回传）
        gb.configure_column(
            "上下文", editable=False, width=380,
            cellRenderer=_ctx_cell_renderer(),
        )
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

        # 上下文定位点击（R10）：先于勾选同步消费，防止同步分支的 rerun
        # 丢弃点击标记；序号守卫保证同一点击只消费一次
        _locate_click = _consume_grid_locate_click(grid_response)
        if _locate_click is not None:
            st.session_state["ctx_locate_idx"] = _locate_click

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

    # R10：上下文定位弹窗（点击表格「上下文」列后打开；右上角 ✕ 关闭时
    # on_dismiss 回调清标记。放在表格区块外：筛选变化后仍可正常弹出）
    if st.session_state.get("ctx_locate_idx") is not None:
        _context_locate_dialog()

    # R9：未勾选项一键永久排除——勾选瞬间把当前所有未勾选的检测词
    # 写入白名单（去重），点「重新检测」后不再被识别；白名单可在
    # 「设置 → 词库管理 → 白名单」维护。替代原 whitelist.yaml 手工提示。
    # 排除 AI 增量检出项：AI 建议档默认不勾选（见 _default_selection），
    # 若一并计入会把“用户尚未审阅的 AI 真阳性”写进白名单 → 之后永不识别。
    unchecked_words = list(dict.fromkeys(
        r.text for i, r in enumerate(all_results)
        if not st.session_state["user_selections"].get(i, False)
        and not is_ai_detected(r)
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
        # R10：关闭可能残留的定位弹窗标记（同轮只允许一个对话框）
        st.session_state.pop("ctx_locate_idx", None)
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
            mask_style=mask_style,
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
        # T2.3：确认页对 AI 建议的采纳统计（脱敏结束后写入）
        if s.get("ai_detected"):
            stat += (f" · AI 检出采用 {s.get('ai_detected_accepted', 0)}"
                     f"/{s.get('ai_detected')}")
        if s.get("ai_dropped"):
            stat += (f" · AI 剔除被改回 {s.get('ai_dropped_overridden', 0)}"
                     f"/{s.get('ai_dropped')}")
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
                         mask_style, mask_filenames, custom_words_text, manual_only):
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
        _loading_overlay(
            "正在执行脱敏，生成产物…",
            ("AI 增强已开启：含模型复核环节，超预算自动降级纯规则"
             if bool(st.session_state.get("llm_enabled", False)) else
             "正在按您的勾选替换敏感内容"),
        )
        _run_masking(
                uploaded_files, dialog_selected, all_results,
                mode, ner_enabled, irreversible, learn_words,
                batch_id, batch_name, mask_style=mask_style,
                mask_filenames=mask_filenames,
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

    # AI 增强运行摘要（含确认页采纳率，脱敏结束时由 service 固化）
    _render_llm_banner()

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

