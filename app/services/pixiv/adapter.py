"""Pixiv 载荷 → 本服务 ContentItem 的映射。

设计要点（对应 docs/EXECUTION_PLAN.md 7.1–7.5）：

- **同构**：产出的字典与 ``app.services.catalog.build_image`` 完全同形，客户端只认
  ``cover_url`` / ``image_url``（都是本服务同源地址），不知道上游是 i.pximg.net。
- **两种来源字段都要吃**：``ranking.php?format=json`` 用 ``illust_id``/``user_name``/
  ``x_restrict`` 这类下划线命名，``/ajax/illust/{pid}`` 用 ``id``/``userName``/
  ``xRestrict`` 驼峰命名，这里统一。
- **敏感判定组合三个信号**（实测 ``sl=2`` 的普通插画会被误判，不能只看它）：
  ``xRestrict``（0 全年龄 / 1 R18 / 2 R18G）+ ``illust_content_type`` + 标签兜底。
- **id 前缀**：内容 ID 统一加 ``px_`` 前缀（如 ``px_123456789``），图片代理据此路由到
  pximg 回源，同时与本地夹具 ID（``img_0001``）天然隔离。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from app.core.timeutil import SHANGHAI, now

CONTENT_ID_PREFIX = "px_"

ARTWORK_URL = "https://www.pixiv.net/artworks/{pid}"

# 标签兜底：仅在 xRestrict / illust_content_type 都拿不到时才需要
SENSITIVE_TAGS = frozenset(
    {
        "R-18",
        "R18",
        "R-18G",
        "R18G",
        "R18+",
        "成人向",
        "成人向け",
        "エロ",
        "エッチ",
        "グロ",
        "NSFW",
        "SENSITIVE",
        "18禁",
    }
)

_TAG_SEPARATOR = {" ", "_", "・"}


# ---------------------------------------------------------------------- ID


def content_id_for(illust_id: Any) -> str:
    return f"{CONTENT_ID_PREFIX}{illust_id}"


def illust_id_of(content_id: str) -> str | None:
    """``px_123`` → ``123``；不是 pixiv 条目时返回 None。"""
    if not content_id.startswith(CONTENT_ID_PREFIX):
        return None
    pid = content_id[len(CONTENT_ID_PREFIX) :].strip()
    return pid if pid.isdigit() else None


def is_pixiv_id(content_id: str) -> bool:
    return illust_id_of(content_id) is not None


# ------------------------------------------------------------------ 小工具


def body_of(envelope: Mapping[str, Any] | None) -> Any:
    """取 ``{error, message, body}`` 的 body。"""
    if not isinstance(envelope, Mapping):
        return None
    return envelope.get("body")


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _optional_int(value: Any) -> int | None:
    """字段缺失返回 None 而不是 0：界面要能区分「真的是 0」与「上游没给」。
"""
    if value is None or value == "":
        return None
    return _int(value)


def _first(item: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    """按顺序取第一个存在且非空的字段（兼容下划线与驼峰两种命名）。"""
    for key in keys:
        value = item.get(key)
        if value not in (None, "", []):
            return value
    return default


def tags_of(item: Mapping[str, Any]) -> list[str]:
    """标签：``ranking`` 给字符串数组，``ajax`` 给 ``{tag, translation}`` 数组。"""
    raw = _first(item, "tags", default=[])
    result: list[str] = []
    if isinstance(raw, Mapping):
        raw = raw.get("tags") or []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return result
    for entry in raw:
        if isinstance(entry, Mapping):
            name = _text(entry.get("tag"))
        else:
            name = _text(entry)
        if name and name not in result:
            result.append(name)
    return result


def _parse_datetime(value: Any) -> datetime:
    """``2024-01-02T03:04:05+09:00``（ajax）与 ``2024-01-02 03:04:05``（ranking）。"""
    text = _text(value)
    if not text:
        return now()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return now()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI).replace(microsecond=0)


# ------------------------------------------------------------- 敏感内容判定


def _content_type_flags(item: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = _first(item, "illust_content_type", "illustContentType", default={})
    return raw if isinstance(raw, Mapping) else {}


def is_sensitive(item: Mapping[str, Any]) -> bool:
    """组合判定，宁可偏保守（多过滤）也不要漏放 R18。"""
    if _int(_first(item, "xRestrict", "x_restrict", default=0)) > 0:
        return True
    flags = _content_type_flags(item)
    for key in ("sexual", "grotesque", "violent"):
        if _int(flags.get(key, 0)) > 0:
            return True
    for tag in tags_of(item):
        normalized = tag.strip().upper().replace(" ", "")
        if normalized in SENSITIVE_TAGS:
            return True
    return False


# ------------------------------------------------------------------ 映射


def _page_count(item: Mapping[str, Any], fallback: int) -> int:
    count = _int(_first(item, "pageCount", "page_count", "illust_page_count", default=0))
    return count if count > 0 else fallback


def _illust_type(item: Mapping[str, Any]) -> int:
    """0 插画 / 1 漫画 / 2 动图。动图（ugoira）暂按插画处理，仅记录类型。"""
    return _int(_first(item, "illustType", "illust_type", default=0))


def illust_type(item: Mapping[str, Any]) -> int:
    """公开版 :func:`_illust_type`：动图筛选（``illustType == 2``）在 source 层用。"""
    return _illust_type(item)


def build_illust(
    item: Mapping[str, Any],
    *,
    base_url: str,
    favorited: bool = False,
    page_count: int = 1,
) -> dict[str, Any]:
    """把一条 Pixiv 条目映射成契约 ContentItem（形状与本地夹具一致）。"""
    from app.services.catalog import file_url  # 局部导入，避免模块级循环依赖

    pid = _text(_first(item, "id", "illust_id", "illustId", default=""))
    content_id = content_id_for(pid)
    width = _int(_first(item, "width", "illust_width", default=0)) or 0
    height = _int(_first(item, "height", "illust_height", default=0)) or 0
    pages = _page_count(item, page_count)
    thumbnail = _text(_first(item, "url", "thumbnail", default=""))
    author = _text(_first(item, "userName", "user_name", default="pixiv"))
    return {
        "id": content_id,
        "type": "image",
        "title": _text(_first(item, "title", default="无题")) or "无题",
        "subtitle": author,
        "cover_url": file_url(base_url, content_id, "thumb"),
        "tags": tags_of(item),
        "source": "pixiv",
        "source_url": ARTWORK_URL.format(pid=pid),
        "payload": {
            "author": author,
            "user_id": _text(_first(item, "userId", "user_id", default="")),
            "thumbnail_url": file_url(base_url, content_id, "thumb"),
            "image_url": file_url(base_url, content_id, "original"),
            "width": width,
            "height": height,
            "aspect_ratio": round(width / height, 4) if width and height else 1.0,
            "is_favorited": favorited,
            "created_at": _parse_datetime(_first(item, "createDate", "create_date", default="")),
            "page_count": pages,
            "illust_type": _illust_type(item),
            "author_avatar": _text(
                _first(item, "profileImageUrl", "profile_image_url", default="")
            )
            or None,
            "like_count": _optional_int(_first(item, "likeCount", "like_count", default=None)),
            "bookmark_count": _optional_int(
                _first(item, "bookmarkCount", "bookmark_count", default=None)
            ),
            "view_count": _optional_int(_first(item, "viewCount", "view_count", default=None)),
            "comment_count": _optional_int(
                _first(item, "commentCount", "comment_count", default=None)
            ),
            # 上游缩略图仅用于服务端内部（代理预热），不下发给客户端
            "_upstream_thumbnail": thumbnail,
        },
        "is_sensitive": is_sensitive(item),
    }


def strip_internal(item: dict[str, Any]) -> dict[str, Any]:
    """去掉内部字段后返回给客户端。"""
    payload = item.get("payload")
    if isinstance(payload, dict) and "_upstream_thumbnail" in payload:
        payload = dict(payload)
        payload.pop("_upstream_thumbnail", None)
        item = {**item, "payload": payload}
    return item


def search_illusts(envelope: Mapping[str, Any] | None) -> tuple[list[dict[str, Any]], int]:
    """``/ajax/search/artworks/{word}`` →（条目列表, 最后一页页码）。

    信封形状：``{error, body:{illustManga:{data:[...], total, lastPage}}}``。``data``
    每页固定 60 条，字段与 ``/ajax/illust/{pid}`` 同形（驼峰命名），因此可以直接交给
    :func:`build_illust`；``lastPage`` 用来判断还要不要继续翻页。
    """
    body = body_of(envelope)
    section: Any = None
    if isinstance(body, Mapping):
        section = body.get("illustManga") or body.get("illust")
    if not isinstance(section, Mapping):
        return [], 0
    raw = section.get("data")
    if not isinstance(raw, list):
        return [], 0
    items = [
        dict(entry)
        for entry in raw
        if isinstance(entry, Mapping) and _first(entry, "id", "illust_id") is not None
    ]
    return items, _int(section.get("lastPage", 0))


def ranking_illusts(envelope: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """``ranking.php?...&format=json`` → 条目列表（去掉广告位等非插画项）。"""
    payload = envelope if isinstance(envelope, Mapping) else {}
    contents = payload.get("contents")
    if not isinstance(contents, list):
        return []
    result: list[dict[str, Any]] = []
    for entry in contents:
        if isinstance(entry, Mapping) and _first(entry, "illust_id", "id") is not None:
            result.append(dict(entry))
    return result


def page_urls(envelope: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """``/ajax/illust/{pid}/pages`` → 每页的 urls（含 thumb_mini/small/regular/original）。"""
    raw = body_of(envelope)
    if not isinstance(raw, list):
        return []
    result: list[dict[str, str]] = []
    for entry in raw:
        urls = entry.get("urls") if isinstance(entry, Mapping) else None
        if isinstance(urls, Mapping):
            result.append({str(key): _text(value) for key, value in urls.items()})
    return result


def pick_url(urls: Mapping[str, str], variant: str) -> str | None:
    """按契约的变体名取上游链接。

    ``thumb`` 一档从 ``small``（540）起挑，**不碰** ``thumb_mini``：那档只有 128×128，
    是给作品详情页的小图用的，摆在列表卡片上必然糊（作者页「除了第一张都是糊的」
    就是这么来的）。
    """
    mapping: Iterable[str]
    if variant == "thumb":
        mapping = ("small", "regular", "thumb_mini", "original")
    elif variant == "regular":
        mapping = ("regular", "small", "original", "thumb_mini")
    else:
        mapping = ("original", "regular", "small", "thumb_mini")
    for key in mapping:
        value = urls.get(key)
        if value:
            return value
    return None


# 卡片的缩略图档位（px）。上游 ``small`` 一档就是 540，正好覆盖双列卡片
# 「240 逻辑宽 × 2 倍屏」的解码需求。
CARD_THUMB_SIZE = 540

# pximg 的尺寸段：``/c/250x250_80_a2/``、``/c/540x540_70/``、``/c/240x480/``……
_SIZE_SEGMENT = re.compile(r"/c/(\d+)x(\d+)(?:_[0-9a-z]+)*/")


def upgrade_thumb(url: str) -> str:
    """把上游给列表的缩略图抬到卡片档（见 :data:`CARD_THUMB_SIZE`）。

    各列表给的规格差得很远：发现流是 ``c/360x360_70``，作者页的作品列表给的是
    ``c/250x250_80_a2``，而作品页的 ``thumb_mini`` 只有 128×128。250 与 128 这两档
    摆在双列卡片上怎么解都糊。

    pximg 的尺寸段只是 CDN 现裁 —— 同一张原图换个前缀就能拿到更大的一档，不额外多
    一次上游请求，所以小于 540 的一律抬到 540。
    """
    trimmed = (url or "").strip()
    if not trimmed or "/img-master/" not in trimmed:
        return trimmed
    match = _SIZE_SEGMENT.search(trimmed)
    if match is None:
        return trimmed
    if int(match.group(1)) >= CARD_THUMB_SIZE and int(match.group(2)) >= CARD_THUMB_SIZE:
        return trimmed
    return f"{trimmed[:match.start()]}/c/{CARD_THUMB_SIZE}x{CARD_THUMB_SIZE}_70/{trimmed[match.end():]}"
