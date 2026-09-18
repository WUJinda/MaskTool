"""mask-tool 桌面入口：pywebview 原生窗口包装 Streamlit Web UI。

架构（零侵入 web/app.py）：
    desktop.py
      ├─ 找空闲端口，subprocess 启动 streamlit（仅本机监听 + headless + 关遥测）
      ├─ 轮询 /_stcore/health 直到就绪
      ├─ pywebview 创建原生窗口（WebView2/EdgeChromium 后端）加载该地址
      ├─ js_api.save_file：下载兜底通道（fetch → base64 → 原生保存对话框写盘）
      └─ 窗口关闭 → 结束 streamlit 进程树，退出

用法：
    mask-tool-desktop            # 控制台入口（pyproject 已注册）
    python -m mask_tool.desktop  # 等价
"""

from __future__ import annotations

import base64
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import webview

APP_TITLE = "mask-tool 文件脱敏工具"
WINDOW_SIZE = (1440, 900)
MIN_SIZE = (1100, 700)
STARTUP_TIMEOUT = 60  # 秒

_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
ICON_PATH = _ASSETS_DIR / "masktool.ico"


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


def _start_server(port: int) -> subprocess.Popen:
    """以子进程启动 streamlit（仅本机监听、headless、关遥测）。"""
    app_file = Path(__file__).resolve().parent / "web" / "app.py"
    cmd = [
        sys.executable, "-m", "streamlit", "run", str(app_file),
        "--server.port", str(port),
        "--server.address", "127.0.0.1",
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
    ]
    return subprocess.Popen(
        cmd,
        cwd=str(app_file.parents[2]),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def _terminate_tree(proc: subprocess.Popen) -> None:
    """结束 streamlit 进程树（Windows 用 taskkill /T）。"""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=10,
            )
        else:
            proc.terminate()
            proc.wait(timeout=10)
    except Exception:
        proc.kill()


class _DesktopApi:
    """暴露给页面 JS 的原生能力（下载兜底通道）。"""

    def __init__(self, window_ref: list):
        # 延迟持有 window 引用（webview 启动后才存在）
        self._window_ref = window_ref

    def save_file(self, filename: str, data_b64: str) -> bool:
        """接收 base64 文件内容，弹原生保存对话框并写盘。

        返回 True=已保存；False=用户取消。
        """
        import webview as _wv

        window = self._window_ref[0] if self._window_ref else None
        if window is None:
            return False
        target = window.create_file_dialog(
            _wv.SAVE_DIALOG, save_filename=filename or "download.bin",
        )
        if not target:
            return False
        path = Path(target if isinstance(target, str) else target[0])
        path.write_bytes(base64.b64decode(data_b64))
        return True


# 注入页面的下载拦截脚本：
# st.download_button 生成 <a download href=...>；拦截点击 → fetch → base64 →
# 调用 pywebview.api.save_file 弹原生保存对话框。若 WebView2 原生下载可用，
# 原生行为优先生效（本脚本只兜底，不阻断正常导航之外的流程）。
_DOWNLOAD_SHIM_JS = """
(function () {
  if (window.__mtDownloadShimInstalled) return;
  window.__mtDownloadShimInstalled = true;
  document.addEventListener('click', function (ev) {
    var a = ev.target && ev.target.closest ? ev.target.closest('a[download]') : null;
    if (!a) return;
    var href = a.href || '';
    if (!href || href.startsWith('blob:') === false && href.startsWith('/') === false
        && href.startsWith(location.origin) === false) {
      return; // 非本站/非 blob 链接不处理
    }
    ev.preventDefault();
    ev.stopPropagation();
    var name = a.getAttribute('download') || 'download.bin';
    fetch(href)
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.arrayBuffer(); })
      .then(function (buf) {
        var bytes = new Uint8Array(buf);
        var bin = '';
        var CHUNK = 0x8000;
        for (var i = 0; i < bytes.length; i += CHUNK) {
          bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
        }
        return btoa(bin);
      })
      .then(function (b64) { return window.pywebview && pywebview.api.save_file(name, b64); })
      .catch(function (e) { console.error('mask-tool 下载兜底失败:', e); });
  }, true);
})();
"""


def main() -> None:
    port = _free_port()
    proc = _start_server(port)
    window_ref: list = []

    try:
        _wait_ready(port)
    except Exception:
        _terminate_tree(proc)
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

    def _on_loaded():
        # 页面每次（重新）加载后注入下载兜底脚本
        try:
            window.evaluate_js(_DOWNLOAD_SHIM_JS)
        except Exception:
            pass

    window.events.loaded += _on_loaded
    window.events.closed += lambda: _terminate_tree(proc)

    try:
        webview.start(icon=str(ICON_PATH) if ICON_PATH.exists() else None)
    finally:
        _terminate_tree(proc)


if __name__ == "__main__":
    main()
