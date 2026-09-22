"""FastAPI 依赖：配置、模拟开关、鉴权与分页参数。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Query, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import MockFlags, Settings, base_url_for, mock_flags_for
from app.core.errors import AppError, unauthorized
from app.core.timeutil import now
from app.repositories import store

# scheme_name 决定导出的 OpenAPI 里安全方案的名字，必须与契约一致
bearer_scheme = HTTPBearer(
    scheme_name="bearerAuth",
    bearerFormat="opaque",
    description=(
        "由 POST /v1/users/anonymous 签发的访问令牌。"
        "缺失或不合法返回 401 UNAUTHORIZED；过期返回 401 TOKEN_EXPIRED。"
    ),
    auto_error=False,
)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_flags(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> MockFlags:
    return mock_flags_for(request, settings)


def get_base_url(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> str:
    return base_url_for(request, settings)


def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer_scheme)],
) -> dict[str, Any]:
    """Bearer 鉴权：缺失或非法返回 401 UNAUTHORIZED，过期返回 401 TOKEN_EXPIRED。"""
    if credentials is None or not credentials.credentials:
        raise unauthorized()
    record = store.resolve_token(credentials.credentials)
    if record is None:
        raise unauthorized()
    if record["expires_at"] <= now():
        raise unauthorized("TOKEN_EXPIRED")
    return record


CurrentUser = Annotated[dict[str, Any], Depends(current_user)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
FlagsDep = Annotated[MockFlags, Depends(get_flags)]
BaseUrlDep = Annotated[str, Depends(get_base_url)]

CursorQuery = Annotated[
    str | None,
    Query(description="不透明游标，取自上一页 meta.next_cursor。格式非法返回 400 BAD_REQUEST。"),
]
LimitQuery = Annotated[
    int,
    Query(
        ge=1,
        le=50,
        description="每页条数，默认 20，最大 50；超出范围返回 422 VALIDATION_FAILED。",
    ),
]


def require_admin_token(settings: Settings, token: str | None) -> None:
    """开发重置的准入校验：开关未开 → 403，令牌不匹配 → 401。"""
    if not settings.dev_reset_enabled:
        raise AppError("FORBIDDEN", details={"hint": "DEV_RESET_ENABLED 未开启"})
    if token != settings.admin_token:
        raise unauthorized()

