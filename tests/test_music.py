"""音乐页：id 映射、字段映射、mock 夹具、真实上游（MockTransport）与收藏夹。

真实上游的用例全部注入 ``httpx.MockTransport``：不产生真实网络请求。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings, load_settings
from app.core.errors import AppError
from app.repositories.memory import store
from app.services.music import adapter
from app.services.music import signature as music_signature
from app.services.music import source as music_source
from app.services.music.client import MusicClient

TRACK_SOURCE_ID = "3362265838"
TRACK_ID = f"ne_{TRACK_SOURCE_ID}"
COVER = "https://p3.music.126.net/cover/hash.jpg"
AUDIO = "https://m702.music.126.net/2026/signed.mp3"

SEARCH_SONG: dict[str, Any] = {
    "id": int(TRACK_SOURCE_ID),
    "name": "海阔天空 (Cover 黄家驹)",
    "artists": [{"id": 0, "name": "潮汕家丁"}],
    "album": {"id": 0, "name": "海阔天空"},
    "duration": 313062,
    "fee": 0,
}

DETAIL_SONG: dict[str, Any] = {
    "id": int(TRACK_SOURCE_ID),
    "name": "海阔天空 (Cover 黄家驹)",
    "ar": [{"id": 0, "name": "潮汕家丁"}],
    "al": {"id": 0, "name": "海阔天空", "picUrl": COVER},
    "dt": 313062,
    "fee": 0,
}

COMMENT_RAW: dict[str, Any] = {
    "commentId": 5031795,
    "content": "有些人死了，他还活着",
    "likedCount": 223436,
    "time": 1413809782401,
    "user": {"nickname": "冰凍七音"},
    "ipLocation": {"location": "上海"},
}


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "music_source": "node",
        "music_api_base": "http://music.test",
        "music_max_retries": 0,
        "music_retry_initial_seconds": 0.01,
        "music_cache_ttl_seconds": 60,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def make_client(handler, config: Settings | None = None) -> MusicClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="http://music.test")
    return MusicClient(config or settings(), client=http)


def upstream(request: httpx.Request) -> httpx.Response:
    """把 Node 服务的几个真实响应形状原样复刻。"""
    path = request.url.path
    if path == "/search":
        return httpx.Response(200, json={"code": 200, "result": {"songs": [SEARCH_SONG]}})
    if path == "/song/detail":
        return httpx.Response(200, json={"code": 200, "songs": [DETAIL_SONG]})
    if path == "/top/song":
        return httpx.Response(200, json={"code": 200, "data": [SEARCH_SONG]})
    if path == "/lyric":
        return httpx.Response(
            200,
            json={
                "code": 200,
                "lrc": {"lyric": "[00:18.54]今天我寒夜里看雪飘过"},
                "tlyric": {"lyric": "[00:18.54]今日我寒夜里看雪飘过"},
            },
        )
    if path == "/comment/hot":
        return httpx.Response(
            200,
            json={"code": 200, "hotComments": [COMMENT_RAW], "topComments": [], "total": 1451},
        )
    if path == "/song/url/v1":
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": [
                    {
                        "id": int(TRACK_SOURCE_ID),
                        "url": AUDIO,
                        "br": 128001,
                        "size": 5010956,
                        "type": "mp3",
                        "level": "standard",
                        "time": 313062,
                        "fee": 0,
                        "freeTrialInfo": None,
                    }
                ],
            },
        )
    if path == "/media/signed.mp3":
        return httpx.Response(
            200,
            content=b"ID3fake-audio-bytes",
            headers={"content-type": "audio/mpeg", "content-length": "18"},
        )
    if path == "/media/cover.jpg":
        return httpx.Response(
            200, content=b"\xff\xd8\xff-jpeg", headers={"content-type": "image/jpeg"}
        )
    return httpx.Response(404, json={"code": 404, "message": "not found"})


def install_fake(handler=upstream, **overrides: Any) -> Settings:
    config = settings(**overrides)
    music_source.reset()
    music_source.configure(config, client=make_client(handler, config))
    return config


def signed_stream_url(track_id: str = TRACK_ID, **extra: Any) -> str:
    """按服务端配置现算签名。

    ``/stream`` 不吃 Authorization（媒体元素加不上请求头），只认 URL 上的
    ``exp`` / ``sig``，所以用例必须自己签一次再来请求。
    """
    config = load_settings()
    query = music_signature.signed_query(
        track_id, config.music_stream_ttl_seconds, config.music_stream_secret
    )
    tail = "".join(f"&{key}={value}" for key, value in extra.items())
    return f"/v1/music/tracks/{track_id}/stream?{query}{tail}"


@pytest.fixture(autouse=True)
def restore_source():
    """每个用例前后都还原音乐源，避免污染 session 级 app。"""
    music_source.reset()
    yield
    music_source.reset()
    music_source.configure(load_settings())


# ------------------------------------------------------------------ id 映射


def test_track_id_round_trip() -> None:
    assert adapter.track_id("netease", TRACK_SOURCE_ID) == TRACK_ID
    assert adapter.split_track_id(TRACK_ID) == ("netease", TRACK_SOURCE_ID)
    assert adapter.split_track_id("qq_0039MnYb0qxYhV") == ("qqmusic", "0039MnYb0qxYhV")
    assert adapter.is_music_id(TRACK_ID) is True
    # 图片 / 视频 / 本地夹具的 ID 都不能被音乐源认领
    for value in ("img_0001", "px_111", "bv_BV1Sve26eENf", "ne_", "_123", "zz_123"):
        assert adapter.split_track_id(value) is None


def test_support_levels_degrades_from_requested() -> None:
    assert adapter.support_levels("lossless") == ["lossless", "exhigh", "standard"]
    assert adapter.support_levels(None) == ["exhigh", "standard"]
    # 未知档位回落到默认值，不会把垃圾参数透给上游
    assert adapter.support_levels("不存在的音质") == ["exhigh", "standard"]
    assert adapter.support_levels("jymaster")[0] == "jymaster"


# ------------------------------------------------------------------ 字段映射


def test_build_track_merges_detail_and_points_to_same_origin() -> None:
    track = adapter.build_track(
        SEARCH_SONG, "netease", base_url="http://127.0.0.1:18421", detail=DETAIL_SONG
    )
    assert track is not None
    assert track["id"] == TRACK_ID
    assert track["platform"] == "netease"
    assert track["source_id"] == TRACK_SOURCE_ID
    assert track["title"] == "海阔天空 (Cover 黄家驹)"
    assert track["artists"] == ["潮汕家丁"]
    assert track["album"] == "海阔天空"
    assert track["duration_ms"] == 313062
    # 封面永远是本服务同源地址：上游 CDN 不返回 CORS 头，Web 端直连会失败
    assert track["cover_url"] == f"http://127.0.0.1:18421/v1/music/tracks/{TRACK_ID}/cover"
    assert track["source_url"] == "https://music.163.com/#/song?id=3362265838"
    assert track["is_favorited"] is False


def test_build_track_handles_qq_shapes_and_missing_cover() -> None:
    raw = {
        "songmid": "0039MnYb0qxYhV",
        "songname": "稻香",
        "singer": [{"name": "周杰伦"}],
        "albumname": "魔杰座",
        "interval": 223,  # QQ 音乐用秒
        "fee": 1,
    }
    track = adapter.build_track(raw, "qqmusic", base_url="http://host")
    assert track is not None
    assert track["id"] == "qq_0039MnYb0qxYhV"
    assert track["duration_ms"] == 223000
    assert track["album"] == "魔杰座"
    assert track["cover_url"] == "http://host/v1/music/tracks/qq_0039MnYb0qxYhV/cover"


def test_build_track_without_id_is_dropped() -> None:
    assert adapter.build_track({"name": "没有 id"}, "netease", base_url="http://host") is None


def test_build_comment_maps_epoch_and_location() -> None:
    comment = adapter.build_comment(COMMENT_RAW)
    assert comment is not None
    assert comment["id"] == "5031795"
    assert comment["author"] == "冰凍七音"
    assert comment["liked_count"] == 223436
    assert comment["location"] == "上海"
    # 毫秒时间戳换算成契约固定的 +08:00
    assert comment["created_at"].utcoffset().total_seconds() == 8 * 3600
    assert comment["created_at"].year == 2014
    assert adapter.build_comment({"commentId": "1"}) is None


def test_build_playback_marks_trial_segment() -> None:
    playback = adapter.build_playback(
        {
            "url": AUDIO,
            "br": 128001,
            "type": "mp3",
            "level": "standard",
            "time": 481115,
            "freeTrialInfo": {"start": 0, "end": 30000},
        },
        track=TRACK_ID,
        requested_level="exhigh",
        duration_ms=313062,
    )
    assert playback["id"] == TRACK_ID
    assert playback["stream_url"] == f"/v1/music/tracks/{TRACK_ID}/stream"
    assert playback["bitrate"] == 128001
    assert playback["effective_level"] == "standard"
    assert playback["is_trial"] is True
    assert playback["trial_end_ms"] == 30000
    assert playback["mime_type"] == "audio/mp3"
    # 时长以上游 /song/detail 的 dt 为准，而不是试听片段的 time
    assert playback["duration_ms"] == 313062


# ------------------------------------------------------------------ 媒体白名单


def test_media_host_allowlist_matches_suffix() -> None:
    client = make_client(upstream)
    assert client.stream_allowed(AUDIO) is True
    assert client.stream_allowed("https://p3.music.126.net/x.jpg") is True
    assert client.stream_allowed("https://m801.music.126.net/a.mp3") is True
    # 结尾相同但域名不同的主机不能被放行
    assert client.stream_allowed("https://evil-music.126.net.attacker.com/a.mp3") is False
    assert client.stream_allowed("https://example.com/a.mp3") is False
    assert client.stream_allowed("not-a-url") is False


def test_media_host_allowlist_rejects_unknown_host_at_runtime() -> None:
    install_fake()
    client = music_source._require_client()
    with pytest.raises(AppError) as error:
        client.open_media("https://evil.example.com/a.mp3")
    assert error.value.details["reason"] == "unexpected_media_host"


# ------------------------------------------------------------------ mock 夹具


def test_mock_source_serves_search_and_detail(client: TestClient, auth: dict[str, str]) -> None:
    response = client.get("/v1/music/search", params={"q": "海阔天空"}, headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["source"] == "music"
    assert body["error"] is None
    first = body["data"]["items"][0]
    assert first["id"] == "ne_347230"
    assert first["cover_url"].endswith("/v1/music/tracks/ne_347230/cover")

    detail = client.get("/v1/music/tracks/ne_347230", headers=auth)
    assert detail.status_code == 200
    assert detail.json()["data"]["title"] == "海阔天空"


def test_mock_source_serves_lyric_and_comments(client: TestClient, auth: dict[str, str]) -> None:
    lyric = client.get("/v1/music/tracks/ne_347230/lyric", headers=auth)
    assert lyric.status_code == 200
    assert "mock" in lyric.json()["data"]["lyric"]
    comments = client.get("/v1/music/tracks/ne_347230/comments", headers=auth)
    assert comments.status_code == 200
    assert comments.json()["data"]["total"] == 1


def test_mock_cover_falls_back_to_local_sample(client: TestClient, auth: dict[str, str]) -> None:
    response = client.get("/v1/music/tracks/ne_347230/cover", headers=auth)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")
    assert response.content


def test_stream_rejects_unsigned_request(client: TestClient, auth: dict[str, str]) -> None:
    """没有签名就取不到音频：/stream 不是公开代理。"""
    response = client.get("/v1/music/tracks/ne_347230/stream", headers=auth)
    assert response.status_code == 403
    assert response.json()["error"]["details"]["reason"] == "invalid_stream_signature"


def test_stream_rejects_tampered_signature(client: TestClient, auth: dict[str, str]) -> None:
    tampered = signed_stream_url("ne_347230").replace("sig=", "sig=0")
    assert client.get(tampered, headers=auth).status_code == 403


def test_mock_source_cannot_stream(client: TestClient, auth: dict[str, str]) -> None:
    """签名没问题，但 mock 源本身没有音频字节。"""
    response = client.get(signed_stream_url("ne_347230"), headers=auth)
    assert response.status_code == 503
    assert response.json()["error"]["details"]["reason"] == "music_source_mock"


def test_music_requires_token(client: TestClient) -> None:
    assert client.get("/v1/music/daily").status_code == 401
    assert client.get("/v1/music/favorites").status_code == 401
    assert client.put("/v1/music/favorites/ne_347230").status_code == 401


# ------------------------------------------------------------------ 真实上游


def test_search_maps_upstream_songs(client: TestClient, auth: dict[str, str]) -> None:
    install_fake()
    response = client.get("/v1/music/search", params={"q": "稻香", "limit": 3}, headers=auth)
    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert [item["id"] for item in items] == [TRACK_ID]
    assert items[0]["artists"] == ["潮汕家丁"]
    assert items[0]["duration_ms"] == 313062


def test_daily_music_uses_top_song(client: TestClient, auth: dict[str, str]) -> None:
    install_fake()
    response = client.get("/v1/music/daily", params={"limit": 5}, headers=auth)
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]["items"]] == [TRACK_ID]


def test_lyric_and_comments_from_upstream(client: TestClient, auth: dict[str, str]) -> None:
    install_fake()
    lyric = client.get(f"/v1/music/tracks/{TRACK_ID}/lyric", headers=auth)
    assert lyric.status_code == 200
    assert lyric.json()["data"]["lyric"].startswith("[00:18.54]")
    assert lyric.json()["data"]["translated_lyric"].startswith("[00:18.54]")

    comments = client.get(f"/v1/music/tracks/{TRACK_ID}/comments", headers=auth)
    assert comments.status_code == 200
    body = comments.json()["data"]
    assert body["total"] == 1451
    assert body["items"][0]["content"] == "有些人死了，他还活着"


def test_playback_reports_effective_level(client: TestClient, auth: dict[str, str]) -> None:
    install_fake()
    response = client.get(f"/v1/music/tracks/{TRACK_ID}/playback", headers=auth)
    assert response.status_code == 200
    data = response.json()["data"]
    # 请求最高档但上游只给 standard：响应里的 effective_level 必须反映真实结果
    assert data["requested_level"] == "exhigh"
    assert data["effective_level"] == "standard"
    assert data["is_trial"] is False
    # 播放地址必须带签名：紧随其后的 /stream 只靠它鉴权
    assert data["stream_url"].startswith(f"/v1/music/tracks/{TRACK_ID}/stream?exp=")
    assert "&sig=" in data["stream_url"]


def test_playback_degrades_when_upstream_has_no_url(client: TestClient, auth: dict[str, str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/song/url/v1":
            return httpx.Response(200, json={"code": 200, "data": [{"id": 1, "url": None}]})
        return upstream(request)

    install_fake(handler)
    response = client.get(f"/v1/music/tracks/{TRACK_ID}/playback", headers=auth)
    assert response.status_code == 503
    assert response.json()["error"]["details"]["reason"] == "no_playable_url"


def test_playback_retries_when_upstream_blips(client: TestClient, auth: dict[str, str]) -> None:
    """上游「这一秒给空地址」是常态：整条音质链都空时补一次重试就该拿到地址。

    实测同一首曲子第一次回 ``url: null``、隔一秒再问就有（36 首里撞到 2 次），
    而 ``MusicClient`` 的重试只覆盖 HTTP / 传输层失败，管不到这种「200 + 空地址」。
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/song/url/v1":
            calls["n"] += 1
            # 第一趟（exhigh + standard 两个档位）全空，重试那一趟给地址
            url = None if calls["n"] <= 2 else AUDIO
            return httpx.Response(200, json={"code": 200, "data": [{"id": 1, "url": url}]})
        return upstream(request)

    install_fake(handler)
    response = client.get(f"/v1/music/tracks/{TRACK_ID}/playback", headers=auth)
    assert response.status_code == 200
    assert calls["n"] > 2, "第一趟拿不到地址时必须再问一遍"


def test_stream_reuses_the_url_playback_resolved(client: TestClient, auth: dict[str, str]) -> None:
    """``/stream`` 不许再解析一次。

    重新解析既多一趟上游往返，又可能撞上「200 + 空地址」把一次本来能播的播放判死
    （``/playback`` 成功、紧随其后的 ``/stream`` 却 503，用户看到「突然加载不出来」）。
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/song/url/v1":
            calls["n"] += 1
            return httpx.Response(200, json={"code": 200, "data": [{"id": 1, "url": AUDIO}]})
        if request.url.path.endswith("/signed.mp3"):
            return httpx.Response(
                200, content=b"ID3fake", headers={"content-type": "audio/mpeg"}
            )
        return upstream(request)

    install_fake(handler)
    assert client.get(f"/v1/music/tracks/{TRACK_ID}/playback", headers=auth).status_code == 200
    assert calls["n"] == 1
    assert client.get(signed_stream_url(), headers=auth).status_code == 200
    assert calls["n"] == 1, "解析结果要被 /stream 复用，而不是再问一次上游"


def test_stream_refreshes_cached_url_when_it_expires(
    client: TestClient, auth: dict[str, str]
) -> None:
    """缓存里的地址取不到（过期 / 被上游回收）时要重解析，而不是把播放判死。"""
    calls = {"n": 0}
    stale = "https://m702.music.126.net/2026/stale.mp3"
    fresh = "https://m702.music.126.net/2026/fresh.mp3"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/song/url/v1":
            calls["n"] += 1
            url = stale if calls["n"] == 1 else fresh
            return httpx.Response(200, json={"code": 200, "data": [{"id": 1, "url": url}]})
        if request.url.path.endswith("/stale.mp3"):
            return httpx.Response(403, json={"code": 403})
        if request.url.path.endswith("/fresh.mp3"):
            return httpx.Response(
                200, content=b"ID3fresh", headers={"content-type": "audio/mpeg"}
            )
        return upstream(request)

    install_fake(handler)
    assert client.get(f"/v1/music/tracks/{TRACK_ID}/playback", headers=auth).status_code == 200
    response = client.get(signed_stream_url(), headers=auth)
    assert response.status_code == 200
    assert response.content == b"ID3fresh"
    assert calls["n"] == 2, "旧地址取不到就要重解析一次"


def test_media_request_asks_for_identity_encoding() -> None:
    """媒体请求要显式声明不压缩。

    ``/stream`` 把上游的 ``content-length`` 原样转发给播放器，而 httpx 的
    ``iter_bytes()`` 会自动解压：上游一旦压过，声明长度与实际字节就对不上，
    播放器只能判这次加载失败 —— 表现是「这首突然播不了，刷新又好」。
    """
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/signed.mp3"):
            seen["accept_encoding"] = request.headers.get("accept-encoding")
            return httpx.Response(200, content=b"ID3", headers={"content-type": "audio/mpeg"})
        return upstream(request)

    lease, _ = make_client(handler).open_media(AUDIO)
    try:
        assert seen["accept_encoding"] == "identity"
    finally:
        lease.close()


def test_stream_proxies_bytes_and_range(client: TestClient, auth: dict[str, str]) -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/signed.mp3"):
            seen["range"] = request.headers.get("range")
            seen["referer"] = request.headers.get("referer")
            return httpx.Response(
                206,
                content=b"ID3fake",
                headers={
                    "content-type": "audio/mpeg",
                    "content-length": "7",
                    "content-range": "bytes 0-6/5010956",
                    "accept-ranges": "bytes",
                },
            )
        return upstream(request)

    install_fake(handler)
    response = client.get(
        signed_stream_url(),
        headers={**auth, "Range": "bytes=0-6"},
    )
    assert response.status_code == 206
    assert response.content == b"ID3fake"
    assert response.headers["content-range"] == "bytes 0-6/5010956"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["cache-control"] == "no-store"
    # Range 与站点 Referer 都要透给 CDN，否则拿不到分段
    assert seen["range"] == "bytes=0-6"
    assert seen["referer"] == "https://music.163.com/"


def test_stream_drops_content_length_when_upstream_still_compresses(
    client: TestClient, auth: dict[str, str]
) -> None:
    """上游硬要压缩时，不能把它的 content-length 再转发出去。

    ``Accept-Encoding: identity`` 只是请求，上游 CDN 有权不理它。真压了的话
    ``httpx`` 的 ``iter_bytes()`` 会自动解压，声明长度就比实际写出的字节少，
    播放器读到一半判加载失败 —— 这时宁可不给长度（退回 chunked）。
    """
    import gzip

    payload = b"ID3fake-audio-bytes"
    packed = gzip.compress(payload)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/signed.mp3"):
            return httpx.Response(
                200,
                content=packed,
                headers={
                    "content-type": "audio/mpeg",
                    "content-encoding": "gzip",
                    "content-length": str(len(packed)),
                },
            )
        return upstream(request)

    install_fake(handler)
    response = client.get(signed_stream_url(), headers=auth)

    assert response.status_code == 200
    assert response.content == payload
    assert "content-length" not in response.headers


def test_cover_is_proxied_from_upstream() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "p3.music.126.net":
            return httpx.Response(
                200, content=b"\xff\xd8\xff-cover", headers={"content-type": "image/jpeg"}
            )
        return upstream(request)

    install_fake(handler)
    content, media_type = music_source.cover_bytes(TRACK_ID)
    assert media_type == "image/jpeg"
    assert content.startswith(b"\xff\xd8\xff")


def test_upstream_error_code_becomes_503(client: TestClient, auth: dict[str, str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 502, "message": "upstream boom"})

    install_fake(handler)
    response = client.get("/v1/music/search", params={"q": "稻香"}, headers=auth)
    assert response.status_code == 503
    details = response.json()["error"]["details"]
    assert details["reason"] == "upstream_error"
    # 上游文案绝不透传给客户端
    assert "boom" not in response.text


def test_unknown_track_id_is_404(client: TestClient, auth: dict[str, str]) -> None:
    install_fake()
    assert client.get("/v1/music/tracks/img_0001", headers=auth).status_code == 404
    assert client.get("/v1/music/tracks/img_0001/lyric", headers=auth).status_code == 404
    assert client.put("/v1/music/favorites/img_0001", headers=auth).status_code == 404


# ------------------------------------------------------------------ 收藏夹


def test_favorites_round_trip_is_idempotent(client: TestClient, auth: dict[str, str]) -> None:
    install_fake()
    first = client.put(f"/v1/music/favorites/{TRACK_ID}", headers=auth)
    assert first.status_code == 200
    assert first.json()["data"] == {"id": TRACK_ID, "is_favorited": True}
    again = client.put(f"/v1/music/favorites/{TRACK_ID}", headers=auth)
    assert again.json()["data"]["is_favorited"] is True

    listing = client.get("/v1/music/favorites", headers=auth)
    items = listing.json()["data"]["items"]
    assert [item["id"] for item in items] == [TRACK_ID]
    # 收藏夹读的是内存快照：标题、封面都取自收藏那一刻
    assert items[0]["title"] == "海阔天空 (Cover 黄家驹)"
    assert items[0]["is_favorited"] is True

    removed = client.delete(f"/v1/music/favorites/{TRACK_ID}", headers=auth)
    assert removed.json()["data"]["is_favorited"] is False
    assert client.get("/v1/music/favorites", headers=auth).json()["data"]["items"] == []


def test_music_favorites_do_not_pollute_image_favorites(
    client: TestClient, auth: dict[str, str]
) -> None:
    install_fake()
    client.put(f"/v1/music/favorites/{TRACK_ID}", headers=auth)
    # 图片收藏读的是另一个命名空间，不能出现音乐曲目
    images = client.get("/v1/images/favorites", headers=auth)
    assert images.status_code == 200
    assert images.json()["data"]["items"] == []


def test_favorites_are_per_user(client: TestClient, auth: dict[str, str], second_user) -> None:
    install_fake()
    client.put(f"/v1/music/favorites/{TRACK_ID}", headers=auth)
    assert client.get("/v1/music/favorites", headers=second_user).json()["data"]["items"] == []


# ------------------------------------------------------------------ 歌单（收藏夹）


def _playlists(client: TestClient, headers: dict[str, str]) -> list[dict[str, Any]]:
    response = client.get("/v1/music/playlists", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]["items"]


def _create_playlist(client: TestClient, headers: dict[str, str], name: str) -> str:
    response = client.post("/v1/music/playlists", headers=headers, json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["data"]["id"]


def test_playlists_start_with_the_default_one(
    client: TestClient, auth: dict[str, str]
) -> None:
    install_fake()
    items = _playlists(client, auth)
    assert [item["name"] for item in items] == ["我喜欢的音乐"]
    assert items[0]["is_default"] is True
    assert items[0]["track_count"] == 0
    assert items[0]["track_ids"] == []


def test_create_playlist_trims_name_and_lists_in_creation_order(
    client: TestClient, auth: dict[str, str]
) -> None:
    install_fake()
    created = client.post("/v1/music/playlists", headers=auth, json={"name": "  通勤路上  "})
    assert created.status_code == 200
    data = created.json()["data"]
    assert data["name"] == "通勤路上"
    assert data["is_default"] is False
    assert data["track_count"] == 0
    assert data["id"].startswith("pl_")
    assert data["created_at"]

    names = [item["name"] for item in _playlists(client, auth)]
    assert names == ["我喜欢的音乐", "通勤路上"]


def test_blank_playlist_name_is_rejected(client: TestClient, auth: dict[str, str]) -> None:
    install_fake()
    # 纯空白：过得了长度校验但没意义，服务端 400 拒绝
    assert (
        client.post("/v1/music/playlists", headers=auth, json={"name": "   "}).status_code == 400
    )
    # 空串与超长名字交给 schema 校验
    assert client.post("/v1/music/playlists", headers=auth, json={"name": ""}).status_code == 422
    assert (
        client.post("/v1/music/playlists", headers=auth, json={"name": "x" * 41}).status_code == 422
    )


def test_playlist_track_round_trip(client: TestClient, auth: dict[str, str]) -> None:
    install_fake()
    playlist_id = _create_playlist(client, auth, "夜间")

    added = client.put(f"/v1/music/playlists/{playlist_id}/tracks/{TRACK_ID}", headers=auth)
    assert added.status_code == 200, added.text
    assert added.json()["data"] == {
        "playlist_id": playlist_id,
        "track_id": TRACK_ID,
        "in_playlist": True,
        "track_count": 1,
    }

    # 幂等：重复加入不产生第二份
    again = client.put(f"/v1/music/playlists/{playlist_id}/tracks/{TRACK_ID}", headers=auth)
    assert again.json()["data"]["track_count"] == 1

    tracks = client.get(f"/v1/music/playlists/{playlist_id}", headers=auth).json()["data"]["items"]
    assert [item["id"] for item in tracks] == [TRACK_ID]
    # 曲目读的是加入那一刻的内存快照
    assert tracks[0]["title"] == "海阔天空 (Cover 黄家驹)"
    assert tracks[0]["is_favorited"] is True

    summary = _playlists(client, auth)[1]
    assert summary["track_ids"] == [TRACK_ID]

    # 收进任意歌单即算「已收藏」，旧版收藏夹接口随之可见
    favorites = client.get("/v1/music/favorites", headers=auth).json()["data"]["items"]
    assert [item["id"] for item in favorites] == [TRACK_ID]

    removed = client.delete(f"/v1/music/playlists/{playlist_id}/tracks/{TRACK_ID}", headers=auth)
    assert removed.json()["data"]["in_playlist"] is False
    assert removed.json()["data"]["track_count"] == 0
    assert client.get(f"/v1/music/playlists/{playlist_id}", headers=auth).json()["data"]["items"] == []
    assert client.get("/v1/music/favorites", headers=auth).json()["data"]["items"] == []


def test_one_track_can_live_in_several_playlists(
    client: TestClient, auth: dict[str, str]
) -> None:
    install_fake()
    first = _create_playlist(client, auth, "甲")
    second = _create_playlist(client, auth, "乙")
    for playlist_id in (first, second):
        assert (
            client.put(
                f"/v1/music/playlists/{playlist_id}/tracks/{TRACK_ID}", headers=auth
            ).status_code
            == 200
        )
    summaries = {item["name"]: item for item in _playlists(client, auth)}
    assert summaries["甲"]["track_ids"] == [TRACK_ID]
    assert summaries["乙"]["track_ids"] == [TRACK_ID]
    # 并集去重：收藏夹里只出现一次
    assert len(client.get("/v1/music/favorites", headers=auth).json()["data"]["items"]) == 1


def test_quick_favorite_lands_in_the_default_playlist_and_unfavorite_clears_every_playlist(
    client: TestClient, auth: dict[str, str]
) -> None:
    install_fake()
    custom = _create_playlist(client, auth, "自选")
    client.put(f"/v1/music/playlists/{custom}/tracks/{TRACK_ID}", headers=auth)
    # ♥ 快速收藏进默认歌单
    client.put(f"/v1/music/favorites/{TRACK_ID}", headers=auth)
    summaries = {item["name"]: item for item in _playlists(client, auth)}
    assert summaries["我喜欢的音乐"]["track_ids"] == [TRACK_ID]
    assert summaries["自选"]["track_ids"] == [TRACK_ID]

    # 取消收藏 = 从所有歌单移除（列表页的星星表达的是「收没收藏过」）
    client.delete(f"/v1/music/favorites/{TRACK_ID}", headers=auth)
    summaries = {item["name"]: item for item in _playlists(client, auth)}
    assert summaries["我喜欢的音乐"]["track_ids"] == []
    assert summaries["自选"]["track_ids"] == []
    assert client.get("/v1/music/favorites", headers=auth).json()["data"]["items"] == []


def test_delete_playlist(client: TestClient, auth: dict[str, str]) -> None:
    install_fake()
    temporary = _create_playlist(client, auth, "临时")
    deleted = client.delete(f"/v1/music/playlists/{temporary}", headers=auth)
    assert deleted.status_code == 200
    assert [item["name"] for item in deleted.json()["data"]["items"]] == ["我喜欢的音乐"]

    # 默认歌单不可删：否则旧的 /favorites 就没有落点了
    default_id = _playlists(client, auth)[0]["id"]
    refused = client.delete(f"/v1/music/playlists/{default_id}", headers=auth)
    assert refused.status_code == 403
    assert refused.json()["error"]["details"]["reason"] == "default_playlist"

    assert client.delete("/v1/music/playlists/pl_missing", headers=auth).status_code == 404


def test_playlist_routes_404_on_unknown_playlist(
    client: TestClient, auth: dict[str, str]
) -> None:
    install_fake()
    assert client.get("/v1/music/playlists/pl_missing", headers=auth).status_code == 404
    assert (
        client.put(f"/v1/music/playlists/pl_missing/tracks/{TRACK_ID}", headers=auth).status_code
        == 404
    )
    # 歌单存在但曲目 ID 不合法：按曲目的 404 语义报错
    playlist_id = _create_playlist(client, auth, "甲")
    assert (
        client.put(f"/v1/music/playlists/{playlist_id}/tracks/img_0001", headers=auth).status_code
        == 404
    )


def test_playlists_are_per_user(
    client: TestClient, auth: dict[str, str], second_user: dict[str, str]
) -> None:
    install_fake()
    _create_playlist(client, auth, "只属于我")
    other = client.get("/v1/music/playlists", headers=second_user).json()["data"]["items"]
    assert [item["name"] for item in other] == ["我喜欢的音乐"]


def test_playlist_routes_require_auth(client: TestClient) -> None:
    assert client.get("/v1/music/playlists").status_code == 401
    assert client.post("/v1/music/playlists", json={"name": "x"}).status_code == 401
    assert client.delete("/v1/music/playlists/pl_x").status_code == 401
    assert client.get("/v1/music/playlists/pl_x").status_code == 401
    assert client.put("/v1/music/playlists/pl_x/tracks/ne_1").status_code == 401


def test_reset_clears_playlists(client: TestClient, auth: dict[str, str]) -> None:
    install_fake()
    _create_playlist(client, auth, "会被清掉")
    store.reset()
    assert store.music_playlists == {}
    assert store.music_playlist_items == {}


def test_dev_reset_clears_music_cache() -> None:
    install_fake()
    music_source.lyric_for(TRACK_ID)
    assert music_source._lyric_cache
    music_source.reset()
    assert music_source._lyric_cache == {}


# ------------------------------------------------------------------ 搜索合并（电台 / 声音）


VOICE_RESOURCE: dict[str, Any] = {
    "resourceType": "voice",
    "baseInfo": {
        "mainSong": {
            "id": 2091949390,
            "name": "『倒带』千禧年┋ 盘点周杰伦写给蔡依林的歌",
            "duration": 2044525,
            "fee": 0,
            "album": {
                "name": "[DJ节目]桃夭夭没有吱的DJ节目 第96期",
                "picUrl": "https://p3.music.126.net/voice.jpg",
                "artists": [{"id": 1, "name": "桃夭夭没有吱"}],
            },
        }
    },
}

RADIO_PROGRAM_RAW: dict[str, Any] = {
    "id": 3071272917,
    "name": "没关系",
    "mainTrackId": 2673673705,
    "duration": 218810,
    "coverUrl": "https://p3.music.126.net/program.jpg",
    "radio": {"id": 992626291, "name": "北梦梦梦的DJ节目"},
    "mainSong": {
        "id": 2673673705,
        "name": "没关系",
        "duration": 218810,
        "fee": 0,
        "artists": [],
        "album": {
            "name": "[DJ节目]北梦梦梦的DJ节目 第114期",
            "picUrl": "https://p3.music.126.net/program.jpg",
            "artists": [{"id": 2, "name": "北梦梦梦"}],
        },
    },
}


def merge_handler(request: httpx.Request) -> httpx.Response:
    """单曲照旧，声音 / 电台按真实上游形状返回。"""
    path = request.url.path
    search_type = request.url.params.get("type")
    if path == "/search" and search_type == "2000":
        return httpx.Response(
            200, json={"code": 200, "result": {"data": {"resources": [VOICE_RESOURCE]}}}
        )
    if path == "/search" and search_type == "1009":
        return httpx.Response(
            200,
            json={
                "code": 200,
                "result": {"djRadios": [{"id": 992626291, "name": "北梦梦梦的DJ节目"}]},
            },
        )
    if path == "/dj/program":
        return httpx.Response(200, json={"code": 200, "programs": [RADIO_PROGRAM_RAW]})
    return upstream(request)


def test_search_merges_voice_and_radio_programs(
    client: TestClient, auth: dict[str, str]
) -> None:
    install_fake(merge_handler)
    response = client.get("/v1/music/search", params={"q": "周杰伦", "limit": 3}, headers=auth)
    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert [item["id"] for item in items] == [TRACK_ID, "ne_2091949390", "ne_2673673705"]
    by_kind = {item["kind"]: item for item in items}
    assert by_kind["song"]["title"] == "海阔天空 (Cover 黄家驹)"
    assert by_kind["voice"]["artists"] == ["桃夭夭没有吱"]
    assert by_kind["voice"]["duration_ms"] == 2044525
    assert by_kind["radio"]["title"] == "没关系"
    assert by_kind["radio"]["artists"] == ["北梦梦梦"]
    # 合并进来的曲目同样走本服务的封面代理，客户端永远拿不到 CDN 直链
    assert by_kind["voice"]["cover_url"].endswith("/v1/music/tracks/ne_2091949390/cover")


def test_search_merge_skips_duplicates_and_only_on_first_page(
    client: TestClient, auth: dict[str, str]
) -> None:
    def duplicate_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search" and request.url.params.get("type") == "2000":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "result": {
                        "data": {
                            "resources": [
                                {
                                    "baseInfo": {
                                        "mainSong": {
                                            "id": int(TRACK_SOURCE_ID),
                                            "name": "海阔天空 (Cover 黄家驹)",
                                            "duration": 313062,
                                            "fee": 0,
                                            "album": {"name": "海阔天空", "picUrl": COVER},
                                        }
                                    }
                                }
                            ]
                        }
                    },
                },
            )
        return upstream(request)

    install_fake(duplicate_handler)
    first = client.get("/v1/music/search", params={"q": "海阔天空", "limit": 3}, headers=auth)
    assert [item["id"] for item in first.json()["data"]["items"]] == [TRACK_ID]

    second = client.get(
        "/v1/music/search", params={"q": "海阔天空", "limit": 3, "offset": 3}, headers=auth
    )
    assert second.status_code == 200


def test_search_survives_merge_failure(client: TestClient, auth: dict[str, str]) -> None:
    """电台 / 声音取不到时只少几条结果，单曲搜索绝不能跟着 503。"""

    def broken_merge(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search" and request.url.params.get("type") != "1":
            raise httpx.ConnectError("merge boom")
        return upstream(request)

    install_fake(broken_merge)
    response = client.get("/v1/music/search", params={"q": "稻香", "limit": 3}, headers=auth)
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]["items"]] == [TRACK_ID]


def test_playback_resolves_for_merged_voice_track(client: TestClient, auth: dict[str, str]) -> None:
    """声音 / 电台的 mainSong 就是普通歌曲 ID，播放解析不需要特殊分支。"""
    install_fake(merge_handler)
    searched = client.get("/v1/music/search", params={"q": "周杰伦"}, headers=auth)
    voice_id = [item["id"] for item in searched.json()["data"]["items"] if item["kind"] == "voice"][0]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/song/url/v1":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": [
                        {
                            "id": 2091949390,
                            "url": AUDIO,
                            "br": 320001,
                            "size": 5010956,
                            "type": "mp3",
                            "level": "exhigh",
                            "time": 2044525,
                            "fee": 0,
                            "freeTrialInfo": None,
                        }
                    ],
                },
            )
        return merge_handler(request)

    install_fake(handler)
    response = client.get(f"/v1/music/tracks/{voice_id}/playback", headers=auth)
    assert response.status_code == 200
    assert response.json()["data"]["effective_level"] == "exhigh"


# ------------------------------------------------------------------ 上游抖动降级
#
# 现场教训（2026-09-21）：Node 音乐服务进程状态腐化时 /song/detail 会间歇性 500。
# 详情只用来补封面，标题 / 歌手 / 时长在 /search、/top/song 里就有了，所以补全失败
# 绝不能把整个列表打成 503——那会让音乐页直接白屏。


def broken_detail_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/song/detail":
        return httpx.Response(500, json={"code": 500, "message": "boom"})
    return upstream(request)


def test_search_survives_detail_outage(client: TestClient, auth: dict[str, str]) -> None:
    install_fake(broken_detail_handler)
    response = client.get("/v1/music/search", params={"q": "稻香", "limit": 3}, headers=auth)
    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert [item["id"] for item in items] == [TRACK_ID]
    # 标题 / 歌手来自 /search 本身，降级后仍然完整
    assert items[0]["artists"] == ["潮汕家丁"]


def test_daily_survives_detail_outage(client: TestClient, auth: dict[str, str]) -> None:
    install_fake(broken_detail_handler)
    response = client.get("/v1/music/daily", params={"limit": 5}, headers=auth)
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]["items"]] == [TRACK_ID]


def test_single_detail_outage_is_still_503(client: TestClient, auth: dict[str, str]) -> None:
    """单曲详情必须拿到 detail，所以这里照旧 503（而不是被降级成 404）。"""
    install_fake(broken_detail_handler)
    response = client.get(f"/v1/music/tracks/{TRACK_ID}", headers=auth)
    assert response.status_code == 503
