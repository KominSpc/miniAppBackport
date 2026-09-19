"""健康检查与开发重置。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import MOCK_CONTENT_VERSION
from app.core.timeutil import TIMEZONE_NAME


def test_health_shape(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["error"] is None
    assert body["meta"]["source"] == "mock"
    assert body["meta"]["version"] == MOCK_CONTENT_VERSION
    assert body["meta"]["request_id"].startswith("req_")
    assert len(body["meta"]["request_id"]) == 12

    data = body["data"]
    assert data["status"] == "ok"
    assert data["timezone"] == TIMEZONE_NAME
    assert set(data["mock"]) == {"latency_ms", "empty", "rate_limited", "error", "dev_reset_enabled"}
    assert data["time"][-6:] in {"+08:00"}


def test_health_is_public_and_echoes_switches(client: TestClient) -> None:
    body = client.get("/health", headers={"X-Mock-Empty": "true"}).json()
    assert body["data"]["mock"]["empty"] is True


def test_health_survives_error_switch(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Mock-Error": "true"})
    assert response.status_code == 200
    assert response.json()["data"]["mock"]["error"] is True


def test_dev_reset_forbidden_by_default(client: TestClient, auth: dict[str, str]) -> None:
    response = client.post("/v1/dev/reset", headers=auth)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_dev_reset_requires_matching_admin_token(client: TestClient, auth: dict[str, str]) -> None:
    from app.main import app as fastapi_app

    settings = fastapi_app.state.settings
    object.__setattr__(settings, "dev_reset_enabled", True)
    try:
        response = client.post("/v1/dev/reset", headers=auth)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHORIZED"
        wrong = client.post("/v1/dev/reset", headers={**auth, "X-Admin-Token": "nope"})
        assert wrong.status_code == 401
    finally:
        object.__setattr__(settings, "dev_reset_enabled", False)


def test_dev_reset_clears_data(client: TestClient, auth: dict[str, str]) -> None:
    from app.main import app as fastapi_app

    settings = fastapi_app.state.settings
    object.__setattr__(settings, "dev_reset_enabled", True)
    try:
        client.put("/v1/images/img_0001/favorite", headers=auth)
        response = client.post(
            "/v1/dev/reset",
            headers={**auth, "X-Admin-Token": settings.admin_token},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["cleared"]["users"] == 1
        assert data["cleared"]["favorites"] == 1
        assert data["reset_at"][-6:] == "+08:00"
        assert client.get("/v1/images/daily", headers=auth).status_code == 401
    finally:
        object.__setattr__(settings, "dev_reset_enabled", False)

