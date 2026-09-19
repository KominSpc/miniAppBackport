"""今日更新与游戏总单。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.common import error_responses
from app.core import envelope
from app.core.pagination import paginate
from app.deps import BaseUrlDep, CurrentUser, CursorQuery, LimitQuery
from app.schemas.envelopes import EnvelopeContentItemList, EnvelopeContentItemPage
from app.services import catalog, interaction

router = APIRouter(prefix="/v1/games", tags=["games"])


@router.get(
    "/today",
    operation_id="getTodayGames",
    response_model=EnvelopeContentItemList,
    summary="今日更新游戏",
    responses=error_responses(401, 429, 500),
)
def get_today_games(user: CurrentUser, base_url: BaseUrlDep):
    items = catalog.today_games(
        base_url, safe_mode=interaction.safe_mode_for(user["user_id"])
    )
    return envelope.ok({"items": items})


@router.get(
    "",
    operation_id="getGames",
    response_model=EnvelopeContentItemPage,
    summary="游戏总单与平台/类型筛选",
    responses=error_responses(400, 401, 429, 500),
)
def get_games(
    user: CurrentUser,
    base_url: BaseUrlDep,
    platform: str | None = None,
    genre: str | None = None,
    cursor: CursorQuery = None,
    limit: LimitQuery = 20,
):
    items = catalog.games_by_filter(
        catalog.all_games(base_url, safe_mode=interaction.safe_mode_for(user["user_id"])),
        platform,
        genre,
    )
    page, next_cursor, has_more = paginate(
        items,
        scope=f"games:{platform or ''}:{genre or ''}",
        cursor=cursor,
        limit=limit,
    )
    return envelope.ok({"items": page}, next_cursor=next_cursor, has_more=has_more)
