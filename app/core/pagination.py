"""不透明游标分页。

客户端只做透传，不得解析游标内容；格式非法返回 400 BAD_REQUEST。
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Sequence

from app.core.errors import bad_request

_CURSOR_PREFIX = "c1"


def encode_cursor(scope: str, offset: int) -> str:
    raw = json.dumps({"s": scope, "o": offset}, separators=(",", ":"), ensure_ascii=False)
    return _CURSOR_PREFIX + base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None, scope: str) -> int:
    """解析游标为偏移量；cursor 为空表示第一页。"""
    if not cursor:
        return 0
    if not cursor.startswith(_CURSOR_PREFIX):
        raise bad_request({"cursor": "格式非法"})
    payload = cursor[len(_CURSOR_PREFIX):]
    padded = payload + "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        obj = json.loads(raw.decode("utf-8"))
        if not isinstance(obj, dict) or obj.get("s") != scope:
            raise ValueError("scope mismatch")
        offset = int(obj["o"])
        if offset < 0:
            raise ValueError("negative offset")
    except (binascii.Error, UnicodeDecodeError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise bad_request({"cursor": "格式非法"}) from None
    return offset


def paginate(
    items: Sequence[Any],
    *,
    scope: str,
    cursor: str | None,
    limit: int,
) -> tuple[list[Any], str | None, bool]:
    """返回 (当页数据, next_cursor, has_more)。"""
    offset = decode_cursor(cursor, scope)
    page = list(items[offset : offset + limit])
    consumed = offset + len(page)
    has_more = consumed < len(items)
    next_cursor = encode_cursor(scope, consumed) if has_more else None
    return page, next_cursor, has_more
