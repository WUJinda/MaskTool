# -*- coding: utf-8 -*-
"""恢复还原 tab：映射选择（历史/手动上传）、还原执行（单文件与目录 zip）。

UI 与脱敏主流程同一套设计系统（app.css）：步骤指示器 / up-title 双栏
上传 / notice 空态引导 / expander+dataframe 映射预览，明暗主题随全局适配。
"""
import html as _html
import json
import shutil
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path
from typing import List, Optional

import pandas as pd
import streamlit as st

from mask_tool.core.path_masker import PathMasker, unique_name

from ..artifacts import _render_save_to_dir_panel
from ..files import _safe_unzip
from ..history import _load_history
from ..service import _unmask_file
from .masking import render_steps

# ──────────────────────────────────────────────
# 标签页2：恢复还原
# ──────────────────────────────────────────────

_RESTORE_STEPS = [
    ("1", "🔑 选映射"),
    ("2", "📤 传文件"),
    ("3", "♻️ 执行还原"),
]


def _restore_flow_step() -> int:
    """恢复流程当前步骤（与脱敏侧同由会话状态推导，而非渲染位置）。

    依据本轮交互前的控件值（radio/上传器 key）：已选映射来源且映射可加载
    → 2 起步；已上传待还原文件 → 3。历史无记录时视为映射未就绪。
    """
    method = st.session_state.get("restore_method", "从历史记录恢复")
    if method == "从历史记录恢复":
        mapping_ready = bool(_load_history())
    else:
        mapping_ready = st.session_state.get("restore_mapping_file") is not None
    if not mapping_ready:
        return 1
    has_files = (
        bool(st.session_state.get("restore_masked_files"))
        or st.session_state.get("restore_zip_upload") is not None
    )
    return 3 if has_files else 2


def _render_restore_tab():
    """渲染恢复还原标签页（与脱敏页同一套视觉语言）"""

    # 面板锤点：隐藏标记，供 app.css 以 :has() 锚定本面板做垂直节奏
    # 调优（stMarkdownContainer 默认 -16px 下边距会让自定义块互相侵入）
    st.markdown('<div class="mt-restore-scope" hidden></div>', unsafe_allow_html=True)

    # 恢复结果页（持久化到 session_state；风格对齐脱敏结果页）
    if "restore_result" in st.session_state:
        restore_result = st.session_state["restore_result"]

        st.markdown(
            '<div class="success-banner">'
            '<h2>🎉 恢复完成！</h2>'
            f'<p>成功恢复 {restore_result["file_count"]} 个文件</p>'
            '</div>',
            unsafe_allow_html=True,
        )

        # 还原后对账结果：残留即还原缺陷，分发前务必核查
        residuals = restore_result.get("residuals") or []
        if residuals:
            st.error(
                "⚠️ 还原后仍检测到映射表内替换串残留（还原缺陷），"
                "请勿直接分发，建议核查映射表与本批文件的对应关系：\n"
                + "\n".join(f"- {r}" for r in residuals)
            )

        # 保存到目标文件夹（桌面交付方式；浏览器下载入口已随桌面化移除）
        st.markdown("#### 💾 保存还原文件")
        _render_save_to_dir_panel(
            artifacts=[("restored_files.zip", restore_result["zip_buffer"])],
            panel_key="restore",
            not_set_what="还原文件",
        )

        if st.button("🔄 返回", width="stretch"):
            del st.session_state["restore_result"]
            st.rerun()
        return

    # ── 唯一步骤条：固定顶部，下滑始终可见（与脱敏页同构）──
    render_steps(_restore_flow_step(), _RESTORE_STEPS)

    # 映射来源选择（还原钥匙）
    st.markdown(
        '<div class="mi-label"><b>🔑 映射表来源</b>'
        '<span class="badge">还原钥匙，请妥善保管</span></div>',
        unsafe_allow_html=True,
    )
    restore_method = st.radio(
        "映射表来源",
        options=["从历史记录恢复", "手动上传文件恢复"],
        horizontal=True,
        label_visibility="collapsed",
        key="restore_method",
    )

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

    # 映射表预览（与脱敏结果页映射表同构：expander + dataframe）
    st.markdown(
        '<div class="mi-label"><b>📋 映射表预览</b>'
        f'<span class="badge">共 {len(tokens)} 条</span></div>',
        unsafe_allow_html=True,
    )
    with st.expander("展开查看 token → 原文对照", expanded=False):
        st.dataframe(
            pd.DataFrame({
                "Token": list(tokens.keys()),
                "原文": list(tokens.values()),
            }),
            width="stretch",
            hide_index=True,
        )

    st.markdown("---")

    # ── Step 2: 上传脱敏后的文件（与脱敏页同构：左单文件 / 右目录 zip）──
    up_cols = st.columns([1.35, 1])
    with up_cols[0]:
        st.markdown(
            '<div class="up-title">📄 <b>单文件上传</b>'
            '<span>支持多选 .docx / .xlsx / .pptx / .txt</span></div>',
            unsafe_allow_html=True,
        )
        masked_files = st.file_uploader(
            "上传需要恢复的脱敏文件",
            type=["docx", "xlsx", "pptx", "txt"],
            accept_multiple_files=True,
            label_visibility="collapsed",
            key="restore_masked_files",
        )
    with up_cols[1]:
        st.markdown(
            '<div class="up-title">🗂️ <b>目录脱敏 zip</b>'
            '<span>文件名与内容整批还原</span></div>',
            unsafe_allow_html=True,
        )
        restore_zip = st.file_uploader(
            "或上传目录脱敏 zip（.zip）",
            type=["zip"],
            key="restore_zip_upload",
            label_visibility="collapsed",
            help=("上传目录脱敏产物的 zip：内容与文件名/目录名一并还原，"
                  "输出同结构目录的 zip"),
        )
    if restore_zip is not None and masked_files:
        st.info("已同时上传单文件与目录 zip：本次按目录 zip 还原，单文件列表忽略")

    if not masked_files and restore_zip is None:
        st.markdown(
            '<div class="notice"><span class="ic">📤</span>'
            '<div class="tx">请上传需要还原的脱敏文件'
            '<small>单文件可选 .docx / .xlsx / .pptx / .txt；目录脱敏 zip 可整批还原</small>'
            '</div></div>',
            unsafe_allow_html=True,
        )
        return

    # 已上传文件：由上传框内原生文件 chips 展示（含单个删除与追加，
    # 交互完整）；不再另渲染自定义清单，避免同一信息双重呈现

    # ── Step 3: 执行还原 ──
    if st.button("♻️ 执行恢复", type="primary", width="stretch"):
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

    st.markdown(
        '<div class="up-title">📚 <b>历史批次</b>'
        '<span>最近执行过的脱敏任务</span></div>',
        unsafe_allow_html=True,
    )

    # 构建选择列表（时间戳去掉 T 分隔，便于扫读）
    options = []
    for r in reversed(records):  # 最新的在前
        name_part = f" | {r.batch_name}" if r.batch_name else ""
        options.append(
            f"{r.batch_id}{name_part} | {r.created_at[:19].replace('T', ' ')} | "
            f"{r.file_count}个文件 | {r.mask_count}项脱敏"
        )

    selected_idx = st.selectbox(
        "选择批次",
        options=range(len(options)),
        format_func=lambda i: options[i],
        label_visibility="collapsed",
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

    # 批次详情不另设摘要行：下拉框选中态始终完整显示
    # 「批次ID | 名称 | 日期 | N个文件 | N项脱敏」，映射数由下方
    # 「映射表预览 · 共 N 条」徽标承载，避免同信息二次表达

    return mapping_json


def _render_manual_upload() -> Optional[dict]:
    """手动上传映射表，返回原始 mapping dict（含 tokens 与可选 paths 段）"""
    st.markdown(
        '<div class="up-title">📋 <b>上传映射表</b>'
        '<span>脱敏时生成的 mapping.json</span></div>',
        unsafe_allow_html=True,
    )

    mapping_file = st.file_uploader(
        "上传映射表 JSON",
        type=["json"],
        label_visibility="collapsed",
        key="restore_mapping_file",
    )

    if not mapping_file:
        st.markdown(
            '<div class="notice"><span class="ic">🔑</span>'
            '<div class="tx">请上传脱敏时生成的 <b>mapping.json</b> 映射表'
            '<small>可在脱敏结果的保存目录中找到；映射表是还原钥匙，请分开保管</small>'
            '</div></div>',
            unsafe_allow_html=True,
        )
        return None

    try:
        content = mapping_file.read().decode("utf-8")
        mapping_json = json.loads(content)
        tokens = _mapping_to_tokens(mapping_json)
        if not tokens:
            st.warning("映射表为空")
            return None
        st.markdown(
            f'<div class="mi-count">已加载 <b>{len(tokens)}</b> 条映射</div>',
            unsafe_allow_html=True,
        )
        return mapping_json
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as e:
        st.error(f"映射表解析失败: {e}")
        return None


def _audit_restore_residuals(files: List[Path], token_map: dict) -> List[str]:
    """还原后对账（与 CLI unmask 防线二同口径，审查 m-3 补线）。

    扫描产物中残留的映射表内替换串：token 样式走正则，伪装词键
    （不含 '['）逐键精确查；返回发现列表（"文件: 替换串"）。
    扫描失败的文件跳过（尽力而为，不阻断交付）。"""
    from mask_tool.adapters.extract import extract_texts
    from mask_tool.core.engine import ReplacementEngine

    alias_keys = [k for k in token_map if k and "[" not in k]
    found: List[str] = []
    for f in files:
        if f.suffix.lower() not in {".docx", ".xlsx", ".xlsm", ".pdf"}:
            continue  # txt/pptx 无统一提取器，尽力而为跳过
        try:
            text = extract_texts(f)
        except Exception:
            continue
        residuals = ReplacementEngine.scan_tokens(text) & set(token_map)
        residuals |= ReplacementEngine.scan_alias_residuals(text, alias_keys)
        for r in sorted(residuals):
            found.append(f"{f.name}: {r}")
    return found


def _run_restore(masked_files, mapping: dict):
    """执行恢复流程。

    mapping 为原始 mapping dict：tokens 段用于内容还原（保留 kind 字段，
    数字单元格可还原为数值，R1-A2）；paths 段（PathMasker.load_paths）
    用于把脱敏后的文件名还原为原主名。

    重名防御：同名上传文件暂存名去重（原名仅用于 paths 匹配，不受暂存
    改名影响）；输出名用 unique_name 消解——不同目录的同原名文件
    （如 a/合同.docx 与 b/合同.docx）各自产出，不互相覆盖、不产生
    zip 重复条目（与 unmask_tree 同一冲突消解语义）。
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
    paths = PathMasker.load_paths(mapping)

    tmp_dir = Path(tempfile.mkdtemp())
    try:
        output_dir = tmp_dir / "restored"
        output_dir.mkdir(parents=True, exist_ok=True)

        # 暂存上传文件（同名去重；basename 归一防 API 途径带路径分隔符）
        saved: List[tuple] = []                    # (原始上传名, 暂存路径)
        taken_stage: set = set()
        for f in masked_files:
            original_name = Path(f.name).name
            stage_name = unique_name(tmp_dir, original_name, taken_stage)
            taken_stage.add(stage_name.casefold())
            save_path = tmp_dir / stage_name
            with open(save_path, "wb") as fp:
                fp.write(f.read())
            saved.append((original_name, save_path))

        # 逐文件恢复（输出重名消解）
        taken_out: set = set()
        output_files: List[Path] = []
        for original_name, file_path in saved:
            suffix = file_path.suffix.lower()
            # 还原分发底层 _unmask_file 已支持 docx/xlsx/xlsm/pdf
            # （pptx 走 _unmask_pptx，txt 走纯文本替换）
            if suffix not in {".docx", ".xlsx", ".xlsm", ".pdf",
                              ".pptx", ".txt"}:
                st.warning(f"跳过不支持的文件类型: {original_name}")
                continue

            # 输出名：paths 段有匹配记录（rel_new 文件名）→ 还原原主名；否则 _restored 后缀
            pm_record = next(
                (m for m in paths
                 if m.rel_new == original_name or Path(m.rel_new).name == original_name),
                None,
            )
            desired = (
                pm_record.old_name if pm_record is not None
                else f"{Path(original_name).stem}_restored{suffix}"
            )
            out_name = unique_name(output_dir, desired, taken_out)
            taken_out.add(out_name.casefold())

            output_path = output_dir / out_name
            result = _unmask_file(file_path, output_path, raw_tokens)
            if result:
                output_files.append(result)

        if not output_files:
            st.error("❌ 未能恢复任何文件")
            return

        # 还原后对账：残留（还原缺陷）记入结果页红字提示（不阻断交付，
        # 用户可自行核查后再分发）
        residuals = _audit_restore_residuals(output_files, raw_tokens)

        # 打包为 ZIP
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for fp in output_files:
                zf.write(fp, fp.name)

        # 存入 session_state（结果页由 _render_restore_tab 读取渲染）
        st.session_state["restore_result"] = {
            "zip_buffer": zip_buffer.getvalue(),
            "file_count": len(output_files),
            "residuals": residuals,
        }

        st.rerun()
    finally:
        # 临时目录用完即删（zip bytes 已先取入 session）
        shutil.rmtree(tmp_dir, ignore_errors=True)


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
            if f.suffix.lower() in (".docx", ".xlsx", ".xlsm", ".pdf"):
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

        # 3) 还原后对账（同 _run_restore，残留记入结果页红字提示）
        _doc_exts = {".docx", ".xlsx", ".xlsm", ".pdf"}
        residuals = _audit_restore_residuals(
            [p for p in restored_root.rglob("*")
             if p.is_file() and p.suffix.lower() in _doc_exts],
            token_map,
        )

        # 4) 重新打包 zip
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for fp in sorted(p for p in restored_root.rglob("*") if p.is_file()):
                zf.write(fp, fp.relative_to(restored_root).as_posix())

        st.session_state["restore_result"] = {
            "zip_buffer": zip_buffer.getvalue(),
            "file_count": restored_files,
            "residuals": residuals,
        }
        st.rerun()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

