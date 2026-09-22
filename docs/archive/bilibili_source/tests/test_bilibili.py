"""B 站适配器：id 映射、质量筛选、载荷映射、错误映射与图片回源。

全部用例都注入 ``httpx.MockTransport``：不产生真实网络请求。
"""

from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.errors import AppError
from app.services.bilibili import adapter
from app.services.bilibili import source as bilibili_source
from app.services.bilibili.client import BilibiliClient

BVID_A = "BV1Sve26eENf"
BVID_B = "BV1AQYYzYEBb"
BVID_C = "BV1hteAzhEbD"

COVER = "http://i0.hdslb.com/bfs/archive/10363747ec10a717d228bd39bf111e5a1db9d3f2.jpg"


def video(
    bvid: str,
    *,
    view: int,
    like: int,
    title: str = "测试视频",
    tname: str = "游戏",
    duration: int = 300,
    pubdate: int = 1789790700,
    pic: str = COVER,
) -> dict:
    return {
        "bvid": bvid,
        "aid": 1,
        "title": title,
        "pic": pic,
        "desc": "简介",
        "duration": duration,
        "pubdate": pubdate,
        "tname": tname,
        "tid": 4,
        "owner": {"mid": 13354765, "name": "测试UP"},
        "stat": {
            "view": view,
            "like": like,
            "coin": 10,
            "favorite": 20,
            "share": 3,
            "reply": 5,
            "danmaku": 8,
        },
    }


def envelope(items: list[dict]) -> dict:
    return {"code": 0, "message": "0", "ttl": 1, "data": {"list": items}}


# 高赞（点赞率 0.4 / 0.3）与低赞（0.01）混合，用来验证质量优先与补齐
POPULAR_ITEMS = [
    video(BVID_A, view=1000, like=400, title="高质量A"),
    video(BVID_B, view=1000, like=300, title="高质量B"),
    video(BVID_C, view=1000, like=10, title="普通C"),
]


def settings(**overrides) -> Settings:
    base = {
        "video_source": "bilibili",
        "bilibili_max_retries": 0,
        "bilibili_retry_initial_seconds": 0.01,
        "bilibili_min_interval_seconds": 0.0,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def make_client(handler, config: Settings | None = None) -> BilibiliClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="https://api.bilibili.com")
    return BilibiliClient(config or settings(), client=http)


def view_envelope(item: dict) -> dict:
    return {"code": 0, "message": "0", "ttl": 1, "data": item}


@pytest.fixture(autouse=True)
def restore_source():
    """每个用例前后都还原内容源，避免污染 session 级 app。"""
    bilibili_source.reset()
    yield
    bilibili_source.reset()


# ------------------------------------------------------------------ id 映射


def test_content_id_round_trip() -> None:
    assert adapter.content_id_for(BVID_A) == f"bv_{BVID_A}"
    assert adapter.bvid_of(f"bv_{BVID_A}") == BVID_A
    assert adapter.is_bilibili_id(f"bv_{BVID_A}") is True
    # 本地夹具与 pixiv 的 ID 都不能被 B 站源认领
    assert adapter.bvid_of("video_0001") is None
    assert adapter.bvid_of("px_123") is None
    assert adapter.bvid_of("bv_123") is None
    assert adapter.is_bilibili_id("video_0001") is False


def test_category_table_matches_crawler() -> None:
    assert adapter.category_rid("game") == 4
    assert adapter.category_name("game") == "游戏"
    assert adapter.category_rid("GAME") == 4
    assert adapter.category_rid("all") == 0
    assert adapter.category_rid("不存在的分区") is None
    assert adapter.category_name(None) == ""
    # 爬虫的 20 个分类都必须在表里（rid 唯一性由字典值保证）
    for slug in ("all", "douga", "music", "game", "ent", "knowledge", "kichiku", "dance"):
        assert adapter.category_rid(slug) is not None


# ------------------------------------------------------------------ 载荷映射


def test_video_items_skips_entries_without_bvid() -> None:
    payload = envelope([POPULAR_ITEMS[0], {"title": "没有 bvid"}, {"bvid": "坏值"}])
    parsed = adapter.video_items(payload)
    assert [entry["bvid"] for entry in parsed] == [BVID_A]
    assert adapter.video_items({"data": {"list": "坏值"}}) == []
    assert adapter.video_items(None) == []


def test_build_video_maps_contract_shape() -> None:
    item = adapter.build_video(POPULAR_ITEMS[0], base_url="http://127.0.0.1:8000")

    assert item["id"] == f"bv_{BVID_A}"
    assert item["type"] == "video"
    assert item["title"] == "高质量A"
    assert item["subtitle"] == "测试UP"
    assert item["source"] == "bilibili"
    assert item["source_url"] == f"https://www.bilibili.com/video/{BVID_A}"
    assert item["cover_url"] == f"http://127.0.0.1:8000/v1/images/bv_{BVID_A}/file?variant=thumb"
    assert item["tags"] == ["游戏"]
    assert item["is_sensitive"] is False
    payload = item["payload"]
    assert payload["bvid"] == BVID_A
    assert payload["uploader"] == "测试UP"
    assert payload["play_count"] == 1000
    assert payload["duration"] == 300
    assert payload["like_count"] == 400
    assert payload["like_rate"] == pytest.approx(0.4)
    # hot_score 沿用契约的 0-100 量纲：就是点赞率百分比
    assert payload["hot_score"] == pytest.approx(40.0)
    assert payload["is_hot"] is True
    assert payload["category"] == "游戏"
    assert payload["published_at"].isoformat().startswith("2026-09-")


def test_build_video_marks_low_like_rate_as_not_hot() -> None:
    item = adapter.build_video(POPULAR_ITEMS[2], base_url="http://127.0.0.1:8000")
    assert item["payload"]["is_hot"] is False
    assert item["payload"]["like_rate"] == pytest.approx(0.01)


def test_like_rate_guards_zero_view() -> None:
    assert adapter.like_rate_of(video(BVID_A, view=0, like=10)) == 0.0
    assert adapter.like_rate_of({"stat": "坏值"}) == 0.0


def test_cover_normalisation_and_variants() -> None:
    assert adapter.normalise_cover(COVER) == COVER.replace("http://", "https://")
    # 上游可能自带缩放后缀，必须去掉后再拼我们自己的
    assert adapter.normalise_cover(f"{COVER}@320w_200h.jpg") == COVER.replace("http://", "https://")
    assert adapter.normalise_cover("") == ""
    thumb = adapter.cover_variant(COVER, "thumb")
    assert thumb.endswith(adapter.THUMB_SUFFIX)
    assert thumb.startswith("https://i0.hdslb.com/")
    assert adapter.cover_variant(COVER, "original") == COVER.replace("http://", "https://")
    assert adapter.cover_variant("", "thumb") == ""


# ---------------------------------------------------------------- 客户端语义


def test_client_sends_browser_headers_upstream() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=envelope([]))

    make_client(handler).get_json("/x/web-interface/popular", params={"ps": 20, "pn": 1})
    assert seen[0].headers["referer"] == "https://www.bilibili.com/"
    assert seen[0].headers["user-agent"].startswith("Mozilla/5.0")
    assert "zh-CN" in seen[0].headers["accept-language"]
    assert "cookie" not in seen[0].headers
    assert str(seen[0].url).startswith("https://api.bilibili.com/x/web-interface/popular")


def test_client_sends_optional_cookie_when_configured() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=envelope([]))

    make_client(handler, settings(bilibili_cookie="buvid3=test")).get_json(
        "/x/web-interface/popular"
    )
    assert seen[0].headers["cookie"] == "buvid3=test"


def test_client_maps_risk_control_and_missing() -> None:
    def risk(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -352, "message": "-352", "data": None})

    with pytest.raises(AppError) as blocked:
        make_client(risk).get_json("/x/web-interface/ranking/v2", params={"rid": 0})
    assert blocked.value.code == "UPSTREAM_UNAVAILABLE"
    assert blocked.value.details["reason"] == "risk_control"
    # 上游 message 不外泄：只保留稳定的 reason
    assert "-352" not in blocked.value.message

    def blocked_http(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412)

    with pytest.raises(AppError) as banned:
        make_client(blocked_http).get_json("/x/web-interface/ranking/v2")
    assert banned.value.details["reason"] == "risk_control"

    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -404, "message": "啥都木有", "data": None})

    with pytest.raises(AppError) as gone:
        make_client(missing).get_json("/x/web-interface/view", params={"bvid": "BV1bad"})
    assert gone.value.code == "NOT_FOUND"


def test_client_rejects_unexpected_image_host() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x", headers={"content-type": "image/jpeg"})

    with pytest.raises(AppError) as error:
        make_client(handler).get_bytes("https://evil.example.com/a.jpg")
    assert error.value.details["reason"] == "unexpected_image_host"


# ------------------------------------------------------------------ 数据源


def test_all_videos_prioritises_high_like_rate() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=envelope(POPULAR_ITEMS))

    bilibili_source.configure(settings(), client=make_client(handler))
    items = bilibili_source.all_videos("http://127.0.0.1:8000", limit=3)

    assert [item["payload"]["bvid"] for item in items] == [BVID_A, BVID_B, BVID_C]
    assert items[0]["payload"]["like_rate"] > items[1]["payload"]["like_rate"]
    # 热门综合榜：至少翻 MIN_POPULAR_PAGES 页，但每页只打一次上游
    assert calls.count("/x/web-interface/popular") == bilibili_source.MIN_POPULAR_PAGES
    # 封面地址已进缓存，图片代理不需要额外请求
    assert bilibili_source._cover_cache[f"bv_{BVID_A}"][1].startswith("https://i0.hdslb.com/")


def test_all_videos_fills_up_when_few_pass_threshold() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope(POPULAR_ITEMS))

    bilibili_source.configure(settings(), client=make_client(handler))
    items = bilibili_source.all_videos("http://127.0.0.1:8000", limit=12)

    # 只有 2 条越过阈值，其余按点赞率补齐，绝不返回空列表
    assert len(items) == 3
    assert items[-1]["payload"]["like_rate"] < 0.1


def test_all_videos_uses_cache_for_repeat_calls() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=envelope(POPULAR_ITEMS))

    bilibili_source.configure(settings(), client=make_client(handler))
    bilibili_source.all_videos("http://127.0.0.1:8000", limit=3)
    first = len(calls)
    bilibili_source.all_videos("http://127.0.0.1:8000", limit=3)

    assert len(calls) == first  # 第二次全部命中 TTL 缓存


def test_category_ranking_uses_rid_and_falls_back_to_stale() -> None:
    state = {"risk": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/ranking/v2":
            assert request.url.params["rid"] == "4"
            if state["risk"]:
                return httpx.Response(200, json={"code": -352, "message": "-352", "data": None})
            return httpx.Response(200, json=envelope(POPULAR_ITEMS))
        return httpx.Response(404)

    bilibili_source.configure(settings(), client=make_client(handler))
    first = bilibili_source.all_videos("http://127.0.0.1:8000", category="game", limit=2)
    assert [item["payload"]["category"] for item in first] == ["游戏", "游戏"]

    # 把分类榜缓存人为置为过期，再让上游返回风控 → 必须回落到上一次结果
    stored_at, entries = bilibili_source._ranking_cache[4]
    bilibili_source._ranking_cache[4] = (stored_at - 10_000, entries)
    state["risk"] = True
    stale = bilibili_source.all_videos("http://127.0.0.1:8000", category="game", limit=2)
    assert [item["payload"]["bvid"] for item in stale] == [BVID_A, BVID_B]


def test_category_ranking_without_cache_raises_risk_control() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -352, "message": "-352", "data": None})

    bilibili_source.configure(settings(), client=make_client(handler))
    with pytest.raises(AppError) as blocked:
        bilibili_source.all_videos("http://127.0.0.1:8000", category="dance", limit=2)
    assert blocked.value.details["reason"] == "risk_control"
    assert bilibili_source.degraded() is True


def test_image_bytes_uses_webp_variant_and_self_heals_cover() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/x/web-interface/view":
            return httpx.Response(200, json=view_envelope(POPULAR_ITEMS[0]))
        return httpx.Response(200, content=b"webpbytes", headers={"content-type": "image/webp"})

    bilibili_source.configure(settings(), client=make_client(handler))
    # 封面缓存为空：必须用 view 接口自愈，而不是直接失败
    resolved = bilibili_source.image_bytes(f"bv_{BVID_A}", "thumb", 0)

    assert resolved is not None
    content, media_type = resolved
    assert content == b"webpbytes"
    assert media_type == "image/webp"
    assert any("/x/web-interface/view" in url for url in seen)
    assert any(adapter.THUMB_SUFFIX in url for url in seen)


def test_item_by_id_uses_view_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/x/web-interface/view"
        assert request.url.params["bvid"] == BVID_A
        return httpx.Response(200, json=view_envelope(POPULAR_ITEMS[0]))

    bilibili_source.configure(settings(), client=make_client(handler))
    item = bilibili_source.item_by_id(f"bv_{BVID_A}", base_url="http://127.0.0.1:8000")

    assert item is not None
    assert item["id"] == f"bv_{BVID_A}"
    assert item["payload"]["uploader"] == "测试UP"
    assert bilibili_source.item_by_id("video_0001", base_url="http://127.0.0.1:8000") is None


# ------------------------------------------------------------------ 接口层


def test_daily_bilibili_returns_upstream_items(client: TestClient, auth: dict[str, str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope(POPULAR_ITEMS))

    bilibili_source.configure(settings(), client=make_client(handler))
    response = client.get("/v1/daily/bilibili", headers=auth)

    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    assert [item["id"] for item in items] == [f"bv_{BVID_A}", f"bv_{BVID_B}", f"bv_{BVID_C}"]
    assert all(item["source"] == "bilibili" for item in items)
    assert all(item["type"] == "video" for item in items)


def test_daily_bilibili_rejects_unknown_category(client: TestClient, auth: dict[str, str]) -> None:
    bilibili_source.configure(settings(), client=make_client(lambda request: httpx.Response(500)))
    response = client.get("/v1/daily/bilibili", params={"category": "不存在"}, headers=auth)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


def test_daily_bilibili_category_hits_ranking(client: TestClient, auth: dict[str, str]) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=envelope(POPULAR_ITEMS))

    bilibili_source.configure(settings(), client=make_client(handler))
    response = client.get("/v1/daily/bilibili", params={"category": "game"}, headers=auth)

    assert response.status_code == 200, response.text
    assert "/x/web-interface/ranking/v2" in paths


def test_image_file_proxies_hdslb_bytes(client: TestClient, auth: dict[str, str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/view":
            return httpx.Response(200, json=view_envelope(POPULAR_ITEMS[0]))
        return httpx.Response(200, content=b"jpgbytes", headers={"content-type": "image/jpeg"})

    bilibili_source.configure(settings(), client=make_client(handler))
    response = client.get(f"/v1/images/bv_{BVID_A}/file", headers=auth)

    assert response.status_code == 200, response.text
    assert response.content == b"jpgbytes"
    assert response.headers["content-type"] == "image/jpeg"


def test_image_file_requires_active_source(client: TestClient, auth: dict[str, str]) -> None:
    # 默认 mock 源：bv_ 前缀的条目必须 404，不能落到静态图片目录
    response = client.get(f"/v1/images/bv_{BVID_A}/file", headers=auth)
    assert response.status_code == 404
    assert response.json()["error"]["details"]["reason"] == "content_source_mock"


def test_health_reports_degraded_after_upstream_failure(
    client: TestClient, auth: dict[str, str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -352, "message": "-352", "data": None})

    bilibili_source.configure(settings(), client=make_client(handler))
    with pytest.raises(AppError):
        bilibili_source.all_videos("http://127.0.0.1:8000", category="dance", limit=2)

    health = client.get("/health").json()["data"]
    assert health["status"] == "degraded"


def test_throttle_waits_between_upstream_calls() -> None:
    stamps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        stamps.append(time.monotonic())
        return httpx.Response(200, json=envelope([]))

    config = settings(bilibili_min_interval_seconds=0.05)
    bilibili_source.configure(config, client=make_client(handler, config))
    bilibili_source._call(bilibili_source.client().get_json, "/x/web-interface/popular")
    bilibili_source._call(bilibili_source.client().get_json, "/x/web-interface/popular")

    assert len(stamps) == 2
    assert stamps[1] - stamps[0] >= 0.04