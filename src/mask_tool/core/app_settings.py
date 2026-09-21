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


def get_llm_settings() -> Dict[str, object]:
    """读取应用级 LLM 配置段（设置弹窗「模型配置」维护）。

    返回 llm 段 dict（可能为空 dict = 未配置）；字段与
    models.config.LLMConfig 同名（base_url/model/api_key/role 等）。
    合并优先级见 config_loader.load_config：app_settings.llm >
    default.yaml.llm > 代码默认。
    """
    data = load_settings().get("llm")
    return dict(data) if isinstance(data, dict) else {}


def set_llm_settings(updates: Dict[str, object]) -> bool:
    """整体写入 LLM 配置段（总是写完整段，空值表示清除该字段）。"""
    return save_settings({"llm": dict(updates)})


def llm_config_sig(base_url: str, model: str, api_key: str) -> str:
    """模型端点配置指纹：base_url/model/api_key 三元组 hash。

    作为连通状态（last_test）的匹配键：配置未变时测试结果跨重启有效。
    """
    import hashlib
    raw = f"{(base_url or '').strip()}|{(model or '').strip()}|{api_key or ''}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def get_llm_test_state() -> Dict[str, object]:
    """读取持久化的模型连通状态 llm.last_test（{ok,msg,at,sig}；无则空 dict）。"""
    lt = get_llm_settings().get("last_test")
    return dict(lt) if isinstance(lt, dict) else {}


# ── 界面偏好（2026-09-21）：运行设置持久化，重启软件后保持上次选择 ──

# 键名与对应 widget 的 session_state key 一致，便于 on_change 回调直接写回。
# 临时自定义敏感词不入此段（产品语义：仅本次任务生效）。
UI_PREF_DEFAULTS: Dict[str, object] = {
    "run_mode": "smart",        # 侧栏运行模式（focused/smart/strict/aggressive）
    "irreversible": False,      # 不可逆脱敏
    "learn_words": True,        # 学习新词到词库
    "llm_enabled": False,       # AI 增强检测开关（端点未就绪时运行时忽略）
    "mask_filenames": True,     # 同时脱敏文件名
    "manual_only_mode": False,  # 仅脱敏我指定的词
}


def get_ui_prefs() -> Dict[str, object]:
    """读取界面偏好段 ui_prefs（与默认值融合，只认白名单键）。"""
    raw = load_settings().get("ui_prefs")
    prefs = dict(UI_PREF_DEFAULTS)
    if isinstance(raw, dict):
        prefs.update(
            {k: v for k, v in raw.items() if k in UI_PREF_DEFAULTS}
        )
    return prefs


def set_ui_prefs(updates: Dict[str, object]) -> bool:
    """合并写入界面偏好段；失败返回 False（静默降级为会话级，不影响运行）。"""
    cur = load_settings().get("ui_prefs")
    cur = dict(cur) if isinstance(cur, dict) else {}
    cur.update(updates)
    return save_settings({"ui_prefs": cur})


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
