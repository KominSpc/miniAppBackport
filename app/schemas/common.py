"""契约公共模型：枚举、元信息、错误与响应信封。

命名与 contract/openapi.json 的 components.schemas 一一对应，
导入后导出的 OpenAPI 必须与契约快照结构一致（见 tests/test_contract_sync.py）。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class Source(str, Enum):
    mock = "mock"
    pixiv = "pixiv"
    bilibili = "bilibili"
    game = "game"
    music = "music"
    comic = "comic"


class ContentType(str, Enum):
    image = "image"
    video = "video"
    game = "game"
    card = "card"


class ErrorCode(str, Enum):
    BAD_REQUEST = "BAD_REQUEST"
    UNAUTHORIZED = "UNAUTHORIZED"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"


class Meta(BaseModel):
    request_id: str
    next_cursor: str | None = Field(default=None, description="下一页游标；无下一页时为 null")
    has_more: bool
    source: Source
    version: str


class ErrorInfo(BaseModel):
    code: ErrorCode
    message: str = Field(description="可直接展示的中文文案，客户端不做二次拼接")
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    data: None
    meta: Meta
    error: ErrorInfo


class Envelope(BaseModel, Generic[T]):
    data: T
    meta: Meta
    error: None



