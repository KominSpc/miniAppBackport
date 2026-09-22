"""Pixiv 适配器：id 映射、敏感判定、载荷映射、错误映射与图片代理。

全部用例都注入 ``httpx.MockTransport``：不产生真实网络请求，也不依赖登录态。
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.errors import AppError
from app.services import catalog
from app.services.pixiv import adapter
from app.services.pixiv import source as pixiv_source
from app.services.pixiv.client import PixivClient

RANKING_PAYLOAD = {
    "contents": [
        {
            "illust_id": 111,
            "title": "初音ミク",
            "user_id": "9001",
            "user_name": "绘师A",
            "width": 1200,
            "height": 1600,
            "tags": ["初音ミク", "VOCALOID"],
            "url": "https://i.pximg.net/c/240x480/img-master/111_p0_master1200.jpg",
            "x_restrict": 0,
            "illust_content_type": {"sexual": 0, "grotesque": 0},
            "illust_type": 0,
            "rank": 1,
        },
        {
            "illust_id": 222,
            "title": "R-18 作品",
            "user_name": "绘师B",
            "width": 800,
            "height": 1200,
            "tags": ["R-18", "オリジナル"],
            "url": "https://i.pximg.net/c/240x480/img-master/222_p0_master1200.jpg",
            "x_restrict": 1,
        },
        {"is_ad_container": True},
    ]
}

DETAIL_PAYLOAD = {
    "error": False,
    "message": "",
    "body": {
        "id": "111",
        "title": "初音ミク",
        "userId": "9001",
        "userName": "绘师A",
        "width": 1200,
        "height": 1600,
        "pageCount": 2,
        "xRestrict": 0,
        "createDate": "2026-09-01T12:00:00+09:00",
        "tags": {"tags": [{"tag": "初音ミク", "translation": {"en": "Hatsune Miku"}}]},
    },
}


def settings(**overrides) -> Settings:
    base = {
        "content_source": "pixiv",
        "pixiv_cookie": "PHPSESSID=test",
        "pixiv_max_retries": 0,
        "pixiv_retry_initial_seconds": 0.01,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def make_client(handler, config: Settings | None = None) -> PixivClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="https://www.pixiv.net")
    return PixivClient(config or settings(), client=http)


@pytest.fixture(autouse=True)
def restore_source():
    """每个用例前后都还原内容源，避免污染 session 级 app。"""
    pixiv_source.reset()
    yield
    pixiv_source.reset()


# --------------------------------------------------------------- 动图帧表缓存


def test_ugoira_meta_caches_only_definitive_absence() -> None:
    """「不是动图」是稳定结论，可以缓存；网络失败绝不能缓存。

    用户实测：首次因网络失败的作品，第二次直接按缓存判成「非动图」→ 只能看静态图。
    """
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.path)
        return httpx.Response(404, json={"error": True, "message": "指定的ID不是动图"})

    pixiv_source.configure(settings(), client=make_client(handler))

    assert pixiv_source.ugoira_meta("111") is None
    assert pixiv_source.ugoira_meta("111") is None
    assert len(hits) == 1, "非动图判定要缓存，第二次不该再回源"


def test_ugoira_meta_network_failure_is_retryable() -> None:
    """一次网络抖动之后，第二次仍要能拿到帧表（写进缓存就等于把动图判死）。"""
    hits: list[str] = []
    broken = {"on": True}

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.path)
        if broken["on"]:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(
            200,
            json={
                "error": False,
                "message": "",
                "body": {
                    "src": "https://i.pximg.net/ugoira/src/222_ugoira0.jpg",
                    "original_src": "https://i.pximg.net/ugoira/src/222_ugoira0.jpg",
                    "frames": [{"file": "000000.jpg", "delay": 120}],
                },
            },
        )

    pixiv_source.configure(settings(), client=make_client(handler))

    assert pixiv_source.ugoira_meta("222") is None, "网络失败这一次拿不到"
    assert len(hits) == 1

    broken["on"] = False
    meta = pixiv_source.ugoira_meta("222")
    assert meta is not None, "网络失败不能被写成「这不是动图」缓存"
    assert meta["frames"][0]["file"] == "000000.jpg"
    assert len(hits) == 2


# ------------------------------------------------------------------ id 映射


def test_content_id_round_trip() -> None:
    assert adapter.content_id_for(123) == "px_123"
    assert adapter.illust_id_of("px_123") == "123"
    assert adapter.is_pixiv_id("px_123") is True
    assert adapter.illust_id_of("img_0001") is None
    assert adapter.illust_id_of("px_abc") is None
    assert adapter.is_pixiv_id("img_0001") is False


# ------------------------------------------------------------- 敏感内容判定


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"xRestrict": 0, "tags": ["初音ミク"]}, False),
        # 实测：普通插画也可能 sl=2，只凭 sl 会误判 —— 这里根本不看 sl
        ({"xRestrict": 0, "sl": 2, "tags": ["初音ミク"]}, False),
        ({"xRestrict": 1}, True),
        ({"x_restrict": 2}, True),
        ({"xRestrict": 0, "illust_content_type": {"sexual": 1}}, True),
        ({"illustContentType": {"grotesque": 1}}, True),
        ({"x_restrict": 0, "tags": ["R-18"]}, True),
        ({"x_restrict": 0, "tags": ["エロ"]}, True),
        ({"x_restrict": "0", "tags": "R18"}, False),
    ],
)
def test_is_sensitive(item: dict, expected: bool) -> None:
    assert adapter.is_sensitive(item) is expected


# ------------------------------------------------------------------ 载荷映射


def test_ranking_illusts_skips_non_illust_entries() -> None:
    parsed = adapter.ranking_illusts(RANKING_PAYLOAD)
    assert [entry["illust_id"] for entry in parsed] == [111, 222]
    assert adapter.ranking_illusts({"contents": "坏值"}) == []
    assert adapter.ranking_illusts(None) == []


def test_build_illust_from_ranking_shape() -> None:
    item = adapter.build_illust(RANKING_PAYLOAD["contents"][0], base_url="http://127.0.0.1:8000")

    assert item["id"] == "px_111"
    assert item["type"] == "image"
    assert item["subtitle"] == "绘师A"
    assert item["source"] == "pixiv"
    assert item["source_url"] == "https://www.pixiv.net/artworks/111"
    assert item["cover_url"] == "http://127.0.0.1:8000/v1/images/px_111/file?variant=thumb"
    assert item["payload"]["image_url"] == "http://127.0.0.1:8000/v1/images/px_111/file?variant=original"
    assert item["payload"]["aspect_ratio"] == pytest.approx(0.75)
    assert item["is_sensitive"] is False
    # 上游缩略图只用于服务端预热，不下发给客户端
    assert "_upstream_thumbnail" in item["payload"]
    assert "_upstream_thumbnail" not in adapter.strip_internal(item)["payload"]


def test_build_illust_from_ajax_shape() -> None:
    item = adapter.build_illust(DETAIL_PAYLOAD["body"], base_url="http://127.0.0.1:8000")

    assert item["id"] == "px_111"
    assert item["title"] == "初音ミク"
    assert item["tags"] == ["初音ミク"]
    assert item["payload"]["page_count"] == 2
    assert item["payload"]["user_id"] == "9001"
    assert item["payload"]["created_at"].isoformat() == "2026-09-01T11:00:00+08:00"


def test_page_urls_and_pick_url() -> None:
    envelope = {
        "error": False,
        "body": [{"urls": {"thumb_mini": "t", "small": "s", "regular": "r", "original": "o"}}],
    }
    urls = adapter.page_urls(envelope)
    assert urls == [{"thumb_mini": "t", "small": "s", "regular": "r", "original": "o"}]
    assert adapter.pick_url(urls[0], "thumb") == "s"
    assert adapter.pick_url(urls[0], "regular") == "r"
    assert adapter.pick_url(urls[0], "original") == "o"
    # thumb 一档优先要能摆在卡片上的规格：thumb_mini 只有 128×128，放列表上必糊
    assert adapter.pick_url({"thumb_mini": "t", "small": "s"}, "thumb") == "s"
    assert adapter.pick_url({"thumb_mini": "t"}, "thumb") == "t"
    assert adapter.pick_url({}, "thumb") is None
    assert adapter.page_urls({"body": "坏值"}) == []


def test_upgrade_thumb_raises_small_listing_thumbs() -> None:
    """列表缩略图统一抬到 540：作者页给的 250 方图与作品页的 128 方图都要抬。"""
    base = "https://i.pximg.net"
    assert (
        adapter.upgrade_thumb(f"{base}/c/250x250_80_a2/img-master/img/1_p0_square1200.jpg")
        == f"{base}/c/540x540_70/img-master/img/1_p0_square1200.jpg"
    )
    assert (
        adapter.upgrade_thumb(f"{base}/c/128x128/img-master/img/1_p0_square1200.jpg")
        == f"{base}/c/540x540_70/img-master/img/1_p0_square1200.jpg"
    )
    # 本来就够大 / 不是 pximg 的裁剪链接：原样返回
    big = f"{base}/c/540x540_70/img-master/img/1_p0_master1200.jpg"
    assert adapter.upgrade_thumb(big) == big
    assert adapter.upgrade_thumb("https://example.com/cover.jpg") == "https://example.com/cover.jpg"


# ---------------------------------------------------------------- 客户端语义


def test_client_sends_cookie_and_referer_upstream() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"error": False, "body": []})

    make_client(handler).get_json("/ajax/illust/111/pages", params={"lang": "zh"})
    assert seen[0].headers["cookie"] == "PHPSESSID=test"
    assert seen[0].headers["referer"] == "https://www.pixiv.net/"
    assert "zh-CN" in seen[0].headers["accept-language"]


def test_client_maps_login_state_and_missing() -> None:
    def unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    with pytest.raises(AppError) as blocked:
        make_client(unauthorized).get_json("/ajax/illust/111")
    assert blocked.value.code == "UPSTREAM_UNAVAILABLE"
    assert blocked.value.details["reason"] == "login_state_invalid"

    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://accounts.pixiv.net/login"})

    with pytest.raises(AppError) as login_page:
        make_client(redirect).get_json("/ajax/illust/111")
    assert login_page.value.code == "UPSTREAM_UNAVAILABLE"

    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(AppError) as gone:
        make_client(missing).get_json("/ajax/illust/111")
    assert gone.value.code == "NOT_FOUND"


def test_client_rate_limit_uses_retry_after() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "0"})

    with pytest.raises(AppError) as limited:
        make_client(handler).get_json("/ranking.php")
    assert limited.value.code == "RATE_LIMITED"
    assert limited.value.details["retry_after"] >= 1
    assert len(calls) == 1, "max_retries=0 时不再重试"


def test_client_retries_then_succeeds() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"error": False, "body": {"ok": 1}})

    client = make_client(handler, settings(pixiv_max_retries=2))
    assert client.get_json("/ranking.php")["body"] == {"ok": 1}
    assert len(calls) == 2


def test_client_rebuilds_pool_after_connection_error(monkeypatch) -> None:
    """连接级失败：丢掉坏掉的连接池、换一条连接重试，最终成功。

    本机走代理（Clash / SakuraCat）时最常见：代理把连接握在手里，上游断了不是
    立刻 RST 而是「干等」。只重试、不换连接，等于一直复用同一条坏连接 —— 表现
    就是「pixiv 一直转圈，之后每次也一样，直到重启后端」。
    """

    class FakeHttp:
        instances: list["FakeHttp"] = []

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.closed = False
            self.calls = 0
            FakeHttp.instances.append(self)

        def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
            self.calls += 1
            if len(FakeHttp.instances) == 1:
                raise httpx.ConnectError("stale keep-alive", request=httpx.Request(method, url))
            return httpx.Response(200, json={"error": False, "body": {"ok": 1}})

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("app.services.pixiv.client.httpx.Client", FakeHttp)
    client = PixivClient(settings(pixiv_max_retries=3))
    assert client.get_json("/ranking.php")["body"] == {"ok": 1}
    assert len(FakeHttp.instances) == 2, "第一次失败之后必须重建客户端"
    assert FakeHttp.instances[0].closed is True, "坏掉的连接池要被丢掉"
    assert FakeHttp.instances[1].calls == 1


def test_client_timeout_retries_once_then_gives_up(monkeypatch) -> None:
    """超时：只允许为「换一条连接」多试一次，之后立刻认输。

    默认 20 秒 × 4 次 = 80 秒，而客户端 20 秒读超时早就放弃了 —— 用户看到的是
    「作者页一直转圈」。换过连接还超时，说明上游真不通，必须快速失败。
    """

    class FakeHttp:
        instances: list["FakeHttp"] = []

        def __init__(self, **kwargs: object) -> None:
            self.closed = False
            self.calls = 0
            FakeHttp.instances.append(self)

        def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
            self.calls += 1
            raise httpx.ReadTimeout("hang", request=httpx.Request(method, url))

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("app.services.pixiv.client.httpx.Client", FakeHttp)
    client = PixivClient(settings(pixiv_max_retries=3))
    with pytest.raises(AppError) as failure:
        client.get_json("/ajax/user/9001")
    assert failure.value.details["reason"] == "ReadTimeout"
    assert len(FakeHttp.instances) == 2, "超时只换一次连接，不做 4 轮"


def test_client_rejects_error_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
         return httpx.Response(200, json={"error": True, "message": "boom"})

    with pytest.raises(AppError) as failure:
        make_client(handler).get_json("/ajax/illust/111")
    assert failure.value.code == "UPSTREAM_UNAVAILABLE"


# ---------------------------------------------------- 数据源装配（端到端）


def test_source_serves_ranking_and_filters_sensitive() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ranking.php":
            return httpx.Response(200, json=RANKING_PAYLOAD)
        if request.url.path == "/ajax/illust/111/pages":
            return httpx.Response(
                200,
                json={
                    "error": False,
                    "body": [
                        {"urls": {"thumb_mini": "https://i.pximg.net/t.jpg"}},
                        {"urls": {"thumb_mini": "https://i.pximg.net/t2.jpg"}},
                    ],
                },
            )
        if request.url.path == "/ajax/illust/111":
            return httpx.Response(200, json=DETAIL_PAYLOAD)
        if request.url.host == "i.pximg.net":
            return httpx.Response(200, content=b"px-bytes", headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    pixiv_source.configure(settings(), client=make_client(handler))
    items = catalog.all_images("http://127.0.0.1:8000")

    assert [item["id"] for item in items] == ["px_111"], "R18 条目在 safe_mode 下必须被过滤"
    assert items[0]["source"] == "pixiv"
    assert items[0]["cover_url"].startswith("http://127.0.0.1:8000/v1/images/px_111/file")

    detail = catalog.item_by_id("px_111", base_url="http://127.0.0.1:8000")
    assert detail is not None
    assert detail["payload"]["page_count"] == 2

    # 榜单已经预热 page 0 的缩略图：thumb 直接回源图片字节，不需要再请求 /pages
    resolved = pixiv_source.image_bytes("px_111", "thumb", 0)
    assert resolved is not None
    assert resolved[0] == b"px-bytes"
    assert resolved[1] == "image/jpeg"
    assert pixiv_source.pages_for("111")[0]["thumb_mini"] == (
        "https://i.pximg.net/c/240x480/img-master/111_p0_master1200.jpg"
    ), "榜单预热了 page 0，首屏不发生 N+1"


def test_source_proxies_pximg_with_referer() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "i.pximg.net":
            assert request.headers["referer"] == "https://www.pixiv.net/", "pximg 必须带 Referer"
            return httpx.Response(200, content=b"jpeg-bytes", headers={"content-type": "image/jpeg"})
        if request.url.path.endswith("/pages"):
            return httpx.Response(
                200,
                json={
                    "error": False,
                    "body": [{"urls": {"thumb_mini": "https://i.pximg.net/px.jpg"}}],
                },
            )
        return httpx.Response(404)

    pixiv_source.configure(settings(), client=make_client(handler))
    resolved = pixiv_source.image_bytes("px_999", "thumb", 0)
    assert resolved is not None and resolved[0] == b"jpeg-bytes"
    assert any(request.url.path.endswith("/pages") for request in seen), "未缓存时必须回源 /pages"
    assert any(request.url.host == "i.pximg.net" for request in seen), "图片必须由服务端回源"


def test_health_degrades_without_login_state(client: TestClient) -> None:
    pixiv_source.configure(Settings(content_source="pixiv"))
    body = client.get("/health").json()
    assert body["data"]["status"] == "degraded"

    pixiv_source.configure(Settings(content_source="pixiv", pixiv_cookie="PHPSESSID=x"))
    assert client.get("/health").json()["data"]["status"] == "ok"

    pixiv_source.configure(Settings(content_source="mock"))
    assert client.get("/health").json()["data"]["status"] == "ok"


def test_image_route_proxies_pixiv_ids(client: TestClient, auth: dict) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pages"):
            return httpx.Response(
                200,
                json={
                    "error": False,
                    "body": [{"urls": {"thumb_mini": "https://i.pximg.net/px.jpg"}}],
                },
            )
        return httpx.Response(200, content=b"pximg-bytes", headers={"content-type": "image/jpeg"})

    pixiv_source.configure(settings(), client=make_client(handler))
    response = client.get("/v1/images/px_111/file?variant=thumb", headers=auth)
    assert response.status_code == 200
    assert response.content == b"pximg-bytes"
    assert response.headers["content-type"].startswith("image/jpeg")

    pixiv_source.reset()
    missing = client.get("/v1/images/px_111/file?variant=thumb", headers=auth)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"


# ------------------------------------------------------------ 关键词搜索


SEARCH_PAYLOAD = {
    "error": False,
    "body": {
        "illustManga": {
            "data": [
                {
                    "id": "333",
                    "title": "樱花与少女",
                    "userId": "9002",
                    "userName": "绘师C",
                    "width": 1000,
                    "height": 1400,
                    "pageCount": 1,
                    "illustType": 0,
                    "xRestrict": 0,
                    "createDate": "2026-09-02T10:00:00+09:00",
                    "url": "https://i.pximg.net/c/250x250_80_a2/img-master/img/333_p0_square1200.jpg",
                    "tags": ["桜", "オリジナル"],
                },
                {
                    "id": "444",
                    "title": "R-18 樱花",
                    "userName": "绘师D",
                    "width": 800,
                    "height": 1200,
                    "xRestrict": 1,
                    "url": "https://i.pximg.net/c/250x250_80_a2/img-master/img/444_p0_square1200.jpg",
                    "tags": ["R-18"],
                },
            ],
            "total": 2,
            "lastPage": 1,
        }
    },
}

SEARCH_DETAIL_PAYLOAD = {
    "error": False,
    "message": "",
    "body": {
        "id": "333",
        "title": "樱花与少女",
        "userId": "9002",
        "userName": "绘师C",
        "width": 1000,
        "height": 1400,
        "pageCount": 1,
        "xRestrict": 0,
        "tags": {"tags": [{"tag": "桜"}]},
    },
}


def search_handler(seen: list[httpx.Request], *, detail: object = SEARCH_DETAIL_PAYLOAD):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.startswith("/ajax/search/artworks/"):
            return httpx.Response(200, json=SEARCH_PAYLOAD)
        if request.url.path == "/ajax/illust/333":
            return httpx.Response(200, json=detail)
        if request.url.host == "i.pximg.net":
            return httpx.Response(
                200, content=b"px-bytes", headers={"content-type": "image/jpeg"}
            )
        return httpx.Response(404)

    return handler


def test_search_illusts_parses_envelope() -> None:
    items, last_page = adapter.search_illusts(SEARCH_PAYLOAD)

    assert [item["id"] for item in items] == ["333", "444"]
    assert last_page == 1
    assert adapter.search_illusts({"body": {"illustManga": {"data": "坏值"}}}) == ([], 0)
    assert adapter.search_illusts(None) == ([], 0)


def test_source_search_maps_safe_items_and_warms_thumbnail() -> None:
    seen: list[httpx.Request] = []
    pixiv_source.configure(settings(), client=make_client(search_handler(seen)))

    items = pixiv_source.search_images("http://127.0.0.1:8000", "樱花")

    assert [item["id"] for item in items] == ["px_333"], "R18 条目在 safe_mode 下必须被过滤"
    assert items[0]["title"] == "樱花与少女"
    assert items[0]["source"] == "pixiv"
    assert items[0]["cover_url"] == "http://127.0.0.1:8000/v1/images/px_333/file?variant=thumb"
    request = seen[0]
    assert request.url.path.startswith("/ajax/search/artworks/")
    assert request.url.params["word"] == "樱花"
    assert request.url.params["p"] == "1"
    assert request.url.params["order"] == "date_d"

    # 搜索自带的缩略图已预热 page 0：首屏缩略图不再回源 /pages
    resolved = pixiv_source.image_bytes("px_333", "thumb", 0)
    assert resolved is not None and resolved[0] == b"px-bytes"
    assert not any(request.url.path.endswith("/pages") for request in seen)


def test_source_search_caches_pages_and_marks_favorites() -> None:
    seen: list[httpx.Request] = []
    pixiv_source.configure(settings(), client=make_client(search_handler(seen)))

    first = pixiv_source.search_images("http://127.0.0.1:8000", "樱花", limit=20)
    second = pixiv_source.search_images(
        "http://127.0.0.1:8000", "樱花", favorited_ids=frozenset({"px_333"}), limit=40
    )

    assert len(seen) == 1, "同一页命中缓存后不再打上游"
    assert second[0]["payload"]["is_favorited"] is True
    assert [item["id"] for item in first] == [item["id"] for item in second]


def test_catalog_search_hits_upstream_instead_of_ranking() -> None:
    seen: list[httpx.Request] = []
    pixiv_source.configure(settings(), client=make_client(search_handler(seen)))

    items = catalog.search_images("http://127.0.0.1:8000", "樱花")

    assert [item["id"] for item in items] == ["px_333"]
    assert any(request.url.path.startswith("/ajax/search/artworks/") for request in seen)
    assert not any(request.url.path == "/ranking.php" for request in seen), (
        "搜索不能退化成榜单子串过滤"
    )


def test_search_endpoint_paginates_upstream_results(client: TestClient, auth: dict) -> None:
    seen: list[httpx.Request] = []
    pixiv_source.configure(settings(), client=make_client(search_handler(seen)))

    response = client.get("/v1/images/search", params={"q": "樱花", "limit": 1}, headers=auth)

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["data"]["items"]] == ["px_333"]
    assert body["meta"]["has_more"] is False
    assert body["meta"]["next_cursor"] is None
    assert any(request.url.path.startswith("/ajax/search/artworks/") for request in seen)


def test_search_endpoint_rejects_bad_cursor(client: TestClient, auth: dict) -> None:
    pixiv_source.configure(settings(), client=make_client(search_handler([])))

    response = client.get(
        "/v1/images/search", params={"q": "樱花", "cursor": "nope"}, headers=auth
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


# ------------------------------------------------------------ 收藏内存快照


def test_favorites_are_served_from_memory_snapshot(client: TestClient, auth: dict) -> None:
    seen: list[httpx.Request] = []
    pixiv_source.configure(settings(), client=make_client(search_handler(seen)))

    created = client.put("/v1/images/px_333/favorite", headers=auth)
    assert created.status_code == 200, created.text

    before = len(seen)
    listed = client.get("/v1/images/favorites", headers=auth)

    assert listed.status_code == 200, listed.text
    items = listed.json()["data"]["items"]
    assert [item["id"] for item in items] == ["px_333"]
    assert items[0]["payload"]["is_favorited"] is True
    assert items[0]["cover_url"] == "http://testserver/v1/images/px_333/file?variant=thumb"
    assert seen[before:] == [], "收藏列表必须直接读内存快照，不再逐条回源上游"

    removed = client.delete("/v1/images/px_333/favorite", headers=auth)
    assert removed.status_code == 200
    assert client.get("/v1/images/favorites", headers=auth).json()["data"]["items"] == []


def test_rebase_item_rewrites_same_origin_links() -> None:
    item = adapter.strip_internal(
        adapter.build_illust(SEARCH_PAYLOAD["body"]["illustManga"]["data"][0], base_url="http://a")
    )
    rebased = catalog.rebase_item(item, base_url="http://b", favorited=True)

    assert rebased["cover_url"] == "http://b/v1/images/px_333/file?variant=thumb"
    assert rebased["payload"]["image_url"] == "http://b/v1/images/px_333/file?variant=original"
    assert rebased["payload"]["is_favorited"] is True
    assert item["cover_url"] == "http://a/v1/images/px_333/file?variant=thumb", "原快照不被改写"


# ------------------------------------------------------------ 搜索筛选参数


def test_source_search_passes_filters_upstream() -> None:
    seen: list[httpx.Request] = []
    pixiv_source.configure(settings(), client=make_client(search_handler(seen)))

    pixiv_source.search_images(
        "http://127.0.0.1:8000", "樱花", min_bookmarks=5000, lang="ja", allow_r18=True
    )

    params = seen[0].url.params
    assert params["bl"] == "5000"
    assert params["lang"] == "ja"
    assert params["mode"] == "all"


def test_source_search_defaults_stay_safe_and_unfiltered() -> None:
    seen: list[httpx.Request] = []
    pixiv_source.configure(settings(), client=make_client(search_handler(seen)))

    pixiv_source.search_images("http://127.0.0.1:8000", "樱花")

    params = seen[0].url.params
    assert params["mode"] == "safe"
    assert params["lang"] == "zh"
    assert "bl" not in params


def test_source_search_cache_separates_filter_combinations() -> None:
    seen: list[httpx.Request] = []
    pixiv_source.configure(settings(), client=make_client(search_handler(seen)))

    pixiv_source.search_images("http://127.0.0.1:8000", "樱花")
    pixiv_source.search_images("http://127.0.0.1:8000", "樱花", min_bookmarks=1000)

    assert len(seen) == 2, "筛选条件不同必须分别回源，不能命中同一份缓存"


def test_catalog_search_scope_tracks_filters() -> None:
    assert catalog.search_scope("樱花") == catalog.search_scope(
        "樱花", min_bookmarks=None, lang=None, r18=False
    )
    assert catalog.search_scope("樱花") != catalog.search_scope("樱花", min_bookmarks=1000)
    assert catalog.search_scope("樱花") != catalog.search_scope("樱花", lang="ja")
    assert catalog.search_scope("樱花") != catalog.search_scope("樱花", r18=True)


def test_catalog_search_r18_requires_safe_mode_off() -> None:
    seen: list[httpx.Request] = []
    pixiv_source.configure(settings(), client=make_client(search_handler(seen)))

    catalog.search_images("http://127.0.0.1:8000", "樱花", safe_mode=True, r18=True)

    assert seen[0].url.params["mode"] == "safe", "safe_mode 未关闭时 R18 闸门必须拦下"


def test_search_endpoint_passes_filters_and_keeps_r18_gate(
    client: TestClient, auth: dict
) -> None:
    seen: list[httpx.Request] = []
    pixiv_source.configure(settings(), client=make_client(search_handler(seen)))

    response = client.get(
        "/v1/images/search",
        params={"q": "樱花", "min_bookmarks": 5000, "lang": "ja", "r18": "true"},
        headers=auth,
    )

    assert response.status_code == 200, response.text
    params = seen[0].url.params
    assert params["bl"] == "5000"
    assert params["lang"] == "ja"
    assert params["mode"] == "safe", "默认 safe_mode=true，r18 参数不能绕过闸门"


def test_search_endpoint_allows_r18_after_opt_out(client: TestClient, auth: dict) -> None:
    seen: list[httpx.Request] = []
    pixiv_source.configure(settings(), client=make_client(search_handler(seen)))

    updated = client.put(
        "/v1/users/me/preferences",
        json={"tags": [], "platforms": [], "genres": [], "safe_mode": False},
        headers=auth,
    )
    assert updated.status_code == 200, updated.text

    response = client.get(
        "/v1/images/search", params={"q": "樱花", "r18": "true"}, headers=auth
    )

    assert response.status_code == 200, response.text
    assert seen[0].url.params["mode"] == "all"


def test_search_endpoint_rejects_bad_filter_values(client: TestClient, auth: dict) -> None:
    assert (
        client.get("/v1/images/search?q=樱花&min_bookmarks=-1", headers=auth).status_code == 422
    )
    assert client.get("/v1/images/search?q=樱花&lang=fr", headers=auth).status_code == 422
    assert (
        client.get("/v1/images/search?q=樱花&min_bookmarks=99999999", headers=auth).status_code
        == 422
    )

UGOIRA_SEARCH_PAYLOAD = {
    "error": False,
    "body": {
        "illustManga": {
            "data": [
                {
                    "id": "333",
                    "title": "静止画",
                    "userName": "绘师C",
                    "width": 1000,
                    "height": 1400,
                    "illustType": 0,
                    "xRestrict": 0,
                    "url": "https://i.pximg.net/c/250x250_80_a2/img-master/img/333_p0_square1200.jpg",
                    "tags": ["桜"],
                },
                {
                    "id": "555",
                    "title": "うごイラ",
                    "userName": "绘师E",
                    "width": 800,
                    "height": 800,
                    "illustType": 2,
                    "xRestrict": 0,
                    "url": "https://i.pximg.net/c/250x250_80_a2/img-master/img/555_p0_square1200.jpg",
                    "tags": ["うごイラ"],
                },
            ],
            "total": 2,
            "lastPage": 1,
        }
    },
}


def paged_search_handler(seen: list[httpx.Request], *, last_page: int = 5, per_page: int = 60):
    """每页返回互不相同的 id，用来验证翻页与 has_more。"""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.startswith("/ajax/search/artworks/"):
            page = int(request.url.params.get("p", "1"))
            data = [
                {
                    "id": str(page * 1000 + index),
                    "title": f"第{page}页第{index}张",
                    "userName": "绘师",
                    "width": 1000,
                    "height": 1400,
                    "illustType": 0,
                    "xRestrict": 0,
                    "url": f"https://i.pximg.net/c/250x250_80_a2/img-master/img/{page}_{index}_p0_square1200.jpg",
                    "tags": ["桜"],
                }
                for index in range(per_page)
            ]
            return httpx.Response(
                200,
                json={
                    "error": False,
                    "body": {
                        "illustManga": {
                            "data": data,
                            "total": last_page * per_page,
                            "lastPage": last_page,
                        }
                    },
                },
            )
        return httpx.Response(404)

    return handler


def test_source_search_ugoira_uses_upstream_type_and_local_filter() -> None:
    seen: list[httpx.Request] = []
    pixiv_source.configure(settings(), client=make_client(search_handler(seen)))

    items = pixiv_source.search_images(
        "http://127.0.0.1:8000", "樱花", illust_type="ugoira"
    )

    assert seen[0].url.params["type"] == "ugoira", "动图要给上游 type 提示（虽然实测会被忽略）"
    assert items == [], "上游忽略 type，本地按 illustType 过滤后没有动图就是空"


def test_source_search_ugoira_filters_by_illust_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/ajax/search/artworks/"):
            return httpx.Response(200, json=UGOIRA_SEARCH_PAYLOAD)
        return httpx.Response(404)

    pixiv_source.configure(settings(), client=make_client(handler))

    items = pixiv_source.search_images(
        "http://127.0.0.1:8000", "うごイラ", illust_type="ugoira"
    )

    assert [item["id"] for item in items] == ["px_555"]
    assert items[0]["payload"]["illust_type"] == 2


def test_source_search_manga_keeps_scanning_until_the_page_is_full() -> None:
    """作品类型在本地过滤，一页上游结果不够时要自动接着翻。

    上游每页 60 条里只有 1 条漫画，想凑够一页 20 条就得翻到第 4 页；
    不翻的话第一页只有 1 条，用户看到的就是「搜不到漫画」。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("p", "1"))
        data = [
            {
                "id": f"{page}_{index}",
                "title": f"第{page}页第{index}张",
                "userName": "绘师",
                "width": 1000,
                "height": 1400,
                "illustType": 1 if index == 0 else 0,
                "xRestrict": 0,
                "url": f"https://i.pximg.net/c/250x250_80_a2/img-master/img/{page}_{index}_p0_square1200.jpg",
                "tags": ["樱"],
            }
            for index in range(60)
        ]
        return httpx.Response(
            200,
            json={
                "error": False,
                "body": {
                    "illustManga": {"data": data, "total": 600, "lastPage": 10}
                },
            },
        )

    pixiv_source.configure(settings(), client=make_client(handler))

    images = pixiv_source.search_images("http://127.0.0.1:8000", "樱", illust_type="manga", limit=5)

    assert len(images) == 6, "多取一页：5 条窗口外再多拿 1 条，调用方才能判断还有没有下一页"
    assert all(item["payload"]["illust_type"] == 1 for item in images)


def test_source_search_looks_one_page_ahead_for_has_more() -> None:
    seen: list[httpx.Request] = []
    pixiv_source.configure(settings(), client=make_client(paged_search_handler(seen)))

    first = pixiv_source.search_images("http://127.0.0.1:8000", "桜", limit=20)

    assert len(first) == 120, "窗口只要 20 条，但要多看一页，调用方才能判断还有没有下一页"


def test_search_endpoint_keeps_has_more_past_the_first_upstream_page(
    client: TestClient, auth: dict
) -> None:
    pixiv_source.configure(settings(), client=make_client(paged_search_handler([])))

    cursor: str | None = None
    seen_ids: list[str] = []
    for page_no in range(1, 4):
        params: dict[str, object] = {"q": "桜", "limit": 20}
        if cursor:
            params["cursor"] = cursor
        response = client.get("/v1/images/search", params=params, headers=auth)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["meta"]["has_more"] is True, (
            f"第 {page_no} 页就到底了：搜索只能在首屏上游页里翻，无法无限下滑"
        )
        seen_ids.extend(item["id"] for item in body["data"]["items"])
        cursor = body["meta"]["next_cursor"]

    assert len(set(seen_ids)) == 60, "三页内容不能重复"


def test_search_endpoint_accepts_illust_type(client: TestClient, auth: dict) -> None:
    seen: list[httpx.Request] = []
    pixiv_source.configure(settings(), client=make_client(search_handler(seen)))

    response = client.get(
        "/v1/images/search",
        params={"q": "樱花", "illust_type": "ugoira"},
        headers=auth,
    )

    assert response.status_code == 200, response.text
    assert seen[0].url.params["type"] == "ugoira"
    assert response.json()["data"]["items"] == []


def test_search_endpoint_rejects_unknown_illust_type(client: TestClient, auth: dict) -> None:
    response = client.get(
        "/v1/images/search", params={"q": "樱花", "illust_type": "novel"}, headers=auth
    )

    assert response.status_code == 422


def test_search_scope_tracks_illust_type() -> None:
    assert catalog.search_scope("樱花") != catalog.search_scope("樱花", illust_type="ugoira")
    assert catalog.search_scope("樱花", illust_type="manga") != catalog.search_scope(
        "樱花", illust_type="ugoira"
    )
# ------------------------------------------------------------------ 动图（ugoira）

# 上游实测形状：动图不是视频，而是一个 zip（内含按序的 JPEG 帧）+ 逐帧延时。
UGOIRA_META_PAYLOAD = {
    "error": False,
    "message": "",
    "body": {
        "src": "https://i.pximg.net/img-zip-ugoira/img/2026/09/20/11/07/34/149877054_ugoira600x600.zip",
        "originalSrc": (
            "https://i.pximg.net/img-zip-ugoira/img/2026/09/20/11/07/34/149877054_ugoira1920x1080.zip"
        ),
        "mime_type": "image/jpeg",
        "frames": [
            {"file": "000000.jpg", "delay": 70},
            {"file": "000001.jpg", "delay": 60},
        ],
    },
}


def ugoira_handler(seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/ugoira_meta"):
            return httpx.Response(200, json=UGOIRA_META_PAYLOAD)
        if request.url.host == "i.pximg.net":
            assert request.headers["referer"] == "https://www.pixiv.net/", "pximg 必须带 Referer"
            return httpx.Response(
                200, content=b"PK\x03\x04zip", headers={"content-type": "application/zip"}
            )
        return httpx.Response(404)

    return handler


def test_source_reads_ugoira_frames_and_archive() -> None:
    seen: list[httpx.Request] = []
    pixiv_source.configure(settings(), client=make_client(ugoira_handler(seen)))

    meta = pixiv_source.ugoira_meta("149877054")

    assert meta is not None
    assert [frame["file"] for frame in meta["frames"]] == ["000000.jpg", "000001.jpg"]
    assert [frame["delay_ms"] for frame in meta["frames"]] == [70, 60]

    resolved = pixiv_source.ugoira_archive("149877054")
    assert resolved is not None and resolved[0].startswith(b"PK")
    assert resolved[1] == "application/zip"

    # 帧表与 zip 地址都走缓存：再问一次不该再打上游
    before = len(seen)
    pixiv_source.ugoira_meta("149877054")
    pixiv_source.ugoira_archive("149877054")
    assert len(seen) == before + 1, "只有 zip 字节每次都回源，帧表必须命中缓存"


def test_source_treats_non_animated_as_none() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"error": True, "message": "not found", "body": None})

    pixiv_source.configure(settings(), client=make_client(handler))

    assert pixiv_source.ugoira_meta("111") is None
    assert pixiv_source.ugoira_archive("111") is None
    # 已确认不是动图：负缓存生效，不再反复回源
    before = len(seen)
    assert pixiv_source.ugoira_meta("111") is None
    assert len(seen) == before


def test_ugoira_routes_return_frames_and_zip(client: TestClient, auth: dict) -> None:
    seen: list[httpx.Request] = []
    pixiv_source.configure(settings(), client=make_client(ugoira_handler(seen)))

    meta = client.get("/v1/images/px_149877054/ugoira", headers=auth)
    assert meta.status_code == 200, meta.text
    body = meta.json()["data"]
    assert body["id"] == "px_149877054"
    assert body["frame_count"] == 2
    assert body["total_ms"] == 130
    assert body["frames"][0] == {"file": "000000.jpg", "delay_ms": 70}
    assert body["archive_url"].endswith("/v1/images/px_149877054/ugoira/file")

    archive = client.get("/v1/images/px_149877054/ugoira/file", headers=auth)
    assert archive.status_code == 200
    assert archive.headers["content-type"] == "application/zip"
    assert archive.content.startswith(b"PK")

    original = client.get(
        "/v1/images/px_149877054/ugoira/file?variant=original", headers=auth
    )
    assert original.status_code == 200
    assert any("1920x1080" in str(request.url) for request in seen), "原图要取原分辨率 zip"


def test_ugoira_routes_404_for_static_illust(client: TestClient, auth: dict) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": True, "message": "not found", "body": None})

    pixiv_source.configure(settings(), client=make_client(handler))
    assert client.get("/v1/images/px_111/ugoira", headers=auth).status_code == 404
    assert client.get("/v1/images/px_111/ugoira/file", headers=auth).status_code == 404

    # 非 pixiv 源（mock）下同样 404，客户端据此回落静态图
    pixiv_source.reset()
    assert client.get("/v1/images/px_111/ugoira", headers=auth).status_code == 404
    assert client.get("/v1/images/img_0001/ugoira", headers=auth).status_code == 404
