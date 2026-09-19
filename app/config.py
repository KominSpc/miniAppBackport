"""服务配置与模拟开关。

所有模拟开关都可以由环境变量或请求头控制，`GET /health` 会回显当前生效值。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace

from starlette.requests import Request

MOCK_CONTENT_VERSION = "mock-2026.09"
SERVICE_VERSION = "1.0.0-mock.1"
MAX_CHAT_MESSAGE_LENGTH = 500
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50

_TRUTHY = {"1", "true", "yes", "on"}

# 模拟开关对应的请求头，便于单个请求临时切换
HEADER_LATENCY = "x-mock-latency-ms"
HEADER_EMPTY = "x-mock-empty"
HEADER_RATE_LIMITED = "x-mock-rate-limited"
HEADER_ERROR = "x-mock-error"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _header_bool(request: Request, name: str) -> bool | None:
    raw = request.headers.get(name)
    if raw is None:
        return None
    return raw.strip().lower() in _TRUTHY


@dataclass(frozen=True)
class Settings:
    service_version: str = SERVICE_VERSION
    content_version: str = MOCK_CONTENT_VERSION
    latency_ms: int = 0
    empty: bool = False
    rate_limited: bool = False
    error: bool = False
    dev_reset_enabled: bool = False
    admin_token: str = "dev-admin-token"
    token_ttl_hours: int = 720
    rate_limit_per_minute: int = 600
    public_base_url: str = ""
    cors_extra_origins: tuple[str, ...] = ()
    pixiv_cookie: str = ""

    @property
    def page_default(self) -> int:
        return DEFAULT_PAGE_SIZE

    @property
    def page_max(self) -> int:
        return MAX_PAGE_SIZE


@dataclass(frozen=True)
class MockFlags:
    latency_ms: int = 0
    empty: bool = False
    rate_limited: bool = False
    error: bool = False
    dev_reset_enabled: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "latency_ms": self.latency_ms,
            "empty": self.empty,
            "rate_limited": self.rate_limited,
            "error": self.error,
            "dev_reset_enabled": self.dev_reset_enabled,
        }


def load_settings() -> Settings:
    extra = tuple(
        origin.strip()
        for origin in os.getenv("CORS_EXTRA_ORIGINS", "").split(",")
        if origin.strip()
    )
    return Settings(
        latency_ms=_env_int("MOCK_LATENCY_MS", 0),
        empty=_env_bool("MOCK_EMPTY"),
        rate_limited=_env_bool("MOCK_RATE_LIMITED"),
        error=_env_bool("MOCK_ERROR"),
        dev_reset_enabled=_env_bool("DEV_RESET_ENABLED"),
        admin_token=os.getenv("DEV_ADMIN_TOKEN", "dev-admin-token"),
        token_ttl_hours=_env_int("TOKEN_TTL_HOURS", 720),
        rate_limit_per_minute=_env_int("RATE_LIMIT_PER_MINUTE", 600),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "").rstrip("/"),
        cors_extra_origins=extra,
        pixiv_cookie=os.getenv("PIXIV_COOKIE", ""),
    )


def mock_flags_for(request: Request, settings: Settings) -> MockFlags:
    """环境变量为底，请求头可按单次请求覆盖。"""
    flags = MockFlags(
        latency_ms=settings.latency_ms,
        empty=settings.empty,
        rate_limited=settings.rate_limited,
        error=settings.error,
        dev_reset_enabled=settings.dev_reset_enabled,
    )
    raw_latency = request.headers.get(HEADER_LATENCY)
    if raw_latency is not None:
        try:
            flags = replace(flags, latency_ms=max(0, int(raw_latency.strip())))
        except ValueError:
            pass
    for header, field in (
        (HEADER_EMPTY, "empty"),
        (HEADER_RATE_LIMITED, "rate_limited"),
        (HEADER_ERROR, "error"),
    ):
        override = _header_bool(request, header)
        if override is not None:
            flags = replace(flags, **{field: override})
    return flags


def base_url_for(request: Request, settings: Settings) -> str:
    """内容链接的根地址。

    默认取自当前请求，使本机 Edge 调试与局域网真机联调都能拿到可达地址；
    需要固定地址时用 PUBLIC_BASE_URL 覆盖。
    """
    if settings.public_base_url:
        return settings.public_base_url
    return str(request.base_url).rstrip("/")

