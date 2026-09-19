"""美图列表、搜索、收藏与图片代理。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

LIST_PATHS = (
    "/v1/images/daily",
    "/v1/images/recommended",
    "/v1/images/search?q=初音",
    "/v1/images/favorites",
)


def test_recommended_default_page_size(client: TestClient, auth: dict[str, str]) -> None:
    body = client.get("/v1/images/recommended", headers=auth).json()
    assert len(body["data"]["items"]) == 20
    assert body["meta"]["has_more"] is True
    assert body["meta"]["next_cursor"]


def test_pages_do_not_overlap(client: TestClient, auth: dict[str, str]) -> None:
    first = client.get("/v1/images/recommended?limit=10", headers=auth).json()
    second = client.get(
        f"/v1/images/recommended?limit=10&cursor={first['meta']['next_cursor']}", headers=auth
    ).json()
    first_ids = {item["id"] for item in first["data"]["items"]}
    second_ids = {item["id"] for item in second["data"]["items"]}
    assert first_ids & second_ids == set()
    assert len(second_ids) == 10


def test_last_page_has_no_cursor(client: TestClient, auth: dict[str, str]) -> None:
    body = client.get("/v1/images/recommended?limit=50", headers=auth).json()
    assert body["meta"]["has_more"] is False
    assert body["meta"]["next_cursor"] is None


def test_limit_out_of_range_is_validation_failed(client: TestClient, auth: dict[str, str]) -> None:
    for query in ("limit=0", "limit=51", "limit=abc", "limit=-3"):
        response = client.get(f"/v1/images/recommended?{query}", headers=auth)
        assert response.status_code == 422, query
        assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_limit_upper_bound_is_accepted(client: TestClient, auth: dict[str, str]) -> None:
    assert client.get("/v1/images/recommended?limit=50", headers=auth).status_code == 200


def test_malformed_cursor_is_bad_request(client: TestClient, auth: dict[str, str]) -> None:
    for cursor in ("!!!", "c1@@@@", "not-a-cursor", "c1" ):
        response = client.get(f"/v1/images/recommended?cursor={cursor}", headers=auth)
        assert response.status_code == 400, cursor
        assert response.json()["error"]["code"] == "BAD_REQUEST"


def test_cursor_is_bound_to_its_query(client: TestClient, auth: dict[str, str]) -> None:
    cursor = client.get("/v1/images/recommended?limit=5", headers=auth).json()["meta"]["next_cursor"]
    # 换一个 scope 复用同一游标应被拒绝，避免客户端串页
    assert client.get(f"/v1/games?cursor={cursor}", headers=auth).status_code == 400
    assert client.get(f"/v1/images/recommended?tag=桜&cursor={cursor}", headers=auth).status_code == 400


def test_search_hits_and_empty_result(client: TestClient, auth: dict[str, str]) -> None:
    hits = client.get("/v1/images/search?q=初音", headers=auth).json()
    assert hits["data"]["items"]
    assert all("初音" in json.dumps(item, ensure_ascii=False) for item in hits["data"]["items"])

    miss = client.get("/v1/images/search?q=zzzz", headers=auth).json()
    assert miss["data"]["items"] == []
    assert miss["meta"]["has_more"] is False
    assert miss["meta"]["next_cursor"] is None


def test_search_requires_non_empty_query(client: TestClient, auth: dict[str, str]) -> None:
    assert client.get("/v1/images/search", headers=auth).status_code == 422
    assert client.get("/v1/images/search?q=", headers=auth).status_code == 422


def test_tag_filter(client: TestClient, auth: dict[str, str]) -> None:
    body = client.get("/v1/images/recommended?tag=桜&limit=50", headers=auth).json()
    assert body["data"]["items"]
    assert all(
        any("桜" in tag for tag in item["tags"]) or "桜" in item["title"]
        for item in body["data"]["items"]
    )
    assert client.get("/v1/images/recommended?tag=不存在的标签&limit=50", headers=auth).json()["data"]["items"] == []


def test_safe_mode_hides_sensitive_content(client: TestClient, auth: dict[str, str]) -> None:
    ids = {item["id"] for item in client.get("/v1/images/recommended?limit=50", headers=auth).json()["data"]["items"]}
    assert "img_0005" not in ids
    assert client.get("/v1/images/img_0005", headers=auth).status_code == 404

    client.put(
        "/v1/users/me/preferences",
        json={"tags": [], "platforms": [], "genres": [], "safe_mode": False},
        headers=auth,
    )
    ids = {item["id"] for item in client.get("/v1/images/recommended?limit=50", headers=auth).json()["data"]["items"]}
    assert "img_0005" in ids
    assert client.get("/v1/images/img_0005", headers=auth).status_code == 200


def test_daily_images_are_stable_within_a_day(client: TestClient, auth: dict[str, str]) -> None:
    first = [item["id"] for item in client.get("/v1/images/daily", headers=auth).json()["data"]["items"]]
    second = [item["id"] for item in client.get("/v1/images/daily", headers=auth).json()["data"]["items"]]
    assert first == second
    assert len(first) == 8


def test_detail_returns_full_payload(client: TestClient, auth: dict[str, str]) -> None:
    item = client.get("/v1/images/img_0001", headers=auth).json()["data"]
    assert item["type"] == "image"
    assert item["title"] == "樱花下的少女"
    assert item["payload"]["author"]
    assert item["payload"]["width"] == 1400
    assert item["payload"]["height"] == 975
    assert item["payload"]["aspect_ratio"] == 1.4359
    assert item["payload"]["created_at"][-6:] == "+08:00"


def test_detail_unknown_id_is_not_found(client: TestClient, auth: dict[str, str]) -> None:
    response = client.get("/v1/images/nope_9999", headers=auth)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert response.json()["data"] is None


def test_all_urls_are_same_origin(client: TestClient, auth: dict[str, str]) -> None:
    items = client.get("/v1/images/recommended?limit=50", headers=auth).json()["data"]["items"]
    for item in items:
        assert item["cover_url"].startswith("http://testserver/")
        assert item["source_url"].startswith("http://testserver/")
        assert item["payload"]["thumbnail_url"].startswith("http://testserver/")
        assert item["payload"]["image_url"].startswith("http://testserver/")
        assert "pximg" not in item["cover_url"]


def test_favorite_is_idempotent(client: TestClient, auth: dict[str, str]) -> None:
    first = client.put("/v1/images/img_0001/favorite", headers=auth).json()["data"]
    assert first == {"id": "img_0001", "is_favorited": True, "favorite_count": 1}

    again = client.put("/v1/images/img_0001/favorite", headers=auth).json()["data"]
    assert again == first

    removed = client.delete("/v1/images/img_0001/favorite", headers=auth).json()["data"]
    assert removed == {"id": "img_0001", "is_favorited": False, "favorite_count": 0}

    removed_again = client.delete("/v1/images/img_0001/favorite", headers=auth).json()["data"]
    assert removed_again == removed


def test_favorite_state_is_reflected_in_lists(client: TestClient, auth: dict[str, str]) -> None:
    client.put("/v1/images/img_0002/favorite", headers=auth)
    detail = client.get("/v1/images/img_0002", headers=auth).json()["data"]
    assert detail["payload"]["is_favorited"] is True
    favorites = client.get("/v1/images/favorites", headers=auth).json()["data"]["items"]
    assert [item["id"] for item in favorites] == ["img_0002"]


def test_favorites_are_isolated_per_user(
    client: TestClient, auth: dict[str, str], second_user: dict[str, str]
) -> None:
    client.put("/v1/images/img_0001/favorite", headers=auth)
    assert client.get("/v1/images/favorites", headers=second_user).json()["data"]["items"] == []
    assert client.get("/v1/images/img_0001", headers=second_user).json()["data"]["payload"]["is_favorited"] is False


def test_favorite_unknown_id_is_not_found(client: TestClient, auth: dict[str, str]) -> None:
    assert client.put("/v1/images/nope_0001/favorite", headers=auth).status_code == 404
    assert client.delete("/v1/images/nope_0001/favorite", headers=auth).status_code == 404


def test_image_file_serves_jpeg(client: TestClient, auth: dict[str, str]) -> None:
    for variant in ("thumb", "regular", "original"):
        response = client.get(f"/v1/images/img_0001/file?variant={variant}", headers=auth)
        assert response.status_code == 200, variant
        assert response.headers["content-type"] == "image/jpeg"
        assert response.content[:2] == b"\xff\xd8"
        assert response.headers["cache-control"] == "public, max-age=3600"


def test_image_file_page_parameter(client: TestClient, auth: dict[str, str]) -> None:
    assert client.get("/v1/images/img_0001/file", headers=auth).status_code == 200
    assert client.get("/v1/images/img_0001/file?variant=original&page=1", headers=auth).status_code == 200
    # img_0001 只有 2 页
    assert client.get("/v1/images/img_0001/file?variant=original&page=2", headers=auth).status_code == 404
    assert client.get("/v1/images/img_0001/file?variant=original&page=-1", headers=auth).status_code == 422


def test_image_file_bad_variant_and_unknown_id(client: TestClient, auth: dict[str, str]) -> None:
    assert client.get("/v1/images/img_0001/file?variant=huge", headers=auth).status_code == 422
    assert client.get("/v1/images/nope_0001/file", headers=auth).status_code == 404


def test_image_file_requires_auth(client: TestClient) -> None:
    response = client.get("/v1/images/img_0001/file")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_list_endpoints_require_auth(client: TestClient) -> None:
    for path in LIST_PATHS:
        assert client.get(path).status_code == 401, path
