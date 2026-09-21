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
            f'<div class="side-head"><span class="logo">{_logo_svg(29)}</span>'
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
                "模式",
                options=["focused", "smart", "strict", "aggressive"],
                format_func=lambda x: {
                    "focused": "🎯 精准模式",
                    "smart": "🧠 智能模式（推荐）",
                    "strict": "⛔ 严格模式",
                    "aggressive": "🚀 激进模式",
                }.get(x, x),
                index=1,
                label_visibility="collapsed",
            )
            st.caption(MODE_DESCRIPTIONS.get(mode, ""))

        # 脱敏选项（功能分组卡）。NER 开关已移除（v2.4）：引擎为必选能力，
        # 恒启用；AI 模型接入后在「设置 → 模型配置」配置端点（P3）。
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

        # AI 增强检测（P3）：端点在「设置 → 模型配置」维护，此处仅运行开关；
        # 端点未配置时禁用开关并引导去设置。开关为会话级（widget 状态跨
        # 任务保留，与不可逆/学习新词一致）
        from mask_tool.core.app_settings import get_llm_settings
        _llm_cfg = get_llm_settings()
        _llm_ready = bool(_llm_cfg.get("base_url") and _llm_cfg.get("model"))
        with st.container(border=True):
            st.markdown(
                '<div class="side-label">AI 增强</div>', unsafe_allow_html=True
            )
            llm_enabled = st.checkbox(
                "AI 增强检测",
                value=False,
                key="llm_enabled",
                disabled=not _llm_ready,
                help=(
                    "启用后由内网大模型复核误报 / 补充检测词库未覆盖的实体；"
                    "仅智能/激进模式生效，关闭即恢复纯规则模式"
                    if _llm_ready else
                    "请先在「设置 → 模型配置」配置内网模型端点后启用"
                ),
            )
            if _llm_ready:
                _role = {"adjudicator": "仅复核", "detector": "仅补充检测",
                         "both": "复核+检测"}.get(str(_llm_cfg.get("role", "adjudicator")), "仅复核")
                _health = st.session_state.get("llm_health") or {}
                if _health.get("ok"):
                    _badge = "🟢 已连通"
                elif _health:
                    _badge = "🔴 " + str(_health.get("msg", "未连通"))[:24]
                else:
                    _badge = "⚪ 未测试（设置 → 模型配置 → 测试连接）"
                st.caption(
                    f"{_llm_cfg.get('model', '')} · {_role} · {_badge}"
                )
            else:
                st.caption("未配置模型端点：⚙️ 设置 → 模型配置")

        # 设置弹窗由自定义组件注入（侧栏底部图标按钮 + 弹窗 UI，与原型一致）
        _render_settings_component()

    # jieba NER 恒启用（开关已随「识别引擎」卡移除）
    return mode, True, irreversible, learn_words

