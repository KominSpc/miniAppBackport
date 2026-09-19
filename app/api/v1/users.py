"""匿名用户、偏好与历史。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter

from app.api.v1.common import error_responses
from app.core import envelope
from app.core.pagination import paginate
from app.core.timeutil import now
from app.deps import CurrentUser, CursorQuery, FlagsDep, LimitQuery, SettingsDep
from app.repositories.memory import store
from app.schemas.envelopes import (
    EnvelopeAnonymousUser,
    EnvelopeHistoryDelete,
    EnvelopeHistoryPage,
    EnvelopeUserPreferences,
)
from app.schemas.users import (
    AnonymousUser,
    AnonymousUserRequest,
    HistoryDeleteResult,
    UserPreferences,
)
from app.services import interaction

router = APIRouter(prefix="/v1/users", tags=["users"])


def _preferences_model(prefs: dict) -> UserPreferences:
    return UserPreferences(
        tags=list(prefs.get("tags") or []),
        platforms=list(prefs.get("platforms") or []),
        genres=list(prefs.get("genres") or []),
        safe_mode=bool(prefs.get("safe_mode", True)),
    )


@router.post(
    "/anonymous",
    operation_id="postAnonymousUser",
    response_model=EnvelopeAnonymousUser,
    summary="创建或复用匿名用户",
    description=(
        "首次启动时调用。传入 install_id 时，若在令牌有效期内命中同一匿名用户则复用并轮换令牌，"
        "否则新建。install_id 由客户端本地随机生成，不使用 IDFA / Android ID 等可追踪标识。"
    ),
    responses=error_responses(400, 422, 429, 500),
)
def post_anonymous_user(
    settings: SettingsDep,
    flags: FlagsDep,
    payload: AnonymousUserRequest | None = None,
):
    install_id = (payload.install_id if payload else None) or None
    platform = payload.platform if payload else None
    app_version = payload.app_version if payload else None

    record = None
    if install_id:
        existing = store.find_by_install_id(install_id)
        if existing is not None and existing["expires_at"] > now():
            record = store.rotate_token(existing["user_id"], settings.token_ttl_hours)

    if record is None:
        record = store.create_user(
            install_id=install_id,
            platform=platform,
            app_version=app_version,
            ttl_hours=settings.token_ttl_hours,
        )

    return envelope.ok(
        AnonymousUser(
            user_id=record["user_id"],
            access_token=record["access_token"],
            token_type="Bearer",
            expires_at=record["expires_at"],
            preferences=_preferences_model(record["preferences"]),
        )
    )


@router.get(
    "/me/preferences",
    operation_id="getMyPreferences",
    response_model=EnvelopeUserPreferences,
    summary="获取内容偏好",
    responses=error_responses(401, 429, 500),
)
def get_my_preferences(user: CurrentUser):
    return envelope.ok(_preferences_model(interaction.preferences_for(user["user_id"])))


@router.put(
    "/me/preferences",
    operation_id="putMyPreferences",
    response_model=EnvelopeUserPreferences,
    summary="更新内容偏好",
    responses=error_responses(401, 422, 429, 500),
)
def put_my_preferences(payload: UserPreferences, user: CurrentUser):
    patch = {
        "tags": list(payload.tags),
        "platforms": list(payload.platforms),
        "genres": list(payload.genres),
        "safe_mode": payload.safe_mode,
    }
    return envelope.ok(_preferences_model(interaction.update_preferences(user["user_id"], patch)))


@router.get(
    "/me/history",
    operation_id="getMyHistory",
    response_model=EnvelopeHistoryPage,
    summary="获取浏览与搜索历史",
    responses=error_responses(400, 401, 429, 500),
)
def get_my_history(
    user: CurrentUser,
    cursor: CursorQuery = None,
    limit: LimitQuery = 20,
    kind: Literal["browse", "search"] | None = None,
):
    entries = interaction.history_entries(user["user_id"], kind)
    page, next_cursor, has_more = paginate(
        entries, scope=f"history:{kind or 'all'}", cursor=cursor, limit=limit
    )
    return envelope.ok({"items": page}, next_cursor=next_cursor, has_more=has_more)


@router.delete(
    "/me/history",
    operation_id="deleteMyHistory",
    response_model=EnvelopeHistoryDelete,
    summary="删除历史",
    responses=error_responses(401, 429, 500),
)
def delete_my_history(user: CurrentUser, kind: Literal["browse", "search"] | None = None):
    return envelope.ok(HistoryDeleteResult(**interaction.delete_history(user["user_id"], kind)))


@router.delete(
    "/me/history/search",
    operation_id="deleteMySearchHistory",
    response_model=EnvelopeHistoryDelete,
    summary="清空搜索历史",
    responses=error_responses(401, 429, 500),
)
def delete_my_search_history(user: CurrentUser):
    return envelope.ok(HistoryDeleteResult(**interaction.delete_history(user["user_id"], "search")))
