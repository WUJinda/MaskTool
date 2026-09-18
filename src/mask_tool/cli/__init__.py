"""CLI命令行入口

波次2（D1 §4/§5）要点：
- 配置回退链（H5）：显式 --config 缺失即报错；CWD/config -> 内嵌模板 -> 纯默认，
  每级回退警告并明示词库路径与存在性；lexicon.yaml 缺失自动从 sample 复制（N2）。
- mask 批次化（M4）：所有产物写入 output/<时间戳-短uuid>/；目录输入走镜像树
  模式（docx/xlsx 内容脱敏 -> 非文档拷贝 -> PathMasker.mask_tree 改名 ->
  mapping.json 合并 paths 段）。
- unmask 三道防线（M3）：reserved 预扫描在 mask 侧（pipeline.prepare）；
  还原后 TOKEN_PATTERN 对账扫描（∈mapping 报错 / ∉mapping 警告 / --force 忽略）；
  --verify 指纹复算比对。
- --confirm 重构（H6/N3）：walker 全文检测 -> 勾选 ->
  pipeline.process_file(allowed_originals, statuses={AUTO,SUGGEST})；
  _write_masked_file 已删除，不再出现"纯文本写 .pdf"路径。
- _collect_files 使用 core.formats 常量，PPT/PDF 命中 BLOCKED_EXTS 黄色警告跳过。
"""

import json
import os
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Set, Tuple

import typer
from rich.console import Console
from rich.table import Table

from mask_tool import __version__
from mask_tool.models.config import MaskConfig
from mask_tool.models.detection import DetectionStatus
from mask_tool.models.mapping import fingerprint_of
from mask_tool.core.engine import ReplacementEngine, TOKEN_PATTERN
from mask_tool.core.formats import BLOCKED_EXTS, SUPPORTED_MASK_EXTS
from mask_tool.core.pipeline import Pipeline

app = typer.Typer(
    name="mask-tool",
    help="本地文件脱敏工具 - 支持可逆脱敏、智能识别、多格式办公文件处理",
    no_args_is_help=True,
)
console = Console()


# ---------------------------------------------------------------------------
# 配置加载（H5 回退链 + N2 自动复制；R1-B6：公共实现 core/config_loader.py，
# CLI/Web 共用，此处仅做 rich console 渲染包装）
# ---------------------------------------------------------------------------

def _config_from_dict(data: dict, config_dir: str = "") -> MaskConfig:
    """从已解析的 yaml dict 构造 MaskConfig（委托公共实现）。"""
    from mask_tool.core.config_loader import config_from_dict
    return config_from_dict(data, config_dir)


def _resolve_data_path(value: str, base_dir: Path) -> Path:
    """词库等数据路径解析（委托公共实现）。"""
    from mask_tool.core.config_loader import resolve_data_path
    return resolve_data_path(value, base_dir)


_RENDER_STYLE = {"ok": "green", "info": "blue", "warn": "yellow", "error": "red"}


def _render_config_events(events) -> None:
    """把公共配置事件渲染到 rich console（级别 -> 颜色）。"""
    for level, message in events:
        style = _RENDER_STYLE.get(level, "yellow")
        console.print(f"[{style}]{message}[/{style}]")


def _finalize_paths(cfg: MaskConfig, source_desc: str) -> None:
    """解析词库/白名单为绝对路径并做存在性检查（H5/N2，委托公共实现）。

    - lexicon.yaml 缺失且同目录存在 sample_lexicon.yaml -> 自动复制（N2）
    - 解析后仍不存在 -> 警告明示"词库为空"
    """
    from mask_tool.core.config_loader import finalize_paths
    _render_config_events(finalize_paths(cfg, source_desc))


def _load_config(config_path: Optional[Path], mode: str) -> MaskConfig:
    """加载配置（H5 四级回退链，R1-B6 委托公共实现 core/config_loader）。

    1. 显式 --config：不存在 -> 报错退出（不再静默回退）
    2. CWD/config/default.yaml
    3. 内嵌模板 core/templates.DEFAULT_CONFIG_YAML
    4. 纯代码默认 MaskConfig(mode)（模板解析失败时的兜底）

    每次回退均警告，并打印当前生效的词库路径与是否存在。
    """
    from mask_tool.core.config_loader import ConfigError, load_config

    try:
        cfg, events = load_config(config_path, mode)
    except ConfigError as e:
        console.print(f"[red]错误: {e}[/red]")
        raise typer.Exit(1)
    _render_config_events(events)
    return cfg


# ---------------------------------------------------------------------------
# 文件收集（§5：formats 常量 + 屏蔽警告）
# ---------------------------------------------------------------------------

def _warn_blocked(path: Path) -> None:
    suffix = path.suffix.lower()
    console.print(f"[yellow]警告: 跳过 {path.name} — {BLOCKED_EXTS.get(suffix, '不支持的格式')}[/yellow]")


def _is_under(path: Path, base: Path) -> bool:
    """path 是否位于 base 目录内（含相等；两侧均 resolve 后比较）。"""
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _output_excludes(output: Path, inputs, batch_dir: Path) -> List[Path]:
    """R1-B1：计算输入收集的排除集。

    常规：排除整个输出目录子树（防自嵌套吸入历史批次产物）；
    病态（任一输入位于 output 内，如 mask . -o .）：排除整个 output 会把
    输入全部排除，退化为仅排除当前批次目录（至少阻断本批次递归）。
    """
    out_r = Path(output).resolve()
    for p in inputs:
        if _is_under(Path(p).resolve(), out_r):
            return [batch_dir.resolve()]
    return [out_r]


def _collect_files(
    input_path: Path,
    exclude: Optional[List[Path]] = None,
) -> List[Path]:
    """收集要处理的文件（递归；PPT/PDF 命中 BLOCKED_EXTS 黄色警告跳过）。

    exclude（R1-B1）：目录收集时排除位于这些目录（含子树）内的文件，
    防止输出目录在源目录内部时历史批次产物被再次吸入（自嵌套）。
    """
    excludes = [Path(e).resolve() for e in (exclude or [])]
    files: List[Path] = []
    if input_path.is_file():
        suffix = input_path.suffix.lower()
        if suffix in SUPPORTED_MASK_EXTS:
            files.append(input_path)
        elif suffix in BLOCKED_EXTS:
            _warn_blocked(input_path)
        else:
            console.print(f"[yellow]警告: 不支持的文件格式 {input_path.suffix}，已跳过: {input_path}[/yellow]")
    elif input_path.is_dir():
        for f in sorted(input_path.rglob("*")):
            if not f.is_file():
                continue
            if excludes and any(
                _is_under(f.resolve(), ex) for ex in excludes
            ):
                continue  # R1-B1：输出目录子树不收集
            suffix = f.suffix.lower()
            if suffix in SUPPORTED_MASK_EXTS:
                files.append(f)
            elif suffix in BLOCKED_EXTS:
                _warn_blocked(f)
    return files


def _iter_tree_files(root: Path, exclude: Optional[List[Path]] = None):
    """稳定顺序递归产出目录下全部文件（exclude 同 _collect_files，R1-B1）。"""
    excludes = [Path(e).resolve() for e in (exclude or [])]
    for f in sorted(root.rglob("*")):
        if not f.is_file() or f.is_symlink():
            continue
        if excludes and any(_is_under(f.resolve(), ex) for ex in excludes):
            continue
        yield f


def _print_detection_table(results: list) -> None:
    """用rich表格展示检测结果"""
    table = Table(title="检测结果", show_lines=True)
    table.add_column("敏感信息", style="red bold")
    table.add_column("类别")
    table.add_column("来源")
    table.add_column("置信度", justify="right")
    table.add_column("处置")

    status_map = {
        DetectionStatus.AUTO_MASK: "[green]自动脱敏[/green]",
        DetectionStatus.SUGGEST_MASK: "[yellow]建议脱敏[/yellow]",
        DetectionStatus.HINT_ONLY: "[dim]仅提示[/dim]",
    }

    for r in results:
        table.add_row(
            r.text,
            r.text_type.value,
            r.source,
            f"{r.confidence:.2f}",
            status_map.get(r.status, str(r.status)),
        )

    console.print(table)


def _new_confirm_engine():
    """确认引擎工厂（测试可替换以注入 auto_yes）。"""
    from mask_tool.core.confirm import ConfirmEngine
    return ConfirmEngine()


# ---------------------------------------------------------------------------
# mask 命令
# ---------------------------------------------------------------------------

@app.command()
def mask(
    input_path: List[Path] = typer.Argument(..., help="输入文件或目录路径", exists=True),
    output: Path = typer.Option("./output", "--output", "-o", help="输出目录（产物写入其下批次目录）"),
    mode: str = typer.Option("smart", "--mode", "-m", help="运行模式: focused/strict/smart/aggressive"),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="配置文件路径"),
    irreversible: bool = typer.Option(False, "--irreversible", help="使用不可逆脱敏"),
    confirm: bool = typer.Option(False, "--confirm", help="启用交互式确认模式（逐项勾选）"),
    all_items: bool = typer.Option(False, "--all", help="同时替换建议脱敏项(SUGGEST_MASK)；默认仅自动脱敏项"),
    learn: bool = typer.Option(True, "--learn/--no-learn", help="确认时学习到词库（默认开启）"),
    mask_names: bool = typer.Option(
        True, "--mask-names/--no-mask-names",
        help="对输出产物做文件名/目录名脱敏（单文件脱敏主名，目录模式脱敏镜像树；默认开启）",
    ),
) -> None:
    """对文件或目录执行脱敏处理

    单文件：产物写入批次目录，默认主名同步脱敏并追加 _masked 后缀
    （--no-mask-names 关闭，关闭后为 {stem}_masked.ext）。
    目录：递归处理 docx/xlsx 到批次目录下的镜像树（保持原文件名），
    非文档文件原样拷贝，pptx/pdf 跳过并警告；--confirm 下未勾选
    （内容未脱敏）的文件拷入镜像树内 skipped_unmasked/ 隔离子目录；
    默认对镜像树做文件名/目录名脱敏（--no-mask-names 关闭）。
    """
    console.print(f"[bold green]mask-tool[/bold green] v{__version__}")
    console.print(
        f"模式: {mode} | 不可逆: {irreversible} | 确认: {'开' if confirm else '关'}"
        f" | 建议项(--all): {'开' if all_items else '关'}"
    )

    cfg = _load_config(config, mode)

    # M4 批次目录：output/<时间戳-短uuid>，与 mapping 的 batch_id 一致
    batch_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    batch_dir = Path(output) / batch_id
    # R1-B1：输出子树不作为输入收集（防自嵌套吸入历史批次产物）
    output_excludes = _output_excludes(Path(output), input_path, batch_dir)
    pipeline = Pipeline(
        cfg,
        lexicon_path=cfg.lexicon_path,
        whitelist_path=cfg.whitelist_path,
        batch_id=batch_id,
    )
    if irreversible:
        pipeline.masker = type(pipeline.masker)(
            pipeline.masker.token_gen,
            irreversible=True,
            amount_mode=cfg.amount_mode,
            batch_id=batch_id,
        )

    # 输入分流：文件走单文件模式，目录走镜像树模式
    file_inputs: List[Path] = []
    dir_inputs: List[Path] = []
    for p in input_path:
        if p.is_dir():
            dir_inputs.append(p)
        else:
            file_inputs.append(p)

    single_files: List[Path] = []
    for p in file_inputs:
        single_files.extend(_collect_files(p, exclude=output_excludes))

    dir_docs: List[Path] = []
    for d in dir_inputs:
        for f in _iter_tree_files(d, exclude=output_excludes):
            if f.suffix.lower() in SUPPORTED_MASK_EXTS:
                dir_docs.append(f)
            elif f.suffix.lower() in BLOCKED_EXTS:
                _warn_blocked(f)

    if not single_files and not dir_inputs:
        console.print("[yellow]未找到可处理的文件[/yellow]")
        raise typer.Exit(1)

    total = len(single_files) + len(dir_docs)
    console.print(f"找到 {total} 个文件待处理（目录 {len(dir_inputs)} 个）\n")

    # M3 防线一：mask 前预扫描已出现的 token，编号让位
    pipeline.prepare([*single_files, *dir_docs])

    statuses = (
        frozenset({DetectionStatus.AUTO_MASK, DetectionStatus.SUGGEST_MASK})
        if all_items else None
    )
    # R1-B2：路径处置集与内容对齐——默认仅 AUTO；--all 含 SUGGEST；
    # --confirm 逐文件勾选（statuses={AUTO,SUGGEST} + 勾选集），路径侧
    # 同取 {AUTO,SUGGEST}（R3-B3：勾选即放行，与内容侧/Web 语义一致；
    # 默认/--all 模式维持原行为）
    if confirm:
        path_statuses = frozenset(
            {DetectionStatus.AUTO_MASK, DetectionStatus.SUGGEST_MASK}
        )
    else:
        path_statuses = statuses or frozenset({DetectionStatus.AUTO_MASK})
    confirm_engine = _new_confirm_engine() if confirm else None

    start_time = time.time()

    path_dicts: List[dict] = []
    for f in single_files:
        path_dicts.extend(
            _mask_single_file(
                f, batch_dir, pipeline, confirm_engine, statuses,
                mask_names, path_statuses, irreversible,
            ),
        )

    skipped_unmasked_total = 0
    for d in dir_inputs:
        tree_dicts, tree_skipped = _mask_directory_tree(
            d, batch_dir, pipeline, confirm_engine,
            statuses, mask_names, irreversible, path_statuses,
            output_excludes,
        )
        path_dicts.extend(tree_dicts)
        skipped_unmasked_total += tree_skipped

    elapsed = time.time() - start_time
    pipeline.report.processing_time_seconds = elapsed

    # 学习机制：将用户确认的词写入词库
    if confirm_engine and learn and confirm_engine.learned:
        _save_learned_words(confirm_engine.get_learned_words(), cfg)

    # 保存映射表（目录模式与单文件名脱敏均合并 paths 段；无改名时也写入空段，
    # 明示已关闭/无命中）
    mapping_path = batch_dir / "mapping.json"
    pipeline.save_mapping(mapping_path)
    if dir_inputs or path_dicts:
        from mask_tool.core.path_masker import PathMasker

        data = json.loads(mapping_path.read_text(encoding="utf-8"))
        PathMasker.merge_into_mapping(data, path_dicts)
        mapping_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    console.print(f"映射表: {mapping_path}")

    # 保存报告
    report_path = batch_dir / "report.json"
    pipeline.save_report(report_path)
    console.print(f"脱敏报告: {report_path}")

    legacy_flat = Path(output) / "mapping.json"
    if legacy_flat.exists():
        console.print(
            f"[dim]提示: 检测到旧版平铺映射表 {legacy_flat}，可能属于历史批次，"
            "本轮已改为批次目录存放，不会覆盖该文件[/dim]"
        )

    # 打印摘要
    summary = pipeline.report.summary()
    console.print(f"\n[bold]处理摘要:[/bold]")
    console.print(f"  输入文件: {summary['total_input_files']}")
    console.print(f"  自动脱敏: {summary['auto_masked_count']} 项")
    console.print(f"  建议脱敏: {summary['suggested_count']} 项")
    console.print(f"  仅提示: {summary['hint_count']} 项")
    if confirm_engine and confirm_engine.learned:
        console.print(f"  新学词: {len(confirm_engine.learned)} 个")
    if path_dicts:
        console.print(f"  路径改名: {len(path_dicts)} 项")
    if skipped_unmasked_total:
        console.print(
            f"  [yellow]未勾选（内容未脱敏）文件: {skipped_unmasked_total} 个，"
            "已隔离至镜像树内 skipped_unmasked/ 子目录，请勿作为脱敏产物分发[/yellow]"
        )
    console.print(f"  耗时: {elapsed:.2f}s")

    console.print(f"\n[green]批次目录: {batch_dir}[/green]")
    if len(dir_inputs) == 1 and dir_inputs:
        # R1-B4：根名脱敏后示例指向实际存在的树根（paths 段 rel_new=".").
        root_entry = next(
            (p for p in path_dicts if p.get("rel_new") == "."), None,
        )
        tree_name = (
            root_entry["new_name"] if root_entry else dir_inputs[0].name
        )
        restore_target = str(batch_dir / tree_name)
    else:
        restore_target = str(batch_dir)
    console.print("还原命令示例:")
    console.print(
        f'  mask-tool unmask "{restore_target}" --mapping "{mapping_path}" --output ./restored'
    )


def _mask_single_file(
    f: Path,
    batch_dir: Path,
    pipeline: Pipeline,
    confirm_engine,
    statuses: Optional[frozenset],
    mask_names: bool = True,
    path_statuses: Optional[frozenset] = None,
    irreversible: bool = False,
) -> List[dict]:
    """单文件模式：产物保持 {stem}_masked 后缀。

    R1-B3：mask_names 开启时（默认）对产物主名同步脱敏（先内容后名字，
    token 与正文共享；改名记录以 paths 段写回 mapping，unmask 可还原）。
    未命中敏感词时主名不变，行为与旧版一致。

    Returns:
        本文件产生的路径改名记录（无则空列表）
    """
    console.print(f"  处理: {f.name} ...", end=" ")
    try:
        allowed: Optional[Set[str]] = None
        file_statuses = statuses
        if confirm_engine is not None:
            allowed = _confirm_file_detections(f, pipeline, confirm_engine)
            if allowed is None or not allowed:
                console.print("[dim]跳过（无确认项）[/dim]")
                return []
            file_statuses = frozenset(
                {DetectionStatus.AUTO_MASK, DetectionStatus.SUGGEST_MASK}
            )
        result_path = pipeline.process_file(
            f, batch_dir,
            allowed_originals=allowed,
            statuses=file_statuses,
        )
        console.print("[green]✓[/green]")
    except Exception as e:
        console.print(f"[red]✗ {e}[/red]")
        return []
    if result_path is None or not mask_names:
        return []
    return _mask_output_file_name(
        result_path, pipeline, allowed, path_statuses, irreversible,
    )


def _mask_output_file_name(
    result_path: Path,
    pipeline: Pipeline,
    allowed_originals: Optional[Set[str]],
    path_statuses: Optional[frozenset],
    irreversible: bool = False,
) -> List[dict]:
    """对已脱敏内容的产物做主名脱敏（单文件模式，R1-B3）。

    与 Web 的 mask_filename 路径同源；改名记录导出为 paths 段 dict 列表。
    R3-A4：irreversible 与目录模式/Web 一致传入——不可逆模式下文件名
    用 ``***``（落盘 sanitize 为 ``___``）而非可逆 token。
    """
    from mask_tool.core.path_masker import PathMasker

    pm = PathMasker(
        pipeline.detector, pipeline.policy, pipeline.token_gen,
        irreversible=irreversible,
    )
    new_path = pm.mask_filename(
        result_path,
        allowed_originals=allowed_originals,
        statuses=path_statuses or frozenset({DetectionStatus.AUTO_MASK}),
    )
    for w in pm.last_result.warnings:
        console.print(f"  [yellow]{w}[/yellow]")
    if not pm.last_result.renamed:
        return []
    return pm.export_mappings()


def _confirm_file_detections(
    f: Path,
    pipeline: Pipeline,
    confirm_engine,
) -> Optional[Set[str]]:
    """--confirm 流程（H6/N3 重构，R1-A1 检测面=处理面）：adapter 同源
    全量检测（walker 全部件 + xlsx 全部件含数字合成项）-> 勾选 ->
    返回勾选原文集。

    返回 None 表示检测失败；空集合表示无确认项。
    """
    from mask_tool.adapters.extract import detect_file_results

    console.print(f"[bold]  文件: {f.name}[/bold]")
    try:
        all_results = detect_file_results(f, pipeline.detector, pipeline.policy)
    except Exception as e:
        console.print(f"    [red]文本提取失败: {e}[/red]\n")
        return None
    if not all_results:
        console.print("    [dim]未检测到敏感信息，跳过[/dim]\n")
        return set()
    confirmed = confirm_engine.confirm_batch(all_results, f.name)
    console.print()
    return {r.text for r in confirmed}


def _mask_directory_tree(
    d: Path,
    batch_dir: Path,
    pipeline: Pipeline,
    confirm_engine,
    statuses: Optional[frozenset],
    mask_names: bool,
    irreversible: bool,
    path_statuses: Optional[frozenset] = None,
    tree_excludes: Optional[List[Path]] = None,
) -> Tuple[List[dict], int]:
    """目录模式（D3 集成）：镜像树构建 + 文件名/目录名脱敏。

    流程：docx/xlsx -> process_file（output_name=原文件名，不加 _masked）；
    非文档文件原样拷贝；pptx/pdf 已在上游警告跳过；
    --confirm 下未勾选（无确认项）的文件不再混入产物树，拷入
    skipped_unmasked/ 隔离子目录（R1-B9，mask_tree 计划对其豁免改名）；
    空目录（含仅含空目录的链）一并迁移，目录名随 mask_tree 计划脱敏（R1-B7）；
    最后 PathMasker.mask_tree 对镜像树自底向上改名（默认开启，statuses 与
    内容处置对齐，R1-B2）。

    Returns:
        (路径改名映射的 dict 列表, 未勾选隔离文件数)
    """
    from mask_tool.core.path_masker import SKIPPED_UNMASKED_DIR

    console.print(f"[bold]目录: {d}[/bold]")
    mirror_root = batch_dir / d.name
    mirror_root.mkdir(parents=True, exist_ok=True)

    # R1-B1：输出子树不参与源遍历——镜像树创建后源目录内已含产物，
    # 不排除会自嵌套吸入历史批次
    batch_exclude = [Path(e).resolve() for e in (tree_excludes or [batch_dir])]

    skipped_dir = mirror_root / SKIPPED_UNMASKED_DIR
    skipped_count = 0
    allowed_union: Set[str] = set()

    for f in _iter_tree_files(d, exclude=batch_exclude):
        rel = f.relative_to(d)
        suffix = f.suffix.lower()
        target_dir = mirror_root / rel.parent
        if suffix in SUPPORTED_MASK_EXTS:
            console.print(f"  处理: {rel.as_posix()} ...", end=" ")
            try:
                if confirm_engine is not None:
                    allowed = _confirm_file_detections(f, pipeline, confirm_engine)
                    if allowed is None or not allowed:
                        # R1-B9：未勾选任何项——内容未脱敏，拷入隔离子目录
                        # （保持原相对结构与原名，mask_tree 豁免改名），
                        # 避免产物树出现"名已脱敏、内容未脱敏"的误导文件
                        dest = skipped_dir / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(f, dest)
                        skipped_count += 1
                        console.print(
                            f"[dim]跳过（无确认项，已隔离至 {SKIPPED_UNMASKED_DIR}/{rel.as_posix()}）[/dim]"
                        )
                        continue
                    allowed_union |= allowed
                    pipeline.process_file(
                        f, target_dir, output_name=f.name,
                        allowed_originals=allowed,
                        statuses=frozenset({DetectionStatus.AUTO_MASK, DetectionStatus.SUGGEST_MASK}),
                    )
                else:
                    pipeline.process_file(f, target_dir, output_name=f.name, statuses=statuses)
                console.print("[green]✓[/green]")
            except Exception as e:
                console.print(f"[red]✗ {e}[/red]")
        elif suffix in BLOCKED_EXTS:
            continue  # 上游已警告
        else:
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target_dir / f.name)

    # R1-B7：空目录（含仅含空目录的链）迁移到镜像树，目录名脱敏交给
    # mask_tree 计划统一处理（剪枝输出子树，B1；防产物目录被镜像）
    for dirpath, dirnames, _filenames in os.walk(d):
        dp = Path(dirpath)
        dirnames[:] = [
            n for n in dirnames
            if not any(_is_under((dp / n).resolve(), ex) for ex in batch_exclude)
        ]
        for name in dirnames:
            rel = (dp / name).relative_to(d)
            (mirror_root / rel).mkdir(parents=True, exist_ok=True)

    if not mask_names:
        console.print("  [dim]文件名/目录名脱敏已关闭（--no-mask-names）[/dim]")
        return [], skipped_count

    from mask_tool.core.path_masker import PathMasker

    path_masker = PathMasker(
        pipeline.detector, pipeline.policy, pipeline.token_gen,
        irreversible=irreversible,
    )
    tree_statuses = path_statuses or frozenset({DetectionStatus.AUTO_MASK})
    allowed_arg: Optional[Set[str]] = (
        allowed_union if confirm_engine is not None else None
    )
    result = path_masker.mask_tree(
        mirror_root, allowed_originals=allowed_arg, statuses=tree_statuses,
    )
    for pm in result.renamed:
        console.print(f"  改名: {pm.rel_old} -> {pm.rel_new}")
    for w in result.warnings:
        console.print(f"  [yellow]{w}[/yellow]")
    dicts = path_masker.export_mappings()
    # mask_tree 按设计不改 root 自身；镜像树根名（即输入目录名）单独脱敏，
    # 以 rel_new="." / depth=0 记录，与 unmask_tree 先子后父还原兼容
    root_entry = _mask_tree_root_name(
        path_masker, mirror_root, allowed_arg, tree_statuses,
    )
    if root_entry is not None:
        console.print(f"  改名: {root_entry['old_name']} -> {root_entry['new_name']}")
        dicts.append(root_entry)
    return dicts, skipped_count


def _mask_tree_root_name(
    path_masker, mirror_root: Path,
    allowed_originals: Optional[Set[str]] = None,
    statuses: Optional[frozenset] = None,
) -> Optional[dict]:
    """镜像树根目录自身名字的脱敏（mask_tree 不处理 root 自身）。

    失败（改名被占用等）时保原名并返回 None。
    """
    from mask_tool.core.path_masker import (
        STATUS_CONFLICT, STATUS_RENAMED, PathMapping, rename_with_retry,
        unique_name, win_long,
    )

    old_name = mirror_root.name
    new_name, detections = path_masker.build_new_name(
        old_name, context=str(mirror_root), kind="dir",
        allowed_originals=allowed_originals, statuses=statuses,
    )
    if new_name == old_name:
        return None
    parent = mirror_root.parent
    taken = {e.name.casefold() for e in parent.iterdir()}
    final = unique_name(parent, new_name, taken)
    actionable = statuses or frozenset({DetectionStatus.AUTO_MASK})
    tokens_used: List[str] = []
    categories: List[str] = []
    for r in detections:
        if r.status not in actionable:
            continue
        if r.text_type.value not in categories:
            categories.append(r.text_type.value)
        if not path_masker.irreversible and r.text in old_name:
            token = path_masker.token_gen.generate(r.text, r.text_type)
            if token not in tokens_used:
                tokens_used.append(token)
    try:
        rename_with_retry(win_long(mirror_root), win_long(parent / final))
    except OSError as e:
        console.print(f"  [yellow]根目录改名失败，保留原名: {mirror_root} ({e})[/yellow]")
        return None
    pm = PathMapping(
        kind="dir", old_name=old_name, new_name=final,
        rel_old=old_name, rel_new=".", depth=0,
        categories=categories, tokens_used=tokens_used,
        fingerprint="",
        status=STATUS_CONFLICT if final != new_name else STATUS_RENAMED,
    )
    pm.fingerprint = pm.make_fingerprint()
    return pm.to_dict()


# ---------------------------------------------------------------------------
# unmask 命令
# ---------------------------------------------------------------------------

@app.command()
def unmask(
    input_path: List[Path] = typer.Argument(..., help="脱敏后的文件或目录路径", exists=True),
    mapping: Path = typer.Option(..., "--mapping", help="映射表文件路径(JSON)", exists=True),
    output: Path = typer.Option("./restored", "--output", "-o", help="输出目录"),
    force: bool = typer.Option(False, "--force", help="忽略对账报错（token 残留/指纹不匹配时不再中断）"),
    verify: bool = typer.Option(False, "--verify", help="复算映射表指纹并比对（发现手工改动/跨批次混用）"),
) -> None:
    """反脱敏：根据映射表还原原文（内容 + 文件名/目录名）。

    对账防线（M3）：还原后扫描输出全文的 token 残留——
    残留 ∈ 本次映射表 = 还原缺陷，报错（--force 可忽略）；
    残留 ∉ 映射表 = 文档自然含 token 样式文本或跨批次文件，警告不中断。
    """
    console.print(f"[bold green]mask-tool[/bold green] v{__version__}")

    with open(mapping, "r", encoding="utf-8") as f:
        data = json.load(f)

    tokens: Dict[str, dict] = data.get("tokens", {})
    if not tokens and not data.get("paths"):
        console.print("[yellow]映射表为空[/yellow]")
        raise typer.Exit(1)
    console.print(f"加载了 {len(tokens)} 条映射")

    from mask_tool.core.path_masker import PathMasker

    paths = PathMasker.load_paths(data)

    # --verify：指纹复算比对（M3 防线三）
    if verify:
        mismatched, legacy = _verify_fingerprints(tokens)
        for token in mismatched:
            console.print(
                f"[red]指纹不匹配: {token} -> {tokens[token].get('original', '')!r} "
                f"(登记 {tokens[token].get('fingerprint', '')}，"
                f"复算 {fingerprint_of(str(tokens[token].get('original', '')))})"
                f"，映射表可能被手工改动或跨批次混用[/red]"
            )
        for token in legacy:
            console.print(f"[yellow]旧格式映射无指纹，跳过校验: {token}[/yellow]")
        if mismatched and not force:
            raise typer.Exit(1)

    # 输入分流
    files: List[Path] = []
    dirs: List[Path] = []
    for p in input_path:
        if p.is_dir():
            dirs.append(p)
        elif p.suffix.lower() in SUPPORTED_MASK_EXTS:
            files.append(p)
        elif p.suffix.lower() in BLOCKED_EXTS:
            _warn_blocked(p)
        else:
            console.print(f"[yellow]警告: 不支持的文件格式 {p.suffix}，已跳过: {p}[/yellow]")

    if not files and not dirs:
        console.print("[yellow]未找到可处理的文件[/yellow]")
        raise typer.Exit(1)

    # R1-B8：与 metadata.input_files 比对（尽力而为，不阻断）——完全无交集
    # 时提示可能拿错了别的批次的映射表（还原会静默产出错误原文）
    _warn_if_mapping_inputs_mismatch(data, files, dirs)

    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    restored_docs: List[Path] = []
    token_map = dict(tokens)

    for f in files:
        console.print(f"  还原: {f.name} ...", end=" ")
        try:
            out_name = _restored_file_name(f, paths)
            out_path = _unique_path(output / out_name)
            _restore_file_content(f, out_path, token_map)
            restored_docs.append(out_path)
            console.print("[green]✓[/green]")
        except Exception as e:
            console.print(f"[red]✗ {e}[/red]")

    for d in dirs:
        console.print(f"[bold]目录: {d}[/bold]")
        tree_out = _unique_path(output / d.name)
        shutil.copytree(d, tree_out)
        for f in _iter_tree_files(tree_out):
            rel = f.relative_to(tree_out)
            if f.suffix.lower() in SUPPORTED_MASK_EXTS:
                console.print(f"  还原: {rel.as_posix()} ...", end=" ")
                try:
                    _restore_file_content(f, f, token_map)  # 原地还原
                    restored_docs.append(f)
                    console.print("[green]✓[/green]")
                except Exception as e:
                    console.print(f"[red]✗ {e}[/red]")
        # 文件名/目录名还原：先子后父
        if paths:
            dummy = PathMasker(None, None, None)
            result = PathMasker.unmask_tree(dummy, tree_out, paths)
            for pm in result.restored:
                console.print(f"  还原名: {pm.rel_new} -> {pm.rel_old}")
            for m in result.missing:
                console.print(f"  [yellow]还原目标不存在（可能已被改动/删除）: {m.rel_new}[/yellow]")
            for extra in result.extra:
                console.print(f"  [yellow]目录树中存在未覆盖的改名残留: {extra}[/yellow]")
            # R3-C10：还原名被占用加后缀等警告透出（不再静默改名）
            for w in result.warnings:
                console.print(f"  [yellow]{w}[/yellow]")

    # M3 防线二：还原后对账扫描
    bad_residual: List[Tuple[Path, str]] = []
    for doc in restored_docs:
        try:
            residual = ReplacementEngine.scan_tokens(_extract_text(doc))
        except Exception:
            continue
        for token in sorted(residual):
            if token in token_map:
                bad_residual.append((doc, token))
            else:
                console.print(
                    f"[yellow]警告: {doc.name} 含 token 样式文本 {token}，"
                    "不在本次映射表中（文档自然文本或跨批次文件），未做还原[/yellow]"
                )
    if bad_residual:
        for doc, token in bad_residual:
            console.print(
                f"[red]错误: {doc.name} 还原后仍残留映射表内 token {token}（还原缺陷）[/red]"
            )
        if not force:
            raise typer.Exit(1)

    console.print(f"\n[green]反脱敏完成，输出到: {output}/[/green]")


def _warn_if_mapping_inputs_mismatch(
    data: dict, files: List[Path], dirs: List[Path],
) -> None:
    """R1-B8：映射表登记的源文件清单与实际输入比对。

    完全无交集时打黄色警告，不阻断。名字脱敏/加 _masked 后缀的产物名
    与源名天然不同，比对前做归一化：
    - 剥产物名的 ``_masked`` 后缀；
    - paths 段记录了改名的，沿 new_name -> old_name 链回溯。
    只捕获"整批完全不相干"的高置信错配场景，避免高频误报。
    """
    from mask_tool.core.path_masker import PathMasker

    meta_inputs = data.get("metadata", {}).get("input_files", [])
    if not meta_inputs:
        return  # 旧版 mapping 无此字段
    meta_names = set()
    for m in meta_inputs:
        try:
            meta_names.add(Path(m).name)
        except (TypeError, ValueError):
            continue
    if not meta_names:
        return
    rename_map = {}
    for pm in PathMasker.load_paths(data):
        if pm.new_name:
            rename_map[pm.new_name] = pm.old_name

    def _strip_masked(name: str) -> str:
        stem, dot, suffix = name.rpartition(".")
        if dot and stem.endswith("_masked"):
            return stem[: -len("_masked")] + "." + suffix
        return name

    def _source_candidates(name: str) -> set:
        cands = {name}
        cur = name
        for _ in range(3):  # 文件名 + 根名两层改名链足够
            if cur in rename_map:
                cur = rename_map[cur]
                cands.add(cur)
            else:
                break
        return cands | {_strip_masked(c) for c in cands}

    actual_names = {p.name for p in files} | {d.name for d in dirs}
    for d in dirs:
        for f in _iter_tree_files(d):
            actual_names.add(f.name)
    for name in actual_names:
        if _source_candidates(name) & meta_names:
            return
    console.print(
        f"[yellow]警告: 实际输入文件与映射表登记的源文件清单（"
        f"{len(meta_inputs)} 个）无任何交集，可能混用了其他批次的映射表，"
        "还原结果将不正确；请核对 mapping.json 的批次信息（--verify 可复算指纹）[/yellow]"
    )


def _verify_fingerprints(tokens: Dict[str, dict]) -> Tuple[List[str], List[str]]:
    """复算指纹。返回 (不匹配列表, 旧格式无指纹列表)。"""
    mismatched: List[str] = []
    legacy: List[str] = []
    for token, info in tokens.items():
        fp = info.get("fingerprint", "")
        if not fp:
            legacy.append(token)
            continue
        original = str(info.get("original", ""))
        if fingerprint_of(original) != fp:
            mismatched.append(token)
    return mismatched, legacy


def _restored_file_name(f: Path, paths) -> str:
    """单文件还原名：映射表 paths 段能唯一定位该文件时用原名，否则保持现名。"""
    candidates = [
        m for m in paths
        if m.kind == "file" and PurePosixPath(m.rel_new).name == f.name
        and m.status in ("renamed", "conflict_suffixed")
    ]
    if len(candidates) == 1:
        return candidates[0].old_name
    return f.name


def _unique_path(path: Path) -> Path:
    """输出路径防覆盖：存在时追加 _1/_2 序号。"""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 1
    while (path.parent / f"{stem}_{n}{suffix}").exists():
        n += 1
    return path.parent / f"{stem}_{n}{suffix}"


def _restore_file_content(f: Path, output_path: Path, token_map: Dict[str, dict]) -> None:
    """单文件内容还原（R1-A2：委托公共实现 adapters/restore.py，CLI/Web 共用）。

    docx 走 walker（页眉/脚注/批注等全覆盖）；xlsx 走 restore_value
    （kind=number 的整值 token 还原为数值；rich_text=True 保内联格式）。
    """
    from mask_tool.adapters.restore import restore_file_content

    restore_file_content(f, output_path, token_map)


# ---------------------------------------------------------------------------
# inspect 命令
# ---------------------------------------------------------------------------

@app.command()
def inspect(
    input_path: List[Path] = typer.Argument(..., help="输入文件或目录路径", exists=True),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="配置文件路径"),
    mode: str = typer.Option("smart", "--mode", "-m", help="运行模式: focused/strict/smart/aggressive"),
) -> None:
    """检测文件中的敏感信息（不执行脱敏）。

    提取与 mask 同源（docx walker / xlsx 全部件），无表格/页眉脚/脚注盲区（N1）；
    目录输入额外检测文件名/目录名（source=path）。
    """
    console.print(f"[bold green]mask-tool[/bold green] v{__version__}")
    console.print(f"模式: {mode}")

    cfg = _load_config(config, mode)
    pipeline = Pipeline(
        cfg, lexicon_path=cfg.lexicon_path, whitelist_path=cfg.whitelist_path,
    )

    files: List[Path] = []
    dirs: List[Path] = []
    for p in input_path:
        if p.is_dir():
            dirs.append(p)
        else:
            files.extend(_collect_files(p))
    if not files and not dirs:
        console.print("[yellow]未找到可处理的文件[/yellow]")
        raise typer.Exit(1)

    all_results = []
    from mask_tool.adapters.extract import detect_file_results

    for f in files:
        console.print(f"  检测: {f.name} ...", end=" ")
        try:
            # R1-A1：与 adapter 处理面同源（含 xlsx 数字合成项）
            results = detect_file_results(f, pipeline.detector, pipeline.policy)
            all_results.extend(results)
            # 文件名检测（source=path）
            all_results.extend(_detect_path_items(pipeline, f))
            console.print(f"发现 {len(results)} 项")
        except Exception as e:
            console.print(f"[red]✗ {e}[/red]")

    for d in dirs:
        console.print(f"[bold]目录: {d}[/bold]")
        for f in _collect_files(d):
            console.print(f"  检测: {f.relative_to(d).as_posix()} ...", end=" ")
            try:
                # R1-A1：与 adapter 处理面同源（含 xlsx 数字合成项）
                results = detect_file_results(f, pipeline.detector, pipeline.policy)
                # 目录模式下文件自身与其祖先目录名都纳入检测
                results.extend(_detect_path_items(pipeline, f))
                for parent in f.relative_to(d).parents:
                    results.extend(_detect_path_items(pipeline, d / parent, kind="dir"))
                all_results.extend(results)
                console.print(f"发现 {len(results)} 项")
            except Exception as e:
                console.print(f"[red]✗ {e}[/red]")

    if all_results:
        # 按置信度降序排列
        all_results.sort(key=lambda r: r.confidence, reverse=True)
        _print_detection_table(all_results)

        auto = sum(1 for r in all_results if r.status == DetectionStatus.AUTO_MASK)
        suggest = sum(1 for r in all_results if r.status == DetectionStatus.SUGGEST_MASK)
        hint = sum(1 for r in all_results if r.status == DetectionStatus.HINT_ONLY)
        path_hits = sum(1 for r in all_results if r.source == "path")
        console.print(
            f"\n[bold]统计: 自动脱敏 {auto} | 建议脱敏 {suggest} | 仅提示 {hint}"
            + (f" | 文件/目录名 {path_hits}" if path_hits else "")
            + "[/bold]"
        )
    else:
        console.print("\n[green]未检测到敏感信息[/green]")


def _detect_path_items(pipeline: Pipeline, path: Path, kind: str = "file") -> list:
    """检测单个文件名（stem）或目录名，结果 source 标注为 "path"。"""
    from mask_tool.core.path_masker import PathMasker

    masker = PathMasker(pipeline.detector, pipeline.policy, pipeline.token_gen)
    name = path.stem if kind == "file" else path.name
    results = masker.detect_name(name, context=str(path))
    results = pipeline.policy.apply(results)
    return results


# ---------------------------------------------------------------------------
# 文本提取（N1：与 mask 同源）
# ---------------------------------------------------------------------------

def _extract_text(file_path: Path) -> str:
    """从文件中提取纯文本（R1-A1：委托 adapters/extract 公共实现，覆盖面与
    脱敏一致——docx walker 全部件；xlsx 单元格（纯文本/富文本/数字 canonical）/
    公式字面量/批注/超链接 tooltip/页眉脚/验证列表/定义名称）。

    用途：unmask 对账扫描与通用文本检测；确认模式的检测面入口
    detect_file_results（含 xlsx 数字合成项）在 adapters/extract.py。
    """
    from mask_tool.adapters.extract import extract_texts

    return extract_texts(file_path)


def _extract_xlsx_text(file_path: Path) -> str:
    """xlsx 全部件文本提取（R1-A1：委托 adapters/extract，检测面=处理面）。"""
    from mask_tool.adapters.extract import extract_xlsx_texts

    return "\n".join(extract_xlsx_texts(file_path))


# ---------------------------------------------------------------------------
# 其他
# ---------------------------------------------------------------------------

def _save_learned_words(
    learned: dict,
    config: MaskConfig,
) -> None:
    """将学习到的词追加到词库文件"""
    import yaml

    lexicon_path = Path(config.lexicon_path)
    if not lexicon_path.exists():
        console.print("  [yellow]词库文件不存在，跳过学习写入[/yellow]")
        return

    # 加载现有词库
    with open(lexicon_path, "r", encoding="utf-8") as f:
        existing = yaml.safe_load(f) or {}

    # 合并新词
    new_count = 0
    for category, words in learned.items():
        if category not in existing:
            existing[category] = []
        for word in words:
            if word not in existing[category]:
                existing[category].append(word)
                new_count += 1

    if new_count > 0:
        with open(lexicon_path, "w", encoding="utf-8") as f:
            yaml.dump(existing, f, allow_unicode=True, default_flow_style=False)
        console.print(f"  [blue]✓ {new_count} 个新词已写入词库: {lexicon_path}[/blue]")
    else:
        console.print("  [dim]所有词已存在于词库中，无需更新[/dim]")


@app.command("config")
def config_init(
    output: Path = typer.Option(".", "--output", "-o", help="输出目录"),
) -> None:
    """生成默认配置文件和示例词库（内嵌模板，不依赖源码目录）"""
    console.print(f"[bold green]mask-tool[/bold green] v{__version__}")

    from mask_tool.core.templates import (
        DEFAULT_CONFIG_YAML, SAMPLE_LEXICON_YAML, WHITELIST_YAML,
    )

    target_dir = Path(output) / "config"
    target_dir.mkdir(parents=True, exist_ok=True)

    templates = {
        "default.yaml": DEFAULT_CONFIG_YAML,
        "sample_lexicon.yaml": SAMPLE_LEXICON_YAML,
        "whitelist.yaml": WHITELIST_YAML,
    }
    for name, content in templates.items():
        target = target_dir / name
        if target.exists():
            console.print(f"  跳过(已存在): {target}")
            continue
        target.write_text(content, encoding="utf-8")
        console.print(f"  创建: {target}")

    console.print(f"\n[green]配置文件已生成到 {target_dir}/[/green]")
    console.print("编辑 config/default.yaml 自定义配置")
    console.print("复制 config/sample_lexicon.yaml 为 config/lexicon.yaml 并维护你自己的词库")


@app.command()
def version() -> None:
    """显示版本信息"""
    console.print(f"mask-tool v{__version__}")


@app.command("app")
def launch_app() -> None:
    """启动桌面应用窗口（pywebview 原生窗口，无需浏览器）

    与 mask-tool-desktop 控制台命令、start-windows.bat 双击启动等价；
    浏览器 Web 入口已下线，UI 仅在桌面窗口内渲染。
    """
    console.print(f"[bold green]mask-tool[/bold green] v{__version__} 正在打开桌面窗口…")
    try:
        from mask_tool.desktop import main as desktop_main
    except ImportError as e:
        console.print(
            f"[red]桌面组件未安装（{e}）。请执行：[/red]"
            '[red]pip install -e ".[app]"[/red]'
        )
        raise typer.Exit(1)
    desktop_main()
