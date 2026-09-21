# -*- coding: utf-8 -*-
"""tests/test_app_save_settings.py — 保存到目标文件夹功能回归（2026-09-20）

背景：桌面化后浏览器"下载"在 pywebview 窗口内不可用，交付改为
"保存到用户配置的目标文件夹"（config/app_settings.yaml 的 save_dir）。

覆盖：
- core/app_settings：读写持久化、合并保存、未设置空串、默认建议值、写失败
- web/app.py：_unique_target 重名序号、_write_artifacts 自动建目录与
  错误返回、_mask_result_artifacts 组装、保存面板未设置警告/保存成功
- desktop.py：pick_save_folder 原生目录选择 -> 写设置链路
"""

from unittest.mock import MagicMock, patch

import pytest

app_settings = pytest.importorskip("mask_tool.core.app_settings")
webapp = pytest.importorskip("mask_tool.web.app")
desktop = pytest.importorskip("mask_tool.desktop")


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """设置文件读写隔离到 tmp（不碰真实项目/ exe 目录）。"""
    root = tmp_path / "anchor"
    root.mkdir()  # 测试内可能需要在其下创建子目录
    monkeypatch.setattr(app_settings, "find_data_file", lambda rel: None)
    monkeypatch.setattr(
        app_settings, "writable_anchor_dir", lambda: root,
    )
    return root


# ──────────────────────────────────────────────
# core/app_settings
# ──────────────────────────────────────────────

class TestAppSettings:
    def test_set_and_get_roundtrip(self, isolated_settings):
        # 未设置时回退默认（安装目录下 output）
        assert app_settings.get_explicit_save_dir() == ""
        assert app_settings.get_save_dir() == app_settings.default_save_dir()
        assert app_settings.set_save_dir(r"D:\脱敏输出") is True
        assert app_settings.get_save_dir() == r"D:\脱敏输出"
        assert app_settings.get_explicit_save_dir() == r"D:\脱敏输出"

    def test_default_save_dir_is_anchor_output(self, isolated_settings):
        """默认=可写锚点（开发=源码树根 / frozen=exe 同级）下 output"""
        from unittest.mock import patch as _patch

        root = isolated_settings
        # 开发模式：锚点即 isolated_settings 根
        assert app_settings.default_save_dir() == str(root / "output")

        # frozen 场景：exe 目录为锚
        exe_dir = root / "app"
        exe_dir.mkdir()
        with _patch.object(app_settings, "writable_anchor_dir", lambda: exe_dir):
            assert app_settings.default_save_dir() == str(exe_dir / "output")

    def test_reset_to_default_via_empty_string(self, isolated_settings):
        app_settings.set_save_dir(r"D:\自定义")
        assert app_settings.get_save_dir() == r"D:\自定义"
        app_settings.set_save_dir("")  # 空串=恢复默认
        assert app_settings.get_explicit_save_dir() == ""
        assert app_settings.get_save_dir() == app_settings.default_save_dir()

    def test_save_preserves_other_keys(self, isolated_settings):
        app_settings.save_settings({"theme": "dark"})
        app_settings.set_save_dir(r"E:\out")
        settings = app_settings.load_settings()
        assert settings == {"theme": "dark", "save_dir": r"E:\out"}

    def test_save_failure_returns_false(self, tmp_path, monkeypatch):
        # 可写锚指向一个已存在的"文件"：mkdir 按目录创建失败 -> OSError
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file", encoding="utf-8")
        monkeypatch.setattr(app_settings, "find_data_file", lambda rel: None)
        monkeypatch.setattr(app_settings, "writable_anchor_dir", lambda: blocker)
        assert app_settings.set_save_dir(r"D:\x") is False

    def test_default_save_dir_no_longer_downloads(self, isolated_settings):
        """默认值已改为安装目录 output（不再指向下载目录）"""
        from pathlib import Path as _P
        d = app_settings.default_save_dir()
        assert _P(d).name == "output"
        assert str(_P.home() / "Downloads") != d

    def test_corrupt_settings_returns_empty(self, isolated_settings):
        p = isolated_settings / "config" / "app_settings.yaml"
        p.parent.mkdir(parents=True)
        p.write_text("::::[not yaml", encoding="utf-8")
        assert app_settings.load_settings() == {}
        # 损坏时同样回退默认，交付不中断
        assert app_settings.get_save_dir() == app_settings.default_save_dir()

    def test_default_save_dir_exists_check_removed(self, isolated_settings):
        """默认目录无需预先存在：首次保存时自动创建（_write_artifacts mkdir）"""
        d = app_settings.default_save_dir()
        from pathlib import Path as _P
        assert not _P(d).exists()  # 隔离锚点下尚不存在
        ok, payload = webapp._write_artifacts(_P(d), [("a.docx", b"x")])
        assert ok and (_P(d) / "a.docx").exists()


# ──────────────────────────────────────────────
# web/app.py 保存工具
# ──────────────────────────────────────────────

class TestUniqueTarget:
    def test_no_conflict(self, tmp_path):
        assert webapp._unique_target(tmp_path, "a.docx") == tmp_path / "a.docx"

    def test_conflict_appends_suffix(self, tmp_path):
        (tmp_path / "a.docx").write_bytes(b"1")
        (tmp_path / "a-1.docx").write_bytes(b"2")
        assert webapp._unique_target(tmp_path, "a.docx") == tmp_path / "a-2.docx"


class TestWriteArtifacts:
    def test_creates_dir_and_writes(self, tmp_path):
        target = tmp_path / "new" / "deep"
        ok, payload = webapp._write_artifacts(
            target, [("x.docx", b"abc"), ("m.json", b"{}")],
        )
        assert ok and len(payload) == 2
        assert (target / "x.docx").read_bytes() == b"abc"
        assert (target / "m.json").read_bytes() == b"{}"

    def test_duplicate_names_not_overwritten(self, tmp_path):
        (tmp_path / "x.zip").write_bytes(b"old")
        ok, payload = webapp._write_artifacts(tmp_path, [("x.zip", b"new")])
        assert ok and payload == [tmp_path / "x-1.zip"]
        assert (tmp_path / "x.zip").read_bytes() == b"old"

    def test_failure_returns_error(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("file", encoding="utf-8")
        ok, err = webapp._write_artifacts(blocker, [("x.zip", b"")])
        assert ok is False and isinstance(err, str)


class TestMaskResultArtifacts:
    def test_single_file_with_mapping(self):
        items = webapp._mask_result_artifacts({
            "download_kind": "file",
            "file_name": "合同.docx",
            "file_bytes": b"BB",
            "mapping_data": '{"tokens": []}',
            "batch_id": "B1",
        })
        assert items == [
            ("合同.docx", b"BB"),
            ("B1_mapping.json", b'{"tokens": []}'),
        ]

    def test_zip_kind(self):
        items = webapp._mask_result_artifacts({
            "download_kind": "zip",
            "zip_buffer": b"ZZ",
            "mapping_data": "",
            "batch_id": "B2",
        })
        assert items == [("B2_masked.zip", b"ZZ")]


class TestSavePanel:
    def _panel(self, save_dir):
        # 面板函数内 import get_save_dir -> patch 源模块属性即生效
        return patch.object(app_settings, "get_save_dir", lambda: save_dir)

    def test_not_set_warns_and_returns_false(self, monkeypatch):
        warnings = []
        monkeypatch.setattr(webapp.st, "warning", lambda m, **k: warnings.append(str(m)))
        monkeypatch.setattr(webapp.st, "button", lambda *a, **k: False)
        with self._panel(""):
            ok = webapp._render_save_to_dir_panel([("a.docx", b"x")], "mask", "脱敏文件")
        assert ok is False
        assert any("保存位置" in w for w in warnings)

    def test_save_on_click(self, tmp_path, monkeypatch):
        out = tmp_path / "out"
        monkeypatch.setattr(webapp.st, "warning", lambda *a, **k: None)
        monkeypatch.setattr(webapp.st, "caption", lambda *a, **k: None)
        # 只模拟点击"💾 保存…"按钮：恒 True 会连带触发"📂 打开保存
        # 文件夹"（os.startfile 弹资源管理器，R9）
        monkeypatch.setattr(
            webapp.st, "button",
            lambda label, *a, **k: str(label).startswith("💾"),
        )
        successes = []
        monkeypatch.setattr(webapp.st, "success", lambda m, **k: successes.append(str(m)))
        monkeypatch.setattr(webapp.st, "error", lambda m, **k: successes.append("ERR:" + str(m)))
        webapp.st.session_state.clear()
        with self._panel(str(out)):
            ok = webapp._render_save_to_dir_panel(
                [("a.docx", b"data"), ("m.json", b"{}")], "mask", "脱敏文件",
            )
        assert ok is True
        assert (out / "a.docx").read_bytes() == b"data"
        assert (out / "m.json").exists()
        assert successes and "已保存 2 个文件" in successes[0]
        assert webapp.st.session_state["last_saved_dir_mask"] == str(out)

    def test_save_failure_reports(self, tmp_path, monkeypatch):
        blocker = tmp_path / "blocker"
        blocker.write_text("file", encoding="utf-8")
        monkeypatch.setattr(webapp.st, "warning", lambda *a, **k: None)
        monkeypatch.setattr(webapp.st, "caption", lambda *a, **k: None)
        monkeypatch.setattr(
            webapp.st, "button",
            lambda label, *a, **k: str(label).startswith("💾"),
        )
        errors = []
        monkeypatch.setattr(webapp.st, "error", lambda m, **k: errors.append(str(m)))
        webapp.st.session_state.clear()
        with self._panel(str(blocker)):
            ok = webapp._render_save_to_dir_panel([("a.docx", b"x")], "mask", "脱敏文件")
        assert ok is False and errors


# ──────────────────────────────────────────────
# desktop.pick_save_folder
# ──────────────────────────────────────────────

class TestPickSaveFolder:
    def _api(self, dialog_result):
        window = MagicMock()
        window.create_file_dialog.return_value = dialog_result
        return desktop._DesktopApi([window]), window

    def test_writes_setting_and_returns_path(self):
        api, window = self._api([r"D:\脱敏输出"])
        captured = {}
        with patch.object(app_settings, "set_save_dir",
                          lambda p: captured.update(path=p) or True):
            result = api.pick_save_folder()
        assert result == r"D:\脱敏输出"
        assert captured["path"] == r"D:\脱敏输出"

    def test_cancel_returns_none(self):
        api, _ = self._api(None)  # 用户取消
        assert api.pick_save_folder() is None

    def test_no_window_returns_none(self):
        api = desktop._DesktopApi([])  # 窗口未就绪
        assert api.pick_save_folder() is None
