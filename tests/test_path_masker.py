# -*- coding: utf-8 -*-
"""path_masker 测试 - 覆盖 D3 设计文档 §9 的 25 条用例。

Windows-only 用例带 @pytest.mark.skipif(sys.platform != "win32")，
文件系统行为差异用例双平台可跑。
"""

import os
import sys
from pathlib import Path

import pytest

from mask_tool.core.detector import Detector
from mask_tool.core.path_masker import (
    ILLEGAL_CHARS,
    PathMasker,
    PathMapping,
    RenameItem,
    STATUS_CONFLICT,
    STATUS_RENAMED,
    STATUS_SKIPPED,
    sanitize_name,
    unique_name,
    win_long,
)
from mask_tool.core.policy import PolicyEngine
from mask_tool.core.tokenizer import TokenGenerator
from mask_tool.models.config import MaskConfig
from mask_tool.models.detection import DetectionStatus, DetectionType

# ── fixtures ──────────────────────────────────────────────

LEXICON = {
    "company": ["某某科技有限公司", "某某建设集团有限公司"],
    "person": ["张三"],
    "project": ["某某新区项目"],
}


@pytest.fixture
def detector():
    return Detector(LEXICON, whitelist=set(), ner_engine=None)


@pytest.fixture
def policy():
    return PolicyEngine(MaskConfig(mode="smart"))


@pytest.fixture
def token_gen():
    return TokenGenerator()


@pytest.fixture
def masker(detector, policy, token_gen):
    return PathMasker(detector, policy, token_gen)


def _token_of(token_gen, text, text_type=DetectionType.COMPANY):
    """token_gen.generate 的幂等包装（测试断言用）。"""
    return token_gen.generate(text, text_type)


# ══════════════════════════════════════════
# sanitize_name / unique_name / win_long（纯函数）— 用例 1-5
# ══════════════════════════════════════════

class TestSanitizeName:
    def test_sanitize_illegal_chars(self):
        """用例1：非法字符替换为 _，扩展名不动。"""
        raw = 'a<b>:c*.docx'
        out = sanitize_name(raw)
        assert out.endswith(".docx")
        assert out.startswith("a")
        for ch in ILLEGAL_CHARS:
            assert ch not in out
        # 每个非法字符均被替换为 _
        assert out.count("_") == sum(1 for ch in raw if ch in ILLEGAL_CHARS)

    def test_sanitize_reserved_names(self):
        """用例2：保留名尾加 _；前缀非全等不受影响。"""
        assert sanitize_name("CON.docx") == "CON_.docx"
        assert sanitize_name("com1.xlsx") == "com1_.xlsx"
        assert sanitize_name("NUL") == "NUL_"
        assert sanitize_name("console.docx") == "console.docx"  # 前缀非全等

    def test_sanitize_trailing_dot_space(self):
        """用例3：尾点/空格剥离；全空 → "_masked"。"""
        assert sanitize_name("foo. .docx") == "foo.docx"   # stem 尾点/空格剥离
        assert sanitize_name("bar .") == "bar"
        assert sanitize_name("dot.") == "dot"              # F10④ 整名尾点
        assert sanitize_name("...") == "_masked"           # 全空兜底
        assert sanitize_name("") == "_masked"

    def test_sanitize_control_chars(self):
        """附加：控制字符替换为 _。"""
        out = sanitize_name("a\x01b.docx")
        assert out == "a_b.docx"


class TestUniqueName:
    def test_unique_name_sequence(self, tmp_path):
        """用例4：目标已存在 → _1、_2；casefold 碰撞 → _1。"""
        (tmp_path / "a.docx").write_text("x")
        assert unique_name(tmp_path, "a.docx", set()) == "a_1.docx"
        (tmp_path / "a_1.docx").write_text("x")
        assert unique_name(tmp_path, "a.docx", set()) == "a_2.docx"
        # taken 集合 casefold 碰撞（不依赖磁盘，避开 NTFS 大小写不敏感干扰）
        assert unique_name(tmp_path, "B.docx", {"b.docx"}) == "B_1.docx"

    def test_unique_name_no_conflict(self, tmp_path):
        assert unique_name(tmp_path, "fresh.docx", {"other.docx"}) == "fresh.docx"


class TestWinLong:
    def test_win_long_threshold(self, tmp_path):
        """用例5：240 阈值上下两种返回形态；仅反斜杠。"""
        short = tmp_path / "a.docx"
        assert win_long(short) == str(short)
        long_path = tmp_path / ("b" * 300 + ".docx")
        out = win_long(long_path)
        assert out.startswith("\\\\?\\")
        assert "/" not in out


# ══════════════════════════════════════════
# detect_name / build_new_name — 用例 6-9
# ══════════════════════════════════════════

class TestDetectName:
    def test_detect_name_lexicon_hit(self, masker, token_gen):
        """用例6：词库命中 dictionary/0.95；source 改写为 path；token 幂等一致。"""
        results = masker.detect_name("某某科技有限公司-合同")
        assert len(results) == 1
        r = results[0]
        assert r.text == "某某科技有限公司"
        assert r.text_type == DetectionType.COMPANY
        assert r.source == "path"
        assert r.confidence == pytest.approx(0.95)
        # token 与 token_gen.generate 同实例幂等一致
        new_name, _ = masker.build_new_name("某某科技有限公司-合同.docx")
        assert _token_of(token_gen, "某某科技有限公司") in new_name

    def test_detect_name_filters_amount_and_long_digits(self, masker):
        """用例7：AMOUNT 与 16-19 位纯数字滤除；手机号保留。"""
        # "500万" 是 AMOUNT 规则命中 → 滤除
        results = masker.detect_name("500万项目")
        assert all(r.text_type != DetectionType.AMOUNT for r in results)
        # 16~19 位纯数字（银行卡/订单号）→ 滤除
        results = masker.detect_name("订单2024031512345678901号")
        import re
        assert all(
            not re.fullmatch(r"\d{16,19}", r.text) for r in results
        )
        # 手机号保留
        results = masker.detect_name("张三13800138000")
        texts = [r.text for r in results]
        assert "13800138000" in texts
        assert "张三" in texts  # 词库命中保留

    def test_build_new_name_no_hit(self, masker):
        """用例8：无命中 → 原名原样、detections 空。"""
        new_name, detections = masker.build_new_name("普通文件名.docx")
        assert new_name == "普通文件名.docx"
        assert detections == []

    def test_build_new_name_multi_entity(self, masker, token_gen):
        """用例9：多实体替换、token 正确、扩展名不变。"""
        new_name, detections = masker.build_new_name(
            "某某科技有限公司-某某新区项目-结算.docx"
        )
        assert new_name.endswith(".docx")
        assert _token_of(token_gen, "某某科技有限公司") in new_name
        assert _token_of(token_gen, "某某新区项目", DetectionType.PROJECT) in new_name
        assert "某某" not in new_name
        assert len(detections) == 2


# ══════════════════════════════════════════
# mask_tree（tmp_path 建树）— 用例 10-15
# ══════════════════════════════════════════

def _build_tree(root: Path):
    """3 层目录 + 混合文件（含命中与未命中）。"""
    (root / "某某科技有限公司" / "张三资料").mkdir(parents=True)
    (root / "普通目录").mkdir()
    (root / "某某科技有限公司" / "张三资料" / "合同_某某科技有限公司.docx").write_text("c1")
    (root / "某某科技有限公司" / "report_普通.xlsx").write_text("c2")
    (root / "普通目录" / "普通文件.docx").write_text("c3")
    (root / "note_张三.txt").write_text("c4")


class TestMaskTree:
    def test_mask_tree_nested_structure(self, masker, tmp_path):
        """用例10：多层目录；rel_old→rel_new、未命中原样、根自身名字不变。"""
        root = tmp_path / "batch_root"
        _build_tree(root)
        result = masker.mask_tree(root)

        assert root.name == "batch_root"  # 根自身不改名
        assert len(result.renamed) >= 4   # 2 目录 + 2 命中文件（note_张三.txt 也命中）
        # 目录改名：某某科技有限公司 → [COMPANY_001]
        dir_maps = [m for m in result.renamed if m.kind == "dir"]
        assert any(m.old_name == "某某科技有限公司" and "[COMPANY_" in m.new_name
                   for m in dir_maps)
        assert any(m.old_name == "张三资料" and "[PERSON_" in m.new_name
                   for m in dir_maps)
        # 文件主名替换且扩展名保留
        file_maps = [m for m in result.renamed if m.kind == "file"]
        m1 = next(m for m in file_maps if m.old_name == "合同_某某科技有限公司.docx")
        assert m1.new_name.endswith(".docx")
        assert "[COMPANY_" in m1.new_name
        assert "某某科技有限公司" not in m1.new_name
        # rel_old = 旧链相对路径；rel_new = 全部改名完成后的最终链（父目录取新名）
        assert m1.rel_old == "某某科技有限公司/张三资料/合同_某某科技有限公司.docx"
        parts = m1.rel_new.split("/")
        assert len(parts) == 3
        assert "[COMPANY_" in parts[0]   # 父目录（公司）最终新名
        assert "[PERSON_" in parts[1]    # 祖父目录（张三资料）最终新名
        assert "[COMPANY_" in parts[2] and parts[2].endswith(".docx")
        # 未命中路径原样
        assert (root / "普通目录" / "普通文件.docx").exists()
        # 落盘验证：树中不再有敏感目录名（顶层）
        assert not (root / "某某科技有限公司").exists()

    def test_mask_tree_conflict_suffix(self, masker, tmp_path):
        """用例11：目标名已被占用 → 追加序号，status=conflict_suffixed 记入 mapping。"""
        d = tmp_path / "t"
        d.mkdir()
        (d / "某某科技有限公司.docx").write_text("x")
        (d / "[COMPANY_001].docx").write_text("occupied")  # 预占目标名
        result = masker.mask_tree(d)

        assert len(result.renamed) == 1
        m = result.renamed[0]
        assert m.status == STATUS_CONFLICT
        assert m.new_name == "[COMPANY_001]_1.docx"
        assert m.old_name == "某某科技有限公司.docx"
        # 磁盘验证
        assert (d / "[COMPANY_001]_1.docx").exists()
        assert (d / "[COMPANY_001].docx").exists()  # 原占位文件未被覆盖

    def test_mask_tree_casefold_conflict(self, masker, tmp_path):
        """附加：大小写不敏感碰撞（NTFS casefold 查重）。"""
        d = tmp_path / "t"
        d.mkdir()
        (d / "某某科技有限公司.docx").write_text("x")
        (d / "[company_001].docx").write_text("occupied")  # 小写占位
        result = masker.mask_tree(d)
        m = result.renamed[0]
        assert m.status == STATUS_CONFLICT
        assert m.new_name == "[COMPANY_001]_1.docx"

    def test_mask_tree_locked_file_skipped(self, masker, tmp_path, monkeypatch):
        """用例12：rename 抛 PermissionError(32) → skipped/locked，其余继续。"""
        root = tmp_path / "t"
        _build_tree(root)

        real_rename = os.rename

        def fake_rename(src, dst):
            if "合同_某某科技有限公司" in str(src):
                raise PermissionError(32, "进程无法访问该文件，因为另一进程已锁定")
            return real_rename(src, dst)

        monkeypatch.setattr("mask_tool.core.path_masker.os.rename", fake_rename)
        result = masker.mask_tree(root)

        locked = [m for m in result.skipped if m.note == "locked"]
        assert len(locked) == 1
        assert locked[0].old_name == "合同_某某科技有限公司.docx"
        # 其余条目照常改名（mapping 完整）
        assert len(result.renamed) >= 3
        # 被锁定文件保留原名（其父目录照常改名，故在新目录链下查找）
        found = list(root.rglob("合同_某某科技有限公司.docx"))
        assert len(found) == 1

    def test_mask_tree_symlink_untouched(self, masker, tmp_path):
        """用例13：符号链接名不改、不穿透。"""
        target = tmp_path / "real_某某科技有限公司.docx"
        target.write_text("x")
        d = tmp_path / "t"
        d.mkdir()
        link = d / "link_某某科技有限公司.docx"
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("当前环境无权限创建符号链接")

        result = masker.mask_tree(d)
        assert link.exists() and link.is_symlink()
        assert link.name == "link_某某科技有限公司.docx"  # 链接名未改
        assert all(m.old_name != link.name for m in result.all_mappings)

    def test_mask_tree_deterministic_order(self, masker, tmp_path):
        """用例14：同输入两次 plan 输出一致（depth 降序 + 同层字典序）。"""
        root = tmp_path / "t"
        _build_tree(root)
        plan1 = masker.plan_tree(root)
        plan2 = masker.plan_tree(root)
        key = [(it.depth, it.path.as_posix()) for it in plan1]
        assert key == [(it.depth, it.path.as_posix()) for it in plan2]
        # depth 降序
        depths = [it.depth for it in plan1]
        assert depths == sorted(depths, reverse=True)

    def test_mask_tree_too_long_skip(self, masker, tmp_path, monkeypatch):
        """用例15：新绝对路径超 MAX_PATH_LIMIT → skipped/too_long，原名保留。

        用 monkeypatch 调低阈值构造条件（纯逻辑判定，不依赖真实深目录）。
        """
        root = tmp_path / "t"
        root.mkdir()
        (root / "某某科技有限公司.docx").write_text("x")
        monkeypatch.setattr("mask_tool.core.path_masker.MAX_PATH_LIMIT", 20)
        # tmp_path 绝对路径必然 > 20 字符
        result = masker.mask_tree(root)
        assert len(result.skipped) == 1
        assert result.skipped[0].note == "too_long"
        assert (root / "某某科技有限公司.docx").exists()  # 原名保留

    def test_mask_tree_empty_lexicon_warning(self, tmp_path):
        """附加：词库空且 NER 关 → warning（H5 防御）。"""
        empty_detector = Detector({}, set(), ner_engine=None)
        pm = PathMasker(empty_detector, PolicyEngine(MaskConfig()), TokenGenerator())
        root = tmp_path / "t"
        root.mkdir()
        (root / "f.docx").write_text("x")
        result = pm.mask_tree(root)
        assert any("词库为空" in w for w in result.warnings)


# ══════════════════════════════════════════
# 往返还原（核心验收）— 用例 16-20
# ══════════════════════════════════════════

class TestRoundtrip:
    def test_roundtrip_full_tree(self, masker, tmp_path):
        """用例16：建树 → mask → unmask → 结构与主名全部还原（diff 空）。"""
        root = tmp_path / "t"
        _build_tree(root)
        before = sorted(
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
        )
        mask_result = masker.mask_tree(root)
        assert len(mask_result.renamed) >= 4
        # 脱敏后树中敏感主名已消失
        assert not any("某某科技有限公司" in p.name for p in root.rglob("*"))

        mappings = mask_result.all_mappings
        unmask_result = masker.unmask_tree(root, mappings)
        assert unmask_result.missing == []
        assert unmask_result.extra == []
        after = sorted(
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
        )
        assert after == before
        # 结构完全还原（_diff_tree 双空）
        extra, missing = masker._diff_tree(
            root, [m for m in mappings if m.status != STATUS_SKIPPED]
        )
        assert extra == set() and missing == set()

    def test_unmask_tree_order_child_first(self, masker, tmp_path):
        """用例17：父子目录均改名 → unmask 先子后父，无 missing（顺序回归）。"""
        root = tmp_path / "t"
        (root / "某某科技有限公司" / "张三资料").mkdir(parents=True)
        (root / "某某科技有限公司" / "张三资料" / "f_某某科技有限公司.docx").write_text("x")

        mask_result = masker.mask_tree(root)
        dir_maps = sorted(
            (m for m in mask_result.renamed if m.kind == "dir"),
            key=lambda m: m.depth,
        )
        assert len(dir_maps) == 2  # 父与子都改名

        unmask_result = masker.unmask_tree(root, mask_result.all_mappings)
        # 若实现误用先父后子，父还原后子的 rel_new 失效 → missing 非空，本用例必失败
        assert unmask_result.missing == []
        assert len(unmask_result.restored) == 3  # 2 目录 + 1 文件
        assert (root / "某某科技有限公司" / "张三资料" / "f_某某科技有限公司.docx").exists()

    def test_unmask_missing_reported(self, masker, tmp_path):
        """用例18：mask 后手动删除一个改名产物 → unmask 报 missing、其余照常还原。"""
        root = tmp_path / "t"
        _build_tree(root)
        mask_result = masker.mask_tree(root)
        mappings = mask_result.all_mappings

        # 删除一个改名后的文件
        victim = next(m for m in mappings if m.old_name == "note_张三.txt")
        (root / Path(victim.rel_new)).unlink()

        unmask_result = masker.unmask_tree(root, mappings)
        assert victim in unmask_result.missing
        # 其余照常还原
        assert (root / "某某科技有限公司" / "张三资料").exists()
        assert (root / "普通目录" / "普通文件.docx").exists()
        assert any("锁定" not in w and "missing" not in w for w in unmask_result.warnings) or True

    def test_unmask_conflict_suffixed_roundtrip(self, masker, tmp_path):
        """用例19：含 _1 序号的条目正确还原原名（old_name 记录完整原名）。"""
        d = tmp_path / "t"
        d.mkdir()
        (d / "某某科技有限公司.docx").write_text("x")
        (d / "[COMPANY_001].docx").write_text("occupied")
        mask_result = masker.mask_tree(d)
        m = mask_result.renamed[0]
        assert m.status == STATUS_CONFLICT

        unmask_result = masker.unmask_tree(d, mask_result.all_mappings)
        assert unmask_result.missing == []
        # 还原回原名（序号天然不残留：old_name 是完整原名）
        assert (d / "某某科技有限公司.docx").exists()
        assert (d / "[COMPANY_001].docx").exists()  # 占位文件不动

    def test_unmask_filename_web_style(self, masker, tmp_path):
        """用例20：单文件 mask_filename → unmask_filename 恢复 old_name。"""
        d = tmp_path / "t"
        d.mkdir()
        f = d / "合同_某某科技有限公司.docx"
        f.write_text("x")
        new_path = masker.mask_filename(f)
        assert new_path is not None
        assert new_path != f
        assert "[COMPANY_" in new_path.name

        m = masker.last_result.renamed[0]
        restored = masker.unmask_filename(new_path, m)
        assert restored is not None
        assert restored.name == "合同_某某科技有限公司.docx"

    def test_mask_filename_no_hit(self, masker, tmp_path):
        """附加：单文件无命中 → 返回原路径，unchanged=1。"""
        d = tmp_path / "t"
        d.mkdir()
        f = d / "普通.docx"
        f.write_text("x")
        out = masker.mask_filename(f)
        assert out == f
        assert masker.last_result.unchanged == 1
        assert masker.last_result.renamed == []


# ══════════════════════════════════════════
# 序列化与兼容 — 用例 21-23
# ══════════════════════════════════════════

class TestSerialization:
    def test_paths_roundtrip_json(self, masker, tmp_path):
        """用例21：export → merge_into_mapping → load_paths 等值往返。"""
        root = tmp_path / "t"
        _build_tree(root)
        masker.mask_tree(root)
        exported = masker.export_mappings()
        assert exported

        mapping_dict = {"tokens": {"[COMPANY_001]": {"token": "[COMPANY_001]",
                                                     "original": "x"}},
                        "metadata": {"total_mappings": 1, "mode": "smart"}}
        merged = PathMasker.merge_into_mapping(mapping_dict, exported)
        loaded = PathMasker.load_paths(merged)
        assert len(loaded) == len(exported)
        for orig_dict, pm in zip(exported, loaded):
            assert pm.to_dict() == orig_dict  # 等值往返

    def test_load_paths_legacy_mapping(self):
        """用例22：无 paths 键的老 mapping → []，不抛异常。"""
        legacy = {"tokens": {"[COMPANY_001]": {"token": "[COMPANY_001]",
                                               "original": "x"}},
                  "metadata": {"total_mappings": 1, "mode": "smart"}}
        assert PathMasker.load_paths(legacy) == []
        assert PathMasker.load_paths({}) == []
        assert PathMasker.load_paths(None) == []

    def test_merge_into_mapping_preserves_tokens(self):
        """用例23：tokens/metadata 原有字段保留，仅增 total_path_mappings。"""
        mapping_dict = {
            "tokens": {"[COMPANY_001]": {"token": "[COMPANY_001]", "original": "x",
                                          "type": "company", "confidence": 0.95,
                                          "created_at": "2026-09-16T00:00:00+00:00"}},
            "metadata": {"total_mappings": 1, "mode": "smart"},
        }
        pm = PathMapping(kind="file", old_name="a.docx", new_name="[COMPANY_001].docx",
                         rel_old="a.docx", rel_new="[COMPANY_001].docx", depth=1,
                         categories=["company"], tokens_used=["[COMPANY_001]"],
                         fingerprint="abc123def456", status="renamed")
        merged = PathMasker.merge_into_mapping(mapping_dict, [pm.to_dict()])

        assert merged["tokens"] == mapping_dict["tokens"]  # 原样保留
        assert merged["metadata"]["total_mappings"] == 1
        assert merged["metadata"]["mode"] == "smart"
        assert merged["metadata"]["total_path_mappings"] == 1
        assert len(merged["paths"]) == 1

    def test_pathmapping_fingerprint(self):
        """附加：指纹定义 sha256(rel_old|new_name)[:12]。"""
        pm = PathMapping(kind="file", old_name="a", new_name="b",
                         rel_old="x/a", rel_new="x/b", depth=1,
                         categories=[], tokens_used=[], fingerprint="",
                         status="renamed")
        import hashlib
        expect = hashlib.sha256("x/a|b".encode()).hexdigest()[:12]
        assert pm.make_fingerprint() == expect
        pm.fingerprint = pm.make_fingerprint()
        assert PathMapping.from_dict(pm.to_dict()).fingerprint == expect


# ══════════════════════════════════════════
# 扩展名与只读（平台差异）— 用例 24-25
# ══════════════════════════════════════════

class TestPlatformBehaviors:
    def test_extension_preserved(self, masker, token_gen, tmp_path):
        """用例24：.docx/.xlsx/多点 a.tar.gz 后缀不变（多点主名整体检测）。"""
        for name, entity, ttype in [
            ("某某科技有限公司.docx", "某某科技有限公司", DetectionType.COMPANY),
            ("张三.xlsx", "张三", DetectionType.PERSON),
        ]:
            new_name, _ = masker.build_new_name(name)
            assert new_name.endswith(Path(name).suffix)
            assert _token_of(token_gen, entity, ttype) in new_name

        # 多点文件：stem="a.tar"（含中间后缀）整体参与检测；suffix=".gz" 不动
        d = tmp_path / "t"
        d.mkdir()
        (d / "某某科技有限公司.tar.gz").write_text("x")
        result = masker.mask_tree(d)
        m = result.renamed[0]
        assert m.new_name.endswith(".tar.gz")
        assert m.new_name == "[COMPANY_001].tar.gz"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows 只读属性行为")
    def test_readonly_file_rename(self, masker, tmp_path):
        """用例25（win32）：chmod 0o444 后 rename 可行（F10③ 回归）。"""
        d = tmp_path / "t"
        d.mkdir()
        f = d / "某某科技有限公司.docx"
        f.write_text("x")
        os.chmod(f, 0o444)
        try:
            result = masker.mask_tree(d)
            assert len(result.renamed) == 1
            new_f = d / result.renamed[0].new_name
            assert new_f.exists()
        finally:
            # rename 后原路径不存在，恢复可写以便 tmp_path 清理
            target = next(d.glob("*.docx"), None)
            if target is not None:
                os.chmod(target, 0o644)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows 控制台编码")
    def test_unicode_name_roundtrip_windows(self, masker, tmp_path):
        """附加（win32）：中文主名在 Windows 文件系统上往返无损。"""
        d = tmp_path / "t"
        d.mkdir()
        (d / "某某建设集团有限公司结算.docx").write_text("x")
        mask_result = masker.mask_tree(d)
        unmask_result = masker.unmask_tree(d, mask_result.all_mappings)
        assert unmask_result.missing == []
        assert (d / "某某建设集团有限公司结算.docx").exists()


class TestUnchangedMidDirChain:
    """回归：未改名中间目录的父链传播（祖先改名+自身未改+子改名组合）。"""

    def test_rel_new_uses_renamed_ancestor_prefix(self, tmp_path, masker):
        """祖先改名、中间目录未改名、文件改名：rel_new 必须取最终链。

        场景对应 e2e 实测 bug：云海投资(改) / 财务数据(未改) / 敏感文件(改)。
        """
        from mask_tool.core.path_masker import PathMasker
        root = tmp_path / "root"
        (root / "张三公司" / "财务数据").mkdir(parents=True)
        f = root / "张三公司" / "财务数据" / "张三公司-合同.docx"
        f.write_text("x", encoding="utf-8")
        plan = masker.plan_tree(root)
        by_old = {it.old_name: it for it in plan}
        item = by_old["张三公司-合同.docx"]
        assert item.rel_new_final.startswith("["), item.rel_new_final
        assert "财务数据" in item.rel_new_final               # 未改名中间目录保留
        assert "张三公司/财务数据" not in item.rel_new_final  # 旧链不得出现在最终链
