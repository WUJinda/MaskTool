# -*- coding: utf-8 -*-
"""mask-tool 桌面 UI 入口（streamlit run 脚本）。

本文件保持极薄：页面配置 + 主渲染循环，全部实现位于 mask_tool.web.ui 包：

    ui/
    ├── assets.py          静态资产注入（static/app.css、logo、主题桥）
    ├── labels.py          共享文案常量（类型/状态/来源/模式）
    ├── files.py           文件工具（zip 解压/分类/文本抽取）
    ├── state.py           session_state 管理（任务态键/勾选同步）
    ├── history.py         批次历史
    ├── service.py         core 粘合层（检测/脱敏/还原主流程）
    ├── lexicon_io.py      词库定位/读写/CSV 导入导出
    ├── artifacts.py       批次产物落盘与保存面板
    ├── settings_dialog.py 设置弹窗组件链路（components.v2）
    ├── sidebar.py         侧边栏
    ├── results_table.py   检测结果 DataFrame
    └── pages/             脱敏处理 / 恢复还原 两个标签页

依赖单向（下层不得 import 上层）：
    pages → sidebar/results_table/settings_dialog/artifacts
          → state/lexicon_io/service/history → labels/files/assets

本文件同时兼容 re-export 既有符号（tests/test_web_*.py 与桌面链路以
mask_tool.web.app.xxx 引用历史符号）。
"""

import streamlit as st

from mask_tool.utils.logger import setup_logger

setup_logger()  # 落盘 ~/.mask-tool/logs/mask-tool.log（桌面版唯一可见日志渠道）

# ──────────────────────────────────────────────
# 页面配置
# ──────────────────────────────────────────────

st.set_page_config(
    page_title="mask-tool 文件脱敏工具",
    page_icon="⛔",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ──────────────────────────────────────────────
# 兼容 re-export（历史符号 → ui 包新家）
# ──────────────────────────────────────────────

from mask_tool.web.ui.labels import (  # noqa: E402,F401
    BLOCKED_EXTS,
    MODE_DESCRIPTIONS,
    SOURCE_LABELS,
    STATUS_LABELS,
    SUPPORTED_EXTENSIONS,
    SUPPORTED_MASK_EXTS,
    TYPE_LABELS,
)
from mask_tool.web.ui.history import (  # noqa: E402,F401
    HISTORY_PATH,
    MAX_HISTORY,
    BatchRecord,
    _add_history,
    _generate_batch_id,
    _load_history,
    _save_history,
)
from mask_tool.web.ui.files import (  # noqa: E402,F401
    ZIP_MAX_ENTRIES,
    ZIP_MAX_TOTAL_BYTES,
    _classify_tree_files,
    _confidence_class,
    _extract_text,
    _file_icon,
    _safe_unzip,
)
from mask_tool.web.ui.assets import (  # noqa: E402,F401
    _inject_css,
    _inject_theme_bridge,
    _logo_svg,
)
from mask_tool.web.ui.state import (  # noqa: E402,F401
    TASK_STATE_KEYS,
    _apply_grid_selection,
    _clear_task_state,
    _dedup_results,
    _final_selected_indices,
    _parse_custom_words,
    _reset_task_state,
)
from mask_tool.web.ui.results_table import _results_to_dataframe  # noqa: E402,F401
from mask_tool.web.ui.service import (  # noqa: E402,F401
    BATCHES_DIR,
    _do_mask_file,
    _ensure_lexicon_exists,
    _load_config,
    _run_detection,
    _run_masking,
    _save_learned_words,
    _unmask_file,
    _unmask_pptx,
)
from mask_tool.web.ui.lexicon_io import (  # noqa: E402,F401
    _add_words_to_lexicon,
    _create_lexicon_category,
    _ensure_user_lexicon,
    _export_lexicon_csv_bytes,
    _export_lexicon_csv_to_file,
    _find_lexicon_file,
    _get_lexicon_data,
    _get_lexicon_info,
    _import_lexicon,
    _import_lexicon_csv_text,
    _merge_words_into_lexicon,
)
from mask_tool.web.ui.artifacts import (  # noqa: E402,F401
    _mask_result_artifacts,
    _open_in_explorer,
    _render_save_to_dir_panel,
    _unique_target,
    _write_artifacts,
)
from mask_tool.web.ui.settings_dialog import (  # noqa: E402,F401
    _flash,
    _get_settings_component,
    _handle_settings_event,
    _lexicon_payload,
    _render_settings_component,
)
from mask_tool.web.ui.sidebar import render_sidebar  # noqa: E402,F401
from mask_tool.web.ui.pages.masking import (  # noqa: E402,F401
    _render_mask_result,
    _render_masking_tab,
    render_steps,
)
from mask_tool.web.ui.pages.restore import (  # noqa: E402,F401
    _mapping_to_tokens,
    _render_history_selector,
    _render_manual_upload,
    _render_restore_tab,
    _run_restore,
    _run_restore_zip,
)


# ──────────────────────────────────────────────
# 主应用
# ──────────────────────────────────────────────

def main():
    _inject_css()
    _inject_theme_bridge()

    # 侧边栏
    mode, ner_enabled, irreversible, learn_words = render_sidebar()

    # 标题（紧凑行：logo + 标题 + 副标题同行）
    st.markdown(
        f'<div class="main-head">{_logo_svg(34)}<h1>文件脱敏</h1>'
        f'<span class="sub">上传 → 智能检测 → 交互确认 → 一键脱敏保存</span></div>',
        unsafe_allow_html=True,
    )

    # 标签页
    tab1, tab2 = st.tabs(["⛔ 脱敏处理", "♻️ 恢复还原"])

    with tab1:
        _render_masking_tab(mode, ner_enabled, irreversible, learn_words)

    with tab2:
        _render_restore_tab()


# ──────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────

# 【已禁用】独立浏览器/Web 入口（2026-09-18）：本工具定位为桌面软件。
# UI 仅由 mask_tool.desktop（pywebview 原生窗口）内部拉起本模块渲染，
# 不再对外提供 streamlit 浏览器启动方式；mask-tool-web 控制台命令已从
# pyproject [project.scripts] 移除。
# 如需临时恢复（仅调试）：还原本函数原实现（subprocess 启 streamlit run），
# 并在 pyproject 重新注册 mask-tool-web 入口。

def run_web():
    """【已禁用】旧版浏览器 Web 入口存根，调用即退出并提示改用桌面入口"""
    import sys

    print(
        "mask-tool 已改为桌面应用，不再提供独立浏览器/Web 入口。\n"
        "请改用：mask-tool app（或 mask-tool-desktop / "
        "python -m mask_tool.desktop）打开桌面窗口。",
        file=sys.stderr,
    )
    raise SystemExit(1)


if __name__ == "__main__":
    main()
