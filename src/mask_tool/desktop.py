"""mask-tool 桌面入口：pywebview 原生窗口包装 Streamlit Web UI。

架构（零侵入 web/app.py）：
    desktop.py
      ├─ 单实例守卫：锁文件 + PID/创建时间校验；已有实例则唤起其窗口
      │  后退出；残留孤儿进程经确认后清理（R8，2026-09-21）
      ├─ 找空闲端口，subprocess 启动 streamlit（仅本机监听 + headless + 关遥测）
      ├─ 轮询 /_stcore/health 直到就绪
      ├─ pywebview 创建原生窗口（WebView2/EdgeChromium 后端）加载该地址
      ├─ js_api.pick_save_folder：原生目录选择（保存位置设置，2026-09-20）
      └─ 窗口关闭 → 结束 streamlit 进程树并释放锁，退出

用法：
    mask-tool app            # CLI 子命令（推荐记忆点：一个可执行文件搞定全部）
    mask-tool-desktop        # 控制台入口（pyproject 已注册）
    python -m mask_tool.desktop  # 等价
    start-windows.bat / start-mac.command  # 双击启动（桌面软件主入口）

注：独立浏览器/Web 入口（mask-tool-web）已下线，UI 仅在桌面窗口内渲染。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

import webview

APP_TITLE = "mask-tool 文件脱敏工具"
WINDOW_SIZE = (1440, 900)
MIN_SIZE = (1100, 700)
STARTUP_TIMEOUT = 60  # 秒

# 单实例锁文件名（位于 %LOCALAPPDATA%/mask-tool/，与日志同目录；
# 非 Windows 退回 ~/.mask-tool/）
LOCK_FILENAME = "instance.lock"

# 品牌色（与 assets/icon/masktool-icon.svg 一致）
# 标题栏底色 = 图标底色深蓝紫；边框/强调色 = 遮蔽条品牌紫；标题文字用白
_CAPTION_BG = "#1E2440"
_BORDER_ACCENT = "#5B6EE8"
_CAPTION_FG = "#FFFFFF"

# 图标文件由 _resolve_icon_path() 按运行模式定位（开发/打包两种布局）


def _free_port() -> int:
    """随机挑选一个可用端口（避开常用 8501）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(port: int, timeout: float = STARTUP_TIMEOUT) -> None:
    """轮询 streamlit 健康检查端点，超时抛异常。"""
    url = f"http://127.0.0.1:{port}/_stcore/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.3)
    raise RuntimeError(f"Streamlit 未在 {timeout}s 内就绪（端口 {port}）")


def _resolve_app_file() -> Path:
    """定位 web/app.py：开发模式在源码树，frozen 在 PyInstaller 数据目录。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", "") or Path(sys.executable).parent)
        return base / "mask_tool" / "web" / "app.py"
    return Path(__file__).resolve().parent / "web" / "app.py"


def _start_server(port: int) -> subprocess.Popen:
    """以子进程启动 streamlit（仅本机监听、headless、关遥测）。

    - 开发模式：[python, -m, streamlit, run, app.py, ...]
    - PyInstaller frozen：exe 不支持 -m，改为 [exe, --mt-streamlit-server, ...]
      重启自身，子进程在 _streamlit_child_main() 中进程内执行 streamlit CLI。
    """
    app_file = _resolve_app_file()
    common = [
        "--server.port", str(port),
        "--server.address", "127.0.0.1",
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
        # PyInstaller 环境下 streamlit 会误判为开发模式并拒绝 server.port，
        # 显式关闭（正常安装下与默认值一致，无副作用）
        "--global.developmentMode", "false",
    ]
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--mt-streamlit-server", *common]
        cwd = str(Path(sys.executable).parent)
    else:
        cmd = [sys.executable, "-m", "streamlit", "run", str(app_file), *common]
        cwd = str(app_file.parents[2])

    stderr_f: object = subprocess.DEVNULL
    if getattr(sys, "frozen", False):
        # frozen 无控制台：streamlit 子进程的 stderr 落日志便于排障（审查 P2-9）
        try:
            log_dir = Path(
                os.environ.get("LOCALAPPDATA", str(Path.home()))
            ) / "mask-tool"
            log_dir.mkdir(parents=True, exist_ok=True)
            stderr_f = open(log_dir / "streamlit.log", "ab")
        except Exception:
            stderr_f = subprocess.DEVNULL
    return subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=stderr_f,
        # 隐藏 Streamlit 自带的 Deploy 按钮（云端部署推广，与本地工具无关）
        env={**os.environ, "STREAMLIT_CLIENT_TOOLBAR_MODE": "minimal"},
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def _streamlit_child_main() -> None:
    """PyInstaller frozen 子进程模式：作为 streamlit server 运行。

    父进程以 [exe, --mt-streamlit-server, --server.port, ...] 启动本进程；
    这里把剩余参数转交 streamlit CLI 在进程内执行（等效 -m streamlit run）。
    """
    from streamlit.web import cli as stcli

    app_file = _resolve_app_file()
    sys.argv = ["streamlit", "run", str(app_file), *sys.argv[2:]]
    try:
        stcli.main()
    except SystemExit:
        raise
    except BaseException:
        # windowed exe 无 stderr：崩溃原因落日志，再原样退出
        import traceback
        try:
            log_dir = Path(
                os.environ.get("LOCALAPPDATA", str(Path.home()))
            ) / "mask-tool"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "streamlit-child-error.log").write_text(
                traceback.format_exc(), encoding="utf-8"
            )
        except Exception:
            pass
        raise


# 进程树终止的一次性守卫：closed 事件回调与 main() finally 双路径都会
# 调用 _terminate_tree，关窗竞态下几乎同时触发；无守卫时会重复执行
# taskkill（表现为关软件时 cmd 黑框闪烁两次）。
# 桌面应用每次运行只管理一个 streamlit 子进程，模块级守卫即可。
_tree_terminated = False
_tree_lock = threading.Lock()


def _terminate_tree(proc: subprocess.Popen) -> None:
    """结束 streamlit 进程树（Windows 用 taskkill /T，仅执行一次）。

    - 一次性守卫（锁+标志）：closed 事件与 finally 竞态双调用只跑一次；
    - taskkill 以 CREATE_NO_WINDOW 运行：taskkill 是控制台程序，
      无控制台的 GUI 父进程（pythonw / windowed exe）下若不加速标志，
      Windows 会为它新建可见 cmd 窗口（关软件时黑框闪烁的根因）。
    """
    global _tree_terminated
    with _tree_lock:
        if _tree_terminated:
            return
        _tree_terminated = True

    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=10,
                # 静默化：不为控制台子进程新建可见窗口（修复关软件时
                # cmd 黑框闪烁）；stdin 一并重定向，彻底避免控制台创建
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdin=subprocess.DEVNULL,
            )
        else:
            proc.terminate()
            proc.wait(timeout=10)
    except Exception:
        proc.kill()


# ──────────────────────────────────────────────
# 单实例守卫（R8）：锁文件 + PID 探活，防多实例堆积
#
# 背景（2026-09-21）：机器上残留 35 组共 71 个 mask-tool 进程（历次
# 启动/测试未清理），旧实例的模块缓存与磁盘新代码错位导致运行期
# ImportError。守卫让重复启动收敛到单实例，并对孤儿进程给出确认清理。
#
# 设计约束：守卫任何一步失败都必须放行启动（try/except 兑底），
# 绝不能因为守卫自身问题阻断应用。
# ──────────────────────────────────────────────


def _lock_path() -> Path:
    """锁文件路径（用户级，每用户互不影响）。"""
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "mask-tool"
    return base / LOCK_FILENAME


def _pid_start_ft(pid: int) -> Optional[int]:
    """进程创建时间（Windows FILETIME，100ns ticks）；用于防 PID 复用误判。

    非 Windows 或获取失败返回 None（调用方退化为仅探活）。
    """
    if os.name != "nt" or pid <= 0:
        return None
    try:
        import ctypes

        class _FILETIME(ctypes.Structure):
            _fields_ = [
                ("lo", ctypes.c_uint32),
                ("hi", ctypes.c_uint32),
            ]

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            creation = _FILETIME()
            exit_, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
            ok = kernel32.GetProcessTimes(
                h, ctypes.byref(creation), ctypes.byref(exit_),
                ctypes.byref(kernel), ctypes.byref(user),
            )
            if not ok:
                return None
            return (creation.hi << 32) | creation.lo
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return None


def _pid_alive(pid: int, start_ft: Optional[int]) -> bool:
    """PID 存活且创建时间与锁记录一致（防 PID 被无关进程复用误判）。

    Windows：OpenProcess + GetProcessTimes 精确校验；
    非 Windows：os.kill(pid, 0) 仅探活（无创建时间可比，接受低概率误差）。
    """
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        actual = _pid_start_ft(pid)
        if actual is None:
            return False
        return start_ft is None or actual == start_ft
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程存在但属他人（仅探活场景下视为存活）
    except OSError:
        return False


def _read_lock() -> dict:
    """读锁内容；无锁/损坏返回 {}。"""
    try:
        lp = _lock_path()
        if lp.exists():
            data = json.loads(lp.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _write_instance_lock(streamlit_pid: int, port: int) -> None:
    """以原子替换写入本实例的锁（desktop 宿主 + streamlit 子进程）。"""
    try:
        lp = _lock_path()
        lp.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "desktop_pid": os.getpid(),
            "desktop_start_ft": _pid_start_ft(os.getpid()),
            "streamlit_pid": streamlit_pid,
            "streamlit_start_ft": _pid_start_ft(streamlit_pid),
            "port": port,
            "created": datetime.now().isoformat(timespec="seconds"),
        }
        tmp = lp.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, lp)
    except Exception:
        pass  # 锁写入失败不阻断启动（守卫退化为无锁模式）


def _release_instance_lock() -> None:
    """退出时释放锁；仅当锁仍属本进程时删除（防误删新接管实例的锁）。"""
    try:
        lp = _lock_path()
        if not lp.exists():
            return
        if _read_lock().get("desktop_pid") == os.getpid():
            lp.unlink(missing_ok=True)
    except Exception:
        pass


def _focus_existing_window() -> bool:
    """把已有实例的主窗口带到前台（按全局唯一标题定位）。

    返回是否成功前置；非 Windows/未找到窗口返回 False。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, APP_TITLE)
        if not hwnd:
            return False
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE（最小化时先还原）
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def _find_orphan_servers() -> list:
    """扫描不属于本进程的 mask-tool streamlit 服务进程（孤儿候选）。

    匹配特征（覆盖开发/打包两种形态）：
      - python/pythonw 命令行含 streamlit run 且指向 mask_tool/web/app.py；
      - frozen 形态：mask-tool.exe 且带 --mt-streamlit-server。
    desktop 宿主（-m mask_tool.desktop）不匹配，不会被误伤；
    本进程自身不在扫描结果内（扫描发生在 _start_server 之前，子进程尚不存在）。
    非 Windows 不扫描（返回 []）。
    """
    if os.name != "nt":
        return []
    try:
        ps_script = (
            "Get-CimInstance Win32_Process | Where-Object { "
            "$_.Name -notlike 'powershell*' -and $_.Name -notlike 'pwsh*' -and ("
            "($_.CommandLine -like '*streamlit run*mask_tool*web*app.py*') -or "
            "($_.Name -eq 'mask-tool.exe' -and "
            " $_.CommandLine -like '*--mt-streamlit-server*')) } | "
            "Select-Object -ExpandProperty ProcessId"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL,
        )
        pids = [
            int(tok) for tok in out.stdout.split()
            if tok.strip().isdigit() and int(tok) != os.getpid()
        ]
        return sorted(set(pids))
    except Exception:
        return []


def _kill_tree(pid: int) -> None:
    """结束指定进程及其子树（与 _terminate_tree 同风格，无一次性守卫）。"""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdin=subprocess.DEVNULL,
            )
        else:
            os.kill(pid, 9)
    except Exception:
        pass


def _confirm_yesno(text: str) -> bool:
    """原生 YES/NO 确认框；非 Windows 或失败时保守返回 False（取消）。"""
    if os.name == "nt":
        try:
            import ctypes

            # MB_YESNO(0x4) | MB_ICONQUESTION(0x20) | MB_TOPMOST(0x40000)
            ret = ctypes.windll.user32.MessageBoxW(
                0, text, "mask-tool", 0x4 | 0x20 | 0x40000
            )
            return ret == 6  # IDYES
        except Exception:
            return False
    try:
        sys.stderr.write(f"[mask-tool] {text} （非交互环境，保守取消）\n")
    except Exception:
        pass
    return False


def _wait_and_focus(
    d_pid: int, d_start_ft, timeout_s: float = 20.0, poll_s: float = 1.0
) -> bool:
    """等待已有实例的主窗口出现并前置。

    实例可能仍在启动中（锁已写入、pywebview 窗口尚未创建），
    轮询期间宿主进程退出（启动失败）则提前返回 False 交由上层接管。
    """
    deadline = time.monotonic() + timeout_s
    while True:
        if _focus_existing_window():
            return True
        if d_pid and not _pid_alive(d_pid, d_start_ft):
            return False  # 宿主已退出，交由上层走陈旧锁接管
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_s)


def _notify_already_running() -> None:
    """信息提示：应用已在运行或正在启动（不提供清理选项）。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        # MB_OK(0) | MB_ICONINFORMATION(0x40) | MB_TOPMOST(0x40000)
        ctypes.windll.user32.MessageBoxW(
            0,
            "mask-tool 已在运行或正在启动中。\n\n"
            "如果长时间未出现窗口，可在任务管理器中结束"
            " mask-tool 相关进程后重试。",
            "mask-tool",
            0x40 | 0x40000,
        )
    except Exception:
        pass


def _guard_single_instance() -> bool:
    """启动守卫：True=继续启动，False=本次退出。

    分支（R10 修正语义：活实例绝不进清理流）：
    1. 锁内 desktop 宿主存活 → 已有实例在用/启动中：等待其窗口出现
       并前置（等待期间宿主退出则转分支 2 接管）；等不到窗口则信息
       提示后退出——绝不清理仍属活实例的任何进程；
    2. 宿主已死（锁陈旧）：锁内存活进程 + 扫描到的孤儿服务均为真
       残留，用户确认后清理再启动，取消则退出；
    3. 无锁且无孤儿 → 直接启动。
    """
    try:
        data = _read_lock()
        d_pid = int(data.get("desktop_pid") or 0)
        s_pid = int(data.get("streamlit_pid") or 0)
        d_alive = _pid_alive(d_pid, data.get("desktop_start_ft"))
        s_alive = _pid_alive(s_pid, data.get("streamlit_start_ft"))

        if d_alive:
            # 已有实例在用（或仍在启动中）：等待其窗口出现并前置。
            # 此处不扫描、不清理——所有存活进程都属于该实例
            if _wait_and_focus(d_pid, data.get("desktop_start_ft")):
                return False
            if _pid_alive(d_pid, data.get("desktop_start_ft")):
                # 宿主活着但窗口始终未出现（启动极慢/异常）：提示后退出
                _notify_already_running()
                return False
            # 等待期间宿主退出 → 落到下方陈旧锁接管分支

        # 宿主已死：锁内存活进程 + 扫描到的残留服务，均为真孤儿
        known = [p for p, alive in ((d_pid, d_alive), (s_pid, s_alive)) if alive]
        targets = list(dict.fromkeys(known + _find_orphan_servers()))
        if targets:
            ok = _confirm_yesno(
                f"检测到上次异常退出留下的 {len(targets)} 个残留服务进程。\n\n"
                "[是] 结束这些进程并启动应用\n"
                "[否] 保留这些进程，本次不启动"
            )
            if not ok:
                return False
            for pid in targets:
                _kill_tree(pid)
        return True
    except Exception:
        return True  # 守卫自身异常不阻断启动


class _DesktopApi:
    """暴露给页面 JS 的原生能力（保存位置目录选择）。"""

    def __init__(self, window_ref: list):
        # 延迟持有 window 引用（webview 启动后才存在）
        self._window_ref = window_ref

    def pick_save_folder(self) -> "Optional[str]":
        """弹出原生“选择文件夹”对话框，结果直接写入应用设置。

        Web 端（streamlit 子进程）无法弹原生对话框，此接口借持有窗口
        的本进程完成；选择结果由 core.app_settings 落盘（与 Web 端
        读写同一文件），返回选中绝对路径供 iframe 即时回显。
        返回 None=用户取消或窗口未就绪。
        """
        import webview as _wv

        from mask_tool.core.app_settings import set_save_dir

        window = self._window_ref[0] if self._window_ref else None
        if window is None:
            return None
        try:
            target = window.create_file_dialog(_wv.FOLDER_DIALOG)
        except Exception:
            return None
        if not target:
            return None
        path = target if isinstance(target, str) else target[0]
        set_save_dir(str(path))
        return str(path)
def _resolve_icon_path() -> Path:
    """定位 masktool.ico：开发模式在项目根 assets/，frozen 在 _MEIPASS/assets/
    （spec 将其收集到 _internal/assets/），逐候选探测。"""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            candidates.append(Path(meipass) / "assets" / "masktool.ico")
        candidates.append(Path(sys.executable).parent / "assets" / "masktool.ico")
    else:
        candidates.append(Path(__file__).resolve().parents[2] / "assets" / "masktool.ico")
    for c in candidates:
        if c.exists():
            return c
    return candidates[-1]


def _set_app_user_model_id() -> None:
    """显式 AppUserModelID：统一任务栏分组/跳转列表标识。

    不设置时 Windows 按 exe 默认分组：开发模式（pythonw）与打包模式
    （mask-tool.exe）行为不一致；显式同一 ID 后两种模式在任务栏/
    固定到任务栏时表现为同一个应用。
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("masktool.desktop")
    except Exception:
        pass


def _apply_titlebar_theme() -> None:
    """Windows 11+：用 DWM API 把标题栏染成品牌色。

    - DWMWA_BORDER_COLOR(34) / DWMWA_CAPTION_COLOR(35) /
      DWMWA_TEXT_COLOR(36) 仅 Windows 11 (build 22000+) 支持；Win10 及更早
      调用失败，静默保持系统默认标题栏（降级安全）。
    - 通过窗口标题定位 HWND（pywebview 未公开原生句柄）。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = ctypes.windll.user32.FindWindowW(None, APP_TITLE)
        if not hwnd:
            return

        def _set_attr(attr: int, rgb_hex: str) -> None:
            r = int(rgb_hex[1:3], 16)
            g = int(rgb_hex[3:5], 16)
            b = int(rgb_hex[5:7], 16)
            color = wintypes.COLORREF((b << 16) | (g << 8) | r)  # 0x00BBGGRR
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                wintypes.HWND(hwnd), wintypes.DWORD(attr),
                ctypes.byref(color), ctypes.sizeof(color),
            )

        _set_attr(34, _BORDER_ACCENT)   # 窗口边框 DWMWA_BORDER_COLOR
        _set_attr(35, _CAPTION_BG)      # 标题栏底色 DWMWA_CAPTION_COLOR
        _set_attr(36, _CAPTION_FG)      # 标题文字 DWMWA_TEXT_COLOR
    except Exception:
        pass  # 不支持/失败时保持系统默认标题栏，不影响功能


def main() -> None:
    _set_app_user_model_id()
    if not _guard_single_instance():
        return
    port = _free_port()
    proc = _start_server(port)
    _write_instance_lock(proc.pid, port)
    window_ref: list = []

    try:
        _wait_ready(port)
    except Exception:
        _terminate_tree(proc)
        _release_instance_lock()
        raise

    url = f"http://127.0.0.1:{port}"
    window = webview.create_window(
        APP_TITLE,
        url,
        width=WINDOW_SIZE[0],
        height=WINDOW_SIZE[1],
        min_size=MIN_SIZE,
        js_api=_DesktopApi(window_ref),
        text_select=True,
    )
    window_ref.append(window)
    window.events.closed += lambda: _terminate_tree(proc)

    def _on_shown() -> None:
        _apply_titlebar_theme()

    window.events.shown += _on_shown

    icon_file = _resolve_icon_path()
    try:
        webview.start(icon=str(icon_file) if icon_file.exists() else None)
    finally:
        _terminate_tree(proc)


def _fatal_dialog(exc: BaseException) -> None:
    """无控制台场景（pythonw/双击启动）下的启动失败呈现：原生弹窗 + 日志文件。

    有控制台时 stderr 仍可用，但统一走这里也不损失信息。
    """
    import ctypes
    import traceback
    from datetime import datetime

    log_path: Path | None = None
    try:
        log_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "mask-tool"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "desktop-error.log"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now().isoformat(timespec='seconds')}]\n")
            f.write(traceback.format_exc())
            f.write("\n")
    except Exception:
        log_path = None  # 日志写不进去不阻断弹窗

    lines = [
        "mask-tool 启动失败。",
        "",
        f"错误：{exc.__class__.__name__}: {exc}",
        "",
        "常见原因：",
        "  1. 依赖未安装：pip install -e \".[app]\"",
        "  2. WebView2 运行时缺失：",
        "     https://developer.microsoft.com/microsoft-edge/webview2/",
        "  3. 首次使用请先运行 install-windows.bat",
    ]
    if log_path is not None:
        lines += ["", f"详细日志：{log_path}"]
    text = "\n".join(lines)

    if os.name == "nt":
        try:
            # MB_ICONERROR(0x10) | MB_OK(0x0) | MB_TOPMOST(0x40000)
            ctypes.windll.user32.MessageBoxW(0, text, "mask-tool 启动错误", 0x10 | 0x40000)
            return
        except Exception:
            pass
    # 非 Windows / 弹窗失败：退回 stderr
    try:
        sys.stderr.write(text + "\n" + traceback.format_exc() + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    # PyInstaller frozen 下的 multiprocessing 子进程支持（streamlit 内部依赖）
    import multiprocessing

    multiprocessing.freeze_support()

    if len(sys.argv) > 1 and sys.argv[1] == "--mt-streamlit-server":
        # PyInstaller 子进程模式（由 _start_server 启动）：直接作为 streamlit
        # server 运行，不走桌面窗口逻辑
        _streamlit_child_main()
    else:
        try:
            main()
        except BaseException as exc:  # 弹窗后仍以非零码退出
            _fatal_dialog(exc)
            raise SystemExit(1)
