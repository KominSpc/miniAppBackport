"""每日冷知识与 B 站热点视频。"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter

from app.api.v1.common import error_responses
from app.core import envelope
from app.core.timeutil import today
from app.deps import BaseUrlDep, CurrentUser
from app.schemas.envelopes import EnvelopeContentItemList, EnvelopeDailyFact
from app.services import catalog, daily, interaction

router = APIRouter(prefix="/v1/daily", tags=["daily"])

BILIBILI_COUNT = 12


@router.get(
    "/fact",
    operation_id="getDailyFact",
    response_model=EnvelopeDailyFact,
    summary="当日冷知识",
    responses=error_responses(401, 429, 500),
)
def get_daily_fact(user: CurrentUser, date: dt.date | None = None):
    return envelope.ok(daily.fact_for(date or today()))


@router.get(
    "/bilibili",
    operation_id="getDailyBilibili",
    response_model=EnvelopeContentItemList,
    summary="B 站每日热点视频",
    responses=error_responses(401, 429, 500),
)
def get_daily_bilibili(user: CurrentUser, base_url: BaseUrlDep):
    items = catalog.all_videos(
        base_url, safe_mode=interaction.safe_mode_for(user["user_id"])
    )[:BILIBILI_COUNT]
    return envelope.ok({"items": items})
