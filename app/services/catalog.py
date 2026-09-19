"""内容编目：把夹具数据装配成契约约定的 ContentItem。

所有链接都是本服务同源地址，客户端不感知来源平台。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Iterable, Sequence

from app.core.timeutil import hours_ago, now, today
from app.fixtures.dataset import load_fixtures

DAILY_IMAGE_COUNT = 8
BILIBILI_COUNT = 12
TODAY_UPDATED_GAMES = 6


def file_url(base_url: str, content_id: str, variant: str = "thumb", page: int = 0) -> str:
    url = f"{base_url}/v1/images/{content_id}/file?variant={variant}"
    if page:
        url += f"&page={page}"
    return url


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
    rows = sorted(load_fixtures().images, key=lambda row: row["ago_hours"])
    items = [build_image(row, base_url=base_url, favorited=row["id"] in favorited_ids) for row in rows]
    return _visible(items, safe_mode)


def all_videos(base_url: str, *, safe_mode: bool = True) -> list[dict[str, Any]]:
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
    if not tag:
        return list(items)
    needle = tag.strip().lower()
    if not needle:
        return list(items)
    return [
        item
        for item in items
        if any(needle in value.lower() for value in item["tags"])
        or needle in item["title"].lower()
    ]


def search_images(base_url: str, query: str, *, favorited_ids: frozenset[str] = frozenset(), safe_mode: bool = True) -> list[dict[str, Any]]:
    needle = query.strip().lower()
    if not needle:
        return []
    hits = []
    for item in all_images(base_url, favorited_ids=favorited_ids, safe_mode=safe_mode):
        haystack = [item["title"], item.get("subtitle") or "", *item["tags"]]
        payload = item["payload"]
        haystack.append(payload.get("author") or "")
        if any(needle in value.lower() for value in haystack):
            hits.append(item)
    return hits


def games_by_filter(items: Sequence[dict[str, Any]], platform: str | None, genre: str | None) -> list[dict[str, Any]]:
    result = list(items)
    if platform:
        needle = platform.strip().lower()
        result = [item for item in result if any(needle == value.lower() for value in item["payload"]["platforms"])]
    if genre:
        needle = genre.strip().lower()
        result = [item for item in result if any(needle in value.lower() for value in item["payload"]["genres"])]
    return result


def today_games(base_url: str, *, safe_mode: bool = True) -> list[dict[str, Any]]:
    items = [item for item in all_games(base_url, safe_mode=safe_mode) if item["payload"]["today_updated"]]
    items.sort(key=lambda item: item["payload"]["updated_at"], reverse=True)
    return items


def item_by_id(content_id: str, *, base_url: str, favorited: bool = False, safe_mode: bool = True) -> dict[str, Any] | None:
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
