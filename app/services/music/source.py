"""音乐数据源：进程级配置 + 取数 + 缓存。

``MUSIC_SOURCE`` 两种取值：

- ``mock``（默认）：本地夹具，离线可用，覆盖搜索 / 榜单 / 详情 / 歌词 / 评论；
  音频流与封面里的真实 CDN 地址只有 ``node`` 模式才有。
- ``node``：仓库同级的 ``multiPlatformMusicApi`` 服务（默认 ``127.0.0.1:19531``），
  字段与真实上游一致。

缓存存在的理由：搜索引擎只返回 ``album.picId``，拿不到封面地址，必须再打一次
``/song/detail`` 批量补全；封面接口 ``/v1/music/tracks/{id}/cover`` 与详情接口都要
复用同一份 detail 结果，否则列表滚动会反复打上游。TTL 到期即失效。

进程级状态是刻意的：配置在 ``create_app`` 时注入一次，之后只读；测试用 ``configure``
/ ``reset`` 显式控制。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.config import Settings
from app.core.bytecache import ByteCache
from app.core.errors import AppError, not_found
from app.core.timeutil import now
from app.fixtures import images as sample_images
from app.repositories import store
from app.services.music import adapter
from app.services.music.client import MusicClient

logger = logging.getLogger("miniappbackport.music")

# 曲目 ID 不合法 / 上游查不到时统一用这个错误
_TRACK_NOT_FOUND = "track_not_found"

# mock 夹具：ID 与网易云真实 ID 同形，方便切到 node 模式时对照
MOCK_TRACKS: tuple[dict[str, Any], ...] = (
    {
        "id": "ne_347230",
        "title": "海阔天空",
        "artists": ["Beyond"],
        "album": "乐与怒",
        "duration_ms": 313062,
        "source_url": "https://music.163.com/#/song?id=347230",
        "fee": 0,
    },
    {
        "id": "ne_186016",
        "title": "以父之名",
        "artists": ["周杰伦"],
        "album": "叶惠美",
        "duration_ms": 343000,
        "source_url": "https://music.163.com/#/song?id=186016",
        "fee": 1,
    },
    {
        "id": "ne_1330348068",
        "title": "起风了",
        "artists": ["买辣椒也用券"],
        "album": "起风了",
        "duration_ms": 326000,
        "source_url": "https://music.163.com/#/song?id=1330348068",
        "fee": 0,
    },
    {
        "id": "ne_202369",
        "title": "夜曲",
        "artists": ["周杰伦"],
        "album": "十一月的萧邦",
        "duration_ms": 228000,
        "source_url": "https://music.163.com/#/song?id=202369",
        "fee": 1,
    },
    {
        "id": "ne_28815250",
        "title": "晴天",
        "artists": ["周杰伦"],
        "album": "叶惠美",
        "duration_ms": 269000,
        "source_url": "https://music.163.com/#/song?id=28815250",
        "fee": 1,
    },
    {
        "id": "ne_569213220",
        "title": "病态",
        "artists": ["解忧少帅"],
        "album": "病态",
        "duration_ms": 214000,
        "source_url": "https://music.163.com/#/song?id=569213220",
        "fee": 0,
    },
)

MOCK_LYRIC = "[00:00.00]（mock 歌词）\n[00:02.00]切换到 MUSIC_SOURCE=node 即可取到真实歌词\n"
MOCK_COMMENT = "（mock 评论）这条数据来自本地夹具，不代表真实上游。"

#: 「上游给了 HTTP 200 但整条音质链都没有可播地址」时的重试次数。
#: 见 [`resolve_playback`]：上游的解析是非确定性的，多问一次常常就好了。
_RESOLVE_ATTEMPTS = 2

#: 上面那次重试之间的间隔。上游抖动是秒级的，200 毫秒足够跨过去，
#: 又不至于让「这首真的没有音源」多等太久。
_RESOLVE_RETRY_DELAY_SECONDS = 0.2

#: 复用解析结果的时长（秒）。上游直链约 1200 秒过期，这里取一个明显更短的窗口：
#: 播放几乎总是紧接着 `/playback` 发生，几分钟足够覆盖「暂停一会儿再继续听」。
_RESOLVED_TTL_SECONDS = 300.0

#: 解析结果缓存的条数上限；超了整体清空（这里只防无限增长，不做精细 LRU）。
_RESOLVED_MAX_ENTRIES = 512

_settings: Settings | None = None
_client: MusicClient | None = None
_detail_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_lyric_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_comment_cache: dict[str, tuple[float, dict[str, Any]]] = {}
#: `/playback` 解析出来的上游直链，键是 ``(platform, source_id, level)``。
#:
#: 为什么要有它：`/stream` 以前会**再解析一次**，而上游的解析是非确定性的 ——
#: 实测同一首曲子第一次给地址、紧接着第二次回 `url: null`（36 首里撞到 2 次）。
#: 结果是 `/playback` 明明成功、紧随其后的 `/stream` 却 503，用户看到「音乐突然
#: 加载不出来」，重新打开页面又好了。复用同一条地址之后这类分裂直接消失，
#: 顺带把上游调用量减半。
_resolved_cache: dict[
    tuple[str, str, str], tuple[float, tuple[dict[str, Any], str]]
] = {}
_COVER_CACHE: ByteCache = ByteCache(max_bytes=64 * 1024 * 1024, max_entries=1024)
_last_error: str | None = None


# ------------------------------------------------------------------ 装配


def configure(settings: Settings, *, client: MusicClient | None = None) -> None:
    """启动时注入配置（``create_app`` 调用）；``client`` 供测试注入假传输。"""
    global _settings, _client, _last_error
    _settings = settings
    _client = client or (MusicClient(settings) if active_for(settings) else None)
    _last_error = None
    if active_for(settings):
        logger.info("音乐源已切换为 node（%s）", settings.music_api_base)


def active_for(settings: Settings) -> bool:
    """是否使用真实音乐上游。"""
    return settings.music_source.strip().lower() == "node"


def active() -> bool:
    return _settings is not None and active_for(_settings)


def is_mock() -> bool:
    return _settings is not None and not active_for(_settings)


def reset() -> None:
    """清缓存（测试与 /v1/dev/reset 用）。"""
    _detail_cache.clear()
    _lyric_cache.clear()
    _comment_cache.clear()
    _resolved_cache.clear()
    _COVER_CACHE.clear()


def last_error() -> str | None:
    return _last_error


# ------------------------------------------------------------------ 查询


def search_tracks(
    base_url: str,
    query: str,
    *,
    platform: str | None = None,
    limit: int = 20,
    offset: int = 0,
    favorited_ids: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    resolved = _platform(platform)
    if is_mock():
        return _mock_list(base_url, favorited_ids, limit=limit, offset=offset)
    client = _require_client()
    payload = client.get_json(
        "/search",
        params={
            "keywords": query,
            "platform": resolved,
            "limit": limit,
            "offset": offset,
            "type": 1,
        },
    )
    tracks = _adapt_songs(base_url, resolved, _song_list(payload), favorited_ids)
    if offset == 0:
        # 合并只在第一页做：翻页时再插一遍，同一首歌会在列表里出现两次。
        tracks.extend(
            _merged_tracks(
                client,
                base_url,
                resolved,
                query,
                favorited_ids,
                exclude={track["id"] for track in tracks},
            )
        )
    return tracks


def top_tracks(
    base_url: str,
    *,
    platform: str | None = None,
    limit: int = 20,
    favorited_ids: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """每日音乐：走平台的「新歌/热歌」榜，替代原先的资源页。"""
    resolved = _platform(platform)
    if is_mock():
        return _mock_list(base_url, favorited_ids, limit=limit, offset=0)
    client = _require_client()
    payload = client.get_json("/top/song", params={"platform": resolved})
    return _adapt_songs(base_url, resolved, _song_list(payload), favorited_ids)[:limit]


def track_detail(
    base_url: str,
    track_id: str,
    *,
    favorited: bool = False,
) -> dict[str, Any]:
    parsed = adapter.split_track_id(track_id)
    if parsed is None:
        raise not_found({"id": track_id, "reason": _TRACK_NOT_FOUND})
    platform, source_id = parsed
    if is_mock():
        return _mock_track(base_url, track_id, favorited=favorited)
    detail = _load_details(platform, [source_id]).get(source_id)
    if detail is None:
        raise not_found({"id": track_id, "reason": _TRACK_NOT_FOUND})
    track = adapter.build_track(
        detail, platform, base_url=base_url, detail=detail, favorited=favorited
    )
    if track is None:
        raise not_found({"id": track_id, "reason": _TRACK_NOT_FOUND})
    return track


def lyric_for(track_id: str) -> dict[str, Any]:
    parsed = adapter.split_track_id(track_id)
    if parsed is None:
        raise not_found({"id": track_id, "reason": _TRACK_NOT_FOUND})
    platform, source_id = parsed
    if is_mock():
        return {"id": track_id, "lyric": MOCK_LYRIC, "translated_lyric": None}
    key = f"{platform}:{source_id}"
    cached = _fresh(_lyric_cache, key)
    if cached is not None:
        return cached
    client = _require_client()
    payload = client.get_json("/lyric", params={"id": source_id, "platform": platform})
    lrc = payload.get("lrc") if isinstance(payload.get("lrc"), dict) else {}
    translated = payload.get("tlyric") if isinstance(payload.get("tlyric"), dict) else {}
    result = {
        "id": track_id,
        "lyric": str((lrc or {}).get("lyric") or ""),
        "translated_lyric": str((translated or {}).get("lyric") or "") or None,
    }
    _lyric_cache[key] = (time.monotonic() + _ttl(), result)
    return result


def comments_for(track_id: str, *, limit: int = 20) -> dict[str, Any]:
    parsed = adapter.split_track_id(track_id)
    if parsed is None:
        raise not_found({"id": track_id, "reason": _TRACK_NOT_FOUND})
    platform, source_id = parsed
    if is_mock():
        return {
            "items": [
                {
                    "id": f"{track_id}_c1",
                    "author": "本地夹具",
                    "content": MOCK_COMMENT,
                    "liked_count": 0,
                    "created_at": now(),
                    "location": "上海",
                }
            ],
            "total": 1,
        }
    key = f"{platform}:{source_id}:{limit}"
    cached = _fresh(_comment_cache, key)
    if cached is not None:
        return cached
    client = _require_client()
    payload = client.get_json(
        "/comment/hot",
        params={"id": source_id, "type": 0, "platform": platform, "limit": limit},
    )
    raw = payload.get("hotComments")
    if not isinstance(raw, list) or not raw:
        raw = payload.get("topComments")
    items = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        built = adapter.build_comment(entry)
        if built is not None:
            items.append(built)
    result = {
        "items": items[:limit],
        "total": max(_as_int(payload.get("total"), len(items)), len(items)),
    }
    _comment_cache[key] = (time.monotonic() + _ttl(), result)
    return result


def resolve_playback(track_id: str, *, level: str | None = None) -> tuple[dict[str, Any], str]:
    """解析播放地址，返回 (MusicPlayback, 上游直链)。

    上游直链带签名且约 20 分钟过期，只在进程内传给流式代理，绝不下发客户端。

    上游的**解析结果是非确定性的**：实测同一首曲子这一秒给地址、下一秒回
    `url: null`（36 首里撞到 2 次），而 `MusicClient._request` 的重试只覆盖
    HTTP / 传输层失败，管不到这种「HTTP 200 + 空地址」。整条音质链因此再补一次
    重试 —— 用户看到的是「音乐突然加载不出来、重开页面又好了」，这一次重试正是
    把「重开页面」搬到服务端来做；配合音乐服务的 eapi 指纹轮换，重试时用的已经是
    新指纹，命中率更高。
    """
    parsed = adapter.split_track_id(track_id)
    if parsed is None:
        raise not_found({"id": track_id, "reason": _TRACK_NOT_FOUND})
    platform, source_id = parsed
    if is_mock():
        raise AppError(
            "UPSTREAM_UNAVAILABLE",
            details={"reason": "music_source_mock", "hint": "MUSIC_SOURCE=node 才能播放"},
        )
    requested = (level or _settings_default_level()).strip().lower()
    attempts = max(1, _RESOLVE_ATTEMPTS)
    for attempt in range(attempts):
        resolved = _resolve_once(platform, source_id, track_id, requested)
        if resolved is not None:
            playback, url = resolved
            _remember_resolved(platform, source_id, requested, playback, url)
            return playback, url
        if attempt < attempts - 1:
            logger.info("音乐音源解析为空，重试：%s", track_id)
            time.sleep(_RESOLVE_RETRY_DELAY_SECONDS)
    raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": "no_playable_url"})


def _resolve_once(
    platform: str, source_id: str, track_id: str, requested: str
) -> tuple[dict[str, Any], str] | None:
    """按音质退化链问一遍上游；整条链都没有可播地址时返回 None。"""
    client = _require_client()
    detail = _load_details(platform, [source_id]).get(source_id) or {}
    duration = _duration_ms(detail)
    for candidate in adapter.support_levels(requested, _settings_default_level()):
        payload = client.get_json(
            "/song/url/v1",
            params={"id": source_id, "level": candidate, "platform": platform},
        )
        entries = payload.get("data")
        entry = entries[0] if isinstance(entries, list) and entries else None
        if isinstance(entry, dict) and entry.get("url"):
            playback = adapter.build_playback(
                entry, track=track_id, requested_level=requested, duration_ms=duration
            )
            return playback, str(entry["url"])
    return None


def cover_bytes(track_id: str, *, variant: str = "thumb") -> tuple[bytes, str] | None:
    """封面字节。mock 模式回落到本地示例图，保证离线也能看到列表。

    ``variant`` 决定向上游要多大：``thumb``（默认，200×200，列表用）、``regular``
    （500×500）、``original``。网易云默认给的是原图，一屏二十张能把首屏拖到几十秒。
    """
    parsed = adapter.split_track_id(track_id)
    if parsed is None:
        return None
    platform, source_id = parsed
    if is_mock():
        path = sample_images.image_path("img_0001", "thumb", 0)
        if path is None:
            return None
        return path.read_bytes(), "image/jpeg"
    detail = _load_details(platform, [source_id]).get(source_id) or {}
    url = adapter.cover_from_detail(detail)
    if not url:
        return None
    size = adapter.COVER_SIZES.get(variant, adapter.COVER_SIZES["thumb"])
    url = adapter.sized_cover_url(url, size)
    # 封面按 URL 缓存：列表滚回来、退出详情再进来都会重复要同一张图，实测回源
    # 缩略图单张 8.7KB / 1 秒内，命中缓存后是 0 毫秒；并发重复只回源一次。
    return _COVER_CACHE.load(url, lambda: _fetch_cover(url))


def _fetch_cover(url: str) -> tuple[bytes, str]:
    """真正回源取封面；只在缓存未命中时被调用一次。"""
    client = _require_client()
    handle, response = client.open_media(url)
    try:
        content_type = response.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        return response.read(), content_type or "image/jpeg"
    finally:
        response.close()
        handle.close()


def open_stream(
    track_id: str, *, level: str | None = None, range_header: str | None = None
) -> tuple[dict[str, Any], Any, Any]:
    """打开音频流，返回 (MusicPlayback, 上游连接, 上游响应)。

    字节转发在路由层完成：这里只解析地址并建连，调用方负责关闭连接与响应。

    优先复用 `/playback` 刚刚解析出来、并且已经下发过签名的那条地址：再解析一次
    既多一趟上游往返，又可能撞上「HTTP 200 + 空地址」而把一次本来能播的播放判死
    （这正是「点进去能放、过一会儿突然加载不出来」的来源之一）。缓存里的地址取不到
    时丢掉它重解析一次，仍然失败才如实报错。
    """
    key = _cache_key(track_id, level)
    if key is not None:
        cached = _fresh(_resolved_cache, key)
        if cached is not None:
            playback, url = cached
            try:
                handle, response = _require_client().open_media(
                    url, range_header=range_header
                )
            except AppError:
                # 地址过期 / 被上游回收：丢掉再解析一次，别让缓存把播放卡死。
                _resolved_cache.pop(key, None)
            else:
                return playback, handle, response
    playback, url = resolve_playback(track_id, level=level)
    handle, response = _require_client().open_media(url, range_header=range_header)
    return playback, handle, response


# ------------------------------------------------------------------ 解析结果复用


def _parsed_key(track_id: str) -> tuple[str, str]:
    """``ne_123`` → ``("netease", "123")``；解析不了就是空串对。"""
    parsed = adapter.split_track_id(track_id)
    return parsed if parsed is not None else ("", "")


def _cache_key(track_id: str, level: str | None) -> tuple[str, str, str] | None:
    """解析结果的缓存键 ``(platform, source_id, level)``；曲目 ID 认不出来就是 None。

    音质档位按 [`resolve_playback`] 的同一套归一化取，保证 `/playback` 存进去的
    和 `/stream` 取出来的是同一个键。
    """
    platform, source_id = _parsed_key(track_id)
    if not source_id:
        return None
    return platform, source_id, (level or _settings_default_level()).strip().lower()


def _remember_resolved(
    platform: str, source_id: str, level: str, playback: dict[str, Any], url: str
) -> None:
    """记住刚解析出来的地址（见 [_resolved_cache] 的说明）。"""
    if len(_resolved_cache) > _RESOLVED_MAX_ENTRIES:
        _resolved_cache.clear()
    _resolved_cache[(platform, source_id, level)] = (
        time.monotonic() + _RESOLVED_TTL_SECONDS,
        (playback, url),
    )


# ------------------------------------------------------------------ 收藏


def favorited_ids(user_id: str) -> frozenset[str]:
    return frozenset(store.list_music_favorites(user_id))


def set_favorite(
    user_id: str, track_id: str, favorited: bool, snapshot: dict[str, Any] | None = None
) -> dict[str, Any]:
    state = store.set_music_favorite(user_id, track_id, favorited, snapshot)
    return {"id": track_id, "is_favorited": state}


def favorite_tracks(base_url: str, user_id: str) -> list[dict[str, Any]]:
    """收藏夹列表：直接读内存快照，不再回源上游。"""
    snapshots = store.list_music_favorites(user_id)
    return [{**item, "is_favorited": True} for item in snapshots.values()]


# ------------------------------------------------------------------ 歌单（收藏夹）


def list_playlists(user_id: str) -> list[dict[str, Any]]:
    """歌单列表。顺带保证默认歌单存在：列表页永远至少有一个可用的收藏夹。"""
    store.default_music_playlist_id(user_id)
    return store.list_music_playlists(user_id)


def get_playlist(user_id: str, playlist_id: str) -> dict[str, Any] | None:
    return store.get_music_playlist(user_id, playlist_id)


def create_playlist(user_id: str, name: str) -> dict[str, Any]:
    return store.create_music_playlist(user_id, name)


def delete_playlist(user_id: str, playlist_id: str) -> bool:
    return store.delete_music_playlist(user_id, playlist_id)


def playlist_tracks(user_id: str, playlist_id: str) -> list[dict[str, Any]]:
    """歌单里的曲目：同样只读内存快照。"""
    snapshots = store.music_playlist_tracks(user_id, playlist_id)
    return [{**item, "is_favorited": True} for item in snapshots.values()]


def set_playlist_track(
    user_id: str,
    playlist_id: str,
    track_id: str,
    member: bool,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    in_playlist = store.set_music_playlist_track(
        user_id, playlist_id, track_id, member, snapshot
    )
    return {
        "playlist_id": playlist_id,
        "track_id": track_id,
        "in_playlist": in_playlist,
        "track_count": len(store.music_playlist_tracks(user_id, playlist_id)),
    }


# ------------------------------------------------------------------ 内部


def _settings_default_level() -> str:
    return _settings.music_default_level if _settings is not None else "exhigh"


def _platform(value: str | None) -> str:
    if value:
        return value.strip().lower()
    if _settings is not None and _settings.music_default_platform.strip():
        return _settings.music_default_platform.strip().lower()
    return "netease"


def _ttl() -> float:
    return float(_settings.music_cache_ttl_seconds if _settings is not None else 1800)


def _fresh(cache: dict[Any, tuple[float, Any]], key: Any) -> Any | None:
    entry = cache.get(key)
    if entry is None:
        return None
    expires, value = entry
    if expires <= time.monotonic():
        cache.pop(key, None)
        return None
    return value


def _require_client() -> MusicClient:
    if _client is None:
        raise AppError(
            "UPSTREAM_UNAVAILABLE", details={"reason": "music_service_not_configured"}
        )
    return _client


def _source_id(raw: dict[str, Any]) -> str:
    for key in ("id", "songmid", "mid"):
        value = raw.get(key)
        if value not in (None, "", 0, "0"):
            return str(value)
    return ""


def _song_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """上游歌曲数组在三种位置：``songs`` / ``result.songs`` / ``data``。"""
    direct = payload.get("songs")
    if isinstance(direct, list):
        return [item for item in direct if isinstance(item, dict)]
    for key in ("result", "data"):
        node = payload.get(key)
        if isinstance(node, list):
            return [item for item in node if isinstance(item, dict)]
        if isinstance(node, dict) and isinstance(node.get("songs"), list):
            return [item for item in node["songs"] if isinstance(item, dict)]
    return []


def _load_details(platform: str, ids: list[str]) -> dict[str, dict[str, Any]]:
    """批量补全歌曲详情（封面、时长都从这里来），命中缓存的不再回源。"""
    wanted = [value for value in ids if value]
    if not wanted:
        return {}
    found: dict[str, dict[str, Any]] = {}
    pending: list[str] = []
    for source_id in wanted:
        cached = _fresh(_detail_cache, f"{platform}:{source_id}")
        if cached is None:
            pending.append(source_id)
        else:
            found[source_id] = cached
    if pending:
        client = _require_client()
        payload = client.get_json(
            "/song/detail", params={"ids": ",".join(pending), "platform": platform}
        )
        expires = time.monotonic() + _ttl()
        for song in _song_list(payload):
            source_id = _source_id(song)
            if not source_id:
                continue
            _detail_cache[f"{platform}:{source_id}"] = (expires, song)
            found[source_id] = song
    return found


DETAIL_BATCH_SIZE = 50


def _load_details_soft(platform: str, ids: list[str]) -> dict[str, dict[str, Any]]:
    """列表页的详情补全：上游抖动时退化成「这一批没有封面」，不让整页 503。

    ``/song/detail`` 在这里只用来补封面（搜索引擎只给 ``album.picId``，拼不出地址），
    标题 / 歌手 / 时长在 ``/search``、``/top/song`` 里已经有了。所以这个补充失败完全
    可以安全降级——之前它一失败就把整个 /v1/music/search、/v1/music/daily 打成 503，
    帖吧页直接空白，属于放大了上游故障。

    按批请求而不是一次梭哈：网易云热歌榜有 100 条，一次请求失败就全丢；分批之后
    只有出问题的那几十条没封面。
    """

    wanted = [value for value in ids if value]
    if not wanted:
        return {}
    found: dict[str, dict[str, Any]] = {}
    for start in range(0, len(wanted), DETAIL_BATCH_SIZE):
        batch = wanted[start : start + DETAIL_BATCH_SIZE]
        try:
            found.update(_load_details(platform, batch))
        except AppError as error:
            logger.warning("歌曲详情补全失败（%s 条）已降级：%s", len(batch), error)
    return found


# ------------------------------------------------------------------ 搜索合并
#
# 搜索结果里除了单曲，还把网易云的「声音」（播客单集）与「电台」节目一起带回来。
# 实测（multiPlatformMusicApi）：
# - ``type=2000`` 声音 → ``result.data.resources[*].baseInfo.mainSong`` 本身就是一首歌；
# - ``type=1009`` 电台 → 只给电台本身（不可播），要再打 ``/dj/program`` 取节目，
#   节目的 ``mainSong`` 同样是一首歌。
# 两类都规整成单曲形状后走同一条播放链路（``/song/url/v1``），播放 / 歌词 / 评论三个
# 接口因此不用为它们单开分支。合并失败只记日志，绝不影响单曲结果。
MERGE_VOICE_LIMIT = 4
MERGE_RADIO_LIMIT = 2
MERGE_RADIO_PROGRAM_LIMIT = 1


def _merged_tracks(
    client: MusicClient,
    base_url: str,
    platform: str,
    query: str,
    favorited_ids: frozenset[str],
    *,
    exclude: set[str],
) -> list[dict[str, Any]]:
    """电台 / 声音的合并结果。只有网易云有这两种形态。"""
    if platform != "netease":
        return []
    try:
        songs = _voice_songs(client, query) + _radio_program_songs(client, query)
    except AppError as error:
        logger.warning("搜索合并（电台 / 声音）失败，只返回单曲：%s", error)
        return []
    adapted = _adapt_songs(base_url, platform, songs, favorited_ids, enrich=False)
    return [track for track in adapted if track["id"] not in exclude]


def _voice_songs(client: MusicClient, query: str) -> list[dict[str, Any]]:
    payload = client.get_json(
        "/search",
        params={
            "keywords": query,
            "platform": "netease",
            "limit": MERGE_VOICE_LIMIT,
            "offset": 0,
            "type": 2000,
        },
    )
    result = payload.get("result")
    data = result.get("data") if isinstance(result, dict) else None
    resources = data.get("resources") if isinstance(data, dict) else None
    if not isinstance(resources, list):
        return []
    songs: list[dict[str, Any]] = []
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        base = resource.get("baseInfo")
        song = base.get("mainSong") if isinstance(base, dict) else None
        if not isinstance(song, dict):
            continue
        built = _merge_song(song, kind="voice")
        if built is not None:
            songs.append(built)
    return songs


def _radio_program_songs(client: MusicClient, query: str) -> list[dict[str, Any]]:
    payload = client.get_json(
        "/search",
        params={
            "keywords": query,
            "platform": "netease",
            "limit": MERGE_RADIO_LIMIT,
            "offset": 0,
            "type": 1009,
        },
    )
    result = payload.get("result")
    radios = result.get("djRadios") if isinstance(result, dict) else None
    if not isinstance(radios, list):
        return []
    songs: list[dict[str, Any]] = []
    for radio in radios[:MERGE_RADIO_LIMIT]:
        if not isinstance(radio, dict):
            continue
        radio_id = radio.get("id")
        if radio_id in (None, "", 0, "0"):
            continue
        radio_name = str(radio.get("name") or "").strip()
        try:
            programs = client.get_json(
                "/dj/program",
                params={
                    "rid": radio_id,
                    "limit": MERGE_RADIO_PROGRAM_LIMIT,
                    "offset": 0,
                    "platform": "netease",
                },
            )
        except AppError:
            # 某一个电台取不到节目，其它结果照常返回
            continue
        entries = programs.get("programs")
        for program in entries if isinstance(entries, list) else []:
            if not isinstance(program, dict):
                continue
            song = program.get("mainSong")
            if not isinstance(song, dict):
                continue
            built = _merge_song(
                song,
                kind="radio",
                fallback_artist=radio_name,
                title=str(program.get("name") or "").strip() or None,
            )
            if built is not None:
                songs.append(built)
    return songs


def _merge_song(
    song: dict[str, Any],
    *,
    kind: str,
    fallback_artist: str | None = None,
    title: str | None = None,
) -> dict[str, Any] | None:
    """把声音 / 节目里的 ``mainSong`` 规整成 ``/search`` 的单曲形状。"""
    if _source_id(song) == "":
        return None
    raw_album = song.get("album")
    album: dict[str, Any] = raw_album if isinstance(raw_album, dict) else {}
    return {
        "id": song.get("id"),
        "name": title or str(song.get("name") or "").strip(),
        "ar": [{"name": name} for name in _merge_artists(song, album, fallback_artist)],
        "al": {
            "name": str(album.get("name") or "").strip(),
            "picUrl": str(album.get("picUrl") or "").strip(),
        },
        "dt": _as_int(song.get("duration") or song.get("dt"), 0),
        "fee": song.get("fee"),
        "kind": kind,
    }


def _merge_artists(
    song: dict[str, Any], album: dict[str, Any], fallback: str | None
) -> list[str]:
    """歌手名依次从歌曲、专辑的 ``artists`` / ``ar`` 里找，都没有才用兜底（电台名）。"""
    for source in (
        song.get("artists"),
        song.get("ar"),
        album.get("artists"),
        album.get("ar"),
    ):
        names: list[str] = []
        if isinstance(source, list):
            names = [
                str(item.get("name") or "").strip()
                for item in source
                if isinstance(item, dict)
            ]
        elif isinstance(source, str):
            names = [source.strip()]
        names = [name for name in names if name]
        if names:
            return names
    return [fallback] if fallback else []


def _adapt_songs(
    base_url: str,
    platform: str,
    songs: list[dict[str, Any]],
    favorited_ids: frozenset[str],
    *,
    enrich: bool = True,
) -> list[dict[str, Any]]:
    # 合并进来的电台 / 声音自带完整歌曲字段，不能再打 /song/detail：那个接口对
    # 「声音」只回空的 name / artists，覆盖上去反而把标题抹掉。
    details = (
        _load_details_soft(platform, [_source_id(song) for song in songs]) if enrich else {}
    )
    tracks: list[dict[str, Any]] = []
    for song in songs:
        source_id = _source_id(song)
        if not source_id:
            continue
        track = adapter.build_track(
            song,
            platform,
            base_url=base_url,
            detail=details.get(source_id),
            favorited=adapter.track_id(platform, source_id) in favorited_ids,
        )
        if track is not None:
            tracks.append(track)
    return tracks


def _duration_ms(detail: dict[str, Any]) -> int:
    value = _as_int(detail.get("dt") or detail.get("duration") or detail.get("interval"), 0)
    if value and value < 1000:
        value *= 1000
    return max(0, value)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _mock_track(base_url: str, track_id: str, *, favorited: bool) -> dict[str, Any]:
    parsed = adapter.split_track_id(track_id)
    if parsed is None:
        raise not_found({"id": track_id, "reason": _TRACK_NOT_FOUND})
    platform, source_id = parsed
    for entry in MOCK_TRACKS:
        if entry["id"] == track_id:
            return {
                **entry,
                "platform": platform,
                "source_id": source_id,
                "cover_url": f"{base_url}/v1/music/tracks/{track_id}/cover",
                "is_favorited": favorited,
            }
    raise not_found({"id": track_id, "reason": _TRACK_NOT_FOUND})


def _mock_list(
    base_url: str, favorited_ids: frozenset[str], *, limit: int, offset: int
) -> list[dict[str, Any]]:
    tracks = [
        _mock_track(base_url, entry["id"], favorited=entry["id"] in favorited_ids)
        for entry in MOCK_TRACKS
    ]
    return tracks[offset : offset + limit]
