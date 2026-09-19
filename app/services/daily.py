"""每日冷知识：按 Asia/Shanghai 日期 + 固定种子稳定返回。"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

from app.fixtures.dataset import load_fixtures


def fact_index(day: date, total: int) -> int:
    if total <= 0:
        return 0
    digest = hashlib.sha256(f"fact:{day.isoformat()}".encode("utf-8")).hexdigest()
    return int(digest, 16) % total


def fact_for(day: date) -> dict[str, Any]:
    rows = load_fixtures().facts
    row = rows[fact_index(day, len(rows))]
    return {
        "id": row["id"],
        "date": day,
        "content": row["content"],
        "tags": list(row["tags"]),
        "source_url": row["source_url"],
    }
