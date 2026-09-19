"""逐条校验产出的 ContentItem：顶层结构与 payload 形状都要能被模型接受。"""

from __future__ import annotations

from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.schemas.content import CardPayload, ContentItem, GamePayload, ImagePayload, VideoPayload

PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "image": ImagePayload,
    "video": VideoPayload,
    "game": GamePayload,
    "card": CardPayload,
}


def collect_items(client: TestClient, auth: dict[str, str]) -> list[dict]:
    client.put(
        "/v1/users/me/preferences",
        json={"tags": [], "platforms": [], "genres": [], "safe_mode": False},
        headers=auth,
    )
    items: list[dict] = []
    items += client.get("/v1/images/recommended?limit=50", headers=auth).json()["data"]["items"]
    items += client.get("/v1/images/daily", headers=auth).json()["data"]["items"]
    items += client.get("/v1/daily/bilibili", headers=auth).json()["data"]["items"]
    items += client.get("/v1/games?limit=50", headers=auth).json()["data"]["items"]
    items += client.get("/v1/games/today", headers=auth).json()["data"]["items"]
    items += client.post("/v1/pet/chat", json={"message": "讲个冷知识"}, headers=auth).json()["data"]["suggestions"]
    return items


def test_all_items_validate_against_models(client: TestClient, auth: dict[str, str]) -> None:
    items = collect_items(client, auth)
    assert items
    for item in items:
        ContentItem.model_validate(item)
        model = PAYLOAD_MODELS[item["type"]]
        model.model_validate(item["payload"])


def test_all_content_types_are_covered(client: TestClient, auth: dict[str, str]) -> None:
    types = {item["type"] for item in collect_items(client, auth)}
    assert types == {"image", "video", "game", "card"}


def test_payload_extra_fields_survive_validation(client: TestClient, auth: dict[str, str]) -> None:
    """video 的 payload 额外带 is_favorited，模型必须允许扩展字段。"""
    videos = [
        item for item in collect_items(client, auth) if item["type"] == "video"
    ]
    assert all("is_favorited" in item["payload"] for item in videos)
