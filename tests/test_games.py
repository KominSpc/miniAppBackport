"""今日更新与游戏筛选。"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_today_games_are_all_flagged_today(client: TestClient, auth: dict[str, str]) -> None:
    items = client.get("/v1/games/today", headers=auth).json()["data"]["items"]
    assert len(items) == 6
    assert all(item["type"] == "game" for item in items)
    assert all(item["payload"]["today_updated"] is True for item in items)
    stamps = [item["payload"]["updated_at"] for item in items]
    assert stamps == sorted(stamps, reverse=True)


def test_games_pagination(client: TestClient, auth: dict[str, str]) -> None:
    page = client.get("/v1/games?limit=10", headers=auth).json()
    assert len(page["data"]["items"]) == 10
    assert page["meta"]["has_more"] is True

    everything = client.get("/v1/games?limit=50", headers=auth).json()
    assert len(everything["data"]["items"]) == 24
    assert everything["meta"]["has_more"] is False


def test_platform_filter(client: TestClient, auth: dict[str, str]) -> None:
    items = client.get("/v1/games?platform=Android&limit=50", headers=auth).json()["data"]["items"]
    assert items
    assert all("Android" in item["payload"]["platforms"] for item in items)
    assert client.get("/v1/games?platform=Switch&limit=50", headers=auth).json()["data"]["items"] == []


def test_genre_filter(client: TestClient, auth: dict[str, str]) -> None:
    items = client.get("/v1/games?genre=音乐&limit=50", headers=auth).json()["data"]["items"]
    assert items
    assert all(any("音乐" in genre for genre in item["payload"]["genres"]) for item in items)


def test_combined_filters(client: TestClient, auth: dict[str, str]) -> None:
    items = client.get("/v1/games?platform=Android&genre=音乐&limit=50", headers=auth).json()["data"]["items"]
    for item in items:
        assert "Android" in item["payload"]["platforms"]
        assert any("音乐" in genre for genre in item["payload"]["genres"])


def test_filter_change_invalidates_cursor(client: TestClient, auth: dict[str, str]) -> None:
    cursor = client.get("/v1/games?limit=5", headers=auth).json()["meta"]["next_cursor"]
    assert client.get(f"/v1/games?platform=Android&cursor={cursor}", headers=auth).status_code == 400


def test_games_requires_auth(client: TestClient) -> None:
    assert client.get("/v1/games").status_code == 401
    assert client.get("/v1/games/today").status_code == 401
