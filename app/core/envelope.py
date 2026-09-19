"""统一响应信封 {data, meta, error}。"""

from __future__ import annotations

from typing import Any

from app.config import MOCK_CONTENT_VERSION
from app.core.context import get_request_id

SOURCE_MOCK = "mock"


def build_meta(
    *,
    next_cursor: str | None = None,
    has_more: bool = False,
    source: str = SOURCE_MOCK,
    version: str = MOCK_CONTENT_VERSION,
) -> dict[str, Any]:
    return {
        "request_id": get_request_id(),
        "next_cursor": next_cursor,
        "has_more": has_more,
        "source": source,
        "version": version,
    }


def ok(
    data: Any,
    *,
    next_cursor: str | None = None,
    has_more: bool = False,
    source: str = SOURCE_MOCK,
    version: str = MOCK_CONTENT_VERSION,
) -> dict[str, Any]:
    return {
        "data": data,
        "meta": build_meta(next_cursor=next_cursor, has_more=has_more, source=source, version=version),
        "error": None,
    }


def fail(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    *,
    version: str = MOCK_CONTENT_VERSION,
) -> dict[str, Any]:
    return {
        "data": None,
        "meta": build_meta(version=version),
        "error": {"code": code, "message": message, "details": details or {}},
    }
