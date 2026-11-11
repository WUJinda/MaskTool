"""旧版 Office 二进制格式（.doc / .xls）转换引擎（L1，2026-10-22）

.doc / .xls 是 OLE 复合二进制格式，Python 生态无可保真回写的原生库
（xlrd 仅只读、.doc 无任何纯 Python 读写库）。本模块提供「转换式适配」
的底座：把 .doc/.xls 转为 OOXML（.docx/.xlsx），复用 DocxAdapter /
XlsxAdapter 全套检测、脱敏、还原能力，再把产物转回原格式。

双引擎（自动选择，环境变量 MASK_TOOL_OFFICE_ENGINE=com|soffice 可强制）：

- MSOfficeConverter：Windows + 已装 MS Office 时经 COM 自动化转换
  （pywin32 为可选依赖，缺失时本引擎不可用）。要点：
  * DispatchEx 每次转换创建独立应用实例并退出，不干扰用户已开窗口；
  * AutomationSecurity=3（msoAutomationSecurityForceDisable）强制禁用宏，
    防止转换恶意文档时执行 VBA——安全红线，设置失败即拒绝转换
    （fail-closed，Office 默认值为 Low=允许执行）；
  * Open 显式传哑密码：加密文档的密码输入框是模态对话框，
    DisplayAlerts=0 / Visible=False 均不能抑制（对抗审查 A-M1 实测：
    不传密码时弹「密码」框挂死 56s～无界），传错密码则毫秒级抛错；
  * 全程 watchdog：COM 调用在工作线程执行，超时（_CONVERT_TIMEOUT）
    经 PID taskkill /T 强杀本工具创建的 Office 进程（强杀路径不触碰
    COM 对象，规避 RPC native 异常；DispatchEx 实例独立进程，不影响
    用户已开的窗口）；
  * Word FileFormat=0/12（.doc/.docx）；Excel FileFormat=56/51（.xls/.xlsx）。
- LibreOfficeConverter：soffice --headless --convert-to，跨平台兜底
  （Windows/macOS 安装路径探测 + PATH）。每次调用使用独立
  UserInstallation profile，避免与用户正在运行的 LibreOffice 实例
  冲突；模块级锁串行调用（soffice 同 profile 并发会互斥失败）；超时
  杀整个进程树（launcher 之外 soffice.bin 可能残留）。

进程级转换缓存（convert_cached）：预扫描（pipeline.prepare）与适配器
处理对同一文件会各请求一次转换，缓存按 (路径, mtime_ns, size, 目标后缀)
去重；缓存文件名含同一指纹——文件变化后 key 变化、产物落新文件，
不覆盖旧缓存（防并发读写互踩，对抗审查 A-M4/M6）。per-key 锁保证
同文件并发请求只做一次转换。缓存目录位于系统 temp，进程退出时 atexit
清理；模块加载时顺手清理超过 7 天的旧 `masktool_conv_*` 目录
（atexit 在强杀/异常退出下不执行的兜底）。

异常体系：
- EngineUnavailableError：无可用引擎（pywin32 缺失/Office 未装/soffice
  未找到），message 含安装指引；
- OfficeConvertError：引擎可用但转换失败（文件加密/损坏/引擎报错/
  超时），不回退其他引擎（换引擎大概率同样失败，白等一轮）。

已知限制（README 同步声明）：
- COM 转换在宿主进程内执行，Office 进程崩溃瞬间的 native RPC 异常
  理论上可击穿 Python（审查 A-M3）；根治需子进程隔离，当前以
  「假密码 + watchdog 强杀不触碰 COM 对象」缓解主风险路径；
- watchdog 强杀 Word 后，Office 的 Resiliency 机制可能在下次启动弹
  恢复对话框（仅发生在超时强杀之后；正常路径 Quit 干净退出）。
"""

import atexit
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

__all__ = [
    "OfficeConvertError",
    "EngineUnavailableError",
    "MSOfficeConverter",
    "LibreOfficeConverter",
    "convert_to",
    "convert_cached",
    "describe_engine",
    "clear_cache",
]

# Word/Excel COM 常量（避免依赖 win32com.client.constants 的 gen_py 缓存）。
# 保存格式由【目标后缀】决定：xls→xlsx 与 xlsx→xls 是两个不同的 FileFormat
_WORD_SAVE_FORMAT = {".doc": 0, ".docx": 12}       # wdFormatDocument97 / wdFormatXMLDocument
_EXCEL_SAVE_FORMAT = {".xls": 56, ".xlsx": 51}     # xlExcel8 / xlOpenXMLWorkbook

# COM AutomationSecurity：msoAutomationSecurityForceDisable（强制禁宏）
_MS_AUTO_SEC_FORCE_DISABLE = 3

# 转换超时（秒）：COM watchdog 与 LibreOffice subprocess 共用同一上限
_CONVERT_TIMEOUT = 180

# Open 显式哑密码：加密文档在密码框上静默失败而非挂死（审查 A-M1）
_DUMMY_PASSWORD = "#mask-tool-unset#"


class OfficeConvertError(Exception):
    """引擎可用但转换失败（文件加密/损坏/引擎报错/超时）。"""


class EngineUnavailableError(OfficeConvertError):
    """无可用转换引擎。message 附安装指引。"""

    GUIDANCE = (
        "处理 .doc/.xls 需要 Microsoft Office（Windows）或 LibreOffice"
        "（Windows/macOS）之一：\n"
        "  · Windows：安装 Office，或 pip install pywin32 后重试；\n"
        "  · 或安装 LibreOffice（https://www.libreoffice.org）后重试"
    )


# ---------------------------------------------------------------------------
# 引擎一：MS Office COM（pywin32）
# ---------------------------------------------------------------------------

class MSOfficeConverter:
    """经 COM 自动化调用本机 MS Word/Excel 做格式转换。

    pywin32 缺失时 :meth:`available` 返回 False；已装 pywin32 但 Office
    未安装时，:meth:`convert` 在创建应用实例阶段抛 EngineUnavailableError
    （外层据此回退 LibreOffice）。文件级失败（加密/损坏/超时）抛
    OfficeConvertError，外层不回退（换引擎大概率同样失败）。
    """

    name = "com"

    @staticmethod
    def available() -> bool:
        """pywin32 是否可导入（Office 是否装了要到真正 Dispatch 才知道）。"""
        try:
            import pythoncom  # noqa: F401
            import win32com.client  # noqa: F401
            return True
        except ImportError:
            return False

    # -- 内部：安全策略与进程管理 -------------------------------------------

    @staticmethod
    def _setup_app(app) -> None:
        """安全策略：强制禁宏（fail-closed）+ 静默弹窗 + 隐藏窗口。

        AutomationSecurity 设置失败直接抛错：Office 默认值为 Low（允许宏
        执行），静默降级会让本工具变成「代执行宏」的攻击面（审查 A-m1）。
        """
        try:
            app.AutomationSecurity = _MS_AUTO_SEC_FORCE_DISABLE
        except Exception as e:
            raise OfficeConvertError(
                f"无法设置 Office 宏禁用策略（AutomationSecurity），"
                f"为安全起见拒绝转换: {e}"
            )
        try:
            app.DisplayAlerts = 0  # Word: wdAlertsNone；Excel: False 同值
        except Exception:
            pass
        try:
            app.Visible = False
        except Exception:
            pass

    @staticmethod
    def _resolve_pid(app, word_side: bool) -> Optional[int]:
        """取实例 PID（兼容路径）：Excel Application.Hwnd / Word ActiveWindow.Hwnd
        经 GetWindowThreadProcessId 反查。必须在创建 app 的同一线程调用。
        Word 无窗口时返回 None（主路径改用进程差集法，见 _diff_pids）。"""
        try:
            import win32process

            hwnd = None
            if word_side:
                try:
                    hwnd = app.ActiveWindow.Hwnd
                except Exception:
                    pass
            if hwnd is None:
                hwnd = app.Hwnd  # Excel 2010+；Word 无此属性则抛错跳过
            if hwnd:
                _tid, pid = win32process.GetWindowThreadProcessId(int(hwnd))
                return pid or None
        except Exception:
            pass
        return None

    @staticmethod
    def _snapshot_pids(image_name: str) -> set:
        """枚举指定镜像名的进程 PID 集合（tasklist CSV，无额外依赖）。"""
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {image_name}",
                 "/FO", "CSV", "/NH"],
                capture_output=True, timeout=15,
            ).stdout.decode("utf-8", errors="ignore")
            pids = set()
            for line in out.splitlines():
                parts = [p.strip('"') for p in line.split('","')]
                if len(parts) >= 2 and parts[1].isdigit():
                    pids.add(int(parts[1]))
            return pids
        except Exception:
            return set()

    @classmethod
    def _diff_pids(cls, image_name: str, before: set) -> set:
        """DispatchEx 前后进程集合差 = 本次创建的实例（无窗口依赖，
        不碰 Normal.dotm；差集在毫秒窗口内取，与用户手动启动撞车的
        概率可忽略，且强杀仅在超时异常路径发生）。"""
        return cls._snapshot_pids(image_name) - before

    @staticmethod
    def _kill_process_tree(pid: int) -> None:
        """强杀进程树（仅限本工具 DispatchEx 创建的实例，非用户进程）。"""
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True, timeout=15,
            )
        except Exception:
            pass

    @classmethod
    def _kill_pids(cls, pids) -> None:
        for pid in pids or ():
            cls._kill_process_tree(pid)

    @staticmethod
    def _quit(app) -> None:
        """干净退出（正常路径）。强杀路径不调用本方法——不触碰 COM 对象，
        规避对已死进程 RPC 调用引发的 native 异常（审查 A-M3 缓解）。"""
        try:
            app.Quit(SaveChanges=0)
        except Exception:
            try:
                app.Quit()
            except Exception:
                pass

    # -- 转换核心（在 COM 工作线程内执行） -----------------------------------

    def _convert_impl(
        self,
        src: Path,
        target_suffix: str,
        tmp_out: Path,
        file_format: int,
        word_side: bool,
        pids_slot: List[set],
    ) -> None:
        """单次 COM 转换（线程内）：DispatchEx -> Open(哑密码) -> SaveAs。"""
        import pythoncom
        import win32com.client

        app_cls = "Word.Application" if word_side else "Excel.Application"
        image_name = "WINWORD.EXE" if word_side else "EXCEL.EXE"
        try:
            pythoncom.CoInitialize()
        except Exception as e:
            raise OfficeConvertError(f"COM 初始化失败: {e}")

        app = None
        doc = None
        try:
            # watchdog 需要的 PID：进程差集法（DispatchEx 前后快照差集），
            # 不依赖窗口/Normal.dotm（Documents.Add 会引入新挂点）；
            # COM 对象线程亲和，快照必须在工作线程内取
            before = self._snapshot_pids(image_name)
            try:
                # DispatchEx 创建独立实例：不干扰用户已开窗口，也不受其状态影响；
                # 动态 Dispatch 不需要 gen_py 缓存，PyInstaller 打包友好
                app = win32com.client.DispatchEx(app_cls)
            except Exception as e:
                raise EngineUnavailableError(
                    f"无法启动 {app_cls} COM 组件（Microsoft Office 可能未安装）: {e}"
                ) from e
            pids_slot[0] = self._diff_pids(image_name, before)
            self._setup_app(app)

            collection = getattr(app, "Documents" if word_side else "Workbooks")
            try:
                # 位置参数 + 哑密码防加密文档密码框挂死（审查 A-M1 实测
                # 56s～无界 → 毫秒级）。动态 Dispatch 不支持命名参数
                # （Open(FileName=...) 实测 TypeError）；Excel 第 4 位 Format
                # 有合法值约束（占位 0 报「Open 方法无效」），用
                # pythoncom.Missing（COM 省略值 DISP_E_PARAMNOTFOUND）占位。
                # Word  Documents.Open(FileName, ConfirmConversions, ReadOnly,
                #                       AddToRecentFiles, PasswordDocument, ...)
                # Excel Workbooks.Open(FileName, UpdateLinks, ReadOnly, Format,
                #                      Password, ...)
                if word_side:
                    doc = collection.Open(
                        str(src.resolve()), False, True, False, _DUMMY_PASSWORD,
                    )
                else:
                    doc = collection.Open(
                        str(src.resolve()), 0, True, pythoncom.Missing,
                        _DUMMY_PASSWORD,
                    )
            except Exception as e:
                raise OfficeConvertError(
                    f"Office 打开 {src.name} 失败：文档可能已加密（密码保护）、"
                    f"损坏或格式异常（{e}）"
                ) from e
            try:
                # SaveAs2/SaveAs 位置签名 (FileName, FileFormat)：
                # Word 2010+ 用 SaveAs2，Excel 无 SaveAs2 走 SaveAs
                try:
                    doc.SaveAs2(str(tmp_out.resolve()), file_format)
                except AttributeError:
                    doc.SaveAs(str(tmp_out.resolve()), file_format)
            finally:
                try:
                    doc.Close(SaveChanges=0)
                except Exception:
                    pass
        finally:
            if app is not None:
                self._quit(app)
            app = None  # 先释放 COM 引用再 CoUninitialize，防 IPC 残留
            doc = None
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

    # -- 转换入口（watchdog 包裹） -------------------------------------------

    def convert(
        self,
        src: Path,
        target_suffix: str,
        out_dir: Path,
        out_name: Optional[str] = None,
    ) -> Path:
        """转换单个文件到 target_suffix（".docx"/".xlsx"/".doc"/".xls"）。

        COM 调用在工作线程执行，超过 _CONVERT_TIMEOUT 强杀 Office 进程
        并抛 OfficeConvertError（审查 A-M2/B-M1：Office 未激活向导、恢复
        对话框、COM 死锁等都可能无限挂起）。
        """
        src = Path(src)
        out_dir = Path(out_dir)
        target_suffix = target_suffix.lower()

        src_suffix = src.suffix.lower()
        word_side = (target_suffix in (".doc", ".docx")
                     and src_suffix in (".doc", ".docx"))
        excel_side = (target_suffix in (".xls", ".xlsx")
                      and src_suffix in (".xls", ".xlsx"))
        if word_side:
            app_cls = "Word.Application"
        elif excel_side:
            app_cls = "Excel.Application"
        else:
            raise OfficeConvertError(
                f"不支持的转换方向: {src_suffix} -> {target_suffix}"
            )
        file_format = (_WORD_SAVE_FORMAT if word_side else _EXCEL_SAVE_FORMAT)[target_suffix]

        out_dir.mkdir(parents=True, exist_ok=True)
        final = out_dir / (out_name or f"{src.stem}{target_suffix}")
        # 临时件与 final 同目录（同卷，os.replace 原子替换的前提）；
        # 临时名保持目标后缀（部分 Office 版本按后缀校验 SaveAs 文件名）
        tmp_out = out_dir / f"~converting_{final.stem}{target_suffix}"

        pid_slot: List[set] = [set()]
        errors: List[BaseException] = []
        done = threading.Event()

        def _worker() -> None:
            try:
                self._convert_impl(
                    src, target_suffix, tmp_out, file_format, word_side, pid_slot,
                )
            except BaseException as e:  # 工作线程内异常带回主线程
                errors.append(e)
            finally:
                done.set()

        # daemon 线程：超时强杀 Office 后线程内的阻塞 COM 调用立即返回错误并
        # 自然退出；若 PID 丢失且线程真挂死，也不阻止宿主进程退出
        # （ThreadPoolExecutor 的非 daemon 线程 + with 块 join 会把挂死线程
        # 变成进程退出阻塞，故不用它）
        worker = threading.Thread(
            target=_worker, daemon=True, name="office-convert",
        )
        try:
            worker.start()
            if not done.wait(timeout=_CONVERT_TIMEOUT):
                # 超时：强杀本工具创建的 Office 进程（DispatchEx 独立实例 +
                # 差集窗口毫秒级，不波及用户已开窗口）；不触碰 COM 对象——
                # 对死进程的 RPC 调用可能以 native 异常击穿 Python
                # （审查 A-M3 缓解）。杀后工作线程的阻塞调用立即报错退出
                self._kill_pids(pid_slot[0] or ())
                raise OfficeConvertError(
                    f"Office 转换 {src.name} -> {target_suffix} 超时"
                    f"（>{_CONVERT_TIMEOUT}s，已强制结束本次启动的 Office 进程；"
                    "若反复出现请检查 Office 激活/恢复弹窗状态）"
                )
            if errors:
                raise errors[0]
        except BaseException:
            # 失败路径清理半成品临时件（SaveAs 中断/超时强杀后 ~converting_*
            # 可能残留，原地还原场景 out_dir 即用户源目录，审查 m-5/m-6）
            try:
                if tmp_out.exists():
                    tmp_out.unlink()
            except OSError:
                pass
            raise

        if not tmp_out.exists():
            raise OfficeConvertError(
                f"Office 转换 {src.name} -> {target_suffix} 未产出文件"
                "（文档可能受密码保护）"
            )
        # 原子替换：unlink+rename 之间崩溃会丢旧文件（审查 A-M5）
        os.replace(tmp_out, final)
        return final


# ---------------------------------------------------------------------------
# 引擎二：LibreOffice headless
# ---------------------------------------------------------------------------

_LO_FILTER_BY_SUFFIX = {
    ".docx": "MS Word 2007 XML",
    ".doc": "MS Word 97",
    ".xlsx": "Calc MS Excel 2007 XML",
    ".xls": "MS Excel 97",
}

# soffice 同 profile 并发互斥；不同 profile 虽可并行但进程极重，统一串行
_SOFFICE_LOCK = threading.Lock()


def _detect_soffice() -> Optional[str]:
    """探测 soffice 可执行文件：PATH → Windows/macOS 常见安装路径。"""
    exe = shutil.which("soffice")
    if exe:
        return exe
    candidates: List[Path] = []
    for base in (
        r"C:\Program Files\LibreOffice",
        r"C:\Program Files (x86)\LibreOffice",
    ):
        candidates.append(Path(base) / "program" / "soffice.exe")
    candidates.append(
        Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
    )
    for p in candidates:
        if p.is_file():
            return str(p)
    return None


def _kill_tree(proc: subprocess.Popen) -> None:
    """超时杀进程树：launcher 之外 soffice.bin 可能残留（审查 A-m4）。"""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True, timeout=15,
            )
        else:
            import signal

            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        proc.kill()


class LibreOfficeConverter:
    """soffice --headless --convert-to 转换（跨平台兜底引擎）。"""

    name = "soffice"

    @staticmethod
    def available() -> bool:
        return _detect_soffice() is not None

    def convert(
        self,
        src: Path,
        target_suffix: str,
        out_dir: Path,
        out_name: Optional[str] = None,
    ) -> Path:
        src = Path(src)
        out_dir = Path(out_dir)
        target_suffix = target_suffix.lower()
        filt = _LO_FILTER_BY_SUFFIX.get(target_suffix)
        soffice = _detect_soffice()
        if not soffice:
            raise EngineUnavailableError(EngineUnavailableError.GUIDANCE)
        if not filt:
            raise OfficeConvertError(
                f"LibreOffice 不支持的目标格式: {target_suffix}"
            )

        out_dir.mkdir(parents=True, exist_ok=True)
        final = out_dir / (out_name or f"{src.stem}{target_suffix}")
        # --convert-to 只能按源文件名落盘：转到独立临时目录再搬运到目标名
        tmp_out_dir = Path(tempfile.mkdtemp(prefix="masktool_lo_out_"))
        profile = Path(tempfile.mkdtemp(prefix="masktool_lo_profile_"))
        proc: Optional[subprocess.Popen] = None
        staged: Optional[Path] = None
        try:
            cmd = [
                soffice,
                f"-env:UserInstallation={profile.as_uri()}",
                "--headless", "--norestore", "--invisible", "--nolockcheck",
                "--convert-to", f"{target_suffix.lstrip('.')}:{filt}",
                "--outdir", str(tmp_out_dir),
                str(src.resolve()),
            ]
            with _SOFFICE_LOCK:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                try:
                    _out, _err = proc.communicate(timeout=_CONVERT_TIMEOUT)
                except subprocess.TimeoutExpired:
                    _kill_tree(proc)
                    raise OfficeConvertError(
                        f"LibreOffice 转换 {src.name} 超时（>{_CONVERT_TIMEOUT}s）"
                    )
            # 排除 ~$ owner 锁文件，优先与源同名的精确匹配（审查 A-m3）
            produced = [
                p for p in tmp_out_dir.rglob(f"*{target_suffix}")
                if not p.name.startswith("~$")
            ]
            if proc.returncode != 0 or not produced:
                stderr = (_err or b"").decode("utf-8", errors="ignore")[:200]
                stderr = re.sub(r"\s+", " ", stderr).strip()
                raise OfficeConvertError(
                    f"LibreOffice 转换 {src.name} -> {target_suffix} 失败"
                    f"（文档可能已加密或损坏）{('; ' + stderr) if stderr else ''}"
                )
            exact = [p for p in produced if p.stem == src.stem]
            winner = (exact or produced)[0]
            # 原子落盘（审查 A-M5）：produced 在系统 temp（可能跨卷），先搬到
            # final 同目录的临时名（同卷），再 os.replace 原子替换
            staged = final.parent / f"~converting_{final.stem}{target_suffix}"
            if staged.exists():
                staged.unlink()
            shutil.move(str(winner), str(staged))
            os.replace(staged, final)
            staged = None
            return final
        finally:
            if staged is not None:
                try:
                    staged.unlink()
                except OSError:
                    pass
            shutil.rmtree(tmp_out_dir, ignore_errors=True)
            shutil.rmtree(profile, ignore_errors=True)


# ---------------------------------------------------------------------------
# 引擎选择与高层入口
# ---------------------------------------------------------------------------

def _engine_order() -> List[str]:
    """引擎尝试顺序：环境变量强制 > COM 优先自动（非法值按 auto）。"""
    pref = (os.environ.get("MASK_TOOL_OFFICE_ENGINE") or "auto").strip().lower()
    if pref in ("com", "soffice"):
        return [pref]
    return ["com", "soffice"]


_ENGINES = {"com": MSOfficeConverter, "soffice": LibreOfficeConverter}


def convert_to(
    src: Path,
    target_suffix: str,
    out_dir: Path,
    out_name: Optional[str] = None,
) -> Path:
    """转换单个文件（引擎自动选择，不可用逐个回退）。

    仅 EngineUnavailableError（环境问题）触发回退；文件级失败
    （加密/损坏/超时）不回退——换引擎大概率同样失败，白等一轮。
    """
    src = Path(src)
    last_unavailable: Optional[EngineUnavailableError] = None
    for kind in _engine_order():
        engine_cls = _ENGINES[kind]
        if not engine_cls.available():
            continue
        engine = engine_cls()
        try:
            return engine.convert(src, target_suffix, out_dir, out_name)
        except EngineUnavailableError as e:
            last_unavailable = e  # 如 pywin32 在但 Office 未装 → 试下一引擎
            continue
    if last_unavailable is not None:
        raise last_unavailable
    raise EngineUnavailableError(EngineUnavailableError.GUIDANCE)


def describe_engine() -> str:
    """当前可用的转换引擎描述（日志/UI 提示用）。"""
    order = _engine_order()
    available = [k for k in order if _ENGINES[k].available()]
    if not available:
        return "无"
    return "、".join(available)


# ---------------------------------------------------------------------------
# 进程级转换缓存（预扫描与适配器处理共用，正向转换只做一次）
# ---------------------------------------------------------------------------

_CACHE: Dict[tuple, Path] = {}
_KEY_LOCKS: Dict[tuple, threading.Lock] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_DIR: Optional[Path] = None
_atexit_registered = False

# 旧缓存目录保留天数（进程强杀/异常退出后 atexit 不执行的兜底清理）
_CACHE_DIR_MAX_AGE_DAYS = 7


def _cache_dir() -> Path:
    global _CACHE_DIR, _atexit_registered
    with _CACHE_LOCK:
        if _CACHE_DIR is None:
            _cleanup_stale_cache_dirs()
            _CACHE_DIR = Path(tempfile.mkdtemp(prefix="masktool_conv_"))
        if not _atexit_registered:
            atexit.register(_cleanup_cache_dir)
            _atexit_registered = True
        return _CACHE_DIR


def _cleanup_cache_dir() -> None:
    global _CACHE_DIR
    if _CACHE_DIR is not None:
        shutil.rmtree(_CACHE_DIR, ignore_errors=True)
        _CACHE_DIR = None


def _cleanup_stale_cache_dirs() -> None:
    """清理超过 N 天的旧 masktool_conv_* 目录（尽力而为，失败静默）。"""
    try:
        root = Path(tempfile.gettempdir())
        cutoff = time.time() - _CACHE_DIR_MAX_AGE_DAYS * 86400
        for p in root.glob("masktool_conv_*"):
            try:
                if p.is_dir() and p.stat().st_mtime < cutoff:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                continue
    except Exception:
        pass


def clear_cache() -> None:
    """清空缓存（测试用；生产进程无需调用）。"""
    with _CACHE_LOCK:
        _CACHE.clear()
        _KEY_LOCKS.clear()
        _cleanup_cache_dir()


def convert_cached(src: Path, target_suffix: str) -> Path:
    """带进程级缓存的转换：同 (路径, mtime_ns, size, 目标后缀) 只转一次。

    per-key 锁：并发请求同一文件时只做一次转换（审查 A-M4：锁外转换会
    双重转换且相同 out_name 互踩）。缓存文件名含 mtime/size 指纹：文件
    变化后落新文件、不覆盖旧缓存（审查 A-M6：覆盖会让旧 key 悬挂指向
    新内容，且 Windows 下文件被占用时 unlink 抛错）。

    返回缓存目录内只读的转换产物路径；调用方不得修改该文件。
    """
    src = Path(src)
    st = src.stat()
    key = (str(src.resolve()), st.st_mtime_ns, st.st_size, target_suffix.lower())
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        lock = _KEY_LOCKS.setdefault(key, threading.Lock())
    if hit is not None and hit.exists():
        return hit

    with lock:  # per-key 串行：同文件并发只转一次
        with _CACHE_LOCK:  # double-check（等待期间可能已被并发请求转好）
            hit = _CACHE.get(key)
            if hit is not None and hit.exists():
                return hit
        # 指纹含 mtime+size：与 key 同源，不同版本源文件互不覆盖
        tag = hashlib.sha1(
            f"{key[0]}|{key[1]}|{key[2]}|{key[3]}".encode(
                "utf-8", errors="ignore"
            )
        ).hexdigest()[:12]
        out_name = f"{src.stem}-{tag}{target_suffix.lower()}"
        converted = convert_to(src, target_suffix, _cache_dir(), out_name)
        with _CACHE_LOCK:
            _CACHE[key] = converted
        return converted
