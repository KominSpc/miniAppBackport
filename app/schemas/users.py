"""匿名用户、偏好与历史。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.content import ContentItem

Platform = Literal["android", "ios", "web", "desktop"]
HistoryKind = Literal["browse", "search"]


class AnonymousUserRequest(BaseModel):
    install_id: str | None = Field(default=None, description="客户端本地随机 UUID v4，不使用可追踪标识")
    platform: Platform | None = None
    app_version: str | None = None


class UserPreferences(BaseModel):
    tags: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    genres: list[str] = Field(default_factory=list)
    safe_mode: bool = Field(default=True, description="敏感内容过滤开关，默认开启")


class AnonymousUser(BaseModel):
    user_id: str
    access_token: str
    token_type: Literal["Bearer"]
    expires_at: datetime
    preferences: UserPreferences


class HistoryEntry(BaseModel):
    id: str
    kind: HistoryKind
    query: str | None = None
    content: ContentItem | None = Field(default=None, description="kind=browse 时的内容条目")
    created_at: datetime


class HistoryPage(BaseModel):
    items: list[HistoryEntry]


class HistoryDeleteResult(BaseModel):
    deleted: int = Field(ge=0)
    kind: HistoryKind | None = None
