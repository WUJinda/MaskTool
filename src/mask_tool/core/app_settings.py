"""桌面应用用户设置持久化（保存目标文件夹等）。

背景（2026-09-20）：软件转型桌面应用后，浏览器式"下载"在 pywebview
窗口内不可用，交付改为"直接保存到用户配置的目标文件夹"。保存位置
属于应用级偏好，存储位置与词库共用同一锚点体系：

- 读取：CWD -> exe 同级 -> 源码树项目根，第一个存在的
  ``config/app_settings.yaml``；
- 写入：``config_loader.writable_anchor_dir()``（frozen=exe 同级，
  开发=源码树根），目录不存在时自动创建。

字段（YAML）::

    save_dir: "D:\\脱敏输出"   # 保存目标文件夹；未设置时用默认值

默认保存位置（2026-09-20 需求）：安装目录下的 ``output`` 目录
（frozen=exe 同级 ``<安装目录>/output``；开发=源码树项目根 ``output``），
即 ``config_loader.writable_anchor_dir() / "output"``；目录在首次保存时
自动创建。
"""

from pathlib import Path
from typing import Dict, Optional

import yaml

from mask_tool.core.config_loader import find_data_file, writable_anchor_dir

SETTINGS_RELPATH = "config/app_settings.yaml"
OUTPUT_DIR_NAME = "output"


def _settings_path() -> Path:
    """定位设置文件：已存在者优先；全新环境落可写锚点。"""
    found = find_data_file(SETTINGS_RELPATH)
    if found is not None:
        return found
    return writable_anchor_dir() / SETTINGS_RELPATH


def load_settings() -> Dict[str, object]:
    """读取全部设置；文件缺失/损坏返回空 dict（不抛异常，UI 可用默认值）。"""
    p = _settings_path()
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(updates: Dict[str, object]) -> bool:
    """合并写入设置；失败（目录只读等）返回 False，由调用方提示。"""
    try:
        p = _settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        current = load_settings()
        current.update(updates)
        p.write_text(
            yaml.safe_dump(current, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return True
    except OSError:
        return False


def get_explicit_save_dir() -> str:
    """用户显式设置的保存文件夹（原样字符串）；未设置返回空串。"""
    raw = load_settings().get("save_dir")
    return str(raw).strip() if raw else ""


def get_save_dir() -> str:
    """实际生效的保存目标文件夹。

    优先用户显式设置；未设置时回退默认值（安装目录下 output）。
    不做存在性校验：保存时自动创建（mkdir parents），手动输入的
    笔误由保存动作给出明确错误。
    """
    return get_explicit_save_dir() or default_save_dir()


def set_save_dir(path: str) -> bool:
    """设置保存目标文件夹；空串视为恢复默认。"""
    return save_settings({"save_dir": str(path).strip()})


# ── 主题设置（2026-09-20 设置弹窗）：auto=跟随系统（默认），light/dark 手动 ──

VALID_THEMES = ("auto", "light", "dark")


def get_theme() -> str:
    """主题偏好；未设置或非法值回退 auto（跟随系统）。"""
    raw = load_settings().get("theme")
    return raw if raw in VALID_THEMES else "auto"


def set_theme(theme: str) -> bool:
    """写入主题偏好（auto/light/dark）。"""
    return save_settings({"theme": theme if theme in VALID_THEMES else "auto"})


def default_save_dir() -> str:
    """默认保存位置：安装目录（frozen=exe 同级 / 开发=源码树根）下的 output。"""
    return str(writable_anchor_dir() / OUTPUT_DIR_NAME)
