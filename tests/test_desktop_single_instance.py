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


class TestGuard:
    def _no_orphans(self):
        return patch.object(desktop, "_find_orphan_servers", return_value=[])

    def test_clean_environment_allows_start(self):
        with self._no_orphans():
            assert desktop._guard_single_instance() is True

    def test_live_owner_focus_window_then_exit(self):
        """锁内宿主存活且窗口唤起成功 → 本次启动退出，不进孤儿流程。"""
        desktop._write_instance_lock(1, 2)
        with self._no_orphans(), \
                patch.object(desktop, "_focus_existing_window", return_value=True):
            assert desktop._guard_single_instance() is False

    def test_orphan_confirm_cancel_exits(self):
        desktop._write_instance_lock(1, 2)
        with self._no_orphans(), \
                patch.object(desktop, "_focus_existing_window", return_value=False), \
                patch.object(desktop, "_confirm_yesno", return_value=False):
            assert desktop._guard_single_instance() is False

    def test_orphan_confirm_yes_cleans_lock_pids(self):
        """确认清理 → 杀掉锁内存活进程后正常启动。"""
        desktop._write_instance_lock(1, 2)
        with self._no_orphans(), \
                patch.object(desktop, "_focus_existing_window", return_value=False), \
                patch.object(desktop, "_confirm_yesno", return_value=True), \
                patch.object(desktop, "_kill_tree") as mock_kill:
            assert desktop._guard_single_instance() is True
            mock_kill.assert_called_once_with(os.getpid())

    def test_scanned_orphans_merged_dedup(self):
        """扫描孤儿与锁内进程合并去重后一并确认。"""
        desktop._write_instance_lock(1, 2)
        with patch.object(desktop, "_find_orphan_servers", return_value=[700, 800]), \
                patch.object(desktop, "_focus_existing_window", return_value=False), \
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
