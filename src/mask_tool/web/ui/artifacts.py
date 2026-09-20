# -*- coding: utf-8 -*-
"""批次产物落盘：唯一目标路径、产物写出、资源管理器打开、保存面板。"""
import json
import os
import sys
from pathlib import Path
from typing import List, Tuple

import streamlit as st

# ──────────────────────────────────────────────
# 保存到目标文件夹（桌面交付方式）
# ──────────────────────────────────────────────

def _unique_target(directory: Path, name: str) -> Path:
    """同名冲突时追加 -1/-2 序号，避免覆盖既有文件。"""
    target = directory / name
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for i in range(1, 1000):
        candidate = directory / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate
    return target  # 极端情况（999 个同名）退回原名，写入层报错提示


def _write_artifacts(directory: Path, artifacts: List[Tuple[str, bytes]]) -> Tuple[bool, object]:
    """把 (文件名, 字节) 列表写入目标文件夹（自动建目录、重名加序号）。

    返回 (True, [已写入路径列表]) 或 (False, 错误信息)。
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        saved: List[Path] = []
        for name, blob in artifacts:
            target = _unique_target(directory, name)
            target.write_bytes(blob)
            saved.append(target)
        return True, saved
    except OSError as exc:
        return False, str(exc)


def _open_in_explorer(path: str) -> bool:
    """在系统文件管理器中打开文件夹（Windows/macOS/Linux）。"""
    import subprocess as _sp

    try:
        p = Path(path)
        if not p.is_dir():
            return False
        if os.name == "nt":
            os.startfile(str(p))  # Windows 资源管理器
        elif sys.platform == "darwin":
            _sp.run(["open", str(p)], check=False)
        else:
            _sp.run(["xdg-open", str(p)], check=False)
        return True
    except OSError:
        return False


# 原生目录选择按钮（iframe）：调用 pywebview js_api 弹原生对话框，
# 选择结果由 desktop 端直接写入应用设置；本组件内即时回显。浏览器
# 开发模式（无 pywebview）时提示改用手动输入。
def _mask_result_artifacts(result: dict) -> List[Tuple[str, bytes]]:
    """把 mask_result 组装为可落盘的 (文件名, 字节) 列表。

    产物规则（I6 问题1）：单文件 → 脱敏后同名文件；
    zip/多文件 → 同结构 zip；mapping.json 始终单独落一份（还原钥匙）。
    """
    if result.get("download_kind") == "file":
        items = [(
            result.get("file_name") or "masked.docx",
            result.get("file_bytes") or b"",
        )]
    else:
        items = [(
            f"{result['batch_id']}_masked.zip",
            result.get("zip_buffer") or b"",
        )]
    mapping_data = result.get("mapping_data")
    if mapping_data:
        items.append((
            f"{result['batch_id']}_mapping.json",
            mapping_data.encode("utf-8"),
        ))
    return items


def _render_save_to_dir_panel(
    artifacts: List[Tuple[str, bytes]],
    panel_key: str,
    not_set_what: str,
) -> bool:
    """结果页“保存到目标文件夹”面板（桌面交付主入口）。

    - 未设置保存位置 → 警告并指引侧边栏设置，返回 False；
    - 点击保存 → 全部 artifacts 落盘（自动建目录、重名加序号），
      成功后列出完整路径；
    - “📂 打开保存文件夹”随时可打开目标目录。
    返回本次调用是否完成了保存动作。
    """
    from mask_tool.core.app_settings import get_save_dir

    save_dir = get_save_dir()
    if not save_dir:
        st.warning(
            f"⚠️ 尚未设置保存位置：请在左侧【保存位置】中选择目标文件夹，"
            f"再回来保存{not_set_what}。"
        )
        return False

    if st.button(
        f"💾 保存{not_set_what}到：{save_dir}",
        type="primary", width="stretch", key=f"save_to_dir_{panel_key}",
    ):
        ok, payload = _write_artifacts(Path(save_dir), artifacts)
        if ok:
            names = "\n\n".join(f"`{p}`" for p in payload)
            st.success(f"✅ 已保存 {len(payload)} 个文件：\n\n{names}")
            st.session_state[f"last_saved_dir_{panel_key}"] = str(Path(save_dir))
        else:
            st.error(
                f"❌ 保存失败（{save_dir}）：{payload}。"
                f"请在左侧【保存位置】重新选择可写文件夹。"
            )
            return False

    # 保存成功后的反馈（rerun 后渲染）+ 快捷打开
    last_dir = st.session_state.get(f"last_saved_dir_{panel_key}")
    if last_dir:
        st.caption(f"最近保存位置：`{last_dir}`")
    if st.button("📂 打开保存文件夹", key=f"open_saved_dir_{panel_key}", width="stretch"):
        if not _open_in_explorer(save_dir):
            st.warning("文件夹无法打开（可能已被移动），保存时会重新创建")
    return True

