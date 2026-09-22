"""检索匹配：中文分词、拼音（全拼 / 首字母）与别名。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.services import catalog, search

BASE = "http://testserver"


def _ids(client: TestClient, auth: dict[str, str], keyword: str) -> set[str]:
    response = client.get("/v1/images/search", params={"q": keyword}, headers=auth)
    assert response.status_code == 200, response.text
    return {item["id"] for item in response.json()["data"]["items"]}


def test_tokens_split_on_whitespace_and_punctuation() -> None:
    assert search.tokens(" 樱花，少女 ") == ["樱花", "少女"]
    assert search.tokens("sakura, miku") == ["sakura", "miku"]
    assert search.tokens("，。！") == []


def test_normalize_folds_width_and_case() -> None:
    assert search.normalize("ＶＯＣＡＬＯＩＤ") == "vocaloid"
    assert search.normalize("初音 ミク") == "初音ミク"


def test_pinyin_full_and_initials_match(client: TestClient, auth: dict[str, str]) -> None:
    assert "img_0001" in _ids(client, auth, "樱花下的少女")
    assert "img_0001" in _ids(client, auth, "yinghua")
    assert "img_0001" in _ids(client, auth, "yhxd")


def test_alias_and_romaji_match(client: TestClient, auth: dict[str, str]) -> None:
    # 夹具里的标签是日文「初音ミク」，中文名与罗马字都应命中同一条
    assert "img_0001" in _ids(client, auth, "初音未来")
    assert "img_0001" in _ids(client, auth, "miku")
    assert "img_0001" in _ids(client, auth, "hatsune miku")


def test_author_pinyin_matches(client: TestClient, auth: dict[str, str]) -> None:
    # 作者「澄空Aoi」的全拼是 chengkongaoi
    assert "img_0001" in _ids(client, auth, "chengkong")


def test_multi_token_query_requires_every_token(client: TestClient, auth: dict[str, str]) -> None:
    assert _ids(client, auth, "樱花 少女") == {"img_0001"}
    assert _ids(client, auth, "樱花 便利店") == set()


def test_search_is_case_insensitive(client: TestClient, auth: dict[str, str]) -> None:
    assert _ids(client, auth, "VOCALOID") == _ids(client, auth, "vocaloid")


def test_unknown_keyword_returns_empty_page(client: TestClient, auth: dict[str, str]) -> None:
    response = client.get("/v1/images/search", params={"q": "zzz不存在zzz"}, headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["items"] == []
    assert body["meta"]["has_more"] is False
    assert body["meta"]["next_cursor"] is None


def test_tag_filter_accepts_alias_and_pinyin() -> None:
    items = catalog.all_images(BASE, safe_mode=False)
    assert catalog.filter_by_tag(items, "原创") == catalog.filter_by_tag(items, "オリジナル")
    assert catalog.filter_by_tag(items, "sakura") == catalog.filter_by_tag(items, "桜")
    assert catalog.filter_by_tag(items, "yuanchuang") == catalog.filter_by_tag(items, "原创")


def test_search_keeps_catalog_order() -> None:
    hits = catalog.search_images(BASE, "风景", safe_mode=False)
    expected = [
        item for item in catalog.all_images(BASE, safe_mode=False)
        if item["id"] in {hit["id"] for hit in hits}
    ]
    assert [hit["id"] for hit in hits] == [item["id"] for item in expected]