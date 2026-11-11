# -*- coding: utf-8 -*-
"""tests/test_legacy_office.py — .doc / .xls 旧版格式转换式适配测试（L2，2026-10-22）

两层覆盖：

- 确定性测试（Fake 转换器，无引擎依赖）：样本 ".doc/.xls" 实为 OOXML 内容
  仅扩展名不同，伪 convert_cached / convert_to 只做"改扩展名搬运"，即可在
  逻辑层等价模拟 转换 → 委托 OOXML 适配器 → 转回 的全链路（命名、分发、
  Location 展示路径、还原对称性、缓存去重、无引擎报错）。
- 真实引擎冒烟（skipif：本机无 pywin32+Office 或 LibreOffice 时跳过）：
  真实转换 .doc/.xls 样本，验证 mask 产物含 token 且原词消失、unmask 后
  原文回来（round-trip 内容级还原）。

真实引擎测试涉及启动 Word/Excel COM（每个转换约 1-3s），用例数控制在
最小集；转换统一走 office_convert（AutomationSecurity 禁宏策略在生产
链路生效，测试不专门覆盖宏执行——COM 侧无法安全构造带宏样本）。
"""

import io
import shutil
from pathlib import Path

import pytest
from docx import Document
from openpyxl import Workbook, load_workbook

from mask_tool.adapters import extract as extract_mod
from mask_tool.adapters import office_convert
from mask_tool.adapters.extract import detect_file_results, extract_texts
from mask_tool.adapters.restore import restore_file_content
from mask_tool.adapters.legacy_adapter import DocAdapter, XlsAdapter
from mask_tool.core.detector import Detector
from mask_tool.core.formats import SUPPORTED_MASK_EXTS
from mask_tool.core.masker import Masker
from mask_tool.core.pipeline import Pipeline
from mask_tool.core.policy import PolicyEngine
from mask_tool.core.tokenizer import TokenGenerator
from mask_tool.models.config import MaskConfig
from mask_tool.models.report import MaskReport

COMPANY = "某某科技有限公司"
PERSON = "李四"
PLAIN = "普通文本，不应被改动"

DEFAULT_LEXICON = {
    "company": [COMPANY],
    "person": [PERSON],
}


# ---------------------------------------------------------------------------
# 样本构造：".doc/.xls" 实为 OOXML 内容（仅扩展名不同）
# ---------------------------------------------------------------------------

def _make_doc_sample(path: Path, paragraphs=None) -> Path:
    """构造 .doc 样本：python-docx 内容存为 .doc 扩展名（Fake 转换语义下
    "转换"即扩展名搬运，内容保持 OOXML 可解析）。"""
    doc = Document()
    for text in (paragraphs or [f"甲方：{COMPANY}", f"项目负责人：{PERSON}", PLAIN]):
        doc.add_paragraph(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


def _make_xls_sample(path: Path, cells=None) -> Path:
    """构造 .xls 样本：openpyxl 内容存为 .xls 扩展名。"""
    wb = Workbook()
    ws = wb.active
    for ref, value in (cells or {"A1": f"供应商：{COMPANY}", "B2": PERSON, "C3": PLAIN}).items():
        ws[ref] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(path))
    return path


# ---------------------------------------------------------------------------
# Fake 转换器：扩展名搬运（内容本就是 OOXML）
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_convert(monkeypatch, tmp_path):
    """把 office_convert 的转换函数替换为"改扩展名复制"。

    convert_cached 仍走真实缓存逻辑（key 去重可测），仅底层 convert_to
    被替换；缓存目录固定在 tmp_path 下，随用例自动清理。
    """
    calls = {"to": 0}

    def fake_convert_to(src, target_suffix, out_dir, out_name=None):
        calls["to"] += 1
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        final = out_dir / (out_name or f"{Path(src).stem}{target_suffix}")
        shutil.copy2(src, final)
        return final

    monkeypatch.setattr(office_convert, "_cache_dir", lambda: tmp_path / "conv_cache")
    monkeypatch.setattr(office_convert, "convert_to", fake_convert_to)
    office_convert.clear_cache()
    return calls


@pytest.fixture(autouse=True)
def _clear_conv_cache():
    """用例前后清理转换缓存，隔离真实引擎测试的进程级状态。"""
    office_convert.clear_cache()
    yield
    office_convert.clear_cache()


def _make_adapter(adapter_cls=None, lexicon=None):
    detector = Detector(
        {k: list(v) for k, v in (lexicon or DEFAULT_LEXICON).items()}, set(),
    )
    policy = PolicyEngine(MaskConfig(mode="smart"))
    masker = Masker(TokenGenerator())
    cls = adapter_cls or DocAdapter
    return cls(detector, policy, masker), masker


def _read_docx_text(path: Path) -> str:
    return "\n".join(p.text for p in Document(str(path)).paragraphs)


def _read_xlsx_text(path: Path) -> str:
    # 伪转换产物是 .xls 扩展名的 OOXML 内容；openpyxl 按扩展名校验，
    # 经 BytesIO 读取绕开（生产链路只会拿到 .xlsx 后缀的真实转换产物）
    wb = load_workbook(io.BytesIO(Path(path).read_bytes()))
    try:
        return "\n".join(
            str(c.value) for ws in wb.worksheets for row in ws.iter_rows()
            for c in row if c.value is not None
        )
    finally:
        wb.close()


# ---------------------------------------------------------------------------
# formats 常量与分发
# ---------------------------------------------------------------------------

def test_formats_include_legacy():
    assert {".doc", ".xls"} <= set(SUPPORTED_MASK_EXTS)


# ---------------------------------------------------------------------------
# DocAdapter 全链路（Fake 转换）
# ---------------------------------------------------------------------------

class TestDocAdapter:
    def test_process_default_name_and_mask(self, tmp_path, fake_convert):
        sample = _make_doc_sample(tmp_path / "in" / "报告.doc")
        adapter, masker = _make_adapter(DocAdapter)
        out = adapter.process(sample, tmp_path / "out")
        assert out.name == "报告_masked.doc"
        assert out.exists()
        # 内容（OOXML 直读）：敏感词已替换为 token
        text = _read_docx_text(out)
        assert COMPANY not in text and PERSON not in text
        assert "[COMPANY_001]" in text or "[PERSON_001]" in text
        assert PLAIN in text
        # 映射表可还原
        token_map = {m.token: {"original": m.original} for m in masker.get_mappings()}
        assert token_map, "应产生映射"

    def test_process_output_name_tree_mode(self, tmp_path, fake_convert):
        sample = _make_doc_sample(tmp_path / "in" / "sub" / "合同.doc")
        adapter, _ = _make_adapter(DocAdapter)
        out = adapter.process(sample, tmp_path / "out", output_name="合同.doc")
        assert out == Path(tmp_path / "out" / "合同.doc")
        assert out.exists()

    def test_roundtrip_restore(self, tmp_path, fake_convert):
        sample = _make_doc_sample(tmp_path / "in" / "报告.doc")
        adapter, masker = _make_adapter(DocAdapter)
        out = adapter.process(sample, tmp_path / "out")
        token_map = {m.token: {"original": m.original} for m in masker.get_mappings()}

        restored = tmp_path / "restored" / "报告_restored.doc"
        restore_file_content(out, restored, token_map)
        text = _read_docx_text(restored)
        assert COMPANY in text and PERSON in text
        assert "[COMPANY_" not in text and "[PERSON_" not in text

    def test_roundtrip_restore_inplace(self, tmp_path, fake_convert):
        """CLI 目录模式原地还原语义（output_path == input_path）。"""
        sample = _make_doc_sample(tmp_path / "in" / "报告.doc")
        adapter, masker = _make_adapter(DocAdapter)
        out = adapter.process(sample, tmp_path / "out")
        token_map = {m.token: {"original": m.original} for m in masker.get_mappings()}
        restore_file_content(out, out, token_map)
        text = _read_docx_text(out)
        assert COMPANY in text and "[COMPANY_" not in text


# ---------------------------------------------------------------------------
# XlsAdapter 全链路（Fake 转换）
# ---------------------------------------------------------------------------

class TestXlsAdapter:
    def test_process_default_name_and_mask(self, tmp_path, fake_convert):
        sample = _make_xls_sample(tmp_path / "in" / "台账.xls")
        adapter, masker = _make_adapter(XlsAdapter)
        out = adapter.process(sample, tmp_path / "out")
        assert out.name == "台账_masked.xls"
        text = _read_xlsx_text(out)
        assert COMPANY not in text and PERSON not in text
        assert PLAIN in text
        assert masker.get_mappings()

    def test_roundtrip_restore(self, tmp_path, fake_convert):
        sample = _make_xls_sample(tmp_path / "in" / "台账.xls")
        adapter, masker = _make_adapter(XlsAdapter)
        out = adapter.process(sample, tmp_path / "out")
        token_map = {m.token: {"original": m.original} for m in masker.get_mappings()}
        restored = tmp_path / "restored" / "台账_restored.xls"
        restore_file_content(out, restored, token_map)
        text = _read_xlsx_text(restored)
        assert COMPANY in text and PERSON in text
        assert "[COMPANY_" not in text


# ---------------------------------------------------------------------------
# 提取 / 检测面（检测面 = 处理面）
# ---------------------------------------------------------------------------

class TestExtractSurface:
    def test_extract_texts_doc(self, tmp_path, fake_convert):
        sample = _make_doc_sample(tmp_path / "in" / "报告.doc")
        text = extract_texts(sample)
        assert COMPANY in text and PERSON in text and PLAIN in text

    def test_extract_texts_xls(self, tmp_path, fake_convert):
        sample = _make_xls_sample(tmp_path / "in" / "台账.xls")
        text = extract_texts(sample)
        assert COMPANY in text and PERSON in text

    def test_detect_results_location_points_to_original(self, tmp_path, fake_convert):
        """Location 展示原始 .doc/.xls 路径，临时 OOXML 路径不外泄。"""
        doc = _make_doc_sample(tmp_path / "in" / "报告.doc")
        xls = _make_xls_sample(tmp_path / "in" / "台账.xls")
        adapter, _ = _make_adapter(DocAdapter)
        for f in (doc, xls):
            results = detect_file_results(f, adapter.detector, adapter.policy)
            texts = {r.text for r in results}
            assert COMPANY in texts, f
            assert all(
                str(r.location.file) == str(f) for r in results if r.location
            ), f


# ---------------------------------------------------------------------------
# pipeline 分发与预扫描
# ---------------------------------------------------------------------------

def _bare_pipeline():
    """绕过配置加载的 Pipeline 最小形态（分发/预扫描用例共用）。"""
    cfg = MaskConfig(mode="smart")
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.detector = Detector(
        {k: list(v) for k, v in DEFAULT_LEXICON.items()}, set(),
    )
    pipeline.policy = PolicyEngine(cfg)
    pipeline.masker = Masker(TokenGenerator())
    pipeline.token_gen = pipeline.masker.engine.token_gen
    pipeline.report = MaskReport()
    pipeline.batch_id = "test-batch"
    pipeline.config = cfg
    return pipeline


def test_pipeline_process_file_dispatch(tmp_path, fake_convert):
    sample = _make_doc_sample(tmp_path / "in" / "报告.doc")
    pipeline = _bare_pipeline()

    out = pipeline.process_file(sample, tmp_path / "out")
    assert out is not None and out.name == "报告_masked.doc"
    assert pipeline.report.input_files == [str(sample)]


def test_pipeline_prescan_registers_existing_tokens(tmp_path, fake_convert):
    """预扫描（编号让位）经转换缓存读取 .doc 明文。"""
    sample = _make_doc_sample(
        tmp_path / "in" / "已含token.doc",
        paragraphs=[f"历史标记 [COMPANY_099] 与 {COMPANY}"],
    )
    pipeline = _bare_pipeline()

    reserved = pipeline.prepare([sample])
    assert "[COMPANY_099]" in reserved


# ---------------------------------------------------------------------------
# 转换缓存与引擎选择
# ---------------------------------------------------------------------------

def test_convert_cached_dedup(monkeypatch, tmp_path):
    """同 (路径, mtime, size, 目标) 二次请求不再转换。"""
    monkeypatch.setattr(
        office_convert, "_cache_dir", lambda: (tmp_path / "cache").mkdir(exist_ok=True) or (tmp_path / "cache")
    )
    calls = {"n": 0}

    def fake_convert_to(src, target_suffix, out_dir, out_name=None):
        calls["n"] += 1
        final = Path(out_dir) / (out_name or f"{Path(src).stem}{target_suffix}")
        shutil.copy2(src, final)
        return final

    monkeypatch.setattr(office_convert, "convert_to", fake_convert_to)
    office_convert.clear_cache()
    sample = _make_doc_sample(tmp_path / "in" / "报告.doc")
    first = office_convert.convert_cached(sample, ".docx")
    second = office_convert.convert_cached(sample, ".docx")
    assert calls["n"] == 1
    assert first == second and first.exists()
    # 内容变化（mtime 变化）后重新转换
    sample.write_bytes(sample.read_bytes() + b"\x00")
    office_convert.convert_cached(sample, ".docx")
    assert calls["n"] == 2


def test_engine_unavailable_error(monkeypatch, tmp_path):
    """无可用引擎时抛 EngineUnavailableError，含安装指引。"""
    monkeypatch.setattr(office_convert, "_engine_order", lambda: ["com", "soffice"])
    monkeypatch.setattr(office_convert.MSOfficeConverter, "available", staticmethod(lambda: False))
    monkeypatch.setattr(office_convert.LibreOfficeConverter, "available", staticmethod(lambda: False))
    sample = _make_doc_sample(tmp_path / "in" / "报告.doc")
    with pytest.raises(office_convert.EngineUnavailableError) as ei:
        office_convert.convert_to(sample, ".docx", tmp_path / "out")
    assert "LibreOffice" in str(ei.value)


def test_adapter_propagates_engine_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(office_convert, "_engine_order", lambda: ["com", "soffice"])
    monkeypatch.setattr(office_convert.MSOfficeConverter, "available", staticmethod(lambda: False))
    monkeypatch.setattr(office_convert.LibreOfficeConverter, "available", staticmethod(lambda: False))
    sample = _make_doc_sample(tmp_path / "in" / "报告.doc")
    adapter, _ = _make_adapter(DocAdapter)
    with pytest.raises(office_convert.EngineUnavailableError):
        adapter.process(sample, tmp_path / "out")


# ---------------------------------------------------------------------------
# COM watchdog（审查 M-2/M-1 修复验证，无引擎环境下确定性测试）
# ---------------------------------------------------------------------------

def test_com_watchdog_kills_on_timeout(monkeypatch, tmp_path):
    """COM 转换超时：强杀差集 PID 并抛 OfficeConvertError（不碰 COM 对象）。"""
    import threading as _threading

    monkeypatch.setattr(office_convert, "_CONVERT_TIMEOUT", 0.5)
    monkeypatch.setattr(
        office_convert.MSOfficeConverter, "available", staticmethod(lambda: True)
    )
    killed: list = []
    monkeypatch.setattr(
        office_convert.MSOfficeConverter, "_kill_pids",
        classmethod(lambda cls, pids: killed.extend(pids)),
    )

    started = _threading.Event()

    def hang_impl(self, src, target_suffix, tmp_out, file_format, word_side, pids_slot):
        started.set()
        pids_slot[0] = {424242}  # 模拟差集法已取到的实例 PID
        import time as _time
        _time.sleep(5)            # 模拟 COM 调用无限挂起（密码框/恢复框）

    monkeypatch.setattr(office_convert.MSOfficeConverter, "_convert_impl", hang_impl)

    sample = _make_doc_sample(tmp_path / "in" / "报告.doc")
    engine = office_convert.MSOfficeConverter()
    t0 = office_convert.time.monotonic()
    with pytest.raises(office_convert.OfficeConvertError, match="超时"):
        engine.convert(sample, ".docx", tmp_path / "out")
    elapsed = office_convert.time.monotonic() - t0
    assert elapsed < 3, "应在 watchdog 超时后立即返回，而非等挂起线程结束"
    assert 424242 in killed
    # 超时路径不残留 ~converting_* 临时件（审查 m-5）
    assert not list((tmp_path / "out").glob("~converting_*"))


def test_com_open_with_dummy_password(monkeypatch, tmp_path):
    """Word/Excel Open 均显式传哑密码（加密文档密码框挂死防护，M-1）。

    不启动真实 Office：拦截 collection.Open 验证位置参数第 5 位。
    """
    from unittest.mock import MagicMock

    calls: list = []

    class _FakeCollection:
        def Open(self, *args, **kwargs):
            calls.append(args or kwargs)
            raise office_convert.OfficeConvertError("stop-after-open")

    def fake_dispatch(app_cls):
        app = MagicMock()
        app.AutomationSecurity = None
        app.DisplayAlerts = None
        app.Visible = None
        app.Hwnd = 0
        app.Documents = _FakeCollection()
        app.Workbooks = _FakeCollection()
        app.Quit = lambda *a, **k: None
        return app

    import win32com.client as real_client

    # patch 真实模块方法：函数内 `import win32com.client` 会把父属性还原
    # 为真实模块，直接 mock 模块对象无效；只 patch DispatchEx 方法本身
    monkeypatch.setattr(real_client, "DispatchEx", fake_dispatch)
    import pythoncom  # noqa: F401 — 真实导入即可（CoInitialize 无副作用）

    engine = office_convert.MSOfficeConverter()
    sample = _make_doc_sample(tmp_path / "in" / "报告.doc")
    with pytest.raises(office_convert.OfficeConvertError, match="stop-after-open"):
        engine.convert(sample, ".docx", tmp_path / "out")
    # Word Open(FileName, ConfirmConversions, ReadOnly, AddToRecentFiles,
    #            PasswordDocument)：第 5 位哑密码
    assert calls[-1][2] is True and calls[-1][4] == office_convert._DUMMY_PASSWORD

    xlsx = _make_xls_sample(tmp_path / "in" / "台账.xls")
    with pytest.raises(office_convert.OfficeConvertError, match="stop-after-open"):
        engine.convert(xlsx, ".xlsx", tmp_path / "out")
    # Excel Open(FileName, UpdateLinks, ReadOnly, <Missing>, Password)
    assert calls[-1][4] == office_convert._DUMMY_PASSWORD


def test_convert_cached_filename_fingerprint(monkeypatch, tmp_path):
    """文件变化后缓存落新文件名（不覆盖旧缓存，审查 M-6）。"""
    monkeypatch.setattr(
        office_convert, "_cache_dir",
        lambda: (tmp_path / "cache").mkdir(exist_ok=True) or (tmp_path / "cache"),
    )
    monkeypatch.setattr(office_convert, "convert_to", lambda src, s, d, n=None: (
        (Path(d).mkdir(parents=True, exist_ok=True),
         shutil.copy2(src, Path(d) / (n or f"{Path(src).stem}{s}")))[1]
    ))
    office_convert.clear_cache()
    sample = _make_doc_sample(tmp_path / "in" / "报告.doc")
    first = office_convert.convert_cached(sample, ".docx")
    sample.write_bytes(sample.read_bytes() + b"x")  # mtime/size 变化
    second = office_convert.convert_cached(sample, ".docx")
    assert first != second and first.exists() and second.exists()


# ---------------------------------------------------------------------------
# 真实引擎冒烟（本机需有 MS Office 或 LibreOffice；CI 无引擎时跳过）
# ---------------------------------------------------------------------------

def _real_engine_ready() -> bool:
    """探测真实可用引擎：Word/Excel COM 已注册（查注册表，不启进程）
    或 soffice 可用。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "Word.Application\\CurVer"):
            return True
    except OSError:
        pass
    except ImportError:
        pass
    return office_convert.LibreOfficeConverter.available()


_REAL = pytest.mark.skipif(
    not _real_engine_ready(), reason="本机无可用转换引擎（MS Office COM / LibreOffice）",
)


def _excel_com_registered() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "Excel.Application\\CurVer"):
            return True
    except OSError:
        return office_convert.LibreOfficeConverter.available()
    except ImportError:
        return office_convert.LibreOfficeConverter.available()


_REAL_XLS = pytest.mark.skipif(
    not _excel_com_registered(), reason="本机无 Excel COM / LibreOffice",
)


@_REAL
@pytest.mark.real_engine
class TestRealEngineRoundtrip:
    """真实引擎 round-trip：转换质量与内容级还原（真 Word/Excel 参与）。

    已打 real_engine marker，默认被 pyproject addopts 排除；显式跑：
    pytest tests/test_legacy_office.py -m real_engine
    """

    def test_doc_mask_and_restore(self, tmp_path):
        # 真实 .doc 样本：先造 docx，经真实引擎转为 .doc
        src_docx = tmp_path / "src" / "报告.docx"
        _make_doc_sample(src_docx)
        src_doc = office_convert.convert_to(src_docx, ".doc", tmp_path / "src")

        adapter, masker = _make_adapter(DocAdapter)
        out = adapter.process(src_doc, tmp_path / "out")
        assert out.name == "报告_masked.doc"

        masked_docx = office_convert.convert_cached(out, ".docx")
        masked_text = _read_docx_text(masked_docx)
        assert COMPANY not in masked_text and PERSON not in masked_text
        token_map = {m.token: {"original": m.original} for m in masker.get_mappings()}
        assert "[COMPANY_001]" in masked_text

        restored = tmp_path / "restored" / "报告_restored.doc"
        restore_file_content(out, restored, token_map)
        restored_text = _read_docx_text(
            office_convert.convert_cached(restored, ".docx")
        )
        assert COMPANY in restored_text and PERSON in restored_text
        assert "[COMPANY_" not in restored_text

    @_REAL_XLS
    def test_xls_mask_and_restore(self, tmp_path):
        src_xlsx = tmp_path / "src" / "台账.xlsx"
        _make_xls_sample(src_xlsx)
        src_xls = office_convert.convert_to(src_xlsx, ".xls", tmp_path / "src")

        adapter, masker = _make_adapter(XlsAdapter)
        out = adapter.process(src_xls, tmp_path / "out")
        assert out.name == "台账_masked.xls"

        masked_xlsx = office_convert.convert_cached(out, ".xlsx")
        masked_text = _read_xlsx_text(masked_xlsx)
        assert COMPANY not in masked_text and PERSON not in masked_text
        token_map = {m.token: {"original": m.original} for m in masker.get_mappings()}

        restored = tmp_path / "restored" / "台账_restored.xls"
        restore_file_content(out, restored, token_map)
        restored_text = _read_xlsx_text(
            office_convert.convert_cached(restored, ".xlsx")
        )
        assert COMPANY in restored_text and PERSON in restored_text
        assert "[COMPANY_" not in restored_text
