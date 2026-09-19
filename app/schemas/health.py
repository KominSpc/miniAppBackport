"""健康检查与开发重置。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class MockSwitchState(BaseModel):
    latency_ms: int
    empty: bool
    rate_limited: bool
    error: bool
    dev_reset_enabled: bool


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    time: datetime
    timezone: str
    mock: MockSwitchState | None = Field(default=None, description="当前生效的模拟开关，便于联调时确认状态")


class ResetCounters(BaseModel):
    users: int
    favorites: int
    history: int
    conversations: int


class DevResetResult(BaseModel):
    reset_at: datetime
    cleared: ResetCounters
