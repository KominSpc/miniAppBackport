"""漫画页接口：搜索、标签、最近更新、随机推荐、详情、章节目录与图片代理。

响应统一走 ``{data, meta, error}`` 信封，``meta.source`` 固定为 ``comic``。

**图片不下发上游直链**：manhuagui 的内页图必须带 ``Referer`` 才给（客户端在 web 上
发不出这个头），QQ 与动漫屋的图床又不返回 CORS 头，所以客户端拿到的永远是本服务的
``/v1/comics/image?url=...``。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Path, Query
from fastapi.responses import Response

from app.api.v1.common import error_responses
from app.core import envelope
from app.core.errors import not_found
from app.deps import BaseUrlDep, CurrentUser
from app.schemas.envelopes import (
    EnvelopeComicChapter,
    EnvelopeComicDetail,
    EnvelopeComicSummaryList,
    EnvelopeComicTagGroupList,
)
from app.services.comic import adapter as comic_adapter
from app.services.comic import source as comic_source

router = APIRouter(prefix="/v1/comics", tags=["comics"])

SOURCE = "comic"
IMAGE_CHUNK = 64 * 1024

SiteQuery = Annotated[
    str | None,
    Query(description="站点：manhuagui（默认/最稳）/ qq / dm5；搜索翻页时必须回传首屏命中的站点"),
]

KeywordQuery = Annotated[str, Query(min_length=1, max_length=60, description="漫画名关键词")]

PageQuery = Annotated[int, Query(ge=1, le=50, description="页码，1 起")]

LimitQuery = Annotated[int, Query(ge=1, le=40, description="随机推荐每页条数")]

SeedQuery = Annotated[
    str | None,
    Query(max_length=40, description="随机推荐的洗牌种子；同一 seed + page 结果稳定"),
]

SitePath = Annotated[str, Path(description="站点：manhuagui / qq / dm5")]

ComicIdPath = Annotated[str, Path(description="上游站点内的漫画 ID，如 1128")]

ChapterNumberPath = Annotated[int, Path(ge=0, description="章节序号")]

UrlQuery = Annotated[str, Query(min_length=8, max_length=2048, description="上游图片地址（服务端会校验域名白名单）")]


def _page(items: list[dict], *, page: int) -> dict:
    # 上游按站点分页，拿不到总页数：本页有条目就认为还有下一页，空页即到底
    return envelope.ok(
        {"items": items},
        next_cursor=None,
        has_more=bool(items) and page < 50,
        source=SOURCE,
    )


def _list(items: list[dict]) -> dict:
    return envelope.ok({"items": items}, source=SOURCE)


@router.get(
    "/search",
    operation_id="searchComics",
    response_model=EnvelopeComicSummaryList,
    summary="搜索漫画",
    responses=error_responses(400, 401, 422, 429, 503),
)
def search_comics(
    user: CurrentUser,
    base_url: BaseUrlDep,
    q: KeywordQuery,
    site: SiteQuery = None,
    page: PageQuery = 1,
):
    # 命中哪个站点由条目自带的 site 表达（客户端翻页时把它回传，见 SiteQuery）
    items, _matched = comic_source.search(q, page=page, site=site, base_url=base_url)
    return _page(items, page=page)


@router.get(
    "/latest",
    operation_id="latestComics",
    response_model=EnvelopeComicSummaryList,
    summary="最近更新（热门）",
    responses=error_responses(400, 401, 422, 429, 503),
)
def latest_comics(
    user: CurrentUser,
    base_url: BaseUrlDep,
    site: SiteQuery = None,
    page: PageQuery = 1,
):
    return _page(comic_source.latest(page=page, site=site, base_url=base_url), page=page)


@router.get(
    "/random",
    operation_id="randomComics",
    response_model=EnvelopeComicSummaryList,
    summary="随机推荐（无限下滑）",
    responses=error_responses(400, 401, 422, 429, 503),
)
def random_comics(
    user: CurrentUser,
    base_url: BaseUrlDep,
    page: PageQuery = 1,
    limit: LimitQuery = 20,
    seed: SeedQuery = None,
):
    items, has_more = comic_source.random_page(
        page=page, limit=limit, seed=seed, base_url=base_url
    )
    return envelope.ok({"items": items}, next_cursor=None, has_more=has_more, source=SOURCE)


@router.get(
    "/tags",
    operation_id="comicTags",
    response_model=EnvelopeComicTagGroupList,
    summary="标签分组",
    responses=error_responses(400, 401, 422, 429, 503),
)
def comic_tags(user: CurrentUser, site: SiteQuery = None):
    return _list(comic_source.tags(site=site))


@router.get(
    "/tag",
    operation_id="comicsByTag",
    response_model=EnvelopeComicSummaryList,
    summary="按标签浏览",
    responses=error_responses(400, 401, 422, 429, 503),
)
def comics_by_tag(
    user: CurrentUser,
    base_url: BaseUrlDep,
    tag: Annotated[str, Query(min_length=1, max_length=40, description="标签取值，来自 /tags")],
    site: SiteQuery = None,
    page: PageQuery = 1,
):
    return _page(comic_source.tag_list(tag, page=page, site=site, base_url=base_url), page=page)


@router.get(
    "/image",
    operation_id="comicImage",
    summary="图片代理（封面 / 内页）",
    responses={
        200: {
            "description": "图片字节",
            "content": {"image/jpeg": {"schema": {"type": "string", "format": "binary"}}},
        },
        **error_responses(400, 401, 404, 429, 503),
    },
)
def comic_image(
    user: CurrentUser,
    base_url: BaseUrlDep,
    url: UrlQuery,
    site: SiteQuery = None,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
):
    body, content_type = comic_source.image_bytes(url)
    headers = {"Cache-Control": "public, max-age=3600"}
    if range_header:
        # 漫画阅读是一次性拉整张图，Range 只在少数爬虫带分片时出现；原样透传即可
        headers["Accept-Ranges"] = "bytes"
    return Response(content=body, media_type=content_type, headers=headers)


@router.get(
    "/{site}/{comic_id}",
    operation_id="comicDetail",
    response_model=EnvelopeComicDetail,
    summary="漫画详情（含章节表）",
    responses=error_responses(400, 401, 404, 422, 429, 503),
)
def comic_detail(
    user: CurrentUser, base_url: BaseUrlDep, site: SitePath, comic_id: ComicIdPath
):
    _require_site(site)
    detail = comic_source.detail(site, comic_id, base_url=base_url)
    return envelope.ok(detail, source=SOURCE)


@router.get(
    "/{site}/{comic_id}/chapters/{number}",
    operation_id="comicChapter",
    response_model=EnvelopeComicChapter,
    summary="章节目录（图片列表）",
    responses=error_responses(400, 401, 404, 422, 429, 503),
)
def comic_chapter(
    user: CurrentUser,
    base_url: BaseUrlDep,
    site: SitePath,
    comic_id: ComicIdPath,
    number: ChapterNumberPath,
    ext_name: Annotated[str, Query(max_length=40, description="番外分组名，主篇留空")] = "",
):
    _require_site(site)
    content = comic_source.chapter(site, comic_id, number, ext_name=ext_name, base_url=base_url)
    return envelope.ok(content, source=SOURCE)


def _require_site(site: str) -> None:
    if site not in comic_source.sites():
        raise not_found({"site": site, "reason": "site_not_supported"})
