# -*- coding: utf-8 -*-
"""端点信任判定：区分本机/内网端点与外部端点。

背景：AI 增强会把候选实体前后各 ~50 字上下文（**未脱敏原文**）发送到
配置的 OpenAI 兼容端点。若该端点在外部网络，这一步本身即构成隐私外流，
与工具“数据不出本机”的核心承诺冲突。本模块提供**纯函数**判定，供
侧栏隐私门禁、设置弹窗提示与 pipeline 日志复用；不发起任何网络请求。

判定规则：
- localhost / 127.0.0.0-8 / ::1 ........................ 本机
- RFC1918（10/8、172.16/12、192.168/16）/ 链路本地 ...... 内网
- ``*.local`` / ``*.internal`` / ``*.lan`` ............... 内网域名后缀
- 其它域名（如 open.bigmodel.cn）/ 公网 IP ............... 外部
"""

import ipaddress
from urllib.parse import urlparse

# 内网域名后缀（mDNS / 常见企业内网）
_PRIVATE_HOST_SUFFIXES = (".local", ".internal", ".lan", ".localdomain")


def endpoint_host(base_url: str) -> str:
    """从 base_url 提取主机名（小写）；无 host 时返回空串。"""
    s = (base_url or "").strip()
    if not s:
        return ""
    if "://" not in s:  # 容忍 "192.168.1.10:8000/v1" 这类无 scheme 输入
        s = "http://" + s
    try:
        return (urlparse(s).hostname or "").lower()
    except ValueError:
        return ""


def is_loopback_or_private(base_url: str) -> bool:
    """端点是否为本机/内网地址。

    无法解析出 host 时返回 ``False``（宁可多提示一次，也不放过外部端点）。
    """
    host = endpoint_host(base_url)
    if not host:
        return False
    if host == "localhost" or host.endswith(_PRIVATE_HOST_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # 普通域名（非内网后缀）按外部处理
    return bool(
        ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified
    )
