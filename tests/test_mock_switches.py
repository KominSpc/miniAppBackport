"""模拟行为开关：延迟、空数据、限流、服务错误。"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

LIST_PATHS = (
    "/v1/images/daily",
    "/v1/images/recommended",
    "/v1/images/search?q=初音",
    "/v1/images/favorites",
    "/v1/daily/bilibili",
    "/v1/games/today",
    "/v1/games",
    "/v1/pet/conversations",
    "/v1/users/me/history",
)


def test_empty_switch_clears_every_list_endpoint(client: TestClient, auth: dict[str, str]) -> None:
    headers = {**auth, "X-Mock-Empty": "true"}
    for path in LIST_PATHS:
        body = client.get(path, headers=headers).json()
        assert body["data"]["items"] == [], path
        assert body["meta"]["has_more"] is False, path
        assert body["meta"]["next_cursor"] is None, path


def test_empty_switch_does_not_break_detail_or_binary(client: TestClient, auth: dict[str, str]) -> None:
    headers = {**auth, "X-Mock-Empty": "true"}
    assert client.get("/v1/images/img_0001", headers=headers).json()["data"]["id"] == "img_0001"
    image = client.get("/v1/images/img_0001/file", headers=headers)
    assert image.status_code == 200
    assert image.content[:2] == b"\xff\xd8"


def test_rate_limited_switch_sets_retry_after(client: TestClient, auth: dict[str, str]) -> None:
    response = client.get("/v1/images/daily", headers={**auth, "X-Mock-Rate-Limited": "true"})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "5"
    body = response.json()
    assert body["error"]["code"] == "RATE_LIMITED"
    assert body["error"]["details"] == {"retry_after": 5}
    assert body["data"] is None


def test_error_switch_returns_internal_error(client: TestClient, auth: dict[str, str]) -> None:
    response = client.get("/v1/images/daily", headers={**auth, "X-Mock-Error": "true"})
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"


def test_latency_switch_delays_response(client: TestClient, auth: dict[str, str]) -> None:
    started = time.perf_counter()
    response = client.get("/v1/images/daily", headers={**auth, "X-Mock-Latency-Ms": "120"})
    elapsed = time.perf_counter() - started
    assert response.status_code == 200
    assert elapsed >= 0.1


def test_flags_apply_per_request(client: TestClient, auth: dict[str, str]) -> None:
    assert client.get("/v1/images/daily", headers=auth).status_code == 200
    assert client.get("/v1/images/daily", headers={**auth, "X-Mock-Error": "true"}).status_code == 500
    assert client.get("/v1/images/daily", headers=auth).status_code == 200


def test_env_settings_drive_health_echo(monkeypatch) -> None:
    monkeypatch.setenv("MOCK_EMPTY", "true")
    monkeypatch.setenv("MOCK_LATENCY_MS", "25")
    monkeypatch.setenv("DEV_RESET_ENABLED", "true")
    from app.config import load_settings

    settings = load_settings()
    assert settings.empty is True
    assert settings.latency_ms == 25
    assert settings.dev_reset_enabled is True


def test_env_invalid_values_fall_back(monkeypatch) -> None:
    monkeypatch.setenv("MOCK_LATENCY_MS", "abc")
    monkeypatch.setenv("MOCK_EMPTY", "maybe")
    from app.config import load_settings

    settings = load_settings()
    assert settings.latency_ms == 0
    assert settings.empty is False
