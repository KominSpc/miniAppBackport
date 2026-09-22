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


def test_genre_filter_is_exact_not_substring(client: TestClient, auth: dict[str, str]) -> None:
    """「模拟」只命中「模拟」自身，不能顺带命中「模拟经营」。"""
    items = client.get("/v1/games?genre=模拟&limit=50", headers=auth).json()["data"]["items"]
    assert items
    assert all("模拟" in item["payload"]["genres"] for item in items)
    assert all("模拟经营" not in item["payload"]["genres"] for item in items)

    narrow = client.get("/v1/games?genre=模拟经营&limit=50", headers=auth).json()["data"]["items"]
    assert len(narrow) == 3
    assert all("模拟经营" in item["payload"]["genres"] for item in narrow)


def test_game_filters_cover_every_option(client: TestClient, auth: dict[str, str]) -> None:
    """筛选条选项统计自全部游戏，与当前筛选条件无关。"""
    body = client.get("/v1/games/filters", headers=auth).json()["data"]
    platforms = {row["value"]: row["count"] for row in body["platforms"]}
    genres = {row["value"]: row["count"] for row in body["genres"]}
    assert platforms == {"Android": 24, "iOS": 15, "PC": 6}
    assert len(genres) == 32
    assert genres["角色扮演"] == 4
    assert genres["模拟"] == 1
    assert genres["模拟经营"] == 3


def test_game_filters_counts_match_filtered_pages(client: TestClient, auth: dict[str, str]) -> None:
    """计数与真实筛选一致：点按钮后的列表长度等于按钮上的数量。"""
    body = client.get("/v1/games/filters", headers=auth).json()["data"]
    for row in body["platforms"]:
        page = client.get("/v1/games", params={"platform": row["value"], "limit": 50}, headers=auth).json()
        assert len(page["data"]["items"]) == row["count"], row["value"]
    for row in body["genres"]:
        page = client.get("/v1/games", params={"genre": row["value"], "limit": 50}, headers=auth).json()
        assert len(page["data"]["items"]) == row["count"], row["value"]


def test_game_filters_sorting_is_stable(client: TestClient, auth: dict[str, str]) -> None:
    body = client.get("/v1/games/filters", headers=auth).json()["data"]
    for rows in (body["platforms"], body["genres"]):
        keys = [(-row["count"], row["value"]) for row in rows]
        assert keys == sorted(keys)
    again = client.get("/v1/games/filters", headers=auth).json()["data"]
    assert again == body


def test_game_filters_requires_auth(client: TestClient) -> None:
    assert client.get("/v1/games/filters").status_code == 401
