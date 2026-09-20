# -*- coding: utf-8 -*-
"""批次历史：BatchRecord 与 ~/.mask-tool/history.json 读写（最近 N 条）。"""
import json
import random
import string
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import List

HISTORY_PATH = Path.home() / ".mask-tool" / "history.json"
MAX_HISTORY = 50
# ──────────────────────────────────────────────
# 批次管理
# ──────────────────────────────────────────────

@dataclass
class BatchRecord:
    batch_id: str
    batch_name: str
    created_at: str  # ISO格式
    file_count: int
    mask_count: int
    mapping_data: str  # 兼容字段：仅旧版记录中含完整映射 JSON；新记录恒为 ""（不再保存映射明文）
    mapping_path: str = ""  # 新版：批次目录 mapping.json 的完整路径（映射留存处）


def _generate_batch_id() -> str:
    """生成批次ID，格式：MSK-YYYYMMDD-HHMMSS-XXX"""
    now = datetime.now()
    date_part = now.strftime("%Y%m%d")
    time_part = now.strftime("%H%M%S")
    rand_part = "".join(random.choices(string.ascii_uppercase + string.digits, k=3))
    return f"MSK-{date_part}-{time_part}-{rand_part}"


def _load_history() -> List[BatchRecord]:
    """加载批次历史记录（容忍旧/新字段差异）"""
    if not HISTORY_PATH.exists():
        return []
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        valid_fields = {f_.name for f_ in BatchRecord.__dataclass_fields__.values()}
        records = []
        for item in data:
            filtered = {k: v for k, v in item.items() if k in valid_fields}
            records.append(BatchRecord(**filtered))
        return records
    except Exception:
        return []


def _save_history(records: List[BatchRecord]) -> None:
    """保存批次历史记录"""
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 只保留最近 MAX_HISTORY 条
    records = records[-MAX_HISTORY:]
    data = [asdict(r) for r in records]
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _add_history(record: BatchRecord) -> None:
    """添加一条历史记录"""
    records = _load_history()
    records.append(record)
    _save_history(records)

