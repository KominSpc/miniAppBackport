"""Flutter web 调试依赖的跨域行为。"""

from __future__ import annotations

from fastapi.testclient import TestClient

DEV_ORIGINS = ("http://localhost:54321", "http://127.0.0.1:8080", "https://localhost:3000")


def test_preflight_allows_local_dev_server(client: TestClient) -> None:
    for origin in DEV_ORIGINS:
        response = client.options(
            "/v1/images/daily",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert response.status_code == 200, origin
        assert response.headers["access-control-allow-origin"] == origin
        assert "authorization" in response.headers["access-control-allow-headers"].lower()


def test_simple_request_reflects_origin(client: TestClient) -> None:
    response = client.get("/health", headers={"Origin": DEV_ORIGINS[0]})
    assert response.headers["access-control-allow-origin"] == DEV_ORIGINS[0]


def test_error_responses_keep_cors_headers(client: TestClient) -> None:
    response = client.get("/v1/images/daily", headers={"Origin": DEV_ORIGINS[0]})
    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == DEV_ORIGINS[0]


def test_expose_headers_include_retry_after(client: TestClient, auth: dict[str, str]) -> None:
    response = client.get(
        "/v1/images/daily",
        headers={**auth, "Origin": DEV_ORIGINS[1], "X-Mock-Rate-Limited": "true"},
    )
    assert response.status_code == 429
    exposed = response.headers["access-control-expose-headers"]
    assert "Retry-After" in exposed
    assert "X-Request-Id" in exposed


def test_binary_image_response_has_cors_headers(client: TestClient, auth: dict[str, str]) -> None:
    response = client.get(
        "/v1/images/img_0001/file",
        headers={**auth, "Origin": DEV_ORIGINS[0]},
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == DEV_ORIGINS[0]


def test_foreign_origin_is_not_allowed(client: TestClient) -> None:
    response = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers


def test_request_id_header_is_returned(client: TestClient) -> None:
    response = client.get("/health")
    assert response.headers["x-request-id"].startswith("req_")
