# -*- coding: utf-8 -*-
"""tests/test_desktop_single_instance.py — 单实例守卫回归（R8，2026-09-21）

背景：机器残留 35 组共 71 个 mask-tool 进程（历次启动/测试未清理），
旧实例模块缓存与磁盘新代码错位导致运行期 ImportError。守卫目标：
重复启动收敛到单实例；孤儿进程经确认清理；守卫自身异常绝不阻断启动。

此处测函数级逻辑（不启动 GUI/webview 事件循环）；_find_orphan_servers
的扫描精度（powershell 载体进程不混入）由真实环境手工/端到端验证。
"""

import json
import os
from unittest.mock import patch

import pytest

desktop = pytest.importorskip("mask_tool.desktop")


@pytest.fixture(autouse=True)
def _isolated_lock_dir(tmp_path, monkeypatch):
    """每个用例使用独立锁目录，互不污染真实锁文件。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    desktop._lock_path().parent.mkdir(parents=True, exist_ok=True)
    yield


class TestLockIO:
    def test_read_lock_empty_when_missing_or_corrupt(self, tmp_path):
        assert desktop._read_lock() == {}
        (tmp_path / "mask-tool").mkdir(parents=True, exist_ok=True)
        desktop._lock_path().write_text("not-json{", encoding="utf-8")
        assert desktop._read_lock() == {}

    def test_write_then_read_roundtrip(self):
        desktop._write_instance_lock(streamlit_pid=999999, port=12345)
        data = desktop._read_lock()
        assert data["desktop_pid"] == os.getpid()
        assert data["streamlit_pid"] == 999999
        assert data["port"] == 12345
        assert data["desktop_start_ft"] is not None  # Windows 下可取创建时间

    def test_release_removes_own_lock_only(self):
        desktop._write_instance_lock(1, 2)
        desktop._release_instance_lock()
        assert not desktop._lock_path().exists()

        # 他者接管的锁不误删
        desktop._write_instance_lock(1, 2)
        data = json.loads(desktop._lock_path().read_text(encoding="utf-8"))
        data["desktop_pid"] = 424242
        desktop._lock_path().write_text(json.dumps(data), encoding="utf-8")
        desktop._release_instance_lock()
        assert desktop._lock_path().exists()


class TestPidProbe:
    def test_current_process_alive_with_matching_ft(self):
        ft = desktop._pid_start_ft(os.getpid())
        assert ft is not None
        assert desktop._pid_alive(os.getpid(), ft) is True

    def test_ft_mismatch_rejected(self):
        """创建时间不一致（PID 复用）必须判死，否则会误唤起无关进程。"""
        ft = desktop._pid_start_ft(os.getpid())
        assert desktop._pid_alive(os.getpid(), ft + 12345) is False

    def test_dead_or_invalid_pid(self):
        assert desktop._pid_alive(0, None) is False
        assert desktop._pid_alive(-5, None) is False
        assert desktop._pid_alive(999_999_999, None) is False


class TestWaitAndFocus:
    def test_focus_succeeds_first_try(self):
        with patch.object(desktop, "_focus_existing_window", return_value=True):
            assert desktop._wait_and_focus(1, None, timeout_s=1) is True

    def test_owner_dead_returns_early(self):
        """宿主退出后立即停止等待，交上层接管（避免空等超时）。"""
        with patch.object(desktop, "_focus_existing_window", return_value=False), \
                patch.object(desktop, "_pid_alive", return_value=False):
            assert desktop._wait_and_focus(1, None, timeout_s=5, poll_s=1) is False

    def test_timeout_when_owner_alive_but_no_window(self):
        with patch.object(desktop, "_focus_existing_window", return_value=False), \
                patch.object(desktop, "_pid_alive", return_value=True):
            assert desktop._wait_and_focus(1, None, timeout_s=0.05, poll_s=0.02) is False


class TestGuard:
    def _no_orphans(self):
        return patch.object(desktop, "_find_orphan_servers", return_value=[])

    def _stale_lock(self, live_streamlit_pid=None):
        """构造陈旧锁：宿主已死（留下一个不存在的 PID），
        可选保留存活的 streamlit PID（真残留场景）。"""
        desktop._write_instance_lock(live_streamlit_pid or 0, 12345)
        data = json.loads(desktop._lock_path().read_text(encoding="utf-8"))
        data["desktop_pid"] = 999_999_999
        data["desktop_start_ft"] = None
        desktop._lock_path().write_text(json.dumps(data), encoding="utf-8")

    def test_clean_environment_allows_start(self):
        with self._no_orphans():
            assert desktop._guard_single_instance() is True

    def test_live_owner_focus_window_then_exit(self):
        """锁内宿主存活：等待窗口出现并前置成功 → 本次启动退出。"""
        desktop._write_instance_lock(1, 2)
        with self._no_orphans(), \
                patch.object(desktop, "_wait_and_focus", return_value=True):
            assert desktop._guard_single_instance() is False

    def test_live_owner_wait_timeout_notifies_without_cleanup(self):
        """宿主活但窗口始终未出现：只提示，绝不扫描/清理（R10 核心约束）。"""
        desktop._write_instance_lock(1, 2)
        with patch.object(desktop, "_wait_and_focus", return_value=False), \
                patch.object(desktop, "_notify_already_running") as mock_info, \
                patch.object(desktop, "_confirm_yesno") as mock_confirm, \
                patch.object(desktop, "_kill_tree") as mock_kill, \
                patch.object(desktop, "_find_orphan_servers") as mock_scan:
            assert desktop._guard_single_instance() is False
            mock_info.assert_called_once()
            mock_scan.assert_not_called()
            mock_confirm.assert_not_called()
            mock_kill.assert_not_called()

    def test_orphan_confirm_cancel_exits(self):
        self._stale_lock(live_streamlit_pid=os.getpid())
        with self._no_orphans(), \
                patch.object(desktop, "_confirm_yesno", return_value=False):
            assert desktop._guard_single_instance() is False

    def test_orphan_confirm_yes_cleans_lock_pids(self):
        """宿主已死：确认清理 → 杀掉锁内存活进程后正常启动。"""
        self._stale_lock(live_streamlit_pid=os.getpid())
        with self._no_orphans(), \
                patch.object(desktop, "_confirm_yesno", return_value=True), \
                patch.object(desktop, "_kill_tree") as mock_kill:
            assert desktop._guard_single_instance() is True
            mock_kill.assert_called_once_with(os.getpid())

    def test_scanned_orphans_merged_dedup(self):
        """扫描孤儿与锁内存活进程合并去重后一并确认。"""
        self._stale_lock(live_streamlit_pid=os.getpid())
        with patch.object(desktop, "_find_orphan_servers", return_value=[700, 800]), \
                patch.object(desktop, "_confirm_yesno", return_value=True), \
                patch.object(desktop, "_kill_tree") as mock_kill:
            assert desktop._guard_single_instance() is True
            killed = {c.args[0] for c in mock_kill.call_args_list}
            assert killed == {os.getpid(), 700, 800}

    def test_guard_internal_error_never_blocks(self):
        """守卫自身异常必须放行启动（设计约束）。"""
        with patch.object(desktop, "_read_lock", side_effect=RuntimeError("boom")):
            assert desktop._guard_single_instance() is True


class TestConfirmFallback:
    def test_confirm_yesno_non_windows_or_failure_cancels(self):
        # Windows 正常路径有真实弹窗，单测只覆盖失败兜底：MessageBox 异常 → 取消
        if os.name == "nt":
            import ctypes
            with patch.object(
                ctypes.windll.user32, "MessageBoxW",
                side_effect=OSError("no desktop"),
            ):
                assert desktop._confirm_yesno("x") is False
        else:
            assert desktop._confirm_yesno("x") is False
