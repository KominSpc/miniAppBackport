"""开发环境重置。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header

from app.api.v1.common import error_responses
from app.core import envelope
from app.core.timeutil import now
from app.deps import CurrentUser, SettingsDep, require_admin_token
from app.repositories import store
from app.schemas.envelopes import EnvelopeDevReset
from app.schemas.health import DevResetResult, ResetCounters

router = APIRouter(prefix="/v1/dev", tags=["system"])


@router.post(
    "/reset",
    operation_id="postDevReset",
    response_model=EnvelopeDevReset,
    summary="清空模拟用户数据与互动记录",
    description=(
        "清空模拟用户数据与互动记录。仅当开发配置开启时可用，并要求管理 token"
        "（请求头 X-Admin-Token）。否则返回 403 FORBIDDEN。"
    ),
    responses=error_responses(401, 403, 500),
)
def post_dev_reset(
    user: CurrentUser,
    settings: SettingsDep,
    x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
):
    require_admin_token(settings, x_admin_token)
    cleared = store.reset()
    return envelope.ok(DevResetResult(reset_at=now(), cleared=ResetCounters(**cleared)))
