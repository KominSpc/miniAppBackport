"""美图推荐、搜索、收藏与图片代理。

注意路由声明顺序：静态路径必须先于 /{id} 注册，否则 /v1/images/daily 会被当成条目 ID。
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query
from fastapi.responses import FileResponse

from app.api.v1.common import error_responses
from app.core import envelope
from app.core.errors import not_found
from app.core.pagination import paginate
from app.deps import BaseUrlDep, CurrentUser, CursorQuery, LimitQuery
from app.fixtures import images as sample_images
from app.schemas.envelopes import (
    EnvelopeContentItem,
    EnvelopeContentItemList,
    EnvelopeContentItemPage,
    EnvelopeFavoriteState,
)
from app.schemas.content import FavoriteState
from app.services import catalog, interaction

router = APIRouter(prefix="/v1/images", tags=["images"])

IdPath = Annotated[str, Path(description="内容条目 ID，如 img_0001。")]

_FILE_RESPONSES = {
    200: {
        "description": "图片字节",
        "content": {
            "image/jpeg": {"schema": {"type": "string", "format": "binary"}},
            "image/png": {"schema": {"type": "string", "format": "binary"}},
            "image/webp": {"schema": {"type": "string", "format": "binary"}},
        },
    },
    **error_responses(401, 404, 429, 503),
}


def _favorited(user_id: str) -> frozenset[str]:
    return interaction.favorited_ids(user_id)


@router.get(
    "/daily",
    operation_id="getDailyImages",
    response_model=EnvelopeContentItemList,
    summary="每日热图",
    responses=error_responses(401, 429, 500),
)
def get_daily_images(user: CurrentUser, base_url: BaseUrlDep):
    items = catalog.daily_images(
        base_url,
        favorited_ids=_favorited(user["user_id"]),
        safe_mode=interaction.safe_mode_for(user["user_id"]),
    )
    return envelope.ok({"items": items})


@router.get(
    "/recommended",
    operation_id="getRecommendedImages",
    response_model=EnvelopeContentItemPage,
    summary="分页美图推荐",
    responses=error_responses(400, 401, 429, 500),
)
def get_recommended_images(
    user: CurrentUser,
    base_url: BaseUrlDep,
    cursor: CursorQuery = None,
    limit: LimitQuery = 20,
    tag: str | None = None,
):
    items = catalog.filter_by_tag(
        catalog.all_images(
            base_url,
            favorited_ids=_favorited(user["user_id"]),
            safe_mode=interaction.safe_mode_for(user["user_id"]),
        ),
        tag,
    )
    page, next_cursor, has_more = paginate(
        items, scope=f"images:recommended:{tag or ''}", cursor=cursor, limit=limit
    )
    return envelope.ok({"items": page}, next_cursor=next_cursor, has_more=has_more)


@router.get(
    "/search",
    operation_id="searchImages",
    response_model=EnvelopeContentItemPage,
    summary="按角色或标签搜索",
    responses=error_responses(400, 401, 429, 500),
)
def search_images(
    user: CurrentUser,
    base_url: BaseUrlDep,
    q: Annotated[str, Query(min_length=1, description="检索词，可匹配标题、标签与作者")],
    cursor: CursorQuery = None,
    limit: LimitQuery = 20,
):
    items = catalog.search_images(
        base_url,
        q,
        favorited_ids=_favorited(user["user_id"]),
        safe_mode=interaction.safe_mode_for(user["user_id"]),
    )
    interaction.record_search(user["user_id"], q)
    page, next_cursor, has_more = paginate(
        items, scope=f"images:search:{q}", cursor=cursor, limit=limit
    )
    return envelope.ok({"items": page}, next_cursor=next_cursor, has_more=has_more)


@router.get(
    "/favorites",
    operation_id="getFavoriteImages",
    response_model=EnvelopeContentItemPage,
    summary="收藏列表",
    responses=error_responses(400, 401, 429, 500),
)
def get_favorite_images(
    user: CurrentUser,
    base_url: BaseUrlDep,
    cursor: CursorQuery = None,
    limit: LimitQuery = 20,
):
    safe_mode = interaction.safe_mode_for(user["user_id"])
    items = []
    for content_id in interaction.favorited_ids(user["user_id"]):
        item = catalog.item_by_id(content_id, base_url=base_url, favorited=True, safe_mode=safe_mode)
        if item is not None:
            items.append(item)
    page, next_cursor, has_more = paginate(
        items, scope="images:favorites", cursor=cursor, limit=limit
    )
    return envelope.ok({"items": page}, next_cursor=next_cursor, has_more=has_more)


@router.get(
    "/{id}/file",
    operation_id="getImageFile",
    summary="图片内容代理（thumb / regular / original）",
    responses=_FILE_RESPONSES,
)
def get_image_file(
    user: CurrentUser,
    id: IdPath,
    variant: Literal["thumb", "regular", "original"] = "thumb",
    page: Annotated[int, Query(ge=0)] = 0,
):
    path = sample_images.image_path(id, variant, page)
    if path is None:
        raise not_found({"id": id, "variant": variant, "page": page})
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.put(
    "/{id}/favorite",
    operation_id="putImageFavorite",
    response_model=EnvelopeFavoriteState,
    summary="收藏（幂等）",
    responses=error_responses(401, 404, 429, 500),
)
def put_image_favorite(user: CurrentUser, base_url: BaseUrlDep, id: IdPath):
    return _set_favorite(user, base_url, id, True)


@router.delete(
    "/{id}/favorite",
    operation_id="deleteImageFavorite",
    response_model=EnvelopeFavoriteState,
    summary="取消收藏（幂等）",
    responses=error_responses(401, 404, 429, 500),
)
def delete_image_favorite(user: CurrentUser, base_url: BaseUrlDep, id: IdPath):
    return _set_favorite(user, base_url, id, False)


def _set_favorite(user: dict, base_url: str, content_id: str, favorited: bool):
    visible = catalog.item_by_id(
        content_id,
        base_url=base_url,
        favorited=favorited,
        safe_mode=interaction.safe_mode_for(user["user_id"]),
    )
    if visible is None:
        raise not_found({"id": content_id})
    return envelope.ok(FavoriteState(**interaction.set_favorite(user["user_id"], content_id, favorited)))


@router.get(
    "/{id}",
    operation_id="getImageDetail",
    response_model=EnvelopeContentItem,
    summary="单张图片详情",
    responses=error_responses(401, 404, 429, 500),
)
def get_image_detail(user: CurrentUser, base_url: BaseUrlDep, id: IdPath):
    item = catalog.item_by_id(
        id,
        base_url=base_url,
        favorited=id in interaction.favorited_ids(user["user_id"]),
        safe_mode=interaction.safe_mode_for(user["user_id"]),
    )
    if item is None:
        raise not_found({"id": id})
    interaction.record_browse(user["user_id"], item)
    return envelope.ok(item)

