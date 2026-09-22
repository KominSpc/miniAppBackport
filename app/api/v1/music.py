"""音乐页接口：搜索、榜单、详情、歌词、评论、封面、音频流与收藏夹。

所有响应走同一套 {data, meta, error} 信封，``meta.source`` 固定为 ``music``。

音频与封面都不下发给客户端上游直链：``m801.music.126.net`` 这类 CDN 既不返回 CORS 头
（Flutter Web 直连必然失败），直链本身还会在约 20 分钟后过期。客户端拿到的永远是本服务
的同源地址（见 app/services/music/source.py）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, Path, Query
from fastapi.responses import Response, StreamingResponse

from app.api.v1.common import error_responses
from app.core import envelope
from app.core.errors import bad_request, forbidden, not_found
from app.deps import BaseUrlDep, CurrentUser, SettingsDep
from app.schemas.envelopes import (
    EnvelopeMusicCommentList,
    EnvelopeMusicFavoriteState,
    EnvelopeMusicLyric,
    EnvelopeMusicPlayback,
    EnvelopeMusicPlaylist,
    EnvelopeMusicPlaylistList,
    EnvelopeMusicPlaylistState,
    EnvelopeMusicTrack,
    EnvelopeMusicTrackList,
)
from app.schemas.music import MusicPlaylistCreate
from app.services.music import adapter as music_adapter
from app.services.music import recommend as music_recommend
from app.services.music import signature as music_signature
from app.services.music import source as music_source

router = APIRouter(prefix="/v1/music", tags=["music"])

SOURCE = "music"
STREAM_CHUNK = 64 * 1024
# 上游 CDN 的分段响应头：原样透给客户端，播放器才能拖动进度条
FORWARDED_HEADERS = ("content-length", "content-range", "accept-ranges")


def _is_encoded(content_encoding: str | None) -> bool:
    """上游是否**真的**压缩了响应体（缺省与 `identity` 都算没压）。"""
    if not content_encoding:
        return False
    return content_encoding.strip().lower() not in ("identity", "")

TrackIdPath = Annotated[
    str, Path(description="曲目 ID，形如 ne_347230（平台前缀 + 上游歌曲 ID）")
]

PlaylistIdPath = Annotated[str, Path(description="歌单 ID，形如 pl_ab12cd34ef56")]

KeywordQuery = Annotated[
    str, Query(min_length=1, max_length=60, description="搜索关键词，如「稻香 周杰伦」")
]

PlatformQuery = Annotated[
    str | None, Query(description="音乐平台：netease（默认）/ qqmusic")
]

LevelQuery = Annotated[
    str | None,
    Query(
        description=(
            "音质：jymaster / hires / lossless / exhigh / standard，默认取服务端配置"
            "（MUSIC_DEFAULT_LEVEL）。上游未登录时会忽略该参数并统一返回 128kbps mp3，"
            "响应里的 effective_level 与 is_trial 反映真实结果。"
        )
    ),
]

LimitQuery = Annotated[int, Query(ge=1, le=50, description="每页条数，最大 50。")]

CoverVariantQuery = Annotated[
    str,
    Query(
        description=(
            "封面档位：thumb（默认，200×200，列表用）/ regular（500×500）/ original。"
            "上游默认下发原图，单张可达 2.6MB，列表必须用 thumb。"
        )
    ),
]

OffsetQuery = Annotated[
    int, Query(ge=0, le=5000, description="偏移量，下拉加载更多时用。")
]

_STREAM_RESPONSES = {
    200: {
        "description": "音频字节；带 Range 时返回 206 与 Content-Range",
        "content": {
            "audio/mpeg": {"schema": {"type": "string", "format": "binary"}},
            "audio/mp4": {"schema": {"type": "string", "format": "binary"}},
        },
    },
    **error_responses(401, 404, 429, 503),
}

_COVER_RESPONSES = {
    200: {
        "description": "封面字节",
        "content": {"image/jpeg": {"schema": {"type": "string", "format": "binary"}}},
    },
    **error_responses(401, 404, 429, 503),
}


def _favorited(user_id: str) -> frozenset[str]:
    return music_source.favorited_ids(user_id)


def _require_track(track_id: str) -> None:
    if music_adapter.split_track_id(track_id) is None:
        raise not_found({"id": track_id, "reason": "track_not_found"})


@router.get(
    "/search",
    operation_id="searchMusic",
    response_model=EnvelopeMusicTrackList,
    summary="搜索歌曲",
    responses=error_responses(400, 401, 422, 429, 503),
)
def search_music(
    user: CurrentUser,
    base_url: BaseUrlDep,
    q: KeywordQuery,
    platform: PlatformQuery = None,
    limit: LimitQuery = 20,
    offset: OffsetQuery = 0,
):
    items = music_source.search_tracks(
        base_url,
        q,
        platform=platform,
        limit=limit,
        offset=offset,
        favorited_ids=_favorited(user["user_id"]),
    )
    return envelope.ok({"items": items}, source=SOURCE)


@router.get(
    "/daily",
    operation_id="getDailyMusic",
    response_model=EnvelopeMusicTrackList,
    summary="每日音乐（平台热歌榜）",
    responses=error_responses(401, 422, 429, 503),
)
def get_daily_music(
    user: CurrentUser,
    base_url: BaseUrlDep,
    platform: PlatformQuery = None,
    limit: LimitQuery = 20,
):
    items = music_source.top_tracks(
        base_url,
        platform=platform,
        limit=limit,
        favorited_ids=_favorited(user["user_id"]),
    )
    return envelope.ok({"items": items}, source=SOURCE)


@router.get(
    "/recommend",
    operation_id="recommendMusic",
    response_model=EnvelopeMusicTrackList,
    summary="每日推荐（按收藏歌单让 LLM 挑歌，失败回落平台热歌榜）",
    responses=error_responses(401, 422, 429, 503),
)
def recommend_music(
    user: CurrentUser,
    base_url: BaseUrlDep,
    platform: PlatformQuery = None,
    limit: LimitQuery = 30,
):
    items, _engine = music_recommend.recommend(
        base_url,
        user["user_id"],
        platform=platform,
        limit=limit,
    )
    return envelope.ok({"items": items}, source=SOURCE)


@router.get(
    "/favorites",
    operation_id="listMusicFavorites",
    response_model=EnvelopeMusicTrackList,
    summary="音乐收藏夹",
    responses=error_responses(401, 429, 500),
)
def list_music_favorites(user: CurrentUser, base_url: BaseUrlDep):
    items = music_source.favorite_tracks(base_url, user["user_id"])
    return envelope.ok({"items": items}, source=SOURCE)


@router.put(
    "/favorites/{track_id}",
    operation_id="putMusicFavorite",
    response_model=EnvelopeMusicFavoriteState,
    summary="收藏歌曲（幂等）",
    responses=error_responses(401, 404, 429, 503),
)
def put_music_favorite(user: CurrentUser, base_url: BaseUrlDep, track_id: TrackIdPath):
    return _set_favorite(user, base_url, track_id, True)


@router.delete(
    "/favorites/{track_id}",
    operation_id="deleteMusicFavorite",
    response_model=EnvelopeMusicFavoriteState,
    summary="取消收藏歌曲（幂等）",
    responses=error_responses(401, 404, 429, 503),
)
def delete_music_favorite(user: CurrentUser, base_url: BaseUrlDep, track_id: TrackIdPath):
    return _set_favorite(user, base_url, track_id, False)


def _set_favorite(user: dict, base_url: str, track_id: str, favorited: bool):
    _require_track(track_id)
    # 收藏时把曲目快照一起存进内存：收藏夹从此不必逐条回源上游
    track = music_source.track_detail(base_url, track_id, favorited=favorited)
    state = music_source.set_favorite(user["user_id"], track_id, favorited, track)
    return envelope.ok(state, source=SOURCE)


# ------------------------------------------------------------------ 歌单（收藏夹）
#
# 列表页的 ♥ 是「快速收藏」（进默认歌单），歌单接口则是「收进哪一本」的完整语义：
# 可建可删，一首歌能同时收进多个歌单。


def _require_playlist(user_id: str, playlist_id: str) -> dict[str, Any]:
    record = music_source.get_playlist(user_id, playlist_id)
    if record is None:
        raise not_found({"id": playlist_id, "reason": "playlist_not_found"})
    return record


@router.get(
    "/playlists",
    operation_id="listMusicPlaylists",
    response_model=EnvelopeMusicPlaylistList,
    summary="歌单列表（收藏夹）",
    responses=error_responses(401, 429, 500),
)
def list_music_playlists(user: CurrentUser):
    items = music_source.list_playlists(user["user_id"])
    return envelope.ok({"items": items}, source=SOURCE)


@router.post(
    "/playlists",
    operation_id="createMusicPlaylist",
    response_model=EnvelopeMusicPlaylist,
    summary="新建歌单",
    responses=error_responses(401, 422, 429, 500),
)
def create_music_playlist(user: CurrentUser, payload: MusicPlaylistCreate):
    name = payload.name.strip()
    if not name:
        # 只有空白字符的名字等同于没名字：拒绝，而不是建一个看不见的空壳
        raise bad_request({"reason": "playlist_name_required"})
    return envelope.ok(music_source.create_playlist(user["user_id"], name), source=SOURCE)


@router.delete(
    "/playlists/{playlist_id}",
    operation_id="deleteMusicPlaylist",
    response_model=EnvelopeMusicPlaylistList,
    summary="删除歌单",
    responses=error_responses(401, 403, 404, 429, 500),
)
def delete_music_playlist(user: CurrentUser, playlist_id: PlaylistIdPath):
    _require_playlist(user["user_id"], playlist_id)
    if not music_source.delete_playlist(user["user_id"], playlist_id):
        raise forbidden({"id": playlist_id, "reason": "default_playlist"})
    items = music_source.list_playlists(user["user_id"])
    return envelope.ok({"items": items}, source=SOURCE)


@router.get(
    "/playlists/{playlist_id}",
    operation_id="getMusicPlaylist",
    response_model=EnvelopeMusicTrackList,
    summary="歌单里的曲目",
    responses=error_responses(401, 404, 429, 500),
)
def get_music_playlist(user: CurrentUser, playlist_id: PlaylistIdPath):
    _require_playlist(user["user_id"], playlist_id)
    items = music_source.playlist_tracks(user["user_id"], playlist_id)
    return envelope.ok({"items": items}, source=SOURCE)


@router.put(
    "/playlists/{playlist_id}/tracks/{track_id}",
    operation_id="addMusicPlaylistTrack",
    response_model=EnvelopeMusicPlaylistState,
    summary="把歌曲加入歌单（幂等）",
    responses=error_responses(401, 404, 429, 503),
)
def add_music_playlist_track(
    user: CurrentUser, base_url: BaseUrlDep, playlist_id: PlaylistIdPath, track_id: TrackIdPath
):
    _require_playlist(user["user_id"], playlist_id)
    _require_track(track_id)
    # 与 /favorites 一样存快照，歌单从此不必逐条回源上游
    track = music_source.track_detail(
        base_url, track_id, favorited=track_id in _favorited(user["user_id"])
    )
    state = music_source.set_playlist_track(
        user["user_id"], playlist_id, track_id, True, track
    )
    return envelope.ok(state, source=SOURCE)


@router.delete(
    "/playlists/{playlist_id}/tracks/{track_id}",
    operation_id="removeMusicPlaylistTrack",
    response_model=EnvelopeMusicPlaylistState,
    summary="把歌曲移出歌单（幂等）",
    responses=error_responses(401, 404, 429, 500),
)
def remove_music_playlist_track(
    user: CurrentUser, playlist_id: PlaylistIdPath, track_id: TrackIdPath
):
    _require_playlist(user["user_id"], playlist_id)
    _require_track(track_id)
    state = music_source.set_playlist_track(user["user_id"], playlist_id, track_id, False)
    return envelope.ok(state, source=SOURCE)


@router.get(
    "/tracks/{track_id}",
    operation_id="getMusicTrack",
    response_model=EnvelopeMusicTrack,
    summary="歌曲详情",
    responses=error_responses(401, 404, 429, 503),
)
def get_music_track(user: CurrentUser, base_url: BaseUrlDep, track_id: TrackIdPath):
    _require_track(track_id)
    track = music_source.track_detail(
        base_url, track_id, favorited=track_id in _favorited(user["user_id"])
    )
    return envelope.ok(track, source=SOURCE)


@router.get(
    "/tracks/{track_id}/lyric",
    operation_id="getMusicLyric",
    response_model=EnvelopeMusicLyric,
    summary="歌词",
    responses=error_responses(401, 404, 429, 503),
)
def get_music_lyric(user: CurrentUser, track_id: TrackIdPath):
    _require_track(track_id)
    return envelope.ok(music_source.lyric_for(track_id), source=SOURCE)


@router.get(
    "/tracks/{track_id}/comments",
    operation_id="getMusicComments",
    response_model=EnvelopeMusicCommentList,
    summary="热门评论",
    responses=error_responses(401, 404, 429, 503),
)
def get_music_comments(user: CurrentUser, track_id: TrackIdPath, limit: LimitQuery = 20):
    _require_track(track_id)
    return envelope.ok(music_source.comments_for(track_id, limit=limit), source=SOURCE)


@router.get(
    "/tracks/{track_id}/playback",
    operation_id="getMusicPlayback",
    response_model=EnvelopeMusicPlayback,
    summary="解析播放地址",
    responses=error_responses(401, 404, 429, 503),
)
def get_music_playback(
    user: CurrentUser,
    settings: SettingsDep,
    track_id: TrackIdPath,
    level: LevelQuery = None,
):
    _require_track(track_id)
    playback, _ = music_source.resolve_playback(track_id, level=level)
    # 播放器拿不到 Authorization 头，因此把鉴权挪到 URL 上（见 signature 模块）：
    # 这一条是需要 Bearer 的，签名只是让紧随其后的 /stream 能被媒体元素取用。
    playback["stream_url"] = (
        f"{playback['stream_url']}?"
        f"{music_signature.signed_query(track_id, settings.music_stream_ttl_seconds, settings.music_stream_secret)}"
    )
    if level:
        playback["stream_url"] += f"&level={level}"
    return envelope.ok(playback, source=SOURCE)


@router.get(
    "/tracks/{track_id}/cover",
    operation_id="getMusicCover",
    response_class=Response,
    summary="封面（同源代理）",
    responses=_COVER_RESPONSES,
)
def get_music_cover(
    user: CurrentUser,
    track_id: TrackIdPath,
    variant: CoverVariantQuery = "thumb",
):
    _require_track(track_id)
    resolved = music_source.cover_bytes(track_id, variant=variant)
    if resolved is None:
        raise not_found({"id": track_id, "reason": "cover_unavailable"})
    content, media_type = resolved
    return Response(
        content=content,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get(
    "/tracks/{track_id}/stream",
    operation_id="getMusicStream",
    response_class=StreamingResponse,
    summary="音频流（同源代理，支持 Range）",
    responses=_STREAM_RESPONSES,
)
def get_music_stream(
    settings: SettingsDep,
    track_id: TrackIdPath,
    exp: Annotated[int | None, Query(description="签名过期时刻（unix 秒）")] = None,
    sig: Annotated[str | None, Query(description="由 /playback 下发的 HMAC 签名")] = None,
    level: LevelQuery = None,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
):
    _require_track(track_id)
    # 这里刻意**不**要求 Bearer：媒体元素加不上请求头。签名与曲目绑定且短期有效，
    # 拿不到签名就取不到音频。
    if not music_signature.verify(
        track_id, exp, sig, settings.music_stream_secret
    ):
        raise forbidden({"reason": "invalid_stream_signature"})
    _, handle, upstream = music_source.open_stream(
        track_id, level=level, range_header=range_header
    )
    headers = {
        name: upstream.headers[name] for name in FORWARDED_HEADERS if name in upstream.headers
    }
    # httpx 的 `iter_bytes()` 会自动解压；上游若无视 `Accept-Encoding: identity`
    # 仍然压缩，转发的 content-length 就会大于真正写出的字节数，播放器把这当成
    # 「加载失败」—— 正是「这首突然播不了、刷新又好」的第二种形态。宁可退回
    # chunked 传输，也不能给出一个和实际字节对不上的长度。
    if _is_encoded(upstream.headers.get("content-encoding")):
        headers.pop("content-length", None)
    # 上游直链带签名：这里不做任何缓存，避免把带鉴权的结果存下来
    headers["Cache-Control"] = "no-store"
    media_type = upstream.headers.get("content-type", "audio/mpeg").split(";")[0].strip()

    def iterate():
        try:
            yield from upstream.iter_bytes(STREAM_CHUNK)
        finally:
            upstream.close()
            handle.close()

    return StreamingResponse(
        iterate(),
        status_code=upstream.status_code,
        media_type=media_type or "audio/mpeg",
        headers=headers,
    )
