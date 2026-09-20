# -*- coding: utf-8 -*-
"""恢复还原 tab：映射选择（历史/手动上传）、还原执行（单文件与目录 zip）。"""
import json
import shutil
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path
from typing import List, Optional

import streamlit as st

from mask_tool.core.path_masker import PathMasker

from ..artifacts import _render_save_to_dir_panel
from ..files import _file_icon, _safe_unzip
from ..history import _load_history
from ..service import _unmask_file

# ──────────────────────────────────────────────
# 标签页2：恢复还原
# ──────────────────────────────────────────────

def _render_restore_tab():
    """渲染恢复还原标签页"""

    # 检查是否有恢复结果需要展示
    if "restore_result" in st.session_state:
        restore_result = st.session_state["restore_result"]

        st.markdown(
            '<div class="success-banner">'
            '<h2>🎉 恢复完成！</h2>'
            f'<p>成功恢复 {restore_result["file_count"]} 个文件</p>'
            '</div>',
            unsafe_allow_html=True,
        )

        # 保存到目标文件夹（桌面交付方式；浏览器下载入口已随桌面化移除）
        _render_save_to_dir_panel(
            artifacts=[("restored_files.zip", restore_result["zip_buffer"])],
            panel_key="restore",
            not_set_what="恢复文件",
        )

        if st.button("🔄 返回", width="stretch"):
            del st.session_state["restore_result"]
            st.rerun()
        return

    st.markdown("#### 🔓 恢复还原")
    st.caption("上传脱敏后的文件和映射表，将敏感信息还原为原始内容")

    st.markdown("---")

    # 选择恢复方式
    restore_method = st.radio(
        "选择恢复方式",
        options=["从历史记录恢复", "手动上传文件恢复"],
        horizontal=True,
    )

    tokens: Optional[dict] = None
    mapping: Optional[dict] = None

    if restore_method == "从历史记录恢复":
        mapping = _render_history_selector()
    else:
        mapping = _render_manual_upload()

    if mapping is None:
        return

    # 统一归约为 {token_str: original_str}（兼容 paths 段的原始 mapping 结构）
    tokens = _mapping_to_tokens(mapping)
    if not tokens:
        st.warning("该批次没有映射数据")
        return

    # 显示映射表预览
    st.markdown("#### 📋 映射表预览")
    preview_items = [
        f"- `{token}` → `{original}`"
        for token, original in list(tokens.items())[:20]
    ]
    st.markdown("\n".join(preview_items))
    if len(tokens) > 20:
        st.caption(f"... 共 {len(tokens)} 条映射")

    st.markdown("---")

    # 上传脱敏后的文件（I6 问题2：支持目录 masked zip 整批还原）
    st.markdown("#### 📁 上传脱敏后的文件")
    masked_files = st.file_uploader(
        "上传需要恢复的脱敏文件",
        type=["docx", "xlsx", "pptx", "pdf", "txt"],
        accept_multiple_files=True,
        label_visibility="collapsed",
    )
    restore_zip = st.file_uploader(
        "或上传目录脱敏 zip（.zip）整批还原",
        type=["zip"],
        key="restore_zip_upload",
        help="上传目录脱敏产物的 zip：内容与文件名/目录名一并还原，"
             "输出同结构目录的 zip",
    )
    if restore_zip is not None and masked_files:
        st.info("已同时上传单文件与目录 zip：本次按目录 zip 还原，单文件列表忽略")

    if not masked_files and restore_zip is None:
        st.info("📤 请上传需要恢复的脱敏文件")
        return

    # 显示已上传文件（单文件列表 + 目录 zip）
    shown_uploads = list(masked_files) + (
        [restore_zip] if restore_zip is not None else []
    )
    if shown_uploads:
        file_cols = st.columns(min(len(shown_uploads), 4))
        for i, f in enumerate(shown_uploads):
            with file_cols[i % len(file_cols)]:
                icon = _file_icon(Path(f.name).suffix)
                size_kb = f.size / 1024
                st.markdown(
                    f'<div class="file-card">'
                    f'<span class="icon">{icon}</span>'
                    f'<div><div class="name">{f.name}</div>'
                    f'<div class="size">{size_kb:.1f} KB</div></div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    # 执行恢复按钮
    if st.button("🔓 执行恢复", type="primary", width="stretch"):
        with st.spinner("正在恢复文件..."):
            if restore_zip is not None:
                _run_restore_zip(restore_zip, mapping)
            else:
                _run_restore(masked_files, mapping)


def _mapping_to_tokens(mapping_json) -> dict:
    """把 mapping 结构统一归约为 {token_str: original_str}。

    兼容三种形态：{"tokens": {token: {...original...}}}（新版含 paths 段）、
    裸 {token: original} dict、[{token, original}] 列表。
    """
    tokens = {}
    if isinstance(mapping_json, dict) and "tokens" in mapping_json:
        raw_tokens = mapping_json["tokens"]
    elif isinstance(mapping_json, dict):
        raw_tokens = mapping_json
    elif isinstance(mapping_json, list):
        raw_tokens = mapping_json
    else:
        return tokens
    if isinstance(raw_tokens, dict):
        for k, v in raw_tokens.items():
            if isinstance(v, dict) and "original" in v:
                tokens[k] = v["original"]
            elif isinstance(v, str):
                tokens[k] = v
    elif isinstance(raw_tokens, list):
        for item in raw_tokens:
            if isinstance(item, dict) and "token" in item and "original" in item:
                tokens[item["token"]] = item["original"]
    return tokens


def _render_history_selector() -> Optional[dict]:
    """从历史记录中选择批次，返回原始 mapping dict（含 tokens 与可选 paths 段）"""
    records = _load_history()

    if not records:
        st.warning("⚠️ 暂无历史记录，请先执行脱敏操作或选择手动上传方式")
        return None

    st.markdown("#### 📚 历史记录")

    # 构建选择列表
    options = []
    for r in reversed(records):  # 最新的在前
        name_part = f" | {r.batch_name}" if r.batch_name else ""
        options.append(
            f"{r.batch_id}{name_part} | {r.created_at[:19]} | "
            f"{r.file_count}个文件 | {r.mask_count}项脱敏"
        )

    selected_idx = st.selectbox(
        "选择批次",
        options=range(len(options)),
        format_func=lambda i: options[i],
    )

    # 获取选中的记录（倒序索引）
    record = records[-(selected_idx + 1)]

    # 解析映射数据：新版优先批次目录 mapping.json；旧版回退记录内明文
    mapping_json = None
    if record.mapping_path and Path(record.mapping_path).exists():
        try:
            with open(record.mapping_path, "r", encoding="utf-8") as f:
                mapping_json = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            st.error(f"批次映射表读取失败: {e}")
            return None
    elif record.mapping_data:
        try:
            mapping_json = json.loads(record.mapping_data)
        except (json.JSONDecodeError, TypeError) as e:
            st.error(f"映射表解析失败: {e}")
            return None

    if mapping_json is None:
        st.error("映射表缺失：批次目录 mapping.json 不存在且记录无映射数据")
        return None

    tokens = _mapping_to_tokens(mapping_json)
    if not tokens:
        st.warning("该批次没有映射数据")
        return None

    # 显示批次详情
    detail_cols = st.columns(4)
    with detail_cols[0]:
        st.metric("批次ID", record.batch_id)
    with detail_cols[1]:
        st.metric("批次名称", record.batch_name or "未命名")
    with detail_cols[2]:
        st.metric("文件数量", record.file_count)
    with detail_cols[3]:
        st.metric("脱敏项数", record.mask_count)

    return mapping_json


def _render_manual_upload() -> Optional[dict]:
    """手动上传映射表，返回原始 mapping dict（含 tokens 与可选 paths 段）"""
    st.markdown("#### 📋 上传映射表")
    st.caption("请上传脱敏时生成的 mapping.json 文件")

    mapping_file = st.file_uploader(
        "上传映射表 JSON",
        type=["json"],
        label_visibility="collapsed",
    )

    if not mapping_file:
        st.info("📤 请上传映射表 JSON 文件")
        return None

    try:
        content = mapping_file.read().decode("utf-8")
        mapping_json = json.loads(content)
        tokens = _mapping_to_tokens(mapping_json)
        if not tokens:
            st.warning("映射表为空")
            return None
        st.success(f"✅ 成功加载 {len(tokens)} 条映射")
        return mapping_json
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as e:
        st.error(f"映射表解析失败: {e}")
        return None


def _run_restore(masked_files, mapping: dict):
    """执行恢复流程。

    mapping 为原始 mapping dict：tokens 段用于内容还原（保留 kind 字段，
    数字单元格可还原为数值，R1-A2）；paths 段（PathMasker.load_paths）
    用于把脱敏后的文件名还原为原主名。
    """
    if isinstance(mapping, dict) and isinstance(mapping.get("tokens"), dict):
        raw_tokens = mapping["tokens"]           # 新版：{token: {original, kind, ...}}
    elif isinstance(mapping, list):
        raw_tokens = {
            item.get("token", ""): item
            for item in mapping
            if isinstance(item, dict) and "token" in item and "original" in item
        }
    else:
        raw_tokens = _mapping_to_tokens(mapping)  # 旧版：{token: original_str}
    tokens = _mapping_to_tokens(mapping)          # 预览/存在性检查用
    paths = PathMasker.load_paths(mapping)

    tmp_dir = Path(tempfile.mkdtemp())
    try:
        output_dir = tmp_dir / "restored"
        output_dir.mkdir(parents=True, exist_ok=True)

        # 保存上传文件到临时目录
        saved_paths = []
        for f in masked_files:
            save_path = tmp_dir / f.name
            with open(save_path, "wb") as fp:
                fp.write(f.read())
            saved_paths.append(save_path)

        # 逐文件恢复
        output_files: List[Path] = []
        for file_path in saved_paths:
            suffix = file_path.suffix.lower()
            if suffix not in {".docx", ".xlsx", ".pptx", ".pdf", ".txt"}:
                st.warning(f"跳过不支持的文件类型: {file_path.name}")
                continue

            # 输出名：paths 段有匹配记录（rel_new 文件名）→ 还原原主名；否则 _restored 后缀
            pm_record = next(
                (m for m in paths
                 if m.rel_new == file_path.name or Path(m.rel_new).name == file_path.name),
                None,
            )
            if pm_record is not None:
                out_name = pm_record.old_name
            else:
                out_name = f"{file_path.stem}_restored{suffix}"

            output_path = output_dir / out_name
            result = _unmask_file(file_path, output_path, raw_tokens)
            if result:
                output_files.append(result)

        if not output_files:
            st.error("❌ 未能恢复任何文件")
            return

        # 打包为 ZIP
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for fp in output_files:
                zf.write(fp, fp.name)

        zip_bytes = zip_buffer.getvalue()

        # 存入 session_state
        st.session_state["restore_result"] = {
            "zip_buffer": zip_bytes,
            "file_count": len(output_files),
        }

        st.rerun()
    finally:
        # 临时目录用完即删
        shutil.rmtree(tmp_dir, ignore_errors=True)



    # 展示结果（rerun 后会到达下面的代码）
    # 注意：由于上面已经 rerun，下面的代码不会执行
    # 结果展示在 _render_restore_tab 中检查 restore_result


def _run_restore_zip(zip_file, mapping: dict):
    """目录 masked zip 整批还原（I6 问题2）。

    流程：安全解压（穿越/炸弹防护，与脱敏侧同一套）→ docx/xlsx 内容还原
    到镜像树（restore_file_content，kind=number 数值还原）→ PathMasker
    .unmask_tree 自底向上还原文件名/目录名（paths 段）→ 重新打包 zip。
    mapping.json 不进入还原产物（含明文映射，不应随交付物流转）。
    """
    raw_tokens = {}
    if isinstance(mapping, dict) and isinstance(mapping.get("tokens"), dict):
        raw_tokens = mapping["tokens"]  # {token: {original, kind, ...}}
    else:
        raw_tokens = _mapping_to_tokens(mapping)  # 旧形态：{token: original_str}
    paths = PathMasker.load_paths(mapping)

    tmp_dir = Path(tempfile.mkdtemp())
    try:
        try:
            tree = _safe_unzip(zip_file.read(), tmp_dir)
        except (ValueError, zipfile.BadZipFile, OSError) as e:
            st.error(f"目录 zip 解压失败：{e}")
            return

        restored_root = tmp_dir / "restored"
        restored_root.mkdir(parents=True, exist_ok=True)

        # 1) 内容还原：docx/xlsx 逐文件到镜像树（restore_file_content
        #    契约：输入文件不被修改，因此写副本而非原地改）
        from mask_tool.adapters.restore import restore_file_content

        token_map = {
            t: (v if isinstance(v, dict) else {"original": v})
            for t, v in raw_tokens.items()
        }
        restored_files = 0
        for f in sorted(tree.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(tree)
            if rel.as_posix() == "mapping.json":
                continue  # 映射表不进还原产物
            dest = restored_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if f.suffix.lower() in (".docx", ".xlsx"):
                try:
                    restore_file_content(f, dest, token_map)
                    restored_files += 1
                except Exception as e:
                    st.warning(f"内容还原失败（原样拷入）: {rel.as_posix()} ({e})")
                    shutil.copy2(f, dest)
            else:
                shutil.copy2(f, dest)

        # 2) 名字还原：paths 段自底向上（先子后父）改回原名
        if paths:
            pm = PathMasker(None, None, None)  # 还原仅需改名能力
            res = pm.unmask_tree(restored_root, paths)
            for w in res.warnings:
                st.warning(w)

        # 3) 重新打包 zip
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for fp in sorted(p for p in restored_root.rglob("*") if p.is_file()):
                zf.write(fp, fp.relative_to(restored_root).as_posix())

        st.session_state["restore_result"] = {
            "zip_buffer": zip_buffer.getvalue(),
            "file_count": restored_files,
        }
        st.rerun()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

