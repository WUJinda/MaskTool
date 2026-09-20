# -*- coding: utf-8 -*-
"""verify_page.py — mask-tool 发布流水线页面级验证（build-release.ps1 步骤 3 调用）。

背景（2026-09-20）：v0.1.2 便携包曾出现 frozen 页面 ImportError，而健康检查
（/_stcore/health）只证明服务器存活——Streamlit 只在浏览器经 WebSocket 连接后才
执行页面脚本，且脚本异常只回传浏览器、不写服务端日志。本脚本用 headless Edge
（CDP 协议）真实加载页面，等待 React 渲染后断言 DOM 标记：

  exception（stException）必须为 0，sidebar 与设置弹窗组件根节点必须在位。

退出码：0=PASS；1=FAIL（页面异常或标记缺失）；2=TIMEOUT；3=SKIP（缺 Edge 或
websocket-client，调用方降级为警告即可）。
"""

import argparse
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

MARKERS = {
    # Streamlit 脚本执行异常（ImportError/NameError 等在此暴露）
    "exception": '[data-testid="stException"]',
    # 侧栏（Streamlit 页面骨架）
    "sidebar": '[data-testid="stSidebar"]',
    # 设置弹窗组件根（components/ JS 资产收集与 components.v2 链路）
    "settings": "#mt-settings-root",
}

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_edge():
    for p in EDGE_CANDIDATES:
        if PathExists(p):
            return p
    return None


def PathExists(p):  # noqa: N802 兼容 Windows/大小写差异场景的简单存在性检查
    from pathlib import Path

    return Path(p).exists()


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except OSError:
            time.sleep(0.2)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True, help="streamlit 服务端口")
    ap.add_argument("--timeout", type=int, default=45, help="页面渲染等待秒数")
    args = ap.parse_args()

    try:
        import websocket  # noqa: F401
    except ImportError:
        print("[verify_page] SKIP: venv 缺 websocket-client")
        return 3

    edge = find_edge()
    if not edge:
        print("[verify_page] SKIP: 未找到 msedge.exe（标准安装路径）")
        return 3

    cdp_port = free_port()
    user_data = tempfile.mkdtemp(prefix="mt-verify-cdp-")
    proc = subprocess.Popen(
        [
            edge, "--headless=new", "--disable-gpu", "--no-first-run",
            "--remote-allow-origins=*",
            f"--remote-debugging-port={cdp_port}",
            f"--user-data-dir={user_data}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_http(f"http://127.0.0.1:{cdp_port}/json/version"):
            print("[verify_page] FAIL: CDP 端口未就绪")
            return 1

        req = urllib.request.Request(
            f"http://127.0.0.1:{cdp_port}/json/new?http://127.0.0.1:{args.port}/",
            method="PUT",
        )
        target = json.load(urllib.request.urlopen(req, timeout=5))

        import websocket

        ws = websocket.create_connection(
            target["webSocketDebuggerUrl"], timeout=15)
        mid = 0

        def evaluate(expression):
            nonlocal mid
            mid += 1
            ws.send(json.dumps({
                "id": mid, "method": "Runtime.evaluate",
                "params": {"expression": expression, "returnByValue": True},
            }))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == mid:
                    return msg.get("result", {}).get("result", {}).get("value")

        expr = "JSON.stringify({" + ",".join(
            f"{k}: document.querySelectorAll('{v}').length"
            for k, v in MARKERS.items()) + "})"

        deadline = time.time() + args.timeout
        state = None
        while time.time() < deadline:
            raw = evaluate(expr)
            if raw:
                state = json.loads(raw)
                if state.get("exception"):
                    break  # 异常是终态信号，无需继续等骨架
                if state.get("sidebar") and state.get("settings"):
                    break
            time.sleep(1.0)

        if state is None:
            ws.close()
            print("[verify_page] TIMEOUT: 页面未在时限内渲染出骨架")
            return 2
        ok = (not state["exception"]) and state["sidebar"] and state["settings"]
        level = "PASS" if ok else "FAIL"
        print(f"[verify_page] {level}: {json.dumps(state, ensure_ascii=False)}")
        if not ok and state["exception"]:
            # 把页面上可见的异常首行带出来，便于排障
            detail = evaluate(
                "document.querySelector('[data-testid=\"stException\"]')"
                ".textContent.slice(0, 300)")
            print(f"[verify_page] 页面异常: {detail}")
        ws.close()
        return 0 if ok else 1
    finally:
        proc.kill()
        shutil.rmtree(user_data, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
