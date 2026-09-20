# -*- coding: utf-8 -*-
"""侧边栏：logo/新建任务/运行模式/脱敏选项 + 设置弹窗挂载。"""
import streamlit as st

from mask_tool import __version__

from .assets import _logo_svg
from .labels import MODE_DESCRIPTIONS
from .settings_dialog import _render_settings_component
from .state import _reset_task_state

# ──────────────────────────────────────────────
# 侧边栏
# ──────────────────────────────────────────────

def render_sidebar():
    """渲染侧边栏配置（v2.4：NER 恒启用；词库/保存位置/主题移入设置弹窗）"""
    with st.sidebar:
        st.markdown(
            f'<div class="side-head"><span class="logo">{_logo_svg(22)}</span>'
            f'<span class="name">mask-tool</span>'
            f'<span class="ver">v{__version__}</span></div>',
            unsafe_allow_html=True,
        )

        # 新建任务：任何时候可见，放弃当前流程回到上传页（设置与自定义词保留）
        if st.button(
            "＋ 新建脱敏任务",
            use_container_width=True,
            key="new_task_btn",
            help=("放弃当前检测/勾选/脱敏结果并清空已上传文件，回到上传页；"
                  "运行设置与临时自定义敏感词保留"),
        ):
            _reset_task_state()
            st.rerun()

        # 运行模式（功能分组卡）
        with st.container(border=True):
            st.markdown('<div class="side-label">运行模式</div>', unsafe_allow_html=True)
            mode = st.selectbox(
                "选择模式",
                options=["focused", "smart", "strict", "aggressive"],
                format_func=lambda x: {
                    "focused": "🎯 精准模式",
                    "smart": "🧠 智能模式（推荐）",
                    "strict": "🔒 严格模式",
                    "aggressive": "🚀 激进模式",
                }.get(x, x),
                index=1,
                help=("运行模式决定自动脱敏的激进程度：精准=仅词典高置信命中；"
                      "智能=自动与建议平衡（推荐）；严格=高中置信度分级处理；"
                      "激进=尽可能多脱敏，适合 AI 预处理"),
            )
            st.caption(MODE_DESCRIPTIONS.get(mode, ""))

        # 脱敏选项（功能分组卡）。NER 开关已移除（v2.4）：引擎为必选能力，
        # 恒启用；后续 AI 模型接入后在「设置 → 模型配置」中可选。
        with st.container(border=True):
            st.markdown('<div class="side-label">脱敏选项</div>', unsafe_allow_html=True)
            irreversible = st.checkbox(
                "不可逆脱敏",
                value=False,
                help="启用后将用 *** 替换敏感信息，无法还原",
            )
            learn_words = st.checkbox(
                "学习新词到词库",
                value=True,
                help="确认时标记为'加入词库'的词将写入词库文件",
            )

        # 设置弹窗由自定义组件注入（侧栏底部图标按钮 + 弹窗 UI，与原型一致）
        _render_settings_component()

    # jieba NER 恒启用（开关已随「识别引擎」卡移除）
    return mode, True, irreversible, learn_words

