"""匿名用户生命周期、偏好与历史。"""

from __future__ import annotations

from fastapi.testclient import TestClient



def test_anonymous_creates_new_user(client: TestClient) -> None:
    response = client.post("/v1/users/anonymous", json={"platform": "android", "app_version": "1.0.0"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["token_type"] == "Bearer"
    assert data["access_token"]
    assert data["expires_at"][-6:] == "+08:00"
    assert data["preferences"] == {"tags": [], "platforms": [], "genres": [], "safe_mode": True}


def test_anonymous_request_body_is_optional(client: TestClient) -> None:
    assert client.post("/v1/users/anonymous").status_code == 200


def test_install_id_reuses_user_and_rotates_token(client: TestClient) -> None:
    first = client.post("/v1/users/anonymous", json={"install_id": "install-1"}).json()["data"]
    second = client.post("/v1/users/anonymous", json={"install_id": "install-1"}).json()["data"]
    assert first["user_id"] == second["user_id"]
    assert first["access_token"] != second["access_token"]
    # 旧令牌在轮换后失效
    stale = {"Authorization": f"Bearer {first['access_token']}"}
    assert client.get("/v1/users/me/preferences", headers=stale).status_code == 401


def test_missing_token_is_unauthorized(client: TestClient) -> None:
    response = client.get("/v1/users/me/preferences")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_garbage_token_is_unauthorized(client: TestClient) -> None:
    response = client.get("/v1/users/me/preferences", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_malformed_authorization_header(client: TestClient) -> None:
    response = client.get("/v1/users/me/preferences", headers={"Authorization": "Token abc"})
    assert response.status_code == 401


def test_expired_token_reports_token_expired(client: TestClient, anonymous: dict, expire_token) -> None:
    expire_token(anonymous["access_token"])
    response = client.get(
        "/v1/users/me/preferences",
        headers={"Authorization": f"Bearer {anonymous['access_token']}"},
    )
    assert response.status_code == 401
    # UNAUTHORIZED 与 TOKEN_EXPIRED 都是 401，必须靠 code 区分
    assert response.json()["error"]["code"] == "TOKEN_EXPIRED"


def test_preferences_roundtrip(client: TestClient, auth: dict[str, str]) -> None:
    payload = {"tags": ["初音ミク"], "platforms": ["Android"], "genres": ["音乐"], "safe_mode": False}
    response = client.put("/v1/users/me/preferences", json=payload, headers=auth)
    assert response.status_code == 200
    assert response.json()["data"] == payload
    assert client.get("/v1/users/me/preferences", headers=auth).json()["data"] == payload


def test_preferences_reject_bad_type(client: TestClient, auth: dict[str, str]) -> None:
    response = client.put("/v1/users/me/preferences", json={"tags": "初音ミク"}, headers=auth)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    assert response.json()["error"]["details"]["fields"]


def test_preferences_ignore_unknown_fields(client: TestClient, auth: dict[str, str]) -> None:
    """契约未声明的字段被忽略而不是报错，便于客户端灰度升级。"""
    response = client.put(
        "/v1/users/me/preferences",
        json={"tags": ["初音ミク"], "unknown_field": 1},
        headers=auth,
    )
    assert response.status_code == 200
    assert "unknown_field" not in response.json()["data"]


def test_history_records_search_and_browse(client: TestClient, auth: dict[str, str]) -> None:
    client.get("/v1/images/search?q=初音", headers=auth)
    client.get("/v1/images/img_0001", headers=auth)

    entries = client.get("/v1/users/me/history", headers=auth).json()["data"]["items"]
    kinds = {entry["kind"] for entry in entries}
    assert kinds == {"browse", "search"}
    search_entry = next(entry for entry in entries if entry["kind"] == "search")
    assert search_entry["query"] == "初音"
    browse_entry = next(entry for entry in entries if entry["kind"] == "browse")
    assert browse_entry["content"]["id"] == "img_0001"


def test_history_kind_filter_and_delete(client: TestClient, auth: dict[str, str]) -> None:
    client.get("/v1/images/search?q=初音", headers=auth)
    client.get("/v1/images/img_0001", headers=auth)

    only_search = client.get("/v1/users/me/history?kind=search", headers=auth).json()["data"]["items"]
    assert [entry["kind"] for entry in only_search] == ["search"]

    removed = client.delete("/v1/users/me/history/search", headers=auth).json()["data"]
    assert removed == {"deleted": 1, "kind": "search"}
    remaining = client.get("/v1/users/me/history", headers=auth).json()["data"]["items"]
    assert [entry["kind"] for entry in remaining] == ["browse"]


def test_history_kind_rejects_unknown_value(client: TestClient, auth: dict[str, str]) -> None:
    response = client.get("/v1/users/me/history?kind=other", headers=auth)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_history_is_per_user(client: TestClient, auth: dict[str, str], second_user: dict[str, str]) -> None:
    client.get("/v1/images/img_0002", headers=auth)
    assert client.get("/v1/users/me/history", headers=second_user).json()["data"]["items"] == []


