"""把上游音乐接口的原始字段映射成契约里的音乐模型。

上游是网易云 / QQ 音乐两个平台，同一份数据在两家字段名不同（``artists`` vs ``singer``，
``album`` vs ``album``），因此这里全部用「取第一个存在的键」的容错写法，新增平台时
只要往 ``_PLATFORM_*`` 三张表里加一行即可，不必改动调用侧。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from app.core.timeutil import SHANGHAI

# 曲目 ID 前缀：ne_3362265838 / qq_0039MnYb0qxYhV
PLATFORM_PREFIX: dict[str, str] = {"netease": "ne", "qqmusic": "qq"}
PREFIX_PLATFORM: dict[str, str] = {value: key for key, value in PLATFORM_PREFIX.items()}

# 上游歌曲页（「查看来源」用）
SOURCE_PAGES: dict[str, str] = {
    "netease": "https://music.163.com/#/song?id={id}",
    "qqmusic": "https://y.qq.com/n/ryqq/songDetail/{id}",
}

def track_id(platform: str, source_id: str) -> str:
    return f"{PLATFORM_PREFIX.get(platform, platform)}_{source_id}"


def split_track_id(value: str) -> tuple[str, str] | None:
    """``ne_123`` → ("netease", "123")；前缀不认识时返回 None。"""
    prefix, _, source_id = value.partition("_")
    platform = PREFIX_PLATFORM.get(prefix)
    if platform is None or not source_id:
        return None
    return platform, source_id


def is_music_id(value: str) -> bool:
    return split_track_id(value) is not None


def support_levels(level: str | None, default: str = "exhigh") -> list[str]:
    """请求音质的退化链：从请求档位开始，逐档下调到 standard 为止。"""
    from app.schemas.music import MUSIC_LEVELS  # 局部导入避免循环依赖

    chosen = (level or default).strip().lower()
    if chosen not in MUSIC_LEVELS:
        chosen = default if default in MUSIC_LEVELS else "standard"
    return list(MUSIC_LEVELS[MUSIC_LEVELS.index(chosen):])


def source_page_url(platform: str, source_id: str) -> str:
    template = SOURCE_PAGES.get(platform)
    if template is None:
        return ""
    return template.format(id=source_id)


def _first(raw: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in raw and raw[key] not in (None, "", []):
            return raw[key]
    return None


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _artist_names(raw: dict[str, Any]) -> list[str]:
    artists = _first(raw, "artists", "ar", "singer")
    if isinstance(artists, list):
        names = [
            str(_first(item, "name", "singerName") or "").strip()
            for item in artists
            if isinstance(item, dict)
        ]
        return [name for name in names if name]
    if isinstance(artists, str) and artists.strip():
        return [artists.strip()]
    return []


def _album_name(raw: dict[str, Any]) -> str | None:
    album = _first(raw, "album", "al", "albumName", "albumname")
    if isinstance(album, dict):
        name = _first(album, "name", "albumName")
        return str(name).strip() if name else None
    if isinstance(album, str) and album.strip():
        return album.strip()
    return None


def cover_from_detail(raw: dict[str, Any]) -> str | None:
    """从 /song/detail 的歌曲对象里取封面地址（原始尺寸，调用方按需加尺寸参数）。"""
    album = _first(raw, "al", "album")
    if isinstance(album, dict):
        url = _first(album, "picUrl", "coverUrl", "pic")
        if isinstance(url, str) and url.startswith("http"):
            return url
    url = _first(raw, "picUrl", "coverUrl")
    if isinstance(url, str) and url.startswith("http"):
        return url
    return None


#: 封面档位 -> 边长（像素）。0 表示不加尺寸参数。
COVER_SIZES: dict[str, int] = {"thumb": 200, "regular": 500, "original": 0}

#: 认 ``?param=NyN`` 尺寸参数的 CDN 后缀。
SIZED_COVER_HOSTS: tuple[str, ...] = ("music.126.net",)


def sized_cover_url(url: str, size: int) -> str:
    """给封面地址加上尺寸参数，让上游下发缩略图而不是原图。

    实测（本机直连网易云 CDN）：``al.picUrl`` 默认返回原图，单张 0.06-2.6MB、
    冷取 1-20 秒；同一张图加 ``?param=200y200`` 之后只有 8.7KB、1 秒内返回。
    列表一屏二十张封面，原图能把首屏拖到几十秒，缩略图则是眨眼的事。

    ``size`` 为 0、或地址不在已知 CDN 上时原样返回：QQ 音乐等平台不认这个参数，
    乱加查询串反而可能 404。
    """
    if size <= 0:
        return url
    host = (urlsplit(url).hostname or "").lower()
    if not any(host == suffix or host.endswith("." + suffix) for suffix in SIZED_COVER_HOSTS):
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}param={size}y{size}"


def build_track(
    raw: dict[str, Any],
    platform: str,
    *,
    base_url: str,
    detail: dict[str, Any] | None = None,
    favorited: bool = False,
) -> dict[str, Any] | None:
    """上游歌曲对象 → MusicTrack。

    ``detail`` 是 ``/song/detail`` 的同 ID 结果：搜索引擎只给 ``album.picId``，拿不到
    封面地址，所以封面统一从 detail 取；detail 缺失时封面回落到本服务的占位接口。
    """
    source_id = str(_first(raw, "id", "songmid", "mid") or "")
    if not source_id:
        return None
    merged: dict[str, Any] = dict(raw)
    if detail:
        merged.update(
            {
                key: value
                for key, value in detail.items()
                if value not in (None, "", [], {})
            }
        )
    duration = _as_int(_first(merged, "duration", "dt", "interval"), 0)
    if duration and duration < 1000:  # QQ 音乐用秒，网易云用毫秒
        duration *= 1000
    track = track_id(platform, source_id)

    return {
        "id": track,
        "platform": platform,
        "source_id": source_id,
        "title": str(_first(merged, "name", "title", "songname") or "").strip() or source_id,
        "artists": _artist_names(merged),
        "album": _album_name(merged),
        "duration_ms": max(0, duration),
        # 封面统一走本服务的同源代理：p*.music.126.net 不返回 CORS 头，
        # Flutter Web（CanvasKit）直连会直接失败。
        "cover_url": f"{base_url}/v1/music/tracks/{track}/cover",
        "source_url": source_page_url(platform, source_id),
        "fee": _as_int(_first(merged, "fee"), 0),
        "is_favorited": favorited,
        "kind": str(_first(merged, "kind") or "song"),
    }


def _comment_time(raw: dict[str, Any]) -> datetime:
    """评论时间：上游给的是 epoch（秒或毫秒），换算成契约固定的 +08:00。"""
    stamp = _as_int(_first(raw, "time", "timeUnix"), 0)
    if stamp > 10_000_000_000:
        stamp //= 1000
    if stamp <= 0:
        return datetime.now(SHANGHAI).replace(microsecond=0)
    return datetime.fromtimestamp(stamp, SHANGHAI).replace(microsecond=0)


def build_comment(raw: dict[str, Any]) -> dict[str, Any] | None:
    comment_id = str(_first(raw, "commentId", "id") or "")
    content = _first(raw, "content", "rootCommentContent")
    if not comment_id or not content:
        return None
    user = _first(raw, "user", "userInfo") or {}
    author = ""
    if isinstance(user, dict):
        author = str(_first(user, "nickname", "nick", "name") or "").strip()
    location = _first(raw, "ipLocation")
    if isinstance(location, dict):
        location = _first(location, "location")
    return {
        "id": comment_id,
        "author": author or "匿名用户",
        "content": str(content),
        "liked_count": max(0, _as_int(_first(raw, "likedCount", "liked_count"), 0)),
        "created_at": _comment_time(raw),
        "location": str(location).strip() if location else None,
    }


def build_playback(
    raw: dict[str, Any],
    *,
    track: str,
    requested_level: str,
    duration_ms: int,
) -> dict[str, Any]:
    """``/song/url/v1`` 的单条结果 → MusicPlayback。"""
    trial = _first(raw, "freeTrialInfo")
    trial_end = None
    if isinstance(trial, dict):
        trial_end = _as_int(_first(trial, "end"), 0) or None
    return {
        "id": track,
        "stream_url": f"/v1/music/tracks/{track}/stream",
        "mime_type": f"audio/{_first(raw, 'type', 'encodeType') or 'mpeg'}",
        "bitrate": max(0, _as_int(_first(raw, "br"), 0)),
        "duration_ms": max(0, duration_ms or _as_int(_first(raw, "time"), 0)),
        "requested_level": requested_level,
        "effective_level": str(_first(raw, "level") or "standard"),
        "is_trial": bool(trial),
        "trial_end_ms": trial_end,
    }
