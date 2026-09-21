# -*- coding: utf-8 -*-
"""设置弹窗组件（st.components.v2 双向通道，JS 见 components/settings_dialog/）。

链路：_render_settings_component 挂载组件并下发 payload →
JS sendEvent（setTriggerValue('event', json)）→ trigger 变化自动 rerun →
_handle_settings_event 消费动作 → st.rerun 回推新数据。
组件契约（payload 字段与 action 清单）见 component.js 文件头注释。
"""
import json
from pathlib import Path
from typing import Dict, List

import streamlit as st

from mask_tool.models.detection import DetectionType

from .artifacts import _open_in_explorer
from .assets import WEB_ROOT
from .labels import TYPE_LABELS
from .lexicon_io import (
    _create_lexicon_category,
    _export_lexicon_csv_to_file,
    _get_lexicon_data,
    _get_whitelist,
    _import_lexicon_csv_text,
    _merge_words_into_lexicon,
    _merge_words_into_whitelist,
    _remove_whitelist_words,
)

# ──────────────────────────────────────────────
# 设置弹窗（v2.4：st.components.v2 双向组件注入原型 UI，官方 trigger 通道）
# 注：Streamlit 1.64 的 components.v1 declare_component(path=) 本地路由未注册
# （404）；v2 组件（components.v2.component）JS 运行在主文档，
# setTriggerValue 触发 on_<name>_change 并自动 rerun，为官方双向通道。
# ──────────────────────────────────────────────

_settings_component_ctor = None


def _get_settings_component():
    """注册（并缓存）设置弹窗 v2 组件（components/settings_dialog/component.js）"""
    global _settings_component_ctor
    if _settings_component_ctor is None:
        import streamlit.components.v2 as components_v2

        js = (WEB_ROOT / "components" / "settings_dialog" / "component.js").read_text(
            encoding="utf-8"
        )
        _settings_component_ctor = components_v2.component(
            "settings_dialog", html="<div></div>", js=js,
        )
    return _settings_component_ctor


def _lexicon_payload() -> List[Dict]:
    """词库数据 → 组件渲染所需的 [{cat,label,icon,words}]"""
    data = _get_lexicon_data() or {}
    items = []
    for cat, words in data.items():
        try:
            t = DetectionType(cat)
            full = TYPE_LABELS.get(t, cat)
            icon, label = (full.split(" ", 1) + [full])[:2] if " " in full else ("🏷️", full)
        except ValueError:
            icon, label = "🏷️", cat
        items.append({"cat": cat, "label": label, "icon": icon, "words": list(words)})
    return items


def _flash(level: str, text: str) -> None:
    """一次性提示（下一轮 render 传给组件 toast，随即消费）"""
    st.session_state["_settings_flash"] = {"level": level, "text": text}


def _render_settings_component() -> None:
    """挂载设置弹窗组件并分发 UI 动作（trigger 事件 → 处理 → rerun 回推新数据）。"""
    import json as _json

    from mask_tool.core.app_settings import (
        default_save_dir, get_explicit_save_dir, get_llm_settings, get_save_dir,
        get_theme,
    )

    # P3：LLM 模型配置（api_key 不回传明文，仅回传是否已设置）
    llm = get_llm_settings()
    payload = {
        "lexicon": _lexicon_payload(),
        "whitelist": _get_whitelist(),
        "theme": get_theme(),
        "save_dir": get_save_dir(),
        "save_dir_default": default_save_dir(),
        "save_dir_explicit": bool(get_explicit_save_dir()),
        "llm": {
            "base_url": str(llm.get("base_url", "") or ""),
            "model": str(llm.get("model", "") or ""),
            "role": str(llm.get("role", "adjudicator") or "adjudicator"),
            "api_key": str(llm.get("api_key", "") or ""),
            "api_key_set": bool(llm.get("api_key")),
        },
        "flash": st.session_state.pop("_settings_flash", None),
    }

    comp = _get_settings_component()
    result = comp(data=payload, key="mt_settings", height=0)

    # trigger 事件消费：_ts 时间戳去重（trigger 值跨 rerun 保持）
    ev_raw = getattr(result, "event", None) if result is not None else None
    if ev_raw:
        try:
            ev = _json.loads(ev_raw)
        except (ValueError, TypeError):
            ev = None
        if ev and ev.get("_ts", 0) > st.session_state.get("_settings_ev_ts", 0):
            st.session_state["_settings_ev_ts"] = ev.get("_ts", 0)
            _handle_settings_event(ev)


def _handle_settings_event(ev: Dict) -> None:
    """组件 setComponentValue 事件分发；除纯 UI 动作外处理后均 rerun 刷新数据。"""
    from mask_tool.core.app_settings import (
        default_save_dir, get_save_dir, set_save_dir, set_theme,
    )

    action = ev.get("action")
    if action == "set_theme":
        set_theme(str(ev.get("theme", "auto")))
        st.rerun()
    elif action == "save_dir":
        path = str(ev.get("path", "")).strip()
        if not path:
            _flash("err", "路径不能为空")
        elif not Path(path).is_absolute():
            _flash("err", f"请输入绝对路径：{path}")
        elif set_save_dir(path):
            _flash("ok", f"✅ 已设置保存位置：{path}")
        else:
            _flash("err", "设置写入失败：无法写 config/app_settings.yaml（检查目录权限）")
        st.rerun()
    elif action == "reset_dir":
        set_save_dir("")
        _flash("ok", f"✅ 已恢复默认保存位置：{default_save_dir()}")
        st.rerun()
    elif action == "open_folder":
        if not _open_in_explorer(get_save_dir()):
            _flash("err", "文件夹不存在或无法打开，请先完成一次保存")
        st.rerun()
    elif action == "create_category":
        ok, msg = _create_lexicon_category(str(ev.get("name", "")).strip())
        _flash("ok" if ok else "err", msg)
        st.rerun()
    elif action == "add_words":
        cat = str(ev.get("cat", ""))
        words = [str(w).strip() for w in ev.get("words", []) if str(w).strip()]
        if cat and words:
            added, dup = _merge_words_into_lexicon({cat: words})
            parts = [f"已添加 {added} 条"]
            if dup:
                parts.append(f"跳过重复 {dup} 条")
            _flash("ok" if added else "err", "，".join(parts))
        st.rerun()
    elif action == "add_whitelist":
        # R9：白名单维护（检测排除词，config/whitelist.yaml）
        words = [str(w).strip() for w in ev.get("words", []) if str(w).strip()]
        if words:
            added, dup = _merge_words_into_whitelist(words)
            parts = [f"已添加 {added} 条白名单"]
            if dup:
                parts.append(f"跳过重复 {dup} 条")
            _flash("ok" if added else "err", "，".join(parts))
        st.rerun()
    elif action == "remove_whitelist":
        removed = _remove_whitelist_words([str(ev.get("word", ""))])
        _flash(
            "ok" if removed else "err",
            f"✅ 已从白名单移除「{str(ev.get('word', ''))}」" if removed
            else "词条不在白名单中",
        )
        st.rerun()
    elif action == "export_csv":
        _flash("ok", _export_lexicon_csv_to_file())
        st.rerun()
    elif action == "import_csv":
        _flash("ok", _import_lexicon_csv_text(str(ev.get("text", ""))))
        st.rerun()
    elif action == "save_llm":
        _handle_save_llm(ev)
        st.rerun()
    elif action == "test_llm":
        _handle_test_llm(ev)
        st.rerun()


def _handle_save_llm(ev: Dict) -> None:
    """保存模型配置到 app_settings（整体写 llm 段；api_key 空串=清除）。"""
    from mask_tool.core.app_settings import get_llm_settings, set_llm_settings

    base_url = str(ev.get("base_url", "")).strip()
    model = str(ev.get("model", "")).strip()
    api_key = str(ev.get("api_key", "")).strip()
    role = str(ev.get("role", "adjudicator")).strip().lower()
    if role not in ("adjudicator", "detector", "both"):
        role = "adjudicator"
    if not model:
        _flash("err", "请填写模型名称")
        return
    # api_key 空串=用户未输入新值：保留已存密钥（避免改动模型名时
    # 顺带清掉密钥；前端 password 框刷新后总是空的）。显式清除留待
    # 需要时再加独立动作
    updates = {
        "base_url": base_url, "model": model, "role": role,
        "api_key": api_key if api_key else str(get_llm_settings().get("api_key", "") or ""),
    }
    if set_llm_settings(updates):
        _flash("ok", f"✅ 模型配置已保存：{model or base_url}")
    else:
        _flash("err", "配置写入失败：无法写 config/app_settings.yaml（检查目录权限）")


def _handle_test_llm(ev: Dict) -> None:
    """用表单当前值测试端点连通性（不必先保存）；结果缓存到 llm_health。"""
    from mask_tool.core.llm.client import OpenAICompatClient

    base_url = str(ev.get("base_url", "")).strip()
    model = str(ev.get("model", "")).strip()
    api_key = str(ev.get("api_key", "")).strip()
    if not base_url or not model:
        _flash("err", "请先填写服务地址与模型名称")
        return
    try:
        client = OpenAICompatClient(base_url, model, api_key=api_key, timeout=8)
        ok, msg = client.health_check()
    except Exception as exc:
        ok, msg = False, f"连接失败：{exc}"
    st.session_state["llm_health"] = {"ok": ok, "msg": msg}
    _flash("ok" if ok else "err", ("✅ " if ok else "❌ ") + msg)

