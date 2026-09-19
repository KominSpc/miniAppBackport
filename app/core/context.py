"""请求级上下文：request_id 贯穿日志、埋点与响应信封。"""

from __future__ import annotations

import secrets
from contextvars import ContextVar

_FALLBACK = "req_00000000"

_request_id: ContextVar[str] = ContextVar("request_id", default=_FALLBACK)


def new_request_id() -> str:
    """生成契约示例同规格的 request_id，形如 req_8f3c1a2b。"""
    return "req_" + secrets.token_hex(4)


def set_request_id(value: str) -> None:
    _request_id.set(value)


def get_request_id() -> str:
    return _request_id.get()
