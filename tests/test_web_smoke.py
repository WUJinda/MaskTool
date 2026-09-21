# -*- coding: utf-8 -*-
"""UI 冒烟测试：渲染级（AppTest 无头执行）+ 前端资产契约级。

背景（2026-09-20）：v0.1.2 便携包曾出现 frozen 页面 ImportError——
app.py 仅作数据文件收集，其 import 链未进 PYZ，而发布流水线的
健康检查（/_stcore/health）只验证服务器存活，测不出"服务器活着
但页面已死"。本文件从源头兜住两类缺陷：

1. 渲染级：AppTest 真正执行入口脚本，ImportError/NameError/
   模块缺失直接变红；
2. 契约级：Python↔JS 组件事件协议（action 清单）逐一对齐、
   静态资产在位、设置按钮注入不回退到轮询方案。
"""

import re
from pathlib import Path

import pytest

pytest.importorskip("streamlit")

WEB_ROOT = Path(__file__).resolve().parents[1] / "src" / "mask_tool" / "web"
APP_ENTRY = WEB_ROOT / "app.py"
JS = WEB_ROOT / "components" / "settings_dialog" / "component.js"
CSS = WEB_ROOT / "static" / "app.css"
SETTINGS_PY = WEB_ROOT / "ui" / "settings_dialog.py"


# ──────────────────────────────────────────────
# 渲染级：AppTest 无头执行入口脚本
# ──────────────────────────────────────────────

class TestRenderSmoke:
    """入口脚本真实执行一次初始渲染（上传页 + 侧栏 + 恢复页骨架）。

    任何 import 链断裂（如打包漏模块、拆分漏 import）在此暴露。
    """

    def test_app_renders_without_exception(self):
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_file(str(APP_ENTRY), default_timeout=60)
        at.run()

        assert not at.exception, f"页面渲染异常: {[e.value for e in at.exception]}"
        # 骨架在位：侧栏运行模式选择 + 双标签页（脱敏/恢复）
        assert len(at.sidebar.selectbox) == 1, "侧栏运行模式选择框缺失"
        assert len(at.tabs) == 2, "主界面双标签页缺失"
        # 侧栏按钮（新建脱敏任务）
        assert len(at.sidebar.button) >= 1, "侧栏新建任务按钮缺失"


# ──────────────────────────────────────────────
# 契约级：Python ↔ JS 组件事件协议对齐
# ──────────────────────────────────────────────

class TestComponentContract:
    """设置弹窗为跨语言组件：JS 发出的每个 action 必须有 Python 处理分支，
    反之 Python 的处理分支不应成为 JS 永不发送的死代码。"""

    def test_js_actions_have_python_handlers(self):
        js = JS.read_text(encoding="utf-8")
        py = SETTINGS_PY.read_text(encoding="utf-8")

        sent = set(re.findall(r"action:\s*'([a-z_]+)'", js))
        handled = set(re.findall(r'action\s*==\s*"([a-z_]+)"', py))

        assert sent, "component.js 应至少发送一个 action（匹配式失效？）"
        assert handled, "settings_dialog.py 应至少处理一个 action（匹配式失效？）"
        assert sent <= handled, f"JS 发出但 Python 未处理: {sorted(sent - handled)}"
        assert handled <= sent, f"Python 处理但 JS 从不发送: {sorted(handled - sent)}"

    def test_settings_dialog_resolves_js_via_web_root(self):
        """JS 定位必须经 assets.WEB_ROOT（模块位于 ui/ 子目录，__file__ 相对
        定位会指错层），且组件文件真实存在。"""
        py = SETTINGS_PY.read_text(encoding="utf-8")
        assert "WEB_ROOT" in py, "settings_dialog.py 应通过 WEB_ROOT 定位 component.js"
        assert JS.exists() and JS.stat().st_size > 1000


class TestFrontendAssets:
    def test_css_asset_exists_with_key_selectors(self):
        assert CSS.exists(), f"样式表缺失: {CSS}（打包/拆分遗漏？）"
        css = CSS.read_text(encoding="utf-8")
        # 自有命名空间与 Streamlit 内部选择器各抽一个代表
        assert ".side-label" in css and ".ov-breakdown" in css
        assert '[data-testid="stSidebar"]' in css

    def test_js_balanced_and_poll_free(self):
        """语法结构粗校验（node --check 在 CI 外不一定可用）+ 禁止轮询回归。"""
        js = JS.read_text(encoding="utf-8")
        assert "setInterval(" not in js, (
            "设置按钮注入禁止回退到 setInterval 轮询"
            "（应使用 MutationObserver + rAF 保活，见组件文件头）"
        )
        for a, b in (("{", "}"), ("(", ")"), ("[", "]")):
            assert js.count(a) == js.count(b), f"component.js 括号不配平: {a}{b}"

    def test_entry_reexports_compat_surface(self):
        """兼容 re-export 面：拆分前 mask_tool.web.app 暴露的关键符号仍可导入。"""
        web = pytest.importorskip("mask_tool.web.app")
        for name in (
            "_safe_unzip", "_run_detection", "_run_masking", "_run_restore",
            "_results_to_dataframe", "_render_save_to_dir_panel", "_flash",
            "ZIP_MAX_ENTRIES", "ZIP_MAX_TOTAL_BYTES", "BATCHES_DIR",
            "TASK_STATE_KEYS", "_parse_custom_words",
        ):
            assert hasattr(web, name), f"兼容符号缺失: mask_tool.web.app.{name}"
