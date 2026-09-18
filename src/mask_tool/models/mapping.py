"""Token映射数据模型"""

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Optional

from .detection import DetectionType


def fingerprint_of(original: str) -> str:
    """计算原文指纹：sha256(original)[:8]，用于发现 mapping 被手工改动或跨批次混用"""
    return sha256(original.encode("utf-8")).hexdigest()[:8]


@dataclass
class TokenMapping:
    """Token与原文的映射关系"""
    token: str                        # "[COMPANY_001]"
    original: str                     # "某某建设集团有限公司"
    text_type: DetectionType          # 类别
    confidence: float                 # 置信度
    created_at: str = ""              # ISO时间戳
    batch_id: str = ""                # 批次标识（M3/M4：写入 mapping，便于对账）
    fingerprint: str = ""             # 原文指纹 sha256(original)[:8]，空时自动计算
    kind: str = "text"                # 原值类型: "text"（默认，兼容旧数据）| "number"（xlsx数值单元格）

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()
        if not self.fingerprint:
            self.fingerprint = fingerprint_of(self.original)

    def to_dict(self) -> dict:
        return {
            "token": self.token,
            "original": self.original,
            "type": self.text_type.value,
            "confidence": self.confidence,
            "created_at": self.created_at,
            "batch_id": self.batch_id,
            "fingerprint": self.fingerprint,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TokenMapping":
        return cls(
            token=data["token"],
            original=data["original"],
            text_type=DetectionType(data["type"]),
            confidence=data["confidence"],
            created_at=data.get("created_at", ""),
            batch_id=data.get("batch_id", ""),
            fingerprint=data.get("fingerprint", ""),
            kind=data.get("kind", "text"),
        )
