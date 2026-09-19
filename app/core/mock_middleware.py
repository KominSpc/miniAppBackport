"""MOCK_EMPTY 开关：把所有列表类响应清空。

做成纯 ASGI 中间件而不是在每个路由里判断，是为了保证「所有列表接口」行为一致，
将来新增列表接口也不会漏。开关关闭时完全透传，零额外开销。
"""

from __future__ import annotations

import json
from typing import Any

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings, mock_flags_for

JSON_CONTENT_TYPE = "application/json"


def rewrite_empty(payload: bytes) -> bytes:
    """把 data.items 置空，并同步 meta 的分页字段。"""
    try:
        document: Any = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return payload
    if not isinstance(document, dict):
        return payload
    data = document.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return payload
    data["items"] = []
    meta = document.get("meta")
    if isinstance(meta, dict):
        meta["next_cursor"] = None
        meta["has_more"] = False
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


class EmptyResultMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        flags = mock_flags_for(Request(scope), self.settings)
        if not flags.empty:
            await self.app(scope, receive, send)
            return

        start_message: Message | None = None
        chunks: list[bytes] = []

        async def send_wrapper(message: Message) -> None:
            nonlocal start_message
            if message["type"] == "http.response.start":
                start_message = message
                return
            if message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                if message.get("more_body"):
                    return
                assert start_message is not None
                headers = Headers(raw=start_message["headers"])
                body = b"".join(chunks)
                if JSON_CONTENT_TYPE in headers.get("content-type", ""):
                    body = rewrite_empty(body)
                    raw = [
                        (key, value)
                        for key, value in start_message["headers"]
                        if key.lower() != b"content-length"
                    ]
                    raw.append((b"content-length", str(len(body)).encode("ascii")))
                    start_message["headers"] = raw
                await send(start_message)
                await send({"type": "http.response.body", "body": body, "more_body": False})
                return
            await send(message)

        await self.app(scope, receive, send_wrapper)
