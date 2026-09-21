# -*- coding: utf-8 -*-
"""日志配置：stderr（开发可见）+ 文件落盘（桌面版无控制台，唯一可见渠道）。

落盘位置 ``~/.mask-tool/logs/mask-tool.log``（1MB × 3 滚动）。桌面版
（pythonw）无 stderr 可看，文件日志是排查"检测卡住/AI 无响应"的关键
渠道；LLM 全链路（请求耗时/批次结果/熔断/预算）均写 INFO 级。
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def log_dir() -> Path:
    """日志目录（调用方负责创建；`~/.mask-tool/logs`）。"""
    return Path.home() / ".mask-tool" / "logs"


def setup_logger(
    name: str = "mask_tool",
    level: int = logging.INFO,
    log_file: bool = True,
) -> logging.Logger:
    """配置日志器（幂等：已配置时直接返回）。

    Args:
        name: 日志器名称
        level: 日志级别
        log_file: 是否落盘到 ~/.mask-tool/logs/mask-tool.log
            （只读文件系统/权限异常时自动降级为仅 stderr）
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(level)
    logger.propagate = False

    formatter = logging.Formatter(fmt=_FORMAT, datefmt=_DATEFMT)

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    if log_file:
        try:
            log_dir().mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(
                log_dir() / "mask-tool.log",
                maxBytes=1024 * 1024, backupCount=3, encoding="utf-8",
            )
            fh.setLevel(level)
            fh.setFormatter(formatter)
            logger.addHandler(fh)
        except OSError as exc:
            logger.warning("日志文件不可用（仅 stderr 输出）: %s", exc)

    return logger
