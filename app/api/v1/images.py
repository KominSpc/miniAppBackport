"""美图推荐、搜索、收藏与图片代理。

注意路由声明顺序：静态路径必须先于 /{id} 注册，否则 /v1/images/daily 会被当成条目 ID。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Path, Query
from fastapi.responses import FileResponse, Response

from app.api.v1.common import error_responses
from app.core import envelope
from app.core.errors import not_found
from app.core.pagination import decode_cursor, paginate
from app.deps import BaseUrlDep, CurrentUser, CursorQuery, LimitQuery
from app.fixtures import images as sample_images
from app.schemas.envelopes import (
    EnvelopeContentItem,
    EnvelopeContentItemList,
    EnvelopeContentItemPage,
    EnvelopeFavoriteState,
    EnvelopePixivArtist,
    EnvelopeUgoiraMeta,
)
from app.schemas.content import FavoriteSnapshot, FavoriteState
from app.services import catalog, interaction
from app.services.pixiv import adapter as pixiv_adapter
from app.services.pixiv import source as pixiv_source

router = APIRouter(prefix="/v1/images", tags=["images"])

IdPath = Annotated[str, Path(description="内容条目 ID，如 img_0001。")]

# 搜索筛选参数。三项都由服务端校验后透传给上游，客户端永远拿不到上游细节。
MinBookmarksQuery = Annotated[
    int | None,
    Query(
        ge=0,
        le=1000000,
        description=(
            "收藏数下限（对应上游搜索的 bl 参数），如 1000 表示只要收藏数 ≥ 1000 的作品；"
            "仅真实内容源（CONTENT_SOURCE=pixiv）生效，mock 夹具没有该数据，会被忽略。"
        ),
    ),
]

LangQuery = Annotated[
    Literal["zh", "ja", "en"] | None,
    Query(
        description=(
            "上游语言参数（pixiv 的 lang）：影响标签与标题的翻译文案；"
            "仅真实内容源生效。"
        )
    ),
]

R18Query = Annotated[
    bool,
    Query(
        description=(
            "是否包含 R18 内容。双重门槛：不仅要传 true，还需要该用户偏好的 "
            "safe_mode=false（即已确认 18+）；否则服务端强制按 safe 处理。"
            "注意：pixiv 未登录（服务端未配置 PIXIV_COOKIE）时会忽略 mode 参数，"
            "此时打开 R18 也拿不到 R18 作品。"
        )
    ),
]

IllustTypeQuery = Annotated[
    Literal["all", "illust", "manga", "ugoira"],
    Query(
        description=(
            "作品类型（对齐参考实现 pixiv-api.js 的 illust / manga / ugoira 分类）："
            "all 不筛、illust 插画、manga 漫画、ugoira 动图。实测上游会忽略 type "
            "参数，服务端按条目的 illust_type 本地过滤，筛出的条目不够一"
            "页时会自动接着翻上游页；mock 夹具没有该字段，会被忽略。"
        )
    ),
]

# 动图 zip 的响应形状与图片不同（application/zip），单独声明
_UGOIRA_FILE_RESPONSES = {
    200: {
        "description": "动图 zip（内部是按 frames 顺序排列的 JPEG 帧）",
        "content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}},
    },
    **error_responses(401, 404, 429, 503),
}

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


# 真实内容源：内容 ID 前缀 → 取图模块。新增内容源时只改这一张表。
_REMOTE_SOURCES: tuple[tuple[Any, Any], ...] = (
    (pixiv_adapter.is_pixiv_id, pixiv_source),
)


def _remote_source_for(content_id: str) -> Any | None:
    """按 ID 前缀找真实内容源模块；本地夹具 ID 返回 None（继续走静态图片）。"""
    for matches, module in _REMOTE_SOURCES:
        if matches(content_id):
            return module
    return None


def _artist_offset(cursor: str | None, scope: str) -> int:
    """作者作品游标 → 偏移量。

    同时认两种形状：客户端直连通道用的 ``a:<偏移>`` 与 [paginate] 的 ``c1...``。
    两条通道共用一个列表控制器，用户在直连与后端回退之间切换时游标会交叉传递，
    只认一种就会 400。
    """
    if not cursor:
        return 0
    if cursor.startswith("a:"):
        raw = cursor[2:]
        return int(raw) if raw.isdigit() else 0
    return decode_cursor(cursor, scope)


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
    q: Annotated[str, Query(min_length=1, description="检索词，可匹配标题、标签与作者；支持中文分词、拼音（全拼与首字母）与别名")],
    cursor: CursorQuery = None,
    limit: LimitQuery = 20,
    min_bookmarks: MinBookmarksQuery = None,
    lang: LangQuery = None,
    r18: R18Query = False,
    illust_type: IllustTypeQuery = "all",
):
    # safe_mode 是服务端闸门：R18 只有在这里为 False（用户已确认 18+）时才真的放开，
    # 单靠查询参数无法绕过。
    safe_mode = interaction.safe_mode_for(user["user_id"])
    items = catalog.search_images(
        base_url,
        q,
        favorited_ids=_favorited(user["user_id"]),
        safe_mode=safe_mode,
        cursor=cursor,
        limit=limit,
        min_bookmarks=min_bookmarks,
        lang=lang,
        r18=r18,
        illust_type=illust_type,
    )
    interaction.record_search(user["user_id"], q)
    page, next_cursor, has_more = paginate(
        items,
        scope=catalog.search_scope(
            q,
            min_bookmarks=min_bookmarks,
            lang=lang,
            r18=r18,
            illust_type=illust_type,
        ),
        cursor=cursor,
        limit=limit,
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
    favorited = interaction.favorited_ids(user["user_id"])
    # 收藏时留下的内存快照优先（最近收藏的在前），因此列表不再逐条回源上游；
    # 仅在没有快照时（例如进程重启后重新收藏前的旧记录）回源补一次。
    snapshots = interaction.favorite_snapshots(user["user_id"])
    ordered = [cid for cid in reversed(list(snapshots)) if cid in favorited]
    ordered += [cid for cid in sorted(favorited) if cid not in snapshots]
    items = []
    for content_id in ordered:
        snapshot = snapshots.get(content_id)
        if snapshot is not None:
            item = catalog.rebase_item(snapshot, base_url=base_url, favorited=True)
        else:
            item = catalog.item_by_id(
                content_id, base_url=base_url, favorited=True, safe_mode=safe_mode
            )
        if item is None:
            continue
        if safe_mode and item["is_sensitive"]:
            continue
        items.append(item)
    page, next_cursor, has_more = paginate(
        items, scope="images:favorites", cursor=cursor, limit=limit
    )
    return envelope.ok({"items": page}, next_cursor=next_cursor, has_more=has_more)


@router.get(
    "/artists/{user_id}",
    operation_id="getArtistProfile",
    response_model=EnvelopePixivArtist,
    summary="作者（画师）主页信息",
    responses=error_responses(401, 404, 429, 503),
)
def get_artist_profile(
    user: CurrentUser,
    user_id: Annotated[str, Path(description="pixiv 作者 ID")],
):
    return envelope.ok(catalog.artist_profile(user_id))


@router.get(
    "/artists/{user_id}/works",
    operation_id="getArtistWorks",
    response_model=EnvelopeContentItemPage,
    summary="作者作品列表（分页）",
    responses=error_responses(400, 401, 404, 429, 503),
)
def get_artist_works(
    user: CurrentUser,
    base_url: BaseUrlDep,
    user_id: Annotated[str, Path(description="pixiv 作者 ID")],
    cursor: CursorQuery = None,
    limit: LimitQuery = 20,
):
    scope = catalog.artist_works_scope(user_id)
    offset = _artist_offset(cursor, scope)
    # 多取一条：调用方用「取到的条目数 > 窗口末尾」判断还有没有下一页，
    # 窗口恰好装满会被误判成到底（表现为作者页翻两页就「已经到底啦」）。
    items = catalog.artist_works(
        base_url,
        user_id,
        offset=offset,
        limit=limit + 1,
        favorited_ids=_favorited(user["user_id"]),
        safe_mode=interaction.safe_mode_for(user["user_id"]),
    )
    has_more = len(items) > limit
    page = items[:limit]
    # 游标沿用直连通道的形状（``a:<偏移>``）：两条通道共用一个控制器，
    # 换成 c1... 之后一旦切回直连就会被拒。
    next_cursor = f"a:{offset + len(page)}" if has_more else None
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
    # 真实内容源：px_ 前缀回源 i.pximg.net，bv_ 前缀回源 i*.hdslb.com。两家图床都强
    # 校验 Referer（pixiv 实测无 Referer 返回 403），客户端直连必然失败，因此这里是
    # 唯一的取图通道（见 7.2 第 1 条与 13.x）。
    remote = _remote_source_for(id)
    if remote is not None:
        if not remote.active():
            raise not_found({"id": id, "reason": "content_source_mock"})
        resolved = remote.image_bytes(id, variant, page)
        if resolved is None:
            raise not_found({"id": id, "variant": variant, "page": page})
        content, media_type = resolved
        return Response(
            content=content,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=3600"},
        )

    path = sample_images.image_path(id, variant, page)
    if path is None:
        raise not_found({"id": id, "variant": variant, "page": page})
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get(
    "/{id}/ugoira",
    operation_id="getImageUgoira",
    response_model=EnvelopeUgoiraMeta,
    summary="动图（うごイラ）帧表",
    responses=error_responses(401, 404, 429, 503),
)
def get_image_ugoira(user: CurrentUser, base_url: BaseUrlDep, id: IdPath):
    """动图的逐帧延时表；帧数据另取 ``/{id}/ugoira/file``。

    pixiv 的动图不是视频：上游给的是一个 zip（内含按序的 JPEG 帧）加逐帧延时，
    客户端必须自己按贴图帧播放（见 docs/EXECUTION_PLAN.md 15.x）。非动图作品
    在这里统一 404 —— 客户端据此回落到静态图，不用再猜文件类型。
    """
    pid = pixiv_adapter.illust_id_of(id)
    if pid is None or not pixiv_source.active():
        raise not_found({"id": id, "reason": "not_animated"})
    meta = pixiv_source.ugoira_meta(pid)
    if meta is None:
        raise not_found({"id": id, "reason": "not_animated"})
    frames = list(meta["frames"])
    return envelope.ok(
        {
            "id": id,
            "frame_count": len(frames),
            "frames": frames,
            "total_ms": sum(int(frame["delay_ms"]) for frame in frames),
            "src": meta["src"],
            "original_src": meta["original_src"],
            "archive_url": f"{base_url}/v1/images/{id}/ugoira/file",
        }
    )


@router.get(
    "/{id}/ugoira/file",
    operation_id="getImageUgoiraFile",
    summary="动图 zip 代理（src / original）",
    responses=_UGOIRA_FILE_RESPONSES,
)
def get_image_ugoira_file(
    user: CurrentUser,
    id: IdPath,
    variant: Literal["src", "original"] = "src",
):
    """动图 zip 的字节代理。

    与取图同理：i.pximg.net 强校验 Referer，客户端直连（尤其 web）拿不到，
    所以由服务端代取。zip 内容与 URL 一一对应（帧表变了上游会换 URL），
    可以放心让客户端按小时级 TTL 缓存。
    """
    pid = pixiv_adapter.illust_id_of(id)
    if pid is None or not pixiv_source.active():
        raise not_found({"id": id, "reason": "not_animated"})
    resolved = pixiv_source.ugoira_archive(pid, original=variant == "original")
    if resolved is None:
        raise not_found({"id": id, "variant": variant, "reason": "not_animated"})
    content, _media_type = resolved
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.put(
    "/{id}/favorite",
    operation_id="putImageFavorite",
    response_model=EnvelopeFavoriteState,
    summary="收藏（幂等）",
    responses=error_responses(401, 404, 429, 500),
)
def put_image_favorite(
    user: CurrentUser,
    base_url: BaseUrlDep,
    id: IdPath,
    payload: FavoriteSnapshot | None = None,
):
    return _set_favorite(user, base_url, id, True, payload)


@router.delete(
    "/{id}/favorite",
    operation_id="deleteImageFavorite",
    response_model=EnvelopeFavoriteState,
    summary="取消收藏（幂等）",
    responses=error_responses(401, 404, 429, 500),
)
def delete_image_favorite(user: CurrentUser, base_url: BaseUrlDep, id: IdPath):
    return _set_favorite(user, base_url, id, False)


def _set_favorite(
    user: dict,
    base_url: str,
    content_id: str,
    favorited: bool,
    payload: FavoriteSnapshot | None = None,
):
    visible = catalog.item_by_id(
        content_id,
        base_url=base_url,
        favorited=favorited,
        safe_mode=interaction.safe_mode_for(user["user_id"]),
    )
    if visible is None and favorited and payload is not None:
        # 直连模式：条目直接来自 pixiv 上游，本地目录里没有它（CONTENT_SOURCE=mock
        # 时更是如此），此时以客户端带回的快照为准。只认内容，ID 一律以路径为准，
        # 图片链接也会被 rebase 回本服务地址，客户端不能借这个入口注入任意链接。
        visible = _snapshot_item(payload, content_id, base_url)
    if visible is None:
        raise not_found({"id": content_id})
    # 收藏时把条目快照一起存进内存：收藏列表从此不必逐条回源上游
    return envelope.ok(
        FavoriteState(
            **interaction.set_favorite(user["user_id"], content_id, favorited, visible)
        )
    )


def _snapshot_item(
    payload: FavoriteSnapshot, content_id: str, base_url: str
) -> dict | None:
    """把客户端快照修成可入库的条目；形状不对就当作没有快照。"""
    candidate = dict(payload.item)
    if candidate.get("type") != "image":
        return None
    candidate["id"] = content_id
    candidate.pop("is_sensitive", None)
    return catalog.rebase_item(
        {**candidate, "is_sensitive": False}, base_url=base_url, favorited=True
    )


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
