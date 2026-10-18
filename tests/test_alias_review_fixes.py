"""审查修复回归测试（2026-10-16 对抗式审查发现项）

覆盖：
- M-1（A/B 双确认 Major）：detect_name 改写 source="path" 击穿 alias 风格
  边界——文件名中的手机号（regex-CUSTOM）曾获得伪装词"甲"；修复为
  origin_source 保留真实来源，regex 实体保持 token 样式
- M-2（审查B Major）：配置文件 mask_style 曾是死配置（CLI 默认值/侧栏
  无条件覆盖）；修复为 CLI Optional + Web 初值回退配置文件
- A-Minor-1：token 样式词混入自定义池时与 fallback 复用同一替换串
- A-Minor-2：XML 实体未反转义导致含 & 的自定义池词漏避让
- 降级透出：池耗尽/超限不再静默（CLI/Web 收尾警告）
- 覆盖缺口：PathMasker × alias 文件名 roundtrip、docx 混合批次
"""

import json
from pathlib import Path

import pytest
import yaml
from docx import Document
from typer.testing import CliRunner

import mask_tool.cli as cli_module
from mask_tool.cli import app
from mask_tool.core.alias_generator import AliasGenerator, AliasLexicon
from mask_tool.core.detector import Detector
from mask_tool.core.masker import Masker
from mask_tool.core.path_masker import PathMasker
from mask_tool.core.pipeline import Pipeline
from mask_tool.core.policy import PolicyEngine
from mask_tool.core.templates import (
    DEFAULT_CONFIG_YAML, SAMPLE_LEXICON_YAML, WHITELIST_YAML,
)
from mask_tool.models.config import MaskConfig
from mask_tool.models.detection import DetectionType


@pytest.fixture()
def runner():
    return CliRunner(env={"COLUMNS": "400"})


@pytest.fixture(autouse=True)
def _cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# M-1：文件名路径的 alias 风格边界（origin_source 修复）
# ---------------------------------------------------------------------------

class TestFilenameAliasBoundary:
    def _path_masker(self, lexicon=None):
        detector = Detector(lexicon or {"person": ["李海富"]}, set())
        policy = PolicyEngine(MaskConfig(mode="smart"))
        gen = AliasGenerator()
        return PathMasker(detector, policy, gen), gen

    def test_phone_in_filename_keeps_token_style(self):
        """文件名手机号（regex-CUSTOM）必须拿占位符而非伪装词"甲" """
        pm, gen = self._path_masker()
        new_name, detections = pm.build_new_name("李海富13800138000合同.docx")
        assert "[CUSTOM_001]" in new_name, f"手机号应保持 token 样式: {new_name}"
        assert "汤姆" in new_name, f"词库人名应得伪装词: {new_name}"
        assert "13800138000" not in new_name
        assert "李海富" not in new_name
        # custom 池未被数字类实体消耗
        assert "甲" not in gen.get_used_aliases()

    def test_display_source_and_origin_source(self):
        """展示语义不变：source="path"（表格"📄 文件名"），origin_source 保留真实来源"""
        pm, _ = self._path_masker()
        _, detections = pm.build_new_name("李海富13800138000合同.docx")
        d_map = {r.text: r for r in detections}
        assert d_map["李海富"].source == "path"
        assert d_map["李海富"].origin_source == "dictionary"
        assert d_map["13800138000"].source == "path"
        assert d_map["13800138000"].origin_source == "regex"

    def test_same_entity_content_and_filename_consistent(self):
        """同一实体在正文与文件名中风格一致（memo + 真实来源）"""
        lexicon = {"person": ["李海富"]}
        detector = Detector(lexicon, set())
        policy = PolicyEngine(MaskConfig(mode="smart"))
        gen = AliasGenerator()
        masker = Masker(gen)

        # 正文先行（engine 路径，source=dictionary）
        from mask_tool.models.detection import (
            DetectionResult, DetectionStatus, Location,
        )
        results = [DetectionResult(
            text="李海富", text_type=DetectionType.PERSON, source="dictionary",
            confidence=0.95, location=Location(file="a"),
            status=DetectionStatus.AUTO_MASK,
        )]
        assert masker.mask_text("李海富负责", results) == "汤姆负责"

        # 文件名后行（detect_name 改写 source=path，origin_source=dictionary）
        pm = PathMasker(detector, policy, gen)
        new_name, _ = pm.build_new_name("李海富合同.docx")
        assert new_name == "汤姆合同.docx"

        # 反序：文件名先行也必须得伪装词（首次决策不因 path 改写而走 token）
        gen2 = AliasGenerator()
        pm2 = PathMasker(detector, policy, gen2)
        new_name2, _ = pm2.build_new_name("李海富合同.docx")
        assert new_name2 == "汤姆合同.docx"

    def test_filename_alias_roundtrip_via_unmask_tree(self, tmp_path):
        """PathMasker × alias：改名产物经 unmask_tree 还原原名（paths 段）"""
        lexicon = {"person": ["李海富"]}
        detector = Detector(lexicon, set())
        policy = PolicyEngine(MaskConfig(mode="smart"))
        gen = AliasGenerator()
        pm = PathMasker(detector, policy, gen)

        src_dir = tmp_path / "tree"
        src_dir.mkdir()
        f = src_dir / "李海富合同.docx"
        Document().save(str(f))

        result = pm.mask_tree(src_dir)
        assert any("汤姆" in pm_.new_name for pm_ in result.renamed), \
            [pm_.new_name for pm_ in result.renamed]
        # 还原（load_paths 还原 PathMapping 对象，与 mapping.json 消费方同口径）
        mapping_payload = {"paths": pm.export_mappings()}
        dummy = PathMasker(None, None, None)
        restored = dummy.unmask_tree(
            src_dir, PathMasker.load_paths(mapping_payload))
        assert (src_dir / "李海富合同.docx").exists()


# ---------------------------------------------------------------------------
# M-2：mask_style 配置真实生效
# ---------------------------------------------------------------------------

class TestMaskStyleConfig:
    def _setup_config_dir(self, mask_style="alias"):
        cfgdir = Path("config")
        cfgdir.mkdir(parents=True, exist_ok=True)
        default = DEFAULT_CONFIG_YAML.replace(
            "mask_style: token", f"mask_style: {mask_style}")
        (cfgdir / "default.yaml").write_text(default, encoding="utf-8")
        (cfgdir / "whitelist.yaml").write_text(WHITELIST_YAML, encoding="utf-8")
        (cfgdir / "sample_lexicon.yaml").write_text(
            SAMPLE_LEXICON_YAML, encoding="utf-8")
        (cfgdir / "lexicon.yaml").write_text(
            yaml.safe_dump({"person": ["张三"]}, allow_unicode=True),
            encoding="utf-8")

    def _make_docx(self):
        doc = Document()
        doc.add_paragraph("张三签署本文件。")
        doc.save("input.docx")

    def test_config_alias_effective_without_cli_flag(self, runner):
        """不传 --style 时配置文件 mask_style: alias 必须生效（曾是死配置）"""
        self._setup_config_dir("alias")
        self._make_docx()
        r = runner.invoke(app, ["mask", "input.docx", "--no-mask-names"])
        assert r.exit_code == 0, r.output
        assert "风格: alias" in r.output
        batch_dirs = list(Path("output").iterdir())
        mapping = json.loads(
            (batch_dirs[0] / "mapping.json").read_text(encoding="utf-8"))
        assert mapping["metadata"]["style"] == "alias"
        assert any("[" not in k for k in mapping["tokens"])

    def test_cli_flag_overrides_config(self, runner):
        """显式 --style token 覆盖配置文件的 alias"""
        self._setup_config_dir("alias")
        self._make_docx()
        r = runner.invoke(
            app, ["mask", "input.docx", "--no-mask-names", "--style", "token"])
        assert r.exit_code == 0, r.output
        assert "风格: token" in r.output
        batch_dirs = list(Path("output").iterdir())
        mapping = json.loads(
            (batch_dirs[0] / "mapping.json").read_text(encoding="utf-8"))
        assert mapping["metadata"]["style"] == "token"

    def test_invalid_style_value_rejected(self, runner):
        self._setup_config_dir("token")
        self._make_docx()
        r = runner.invoke(
            app, ["mask", "input.docx", "--style", "bogus"])
        assert r.exit_code == 1
        assert "token 或 alias" in r.output

    def test_sidebar_default_falls_back_to_config(self, tmp_path, monkeypatch):
        """侧栏初值回退链：无持久化偏好时取配置文件 mask_style"""
        from mask_tool.web.ui.sidebar import _config_default_mask_style

        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "default.yaml").write_text(
            "mode: smart\nmask_style: alias\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert _config_default_mask_style() == "alias"

        # 配置缺失/非法 → token
        (tmp_path / "config" / "default.yaml").write_text(
            "mode: smart\nmask_style: bogus\n", encoding="utf-8")
        assert _config_default_mask_style() == "token"


# ---------------------------------------------------------------------------
# A-Minor-1：token 样式词混入自定义池
# ---------------------------------------------------------------------------

class TestTokenStyleWordInPool:
    def test_pool_token_lookalike_rejected(self):
        lex = AliasLexicon(custom_pool=["[CUSTOM_001]", "乙"])
        g = AliasGenerator(lex)
        a = g.generate("词一", DetectionType.CUSTOM, source="manual")
        b = g.generate("词二", DetectionType.CUSTOM, source="manual")
        assert a == "乙"
        assert b == "[CUSTOM_001]"      # 池耗尽回退（此时该串未被 alias 占用）
        assert len(set(g.get_all_mappings().values())) == 2, "两原文不得同替换串"

    def test_fallback_skips_used_tokens(self):
        """极端场景（used 中已有 token 串）时 fallback 跳过，防双原文同词"""
        g = AliasGenerator(AliasLexicon(custom_pool=["甲"]))
        g._used_aliases.append("[CUSTOM_001]")   # 模拟外部混入
        g.generate("词一", DetectionType.CUSTOM, source="manual")  # 甲
        assert g.generate("词二", DetectionType.CUSTOM, source="manual") == "[CUSTOM_002]"


# ---------------------------------------------------------------------------
# A-Minor-2：XML 实体反转义避让
# ---------------------------------------------------------------------------

class TestXMLEntityAvoidance:
    def test_ampersand_word_avoided(self, tmp_path):
        """源文档含字面 "A&B"（XML 存为 A&amp;B）时，池内 "A&B" 不得用作伪装词"""
        alias_cfg = tmp_path / "alias_pool.yaml"
        alias_cfg.write_text(
            yaml.safe_dump({"custom": ["A&B", "乙"]}, allow_unicode=True),
            encoding="utf-8")
        src = tmp_path / "ent.docx"
        doc = Document()
        doc.add_paragraph("利润表A&B汇总。")
        doc.save(src)

        cfg = MaskConfig(
            mode="smart", mask_style="alias",
            alias_lexicon_path=str(alias_cfg), config_dir=str(tmp_path),
        )
        p = Pipeline(cfg, lexicon_path="nonexistent")
        p.prepare([src])
        got = p.token_gen.generate("某词", DetectionType.CUSTOM, source="manual")
        assert got == "乙", "A&B 应因源文档实体反转义命中而被避让"


# ---------------------------------------------------------------------------
# 降级透出（A-Minor-3）
# ---------------------------------------------------------------------------

class TestDegradationWarnings:
    def test_cli_warns_pool_exhaustion(self, runner):
        """词池耗尽回退占位符时 CLI 收尾必须有黄色警告"""
        cfgdir = Path("config")
        cfgdir.mkdir(parents=True, exist_ok=True)
        default = DEFAULT_CONFIG_YAML.replace("mask_style: token", "mask_style: alias")
        (cfgdir / "default.yaml").write_text(default, encoding="utf-8")
        (cfgdir / "whitelist.yaml").write_text(WHITELIST_YAML, encoding="utf-8")
        (cfgdir / "sample_lexicon.yaml").write_text(
            SAMPLE_LEXICON_YAML, encoding="utf-8")
        # 词库 person 仅 1 词；alias 池改到 1 人名 → 第 2 个实体必耗尽
        (cfgdir / "lexicon.yaml").write_text(
            yaml.safe_dump({"person": ["张三", "李四"]}, allow_unicode=True),
            encoding="utf-8")
        (cfgdir / "alias_lexicon.yaml").write_text(
            yaml.safe_dump({"person": ["汤姆"]}, allow_unicode=True),
            encoding="utf-8")
        doc = Document()
        doc.add_paragraph("张三与李四参会。")
        doc.save("input.docx")
        r = runner.invoke(app, ["mask", "input.docx", "--no-mask-names"])
        assert r.exit_code == 0, r.output
        assert "伪装词池已耗尽" in r.output

    def test_style_boundary_not_reported_as_exhaustion(self, runner):
        """风格边界回退（金额/手机号走 token）不是池耗尽，不得误报警告"""
        cfgdir = Path("config")
        cfgdir.mkdir(parents=True, exist_ok=True)
        default = DEFAULT_CONFIG_YAML.replace("mask_style: token", "mask_style: alias")
        (cfgdir / "default.yaml").write_text(default, encoding="utf-8")
        (cfgdir / "whitelist.yaml").write_text(WHITELIST_YAML, encoding="utf-8")
        (cfgdir / "sample_lexicon.yaml").write_text(
            SAMPLE_LEXICON_YAML, encoding="utf-8")
        (cfgdir / "lexicon.yaml").write_text(
            yaml.safe_dump({"person": ["张三"]}, allow_unicode=True),
            encoding="utf-8")
        doc = Document()
        doc.add_paragraph("张三电话13800138000，预算1.2亿。")
        doc.save("input.docx")
        r = runner.invoke(app, ["mask", "input.docx", "--no-mask-names"])
        assert r.exit_code == 0, r.output
        assert "伪装词池已耗尽" not in r.output, "风格边界回退被误报为池耗尽"

    def test_web_warn_helper(self):
        from types import SimpleNamespace
        from mask_tool.web.ui.service import _warn_alias_degradation
        import mask_tool.web.ui.service as svc

        warned = []
        orig_warning = svc.st.warning
        svc.st.warning = lambda msg, **kw: warned.append(msg)
        try:
            gen = AliasGenerator(AliasLexicon(person_pool=["汤姆"]))
            gen.generate("甲乙", DetectionType.PERSON)
            gen.generate("丙丁", DetectionType.PERSON)   # 耗尽 → fallback
            _warn_alias_degradation(SimpleNamespace(token_gen=gen))
        finally:
            svc.st.warning = orig_warning
        assert any("伪装词池已耗尽" in w for w in warned)

        # token 生成器（无这些属性）静默通过
        from mask_tool.core.tokenizer import TokenGenerator
        warned.clear()
        _warn_alias_degradation(SimpleNamespace(token_gen=TokenGenerator()))
        assert warned == []


# ---------------------------------------------------------------------------
# 覆盖缺口：docx 混合批次（alias 实体 + 金额/手机号 token 同文档）
# ---------------------------------------------------------------------------

class TestDocxMixedBatch:
    def test_mixed_roundtrip(self, tmp_path):
        from mask_tool.adapters.docx_adapter import DocxAdapter
        from mask_tool.adapters.restore import restore_file_content
        from mask_tool.core.engine import ReplacementEngine
        from mask_tool.core.tokenizer import TokenGenerator  # noqa: F401

        lexicon = {"company": ["某某科技有限公司"]}
        src = tmp_path / "mixed.docx"
        doc = Document()
        doc.add_paragraph("某某科技有限公司承接，电话13800138000。")
        doc.save(src)

        detector = Detector(lexicon, set())
        policy = PolicyEngine(MaskConfig(mode="smart"))
        masker = Masker(AliasGenerator())
        masked_path = DocxAdapter(detector, policy, masker).process(src, tmp_path)

        token_map = {m.token: {"original": m.original} for m in masker.mappings}
        assert "A集团" in token_map            # 实体 → alias
        assert "[CUSTOM_001]" in token_map      # 手机号 → token
        # 混合批次还原（gate 必须禁用，含 alias 无 '[' 的段落不漏）
        restored = tmp_path / "restored.docx"
        restore_file_content(masked_path, restored, token_map)
        texts = DocxAdapter.extract_paragraph_texts(restored)
        assert texts[0] == "某某科技有限公司承接，电话13800138000。"
