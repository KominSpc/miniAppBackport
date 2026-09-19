"""路由公共片段：错误响应声明与分页封装。"""

from __future__ import annotations

from typing import Any

from app.core import envelope
from app.schemas.common import ErrorResponse

_RATE_LIMITED = {
    "model": ErrorResponse,
    "description": "429 限流，客户端应读取 Retry-After 后按指数退避重试",
    "headers": {
        "Retry-After": {
            "description": "建议等待秒数",
            "schema": {"type": "integer"},
        }
    },
}

_CATALOG: dict[int, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "400 参数错误，如游标格式非法"},
    401: {"model": ErrorResponse, "description": "401 未认证、令牌失效或过期"},
    403: {"model": ErrorResponse, "description": "403 无权限"},
    404: {"model": ErrorResponse, "description": "404 资源不存在"},
    409: {"model": ErrorResponse, "description": "409 状态冲突"},
    422: {"model": ErrorResponse, "description": "422 参数校验失败"},
    429: _RATE_LIMITED,
    500: {"model": ErrorResponse, "description": "500 服务内部错误"},
    503: {"model": ErrorResponse, "description": "503 上游内容源不可用"},
}


def error_responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    return {code: _CATALOG[code] for code in codes}


def page_envelope(
    items: list[Any],
    *,
    next_cursor: str | None,
    has_more: bool,
) -> dict[str, Any]:
    return envelope.ok({"items": items}, next_cursor=next_cursor, has_more=has_more)


def list_envelope(items: list[Any]) -> dict[str, Any]:
    return envelope.ok({"items": items})


def item_envelope(item: Any) -> dict[str, Any]:
    return envelope.ok(item)
