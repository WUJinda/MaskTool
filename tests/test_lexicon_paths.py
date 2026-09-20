# -*- coding: utf-8 -*-
"""tests/test_lexicon_paths.py — 词库路径解析回归（2026-09-20 修复）

背景：桌面/bat 启动时 cwd 不在项目根，web/app.py 侧边栏的
``Path("config/lexicon.yaml")`` 相对路径落空，写入时 config/ 目录
不存在直接 FileNotFoundError。

覆盖（core/config_loader 锚点链 + web/app.py 词库函数）：
- cwd 错位时 resolve_user_lexicon_path 命中源码树词库
- 全部锚点 miss 时锚定可写锚点（frozen=exe 同级 / 开发=源码树根）
- frozen 场景跳过 _MEIPASS 内置锚点（用户词库固定 exe 同级）
- load_config 内嵌模板回退（implicit）时锚点链命中源码树词库
- load_config implicit + frozen：内置词库迁移到 exe 同级可写目录
- 显式 config/default.yaml 场景不逃逸（sample 自动复制行为保留）
- web/_add_words_to_lexicon：config/ 目录缺失时自动创建并写入
- web/_save_learned_words：词库文件缺失时自动初始化而非静默丢弃
"""

import sys
from pathlib import Path

import pytest
import yaml

from mask_tool.core import config_loader as cl
from mask_tool.core.templates import (
    DEFAULT_CONFIG_YAML, SAMPLE_LEXICON_YAML, WHITELIST_YAML,
)


@pytest.fixture
def fake_tree(tmp_path, monkeypatch):
    """隔离的"源码树 + cwd"布局，避免读写真实项目文件。

    - <tmp>/srcroot/config/…      模拟源码树项目根（含词库）
    - <tmp>/elsewhere/            模拟错位的 cwd（无 config/）
    - monkeypatch config_loader.__file__ -> <tmp>/srcroot/src/mask_tool/core/config_loader.py
      使 parents[3] 恰为 <tmp>/srcroot
    """
    srcroot = tmp_path / "srcroot"
    core = srcroot / "src" / "mask_tool" / "core"
    core.mkdir(parents=True)
    fake_self = core / "config_loader.py"
    fake_self.write_text("# fake anchor for tests", encoding="utf-8")
    monkeypatch.setattr(cl, "__file__", str(fake_self))

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return {"srcroot": srcroot, "cwd": elsewhere}


@pytest.fixture
def frozen_env(tmp_path, fake_tree, monkeypatch):
    """在 fake_tree 基础上模拟 PyInstaller onedir 布局。

    - <tmp>/app/_internal/config/…  _MEIPASS 内置出厂词库
    - <tmp>/app/mask-tool.exe       exe（cwd 由 desktop.py 固定为 exe 目录）
    """
    app = tmp_path / "app"
    meipass = app / "_internal"
    (meipass / "config").mkdir(parents=True)
    (meipass / "config" / "lexicon.yaml").write_text(
        yaml.dump({"company": ["内置公司"]}, allow_unicode=True), encoding="utf-8",
    )
    (meipass / "config" / "whitelist.yaml").write_text(WHITELIST_YAML, encoding="utf-8")
    exe = app / "mask-tool.exe"
    exe.write_bytes(b"")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe), raising=False)
    monkeypatch.chdir(app)
    return {"app": app, "meipass": meipass, "exe": exe}


# ──────────────────────────────────────────────
# resolve_user_lexicon_path / 锚点链
# ──────────────────────────────────────────────

class TestResolveUserLexiconPath:
    def test_cwd_mismatch_hits_src_root(self, fake_tree):
        """cwd 错位（无 config/）时命中源码树已有词库——本次报错场景"""
        srcroot = fake_tree["srcroot"]
        (srcroot / "config").mkdir()
        (srcroot / "config" / "lexicon.yaml").write_text(
            yaml.dump({"company": ["甲公司"]}, allow_unicode=True), encoding="utf-8",
        )
        p = cl.resolve_user_lexicon_path()
        assert p == (srcroot / "config" / "lexicon.yaml").resolve()

    def test_all_miss_anchors_to_src_root(self, fake_tree):
        """全部锚点 miss（全新环境）时锚定源码树根，且不主动创建文件"""
        p = cl.resolve_user_lexicon_path()
        assert p == (fake_tree["srcroot"] / "config" / "lexicon.yaml").resolve()
        assert not p.exists()

    def test_frozen_skips_meipass_anchor(self, frozen_env):
        """frozen 时用户词库固定 exe 同级，不读写 _MEIPASS 内置副本"""
        p = cl.resolve_user_lexicon_path()
        assert p == (frozen_env["app"] / "config" / "lexicon.yaml").resolve()
        assert not (frozen_env["meipass"] / "config" / "lexicon.yaml") == p

    def test_cwd_config_takes_priority(self, fake_tree):
        """用户在 cwd 自建 config/lexicon.yaml 时优先尊重 cwd"""
        srcroot = fake_tree["srcroot"]
        (srcroot / "config").mkdir()
        (srcroot / "config" / "lexicon.yaml").write_text("{}", encoding="utf-8")
        cwd_cfg = fake_tree["cwd"] / "config"
        cwd_cfg.mkdir()
        (cwd_cfg / "lexicon.yaml").write_text(
            yaml.dump({"custom": ["cwd词"]}, allow_unicode=True), encoding="utf-8",
        )
        assert cl.resolve_user_lexicon_path() == (cwd_cfg / "lexicon.yaml").resolve()


# ──────────────────────────────────────────────
# load_config / finalize_paths
# ──────────────────────────────────────────────

class TestLoadConfigAnchors:
    def test_implicit_config_hits_src_root_lexicon(self, fake_tree):
        """cwd 无 default.yaml（内嵌模板回退）时，词库经锚点链命中源码树"""
        srcroot = fake_tree["srcroot"]
        (srcroot / "config").mkdir()
        (srcroot / "config" / "lexicon.yaml").write_text(
            yaml.dump({"company": ["甲公司"]}, allow_unicode=True), encoding="utf-8",
        )
        cfg, events = cl.load_config(None, "smart")
        assert Path(cfg.lexicon_path) == (srcroot / "config" / "lexicon.yaml").resolve()
        # 不应出现"词库文件不存在"警告
        assert not any("词库文件不存在" in msg for _, msg in events)

    def test_implicit_frozen_migrates_to_exe_dir(self, frozen_env):
        """frozen + 内置词库 -> 迁移到 exe 同级可写目录（便携用户数据）"""
        cfg, _ = cl.load_config(None, "smart")
        exe_side = frozen_env["app"] / "config" / "lexicon.yaml"
        assert Path(cfg.lexicon_path) == exe_side.resolve()
        assert exe_side.exists()
        data = yaml.safe_load(exe_side.read_text(encoding="utf-8"))
        assert data == {"company": ["内置公司"]}  # 内容来自出厂副本
        # 白名单同样迁移
        assert Path(cfg.whitelist_path) == (frozen_env["app"] / "config" / "whitelist.yaml").resolve()

    def test_explicit_config_does_not_escape(self, fake_tree):
        """回归保护：显式 default.yaml 场景词库锚在配置旁，sample 自动复制"""
        cwd = fake_tree["cwd"]
        cfgdir = cwd / "config"
        cfgdir.mkdir()
        (cfgdir / "default.yaml").write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
        (cfgdir / "sample_lexicon.yaml").write_text(SAMPLE_LEXICON_YAML, encoding="utf-8")
        # 源码树也有词库（诱惑逃逸），但显式配置不应使用它
        srcroot = fake_tree["srcroot"]
        (srcroot / "config").mkdir()
        (srcroot / "config" / "lexicon.yaml").write_text("{}", encoding="utf-8")

        cfg, events = cl.load_config(None, "smart")
        assert Path(cfg.lexicon_path) == (cwd / "config" / "lexicon.yaml").resolve()
        assert (cwd / "config" / "lexicon.yaml").exists()  # 从 sample 自动复制
        assert not any("词库文件不存在" in msg for _, msg in events)

    def test_resolve_data_path_default_semantics_unchanged(self, tmp_path, monkeypatch):
        """resolve_data_path 不传 extra_anchors 时锚定语义与旧版一致
        （cwd 隔离到空目录，候选全 miss，验证模板约定锚定链"""
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.chdir(empty)  # 隔离真实项目 cwd，避免命中项目词库
        base = tmp_path / "proj" / "config"
        base.mkdir(parents=True)
        p = cl.resolve_data_path("config/lexicon.yaml", base)
        assert p == (tmp_path / "proj" / "config" / "lexicon.yaml").resolve()  # 模板约定：父目录


# ──────────────────────────────────────────────
# web/app.py 词库函数
# ──────────────────────────────────────────────

class TestWebLexiconFunctions:
    @pytest.fixture(autouse=True)
    def _import_app(self):
        from mask_tool.web import app  # noqa: F401  确保 streamlit 可导入
        self.app = app

    def test_add_words_creates_lexicon_when_dir_missing(self, fake_tree):
        """本次线上报错复现：cwd 无 config/，录入词条应自动建目录并写入"""
        srcroot = fake_tree["srcroot"]
        (srcroot / "config").mkdir()
        (srcroot / "config" / "sample_lexicon.yaml").write_text(
            yaml.dump({"company": ["样例公司"]}, allow_unicode=True), encoding="utf-8",
        )
        target = srcroot / "config" / "lexicon.yaml"
        assert not target.exists()

        self.app._add_words_to_lexicon("company", "新公司A, 新公司B")

        assert target.exists()  # 目录自动创建，不再 FileNotFoundError
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
        assert data["company"] == ["样例公司", "新公司A", "新公司B"]  # sample 初始化 + 追加

    def test_add_words_appends_to_existing(self, fake_tree):
        """已有词库时追加且去重、不动其他类别"""
        srcroot = fake_tree["srcroot"]
        cfgdir = srcroot / "config"
        cfgdir.mkdir()
        (cfgdir / "lexicon.yaml").write_text(
            yaml.dump({"company": ["已有公司"], "person": ["张三"]}, allow_unicode=True),
            encoding="utf-8",
        )
        self.app._add_words_to_lexicon("company", "已有公司, 第二公司")
        data = yaml.safe_load((cfgdir / "lexicon.yaml").read_text(encoding="utf-8"))
        assert data["company"] == ["已有公司", "第二公司"]
        assert data["person"] == ["张三"]

    def test_save_learned_words_creates_missing_file(self, fake_tree):
        """词库文件（连同父目录）缺失时自动初始化并保存学习词"""
        from mask_tool.models.config import MaskConfig

        target = fake_tree["srcroot"] / "config" / "lexicon.yaml"
        assert not target.parent.exists()
        cfg = MaskConfig()
        cfg.lexicon_path = str(target)

        self.app._save_learned_words({"company": ["学到的公司"]}, cfg)

        data = yaml.safe_load(target.read_text(encoding="utf-8"))
        assert data == {"company": ["学到的公司"]}

    def test_get_lexicon_data_after_add(self, fake_tree):
        """侧边栏展示与写入共用同一文件：添加后立即可读回"""
        srcroot = fake_tree["srcroot"]
        (srcroot / "config").mkdir()
        (srcroot / "config" / "lexicon.yaml").write_text("{}", encoding="utf-8")
        self.app._add_words_to_lexicon("custom", "词条X")
        data = self.app._get_lexicon_data()
        assert data is not None and "词条X" in data["custom"]
