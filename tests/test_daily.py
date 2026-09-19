"""每日冷知识与 B 站热点。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.timeutil import today


def test_fact_is_stable_for_same_date(client: TestClient, auth: dict[str, str]) -> None:
    first = client.get("/v1/daily/fact?date=2026-09-19", headers=auth).json()["data"]
    second = client.get("/v1/daily/fact?date=2026-09-19", headers=auth).json()["data"]
    assert first == second
    assert first["date"] == "2026-09-19"
    assert first["content"]
    assert first["tags"]


def test_fact_varies_across_dates(client: TestClient, auth: dict[str, str]) -> None:
    contents = set()
    for day in range(1, 21):
        body = client.get(f"/v1/daily/fact?date=2026-08-{day:02d}", headers=auth).json()
        contents.add(body["data"]["content"])
    assert len(contents) > 1


def test_fact_defaults_to_shanghai_today(client: TestClient, auth: dict[str, str]) -> None:
    body = client.get("/v1/daily/fact", headers=auth).json()
    assert body["data"]["date"] == today().isoformat()


def test_fact_rejects_invalid_date(client: TestClient, auth: dict[str, str]) -> None:
    response = client.get("/v1/daily/fact?date=2026-13-45", headers=auth)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_bilibili_is_sorted_by_hot_score(client: TestClient, auth: dict[str, str]) -> None:
    items = client.get("/v1/daily/bilibili", headers=auth).json()["data"]["items"]
    assert len(items) == 12
    assert all(item["type"] == "video" for item in items)
    scores = [item["payload"]["hot_score"] for item in items]
    assert scores == sorted(scores, reverse=True)
    assert all(item["payload"]["bvid"].startswith("BV1") for item in items)


def test_bilibili_requires_auth(client: TestClient) -> None:
    assert client.get("/v1/daily/bilibili").status_code == 401
