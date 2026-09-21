# -*- coding: utf-8 -*-
"""core 粘合层：配置加载、单文件脱敏/还原、检测与脱敏主流程、学习词落盘。

UI 页面只编排交互；对 core.Pipeline / adapters 的调用集中在本模块。
"""
import json
import logging
import shutil
import tempfile
import time
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional

import logging

import streamlit as st
import yaml

from mask_tool.core.path_masker import PathMasker
from mask_tool.core.pipeline import Pipeline
from mask_tool.models.config import MaskConfig
from mask_tool.models.detection import DetectionResult, DetectionStatus

from .files import _classify_tree_files, _safe_unzip
from .history import BatchRecord, _add_history
from .labels import BLOCKED_EXTS, SUPPORTED_MASK_EXTS, TYPE_LABELS
from .state import _dedup_results

logger = logging.getLogger("mask_tool")

# 批次目录：脱敏输出与 mapping.json 的持久化位置（~/.mask-tool/batches/<batch_id>/）
BATCHES_DIR = Path.home() / ".mask-tool" / "batches"

def _stash_llm_run_summary(pipeline, cfg: MaskConfig) -> None:
    """AI 增强运行摘要（P3 反馈机制）：进程结束时提取 stats 供结果页横幅。

    摘要含 role/enabled/调用统计/首错（友好化文本）；检测或脱敏结束时
    调 _snapshot_llm_summary 提取。llm 未启用时清空旧摘要（零噪声）。
    """
    if cfg.llm.enabled and pipeline.llm_stats is not None:
        st.session_state["_llm_pipeline_ref"] = pipeline  # 结束时读最终统计
    else:
        st.session_state.pop("_llm_run_summary", None)
        st.session_state.pop("_llm_pipeline_ref", None)


def _snapshot_llm_summary() -> None:
    """流程结束时把 pipeline 的最终 LLM 统计固化为摘要 dict（并回写
    侧栏连通徽标 llm_health：真实调用结果比探活更准）。"""
    pipeline = st.session_state.pop("_llm_pipeline_ref", None)
    if pipeline is None or pipeline.llm_stats is None:
        st.session_state.pop("_llm_run_summary", None)
        return
    s = pipeline.llm_stats
    logging.getLogger("mask_tool").info(
        "AI 增强统计：calls=%d errors=%d 复核=%d(drop=%d,adjust=%d) "
        "检出=%d 缓存命中=%d 耗时=%.1fs 首错=%s",
        s.calls, s.errors, s.items_adjudicated, s.dropped, s.adjusted,
        s.detected, s.cache_hits, s.elapsed_seconds,
        s.first_error or "(无)",
    )
    ok = s.calls > 0 and not (s.errors and s.calls == 0)
    st.session_state["_llm_run_summary"] = {
        **s.to_dict(),
        "role": getattr(pipeline.detector, "role", "adjudicator"),
        "ok": s.calls > 0,
        "tripped": getattr(pipeline.detector, "_adjudicator", None) is not None
                   and getattr(pipeline.detector._adjudicator, "_tripped", False),
    }
    # 侧栏徽标：有成功调用=绿；有错误=红+首错；全缓存命中也算绿
    if s.calls > 0:
        st.session_state["llm_health"] = {"ok": True, "msg": f"模型 {s.model}"}
    elif s.first_error or s.errors:
        st.session_state["llm_health"] = {"ok": False, "msg": s.first_error[:60]}
    # 持久化真实运行结果（重开软件后仍有效）：按 yaml 配置指纹回写
    if s.calls > 0 or s.first_error or s.errors:
        from datetime import datetime
        from mask_tool.core.app_settings import (
            get_llm_settings, llm_config_sig, set_llm_settings,
        )
        _llm = get_llm_settings()
        _llm["last_test"] = {
            "ok": s.calls > 0,
            "msg": (f"模型 {s.model}" if s.calls > 0
                    else (s.first_error or "模型调用失败")[:60]),
            "at": datetime.now().isoformat(timespec="minutes"),
            "sig": llm_config_sig(
                str(_llm.get("base_url", "") or ""),
                str(_llm.get("model", "") or ""),
                str(_llm.get("api_key", "") or "")),
        }
        set_llm_settings(_llm)


def _load_config(mode: str, config_path: Optional[str] = None) -> MaskConfig:
    """加载配置（R1-B6：与 CLI 共用 core/config_loader 四级回退链，
    不再静默回退到空词库）。

    1. 显式路径（存在时）
    2. CWD/config/default.yaml
    3. 内嵌模板（警告）
    4. 纯代码默认（警告）
    每次回退均 st.warning，并自动复制示例词库（N2）。
    """
    from mask_tool.core.config_loader import load_config

    explicit = Path(config_path) if config_path else None
    if explicit is not None and not explicit.exists():
        st.warning(f"指定的配置文件不存在: {explicit}，回退到默认查找链")
        explicit = None
    cfg, events = load_config(explicit, mode)
    for level, message in events:
        if level == "ok":
            st.caption(message)
        elif level == "info":
            st.info(message)
        else:
            st.warning(message)
    return cfg


def _ensure_lexicon_exists(cfg: MaskConfig) -> None:
    """兼容保留：词库自动复制已由 core/config_loader.finalize_paths 统一处理。"""
    from mask_tool.core.config_loader import finalize_paths

    finalize_paths(cfg, "web 兼容入口")

def _do_mask_file(
    input_path: Path,
    output_dir: Path,
    pipeline: Pipeline,
    confirmed_results: Optional[List[DetectionResult]] = None,
    output_name: Optional[str] = None,
) -> Optional[Path]:
    """对单个文件执行脱敏（I1a 契约：改调 pipeline.process_file）。

    旧的"逐 run 文本替换"与"无替换时 copy2 原件"路径已废弃：
    process_file 按格式适配器处理并负责无命中文件的输出（H3 消除）。
    confirmed_results 提供时传入 allowed_originals/statuses（确认模式）。
    output_name：目录（zip）任务传原文件名——保持镜像树相对结构，
    名字脱敏统一交给 PathMasker.mask_tree。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    kwargs = {}
    if confirmed_results:
        kwargs["allowed_originals"] = {r.text for r in confirmed_results}
        kwargs["statuses"] = {DetectionStatus.AUTO_MASK, DetectionStatus.SUGGEST_MASK}
    if output_name is not None:
        kwargs["output_name"] = output_name
    try:
        return pipeline.process_file(input_path, output_dir, **kwargs)
    except TypeError:
        # 防御分支：core/pipeline.py 契约签名（allowed_originals/statuses/output_name，
        # I1a 已就位）若在旧环境下缺失，降级为无确认参数调用（全量自动脱敏）。
        kwargs.pop("output_name", None)
        return pipeline.process_file(input_path, output_dir, **kwargs)


# ──────────────────────────────────────────────
# 反脱敏函数（R1-A2：统一走 adapters/restore 公共实现，与 CLI/adapter 同源）
# ──────────────────────────────────────────────

def _unmask_pptx(input_path: Path, output_path: Path, tokens: dict) -> None:
    """反脱敏 pptx 文件。tokens 格式: {token_str: original_str}

    pptx 已无 mask 来源（BLOCKED_EXTS），仅服务旧版产物，保留旧实现。
    """
    from pptx import Presentation
    shutil.copy2(input_path, output_path)
    prs = Presentation(str(output_path))
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    for run in para.runs:
                        for token, original in tokens.items():
                            if token in run.text:
                                run.text = run.text.replace(token, original)
            if shape.has_table:
                for row in shape.table.rows:
                    for cell in row.cells:
                        if cell.text_frame:
                            for para in cell.text_frame.paragraphs:
                                for run in para.runs:
                                    for token, original in tokens.items():
                                        if token in run.text:
                                            run.text = run.text.replace(token, original)
    prs.save(str(output_path))


def _unmask_file(input_path: Path, output_path: Path, tokens: dict) -> Optional[Path]:
    """根据文件类型分发反脱敏（R1-A2）。

    tokens 兼容两种形态：{token: original_str}（旧版/_mapping_to_tokens）或
    mapping 的 tokens 段 {token: {original, kind, ...}}；docx/xlsx 统一委托
    adapters/restore.restore_file_content——页眉/脚注/批注/富文本全覆盖，
    kind=number 的数字单元格还原为数值（旧版 _restore_cell 单参调用导致
    数值还原静默失效的问题随之消除）。
    """
    suffix = input_path.suffix.lower()
    try:
        if suffix in (".docx", ".xlsx"):
            from mask_tool.adapters.restore import restore_file_content

            token_map = {
                t: (v if isinstance(v, dict) else {"original": v})
                for t, v in tokens.items()
            }
            restore_file_content(input_path, output_path, token_map)
        elif suffix == ".pptx":
            _unmask_pptx(input_path, output_path, tokens)
        else:
            # 纯文本文件
            text = input_path.read_text(encoding="utf-8")
            for token, entry in tokens.items():
                original = (
                    entry.get("original", "") if isinstance(entry, dict) else entry
                )
                text = text.replace(token, original)
            output_path.write_text(text, encoding="utf-8")
        return output_path
    except Exception as e:
        st.error(f"反脱敏 {input_path.name} 时出错: {e}")
        return None

# ──────────────────────────────────────────────
# 检测流程
# ──────────────────────────────────────────────

def _run_detection(uploaded_files, mode: str, ner_enabled: bool,
                   manual_words: Optional[List[str]] = None,
                   manual_only: bool = False,
                   zip_file=None):
    """执行检测流程：正文检测 + 文件主名检测（source="path"，可勾选确认）

    R1-A1：正文检测面与 adapter 处理面同源（detect_file_results，
    docx walker 全部件 + xlsx 全部件含数字合成项）；
    R1-B5：进入新一轮检测前清理上一轮残留的上传临时目录（含敏感信息副本）。

    I6：manual_words 注入 Detector 最高优先级通道（source="manual"，
    置信度 0.95，与词库同档）；manual_only=True 时自动检测通道
    （NER/正则/词库）全部关闭，仅剩手动词。手动词条目置顶展示。

    I6 问题2：zip_file 提供时按目录任务处理——安全解压（穿越/炸弹防护）
    后递归检测 docx/xlsx（含文件名），屏蔽类型警告并登记不进产物。
    """
    # 新一轮检测：旧勾选/学习集索引已失效，先行重置（防止残留索引
    # 误读新结果集——learn_set 旧索引会把不相关的词写进词库文件）
    st.session_state.pop("user_selections", None)
    st.session_state.pop("learn_set", None)
    # 节点日志：用户动作入口（文件清单/模式/开关状态），排查"点了没反应/转圈"的第一现场
    _names = [f.name for f in (uploaded_files or [])] or (
        [zip_file.name] if zip_file else []
    )
    logger.info(
        "检测开始：%d 个文件 %s，模式=%s，NER=%s，AI增强=%s",
        len(_names), _names[:5], mode,
        "开" if (ner_enabled and not manual_only) else "关",
        "开" if st.session_state.get("llm_enabled", False) else "关",
    )
    _t_start = time.perf_counter()
    # 加载配置
    cfg = _load_config(mode)
    cfg.ner.enabled = ner_enabled and not manual_only
    # P3：UI 侧栏「AI 增强检测」开关覆盖 enabled；端点未配置时自动回落
    # （端点在设置弹窗「模型配置」维护，经 config_loader 合并进 cfg.llm）
    cfg.llm.enabled = (
        bool(st.session_state.get("llm_enabled", False))
        and bool(cfg.llm.base_url and cfg.llm.model)
    )

    pipeline = Pipeline(
        cfg, manual_words=manual_words,
        auto_detect_enabled=not manual_only,
    )
    _stash_llm_run_summary(pipeline, cfg)

    # R1-B5：清理旧检测轮的上传临时目录（用户只检测不点脱敏时不再永久残留）
    old_tmp = st.session_state.get("tmp_dir")
    if old_tmp:
        shutil.rmtree(old_tmp, ignore_errors=True)
        for key in ("tmp_dir", "saved_paths"):
            st.session_state.pop(key, None)

    # 保存上传文件到临时目录（zip 任务：安全解压为目录树）
    tmp_dir = Path(tempfile.mkdtemp())
    saved_paths = []
    task_kind = "files"
    zip_tree_root: Optional[str] = None
    zip_blocked: List[str] = []
    if zip_file is not None:
        task_kind = "zip"
        try:
            tree_root = _safe_unzip(zip_file.read(), tmp_dir)
        except (ValueError, zipfile.BadZipFile, OSError) as e:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            st.error(f"目录压缩包解压失败：{e}")
            return
        classified = _classify_tree_files(tree_root)
        for p in classified["blocked"]:
            zip_blocked.append(p.relative_to(tree_root).as_posix())
        saved_paths = classified["docs"]
        zip_tree_root = str(tree_root)
        if not saved_paths:
            st.warning("压缩包内没有可处理的 docx/xlsx 文件")
    else:
        for f in uploaded_files:
            save_path = tmp_dir / f.name
            with open(save_path, "wb") as fp:
                fp.write(f.read())
            saved_paths.append(save_path)

    # 文件名脱敏器（检测阶段仅用其 detect_name；与后续脱敏共享组件）
    token_gen = getattr(pipeline, "token_gen", None) or getattr(
        pipeline.masker, "token_gen"
    )
    pm = PathMasker(pipeline.detector, pipeline.policy, token_gen)

    # 逐文件检测
    all_results: List[DetectionResult] = []
    file_results: Dict[str, List[DetectionResult]] = {}

    from mask_tool.adapters.extract import detect_file_results

    for file_path in saved_paths:
        _t_file = time.perf_counter()
        try:
            suffix = file_path.suffix.lower()
            if suffix in BLOCKED_EXTS:
                logger.error("跳过受控类型 %s: %s", file_path.name, BLOCKED_EXTS.get(suffix))
                st.error(
                    f"跳过 {file_path.name}：{BLOCKED_EXTS.get(suffix)}"
                )
                continue
            if suffix not in SUPPORTED_MASK_EXTS:
                logger.warning("跳过不支持的文件类型: %s", file_path.name)
                st.warning(f"跳过不支持的文件类型: {file_path.name}")
                continue

            # R1-A1：与 adapter 处理面同源的检测（已含 policy 决策）
            results: List[DetectionResult] = list(
                detect_file_results(file_path, pipeline.detector, pipeline.policy)
            )
            # 文件主名检测：location.file 填全路径，避免重名上传文件错配
            name_results = pm.detect_name(file_path.name, str(file_path))
            if name_results:
                results.extend(pipeline.policy.apply(name_results))

            all_results.extend(results)
            # zip 任务按相对路径登记（子目录下同名文件不冲破）；单文件任务用文件名
            if task_kind == "zip":
                file_key = file_path.relative_to(Path(zip_tree_root)).as_posix()
            else:
                file_key = file_path.name
            file_results[file_key] = results
            logger.info(
                "检测文件完成：%s → %d 项（%.1fs）",
                file_path.name, len(results), time.perf_counter() - _t_file,
            )
        except Exception as e:
            logger.exception("检测 %s 失败", file_path.name)
            st.error(f"检测 {file_path.name} 时出错: {e}")

    # 跨文件去重
    all_results = _dedup_results(all_results)

    # I6：手动词条目置顶（稳定排序：manual 在前，其余保持原序）
    if manual_words:
        all_results.sort(key=lambda r: r.source != "manual")

    # 存入 session_state
    st.session_state["detection_results"] = all_results
    st.session_state["file_results"] = file_results
    st.session_state["tmp_dir"] = str(tmp_dir)
    st.session_state["saved_paths"] = [str(p) for p in saved_paths]
    st.session_state["task_kind"] = task_kind
    st.session_state["zip_tree_root"] = zip_tree_root
    st.session_state["zip_blocked_files"] = zip_blocked

    _snapshot_llm_summary()  # P3：固化 AI 运行摘要（结果页横幅/侧栏徽标）
    logger.info(
        "检测完成：%d 个文件 → %d 项敏感信息（去重后，总耗时 %.1fs）",
        len(saved_paths), len(all_results), time.perf_counter() - _t_start,
    )
    st.success(f"✅ 检测完成！共发现 **{len(all_results)}** 项敏感信息")
    st.rerun()


# ──────────────────────────────────────────────
# 脱敏执行流程
# ──────────────────────────────────────────────

def _run_masking(
    uploaded_files,
    selected_indices: List[int],
    all_results: List[DetectionResult],
    mode: str,
    ner_enabled: bool,
    irreversible: bool,
    learn_words: bool,
    batch_id: str,
    batch_name: str,
    mask_filenames: bool = True,
    manual_words: Optional[List[str]] = None,
    manual_only: bool = False,
):
    """执行脱敏流程，结果持久化到 session_state。

    - 输出与 mapping.json 写入持久批次目录 ~/.mask-tool/batches/<batch_id>/，
      history.json 仅存批次元数据（不再保存完整映射明文）；
    - mask_filenames=True 时对输出文件主名执行同套检测替换（Token 与内容共享）；
    - 上传临时目录用完即删（finally 清理）；
    - I6：manual_words/manual_only 必须与检测时传同一套（本函数由检测页控件
      取值注入，与 _run_detection 同源），否则会出现"检测看到了但脱敏没脱"。
    """
    # 加载配置
    cfg = _load_config(mode)
    cfg.ner.enabled = ner_enabled and not manual_only
    # P3：UI 侧栏「AI 增强检测」开关覆盖 enabled；端点未配置时自动回落
    # （端点在设置弹窗「模型配置」维护，经 config_loader 合并进 cfg.llm）
    cfg.llm.enabled = (
        bool(st.session_state.get("llm_enabled", False))
        and bool(cfg.llm.base_url and cfg.llm.model)
    )
    logger.info(
        "脱敏开始：批次 %s，确认 %d 项，%s 任务，AI增强=%s",
        batch_id, len(selected_indices),
        st.session_state.get("task_kind", "files"),
        "开" if cfg.llm.enabled else "关",
    )
    _t_start = time.perf_counter()

    pipeline = Pipeline(
        cfg, batch_id=batch_id, manual_words=manual_words,
        auto_detect_enabled=not manual_only,
    )
    _stash_llm_run_summary(pipeline, cfg)

    if irreversible:
        from mask_tool.core.masker import Masker
        from mask_tool.core.tokenizer import TokenGenerator
        pipeline.masker = Masker(TokenGenerator(), irreversible=True)

    # 获取确认的检测结果
    confirmed_results = [all_results[i] for i in selected_indices]
    # 设置状态为 AUTO_MASK
    for r in confirmed_results:
        r.status = DetectionStatus.AUTO_MASK
    confirmed_originals = {r.text for r in confirmed_results}

    # 处理学习词
    learn_set = st.session_state.get("learn_set", set())
    learned_words: Dict[str, List[str]] = {}
    for i in learn_set:
        if i < len(all_results):
            r = all_results[i]
            cat = r.text_type.value
            if cat not in learned_words:
                learned_words[cat] = []
            if r.text not in learned_words[cat]:
                learned_words[cat].append(r.text)

    # 任务分流（I6 问题2）：zip 任务复用检测阶段已解压的目录树；
    # 单文件任务沿用上传文件落盘（检测阶段已落盘的复用）
    task_kind = st.session_state.get("task_kind", "files")
    zip_tree_root: Optional[Path] = None
    tmp_dir = Path(st.session_state.get("tmp_dir", tempfile.mkdtemp()))
    saved_paths = []
    if task_kind == "zip":
        zip_tree_root = Path(st.session_state["zip_tree_root"])
        saved_paths = [Path(p) for p in st.session_state.get("saved_paths", [])]
    else:
        for f in uploaded_files:
            save_path = tmp_dir / f.name
            if not save_path.exists():
                with open(save_path, "wb") as fp:
                    fp.write(f.read())
            saved_paths.append(save_path)

    # M3 防线一（R3-A3）：mask 前预扫描输入中已出现的 token 样式串，
    # 编号让位防撞号（撞号会导致 unmask 时敏感实体错误注入），
    # 与 CLI mask 的 pipeline.prepare 对齐
    pipeline.prepare(saved_paths)

    # 输出目录：持久批次目录（mapping.json 留存于此）
    output_dir = BATCHES_DIR / batch_id
    output_dir.mkdir(parents=True, exist_ok=True)

    # 文件名脱敏器：与 pipeline 共享检测/策略/Token 组件（同一批次 Token 一致）
    token_gen = getattr(pipeline, "token_gen", None) or getattr(
        pipeline.masker, "token_gen"
    )
    pm = PathMasker(
        pipeline.detector, pipeline.policy, token_gen, irreversible=irreversible,
    )

    # 逐文件脱敏（zip 任务写到镜像树保持相对结构，产物打回 zip）
    output_files: List[Path] = []
    mapping_path = output_dir / "mapping.json"
    out_tree = tmp_dir / "out_tree"  # zip 任务的镜像树根（非 zip 任务不用）
    try:
        for file_path in saved_paths:
            suffix = file_path.suffix.lower()
            if suffix in BLOCKED_EXTS:
                st.error(
                    f"跳过 {file_path.name}：{BLOCKED_EXTS.get(suffix)}"
                )
                continue
            if suffix not in SUPPORTED_MASK_EXTS:
                st.warning(f"跳过不支持的文件类型: {file_path.name}")
                continue
            try:
                if task_kind == "zip":
                    # 目录模式（对齐 CLI _mask_directory_tree）：产物保持原名
                    # （不加 _masked 后缀），文件名/目录名统一交给 mask_tree
                    rel = file_path.relative_to(zip_tree_root)
                    result_path = _do_mask_file(
                        file_path, out_tree / rel.parent, pipeline,
                        confirmed_results, output_name=file_path.name,
                    )
                else:
                    result_path = _do_mask_file(
                        file_path, output_dir, pipeline, confirmed_results,
                    )
                if not result_path:
                    continue
                if task_kind != "zip" and mask_filenames:
                    # 单文件：主名脱敏（不改扩展名），Token 与正文共享；
                    # R1-B2：确认语义下勾选即放行，statuses 含 SUGGEST
                    new_path = pm.mask_filename(
                        result_path,
                        allowed_originals=confirmed_originals,
                        statuses=frozenset({
                            DetectionStatus.AUTO_MASK, DetectionStatus.SUGGEST_MASK,
                        }),
                    )
                    if new_path is not None:
                        result_path = new_path
                    for w in pm.last_result.warnings:
                        st.warning(w)
                output_files.append(result_path)
            except Exception as e:
                logger.exception("脱敏 %s 失败", file_path.name)
                st.error(f"脱敏 {file_path.name} 时出错: {e}")

        # zip 任务：非文档文件原样拷贝 + 空目录迁移 + 镜像树改名（mask_tree）
        if task_kind == "zip":
            classified = _classify_tree_files(zip_tree_root)
            for p in classified["others"]:
                rel = p.relative_to(zip_tree_root)
                dest = out_tree / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest)
            for d in sorted(
                {x for x in zip_tree_root.rglob("*") if x.is_dir()},
                key=lambda x: len(x.parts), reverse=True,
            ):
                rel = d.relative_to(zip_tree_root)
                (out_tree / rel).mkdir(parents=True, exist_ok=True)
            out_tree.mkdir(parents=True, exist_ok=True)
            if mask_filenames:
                tree_result = pm.mask_tree(
                    out_tree,
                    allowed_originals=confirmed_originals,
                    statuses=frozenset({
                        DetectionStatus.AUTO_MASK, DetectionStatus.SUGGEST_MASK,
                    }),
                )
                for w in tree_result.warnings:
                    st.warning(w)
            # 收集改名后的产物树（屏蔽类型已不在 out_tree 内）
            output_files = sorted(
                p for p in out_tree.rglob("*") if p.is_file()
            )

        # 保存映射表（tokens 段）+ 并入文件名改名的 paths 段
        pipeline.save_mapping(mapping_path)
        try:
            with open(mapping_path, "r", encoding="utf-8") as f:
                mapping_dict = json.load(f)
            mapping_dict = PathMasker.merge_into_mapping(mapping_dict, pm.export_mappings())
            with open(mapping_path, "w", encoding="utf-8") as f:
                json.dump(mapping_dict, f, ensure_ascii=False, indent=2)
        except Exception as e:
            st.warning(f"映射表 paths 段写入失败: {e}")

        # 学习新词
        if learn_words and learned_words:
            _save_learned_words(learned_words, cfg)

        # ── 生成结果并持久化 ──
        if output_files:
            # 产物规则（I6 问题1）：zip 任务/多文件 → zip 压缩包；
            # 单文件 → 脱敏后同名文件
            download_kind = (
                "zip" if (task_kind == "zip" or len(output_files) > 1) else "file"
            )
            zip_bytes = b""
            file_bytes = b""
            file_name = ""
            if download_kind == "zip":
                zip_buffer = BytesIO()
                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                    if task_kind == "zip":
                        # 目录任务：同结构镜像树打回 zip（arcname 为相对路径）
                        for fp in output_files:
                            zf.write(fp, fp.relative_to(out_tree).as_posix())
                    else:
                        # 多文件任务：文件名已同步替换（fp 即改名后的新路径）
                        for fp in output_files:
                            zf.write(fp, fp.name)
                    # 同时包含映射表（zip 任务另提供单独下载，这里一并放入便于
                    # 整批还原；单文件任务保持原有行为）
                    if mapping_path.exists():
                        zf.write(mapping_path, "mapping.json")
                zip_bytes = zip_buffer.getvalue()
            else:
                file_bytes = output_files[0].read_bytes()
                file_name = output_files[0].name

            # 读取映射数据
            if mapping_path.exists():
                with open(mapping_path, "r", encoding="utf-8") as f:
                    mapping_data = f.read()
            else:
                mapping_data = "{}"

            # 构建映射列表（用于展示）
            mappings = []
            for m in pipeline.masker.get_mappings():
                mappings.append({
                    "token": m.token,
                    "original": m.original,
                    "type_label": TYPE_LABELS.get(m.text_type, m.text_type.value),
                    "confidence": m.confidence,
                })

            # 持久化到 session_state
            st.session_state["mask_result"] = {
                "download_kind": download_kind,
                "zip_buffer": zip_bytes,
                "file_bytes": file_bytes,
                "file_name": file_name,
                "task_kind": task_kind,
                "mapping_data": mapping_data,
                "mappings": mappings,
                "output_files": [str(p) for p in output_files],
                "confirmed_count": len(confirmed_results),
                "batch_id": batch_id,
                "batch_name": batch_name,
            }

            # 保存批次历史（仅元数据；映射留在批次目录 mapping.json）
            history_record = BatchRecord(
                batch_id=batch_id,
                batch_name=batch_name or "",
                created_at=datetime.now().isoformat(),
                file_count=len(output_files),
                mask_count=len(confirmed_results),
                mapping_data="",
                mapping_path=str(mapping_path),
            )
            _add_history(history_record)

            st.rerun()
        else:
            st.warning("⚠️ 未能生成脱敏文件")
    finally:
        # 上传临时目录用完即删（结果已读入内存/批次目录，不依赖 tmp）
        shutil.rmtree(tmp_dir, ignore_errors=True)
        for key in ("tmp_dir", "saved_paths"):
            st.session_state.pop(key, None)
        logger.info(
            "脱敏结束：产物 %d 个（总耗时 %.1fs）",
            len(output_files), time.perf_counter() - _t_start,
        )
        _snapshot_llm_summary()  # P3：脱敏流程结束固化 AI 摘要


def _save_learned_words(learned: dict, config: MaskConfig):
    """将学习到的词追加到词库文件。

    词库路径已由 config_loader 解析为绝对路径（含 frozen 迁移）；
    文件缺失时自动建目录初始化（不再静默丢弃学习词），写入失败仅
    警告不打断主流程。
    """
    lexicon_path = Path(config.lexicon_path)
    try:
        if not lexicon_path.exists():
            lexicon_path.parent.mkdir(parents=True, exist_ok=True)
            lexicon_path.write_text("", encoding="utf-8")

        with open(lexicon_path, "r", encoding="utf-8") as f:
            existing = yaml.safe_load(f) or {}
    except OSError:
        return  # 无法读写时不阻断确认主流程

    new_count = 0
    for category, words in learned.items():
        if category not in existing:
            existing[category] = []
        for word in words:
            if word not in existing[category]:
                existing[category].append(word)
                new_count += 1

    if new_count > 0:
        try:
            with open(lexicon_path, "w", encoding="utf-8") as f:
                yaml.dump(existing, f, allow_unicode=True, default_flow_style=False)
        except OSError as exc:
            st.warning(f"学习词条保存失败（{lexicon_path}）：{exc}")


