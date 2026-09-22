"""内容编目：把夹具数据装配成契约约定的 ContentItem。

所有链接都是本服务同源地址，客户端不感知来源平台。
"""

from __future__ import annotations

from collections import Counter

import hashlib
from datetime import datetime
from typing import Any, Iterable, Sequence

from app.core import pagination
from app.core.errors import AppError
from app.core.timeutil import hours_ago, now, today
from app.fixtures.dataset import load_fixtures
from app.services import search
from app.services.pixiv import adapter as pixiv_adapter
from app.services.pixiv import source as pixiv_source

DAILY_IMAGE_COUNT = 8
BILIBILI_COUNT = 12
TODAY_UPDATED_GAMES = 6


def file_url(base_url: str, content_id: str, variant: str = "thumb", page: int = 0) -> str:
    url = f"{base_url}/v1/images/{content_id}/file?variant={variant}"
    if page:
        url += f"&page={page}"
    return url


def rebase_item(
    item: dict[str, Any],
    *,
    base_url: str,
    favorited: bool | None = None,
) -> dict[str, Any]:
    """把内存快照里的同源链接改写成**当前请求**的 base_url。

    快照生成于收藏那一刻，base_url 可能来自另一个 host（换端口 / 局域网访问），
    直接下发会让客户端拿到失效的图片地址。这些链接只由 content_id 决定，改前缀即可。
    """
    content_id = item["id"]
    payload = dict(item.get("payload") or {})
    payload["thumbnail_url"] = file_url(base_url, content_id, "thumb")
    payload["image_url"] = file_url(base_url, content_id, "original")
    if favorited is not None:
        payload["is_favorited"] = favorited
    return {
        **item,
        "cover_url": file_url(base_url, content_id, "thumb"),
        "payload": payload,
    }


def source_page_url(base_url: str, content_id: str) -> str:
    return f"{base_url}/mock/source/{content_id}"


def _is_today(moment: datetime) -> bool:
    return moment.date() == today()


def _today_offset(index: int, count: int = TODAY_UPDATED_GAMES) -> float:
    """把「今日更新」均匀铺在当天已经过去的时段内，保证始终属于今天。"""
    midnight = now().replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = max(0.05, (now() - midnight).total_seconds() / 3600.0)
    return round(elapsed * (index + 1) / (count + 1), 4)


def build_image(row: dict[str, Any], *, base_url: str, favorited: bool = False) -> dict[str, Any]:
    content_id = row["id"]
    return {
        "id": content_id,
        "type": "image",
        "title": row["title"],
        "subtitle": row["author"],
        "cover_url": file_url(base_url, content_id, "thumb"),
        "tags": list(row["tags"]),
        "source": "mock",
        "source_url": source_page_url(base_url, content_id),
        "payload": {
            "author": row["author"],
            "thumbnail_url": file_url(base_url, content_id, "thumb"),
            "image_url": file_url(base_url, content_id, "original"),
            "width": row["width"],
            "height": row["height"],
            "aspect_ratio": row["aspect_ratio"],
            "is_favorited": favorited,
            "created_at": hours_ago(row["ago_hours"]),
        },
        "is_sensitive": bool(row["is_sensitive"]),
    }


def build_video(row: dict[str, Any], *, base_url: str, favorited: bool = False) -> dict[str, Any]:
    content_id = row["id"]
    return {
        "id": content_id,
        "type": "video",
        "title": row["title"],
        "subtitle": row["uploader"],
        "cover_url": file_url(base_url, content_id, "thumb"),
        "tags": list(row["tags"]),
        "source": "mock",
        "source_url": source_page_url(base_url, content_id),
        "payload": {
            "bvid": row["bvid"],
            "uploader": row["uploader"],
            "play_count": row["play_count"],
            "published_at": hours_ago(row["ago_hours"]),
            "hot_score": row["hot_score"],
            "is_hot": bool(row["is_hot"]),
            "duration": row["duration"],
            "is_favorited": favorited,
        },
        "is_sensitive": bool(row["is_sensitive"]),
    }


def build_game(row: dict[str, Any], index: int, *, base_url: str) -> dict[str, Any]:
    content_id = row["id"]
    if index < TODAY_UPDATED_GAMES:
        updated_at = hours_ago(_today_offset(index))
    else:
        updated_at = hours_ago(row["ago_hours"])
    return {
        "id": content_id,
        "type": "game",
        "title": row["title"],
        "subtitle": " / ".join(row["platforms"]),
        "cover_url": file_url(base_url, content_id, "thumb"),
        "tags": list(row["genres"]),
        "source": "mock",
        "source_url": source_page_url(base_url, content_id),
        "payload": {
            "platforms": list(row["platforms"]),
            "genres": list(row["genres"]),
            "updated_at": updated_at,
            "description": row["description"],
            "version": row["version"],
            "developer": row["developer"],
            "rating": row["rating"],
            "today_updated": _is_today(updated_at),
        },
        "is_sensitive": bool(row["is_sensitive"]),
    }


def build_fact_card(row: dict[str, Any], *, base_url: str) -> dict[str, Any]:
    """把冷知识包装成 card，供宠物对话与每日页做跳转卡片。"""
    return {
        "id": row["id"],
        "type": "card",
        "title": row["content"][:24],
        "subtitle": "冷知识",
        "cover_url": file_url(base_url, "img_0001", "thumb"),
        "tags": list(row["tags"]),
        "source": "mock",
        "source_url": source_page_url(base_url, row["id"]),
        "payload": {"target_type": "card", "target_id": row["id"]},
        "is_sensitive": False,
    }


def _visible(items: Iterable[dict[str, Any]], safe_mode: bool) -> list[dict[str, Any]]:
    if not safe_mode:
        return list(items)
    return [item for item in items if not item["is_sensitive"]]


def all_images(base_url: str, *, favorited_ids: frozenset[str] = frozenset(), safe_mode: bool = True) -> list[dict[str, Any]]:
    # 内容源开关：CONTENT_SOURCE=pixiv 时改由真实适配器取数（见 docs/EXECUTION_PLAN.md 7.x）。
    # 产出仍走同一套契约，客户端与上游解耦。
    if pixiv_source.active():
        return pixiv_source.all_images(
            base_url, favorited_ids=favorited_ids, safe_mode=safe_mode
        )
    rows = sorted(load_fixtures().images, key=lambda row: row["ago_hours"])
    items = [build_image(row, base_url=base_url, favorited=row["id"] in favorited_ids) for row in rows]
    return _visible(items, safe_mode)


def all_videos(
    base_url: str,
    *,
    safe_mode: bool = True,
    category: str | None = None,
    limit: int = BILIBILI_COUNT,
) -> list[dict[str, Any]]:
    # B 站内容源已下线（见 docs/archive/bilibili_source/README.md）：视频只走本地夹具。
    rows = sorted(load_fixtures().videos, key=lambda row: row["hot_score"], reverse=True)
    return _visible([build_video(row, base_url=base_url) for row in rows], safe_mode)


def all_games(base_url: str, *, safe_mode: bool = True) -> list[dict[str, Any]]:
    items = [build_game(row, index, base_url=base_url) for index, row in enumerate(load_fixtures().games)]
    return _visible(items, safe_mode)


def daily_images(base_url: str, *, favorited_ids: frozenset[str] = frozenset(), safe_mode: bool = True) -> list[dict[str, Any]]:
    """每日热图：按日期 + 固定种子稳定挑选，同一天多次请求结果一致。"""
    pool = all_images(base_url, favorited_ids=favorited_ids, safe_mode=safe_mode)
    if not pool:
        return []
    seed = int(hashlib.sha256(today().isoformat().encode("utf-8")).hexdigest(), 16)
    ordered = sorted(pool, key=lambda item: int(hashlib.sha256(f"{seed}:{item['id']}".encode("utf-8")).hexdigest(), 16))
    return ordered[: DAILY_IMAGE_COUNT if len(ordered) >= DAILY_IMAGE_COUNT else len(ordered)]


def filter_by_tag(items: Sequence[dict[str, Any]], tag: str | None) -> list[dict[str, Any]]:
    """标签筛选：与搜索共用同一套匹配规则（分词 / 拼音 / 别名）。"""
    query_tokens = search.tokens(tag or "")
    if not query_tokens:
        return list(items)
    return [
        item
        for item in items
        if search.matches(search.index_forms(*item["tags"], item["title"]), query_tokens)
    ]


def search_scope(
    query: str,
    *,
    min_bookmarks: int | None = None,
    lang: str | None = None,
    r18: bool = False,
    illust_type: str = "all",
) -> str:
    """搜索分页游标的 scope：取数侧与请求侧共用，避免两处拼错导致 400。

    筛选条件进 scope：换筛选后如果客户端还拿着旧游标，会拿到 400 而不是「页码错位」。
    """
    return (
        f"images:search:{query}:{min_bookmarks or 0}:{lang or ''}"
        f":{int(bool(r18))}:{illust_type}"
    )


def search_images(
    base_url: str,
    query: str,
    *,
    favorited_ids: frozenset[str] = frozenset(),
    safe_mode: bool = True,
    cursor: str | None = None,
    limit: int = 20,
    min_bookmarks: int | None = None,
    lang: str | None = None,
    r18: bool = False,
    illust_type: str = "all",
) -> list[dict[str, Any]]:
    """美图检索。

    - **真实内容源**：走上游关键词搜索 —— 上游按标签/标题命中，能搜到榜单之外的
      作品；本地拿榜单做子串过滤正是「搜不到想要的关键词」的根因。
      收藏数下限 / 语言 / R18 / 作品类型一并透传给上游；R18 还要 `safe_mode=False`
      才真的生效，闸门留在服务端（见 `app/api/v1/images.py` 的参数说明）。
    - **mock 源**：标题 / 标签 / 作者，支持中文分词、拼音（全拼与首字母）与别名。
      只过滤、不重排，保证同一关键词下游标分页稳定。夹具没有收藏数与语言字段，
      因此 `min_bookmarks` / `lang` 在 mock 源下被忽略；R18 等价于 `safe_mode`。
    """
    if pixiv_source.active() and query.strip():
        return pixiv_source.search_images(
            base_url,
            query,
            favorited_ids=favorited_ids,
            safe_mode=safe_mode,
            offset=pagination.decode_cursor(
                cursor,
                search_scope(
                    query,
                    min_bookmarks=min_bookmarks,
                    lang=lang,
                    r18=r18,
                    illust_type=illust_type,
                ),
            ),
            limit=limit,
            min_bookmarks=min_bookmarks,
            lang=lang,
            allow_r18=r18 and not safe_mode,
            illust_type=illust_type,
        )
    query_tokens = search.tokens(query)
    if not query_tokens:
        return []
    hits = []
    for item in all_images(base_url, favorited_ids=favorited_ids, safe_mode=safe_mode):
        payload = item["payload"]
        forms = search.index_forms(
            item["title"],
            item.get("subtitle") or "",
            *item["tags"],
            payload.get("author") or "",
        )
        if search.matches(forms, query_tokens):
            hits.append(item)
    return hits


def games_by_filter(items: Sequence[dict[str, Any]], platform: str | None, genre: str | None) -> list[dict[str, Any]]:
    """平台 / 类型筛选。

    两者都是**精确匹配**（忽略大小写）：类型用子串匹配会让「RPG」命中「ARPG」、
    「模拟」命中「模拟经营」，与筛选条「点哪个按钮就是哪个类型」的语义不符。
    """
    result = list(items)
    if platform:
        needle = platform.strip().lower()
        result = [item for item in result if any(needle == value.lower() for value in item["payload"]["platforms"])]
    if genre:
        needle = genre.strip().lower()
        result = [item for item in result if any(needle == value.lower() for value in item["payload"]["genres"])]
    return result


def game_filters(items: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """平台 / 类型全集与计数。

    选项统计自**全部**游戏，与当前筛选条件无关：客户端因此不会因为筛选后结果集变小
    而丢掉其它选项（从当页结果聚合属于自反馈，是筛选条塌缩的根因）。
    排序为计数降序、其次取值升序，保证多次请求顺序稳定。
    """
    platforms: Counter[str] = Counter()
    genres: Counter[str] = Counter()
    for item in items:
        platforms.update(item["payload"]["platforms"])
        genres.update(item["payload"]["genres"])

    def dump(counter: Counter[str]) -> list[dict[str, Any]]:
        return [
            {"value": value, "count": count}
            for value, count in sorted(counter.items(), key=lambda row: (-row[1], row[0]))
        ]

    return {"platforms": dump(platforms), "genres": dump(genres)}


def today_games(base_url: str, *, safe_mode: bool = True) -> list[dict[str, Any]]:
    items = [item for item in all_games(base_url, safe_mode=safe_mode) if item["payload"]["today_updated"]]
    items.sort(key=lambda item: item["payload"]["updated_at"], reverse=True)
    return items


def artist_profile(user_id: str) -> dict[str, Any]:
    """作者主页信息。

    只有真实内容源（``CONTENT_SOURCE=pixiv``）能给：夹具里没有作者主页这种结构，
    编一份假的只会让界面看着能用、点进去却是空壳。mock 源下明确报不可用。
    """
    if not pixiv_source.active():
        raise AppError(
            "UPSTREAM_UNAVAILABLE", details={"reason": "content_source_mock"}
        )
    return pixiv_source.artist_profile(user_id)


def artist_works(
    base_url: str,
    user_id: str,
    *,
    offset: int = 0,
    limit: int = 20,
    favorited_ids: frozenset[str] = frozenset(),
    safe_mode: bool = True,
) -> list[dict[str, Any]]:
    """作者作品列表（按 ID 偏移分页）。"""
    if not pixiv_source.active():
        raise AppError(
            "UPSTREAM_UNAVAILABLE", details={"reason": "content_source_mock"}
        )
    return pixiv_source.artist_works(
        base_url,
        user_id,
        offset=offset,
        limit=limit,
        favorited_ids=favorited_ids,
        safe_mode=safe_mode,
    )


def artist_works_scope(user_id: str) -> str:
    """作者作品分页游标的 scope；作者换了游标就该失效。"""
    return f"images:artist:{user_id}"


def item_by_id(content_id: str, *, base_url: str, favorited: bool = False, safe_mode: bool = True) -> dict[str, Any] | None:
    if pixiv_source.active() and pixiv_adapter.is_pixiv_id(content_id):
        return pixiv_source.item_by_id(
            content_id, base_url=base_url, favorited=favorited, safe_mode=safe_mode
        )
    fixtures = load_fixtures()
    for row in fixtures.images:
        if row["id"] == content_id:
            if safe_mode and row["is_sensitive"]:
                return None
            return build_image(row, base_url=base_url, favorited=favorited)
    for row in fixtures.videos:
        if row["id"] == content_id:
            return build_video(row, base_url=base_url, favorited=favorited)
    for index, row in enumerate(fixtures.games):
        if row["id"] == content_id:
            return build_game(row, index, base_url=base_url)
    return None
