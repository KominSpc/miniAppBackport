"""每日冷知识。"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class DailyFact(BaseModel):
    id: str
    date: date
    content: str
    tags: list[str]
    source_url: str | None = None
