# -*- coding: utf-8 -*-
"""tests/test_desktop_terminate.py — 关闭清理静默化回归（2026-09-20）

背景：关软件时 cmd 黑框闪烁两次。根因：
1. taskkill 未带 CREATE_NO_WINDOW —— 无控制台 GUI 父进程下，Windows
   为该控制台子程序新建可见 cmd 窗口；
2. window.events.closed 回调与 main() finally 竞态双触发
   _terminate_tree，各跑一次 taskkill。

此处仅测函数级逻辑（不启动 GUI/webview 事件循环）。
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

desktop = pytest.importorskip("mask_tool.desktop")


@pytest.fixture(autouse=True)
def _reset_guard():
    """每个用例独立守卫状态（模块级标志）"""
    desktop._tree_terminated = False
    yield
    desktop._tree_terminated = False


def _fake_proc(returncode=None):
    proc = MagicMock()
    proc.poll.return_value = returncode  # None=仍在运行
    proc.pid = 4321
    return proc


class TestTerminateTree:
    @pytest.mark.skipif(desktop.os.name != "nt", reason="Windows taskkill 路径")
    def test_taskkill_silent_and_once(self):
        """双路径竞态只执行一次 taskkill，且带 CREATE_NO_WINDOW + stdin 重定向"""
        proc = _fake_proc(returncode=None)
        with patch.object(desktop.subprocess, "run") as run:
            desktop._terminate_tree(proc)
            desktop._terminate_tree(proc)  # finally 路径竞态重入
            assert run.call_count == 1
            args, kwargs = run.call_args
            assert args[0][:3] == ["taskkill", "/PID", "4321"]
            assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW
            assert kwargs["stdin"] == subprocess.DEVNULL

    @pytest.mark.skipif(desktop.os.name != "nt", reason="Windows taskkill 路径")
    def test_already_exited_skips_taskkill(self):
        """子进程已退出时不调 taskkill（异常清理路径复用同一守卫）"""
        proc = _fake_proc(returncode=0)
        with patch.object(desktop.subprocess, "run") as run:
            desktop._terminate_tree(proc)
            run.assert_not_called()

    @pytest.mark.skipif(desktop.os.name == "nt", reason="非 Windows 分支")
    def test_posix_terminate_once(self):
        proc = _fake_proc(returncode=None)
        desktop._terminate_tree(proc)
        desktop._terminate_tree(proc)
        proc.terminate.assert_called_once()
