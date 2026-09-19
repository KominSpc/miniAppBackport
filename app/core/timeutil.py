"""时区与时间工具。

契约固定使用 Asia/Shanghai（+08:00），所有对外时间戳都必须带偏移量。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
TIMEZONE_NAME = "Asia/Shanghai"


def now() -> datetime:
    """当前时间（Asia/Shanghai）。"""
    return datetime.now(SHANGHAI).replace(microsecond=0)


def today() -> date:
    return now().date()


def iso(moment: datetime) -> str:
    """格式化为带偏移的 ISO 8601 字符串。"""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=SHANGHAI)
    return moment.astimezone(SHANGHAI).isoformat(timespec="seconds")


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def hours_ago(hours: float) -> datetime:
    return now() - timedelta(hours=hours)

