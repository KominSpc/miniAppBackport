"""漫画页：站点降级、熔断、关键词兜底、合并去重、标签回落与图片转发。

真实上游的用例全部注入 ``httpx.MockTransport``：不产生真实网络请求。
上游 HTTP 形状取自 ``mini_app/tools/comic_service.py``（Flask 包装 ComicDown）：
``/api/<site>/search`` 返回 ``{"search_result": [...]}``、``/api/<site>/latest`` 返回
``{"latest": [...]}``、``/api/<site>/tags`` 返回 ``{"tags": ...}``，
爬虫报错时回 404 ``{"error": "NOT_FOUND"}``。
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from app.config import Settings
from app.services.comic import source as comic_source
from app.services.comic.client import ComicClient


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "comic_source": "node",
        "comic_api_base": "http://comic.test",
        "comic_sites": ("manhuagui", "qq", "dm5"),
        "comic_timeout_seconds": 8.0,
        "comic_max_retries": 0,
        "comic_retry_initial_seconds": 0.01,
        "comic_cache_ttl_seconds": 60,
        "comic_site_cooldown_seconds": 180,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def comic_raw(name: str, comicid: str, *, cover: str | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "comicid": comicid,
        "cover_image_url": cover or f"//cf.mhgui.com/cpic/b/{comicid}.jpg",
        "source_url": f"https://www.manhuagui.com/comic/{comicid}/",
        "status": "连载中",
    }


class Upstream:
    """按 (site, action) 记账的假上游，便于断言「谁被打了、打了几次」。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.handlers: dict[str, Any] = {}

    def on(self, key: str, handler: Any) -> "Upstream":
        self.handlers[key] = handler
        return self

    def reply(self, key: str, payload: dict[str, Any]) -> "Upstream":
        return self.on(key, lambda request: httpx.Response(200, json=payload))

    def timeout(self, key: str) -> "Upstream":
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("站点被墙（模拟）", request=request)

        return self.on(key, handler)

    def status(self, key: str, code: int) -> "Upstream":
        return self.on(key, lambda request: httpx.Response(code, text="boom"))

    def hits(self, key: str) -> int:
        return sum(1 for call in self.calls if call == key)

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            site = path.split("/")[2] if path.startswith("/api/") else ""
            action = path.rsplit("/", 1)[-1]
            if path.startswith("/api/") and path.endswith("/comic"):
                action = "comic"
            if path.startswith("/api/") and action.isdigit():
                action = "chapter"
            key = f"{site}:{action}"
            self.calls.append(key)
            known = self.handlers.get(key)
            if known is None:
                return httpx.Response(404, json={"error": "NOT_FOUND", "message": key})
            return known(request)

        return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def clean_comic_source():
    comic_source.reset()
    yield
    comic_source.reset()


def install(upstream: Upstream, **overrides: Any) -> Settings:
    config = settings(**overrides)
    http = httpx.Client(transport=upstream.transport(), base_url="http://comic.test")
    comic_source.configure(config, client=ComicClient(config, client=http))
    return config


# ---------------------------------------------------------------- 搜索顺序与熔断


def test_search_falls_back_when_first_site_is_unreachable() -> None:
    """漫画柜连不上就换腾讯：第一个**有结果**的站点胜出，并把 site 带回客户端。"""
    upstream = Upstream()
    upstream.timeout("manhuagui:search")
    upstream.reply("qq:search", {"search_result": [comic_raw("航海王", "657992")]})
    install(upstream)

    items, matched = comic_source.search("航海王")
    assert matched == "qq"
    assert [item["id"] for item in items] == ["qq:657992"]
    assert upstream.hits("manhuagui:search") == 1
    assert upstream.hits("dm5:search") == 0


def test_unreachable_site_is_skipped_while_cooling_down() -> None:
    """熔断：站点连不上后，冷却期内不再白等一个超时（这正是「漫画加载超时」的根因）。"""
    upstream = Upstream()
    upstream.timeout("manhuagui:search")
    upstream.reply("qq:search", {"search_result": [comic_raw("航海王", "657992")]})
    install(upstream)

    comic_source.search("航海王")
    assert comic_source.is_down("manhuagui") is True

    _, matched = comic_source.search("航海王", page=2)
    assert matched == "qq"
    # 第二次搜索直接跳过漫画柜
    assert upstream.hits("manhuagui:search") == 1


def test_site_is_retried_after_cooldown() -> None:
    """冷却到期后再试一次；成功即恢复。"""
    upstream = Upstream()
    upstream.timeout("manhuagui:search")
    upstream.reply("qq:search", {"search_result": [comic_raw("航海王", "657992")]})
    install(upstream, comic_site_cooldown_seconds=0)

    comic_source.search("航海王")
    comic_source.search("航海王")
    # 冷却为 0：每次都重新试漫画柜（第一次成功后再打一次才能确认「恢复」）
    assert upstream.hits("manhuagui:search") == 2
    assert comic_source.is_down("manhuagui") is False


def test_upstream_5xx_does_not_trip_the_breaker() -> None:
    """上游 500 不熔断（dm5 对无关关键词就 500），否则会把本来能用的站点一起摘掉。"""
    upstream = Upstream()
    upstream.status("manhuagui:search", 500)
    upstream.reply("qq:search", {"search_result": [comic_raw("航海王", "657992")]})
    install(upstream)

    comic_source.search("航海王")
    assert comic_source.is_down("manhuagui") is False


def test_hot_list_fallback_is_filtered_out() -> None:
    """腾讯搜不到时会回落成一堆无关热门：标题不含关键词就不当结果，继续换站点。"""
    upstream = Upstream()
    upstream.reply(
        "qq:search",
        {"search_result": [comic_raw("Take me out", "1"), comic_raw("TA.TA", "2")]},
    )
    upstream.reply("dm5:search", {"search_result": [comic_raw("冥府深渊", "3")]})
    install(upstream)

    items, matched = comic_source.search("冥府深渊")
    assert matched == "dm5"
    assert [item["id"] for item in items] == ["dm5:3"]
    assert upstream.hits("dm5:search") == 1


def test_search_returns_empty_when_no_site_has_the_keyword() -> None:
    """全部站点都只有无关结果时，老老实实回空列表（界面显示「没有找到」，不糊弄）。"""
    upstream = Upstream()
    upstream.reply("qq:search", {"search_result": [comic_raw("Take me out", "1")]})
    upstream.reply("dm5:search", {"search_result": [comic_raw("TA.TA", "2")]})
    install(upstream)

    items, matched = comic_source.search("zzz-not-a-real-comic")
    assert items == []
    assert matched is None


def test_explicit_site_is_used_directly() -> None:
    """客户端带 site 翻页时只用该站点，不再降级（否则第 2 页会落到别的站点）。"""
    upstream = Upstream()
    upstream.reply("qq:search", {"search_result": [comic_raw("航海王", "657992")]})
    install(upstream)

    _, matched = comic_source.search("one piece", site="qq", page=2)
    assert matched == "qq"
    assert upstream.hits("manhuagui:search") == 0
    assert upstream.hits("qq:search") == 1


# ---------------------------------------------------------------- 列表 / 标签


def test_latest_merges_sites_and_dedupes() -> None:
    upstream = Upstream()
    upstream.reply("manhuagui:latest", {"latest": [comic_raw("甲", "1")]})
    upstream.reply("qq:latest", {"latest": [comic_raw("乙", "2"), comic_raw("乙", "2")]})
    upstream.reply("dm5:latest", {"latest": [comic_raw("丙", "3")]})
    install(upstream)

    items = comic_source.latest()
    # 同一站点里的重复条目只留一条（榜单首页常有的重复推荐位）
    assert [item["id"] for item in items] == ["manhuagui:1", "qq:2", "dm5:3"]


def test_latest_skips_a_broken_site() -> None:
    upstream = Upstream()
    upstream.timeout("manhuagui:latest")
    upstream.reply("qq:latest", {"latest": [comic_raw("乙", "2")]})
    install(upstream)

    items = comic_source.latest()
    assert [item["id"] for item in items] == ["qq:2"]


def test_tags_fall_back_to_the_next_site() -> None:
    """漫画柜挂了也要能出标签，否则整条标签栏空着（界面会当成「没有分类」）。"""
    upstream = Upstream()
    upstream.timeout("manhuagui:tags")
    upstream.reply(
        "qq:tags",
        {"tags": [{"category": "热血", "tags": [{"name": "冒险", "tag": "冒险"}]}]},
    )
    install(upstream)

    groups = comic_source.tags()
    assert upstream.hits("manhuagui:tags") == 1
    assert upstream.hits("qq:tags") == 1
    assert groups, "标签组不能为空"
    assert groups[0]["category"] == "热血"
    assert [tag["tag"] for tag in groups[0]["tags"]] == ["冒险"]


# ---------------------------------------------------------------- 图片转发


def test_image_proxy_adds_referer_for_manhuagui_only() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"\xff\xd8\xff", headers={"content-type": "image/jpeg"})

    config = settings()
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://comic.test")
    comic_source.configure(config, client=ComicClient(config, client=http))

    body, content_type = comic_source.image_bytes("https://i.hamreus.com/h/1128/1.jpg?e=1&m=2")
    assert body and content_type == "image/jpeg"
    assert seen[0].headers.get("referer") == "https://www.manhuagui.com/"

    comic_source.image_bytes("https://manhua.acimg.cn/vertical/0/cover.jpg/420")
    assert "referer" not in {key.lower() for key in seen[1].headers}


def test_image_proxy_rejects_unknown_host() -> None:
    from app.core.errors import AppError

    config = settings()
    http = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x")),
        base_url="http://comic.test",
    )
    comic_source.configure(config, client=ComicClient(config, client=http))

    with pytest.raises(AppError) as error:
        comic_source.image_bytes("https://evil.example.com/a.jpg")
    assert error.value.details["reason"] == "unexpected_image_host"


# ---------------------------------------------------------------- 关键词判定


@pytest.mark.parametrize(
    ("title", "keyword", "expected"),
    [
        ("ONE PIECE航海王", "one piece", True),
        ("ONE PIECE航海王", "onepiece", True),
        ("honey-trapper 甜心布偶", "Honey Trapper", True),
        ("Take me out", "冥府深渊", False),
        ("TA.TA", "one piece", False),
    ],
)
def test_keyword_matches(title: str, keyword: str, expected: bool) -> None:
    assert comic_source.keyword_matches({"title": title}, keyword) is expected


def test_search_cache_makes_the_second_call_free() -> None:
    upstream = Upstream()
    upstream.reply("manhuagui:search", {"search_result": [comic_raw("航海王", "657992")]})
    install(upstream)

    comic_source.search("航海王")
    comic_source.search("航海王")
    assert upstream.hits("manhuagui:search") == 1


def test_cooldown_expiry_resets_after_time_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """冷却记录存的是到期时刻：把时钟往后拨，站点应重新参与取数。"""
    upstream = Upstream()
    upstream.timeout("manhuagui:latest")
    upstream.reply("qq:latest", {"latest": [comic_raw("乙", "2")]})
    install(upstream, comic_site_cooldown_seconds=1)

    comic_source.latest()
    assert comic_source.is_down("manhuagui") is True

    future = time.monotonic() + 5
    monkeypatch.setattr(comic_source.time, "monotonic", lambda: future)
    assert comic_source.is_down("manhuagui") is False
